"""Disponibilidade persistente e bloqueio transacional de produtos pausados."""
from pathlib import Path
from tempfile import TemporaryDirectory
from types import SimpleNamespace
import unittest
from uuid import uuid4

from fastapi import HTTPException
from fastapi.testclient import TestClient

from server.core import db, hash_password
from server.main import create_app, OrderInput
from server.order_service import register_order


class CatalogTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.password_hash = hash_password('catalog-test-password')

    def setUp(self):
        self.directory = TemporaryDirectory()
        self.data_dir = Path(self.directory.name) / 'private'
        self.app = self.make_app()
        self.client = TestClient(self.app)
        self.request = SimpleNamespace(app=self.app)
        login = self.client.post('/api/admin/login', json={'password': 'catalog-test-password'})
        self.headers = {'X-Sahara-CSRF': login.json()['csrf_token']}
        self.product_count = len(self.client.get('/api/catalog').json()['products'])

    def make_app(self):
        return create_app(data_dir=self.data_dir, admin_password_hash=self.password_hash, secure_cookie=False)

    def tearDown(self):
        self.client.close()
        self.directory.cleanup()

    def toggle(self, value, identifier='carne'):
        response = self.client.patch('/api/admin/products/' + identifier + '/availability', json={'available': value}, headers=self.headers)
        self.assertEqual(response.status_code, 200, response.text)
        return response.json()['product']

    def body(self):
        return {'items': [{'id': 'carne', 'quantity': 2}], 'customer': {'name': 'Teste', 'phone': '44999998888'},
                'delivery': {'street': 'Rua teste', 'number': '25', 'neighborhood': 'Centro'},
                'payment_method': 'Pix', 'idempotency_key': uuid4().hex}

    def test_private_controls_require_session_csrf_and_strict_boolean(self):
        url = '/api/admin/products/carne/availability'
        self.assertEqual(self.client.patch(url, json={'available': False}).status_code, 403)
        self.assertEqual(self.client.patch(url, json={'available': 'false'}, headers=self.headers).status_code, 422)
        self.assertEqual(self.client.patch(url, json={'available': 0}, headers=self.headers).status_code, 422)
        self.assertEqual(self.client.patch('/api/admin/products/not-a-product/availability', json={'available': False}, headers=self.headers).status_code, 404)
        self.client.cookies.clear()
        self.assertEqual(self.client.get('/api/admin/products').status_code, 401)
        self.assertEqual(self.client.patch(url, json={'available': False}, headers=self.headers).status_code, 401)

    def test_pause_hides_public_item_keeps_it_manageable_and_reactivates(self):
        before = next(row for row in self.client.get('/api/catalog').json()['products'] if row['id'] == 'carne')
        paused = self.toggle(False)
        self.assertFalse(paused['available'])
        self.assertEqual(paused['price_cents'], before['price_cents'])
        public = self.client.get('/api/catalog')
        self.assertNotIn('carne', [row['id'] for row in public.json()['products']])
        self.assertEqual(public.headers['cache-control'], 'no-store')
        private = self.client.get('/api/admin/products').json()['products']
        self.assertFalse(next(row for row in private if row['id'] == 'carne')['available'])
        self.toggle(False)  # Repetir a pausa é seguro.
        self.toggle(True)
        self.assertIn('carne', [row['id'] for row in self.client.get('/api/catalog').json()['products']])

    def test_stale_web_and_pdv_orders_are_rejected_without_side_effects(self):
        self.toggle(False)
        for path in ('/api/orders', '/api/admin/orders'):
            response = self.client.post(path, json=self.body(), headers=self.headers)
            self.assertEqual(response.status_code, 409)
            self.assertIn('indisponível', response.json()['detail'])
        with db(self.request) as conn:
            self.assertEqual(conn.execute('SELECT COUNT(*) FROM orders').fetchone()[0], 0)
            self.assertEqual(conn.execute('SELECT COUNT(*) FROM customers').fetchone()[0], 0)
            self.assertEqual(conn.execute('SELECT COUNT(*) FROM order_events').fetchone()[0], 0)
            self.assertEqual(conn.execute('SELECT COUNT(*) FROM whatsapp_outbox').fetchone()[0], 0)
        self.toggle(True)
        self.assertEqual(self.client.post('/api/orders', json=self.body()).status_code, 201)

    def test_shared_whatsapp_checkout_is_blocked_too(self):
        self.toggle(False)
        with self.assertRaises(HTTPException) as error:
            with db(self.request) as conn:
                register_order(conn, OrderInput.model_validate(self.body()), source='whatsapp')
        self.assertEqual(error.exception.status_code, 409)

    def test_whatsapp_can_remove_an_item_paused_after_it_was_added(self):
        from server import whatsapp_checkout
        recipient = '5544999998888'
        with db(self.request) as conn:
            cart = whatsapp_checkout.start(conn, recipient)
            whatsapp_checkout.change_items(conn, cart, [{'id': 'carne', 'quantity': 1, 'action': 'add'}])
        self.toggle(False)
        with db(self.request) as conn:
            cart = whatsapp_checkout.load(conn, recipient)
            with self.assertRaisesRegex(ValueError, 'indisponível'):
                whatsapp_checkout.change_items(conn, cart, [{'id': 'carne', 'quantity': 1, 'action': 'add'}])
            parsed = whatsapp_checkout.parse_items(conn, 'remover carne')
            self.assertIsNotNone(parsed)
            whatsapp_checkout.change_items(conn, cart, parsed)
            self.assertEqual(cart['data']['items'], [])

    def test_existing_orders_and_idempotent_replays_survive_pause(self):
        body = self.body()
        response = self.client.post('/api/orders', json=body)
        self.assertEqual(response.status_code, 201)
        order = response.json()
        self.toggle(False)
        replay = self.client.post('/api/orders', json=body)
        self.assertEqual(replay.status_code, 201)
        self.assertTrue(replay.json()['replayed'])
        self.assertEqual(replay.json()['id'], order['id'])
        update = self.client.patch('/api/admin/orders/' + order['id'], json={'status': 'ready'}, headers=self.headers)
        self.assertEqual(update.status_code, 200)
        self.assertEqual(update.json()['order']['items'][0]['id'], 'carne')

    def test_restart_preserves_pause_and_migration_defaults_old_products_to_active(self):
        self.toggle(False)
        restarted = self.make_app()
        with TestClient(restarted) as client:
            self.assertNotIn('carne', [row['id'] for row in client.get('/api/catalog').json()['products']])
        with db(self.request) as conn:
            conn.execute('ALTER TABLE products DROP COLUMN available')
        migrated = self.make_app()
        with TestClient(migrated) as client:
            products = client.get('/api/catalog').json()['products']
            self.assertEqual(len(products), self.product_count)
            self.assertTrue(all(row['available'] for row in products))

    def test_all_paused_products_can_still_be_reactivated_from_private_list(self):
        with db(self.request) as conn:
            conn.execute('UPDATE products SET available=0')
        self.assertEqual(self.client.get('/api/catalog').json()['products'], [])
        self.assertEqual(len(self.client.get('/api/admin/products').json()['products']), self.product_count)
        self.toggle(True)
        self.assertEqual([row['id'] for row in self.client.get('/api/catalog').json()['products']], ['carne'])


if __name__ == '__main__':
    unittest.main()
