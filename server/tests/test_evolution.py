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
        self.status.start()
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
        image = 'data:image/png;base64,aGVsbG8='
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
        with patch.object(evolution, 'urlopen', return_value=io.BytesIO(b'{"key":{"id":"ABC123"}}')) as open_url:
            result = evolution.send(whatsapp.config(), {'number': '5544999999999', 'text': 'Oi'})
        request = open_url.call_args.args[0]
        self.assertEqual(request.full_url, 'https://evolution.example/message/sendText/sahara')
        self.assertEqual(request.get_header('Apikey'), 'private-test-key')
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


if __name__ == '__main__':
    unittest.main()
