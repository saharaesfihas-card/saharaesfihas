"""Verificação das operações reais, isoladas em SQLite temporária."""

from contextlib import contextmanager
from concurrent.futures import ThreadPoolExecutor
import json
import sqlite3
import tempfile
import unittest
import uuid

from fastapi import HTTPException
from fastapi.testclient import TestClient

from server.core import hash_password, utcnow
from server.main import create_app
from server.operations import apply_stock, has_receivable_payments, record_sale, release_stock


class OperationsTests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.app = create_app(
            data_dir=self.directory.name,
            admin_password_hash=hash_password("test-only"),
            secure_cookie=False,
        )
        self.client = TestClient(self.app)
        self.client.__enter__()
        login = self.client.post("/api/admin/login", json={"password": "test-only"})
        self.assertEqual(login.status_code, 200, login.text)
        self.headers = {"X-Sahara-CSRF": login.json()["csrf_token"]}

    def tearDown(self):
        self.client.__exit__(None, None, None)
        self.directory.cleanup()

    @contextmanager
    def connection(self):
        conn = sqlite3.connect(self.app.state.db_path)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA foreign_keys=ON")
        try:
            conn.execute("BEGIN IMMEDIATE")
            yield conn
            conn.commit()
        except BaseException:
            conn.rollback()
            raise
        finally:
            conn.close()

    def post(self, path, data):
        if path.startswith("/receivables/") and path.endswith("/payments"):
            data = {"idempotency_key": uuid.uuid4().hex, **data}
        return self.client.post("/api/admin" + path, json=data, headers=self.headers)

    def get(self, path):
        response = self.client.get("/api/admin" + path)
        self.assertEqual(response.status_code, 200, response.text)
        return response.json()

    def product(self):
        identifier = "test-" + str(uuid.uuid4())
        with self.connection() as conn:
            conn.execute("INSERT INTO products VALUES (?,?,?,?)", (identifier, "Esfiha de teste", "Teste", 400))
        return identifier

    def inventory(self, label, quantity, product_id=None):
        data = {"label": label, "unit": "un", "on_hand": quantity, "low_threshold": 1}
        if product_id:
            data["product_id"] = product_id
        response = self.post("/inventory", data)
        self.assertEqual(response.status_code, 200, response.text)
        return next(item for item in response.json()["items"] if item["label"] == label)

    def order(self, items, *, payment_status="unpaid", total_cents=1200, status="new", delivery=None):
        identifier = str(uuid.uuid4())
        with self.connection() as conn:
            conn.execute(
                """INSERT INTO orders (
                  id,created_at,customer_name,customer_phone,delivery_json,items_json,subtotal_cents,
                  total_cents,status,payment_status,tracking_token,idempotency_key,request_hash)
                  VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                (identifier, utcnow(), "Cliente de teste", "44999999999",
                 json.dumps(delivery or {"street": "Rua das Palmeiras", "house_number": "25", "city": "Maringá"}),
                 json.dumps(items), total_cents, total_cents, status, payment_status,
                 uuid.uuid4().hex, uuid.uuid4().hex, "test"),
            )
        return identifier

    def stock(self, identifier):
        return next(item["on_hand"] for item in self.get("/inventory")["items"] if item["id"] == identifier)

    def test_private_operations_and_csrf(self):
        with TestClient(self.app) as visitor:
            self.assertEqual(visitor.get("/api/admin/inventory").status_code, 401)
        self.assertEqual(self.client.post("/api/admin/inventory", json={"label": "Farinha"}).status_code, 403)
        self.assertEqual(self.get("/inventory")["items"], [])
        self.assertEqual(self.get("/cash/entries")["entries"], [])
        self.assertEqual(self.get("/drivers")["drivers"], [])

    def test_linked_receivable_requires_total_and_rejects_paid_or_cancelled(self):
        identifier = self.order([], total_cents=1000)
        self.assertEqual(self.post("/receivables", {
            "customer_name": "Cliente", "amount_cents": 999, "order_id": identifier,
        }).status_code, 422)
        response = self.post("/receivables", {
            "customer_name": "Cliente", "amount_cents": 1000, "order_id": identifier,
        })
        self.assertEqual(response.status_code, 200, response.text)
        self.assertEqual(self.post("/receivables", {
            "customer_name": "Duplicado", "amount_cents": 1000, "order_id": identifier,
        }).status_code, 409)
        for state in ({"payment_status": "paid"}, {"status": "cancelled"}):
            invalid = self.order([], total_cents=400, **state)
            self.assertEqual(self.post("/receivables", {
                "customer_name": "Cliente", "amount_cents": 400, "order_id": invalid,
            }).status_code, 409)

    def test_linked_partial_receipts_block_direct_payment_without_duplicate_cash(self):
        identifier = self.order([], total_cents=1000)
        debt = self.post("/receivables", {
            "customer_name": "Cliente", "amount_cents": 1000, "order_id": identifier,
        }).json()["receivables"][0]
        with self.connection() as conn:
            self.assertFalse(has_receivable_payments(conn, identifier))
        response = self.post(f"/receivables/{debt['id']}/payments", {"amount_cents": 300})
        self.assertEqual(response.status_code, 200, response.text)
        with self.connection() as conn:
            self.assertTrue(has_receivable_payments(conn, identifier))
        response = self.client.post(f"/api/admin/orders/{identifier}/payment",
                                    json={"status": "paid"}, headers=self.headers)
        self.assertEqual(response.status_code, 409, response.text)
        with self.connection() as conn:
            self.assertEqual(conn.execute("SELECT payment_status FROM orders WHERE id=?", (identifier,)).fetchone()[0], "unpaid")
        self.assertEqual(self.get("/cash/entries")["balance_cents"], 300)
        response = self.post(f"/receivables/{debt['id']}/payments", {"amount_cents": 700})
        self.assertEqual(response.status_code, 200, response.text)
        response = self.client.post(f"/api/admin/orders/{identifier}/payment",
                                    json={"status": "paid"}, headers=self.headers)
        self.assertEqual(response.status_code, 200, response.text)
        entries = self.get("/cash/entries")
        self.assertEqual(entries["balance_cents"], 1000)
        self.assertEqual(len(entries["entries"]), 2)
        with self.connection() as conn:
            self.assertEqual(conn.execute("SELECT payment_status FROM orders WHERE id=?", (identifier,)).fetchone()[0], "paid")
            self.assertFalse(record_sale(conn, conn.execute("SELECT * FROM orders WHERE id=?", (identifier,)).fetchone()))

    def test_receivable_retries_require_matching_payload_and_create_one_cash_credit(self):
        debt = self.post("/receivables", {"customer_name": "Cliente", "amount_cents": 1000}).json()["receivables"][0]
        path = f"/receivables/{debt['id']}/payments"
        payload = {"amount_cents": 300, "payment_method": "pix", "description": "Recebido",
                   "idempotency_key": uuid.uuid4().hex}
        for _ in range(2):
            response = self.post(path, payload)
            self.assertEqual(response.status_code, 200, response.text)
        self.assertEqual(len(response.json()["receivables"][0]["payments"]), 1)
        self.assertEqual(self.get("/cash/entries")["balance_cents"], 300)
        self.assertEqual(self.post(path, {**payload, "amount_cents": 301}).status_code, 409)
        self.assertEqual(self.post(path, {**payload, "description": "Outro"}).status_code, 409)
        missing_key = self.client.post("/api/admin" + path, json={"amount_cents": 300}, headers=self.headers)
        self.assertEqual(missing_key.status_code, 422)

    def test_concurrent_receivable_retries_never_duplicate_or_overpay(self):
        debt = self.post("/receivables", {"customer_name": "Cliente", "amount_cents": 1000}).json()["receivables"][0]
        path = "/api/admin/receivables/" + debt["id"] + "/payments"
        repeated = {"amount_cents": 700, "idempotency_key": uuid.uuid4().hex}

        def submit(payload):
            with TestClient(self.app) as client:
                client.cookies.update(self.client.cookies)
                return client.post(path, json=payload, headers=self.headers).status_code

        with ThreadPoolExecutor(max_workers=2) as pool:
            statuses = list(pool.map(submit, [repeated, repeated]))
        self.assertEqual(statuses, [200, 200])
        self.assertEqual(self.get("/cash/entries")["balance_cents"], 700)
        self.assertEqual(len(self.get("/receivables")["receivables"][0]["payments"]), 1)
        with ThreadPoolExecutor(max_workers=2) as pool:
            statuses = list(pool.map(submit, [
                {"amount_cents": 300, "idempotency_key": uuid.uuid4().hex},
                {"amount_cents": 300, "idempotency_key": uuid.uuid4().hex},
            ]))
        self.assertEqual(sorted(statuses), [200, 409])
        self.assertEqual(self.get("/cash/entries")["balance_cents"], 1000)
        self.assertEqual(len(self.get("/receivables")["receivables"][0]["payments"]), 2)

    def test_linked_receivable_quitation_awards_loyalty_only_after_delivery_once(self):
        customer = self.post("/customers", {"name": "Cliente", "phone": "44999999999"}).json()["customer"]
        identifier = self.order([], total_cents=1050, status="delivered")
        with self.connection() as conn:
            conn.execute("UPDATE orders SET customer_id=? WHERE id=?", (customer["id"], identifier))
        debt = self.post("/receivables", {
            "customer_name": "Cliente", "amount_cents": 1050, "order_id": identifier,
        }).json()["receivables"][0]
        self.post("/cash/sessions", {"action": "open", "opening_cents": 100})
        self.post(f"/receivables/{debt['id']}/payments", {"amount_cents": 500})
        self.assertEqual(self.get("/customers")["customers"][0]["points"], 0)
        payload = {"amount_cents": 550, "idempotency_key": uuid.uuid4().hex}
        for _ in range(2):
            response = self.post(f"/receivables/{debt['id']}/payments", payload)
            self.assertEqual(response.status_code, 200, response.text)
        self.assertEqual(self.get("/customers")["customers"][0]["points"], 10)
        session = self.get("/cash/sessions")["active_session"]
        self.assertEqual(session["balance_cents"], 1150)
        with self.connection() as conn:
            self.assertEqual(conn.execute("SELECT COUNT(*) FROM loyalty_ledger WHERE order_id=?", (identifier,)).fetchone()[0], 1)
            self.assertEqual(conn.execute("SELECT payment_status FROM orders WHERE id=?", (identifier,)).fetchone()[0], "paid")

    def test_stock_validation_and_negative_balance(self):
        product = self.product()
        item = self.inventory("Esfiha pronta", 2, product)
        self.assertEqual(self.post("/inventory", {"label": "Duplicado", "product_id": product}).status_code, 409)
        response = self.post(f"/inventory/{item['id']}/adjust", {"quantity": -3, "reason": "Saída"})
        self.assertEqual(response.status_code, 409)
        self.assertEqual(self.stock(item["id"]), 2)
        for quantity in [0, "NaN", "Infinity"]:
            self.assertEqual(self.post(f"/inventory/{item['id']}/adjust", {"quantity": quantity, "reason": "Teste"}).status_code, 422)
        self.assertEqual(self.post("/inventory", {"label": "Inválido", "on_hand": -1}).status_code, 422)
        response = self.post(f"/inventory/{item['id']}/adjust", {"quantity": -1.25, "reason": "Perda conferida"})
        self.assertEqual(response.status_code, 200, response.text)
        self.assertEqual(self.stock(item["id"]), 0.75)
        self.assertTrue(next(i for i in response.json()["items"] if i["id"] == item["id"])["low_stock"])

    def test_recipe_consumption_and_original_restoration_are_idempotent(self):
        first, second = self.product(), self.product()
        flour = self.inventory("Farinha", 10)
        meat = self.inventory("Recheio", 6)
        for product, flour_quantity, meat_quantity in [(first, 1.25, 2), (second, 1, 1)]:
            response = self.post("/recipes", {"product_id": product, "components": [
                {"inventory_id": flour["id"], "quantity": flour_quantity},
                {"inventory_id": meat["id"], "quantity": meat_quantity},
            ]})
            self.assertEqual(response.status_code, 200, response.text)
        identifier = self.order([
            {"id": first, "name": "Primeira", "quantity": 2, "price_cents": 400},
            {"id": second, "name": "Segunda", "quantity": 1, "price_cents": 400},
        ])
        with self.connection() as conn:
            order = conn.execute("SELECT * FROM orders WHERE id=?", (identifier,)).fetchone()
            self.assertTrue(apply_stock(conn, order))
            self.assertFalse(apply_stock(conn, order))
        self.assertEqual(self.stock(flour["id"]), 6.5)
        self.assertEqual(self.stock(meat["id"]), 1)
        self.post("/recipes", {"product_id": first, "components": [{"inventory_id": flour["id"], "quantity": 8}]})
        with self.connection() as conn:
            order = conn.execute("SELECT * FROM orders WHERE id=?", (identifier,)).fetchone()
            self.assertTrue(release_stock(conn, order))
            self.assertFalse(release_stock(conn, order))
        self.assertEqual(self.stock(flour["id"]), 10)
        self.assertEqual(self.stock(meat["id"]), 6)

    def test_insufficient_recipe_does_not_partially_consume(self):
        product = self.product()
        available = self.inventory("Disponível", 8)
        insufficient = self.inventory("Sem saldo", 1)
        self.assertEqual(self.post("/recipes", {"product_id": product, "components": [
            {"inventory_id": available["id"], "quantity": 2},
            {"inventory_id": insufficient["id"], "quantity": 2},
        ]}).status_code, 200)
        identifier = self.order([{"id": product, "name": "Esfiha", "quantity": 1, "price_cents": 400}])
        with self.assertRaises(HTTPException) as error:
            with self.connection() as conn:
                apply_stock(conn, conn.execute("SELECT * FROM orders WHERE id=?", (identifier,)).fetchone())
        self.assertEqual(error.exception.status_code, 409)
        self.assertEqual(self.stock(available["id"]), 8)
        self.assertEqual(self.stock(insufficient["id"]), 1)
        with self.connection() as conn:
            self.assertEqual(conn.execute("SELECT COUNT(*) FROM inventory_movements WHERE order_id=?", (identifier,)).fetchone()[0], 0)
            self.assertEqual(conn.execute("SELECT stock_applied FROM orders WHERE id=?", (identifier,)).fetchone()[0], 0)

    def test_direct_stock_and_recipe_validation(self):
        product = self.product()
        item = self.inventory("Bebida", 3, product)
        identifier = self.order([{"id": product, "name": "Bebida", "quantity": 2, "price_cents": 400}])
        with self.connection() as conn:
            apply_stock(conn, conn.execute("SELECT * FROM orders WHERE id=?", (identifier,)).fetchone())
        self.assertEqual(self.stock(item["id"]), 1)
        self.assertEqual(self.post("/recipes", {"product_id": product, "components": [
            {"inventory_id": item["id"], "quantity": 1}, {"inventory_id": item["id"], "quantity": 2},
        ]}).status_code, 422)

    def test_explicit_cash_sessions_entries_and_paid_sale(self):
        response = self.post("/cash/sessions", {"action": "open", "opening_cents": 1000})
        self.assertEqual(response.status_code, 200, response.text)
        session = response.json()["active_session"]
        self.assertEqual(self.post("/cash/sessions", {"action": "open"}).status_code, 409)
        for kind, amount in [("supply", 500), ("withdrawal", 100), ("debit", 200)]:
            self.assertEqual(self.post("/cash/entries", {"kind": kind, "amount_cents": amount, "description": "Conferido"}).status_code, 200)
        identifier = self.order([], total_cents=400)
        with self.connection() as conn:
            order = dict(conn.execute("SELECT * FROM orders WHERE id=?", (identifier,)).fetchone())
            self.assertFalse(record_sale(conn, order))
            order["payment_status"] = "paid"
            self.assertTrue(record_sale(conn, order))
            self.assertFalse(record_sale(conn, order))
        entries = self.get("/cash/entries")
        self.assertEqual(entries["balance_cents"], 600)
        self.assertEqual(len([entry for entry in entries["entries"] if entry["order_id"] == identifier]), 1)
        response = self.post("/cash/sessions", {"action": "close", "session_id": session["id"], "closing_cents": 1550})
        self.assertEqual(response.status_code, 200, response.text)
        closed = response.json()["sessions"][0]
        self.assertEqual(closed["expected_cents"], 1600)
        self.assertEqual(closed["closing_cents"], 1550)
        self.assertIsNone(response.json()["active_session"])
        self.assertEqual(self.post("/cash/entries", {"kind": "credit", "amount_cents": 100, "description": "Teste", "session_id": session["id"]}).status_code, 409)
        self.assertEqual(self.post("/cash/entries", {"kind": "credit", "amount_cents": 1.5, "description": "Teste"}).status_code, 422)

    def test_paid_sale_without_cash_session_is_not_lost(self):
        identifier = self.order([], payment_status="paid", total_cents=400)
        with self.connection() as conn:
            self.assertTrue(record_sale(conn, conn.execute("SELECT * FROM orders WHERE id=?", (identifier,)).fetchone()))
        entries = self.get("/cash/entries")["entries"]
        self.assertEqual(len(entries), 1)
        self.assertIsNone(entries[0]["session_id"])
        self.assertEqual(entries[0]["amount_cents"], 400)

    def test_receivable_partial_payments_cannot_exceed_balance(self):
        response = self.post("/receivables", {"customer_name": "Cliente", "amount_cents": 1000, "due_date": "2026-11-01"})
        self.assertEqual(response.status_code, 200, response.text)
        debt = response.json()["receivables"][0]
        response = self.post(f"/receivables/{debt['id']}/payments", {"amount_cents": 300, "payment_method": "pix"})
        self.assertEqual(response.status_code, 200, response.text)
        self.assertEqual(response.json()["receivables"][0]["balance_cents"], 700)
        self.assertEqual(response.json()["receivables"][0]["status"], "partial")
        self.assertEqual(self.post(f"/receivables/{debt['id']}/payments", {"amount_cents": 701}).status_code, 409)
        self.assertEqual(self.post(f"/receivables/{debt['id']}/payments", {"amount_cents": 0}).status_code, 422)
        response = self.post(f"/receivables/{debt['id']}/payments", {"amount_cents": 700})
        self.assertEqual(response.json()["receivables"][0]["status"], "paid")
        self.assertEqual(len(response.json()["receivables"][0]["payments"]), 2)
        entries = self.get("/cash/entries")["entries"]
        self.assertEqual(sorted(entry["amount_cents"] for entry in entries), [300, 700])
        self.assertTrue(all(entry["kind"] == "credit" for entry in entries))
        self.assertEqual({entry["receivable_payment_id"] for entry in entries},
                         {payment["id"] for payment in response.json()["receivables"][0]["payments"]})

    def test_driver_assignment_map_link_and_inactive_validation(self):
        response = self.post("/drivers", {"name": "Entregador", "phone": "44999999999"})
        self.assertEqual(response.status_code, 200, response.text)
        driver = response.json()["drivers"][0]
        identifier = self.order([], delivery={"location": {"latitude": -23.42, "longitude": -51.93}, "house_number": "25"})
        response = self.client.patch(f"/api/admin/orders/{identifier}/driver", json={"driver_id": driver["id"]}, headers=self.headers)
        self.assertEqual(response.status_code, 200, response.text)
        self.assertEqual(response.json()["order"]["courier_id"], driver["id"])
        self.assertIn("destination=-23.42%2C-51.93", response.json()["order"]["maps_url"])
        self.assertEqual(len(self.get("/drivers")["drivers"][0]["orders"]), 1)
        self.post("/drivers", {"id": driver["id"], "name": "Entregador", "active": False})
        other = self.order([])
        self.assertEqual(self.client.patch(f"/api/admin/orders/{other}/driver", json={"driver_id": driver["id"]}, headers=self.headers).status_code, 409)

    def test_optional_printer_profiles_have_no_automatic_printing(self):
        self.assertEqual(self.get("/printers"), {"printers": [], "print_method": "browser"})
        response = self.post("/printers", {"name": "Cozinha", "paper_width": 58, "copies": 2})
        self.assertEqual(response.status_code, 200, response.text)
        self.assertEqual(response.json()["printers"][0]["copies"], 2)
        self.assertEqual(self.post("/printers", {"name": "Inválido", "paper_width": 90}).status_code, 422)


if __name__ == "__main__":
    unittest.main()
