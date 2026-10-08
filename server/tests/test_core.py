"""Integration checks for checkout, private operations and the delivery API."""

from contextlib import contextmanager
from copy import deepcopy
from datetime import datetime, timezone
from pathlib import Path
import sqlite3
from tempfile import TemporaryDirectory
import unittest
from unittest.mock import patch
from uuid import uuid4

from fastapi.testclient import TestClient

from server.core import COOKIE, SCHEMA, hash_password
from server.main import create_app


def frozen_datetime(instant):
    class FrozenDateTime(datetime):
        @classmethod
        def now(cls, tz=None):
            return instant.astimezone(tz) if tz else instant.replace(tzinfo=None)
    return FrozenDateTime


class CoreTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.password = "temporary-test-password"
        cls.password_hash = hash_password(cls.password)

    def setUp(self):
        self.directory = TemporaryDirectory()
        self.app = create_app(data_dir=Path(self.directory.name) / "private",
                              admin_password_hash=self.password_hash, secure_cookie=False)
        self.client = TestClient(self.app)
        self.client.__enter__()
        login = self.client.post("/api/admin/login", json={"password": self.password})
        self.assertEqual(login.status_code, 200)
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
            yield conn
            conn.commit()
        except BaseException:
            conn.rollback()
            raise
        finally:
            conn.close()

    def body(self, **overrides):
        return {
            "items": [{"id": "carne", "quantity": 2}],
            "customer": {"name": "Cliente de teste", "phone": "(44) 99999-9999"},
            "delivery": {"street": "Rua das Palmeiras", "number": "25", "neighborhood": "Centro"},
            "payment_method": "Pix",
            "idempotency_key": uuid4().hex,
        } | overrides

    def order(self, body=None):
        response = self.client.post("/api/orders", json=body or self.body())
        self.assertEqual(response.status_code, 201, response.text)
        return response.json()

    def post(self, path, body, status=200):
        response = self.client.post("/api/admin" + path, json=body, headers=self.headers)
        self.assertEqual(response.status_code, status, response.text)
        return response.json()

    def transition(self, identifier, status, expected=200):
        response = self.client.patch("/api/admin/orders/" + identifier,
                                     json={"status": status}, headers=self.headers)
        self.assertEqual(response.status_code, expected, response.text)
        return response.json()

    def stored(self, identifier):
        with self.connection() as conn:
            return dict(conn.execute("SELECT * FROM orders WHERE id=?", (identifier,)).fetchone())

    def stock(self, identifier):
        inventory = self.client.get("/api/admin/inventory").json()["items"]
        return next(item["on_hand"] for item in inventory if item["id"] == identifier)

    def test_admin_missing_configuration_is_locked(self):
        app = create_app(data_dir=Path(self.directory.name) / "locked", admin_password_hash="", secure_cookie=False)
        with TestClient(app) as client:
            health = client.get("/api/health")
            self.assertEqual(health.status_code, 200)
            self.assertFalse(health.json()["admin_configured"])
            self.assertTrue(health.json()["business"]["delivery_only"])
            self.assertEqual(client.post("/api/admin/login", json={"password": self.password}).status_code, 503)
            self.assertEqual(client.get("/api/admin/orders").status_code, 401)

    def test_session_cookie_csrf_origin_and_logout(self):
        with TestClient(self.app) as visitor:
            self.assertEqual(visitor.get("/api/admin/dashboard").status_code, 401)
            self.assertEqual(visitor.post("/api/admin/login", json={"password": self.password},
                                          headers={"Origin": "https://foreign.example"}).status_code, 403)
        response = self.client.post("/api/admin/login", json={"password": self.password})
        cookie = response.headers["set-cookie"].lower()
        self.assertIn("httponly", cookie)
        self.assertIn("samesite=strict", cookie)
        self.assertIn("path=/api/admin", cookie)
        with self.connection() as conn:
            stored = conn.execute("SELECT token_hash FROM sessions").fetchall()
            self.assertTrue(stored)
            for session in stored:
                self.assertEqual(len(session["token_hash"]), 64)
                self.assertNotEqual(session["token_hash"], self.client.cookies.get(COOKIE))
        self.headers = {"X-Sahara-CSRF": response.json()["csrf_token"]}
        self.assertEqual(self.client.get("/api/admin/session").status_code, 200)
        path = "/api/admin/orders"
        self.assertEqual(self.client.post(path, json=self.body()).status_code, 403)
        self.assertEqual(self.client.post(path, json=self.body(), headers={"X-Sahara-CSRF": "incorrect"}).status_code, 403)
        self.assertEqual(self.client.post(path, json=self.body(),
                                         headers=self.headers | {"Origin": "https://foreign.example"}).status_code, 403)
        accepted = self.client.post(path, json=self.body(), headers=self.headers | {"Origin": "http://testserver"})
        self.assertEqual(accepted.status_code, 201, accepted.text)
        self.assertEqual(self.stored(accepted.json()["id"])["source"], "pdv")
        self.post("/logout", {})
        self.assertEqual(self.client.get("/api/admin/session").status_code, 401)

    def test_login_rate_limit_and_expired_session(self):
        with TestClient(self.app) as client:
            for _ in range(5):
                self.assertEqual(client.post("/api/admin/login", json={"password": "incorrect-test-value"}).status_code, 401)
            self.assertEqual(client.post("/api/admin/login", json={"password": self.password}).status_code, 429)
        with self.connection() as conn:
            conn.execute("UPDATE sessions SET expires_at=0")
        self.assertEqual(self.client.get("/api/admin/session").status_code, 401)

    def test_password_preserves_spaces_and_secure_cookie_is_enabled_when_configured(self):
        password = " temporary-test-password "
        app = create_app(data_dir=Path(self.directory.name) / "spaces", admin_password_hash=hash_password(password),
                         secure_cookie=True)
        with TestClient(app, base_url="https://testserver") as client:
            self.assertEqual(client.post("/api/admin/login", json={"password": password.strip()}).status_code, 401)
            login = client.post("/api/admin/login", json={"password": password})
            self.assertEqual(login.status_code, 200)
            self.assertIn("secure", login.headers["set-cookie"].lower())
            self.assertEqual(client.get("/api/admin/session").status_code, 200)

    def test_authoritative_catalog_and_customer_normalization(self):
        catalog = self.client.get("/api/catalog").json()["products"]
        prices = {item["id"]: item["price_cents"] for item in catalog}
        self.assertEqual({key: prices[key] for key in ("carne", "frango", "calabresa", "queijo")},
                         {"carne": 400, "frango": 400, "calabresa": 400, "queijo": 400})
        self.assertEqual(prices["calabresa-acebolada"], 450)
        created = self.order(self.body(items=[{"id": "carne", "quantity": 2},
                                             {"id": "calabresa-acebolada", "quantity": 1}]))
        self.assertEqual(created["total_cents"], 1250)
        self.assertEqual(created["status"], "preparing")
        stored = self.stored(created["id"])
        self.assertEqual(stored["payment_status"], "unpaid")
        self.assertEqual(stored["customer_phone"], "44999999999")
        self.assertEqual(stored["subtotal_cents"], 1250)
        modified = self.body()
        modified["items"][0]["price_cents"] = 1
        self.assertEqual(self.client.post("/api/orders", json=modified).status_code, 422)
        modified = self.body(total_cents=1)
        self.assertEqual(self.client.post("/api/orders", json=modified).status_code, 422)

    def test_invalid_items_and_customer_are_rejected_without_orders(self):
        invalid_items = ([], [{"id": "unknown", "quantity": 1}],
                         [{"id": "carne", "quantity": 1}, {"id": "carne", "quantity": 2}],
                         [{"id": "carne", "quantity": 0}], [{"id": "carne", "quantity": 100}],
                         [{"id": "carne", "quantity": 1.5}], [{"id": "carne", "quantity": True}],
                         [{"id": "carne", "quantity": "2"}])
        for items in invalid_items:
            with self.subTest(items=items):
                self.assertEqual(self.client.post("/api/orders", json=self.body(items=items)).status_code, 422)
        for phone in ("123", "telefone privado", "+55 invalid 99999999"):
            response = self.client.post("/api/orders", json=self.body(customer={"phone": phone}))
            self.assertEqual(response.status_code, 422)
            self.assertNotIn(phone, response.text)
        self.assertEqual(self.client.get("/api/admin/orders").json()["orders"], [])

    def test_zero_coordinates_address_validation_and_tracking_privacy(self):
        point = {"latitude": 0, "longitude": 0, "accuracy": 0,
                 "url": "https://untrusted.example/private"}
        created = self.order(self.body(delivery={"number": "25", "location": point}, notes="Preferência privada"))
        details = self.client.get("/api/admin/orders").json()["orders"][0]
        self.assertEqual(details["delivery"]["location"]["url"], "https://www.google.com/maps?q=0.000000,0.000000")
        for field in ("tracking_token", "request_hash", "idempotency_key"):
            self.assertNotIn(field, details)
        response = self.client.get("/api/orders/" + created["id"] + "/track", params={"token": created["tracking_token"]})
        self.assertEqual(response.status_code, 200)
        public = response.json()
        self.assertEqual(public["status"], "preparing")
        self.assertEqual(public["total_cents"], 800)
        for field in ("delivery", "customer", "customer_name", "customer_phone", "customer_id", "notes", "tracking_token"):
            self.assertNotIn(field, public)
        self.assertNotIn("Preferência privada", response.text)
        self.assertEqual(self.client.get("/api/orders/" + created["id"] + "/track", params={"token": "wrong-token"}).status_code, 404)
        self.assertEqual(self.client.get("/api/orders/" + created["id"] + "/track", params={"token": "é"}).status_code, 404)
        self.assertEqual(self.client.get("/api/orders/unknown/track", params={"token": created["tracking_token"]}).status_code, 404)
        for delivery in ({"number": "25"}, {"street": "Rua", "neighborhood": "Centro", "number": ""},
                         {"number": "25", "location": {"latitude": 91, "longitude": 0}},
                         {"number": "25", "location": {"latitude": 0, "longitude": -181}}):
            self.assertEqual(self.client.post("/api/orders", json=self.body(delivery=delivery)).status_code, 422)

    def test_order_idempotency_replays_once_and_rejects_changed_payload(self):
        body = self.body()
        first = self.order(body)
        second = self.order(body)
        self.assertEqual(first["id"], second["id"])
        self.assertEqual(first["tracking_token"], second["tracking_token"])
        self.assertTrue(second["replayed"])
        changed = deepcopy(body)
        changed["items"][0]["quantity"] = 3
        self.assertEqual(self.client.post("/api/orders", json=changed).status_code, 409)
        self.transition(first["id"], "ready")
        replay = self.order(body)
        self.assertEqual(replay["status"], "ready")
        with self.connection() as conn:
            self.assertEqual(conn.execute("SELECT COUNT(*) FROM orders").fetchone()[0], 1)
            self.assertEqual(conn.execute("SELECT COUNT(*) FROM analytics_events WHERE name='order_registered'").fetchone()[0], 1)

    def test_scheduling_uses_maringa_timezone_future_range_and_business_hours(self):
        instant = datetime(2030, 1, 1, 20, 0, tzinfo=timezone.utc)  # 17h in Maringá.
        with patch("server.main.datetime", frozen_datetime(instant)):
            for value in ("2030-01-01T21:00:00+00:00", "2030-01-01T23:00:00-03:00", "2030-01-08T18:00:00-03:00"):
                self.order(self.body(requested_for=value))
            for value in ("2030-01-01T17:00:00-03:00", "2030-01-01T17:59:00-03:00",
                          "2030-01-01T23:01:00-03:00", "2030-01-01T18:00:01-03:00",
                          "2030-01-09T18:00:00-03:00", "2030-01-01T18:00:00"):
                with self.subTest(value=value):
                    self.assertEqual(self.client.post("/api/orders", json=self.body(requested_for=value)).status_code, 422)

    def test_scheduled_order_retry_remains_valid_after_requested_time(self):
        body = self.body(requested_for="2030-01-01T18:30:00-03:00")
        with patch("server.main.datetime", frozen_datetime(datetime(2030, 1, 1, 20, 0, tzinfo=timezone.utc))):
            first = self.order(body)
        with patch("server.main.datetime", frozen_datetime(datetime(2030, 1, 1, 22, 0, tzinfo=timezone.utc))):
            replay = self.order(body)
        self.assertEqual(replay["id"], first["id"])
        self.assertTrue(replay["replayed"])

    def test_preparation_consumes_stock_and_coupon_once_with_atomic_registration(self):
        inventory_id = self.post("/inventory", {"label": "Carne pronta", "product_id": "carne", "on_hand": 4})["items"][0]["id"]
        coupon = self.post("/coupons", {"code": "LIMITE", "kind": "percent", "value": 10, "usage_limit": 1}, 201)["coupon"]
        body = self.body(coupon_code="LIMITE")
        first = self.order(body)
        self.assertEqual(self.stock(inventory_id), 2)
        self.order(body)
        self.transition(first["id"], "preparing")
        self.assertEqual(self.stock(inventory_id), 2)
        denied = self.client.post("/api/orders", json=self.body(coupon_code="LIMITE"))
        self.assertEqual(denied.status_code, 400)
        self.assertEqual(self.stock(inventory_id), 2)
        stored = self.stored(first["id"])
        self.assertEqual((stored["status"], stored["stock_applied"], stored["coupon_applied"]), ("preparing", 1, 1))
        with self.connection() as conn:
            self.assertEqual(conn.execute("SELECT uses FROM coupons WHERE id=?", (coupon["id"],)).fetchone()[0], 1)
            self.assertEqual(conn.execute("SELECT COUNT(*) FROM orders").fetchone()[0], 1)
            self.assertEqual(conn.execute("SELECT COUNT(*) FROM inventory_movements WHERE order_id=?", (first["id"],)).fetchone()[0], 1)
        self.transition(first["id"], "cancelled")
        self.transition(first["id"], "cancelled")
        self.assertEqual(self.stock(inventory_id), 2)  # Production has already started.

    def test_stock_shortage_rejects_registration_and_rolls_back_customer_and_coupon(self):
        inventory_id = self.post("/inventory", {"label": "Carne pronta", "product_id": "carne", "on_hand": 1})["items"][0]["id"]
        coupon = self.post("/coupons", {"code": "ESTOQUE", "kind": "percent", "value": 10}, 201)["coupon"]
        denied = self.client.post("/api/orders", json=self.body(coupon_code="ESTOQUE"))
        self.assertEqual(denied.status_code, 409)
        self.assertEqual(self.stock(inventory_id), 1)
        with self.connection() as conn:
            for table in ("orders", "customers", "order_events"):
                self.assertEqual(conn.execute("SELECT COUNT(*) FROM " + table).fetchone()[0], 0)
            self.assertEqual(conn.execute("SELECT uses FROM coupons WHERE id=?", (coupon["id"],)).fetchone()[0], 0)

    def test_existing_database_with_old_default_still_registers_in_preparation(self):
        directory = Path(self.directory.name) / "legacy"
        directory.mkdir()
        with sqlite3.connect(directory / "sahara.sqlite3") as conn:
            conn.executescript(SCHEMA.replace("DEFAULT 'preparing'", "DEFAULT 'new'"))
        app = create_app(data_dir=directory, admin_password_hash=self.password_hash, secure_cookie=False)
        with TestClient(app) as client:
            response = client.post("/api/orders", json=self.body())
            self.assertEqual(response.status_code, 201, response.text)
            self.assertEqual(response.json()["status"], "preparing")
        with sqlite3.connect(directory / "sahara.sqlite3") as conn:
            self.assertEqual(conn.execute("SELECT status FROM orders").fetchone()[0], "preparing")

    def test_four_stages_require_ready_before_delivery_and_preserve_event_history(self):
        for endpoint in ("/api/orders", "/api/admin/orders"):
            response = self.client.post(endpoint, json=self.body(), headers=self.headers)
            self.assertEqual(response.status_code, 201, response.text)
            order = response.json()
            self.assertEqual(order["status"], "preparing")
            self.transition(order["id"], "out_for_delivery", 409)
            self.transition(order["id"], "delivered", 409)
            for status in ("ready", "out_for_delivery", "delivered"):
                self.transition(order["id"], status)
                self.transition(order["id"], status)  # Retrying does not duplicate a stage.
                tracking = self.client.get("/api/orders/" + order["id"] + "/track", params={"token": order["tracking_token"]})
                self.assertEqual(tracking.json()["status"], status)
            self.transition(order["id"], "ready", 409)
            with self.connection() as conn:
                events = conn.execute("SELECT status FROM order_events WHERE order_id=? ORDER BY rowid", (order["id"],)).fetchall()
                self.assertEqual([event["status"] for event in events], ["preparing", "ready", "out_for_delivery", "delivered"])

    def test_legacy_received_orders_can_start_preparation_or_confirm_and_restore_stock(self):
        old = self.order()
        with self.connection() as conn:
            conn.execute("UPDATE orders SET status='new',stock_applied=0,confirmed_at=NULL WHERE id=?", (old["id"],))
        inventory_id = self.post("/inventory", {"label": "Carne pronta", "product_id": "carne", "on_hand": 4})["items"][0]["id"]
        self.transition(old["id"], "confirmed")
        self.assertEqual(self.stock(inventory_id), 2)
        self.transition(old["id"], "confirmed")
        self.assertEqual(self.stock(inventory_id), 2)
        self.transition(old["id"], "cancelled")
        self.transition(old["id"], "cancelled")
        self.assertEqual(self.stock(inventory_id), 4)
        self.transition(old["id"], "preparing", 409)
        self.post("/orders/" + old["id"] + "/payment", {"status": "paid"}, 409)
        second = self.order(self.body(items=[{"id": "queijo", "quantity": 1}]))
        with self.connection() as conn:
            conn.execute("UPDATE orders SET status='new',stock_applied=0,confirmed_at=NULL WHERE id=?", (second["id"],))
        self.transition(second["id"], "ready", 409)
        self.transition(second["id"], "preparing")
        self.transition(second["id"], "ready")

    def test_payment_cash_and_paid_delivery_loyalty_are_idempotent(self):
        order = self.order()
        identifier = order["id"]
        self.post("/orders/" + identifier + "/payment", {"status": "paid"})
        self.post("/orders/" + identifier + "/payment", {"status": "paid"})
        self.transition(identifier, "cancelled", 409)
        with self.connection() as conn:
            customer = conn.execute("SELECT * FROM customers").fetchone()
            self.assertEqual(customer["points"], 0)
        for status in ("preparing", "ready", "out_for_delivery", "delivered", "delivered"):
            self.transition(identifier, status)
        self.post("/orders/" + identifier + "/payment", {"status": "paid"})
        self.transition(identifier, "preparing", 409)
        entries = self.client.get("/api/admin/cash/entries").json()["entries"]
        self.assertEqual(len(entries), 1)
        self.assertEqual(entries[0]["order_id"], identifier)
        self.assertEqual(entries[0]["amount_cents"], 800)
        with self.connection() as conn:
            self.assertEqual(conn.execute("SELECT points FROM customers").fetchone()[0], 8)
            self.assertEqual(conn.execute("SELECT COUNT(*) FROM loyalty_ledger WHERE order_id=?", (identifier,)).fetchone()[0], 1)

    def test_dashboard_groups_business_dates_and_excludes_cancelled_revenue(self):
        first = self.order(self.body(items=[{"id": "carne", "quantity": 1}]))
        second = self.order(self.body(items=[{"id": "carne", "quantity": 1}]))
        cancelled = self.order(self.body(items=[{"id": "carne", "quantity": 1}]))
        for order in (first, second):
            self.post("/orders/" + order["id"] + "/payment", {"status": "paid"})
        with self.connection() as conn:
            conn.execute("UPDATE orders SET created_at='2030-01-02T00:30:00+00:00' WHERE id=?", (first["id"],))
            conn.execute("UPDATE orders SET created_at='2030-01-02T02:00:00+00:00' WHERE id=?", (second["id"],))
            conn.execute("UPDATE orders SET created_at='2030-01-02T01:00:00+00:00',status='cancelled',payment_status='paid' WHERE id=?", (cancelled["id"],))
        result = self.client.get("/api/admin/dashboard").json()
        self.assertEqual(result["sales_cents"], 800)
        self.assertEqual(result["ticket_cents"], 400)
        self.assertEqual(result["daily_sales"], [{"date": "2030-01-01", "total_cents": 800}])
        self.assertIsNone(result["conversion_rate"])

    def test_linked_receivable_settlement_cash_loyalty_and_retry_are_consistent(self):
        order = self.order()
        identifier = order["id"]
        debt = self.post("/receivables", {"customer_name": "Cliente", "amount_cents": 800,
                                          "order_id": identifier})["receivables"][0]
        payment_path = "/receivables/" + debt["id"] + "/payments"
        partial = {"amount_cents": 300, "payment_method": "pix", "idempotency_key": uuid4().hex}
        self.post(payment_path, partial)
        self.post(payment_path, partial)
        self.post(payment_path, partial | {"amount_cents": 301}, 409)
        self.post("/orders/" + identifier + "/payment", {"status": "paid"}, 409)
        self.assertEqual(self.stored(identifier)["payment_status"], "unpaid")
        self.transition(identifier, "cancelled", 409)
        for status in ("preparing", "ready", "out_for_delivery", "delivered"):
            self.transition(identifier, status)
        with self.connection() as conn:
            self.assertEqual(conn.execute("SELECT points FROM customers").fetchone()[0], 0)
        final = {"amount_cents": 500, "payment_method": "dinheiro", "idempotency_key": uuid4().hex}
        result = self.post(payment_path, final)
        self.assertEqual(result["receivables"][0]["status"], "paid")
        self.post(payment_path, final)
        self.post("/orders/" + identifier + "/payment", {"status": "paid"})
        self.assertEqual(self.stored(identifier)["payment_status"], "paid")
        entries = self.client.get("/api/admin/cash/entries").json()["entries"]
        self.assertEqual(len(entries), 2)
        self.assertEqual(sum(entry["amount_cents"] for entry in entries), 800)
        self.assertEqual(len({entry["receivable_payment_id"] for entry in entries}), 2)
        with self.connection() as conn:
            self.assertEqual(conn.execute("SELECT points FROM customers").fetchone()[0], 8)
            self.assertEqual(conn.execute("SELECT COUNT(*) FROM loyalty_ledger WHERE order_id=?", (identifier,)).fetchone()[0], 1)

    def test_cancelled_unpaid_receivable_cannot_be_received(self):
        order = self.order()
        debt = self.post("/receivables", {"customer_name": "Cliente", "amount_cents": 800,
                                          "order_id": order["id"]})["receivables"][0]
        self.transition(order["id"], "cancelled")
        self.post("/receivables/" + debt["id"] + "/payments",
                  {"amount_cents": 800, "idempotency_key": uuid4().hex}, 409)
        self.assertEqual(self.client.get("/api/admin/cash/entries").json()["entries"], [])

    def test_event_metrics_use_real_views_and_exclude_manual_orders(self):
        self.order()
        self.post("/orders", self.body(), 201)
        for _ in range(4):
            self.assertEqual(self.client.post("/api/events", json={"name": "view"}).status_code, 201)
        self.assertEqual(self.client.post("/api/events", json={"name": "checkout_started"}).status_code, 201)
        result = self.client.get("/api/admin/dashboard").json()
        self.assertEqual(result["orders_count"], 2)
        self.assertEqual(result["views"], 4)
        self.assertEqual(result["conversion_rate"], 0.25)

    def test_public_assets_never_expose_private_files_and_api_is_not_cached(self):
        for path in ("/server/main.py", "/server/core.py", "/.env", "/.git/config", "/server/requirements.txt",
                     "/images/%2e%2e/server/main.py", "/sahara.sqlite3", "/api/does-not-exist"):
            with self.subTest(path=path):
                self.assertEqual(self.client.get(path).status_code, 404)
        self.assertEqual(self.client.get("/").status_code, 200)
        self.assertEqual(self.client.get("/admin.html").status_code, 200)
        config = self.client.get("/system-config.js")
        self.assertEqual(config.status_code, 200)
        self.assertIn('apiBase:"/api"', config.text)
        self.assertEqual(config.headers["cache-control"], "no-store")
        health = self.client.get("/api/health")
        self.assertEqual(health.headers["cache-control"], "no-store")
        self.assertEqual(health.headers["x-content-type-options"], "nosniff")
        self.assertEqual(self.client.get("/admin.html").headers["cache-control"], "no-store")

    def test_public_api_cors_does_not_enable_credential_access(self):
        allowed = self.client.options("/api/orders", headers={"Origin": "https://saharaesfihas-card.github.io",
                                                            "Access-Control-Request-Method": "POST",
                                                            "Access-Control-Request-Headers": "Content-Type"})
        self.assertEqual(allowed.status_code, 200)
        self.assertEqual(allowed.headers["access-control-allow-origin"], "https://saharaesfihas-card.github.io")
        self.assertNotIn("access-control-allow-credentials", allowed.headers)
        rejected = self.client.options("/api/orders", headers={"Origin": "https://foreign.example",
                                                             "Access-Control-Request-Method": "POST"})
        self.assertEqual(rejected.status_code, 400)
        self.assertNotIn("access-control-allow-origin", rejected.headers)


if __name__ == "__main__":
    unittest.main()
