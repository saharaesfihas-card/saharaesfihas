"""Authenticated, incremental arrivals; no polling mutations or external integrations."""
import unittest
from uuid import uuid4

from server import core
from server.tests import test_order_numbers as fixtures


class IncomingOrderTests(unittest.TestCase):
    setUpClass = classmethod(fixtures.OrderNumberTests.setUpClass.__func__)
    setUp = fixtures.OrderNumberTests.setUp
    tearDown = fixtures.OrderNumberTests.tearDown
    make_app = fixtures.OrderNumberTests.make_app
    body = fixtures.OrderNumberTests.body
    register = fixtures.OrderNumberTests.register

    def login(self):
        response = self.client.post('/api/admin/login', json={'password': 'number-tests-only'})
        self.assertEqual(response.status_code, 200)
        return {'X-Sahara-CSRF': response.json()['csrf_token']}

    def feed(self, cursor=None):
        response = self.client.get('/api/admin/orders/incoming', params={} if cursor is None else {'cursor': cursor})
        self.assertEqual(response.status_code, 200, response.text)
        self.assertEqual(response.headers['cache-control'], 'no-store')
        return response.json()

    def test_private_feed_rejects_invalid_cursor_and_is_packaged(self):
        self.assertEqual(self.client.get('/api/admin/orders/incoming').status_code, 401)
        self.login()
        for cursor in ['-1', 'abc', '9223372036854775808']:
            self.assertEqual(self.client.get('/api/admin/orders/incoming', params={'cursor': cursor}).status_code, 422)
        asset = self.client.get('/admin-order-alerts.js')
        self.assertEqual(asset.status_code, 200)
        self.assertIn('sahara-new-order-monitor', asset.text)
        self.assertIn('admin-order-alerts.js', (core.ROOT / 'Dockerfile').read_text())

    def test_baseline_excludes_history_and_new_web_whatsapp_pdv_orders_arrive_once(self):
        previous = self.register()
        headers = self.login()
        baseline = self.feed()
        self.assertEqual(baseline['orders'], [])
        expected = [self.register(source=source) for source in ['web', 'whatsapp', 'pdv']]
        incoming = self.feed(baseline['cursor'])
        self.assertEqual([o['id'] for o in incoming['orders']], [o['id'] for o in expected])
        self.assertNotIn(previous['id'], [o['id'] for o in incoming['orders']])
        self.assertEqual([o['source'] for o in incoming['orders']], ['web', 'whatsapp', 'pdv'])
        self.assertTrue(all('tracking_token' not in o and 'idempotency_key' not in o for o in incoming['orders']))
        self.client.patch('/api/admin/orders/' + expected[0]['id'], json={'status': 'ready'}, headers=headers)
        self.assertEqual(self.feed(incoming['cursor'])['orders'], [])
        self.assertEqual(self.feed()['orders'], [])  # A reload establishes a new baseline.

    def test_replayed_creation_does_not_become_another_arrival(self):
        self.login()
        cursor = self.feed()['cursor']
        body = self.body()
        self.register(body); self.register(body)
        self.assertEqual(len(self.feed(cursor)['orders']), 1)

    def test_backdated_ifood_import_and_large_batch_use_arrival_order_not_timestamp(self):
        self.login()
        cursor = self.feed()['cursor']
        template = self.register()
        with core.db(self.request) as conn:
            columns = [r[1] for r in conn.execute('PRAGMA table_info(orders)')]
            row = dict(conn.execute('SELECT * FROM orders WHERE id=?', (template['id'],)).fetchone())
            for index in range(51):
                values = row | {'id': uuid4().hex, 'idempotency_key': uuid4().hex,
                                'created_at': '2019-01-01T00:00:00+00:00', 'source': 'ifood', 'order_number': None}
                conn.execute('INSERT INTO orders (' + ','.join(columns) + ') VALUES (' + ','.join('?' for _ in columns) + ')', [values[k] for k in columns])
        first = self.feed(cursor)
        self.assertEqual(len(first['orders']), 50); self.assertTrue(first['has_more'])
        second = self.feed(first['cursor'])
        self.assertEqual(len(second['orders']), 2); self.assertFalse(second['has_more'])
        self.assertEqual(len({r['id'] for r in first['orders'] + second['orders']}), 52)
        self.assertEqual(self.feed(second['cursor'])['orders'], [])
