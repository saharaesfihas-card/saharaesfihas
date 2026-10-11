"""Atribuição e envio de comanda em SQLite temporária, sem WhatsApp real."""
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta, timezone
import json
import os
from pathlib import Path
from tempfile import TemporaryDirectory
from types import SimpleNamespace
import unittest
from unittest.mock import patch
from urllib.error import URLError
from urllib.parse import parse_qs, urlsplit
from uuid import uuid4

from fastapi.testclient import TestClient

from server import core, driver_dispatch, evolution, operations, whatsapp
from server.core import db, hash_password
from server.main import create_app


class DriverDispatchTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.password_hash = hash_password('driver-tests-only')

    def setUp(self):
        self.environment = patch.dict(os.environ, {
            'SAHARA_WHATSAPP_PROVIDER': 'evolution', 'SAHARA_WHATSAPP_ENABLED': '1',
            'SAHARA_EVOLUTION_URL': 'https://evolution.example',
            'SAHARA_EVOLUTION_API_KEY': 'test-key', 'SAHARA_EVOLUTION_INSTANCE': 'sahara',
            'SAHARA_EVOLUTION_WEBHOOK_SECRET': 'test-secret',
        })
        self.environment.start()
        self.directory = TemporaryDirectory()
        self.app = create_app(data_dir=Path(self.directory.name) / 'private',
                              admin_password_hash=self.password_hash, secure_cookie=False)
        self.request = SimpleNamespace(app=self.app)
        # Worker is advanced explicitly; no real connection or outbound call.
        self.client = TestClient(self.app)
        login = self.client.post('/api/admin/login', json={'password': 'driver-tests-only'})
        self.headers = {'X-Sahara-CSRF': login.json()['csrf_token']}
        self.driver = self.add_driver('Entregador', '(44) 99999-2222')
        response = self.client.post('/api/orders', json={
            'items': [{'id': 'carne', 'quantity': 2}, {'id': 'queijo', 'quantity': 1}],
            'customer': {'name': 'Cliente teste', 'phone': '44999998888', 'whatsapp_opt_in': False},
            'delivery': {'street': 'Avenida Teste', 'number': '25', 'neighborhood': 'Centro',
                         'complement': 'Casa azul'},
            'notes': 'Sem cebola', 'payment_method': 'Dinheiro', 'idempotency_key': uuid4().hex,
        })
        self.assertEqual(response.status_code, 201, response.text)
        self.order_id = response.json()['id']

    def tearDown(self):
        self.client.close()
        self.directory.cleanup()
        self.environment.stop()

    def add_driver(self, name, phone):
        response = self.client.post('/api/admin/drivers', json={'name': name, 'phone': phone}, headers=self.headers)
        self.assertEqual(response.status_code, 200, response.text)
        return next(driver for driver in response.json()['drivers'] if driver['name'] == name)

    def assign(self, driver=None):
        return self.client.patch(f'/api/admin/orders/{self.order_id}/driver',
                                 json={'driver_id': (driver or self.driver)['id']}, headers=self.headers)

    def rows(self):
        with db(self.request) as conn:
            return [dict(row) for row in conn.execute('SELECT * FROM whatsapp_outbox ORDER BY rowid')]

    def open_connection(self):
        with db(self.request) as conn:
            whatsapp.cache_evolution_connection(conn, whatsapp.config(), 'open')

    def test_assignment_receipt_maps_and_store_session_send(self):
        response = self.assign()
        self.assertEqual(response.status_code, 200, response.text)
        self.assertEqual(response.json()['dispatch']['status'], 'pending')
        row = self.rows()[0]
        self.assertEqual(row['phone'], '5544999992222')
        self.assertEqual(row['kind'], 'driver_dispatch')
        self.assertEqual(row['provider'], 'evolution')
        text = row['body']
        self.assertIn('Pedido #001\n', text)
        self.assertNotIn(self.order_id, text)
        for value in ['Cliente teste', '44999998888', 'Avenida Teste', '25', 'Centro', 'Casa azul',
                      'Sem cebola', '2 × Carne — R$ 8,00', '1 × Queijo — R$ 4,00',
                      'Total dos produtos: R$ 12,00', 'Pagamento: Dinheiro', 'A RECEBER pelos produtos: R$ 12,00']:
            self.assertIn(value, text)
        url = next(line for line in text.splitlines() if line.startswith('https://www.google.com/maps/'))
        self.assertEqual(parse_qs(urlsplit(url).query)['destination'], ['Avenida Teste, 25, Centro, Maringá, PR'])
        self.open_connection()
        with patch.object(evolution, 'api', return_value={'key': {'id': 'driver-test-message'}}) as api:
            self.assertTrue(whatsapp.process_one(self.app))
            cfg, path, payload = api.call_args.args
        self.assertEqual(cfg['SAHARA_EVOLUTION_INSTANCE'], 'sahara')
        self.assertEqual(path, '/message/sendText/sahara')
        self.assertEqual(payload['number'], row['phone'])
        self.assertEqual(payload['text'], text)
        self.assertEqual(self.rows()[0]['status'], 'accepted')
        self.assign()
        self.assertEqual(len(self.rows()), 1)
        self.assertEqual(self.assign().json()['dispatch']['status'], 'accepted')

    def test_concurrent_repeated_assignment_queues_once(self):
        with ThreadPoolExecutor(max_workers=2) as pool:
            responses = list(pool.map(lambda _: self.assign(), range(2)))
        self.assertTrue(all(response.status_code == 200 for response in responses))
        self.assertEqual(len(self.rows()), 1)
        self.assertEqual(responses[0].json()['order']['courier_assignment_key'],
                         responses[1].json()['order']['courier_assignment_key'])

    def test_gps_takes_precedence_and_untrusted_url_is_not_sent(self):
        with db(self.request) as conn:
            address = {'street': 'Avenida Teste', 'number': '25', 'neighborhood': 'Centro',
                       'location': {'latitude': -23.42, 'longitude': -51.93, 'url': 'https://untrusted.example/'}}
            conn.execute('UPDATE orders SET delivery_json=? WHERE id=?', (json.dumps(address), self.order_id))
        self.assign()
        text = self.rows()[0]['body']
        url = next(line for line in text.splitlines() if line.startswith('https://www.google.com/maps/'))
        self.assertEqual(parse_qs(urlsplit(url).query)['destination'], ['-23.42,-51.93'])
        self.assertNotIn('untrusted.example', text)

    def test_reassignment_cancels_unsent_message_and_new_assignment_has_new_key(self):
        self.assign()
        original = self.rows()[0]
        other = self.add_driver('Outro entregador', '44999993333')
        self.assign(other)
        rows = self.rows()
        self.assertEqual(rows[0]['status'], 'skipped')
        self.assertEqual(rows[1]['phone'], '5544999993333')
        with db(self.request) as conn:
            payload, status, _ = whatsapp.payload(conn, original, whatsapp.config())
        self.assertIsNone(payload)
        self.assertEqual(status, 'skipped')
        self.assign(self.driver)
        self.assertEqual(len(self.rows()), 3)
        self.assertNotEqual(self.rows()[2]['event_key'], original['event_key'])

    def test_removing_driver_cancels_pending_dispatch_without_new_message(self):
        self.assign()
        response = self.client.patch(f'/api/admin/orders/{self.order_id}/driver', json={'driver_id': None}, headers=self.headers)
        self.assertEqual(response.json()['dispatch']['status'], 'removed')
        self.assertEqual(len(self.rows()), 1)
        self.assertEqual(self.rows()[0]['status'], 'skipped')

    def test_cancelled_delivered_changed_phone_and_inactive_driver_are_checked_before_send(self):
        self.assign()
        row = self.rows()[0]
        with db(self.request) as conn:
            for status in ['cancelled', 'delivered']:
                conn.execute('UPDATE orders SET status=? WHERE id=?', (status, self.order_id))
                self.assertIsNone(whatsapp.payload(conn, row, whatsapp.config())[0])
            conn.execute("UPDATE orders SET status='ready' WHERE id=?", (self.order_id,))
            conn.execute("UPDATE drivers SET phone='44999994444' WHERE id=?", (self.driver['id'],))
            self.assertIsNone(whatsapp.payload(conn, row, whatsapp.config())[0])
            conn.execute("UPDATE drivers SET phone='44999992222',active=0 WHERE id=?", (self.driver['id'],))
            self.assertIsNone(whatsapp.payload(conn, row, whatsapp.config())[0])

    def test_disconnected_queue_waits_and_then_sends(self):
        self.assign()
        with patch.object(evolution, 'connection', return_value='close'), patch.object(whatsapp, 'send') as send:
            self.assertFalse(whatsapp.process_one(self.app))
            send.assert_not_called()
        self.assertEqual(self.rows()[0]['status'], 'pending')
        self.open_connection()
        with patch.object(whatsapp, 'send', return_value='evolution:reconnected'):
            self.assertTrue(whatsapp.process_one(self.app))
        self.assertEqual(self.rows()[0]['status'], 'accepted')

    def test_invalid_phone_missing_address_and_disabled_whatsapp_are_visible(self):
        for telephone in ['', '123', 'invalid-44999992222', '00999992222']:
            with db(self.request) as conn:
                conn.execute('UPDATE drivers SET phone=? WHERE id=?', (telephone, self.driver['id']))
            response = self.assign()
            self.assertEqual(response.status_code, 200)
            self.assertEqual(response.json()['dispatch']['status'], 'invalid_phone')
            self.assertEqual(response.json()['order']['courier_id'], self.driver['id'])
            self.assertEqual(self.rows(), [])
        with db(self.request) as conn:
            conn.execute("UPDATE drivers SET phone='44999992222' WHERE id=?", (self.driver['id'],))
        with patch.dict(os.environ, {'SAHARA_WHATSAPP_ENABLED': '0'}):
            self.assertEqual(self.assign().json()['dispatch']['status'], 'not_configured')
        self.assertEqual(self.rows(), [])
        with db(self.request) as conn:
            conn.execute("UPDATE orders SET delivery_json='{}' WHERE id=?", (self.order_id,))
        self.assertEqual(self.assign().json()['dispatch']['status'], 'missing_address')
        self.assertEqual(self.rows(), [])

    def test_fixing_configuration_allows_same_assignment_without_duplicate(self):
        with patch.dict(os.environ, {'SAHARA_WHATSAPP_ENABLED': '0'}):
            self.assign()
        self.assign()
        self.assign()
        self.assertEqual(len(self.rows()), 1)

    def test_updating_driver_phone_then_clicking_assignment_uses_new_number(self):
        self.assign()
        with db(self.request) as conn:
            conn.execute("UPDATE drivers SET phone='44999995555' WHERE id=?", (self.driver['id'],))
        self.assign()
        self.assign()
        rows = self.rows()
        self.assertEqual(len(rows), 2)
        self.assertEqual(rows[0]['status'], 'skipped')
        self.assertEqual(rows[1]['phone'], '5544999995555')

    def test_payment_changed_before_send_blocks_old_receipt_and_assignment_refreshes_it(self):
        self.assign()
        original = self.rows()[0]
        with db(self.request) as conn:
            conn.execute("UPDATE orders SET payment_status='paid' WHERE id=?", (self.order_id,))
            body, status, reason = whatsapp.payload(conn, original, whatsapp.config())
        self.assertIsNone(body)
        self.assertEqual(status, 'skipped')
        self.assertIn('atualizado', reason)
        self.assign()
        rows = self.rows()
        self.assertEqual(len(rows), 2)
        self.assertEqual(rows[0]['status'], 'skipped')
        self.assertIn('PAGO — não cobrar', rows[1]['body'])
        self.assertNotIn('A RECEBER', rows[1]['body'])

    def test_paid_and_partial_receipts_do_not_request_full_payment_again(self):
        with db(self.request) as conn:
            order = core.order_dict(conn.execute('SELECT * FROM orders WHERE id=?', (self.order_id,)).fetchone())
        order['maps_url'] = operations._maps_link(order)
        text = driver_dispatch.receipt(order, self.driver, 400)
        self.assertIn('Já recebido: R$ 4,00', text)
        self.assertIn('Valor restante dos produtos: R$ 8,00', text)
        self.assertNotIn('A RECEBER pelos produtos: R$ 12,00', text)
        order['payment_status'] = 'paid'
        text = driver_dispatch.receipt(order, self.driver, 0)
        self.assertIn('PAGO — não cobrar os produtos novamente.', text)
        self.assertNotIn('A RECEBER', text)

    def test_long_receipt_is_complete_and_parts_wait_for_previous_send(self):
        items = [{'id': str(index), 'name': f'Sabor {index} ' + ('x' * 100), 'quantity': 1, 'price_cents': 400} for index in range(100)]
        with db(self.request) as conn:
            conn.execute('UPDATE orders SET items_json=? WHERE id=?', (json.dumps(items), self.order_id))
        self.assign()
        rows = self.rows()
        self.assertGreater(len(rows), 1)
        text = '\n'.join(row['body'] for row in rows)
        self.assertIn('Sabor 0 ', text)
        self.assertIn('Sabor 99 ', text)
        self.assertIn('Total dos produtos: R$ 12,00', text)
        self.assertTrue(all(len(row['body']) <= 4096 for row in rows))
        self.assertEqual(rows[0]['after_event'], '')
        self.assertEqual(rows[1]['after_event'], rows[0]['event_key'])
        with db(self.request) as conn:
            conn.execute("UPDATE whatsapp_outbox SET status='sending' WHERE id=?", (rows[0]['id'],))
            self.assertIsNone(whatsapp.next_outbox(conn, whatsapp.config()))
            conn.execute("UPDATE whatsapp_outbox SET status='accepted' WHERE id=?", (rows[0]['id'],))
            self.assertEqual(whatsapp.next_outbox(conn, whatsapp.config())['id'], rows[1]['id'])
        self.open_connection()
        for row in rows[1:]:
            with patch.object(whatsapp, 'send', return_value='evolution:' + row['id']):
                self.assertTrue(whatsapp.process_one(self.app))
        self.assertTrue(all(row['status'] == 'accepted' for row in self.rows()))

    def test_uncertain_send_is_not_automatically_repeated(self):
        self.assign()
        self.open_connection()
        with patch.object(whatsapp, 'send', side_effect=URLError('simulated')) as send:
            self.assertTrue(whatsapp.process_one(self.app))
            self.assertFalse(whatsapp.process_one(self.app))
            self.assertEqual(send.call_count, 1)
        self.assertEqual(self.assign().json()['dispatch']['status'], 'uncertain')
        self.assertEqual(len(self.rows()), 1)

    def test_stale_receipt_expires(self):
        self.assign()
        with db(self.request) as conn:
            old = (datetime.now(timezone.utc) - timedelta(hours=25)).isoformat()
            conn.execute('UPDATE whatsapp_outbox SET created_at=?', (old,))
            row = conn.execute('SELECT * FROM whatsapp_outbox').fetchone()
            self.assertEqual(whatsapp.payload(conn, row, whatsapp.config())[1], 'skipped')

    def test_admin_auth_and_csrf_are_required(self):
        url = f'/api/admin/orders/{self.order_id}/driver'
        with TestClient(self.app) as visitor:
            self.assertEqual(visitor.patch(url, json={'driver_id': self.driver['id']}).status_code, 401)
        self.assertEqual(self.client.patch(url, json={'driver_id': self.driver['id']}).status_code, 403)
        self.assertEqual(self.rows(), [])

    def test_ifood_test_and_closed_orders_still_reject_assignment(self):
        with db(self.request) as conn:
            conn.execute("UPDATE orders SET source='ifood' WHERE id=?", (self.order_id,))
        self.assertEqual(self.assign().status_code, 409)
        for status in ['delivered', 'cancelled']:
            with db(self.request) as conn:
                conn.execute("UPDATE orders SET source='web',status=? WHERE id=?", (status, self.order_id))
            self.assertEqual(self.assign().status_code, 409)
        self.assertEqual(self.rows(), [])

    def test_migration_preserves_existing_assignment_and_is_repeatable(self):
        with db(self.request) as conn:
            conn.execute('UPDATE orders SET courier_id=? WHERE id=?', (self.driver['id'], self.order_id))
            conn.execute('ALTER TABLE orders DROP COLUMN courier_assignment_key')
            operations.initialize(conn)
            operations.initialize(conn)
            order = conn.execute('SELECT * FROM orders WHERE id=?', (self.order_id,)).fetchone()
            self.assertEqual(order['courier_id'], self.driver['id'])
            self.assertEqual(order['courier_assignment_key'], '')
        self.assign()
        self.assign()
        self.assertEqual(len(self.rows()), 1)


if __name__ == '__main__':
    unittest.main()
