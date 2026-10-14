"""Preços de ofertas pausadas, calendário local e confirmação de carrinhos."""
from datetime import datetime, timezone
from pathlib import Path
from tempfile import TemporaryDirectory
from types import SimpleNamespace
from unittest.mock import patch
from uuid import uuid4
from zoneinfo import ZoneInfo
import unittest

from fastapi.testclient import TestClient

from server import product_promotions, whatsapp_ai, whatsapp_checkout
from server.core import db, hash_password
from server.main import create_app


class ProductPromotionTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.password_hash = hash_password('promotions-test-password')

    def setUp(self):
        self.directory = TemporaryDirectory()
        self.data_dir = Path(self.directory.name) / 'private'
        self.app = create_app(data_dir=self.data_dir, admin_password_hash=self.password_hash, secure_cookie=False)
        self.client = TestClient(self.app)
        self.request = SimpleNamespace(app=self.app)
        login = self.client.post('/api/admin/login', json={'password': 'promotions-test-password'})
        self.headers = {'X-Sahara-CSRF': login.json()['csrf_token']}

    def tearDown(self):
        self.client.close()
        self.directory.cleanup()

    def time(self, weekday):
        return patch.object(product_promotions, 'local_now', return_value=datetime(
            2026, 10, 19 + weekday, 18, tzinfo=ZoneInfo('America/Sao_Paulo')))

    def prices(self):
        return {p['id']: p for p in self.client.get('/api/catalog').json()['products']}

    def toggle(self, identifier, enabled):
        response = self.client.patch('/api/admin/product-promotions/' + identifier,
                                     json={'enabled': enabled}, headers=self.headers)
        self.assertEqual(response.status_code, 200, response.text)
        return response.json()

    def body(self, product='carne', quantity=1):
        return {'items': [{'id': product, 'quantity': quantity}], 'customer': {'name': 'Teste'},
                'delivery': {'street': 'Rua teste', 'number': '25', 'neighborhood': 'Centro'},
                'payment_method': 'Pix', 'idempotency_key': uuid4().hex}

    def test_seeded_offers_are_paused_and_public_prices_unchanged(self):
        with self.time(2):
            offers = self.client.get('/api/admin/product-promotions').json()['promotions']
            self.assertEqual([p['product_id'] for p in offers], ['calabresa', 'frango', 'carne'])
            self.assertTrue(all(p['price_cents'] == 299 and not p['enabled'] and not p['effective_today'] for p in offers))
            products = self.prices()
            self.assertEqual(len(products), 57)
            self.assertTrue(all('promotion' not in p for p in products.values()))
            for identifier in ('calabresa', 'frango', 'carne'):
                self.assertEqual(products[identifier]['price_cents'], 400)

    def test_only_matching_weekday_gets_price_and_existing_products_are_preserved(self):
        for identifier, _, _ in product_promotions.DEFAULTS:
            self.toggle(identifier, True)
        for weekday in range(7):
            with self.subTest(weekday=weekday), self.time(weekday):
                products = self.prices()
                self.assertEqual(len(products), 57)
                for identifier, _, day in product_promotions.DEFAULTS:
                    product = products[identifier]
                    self.assertEqual(product['price_cents'], 299 if weekday == day else 400)
                    self.assertEqual('promotion' in product, weekday == day)
                self.assertEqual(products['queijo']['price_cents'], 400)
                self.assertEqual(products['combo-10']['price_cents'], 3500)

    def test_calendar_uses_maringa_day_at_utc_midnight(self):
        self.toggle('calabresa', True)
        for hour, expected in ((2, 299), (3, 400)):
            frozen = datetime(2026, 10, 20, hour, tzinfo=timezone.utc)
            with patch.object(product_promotions, 'datetime') as clock:
                clock.now.side_effect = lambda tz: frozen.astimezone(tz)
                self.assertEqual(self.prices()['calabresa']['price_cents'], expected)

    def test_programming_requires_auth_csrf_boolean_and_known_offer(self):
        url = '/api/admin/product-promotions/carne'
        self.assertEqual(self.client.patch(url, json={'enabled': True}).status_code, 403)
        for value in ('true', 1, None):
            self.assertEqual(self.client.patch(url, json={'enabled': value}, headers=self.headers).status_code, 422)
        self.assertEqual(self.client.patch('/api/admin/product-promotions/queijo', json={'enabled': True}, headers=self.headers).status_code, 404)
        self.client.cookies.clear()
        self.assertEqual(self.client.get('/api/admin/product-promotions').status_code, 401)
        self.assertEqual(self.client.patch(url, json={'enabled': True}, headers=self.headers).status_code, 401)

    def test_pause_overrides_calendar_and_restart_preserves_owner_choice(self):
        self.toggle('carne', True)
        with self.time(2):
            self.assertEqual(self.prices()['carne']['price_cents'], 299)
            self.toggle('carne', False)
            self.assertEqual(self.prices()['carne']['price_cents'], 400)
        self.toggle('frango', True)
        restarted = create_app(data_dir=self.data_dir, admin_password_hash=self.password_hash, secure_cookie=False)
        with self.time(1), TestClient(restarted) as client:
            products = {p['id']: p for p in client.get('/api/catalog').json()['products']}
            self.assertEqual(products['frango']['price_cents'], 299)
            self.assertEqual(products['carne']['price_cents'], 400)

    def test_product_availability_remains_independent_of_promotions(self):
        self.toggle('carne', True)
        self.client.patch('/api/admin/products/carne/availability', json={'available': False}, headers=self.headers)
        with self.time(2):
            self.assertNotIn('carne', self.prices())
            self.assertEqual(self.client.post('/api/orders', json=self.body()).status_code, 409)
            offer = next(p for p in self.client.get('/api/admin/product-promotions').json()['promotions'] if p['product_id'] == 'carne')
            self.assertTrue(offer['enabled'])
            self.assertFalse(offer['effective_today'])

    def test_no_minimum_or_five_unit_limit_and_all_order_channels_share_price(self):
        self.toggle('carne', True)
        with self.time(2):
            for path, quantity in (('/api/orders', 1), ('/api/orders', 6), ('/api/admin/orders', 10)):
                body = self.body(quantity=quantity)
                body['items'].append({'id': 'queijo', 'quantity': 1})
                response = self.client.post(path, json=body, headers=self.headers)
                self.assertEqual(response.status_code, 201, response.text)
                self.assertEqual(response.json()['subtotal_cents'], quantity * 299 + 400)
            with db(self.request) as conn:
                from server.main import OrderInput
                from server.order_service import register_order
                order = register_order(conn, OrderInput.model_validate(self.body(quantity=6)), 'whatsapp')
                self.assertEqual(order['subtotal_cents'], 1794)
                self.assertEqual(conn.execute("SELECT price_cents FROM products WHERE id='carne'").fetchone()[0], 400)

    def test_existing_order_and_replay_keep_original_promotional_price(self):
        self.toggle('carne', True)
        body = self.body(quantity=3)
        body['expected_subtotal_cents'] = 897
        with self.time(2):
            before = self.client.post('/api/orders', json=body).json()
        with self.time(3):
            replay = self.client.post('/api/orders', json=body)
        self.assertEqual(replay.status_code, 201)
        self.assertTrue(replay.json()['replayed'])
        self.assertEqual(replay.json()['id'], before['id'])
        self.assertEqual(replay.json()['subtotal_cents'], 897)

    def test_expired_web_quote_is_rejected_without_side_effects(self):
        body = self.body()
        body['expected_subtotal_cents'] = 299
        response = self.client.post('/api/orders', json=body)
        self.assertEqual(response.status_code, 409)
        self.assertIn('preços', response.json()['detail'])
        with db(self.request) as conn:
            for table in ('orders', 'customers', 'order_events', 'whatsapp_outbox'):
                self.assertEqual(conn.execute(f'SELECT COUNT(*) FROM {table}').fetchone()[0], 0)

    def test_whatsapp_review_requires_reconfirmation_if_offer_expires(self):
        self.toggle('carne', True)
        with self.time(2), db(self.request) as conn:
            cart = whatsapp_checkout.start(conn, '5544999988888')
            cart['data'] = {'items': [{'id': 'carne', 'quantity': 6}], 'name': 'Teste',
                            'street': 'Rua teste', 'number': '25', 'neighborhood': 'Centro',
                            'complement': '', 'notes': '', 'payment_method': 'Pix'}
            whatsapp_checkout.advance(conn, cart)
            self.assertIn('17,94', whatsapp_checkout.summary(conn, cart))
        with self.time(3), db(self.request) as conn:
            reply = whatsapp_checkout.confirm(conn, cart)
            self.assertIn('confirme novamente', reply)
            self.assertIn('24,00', reply)
            self.assertEqual(conn.execute('SELECT COUNT(*) FROM orders').fetchone()[0], 0)

    def test_ai_renders_only_current_server_prices(self):
        self.toggle('carne', True)
        result = {'intent': 'products', 'product_ids': ['carne'],
                  'reply': '{{name:carne}} custa {{price:carne}}.'}
        with self.time(2), db(self.request) as conn:
            self.assertIn('R$ 2,99', whatsapp_ai.render(conn, '', result))
        with self.time(3), db(self.request) as conn:
            self.assertIn('R$ 4,00', whatsapp_ai.render(conn, '', result))


if __name__ == '__main__':
    unittest.main()
