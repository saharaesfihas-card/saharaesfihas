"""Evolution API v2: QR, contrato HTTP, eventos e identidade, sem WhatsApp real."""
import io
import json
import os
from pathlib import Path
from tempfile import TemporaryDirectory
import time
from types import SimpleNamespace
import unittest
from unittest.mock import patch
from urllib.error import HTTPError
from urllib.request import Request
from uuid import uuid4

from fastapi.testclient import TestClient

from server import evolution, whatsapp
from server.core import db, hash_password
from server.main import create_app


class EvolutionTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.password_hash = hash_password('test-only-password')

    def setUp(self):
        self.environment = patch.dict(os.environ, {
            'SAHARA_WHATSAPP_PROVIDER': 'evolution', 'SAHARA_WHATSAPP_ENABLED': '1',
            'SAHARA_EVOLUTION_URL': 'https://evolution.example', 'SAHARA_EVOLUTION_API_KEY': 'private-test-key',
            'SAHARA_EVOLUTION_INSTANCE': 'sahara', 'SAHARA_EVOLUTION_WEBHOOK_SECRET': 'test-webhook-secret',
        })
        self.environment.start()
        self.status = patch.object(evolution, 'connection', return_value='close')
        self.connection_mock = self.status.start()
        self.directory = TemporaryDirectory()
        self.app = create_app(data_dir=Path(self.directory.name) / 'data', admin_password_hash=self.password_hash, secure_cookie=False)
        self.request = SimpleNamespace(app=self.app)
        self.client = TestClient(self.app)
        login = self.client.post('/api/admin/login', json={'password': 'test-only-password'})
        self.headers = {'X-Sahara-CSRF': login.json()['csrf_token']}

    def tearDown(self):
        self.client.close()
        self.directory.cleanup()
        self.status.stop()
        self.environment.stop()

    def event(self, kind, data, instance='sahara', secret='test-webhook-secret'):
        return self.client.post('/api/whatsapp/evolution/webhook', json={'event': kind, 'instance': instance, 'data': data},
                                headers={'X-Sahara-Webhook-Secret': secret})

    def message(self, text='Olá', **kwargs):
        return {'key': {'remoteJid': '5544999999999@s.whatsapp.net', 'id': uuid4().hex, 'fromMe': False},
                'messageTimestamp': int(time.time()), 'message': {'conversation': text}} | kwargs

    def rows(self):
        with db(self.request) as conn:
            return [dict(row) for row in conn.execute('SELECT * FROM whatsapp_outbox ORDER BY created_at')]

    def order(self):
        body = {'items': [{'id': 'carne', 'quantity': 1}], 'customer': {'phone': '44999999999', 'whatsapp_opt_in': True},
                'delivery': {'street': 'Rua', 'number': '1', 'neighborhood': 'Centro'}, 'idempotency_key': uuid4().hex}
        response = self.client.post('/api/orders', json=body)
        self.assertEqual(response.status_code, 201)
        return response.json()

    def test_webhook_authentication_instance_own_messages_groups_and_history(self):
        message = self.message()
        self.assertEqual(self.event('messages.upsert', message, secret='wrong').status_code, 403)
        self.assertEqual(self.event('messages.upsert', message, instance='another').status_code, 403)
        self.assertEqual(self.client.post('/api/whatsapp/webhook', json={}).status_code, 503)
        for jid, sender in [('123456789@g.us', False), ('status@broadcast', False), ('1234567890123@lid', False), ('5544999999999@s.whatsapp.net', True)]:
            self.event('messages.upsert', self.message(key={'id': uuid4().hex, 'remoteJid': jid, 'fromMe': sender}))
        self.event('messages.upsert', self.message(messageTimestamp=int(time.time()) - 90000))
        self.assertEqual(self.rows(), [])
        self.assertEqual(self.event('MESSAGES_UPSERT', message).status_code, 200)
        self.event('messages.upsert', message)
        self.assertEqual(len(self.rows()), 1)
        self.assertEqual(self.rows()[0]['provider'], 'evolution')

    def test_lid_uses_verified_phone_alternative_and_status_checks_ownership(self):
        own = self.order()
        key = {'id': uuid4().hex, 'fromMe': False, 'remoteJid': '999999999999@lid', 'remoteJidAlt': '5544999999999@s.whatsapp.net'}
        self.event('messages.upsert', self.message('Pedido ' + own['id'], key=key))
        self.assertIn(own['id'], self.rows()[-1]['body'])
        key = key | {'id': uuid4().hex, 'remoteJidAlt': '5511999999999@s.whatsapp.net'}
        self.event('messages.upsert', self.message('Pedido ' + own['id'], key=key))
        self.assertNotIn(own['id'], self.rows()[-1]['body'])

    def test_disconnected_queue_waits_and_order_does_not_require_meta_template(self):
        self.order()
        with patch.object(whatsapp, 'send') as transport:
            self.assertFalse(whatsapp.process_one(self.app))
            transport.assert_not_called()
        self.connection_mock.return_value = 'open'
        self.event('connection.update', {'state': 'open'})
        with patch.object(evolution, 'api', return_value={'key': {'id': 'test-outbound'}}) as api:
            self.assertTrue(whatsapp.process_one(self.app))
        self.assertEqual(api.call_args.args[1], '/message/sendText/sahara')
        self.assertEqual(api.call_args.args[2]['number'], '5544999999999')
        self.assertIn('em preparo', api.call_args.args[2]['text'])
        self.assertEqual(self.rows()[0]['status'], 'accepted')
        self.assertEqual(self.rows()[0]['provider_id'], 'evolution:test-outbound')

    def test_delivery_events_use_key_id_and_never_regress_or_claim_own_inbound_delivered(self):
        self.order()
        self.connection_mock.return_value = 'open'
        self.event('connection.update', {'state': 'open'})
        with patch.object(evolution, 'api', return_value={'key': {'id': 'test-outbound'}}):
            whatsapp.process_one(self.app)
        self.event('messages.update', {'keyId': 'test-outbound', 'fromMe': False, 'status': 'READ'})
        self.assertEqual(self.rows()[0]['status'], 'accepted')
        self.event('messages.update', [{'keyId': 'test-outbound', 'fromMe': True, 'status': 'READ'}])
        self.event('messages.update', {'keyId': 'test-outbound', 'fromMe': True, 'status': 'SERVER_ACK'})
        self.assertEqual(self.rows()[0]['status'], 'read')

    def test_qr_requires_admin_csrf_and_sets_webhook_secret_without_exposing_key(self):
        endpoint = '/api/admin/whatsapp/connect'
        self.client.cookies.clear()
        self.assertEqual(self.client.post(endpoint, json={}).status_code, 401)
        login = self.client.post('/api/admin/login', json={'password': 'test-only-password'})
        self.headers = {'X-Sahara-CSRF': login.json()['csrf_token']}
        self.assertEqual(self.client.post(endpoint, json={}).status_code, 403)
        image = 'data:image/png;base64,iVBORw0KGgo='
        def transport(cfg, path, data=None):
            if path == '/webhook/set/sahara':
                self.assertEqual(data['webhook']['headers']['X-Sahara-Webhook-Secret'], 'test-webhook-secret')
                self.assertFalse(data['webhook']['byEvents'])
                self.assertEqual(data['webhook']['url'], 'https://sahara-esfihas-pdv.onrender.com/api/whatsapp/evolution/webhook')
                return {'enabled': True}
            if path == '/instance/connect/sahara':
                return {'base64': image, 'code': 'private-code', 'apikey': 'private-test-key'}
            self.fail('Endpoint inesperado: ' + path)
        with patch.object(evolution, 'api', side_effect=transport):
            response = self.client.post(endpoint, json={}, headers=self.headers)
        self.assertEqual(response.status_code, 200, response.text)
        self.assertEqual(response.json()['qrcode'], image)
        self.assertNotIn('private-test-key', response.text)
        self.assertNotIn('private-code', response.text)
        self.assertIn('no-store', response.headers['cache-control'])

    def test_create_missing_instance_then_connect_and_reapply_webhook_to_connected_instance(self):
        with patch.object(evolution, 'connection', return_value='not_created'), patch.object(evolution, 'api', return_value={}) as api:
            result = evolution.connect(whatsapp.config())
        self.assertEqual(api.call_args_list[0].args[1], '/instance/create')
        self.assertEqual(api.call_args_list[0].args[2]['integration'], 'WHATSAPP-BAILEYS')
        self.assertEqual(result['state'], 'connecting')
        with patch.object(evolution, 'connection', return_value='open'), patch.object(evolution, 'api', return_value={}) as api:
            result = evolution.connect(whatsapp.config())
        self.assertTrue(result['connected'])
        self.assertEqual(len(api.call_args_list), 1)
        self.assertEqual(api.call_args.args[1], '/webhook/set/sahara')

    def test_http_contract_uses_apikey_and_validates_provider_response(self):
        with patch.object(evolution, '_verify_public_destination'), patch.object(evolution, 'open_url', return_value=io.BytesIO(b'{"key":{"id":"ABC123"}}')) as open_url:
            result = evolution.send(whatsapp.config(), {'number': '5544999999999', 'text': 'Oi'})
        request = open_url.call_args.args[0]
        self.assertEqual(request.full_url, 'https://evolution.example/message/sendText/sahara')
        self.assertEqual(request.get_header('Apikey'), 'private-test-key')
        self.assertEqual(request.get_header('Origin'), 'https://sahara-esfihas-pdv.onrender.com')
        self.assertEqual(request.get_method(), 'POST')
        self.assertEqual(result, 'evolution:ABC123')
        with patch.object(evolution, 'api', return_value={'key': {}}):
            with self.assertRaises(ValueError):
                evolution.send(whatsapp.config(), {})

    def test_configuration_dashboard_and_provider_queue_isolation(self):
        response = self.client.get('/api/admin/whatsapp')
        self.assertEqual(response.json()['provider'], 'evolution')
        self.assertFalse(response.json()['connection']['connected'])
        self.assertNotIn('private-test-key', response.text)
        self.assertNotIn('test-webhook-secret', response.text)
        with patch.dict(os.environ, {'SAHARA_EVOLUTION_URL': 'http://public.example'}):
            self.assertFalse(whatsapp.config()['ready'])
        with db(self.request) as conn, patch.dict(os.environ, {'SAHARA_WHATSAPP_PROVIDER': 'meta'}):
            whatsapp.queue(conn, 'previous-meta', '5544999999999', 'reply', 'Não transferir de conexão')
        self.event('connection.update', {'state': 'open'})
        with patch.object(whatsapp, 'send') as transport:
            self.assertFalse(whatsapp.process_one(self.app))
            transport.assert_not_called()
        row = self.rows()[0]
        self.assertEqual(self.client.post('/api/admin/whatsapp/messages/' + row['id'] + '/retry', headers=self.headers).status_code, 409)

    def test_handoff_manual_reply_after_24h_opt_out_and_non_text(self):
        self.event('messages.upsert', self.message('atendente'))
        self.event('messages.upsert', self.message('Quero alterar meu endereço'))
        self.assertEqual(len(self.rows()), 1)
        with db(self.request) as conn:
            conn.execute('UPDATE whatsapp_contacts SET last_inbound=?', (int(time.time()) - 90000,))
        body = {'phone': '5544999999999', 'message': 'Olá!', 'idempotency_key': uuid4().hex}
        self.assertEqual(self.client.post('/api/admin/whatsapp/reply', json=body, headers=self.headers).status_code, 202)
        self.event('messages.upsert', self.message('PARAR'))
        self.order()
        self.assertFalse(any(row['kind'] == 'order' for row in self.rows()))
        self.event('messages.upsert', self.message(message={'audioMessage': {'mimetype': 'audio/ogg'}}))
        self.assertIn('disponível para a equipe', self.rows()[-1]['body'])

    def test_configuration_rejects_private_addresses_bad_ports_userinfo_and_non_https(self):
        cfg = whatsapp.config()
        for url in ('http://public.example', 'https://localhost', 'https://localhost.localdomain',
                    'https://server.local', 'https://127.0.0.1', 'https://[::1]', 'https://10.0.0.1',
                    'https://169.254.169.254', 'https://192.168.1.1', 'https://public.example:bad',
                    'https://public.example:65536', 'https://public.example:0',
                    'https://user:secret@public.example', 'https://@public.example',
                    'https://public.example?secret=test', 'https://public.example#token',
                    'https://public.example\n'):
            with self.subTest(url=url):
                self.assertFalse(evolution.valid_config(cfg | {'SAHARA_EVOLUTION_URL': url}))
        self.assertTrue(evolution.valid_config(cfg | {'SAHARA_EVOLUTION_URL': 'https://public.example:8443/api'}))

    def test_dns_resolving_to_private_destination_never_receives_apikey(self):
        address = [(2, 1, 6, '', ('127.0.0.1', 443))]
        with patch.object(evolution.socket, 'getaddrinfo', return_value=address), patch.object(evolution, 'open_url') as transport:
            with self.assertRaises(ValueError):
                evolution.api(whatsapp.config(), '/instance/connectionState/sahara')
            transport.assert_not_called()
        mixed = address + [(2, 1, 6, '', ('8.8.8.8', 443))]
        with patch.object(evolution.socket, 'getaddrinfo', return_value=mixed), patch.object(evolution, 'open_url') as transport:
            with self.assertRaises(ValueError):
                evolution.api(whatsapp.config(), '/instance/connectionState/sahara')
            transport.assert_not_called()

    def test_redirect_handler_never_replays_credentials(self):
        request = Request('https://evolution.example/instance/connectionState/sahara', headers={'apikey': 'private-test-key'})
        for code in (301, 302, 303, 307, 308):
            with self.subTest(code=code):
                self.assertIsNone(evolution.NoRedirect().redirect_request(
                    request, None, code, 'redirect', {}, 'https://other.example/collect'))
        opener = SimpleNamespace(open=lambda *args, **kwargs: io.BytesIO(b'{}'))
        with patch.object(evolution, 'build_opener', return_value=opener) as build:
            evolution.open_url(request)
            self.assertIsInstance(build.call_args.args[0], evolution.NoRedirect)

    def test_open_connection_verifies_instance_owner_and_rejects_wrong_number(self):
        cfg = whatsapp.config()
        self.status.stop()
        responses = [{'instance': {'state': 'open'}}, [{'name': 'sahara', 'ownerJid': '5544991748318@s.whatsapp.net'}]]
        with patch.object(evolution, 'api', side_effect=responses) as api:
            self.assertEqual(evolution.connection(cfg), 'open')
        self.assertEqual(api.call_args.args[1], '/instance/fetchInstances?instanceName=sahara')
        for owner in ('5511999999999@s.whatsapp.net', '5544991748318@lid', None):
            responses = [{'instance': {'state': 'open'}}, [{'name': 'sahara', 'ownerJid': owner}]]
            with patch.object(evolution, 'api', side_effect=responses):
                self.assertEqual(evolution.connection(cfg), 'wrong_number')
        with patch.object(evolution, 'api', side_effect=[{'instance': {'state': 'open'}},
                                                       [{'name': 'other-instance', 'ownerJid': '5544991748318@s.whatsapp.net'}]]):
            self.assertEqual(evolution.connection(cfg), 'wrong_number')
        self.status.start()

    def test_open_webhook_alone_cannot_unlock_another_number_and_errors_hide_secrets(self):
        self.order()
        self.connection_mock.return_value = 'wrong_number'
        self.assertEqual(self.event('connection.update', {'state': 'open'}).status_code, 200)
        with patch.object(whatsapp, 'send') as transport:
            self.assertFalse(whatsapp.process_one(self.app))
            transport.assert_not_called()
        response = self.client.get('/api/admin/whatsapp')
        self.assertEqual(response.json()['connection']['state'], 'wrong_number')
        self.assertFalse(response.json()['connection']['connected'])
        self.assertIn('outro número', response.json()['connection']['error'])
        self.connection_mock.side_effect = TypeError('private-test-key test-webhook-secret')
        response = self.client.get('/api/admin/whatsapp')
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json()['connection']['state'], 'unavailable')
        self.assertNotIn('private-test-key', response.text)
        self.assertNotIn('test-webhook-secret', response.text)

    def test_connection_cache_is_bound_to_current_configuration_and_expires(self):
        self.order()
        self.connection_mock.return_value = 'open'
        self.event('connection.update', {'state': 'open'})
        for key, changed in {'SAHARA_EVOLUTION_URL': 'https://another.example',
                             'SAHARA_EVOLUTION_INSTANCE': 'another',
                             'SAHARA_EVOLUTION_API_KEY': 'rotated-private-key',
                             'SAHARA_EVOLUTION_WEBHOOK_SECRET': 'rotated-webhook-secret',
                             'SAHARA_EVOLUTION_EXPECTED_NUMBER': '5511999999999'}.items():
            with self.subTest(key=key), patch.dict(os.environ, {key: changed}), patch.object(evolution, 'connection', return_value='close') as check, patch.object(whatsapp, 'send') as transport:
                self.assertFalse(whatsapp.process_one(self.app))
                check.assert_called_once()
                transport.assert_not_called()
        self.connection_mock.return_value = 'open'
        self.event('connection.update', {'state': 'open'})
        with db(self.request) as conn:
            conn.execute("UPDATE whatsapp_settings SET value='2000-01-01T00:00:00+00:00' WHERE key='evolution_connection_at'")
        with patch.object(evolution, 'connection', return_value='close') as check, patch.object(whatsapp, 'send') as transport:
            self.assertFalse(whatsapp.process_one(self.app))
            check.assert_called_once()
            transport.assert_not_called()
        with patch.dict(os.environ, {'SAHARA_WHATSAPP_ENABLED': '0'}), patch.object(whatsapp, 'send') as transport:
            self.assertFalse(whatsapp.process_one(self.app))
            transport.assert_not_called()

    def test_reconnected_queue_skips_outdated_order_stages(self):
        order = self.order()
        for state in ('ready', 'out_for_delivery', 'delivered'):
            response = self.client.patch('/api/admin/orders/' + order['id'], json={'status': state}, headers=self.headers)
            self.assertEqual(response.status_code, 200)
        self.connection_mock.return_value = 'open'
        with patch.object(whatsapp, 'send', return_value='evolution:latest-stage') as transport:
            for _ in range(4):
                self.assertTrue(whatsapp.process_one(self.app))
            self.assertEqual(transport.call_count, 1)
            self.assertIn('entregue', transport.call_args.args[1]['text'])
        self.assertEqual([row['status'] for row in self.rows()], ['skipped', 'skipped', 'skipped', 'accepted'])
        self.assertTrue(all('Etapa superada' in row['error'] for row in self.rows()[:3]))

    def test_malformed_provider_data_is_generic_and_invalid_qr_never_reaches_browser(self):
        cfg = whatsapp.config()
        for result in (None, [], {'instance': []}, {'instance': {'state': 'unknown'}}):
            self.status.stop()
            with self.subTest(result=result), patch.object(evolution, 'api', return_value=result):
                with self.assertRaises(ValueError):
                    evolution.connection(cfg)
            self.status.start()
        for result in (None, [], {'key': []}, {'key': {'id': None}}):
            with self.subTest(result=result), patch.object(evolution, 'api', return_value=result):
                with self.assertRaises(ValueError):
                    evolution.send(cfg, {})
        for result in ({'base64': 'data:image/png;base64,aGVsbG8='}, {'qrcode': 'private-test-key'}):
            with patch.object(evolution, 'api', side_effect=[{}, result]):
                response = self.client.post('/api/admin/whatsapp/connect', json={}, headers=self.headers)
            self.assertEqual(response.status_code, 502)
            self.assertNotIn('private-test-key', response.text)
        for data in (None, {'key': []}, self.message(message=[])):
            response = self.event('messages.upsert', data)
            self.assertEqual(response.status_code, 400)

    def test_mobile_owner_accepts_known_ninth_digit_alias_only(self):
        cfg = whatsapp.config()
        self.status.stop()
        try:
            for number, expected in (('5544991748318', 'open'), ('554491748318', 'open'),
                                     ('554491748319', 'wrong_number'), ('5544991748319', 'wrong_number')):
                with self.subTest(number=number), patch.object(evolution, 'api', side_effect=[
                    {'instance': {'state': 'open'}}, [{'name': 'sahara', 'ownerJid': number + '@s.whatsapp.net'}]
                ]):
                    self.assertEqual(evolution.connection(cfg), expected)
        finally:
            self.connection_mock = self.status.start()
        self.assertEqual(evolution.phone_aliases('554491748318'), ('5544991748318', '554491748318'))
        for number in ('554433334444', '554422224444', '554455554444', '5544933334444', '521999999999', '5499999999999', '14155552671'):
            with self.subTest(number=number):
                self.assertEqual(evolution.phone_aliases(number), (number,))

    def test_short_mobile_jid_can_consult_and_stop_canonical_orders(self):
        order = self.order()
        short_key = {'id': uuid4().hex, 'fromMe': False, 'remoteJid': '554499999999@s.whatsapp.net'}
        self.assertEqual(self.event('messages.upsert', self.message('Pedido ' + order['id'], key=short_key)).status_code, 200)
        self.assertIn(order['id'], self.rows()[-1]['body'])
        self.assertEqual(self.rows()[-1]['phone'], '5544999999999')
        self.event('messages.upsert', self.message('PARAR', key=short_key | {'id': uuid4().hex}))
        previous_count = len([row for row in self.rows() if row['kind'] == 'order'])
        self.order()
        self.assertEqual(len([row for row in self.rows() if row['kind'] == 'order']), previous_count)
        self.assertEqual(self.rows()[0]['status'], 'skipped')
        self.connection_mock.return_value = 'open'
        with patch.object(whatsapp, 'send', side_effect=lambda cfg, data: 'evolution:' + uuid4().hex) as transport:
            while whatsapp.process_one(self.app):
                pass
            self.assertFalse(any(call.args[1]['text'].startswith('Sahara: seu pedido') for call in transport.call_args_list))

    def test_legacy_short_contact_opt_out_survives_new_canonical_inbound_and_explicit_activation(self):
        with db(self.request) as conn:
            conn.execute('INSERT INTO whatsapp_contacts VALUES(?,?,?,?)', ('554499999999', int(time.time()) - 20, 0, 1))
        self.event('messages.upsert', self.message('Olá'))
        order = self.order()
        self.assertFalse(any(row['kind'] == 'order' for row in self.rows()))
        with db(self.request) as conn:
            conn.execute('UPDATE orders SET whatsapp_opt_in=0 WHERE id=?', (order['id'],))
        self.event('messages.upsert', self.message('ATIVAR AVISOS'))
        with db(self.request) as conn:
            contacts = conn.execute('SELECT phone,opted_out FROM whatsapp_contacts ORDER BY phone').fetchall()
            self.assertEqual(len(contacts), 2)
            self.assertTrue(all(row['opted_out'] == 0 for row in contacts))
            self.assertEqual(conn.execute('SELECT whatsapp_opt_in FROM orders WHERE id=?', (order['id'],)).fetchone()[0], 1)
        self.client.patch('/api/admin/orders/' + order['id'], json={'status': 'ready'}, headers=self.headers)
        self.assertTrue(any(row['kind'] == 'order' and row['order_status'] == 'ready' for row in self.rows()))
        dashboard = self.client.get('/api/admin/whatsapp').json()
        self.assertEqual(len(dashboard['conversations']), 1)
        self.assertEqual(dashboard['conversations'][0]['phone'], '5544999999999')

    def test_legacy_handoff_and_manual_reply_use_aliases_without_losing_human_state(self):
        future = int(time.time()) + 90000
        with db(self.request) as conn:
            conn.execute('INSERT INTO whatsapp_contacts VALUES(?,?,?,?)', ('554499999999', int(time.time()), future, 0))
        self.event('messages.upsert', self.message('horário'))
        self.assertEqual(self.rows(), [])
        dashboard = self.client.get('/api/admin/whatsapp').json()
        self.assertEqual(dashboard['conversations'][0]['human_until'], future)
        body = {'phone': '5544999999999', 'message': 'Olá, equipe!', 'idempotency_key': uuid4().hex}
        self.assertEqual(self.client.post('/api/admin/whatsapp/reply', json=body, headers=self.headers).status_code, 202)
        body['phone'] = '554499999999'
        replay = self.client.post('/api/admin/whatsapp/reply', json=body, headers=self.headers)
        self.assertEqual(replay.status_code, 202)
        self.assertTrue(replay.json()['replayed'])
        with db(self.request) as conn:
            self.assertTrue(all(row[0] == future for row in conn.execute('SELECT human_until FROM whatsapp_contacts')))
        self.assertEqual(self.client.post('/api/admin/whatsapp/conversations/554499999999/resume', headers=self.headers).status_code, 200)
        with db(self.request) as conn:
            self.assertTrue(all(row[0] == 0 for row in conn.execute('SELECT human_until FROM whatsapp_contacts')))
        self.event('messages.upsert', self.message('horário'))
        self.assertIn('18h às 23h', self.rows()[-1]['body'])

    def test_landline_and_foreign_numbers_do_not_gain_mobile_alias_order_access(self):
        for stored, other in (('554433334444', '5544933334444'), ('521999999999', '5219999999999'),
                              ('549999999999', '5499999999999'), ('14155552671', '141955552671')):
            order = self.order()
            with db(self.request) as conn:
                conn.execute('UPDATE orders SET customer_phone=?,whatsapp_opt_in=0 WHERE id=?', (stored, order['id']))
            key = {'id': uuid4().hex, 'fromMe': False, 'remoteJid': other + '@s.whatsapp.net'}
            self.event('messages.upsert', self.message('Pedido ' + order['id'], key=key))
            self.assertNotIn(order['id'], self.rows()[-1]['body'])
            self.assertIn('Não encontrei', self.rows()[-1]['body'])
            key = {'id': uuid4().hex, 'fromMe': False, 'remoteJid': stored + '@s.whatsapp.net'}
            self.event('messages.upsert', self.message('Pedido ' + order['id'], key=key))
            self.assertIn(order['id'], self.rows()[-1]['body'])

    def test_dashboard_missing_instance_is_ready_for_creation_not_service_failure(self):
        for code, expected in ((404, 'not_created'), (401, 'unavailable'), (500, 'unavailable')):
            with self.subTest(code=code):
                self.connection_mock.side_effect = HTTPError('https://evolution.example', code, 'private-test-key', {}, None)
                response = self.client.get('/api/admin/whatsapp')
                self.assertEqual(response.status_code, 200)
                self.assertEqual(response.json()['connection']['state'], expected)
                self.assertFalse(response.json()['connection']['connected'])
                self.assertEqual(bool(response.json()['connection']['error']), code != 404)
                self.assertNotIn('private-test-key', response.text)


if __name__ == '__main__':
    unittest.main()
