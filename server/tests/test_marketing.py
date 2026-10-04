"""Regression checks for private CRM and public order feedback."""

from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta, timezone
from tempfile import TemporaryDirectory
from types import SimpleNamespace
import unittest
from uuid import uuid4

from fastapi import HTTPException
from fastapi.testclient import TestClient

from server.core import db, hash_password, utcnow
from server.main import create_app
from server.marketing import apply_loyalty, coupon_discount, record_coupon_use


class MarketingTests(unittest.TestCase):
    def setUp(self):
        self.directory = TemporaryDirectory()
        self.app = create_app(data_dir=self.directory.name,
                              admin_password_hash=hash_password("test-only"),
                              secure_cookie=False)
        self.request = SimpleNamespace(app=self.app)
        self.client = TestClient(self.app)
        self.client.__enter__()
        login = self.client.post("/api/admin/login", json={"password": "test-only"})
        self.assertEqual(login.status_code, 200, login.text)
        self.headers = {"X-Sahara-CSRF": login.json()["csrf_token"]}

    def tearDown(self):
        self.client.__exit__(None, None, None)
        self.directory.cleanup()

    def post(self, path, body, status=201):
        response = self.client.post(path, json=body, headers=self.headers)
        self.assertEqual(response.status_code, status, response.text)
        return response.json()

    def customer(self, name="Maria", phone="11987654321", opt_in=False):
        return self.post("/api/admin/customers", {"name": name, "phone": phone,
                                                 "marketing_opt_in": opt_in})["customer"]

    def coupon(self, **overrides):
        body = {"code": "SAHARA10", "kind": "percent", "value": 10} | overrides
        return self.post("/api/admin/coupons", body)["coupon"]

    def order(self, customer_id=None, *, status="new", payment_status="unpaid",
              total_cents=2500, coupon_code=None, days_ago=0):
        order_id = str(uuid4())
        created = (datetime.now(timezone.utc) - timedelta(days=days_ago)).isoformat()
        with db(self.request) as conn:
            conn.execute(
                "INSERT INTO orders (id, created_at, customer_id, customer_name, customer_phone, "
                "delivery_json, items_json, subtotal_cents, total_cents, coupon_code, status, payment_status, "
                "tracking_token, idempotency_key, request_hash, delivered_at) "
                "VALUES (?, ?, ?, 'Cliente privado', '11999999999', '{}', '[]', ?, ?, ?, ?, ?, ?, ?, 'test', ?)",
                (order_id, created, customer_id, total_cents, total_cents, coupon_code, status,
                 payment_status, "secret-tracking-token", str(uuid4()), created if status == "delivered" else None),
            )
            return dict(conn.execute("SELECT * FROM orders WHERE id = ?", (order_id,)).fetchone())

    def test_private_endpoints_require_session_and_mutations_require_csrf(self):
        with TestClient(self.app) as anonymous:
            for path in ("customers", "coupons", "rfv", "loyalty/rewards", "campaigns", "reviews"):
                self.assertEqual(anonymous.get("/api/admin/" + path).status_code, 401)
        denied = self.client.post("/api/admin/customers", json={"name": "Maria", "phone": "11987654321"})
        self.assertEqual(denied.status_code, 403)

    def test_customer_normalization_consent_filters_and_protected_points(self):
        customer = self.customer(phone="(11) 98765-4321")
        self.assertEqual(customer["phone"], "11987654321")
        self.assertFalse(customer["marketing_opt_in"])
        self.assertEqual(customer["points"], 0)
        self.post("/api/admin/customers", {"name": "Duplicado", "phone": "11987654321"}, 409)
        matched = self.client.get("/api/admin/customers", params={"q": "(11) 98765"}).json()["customers"]
        self.assertEqual([item["id"] for item in matched], [customer["id"]])
        changed = self.client.patch("/api/admin/customers/" + customer["id"],
                                   json={"marketing_opt_in": True}, headers=self.headers)
        self.assertEqual(changed.status_code, 200)
        self.assertTrue(changed.json()["customer"]["marketing_opt_in"])
        protected = self.client.patch("/api/admin/customers/" + customer["id"],
                                     json={"points": 10000}, headers=self.headers)
        self.assertEqual(protected.status_code, 422)

    def test_coupon_server_calculation_rounding_cap_minimum_and_expiry(self):
        coupon = self.coupon(min_subtotal_cents=1000, max_discount_cents=250)
        with db(self.request) as conn:
            self.assertEqual(coupon_discount(conn, None, 3000), 0)
            self.assertEqual(coupon_discount(conn, "sahara10", 1999), 199)
            self.assertEqual(coupon_discount(conn, "SAHARA10", 5000), 250)
            with self.assertRaises(HTTPException):
                coupon_discount(conn, "SAHARA10", 999)
        patched = self.client.patch("/api/admin/coupons/" + coupon["id"],
                                    json={"active": False}, headers=self.headers)
        self.assertEqual(patched.status_code, 200)
        with db(self.request) as conn:
            with self.assertRaises(HTTPException):
                coupon_discount(conn, "SAHARA10", 3000)
        self.coupon(code="FIXO", kind="fixed", value=5000)
        self.coupon(code="EXPIROU", expires_at=(datetime.now(timezone.utc) - timedelta(days=1)).isoformat())
        with db(self.request) as conn:
            self.assertEqual(coupon_discount(conn, "FIXO", 1500), 1500)
            with self.assertRaises(HTTPException):
                coupon_discount(conn, "EXPIROU", 3000)
        self.post("/api/admin/coupons", {"code": "INVALIDO", "kind": "percent", "value": 101}, 422)
        self.post("/api/admin/coupons", {"code": "SEM_FUSO", "kind": "fixed", "value": 100,
                                        "expires_at": "2030-01-01T12:00:00"}, 422)

    def test_coupon_claim_limit_is_atomic_idempotent_and_rolls_back(self):
        coupon = self.coupon(usage_limit=1)
        first = self.order(coupon_code=coupon["code"])
        second = self.order(coupon_code=coupon["code"])
        with db(self.request) as conn:
            self.assertTrue(record_coupon_use(conn, first))
            self.assertFalse(record_coupon_use(conn, first))
        with self.assertRaises(HTTPException):
            with db(self.request) as conn:
                record_coupon_use(conn, second)
        with db(self.request) as conn:
            self.assertEqual(conn.execute("SELECT uses FROM coupons WHERE id = ?", (coupon["id"],)).fetchone()[0], 1)
            self.assertEqual(conn.execute("SELECT coupon_applied FROM orders WHERE id = ?", (second["id"],)).fetchone()[0], 0)
            with self.assertRaises(HTTPException):
                coupon_discount(conn, coupon["code"], 3000)

    def test_coupon_customer_limit_requires_identity_and_counts_claims(self):
        customer = self.customer()
        coupon = self.coupon(per_customer_limit=1)
        with db(self.request) as conn:
            with self.assertRaises(HTTPException):
                coupon_discount(conn, coupon["code"], 3000)
            self.assertEqual(coupon_discount(conn, coupon["code"], 3000, customer["id"]), 300)
        first = self.order(customer["id"], coupon_code=coupon["code"])
        second = self.order(customer["id"], coupon_code=coupon["code"])
        with db(self.request) as conn:
            record_coupon_use(conn, first)
        with self.assertRaises(HTTPException):
            with db(self.request) as conn:
                record_coupon_use(conn, second)

    def test_loyalty_only_after_paid_delivery_once_on_net_total(self):
        customer = self.customer()
        order = self.order(customer["id"], status="delivered", total_cents=2599)
        with db(self.request) as conn:
            self.assertEqual(apply_loyalty(conn, order), 0)
            conn.execute("UPDATE orders SET payment_status = 'paid' WHERE id = ?", (order["id"],))
            self.assertEqual(apply_loyalty(conn, order), 25)
            self.assertEqual(apply_loyalty(conn, order), 0)
            self.assertEqual(conn.execute("SELECT points FROM customers WHERE id = ?", (customer["id"],)).fetchone()[0], 25)
            self.assertEqual(conn.execute("SELECT COUNT(*) FROM loyalty_ledger WHERE order_id = ?", (order["id"],)).fetchone()[0], 1)
        unpaid = self.order(customer["id"], status="new", payment_status="paid")
        with db(self.request) as conn:
            self.assertEqual(apply_loyalty(conn, unpaid), 0)

    def test_reward_redemption_replay_no_duplicate_debit_and_insufficient_balance(self):
        customer = self.customer()
        order = self.order(customer["id"], status="delivered", payment_status="paid", total_cents=1500)
        with db(self.request) as conn:
            apply_loyalty(conn, order)
        reward = self.post("/api/admin/loyalty/rewards", {"name": "Esfiha", "points_cost": 10})["reward"]
        body = {"customer_id": customer["id"], "reward_id": reward["id"], "idempotency_key": "redemption-one"}
        result = self.post("/api/admin/loyalty/redeem", body, 200)
        self.assertEqual(result["remaining_points"], 5)
        replay = self.post("/api/admin/loyalty/redeem", body, 200)
        self.assertTrue(replay["replayed"])
        self.assertEqual(result["redemption"]["id"], replay["redemption"]["id"])
        self.post("/api/admin/loyalty/redeem", body | {"idempotency_key": "redemption-two"}, 400)
        with db(self.request) as conn:
            self.assertEqual(conn.execute("SELECT points FROM customers WHERE id = ?", (customer["id"],)).fetchone()[0], 5)
            self.assertEqual(conn.execute("SELECT COUNT(*) FROM loyalty_redemptions").fetchone()[0], 1)

    def test_concurrent_redemption_requests_claim_one_reward(self):
        customer = self.customer()
        order = self.order(customer["id"], status="delivered", payment_status="paid", total_cents=1500)
        with db(self.request) as conn:
            apply_loyalty(conn, order)
        reward = self.post("/api/admin/loyalty/rewards", {"name": "Esfiha", "points_cost": 10})["reward"]
        body = {"customer_id": customer["id"], "reward_id": reward["id"], "idempotency_key": "simultaneous-one"}
        with ThreadPoolExecutor(max_workers=2) as pool:
            futures = [pool.submit(self.client.post, "/api/admin/loyalty/redeem", json=body, headers=self.headers) for _ in range(2)]
            results = [future.result() for future in futures]
        self.assertEqual([result.status_code for result in results], [200, 200])
        self.assertEqual(len({result.json()["redemption"]["id"] for result in results}), 1)
        with db(self.request) as conn:
            self.assertEqual(conn.execute("SELECT points FROM customers WHERE id = ?", (customer["id"],)).fetchone()[0], 5)

    def test_rfv_counts_only_paid_deliveries_and_explicit_recency_segments(self):
        customer = self.customer()
        inactive = self.customer("José", "11912345678")
        prospect = self.customer("Ana", "11955554444")
        for _ in range(5):
            self.order(customer["id"], status="delivered", payment_status="paid", total_cents=400)
        self.order(customer["id"], status="delivered", payment_status="unpaid", total_cents=100000)
        self.order(customer["id"], status="cancelled", payment_status="paid", total_cents=100000)
        self.order(inactive["id"], status="delivered", payment_status="paid", days_ago=70)
        result = self.client.get("/api/admin/rfv").json()
        customers = {item["id"]: item for item in result["customers"]}
        self.assertEqual(customers[customer["id"]]["frequency"], 5)
        self.assertEqual(customers[customer["id"]]["monetary_cents"], 2000)
        self.assertEqual(customers[customer["id"]]["segment"], "vip")
        self.assertEqual(customers[inactive["id"]]["segment"], "inativo")
        self.assertEqual(customers[prospect["id"]]["segment"], "sem_compras")
        self.assertIsNone(customers[prospect["id"]]["recency_days"])
        self.assertEqual(result["rules"]["eligible_orders"], "delivered+paid")

    def test_campaigns_are_drafts_only_with_current_marketing_consent(self):
        denied = self.customer()
        base = {"name": "Volte a pedir", "kind": "recovery", "message": "Seu cupom está disponível."}
        self.post("/api/admin/campaigns", base, 400)
        allowed = self.customer("José", "11912345678", True)
        self.post("/api/admin/campaigns", base | {"customer_ids": [denied["id"]]}, 400)
        self.post("/api/admin/campaigns", base | {"status": "sent"}, 422)
        campaign = self.post("/api/admin/campaigns", base)["campaign"]
        self.assertEqual(campaign["status"], "draft")
        self.assertFalse(campaign["sending_available"])
        self.assertEqual(campaign["customer_ids"], [allowed["id"]])
        self.client.patch("/api/admin/customers/" + allowed["id"],
                          json={"marketing_opt_in": False}, headers=self.headers)
        saved = self.client.get("/api/admin/campaigns").json()["campaigns"][0]
        self.assertEqual(saved["audience_count"], 0)
        self.assertEqual(saved["customer_ids"], [])

    def test_feedback_requires_matching_token_delivery_and_one_review(self):
        order = self.order()
        path = "/api/orders/" + order["id"] + "/review"
        base = {"token": "secret-tracking-token", "rating": 5, "comment": "Excelente"}
        self.assertEqual(self.client.post(path, json=base | {"token": "wrong"}).status_code, 404)
        self.assertEqual(self.client.post(path, json=base).status_code, 409)
        with db(self.request) as conn:
            conn.execute("UPDATE orders SET status = 'delivered', delivered_at = ? WHERE id = ?", (utcnow(), order["id"]))
        self.assertEqual(self.client.post(path, json=base | {"rating": True}).status_code, 422)
        response = self.client.post(path, json=base)
        self.assertEqual(response.status_code, 201, response.text)
        for private in ("customer_name", "customer_phone", "tracking_token", "order_id", "comment"):
            self.assertNotIn(private, response.json()["review"])
        self.assertEqual(self.client.post(path, json=base).status_code, 409)
        self.assertEqual(len(self.client.get("/api/admin/reviews").json()["reviews"]), 1)


if __name__ == "__main__":
    unittest.main()
