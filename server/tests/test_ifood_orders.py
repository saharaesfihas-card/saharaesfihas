"""Importação com contratos fornecidos pelo proprietário; sem APIs/pedidos reais."""
from concurrent.futures import ThreadPoolExecutor
from copy import deepcopy
from io import BytesIO
import json
import os
from pathlib import Path
import sqlite3
from tempfile import TemporaryDirectory
from threading import Event
import time
import unittest
from unittest.mock import patch
from urllib.error import HTTPError

from fastapi.testclient import TestClient

from server import ifood, ifood_orders
from server.core import hash_password
from server.main import create_app

MERCHANT = '22222222-2222-4222-8222-222222222222'
CLIENT = '11111111-1111-4111-8111-111111111111'
ORDER = '33333333-3333-4333-8333-333333333333'
EVENT = '44444444-4444-4444-8444-444444444444'
SECRET = 'fictional-ifood-secret-only'
TOKEN = 'fictional-ifood-access-token-only'


def event(identifier=EVENT, full_code='PLACED'):
    return {'id': identifier, 'orderId': ORDER, 'createdAt': '2026-10-10T01:00:00Z',
            'fullCode': full_code, 'code': 'PLC' if full_code == 'PLACED' else 'OTHER'}


def order():
    return {'id': ORDER, 'displayId': 'TEST-123', 'merchant': {'id': MERCHANT}, 'orderType': 'DELIVERY',
        'createdAt': '2026-10-10T01:00:00Z', 'orderTiming': 'IMMEDIATE',
        'total': {'orderAmount': 29.45, 'subTotal': 26.7, 'benefits': 3.25, 'deliveryFee': 5, 'additionalFees': 1},
        'items': [{'id': '55555555-5555-4555-8555-555555555555', 'name': 'Produto externo',
            'quantity': 1.5, 'unitPrice': 10.2, 'totalPrice': 26.7, 'optionsPrice': 11.4,
            'observations': 'Observação do produto', 'options': [{'name': 'Adicional', 'groupName': 'Recheios',
                'quantity': 2, 'unitPrice': 5.7, 'price': 11.4, 'addition': 11.4,
                'customization': [{'name': 'Sem cebola', 'groupName': 'Preferências', 'quantity': 1,
                                  'unitPrice': 0, 'price': 0, 'addition': 0}]}]}],
        'customer': {'name': 'Cliente fictício', 'phone': {'number': 'telefone-localizador'}},
        'payments': {'pending': 0, 'prepaid': 29.45,
            'methods': [{'method': 'CREDIT', 'type': 'ONLINE', 'prepaid': True,
                         'currency': 'BRL', 'value': 29.45, 'card': {'brand': 'TEST'}}]},
        'delivery': {'deliveredBy': 'IFOOD', 'pickupCode': 'TEST', 'deliveryAddress': {
            'streetName': 'Rua fictícia', 'streetNumber': '123', 'neighborhood': 'Bairro fictício',
            'city': 'Maringá', 'state': 'PR', 'complement': 'Complemento fictício',
            'reference': 'Referência fictícia', 'coordinates': {'latitude': -23.4, 'longitude': -51.9}}},
        'extraInfo': 'Observação externa'}


class IFoodOrderTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.password = 'disposable-test-password'
        cls.password_hash = hash_password(cls.password)

    def setUp(self):
        self.directory = TemporaryDirectory()
        self.env = patch.dict(os.environ, {'SAHARA_IFOOD_CLIENT_ID': CLIENT, 'SAHARA_IFOOD_CLIENT_SECRET': SECRET,
            'SAHARA_IFOOD_MERCHANT_ID': MERCHANT, 'SAHARA_IFOOD_ENABLED': '1', 'SAHARA_IFOOD_ENVIRONMENT': 'test',
            'SAHARA_IFOOD_IMPORT_ENABLED': '0', 'SAHARA_WHATSAPP_ENABLED': '0', 'SAHARA_AI_ENABLED': '0'})
        self.env.start()
        self.app = create_app(data_dir=Path(self.directory.name), admin_password_hash=self.password_hash, secure_cookie=False)
        self.client = TestClient(self.app)
        login = self.client.post('/api/admin/login', json={'password': self.password}).json()
        self.headers = {'X-Sahara-CSRF': login['csrf_token']}
        self.events = [event()]
        self.raw_order = order()
        self.calls = []
        self.ack_error = None
        with patch('server.ifood.evolution.open_url', side_effect=self.transport):
            self.assertEqual(self.client.post('/api/admin/ifood/check', json={}, headers=self.headers).json()['state'], 'verified')
        self.calls.clear()

    def tearDown(self):
        self.client.close()
        self.env.stop()
        self.directory.cleanup()

    def transport(self, request, **kwargs):
        self.calls.append(request)
        url = request.full_url
        if url.endswith('/oauth/token'):
            data = {'accessToken': TOKEN}
        elif '/merchant/v1.0/' in url:
            data = {'id': MERCHANT}
        elif url == ifood_orders.EVENTS + '/events:polling':
            data = self.events
        elif url == ifood.HOST + '/order/v1.0/orders/' + ORDER:
            data = self.raw_order
        elif url == ifood_orders.EVENTS + '/events/acknowledgment':
            self.assertEqual(request.get_method(), 'POST')
            self.assertEqual(request.get_header('Content-type'), 'application/json')
            ids = json.loads(request.data)
            # This assertion exercises transaction ordering at the provider boundary.
            with sqlite3.connect(self.app.state.db_path) as conn:
                self.assertGreater(conn.execute('SELECT COUNT(*) FROM orders WHERE source=\'ifood\'').fetchone()[0], 0)
                for item in ids:
                    self.assertEqual(set(item), {'id'})
                    self.assertEqual(conn.execute('SELECT COUNT(*) FROM ifood_imported_events e '
                        'JOIN ifood_imported_orders m ON m.external_id=e.external_order_id AND m.merchant_id=e.merchant_id '
                        'JOIN orders o ON o.id=m.order_id WHERE e.event_id=?', (item['id'],)).fetchone()[0], 1)
            if self.ack_error:
                raise self.ack_error
            response = BytesIO(b'')
            response.status = 204
            return response
        else:
            self.fail('Unexpected provider URL: ' + url)
        return BytesIO(json.dumps(data).encode())

    def sync(self):
        with patch('server.ifood.evolution.open_url', side_effect=self.transport):
            response = self.client.post('/api/admin/ifood/import', json={}, headers=self.headers)
        self.assertEqual(response.status_code, 200)
        self.assertNotIn(SECRET, response.text)
        self.assertNotIn(TOKEN, response.text)
        return response.json()

    def clear_cooldown(self):
        with sqlite3.connect(self.app.state.db_path) as conn:
            conn.execute('UPDATE ifood_import_state SET started_at=0')

    def imported(self):
        return self.client.get('/api/admin/orders').json()['orders']

    def test_original_values_options_payment_and_delivery_without_local_side_effects(self):
        result = self.sync()
        self.assertEqual((result['state'], result['imported'], result['acknowledged']), ('imported', 1, 1))
        stored = self.imported()[0]
        self.assertEqual((stored['source'], stored['status'], stored['total_cents'], stored['subtotal_cents'], stored['discount_cents']),
                         ('ifood', 'ifood_received', 2945, 2670, 325))
        self.assertEqual(stored['items'][0]['total_cents'], 2670)
        self.assertEqual(stored['items'][0]['quantity'], 1.5)
        self.assertEqual(stored['items'][0]['options'][0]['customization'][0]['name'], 'Sem cebola')
        self.assertEqual(stored['delivery']['reference'], 'Referência fictícia')
        self.assertEqual(stored['delivery']['ifood']['prepaid_cents'], 2945)
        self.assertEqual(stored['payment_status'], 'external_paid')
        self.assertIsNone(stored['customer_id'])
        self.assertEqual(stored['stock_applied'], 0)
        with sqlite3.connect(self.app.state.db_path) as conn:
            for table in ('customers', 'cash_entries', 'inventory_movements', 'whatsapp_outbox', 'order_events'):
                self.assertEqual(conn.execute('SELECT COUNT(*) FROM ' + table).fetchone()[0], 0, table)
        self.assertEqual(self.client.get('/api/admin/dashboard').json()['sales_cents'], 0)
        self.assertEqual([r.full_url for r in self.calls], [ifood.HOST + '/authentication/v1.0/oauth/token',
            ifood_orders.EVENTS + '/events:polling', ifood.HOST + '/order/v1.0/orders/' + ORDER,
            ifood_orders.EVENTS + '/events/acknowledgment'])
        self.assertTrue(all(r.get_header('Authorization') == 'Bearer ' + TOKEN for r in self.calls[1:]))

    def test_duplicate_order_distinct_events_and_replay(self):
        second = '66666666-6666-4666-8666-666666666666'
        self.events = [event(), event(second), event()]
        result = self.sync()
        self.assertEqual((result['imported'], result['replayed'], result['acknowledged']), (1, 2, 2))
        self.assertEqual(len(self.imported()), 1)
        self.assertEqual(sum('/order/v1.0/' in r.full_url for r in self.calls), 1)
        self.clear_cooldown()
        result = self.sync()
        self.assertEqual(result['imported'], 0)
        self.assertEqual(len(self.imported()), 1)

    def test_no_content_poll_is_empty_and_limit_pauses_further_requests(self):
        response = BytesIO(b'')
        response.status = 204
        with patch('server.ifood.evolution.open_url', side_effect=[
                BytesIO(json.dumps({'accessToken': TOKEN}).encode()), response]):
            result = self.client.post('/api/admin/ifood/import', json={}, headers=self.headers).json()
        self.assertEqual(result['state'], 'empty')
        self.clear_cooldown()
        error = HTTPError(ifood_orders.EVENTS, 429, 'Limit', {}, BytesIO(b''))
        with patch('server.ifood.evolution.open_url', side_effect=error):
            self.assertEqual(self.client.post('/api/admin/ifood/import', json={}, headers=self.headers).json()['state'], 'quota')
        with sqlite3.connect(self.app.state.db_path) as conn:
            conn.execute('UPDATE ifood_import_state SET started_at=?', (time.time() - 120,))
        with patch('server.ifood.evolution.open_url') as call:
            self.assertEqual(self.client.post('/api/admin/ifood/import', json={}, headers=self.headers).json()['state'], 'wait')
            call.assert_not_called()

    def test_ack_timeout_persists_order_and_retries_without_duplicate(self):
        self.ack_error = TimeoutError()
        result = self.sync()
        self.assertEqual((result['state'], result['pending_ack']), ('partial', 1))
        self.assertEqual(len(self.imported()), 1)
        self.clear_cooldown()
        self.events = []
        self.ack_error = None
        self.calls.clear()
        result = self.sync()
        self.assertEqual((result['acknowledged'], result['pending_ack'], result['imported']), (1, 0, 0))
        self.assertFalse(any('/order/v1.0/' in r.full_url for r in self.calls))

    def test_details_limit_leaves_remaining_orders_for_next_cycle(self):
        self.events = [{**event(), 'id': f'{index:08d}-4444-4444-8444-444444444444',
                       'orderId': f'{index:08d}-3333-4333-8333-333333333333'} for index in range(1, 5)]
        def transport(request, **kwargs):
            if '/order/v1.0/orders/' in request.full_url:
                self.calls.append(request)
                raw = order()
                raw['id'] = request.full_url.rsplit('/', 1)[-1]
                return BytesIO(json.dumps(raw).encode())
            return self.transport(request, **kwargs)
        with patch('server.ifood.evolution.open_url', side_effect=transport):
            result = self.client.post('/api/admin/ifood/import', json={}, headers=self.headers).json()
        self.assertEqual((result['imported'], result['remaining'], result['acknowledged']), (3, 1, 3))
        self.assertEqual(len(self.imported()), 3)
        self.clear_cooldown()
        with patch('server.ifood.evolution.open_url', side_effect=transport):
            result = self.client.post('/api/admin/ifood/import', json={}, headers=self.headers).json()
        self.assertEqual((result['imported'], result['remaining']), (1, 0))
        self.assertEqual(len(self.imported()), 4)

    def test_accepted_ack_response_clears_pending_event(self):
        def transport(request, **kwargs):
            response = self.transport(request, **kwargs)
            if request.full_url.endswith('/acknowledgment'):
                response.status = 202
            return response
        with patch('server.ifood.evolution.open_url', side_effect=transport):
            result = self.client.post('/api/admin/ifood/import', json={}, headers=self.headers).json()
        self.assertEqual((result['state'], result['pending_ack'], result['acknowledged']), ('imported', 0, 1))
        self.assertEqual(result['ack_http_status'], 202)
        self.assertEqual(len(self.imported()), 1)

    def test_unexpected_ack_response_keeps_pending_event_with_safe_diagnostics(self):
        def transport(request, **kwargs):
            response = self.transport(request, **kwargs)
            if request.full_url.endswith('/acknowledgment'):
                response.status = 201
            return response
        with patch('server.ifood.evolution.open_url', side_effect=transport):
            result = self.client.post('/api/admin/ifood/import', json={}, headers=self.headers).json()
        self.assertEqual((result['state'], result['pending_ack'], result['acknowledged']), ('partial', 1, 0))
        self.assertEqual((result['error_phase'], result['http_status']), ('ack', 201))
        self.assertNotIn(TOKEN, json.dumps(result))
        self.assertNotIn(SECRET, json.dumps(result))

    def test_wrong_merchant_invalid_amounts_and_invalid_quantities_never_ack(self):
        for mutation in ('merchant', 'total', 'negative', 'nan', 'quantity'):
            with self.subTest(mutation=mutation):
                self.raw_order = order()
                if mutation == 'merchant': self.raw_order['merchant']['id'] = CLIENT
                elif mutation == 'total': self.raw_order['total']['orderAmount'] = 0.001
                elif mutation == 'negative': self.raw_order['total']['orderAmount'] = -1
                elif mutation == 'nan': self.raw_order['total']['orderAmount'] = float('nan')
                else: self.raw_order['items'][0]['quantity'] = 0
                self.clear_cooldown()
                self.calls.clear()
                result = self.sync()
                self.assertEqual((result['failed'], result['imported']), (1, 0))
                self.assertFalse(any(r.full_url.endswith('/acknowledgment') for r in self.calls))
                self.assertEqual(self.imported(), [])

    def test_scheduled_order_keeps_provider_window_and_partial_payment(self):
        self.raw_order['orderTiming'] = 'SCHEDULED'
        self.raw_order['schedule'] = {'deliveryDateTimeStart': '2026-10-15T12:00:00Z', 'deliveryDateTimeEnd': '2026-10-15T13:00:00Z'}
        self.raw_order['payments'].update(pending=9.45, prepaid=20)
        self.sync()
        stored = self.imported()[0]
        self.assertEqual(stored['requested_for'], '2026-10-15T12:00:00+00:00')
        self.assertEqual(stored['delivery']['ifood']['schedule_end'], '2026-10-15T13:00:00+00:00')
        self.assertEqual(stored['payment_status'], 'external_pending')
        self.assertEqual(stored['delivery']['ifood']['pending_cents'], 945)

    def test_unsupported_and_malformed_events_remain_unacknowledged(self):
        self.events = [event('66666666-6666-4666-8666-666666666666', 'CANCELLED'), {'id': '../../invalid'}, event()]
        result = self.sync()
        self.assertEqual((result['unsupported'], result['failed'], result['imported']), (1, 1, 1))
        ack = next(r for r in self.calls if r.full_url.endswith('/acknowledgment'))
        self.assertEqual(json.loads(ack.data), [{'id': EVENT}])

    def test_persistent_cooldown_only_one_concurrent_poll(self):
        started, release = Event(), Event()
        def delayed(request, **kwargs):
            if request.full_url.endswith('/events:polling'):
                started.set()
                if not release.wait(5): raise TimeoutError()
            return self.transport(request, **kwargs)
        with patch('server.ifood.evolution.open_url', side_effect=delayed), ThreadPoolExecutor(2) as pool:
            future = pool.submit(self.client.post, '/api/admin/ifood/import', json={}, headers=self.headers)
            self.assertTrue(started.wait(5))
            try:
                self.assertEqual(self.client.post('/api/admin/ifood/import', json={}, headers=self.headers).json()['state'], 'wait')
            finally:
                release.set()
            self.assertEqual(future.result().json()['imported'], 1)
        self.assertEqual(sum(r.full_url.endswith('/events:polling') for r in self.calls), 1)

    def test_invalid_event_response_and_http_failure_are_safe(self):
        error = HTTPError(ifood_orders.EVENTS, 403, SECRET, {}, BytesIO((SECRET + TOKEN).encode()))
        for response, state in [(error, 'permissions'), (BytesIO(b'{}'), 'response'), (BytesIO(b'x' * 1_048_577), 'response')]:
            self.clear_cooldown()
            with patch('server.ifood.evolution.open_url', side_effect=[BytesIO(json.dumps({'accessToken': TOKEN}).encode()), response]):
                result = self.client.post('/api/admin/ifood/import', json={}, headers=self.headers)
            self.assertEqual(result.json()['state'], state)
            self.assertNotIn(SECRET, result.text)
            self.assertNotIn(TOKEN, result.text)
            self.assertEqual(self.imported(), [])

    def test_readonly_configuration_and_production_guard(self):
        with patch('server.ifood.evolution.open_url') as call:
            self.assertTrue(self.client.get('/api/admin/ifood/import').json()['import_available'])
            with patch.dict(os.environ, {'SAHARA_IFOOD_ENVIRONMENT': 'production', 'SAHARA_IFOOD_IMPORT_ENABLED': '1'}):
                self.assertFalse(self.client.get('/api/admin/ifood/import').json()['automatic'])
                self.assertEqual(self.client.post('/api/admin/ifood/import', json={}, headers=self.headers).json()['state'], 'not_ready')
            with patch.dict(os.environ, {'SAHARA_IFOOD_CLIENT_SECRET': SECRET + '-changed'}):
                self.assertFalse(self.client.get('/api/admin/ifood/import').json()['import_available'])
            call.assert_not_called()

    def test_session_csrf_and_origin_required(self):
        with patch('server.ifood.evolution.open_url') as call:
            self.assertEqual(self.client.post('/api/admin/ifood/import', json={}).status_code, 403)
            self.assertEqual(self.client.post('/api/admin/ifood/import', json={}, headers={**self.headers, 'Origin': 'https://untrusted.invalid'}).status_code, 403)
            with TestClient(self.app) as anonymous:
                self.assertEqual(anonymous.get('/api/admin/ifood/import').status_code, 401)
                self.assertEqual(anonymous.post('/api/admin/ifood/import', json={}).status_code, 401)
            call.assert_not_called()

    def test_imported_orders_cannot_become_local_payment_debt_stock_or_delivery(self):
        self.sync()
        identifier = self.imported()[0]['id']
        requests = [('PATCH', '/api/admin/orders/' + identifier, {'status': 'preparing'}),
            ('POST', '/api/admin/orders/' + identifier + '/payment', {'status': 'paid'}),
            ('PATCH', '/api/admin/orders/' + identifier + '/driver', {'driver_id': None}),
            ('POST', '/api/admin/receivables', {'customer_name': 'Cliente fictício', 'amount_cents': 2945, 'order_id': identifier})]
        for method, path, body in requests:
            self.assertEqual(self.client.request(method, path, json=body, headers=self.headers).status_code, 409, path)
        self.assertEqual(self.imported()[0]['status'], 'ifood_received')
        with sqlite3.connect(self.app.state.db_path) as conn:
            for table in ('cash_entries', 'receivables', 'inventory_movements'):
                self.assertEqual(conn.execute('SELECT COUNT(*) FROM ' + table).fetchone()[0], 0)

    def test_background_import_works_without_open_admin_panel(self):
        received = Event()
        def transport(request, **kwargs):
            response = self.transport(request, **kwargs)
            if request.full_url.endswith('/acknowledgment'):
                received.set()
            return response
        with patch.dict(os.environ, {'SAHARA_IFOOD_IMPORT_ENABLED': '1'}), \
                patch('server.ifood.evolution.open_url', side_effect=transport), TestClient(self.app):
            self.assertTrue(received.wait(5))
            end = time.monotonic() + 3
            while time.monotonic() < end:
                if self.client.get('/api/admin/ifood/import').json()['pending_ack'] == 0:
                    break
                time.sleep(0.01)
            self.assertEqual(len(self.imported()), 1)
            self.assertEqual(self.client.get('/api/admin/ifood/import').json()['pending_ack'], 0)
