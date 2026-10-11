"""Persistent display sequence; internal IDs and customer access stay separate."""
from concurrent.futures import ThreadPoolExecutor
import os
from pathlib import Path
from tempfile import TemporaryDirectory
from types import SimpleNamespace
import unittest
from unittest.mock import patch
from uuid import uuid4

from fastapi.testclient import TestClient
from server import core, operations, whatsapp
from server.main import create_app, OrderInput
from server.order_service import register_order


class OrderNumberTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.password_hash = core.hash_password('number-tests-only')

    def setUp(self):
        self.environment = patch.dict(os.environ, {'SAHARA_WHATSAPP_ENABLED': '0'})
        self.environment.start()
        self.directory = TemporaryDirectory()
        self.data_dir = Path(self.directory.name)
        self.app = self.make_app()
        self.request = SimpleNamespace(app=self.app)
        self.client = TestClient(self.app)  # no worker or real messages

    def tearDown(self):
        self.client.close()
        self.directory.cleanup()
        self.environment.stop()

    def make_app(self):
        return create_app(data_dir=self.data_dir, admin_password_hash=self.password_hash, secure_cookie=False)

    def body(self, **values):
        return {'items': [{'id': 'carne', 'quantity': 1}],
                'customer': {'name': 'Teste', 'phone': '44999998888'},
                'delivery': {'street': 'Avenida teste', 'number': '25', 'neighborhood': 'Centro'},
                'payment_method': 'Pix', 'idempotency_key': uuid4().hex, **values}

    def register(self, body=None, source='web'):
        with core.db(self.request) as conn:
            return register_order(conn, OrderInput.model_validate(body or self.body()), source)

    def test_shared_sequence_and_replay_keep_internal_id(self):
        first_body = self.body()
        first = self.register(first_body)
        replay = self.register(first_body)
        second = self.register(source='whatsapp')
        third = self.register(source='pdv')
        self.assertEqual([row['display_number'] for row in (first, second, third)], ['001', '002', '003'])
        self.assertEqual(first['id'], replay['id'])
        self.assertEqual(replay['display_number'], '001')
        self.assertTrue(replay['replayed'])
        self.assertEqual(len(first['id']), 32)
        self.assertNotEqual(first['id'], first['display_number'])

    def test_concurrent_orders_are_unique_and_consecutive(self):
        with ThreadPoolExecutor(max_workers=6) as pool:
            results = list(pool.map(lambda _: self.register(), range(12)))
        self.assertEqual(sorted(row['order_number'] for row in results), list(range(1, 13)))
        self.assertEqual(len({row['id'] for row in results}), 12)

    def test_concurrent_replay_does_not_consume_a_number(self):
        body = self.body()
        with ThreadPoolExecutor(max_workers=4) as pool:
            results = list(pool.map(lambda _: self.register(body), range(4)))
        self.assertEqual({row['display_number'] for row in results}, {'001'})
        self.assertEqual(len({row['id'] for row in results}), 1)
        self.assertEqual(self.register()['display_number'], '002')

    def test_failed_registration_rolls_back_number(self):
        with patch.object(operations, 'apply_stock', side_effect=ValueError('Stock unavailable')):
            with self.assertRaises(ValueError):
                self.register()
        self.assertEqual(self.register()['display_number'], '001')

    def test_restart_does_not_reset_sequence_and_supports_four_digits(self):
        self.register()
        self.app = self.make_app()
        self.request = SimpleNamespace(app=self.app)
        self.assertEqual(self.register()['display_number'], '002')
        with core.db(self.request) as conn:
            conn.execute('UPDATE order_number_sequence SET value=999')
        self.assertEqual(self.register()['display_number'], '1000')

    def test_tracking_still_requires_secret_and_internal_id(self):
        response = self.client.post('/api/orders', json=self.body())
        self.assertEqual(response.status_code, 201, response.text)
        order = response.json()
        self.assertEqual(order['display_number'], '001')
        url = f"/api/orders/{order['id']}/track"
        self.assertEqual(self.client.get(url, params={'token': 'wrong'}).status_code, 404)
        tracked = self.client.get(url, params={'token': order['tracking_token']})
        self.assertEqual(tracked.json()['display_number'], '001')
        self.assertEqual(self.client.get('/api/orders/001/track', params={'token': order['tracking_token']}).status_code, 404)

    def test_whatsapp_number_lookup_only_returns_senders_own_orders(self):
        own = self.register()
        other = self.register(self.body(customer={'name': 'Outro', 'phone': '11988887777'}))
        with core.db(self.request) as conn:
            result = whatsapp.answer(conn, '5544999998888', 'Status pedido #001')
            self.assertIn('#001', result)
            self.assertIn('#001', whatsapp.answer(conn, '5544999998888', '#001'))
            self.assertNotIn(own['id'], result)
            self.assertIn('Não encontrei', whatsapp.answer(conn, '5544999998888', 'Status pedido #002'))
            self.assertIn('Não encontrei', whatsapp.answer(conn, '5544999998888', 'Pedido ' + other['id']))

    def test_existing_orders_backfill_once_in_creation_order(self):
        older = self.register(source='whatsapp')
        newer = self.register()
        imported = self.register()
        with core.db(self.request) as conn:
            conn.execute("UPDATE orders SET created_at='2026-01-01T00:00:00+00:00' WHERE id=?", (older['id'],))
            conn.execute("UPDATE orders SET created_at='2026-02-01T00:00:00+00:00' WHERE id=?", (newer['id'],))
            conn.execute("UPDATE orders SET source='ifood' WHERE id=?", (imported['id'],))
            conn.execute('DROP INDEX orders_number')
            conn.execute('ALTER TABLE orders DROP COLUMN order_number')
            conn.execute('DROP TABLE order_number_sequence')
        app = self.make_app()
        self.make_app()  # repeat migration does not renumber
        self.request = SimpleNamespace(app=app)
        with core.db(self.request) as conn:
            rows = {row['id']: dict(row) for row in conn.execute('SELECT * FROM orders')}
            self.assertEqual(rows[older['id']]['order_number'], 1)
            self.assertEqual(rows[newer['id']]['order_number'], 2)
            self.assertIsNone(rows[imported['id']]['order_number'])
            self.assertEqual(rows[older['id']]['tracking_token'], older['tracking_token'])
        self.assertEqual(self.register()['display_number'], '003')

    def test_deleted_orders_do_not_allow_number_reuse(self):
        first = self.register()
        with core.db(self.request) as conn:
            conn.execute('DELETE FROM order_events WHERE order_id=?', (first['id'],))
            conn.execute('DELETE FROM orders WHERE id=?', (first['id'],))
        self.make_app()
        self.assertEqual(self.register()['display_number'], '002')
