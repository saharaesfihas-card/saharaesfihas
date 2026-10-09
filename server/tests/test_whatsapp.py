"""Contratos do WhatsApp sem chamar a Meta nem enviar mensagens reais."""
from contextlib import contextmanager
import hashlib
import hmac
import json
import os
from pathlib import Path
import sqlite3
from tempfile import TemporaryDirectory
import time
from types import SimpleNamespace
import unittest
from unittest.mock import patch
from urllib.error import HTTPError, URLError
from uuid import uuid4

from fastapi.testclient import TestClient

from server import whatsapp
from server.core import db, hash_password
from server.main import create_app


class WhatsAppTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.password = 'only-for-tests'
        cls.password_hash = hash_password(cls.password)

    def setUp(self):
        self.env = patch.dict(os.environ, {
            'SAHARA_WHATSAPP_PROVIDER': 'meta',
            'SAHARA_WHATSAPP_ENABLED': '1', 'SAHARA_WHATSAPP_ACCESS_TOKEN': 'test-token-not-real',
            'SAHARA_WHATSAPP_PHONE_NUMBER_ID': '12345', 'SAHARA_WHATSAPP_APP_SECRET': 'test-secret',
            'SAHARA_WHATSAPP_VERIFY_TOKEN': 'test-verify', 'SAHARA_WHATSAPP_ORDER_TEMPLATE': '',
            'SAHARA_WHATSAPP_API_VERSION': 'v25.0',
        })
        self.env.start()
        self.directory = TemporaryDirectory()
        self.app = create_app(data_dir=Path(self.directory.name) / 'private',
                              admin_password_hash=self.password_hash, secure_cookie=False)
        # Sem entrar no lifespan: controla a fila manualmente, sem worker concorrente.
        self.client = TestClient(self.app)
        self.request = SimpleNamespace(app=self.app)
        login = self.client.post('/api/admin/login', json={'password': self.password})
        self.headers = {'X-Sahara-CSRF': login.json()['csrf_token']}

    def tearDown(self):
        self.client.close()
        self.directory.cleanup()
        self.env.stop()

    def message(self, text='Olá', identifier=None, recipient='5544999999999', timestamp=None, number='12345'):
        return {'object': 'whatsapp_business_account', 'entry': [{'changes': [{'field': 'messages',
                'value': {'metadata': {'phone_number_id': number}, 'messages': [{'id': identifier or uuid4().hex,
                          'from': recipient, 'timestamp': str(int(time.time()) if timestamp is None else timestamp),
                          'type': 'text', 'text': {'body': text}}]}}]}]}

    def webhook(self, body, signed=True):
        raw = json.dumps(body).encode()
        signature = 'sha256=' + hmac.new(b'test-secret', raw, hashlib.sha256).hexdigest()
        return self.client.post('/api/whatsapp/webhook', content=raw,
                                headers={'Content-Type': 'application/json', 'X-Hub-Signature-256': signature if signed else 'bad'})

    def outbox(self):
        with db(self.request) as conn:
            return [dict(row) for row in conn.execute('SELECT * FROM whatsapp_outbox ORDER BY created_at')]

    def order(self, consent=True, telephone='44999999999', key=None):
        body = {'items': [{'id': 'carne', 'quantity': 1}],
                'customer': {'name': 'Teste', 'phone': telephone, 'whatsapp_opt_in': consent},
                'delivery': {'street': 'Rua', 'number': '1', 'neighborhood': 'Centro'},
                'payment_method': 'Pix', 'idempotency_key': key or uuid4().hex}
        response = self.client.post('/api/orders', json=body)
        self.assertEqual(response.status_code, 201, response.text)
        return response.json()

    def test_configuration_and_private_endpoints_do_not_expose_credentials(self):
        with TestClient(self.app) as anonymous:
            self.assertEqual(anonymous.get('/api/admin/whatsapp').status_code, 401)
        response = self.client.get('/api/admin/whatsapp')
        self.assertEqual(response.status_code, 200)
        self.assertNotIn('test-token-not-real', response.text)
        self.assertNotIn('test-secret', response.text)
        self.assertEqual(self.client.post('/api/admin/whatsapp/reply', json={}).status_code, 403)
        with patch.dict(os.environ, {'SAHARA_WHATSAPP_ENABLED': '0'}):
            self.assertFalse(self.client.get('/api/integrations').json()['integrations'][0]['available'])
            self.assertEqual(self.webhook(self.message()).status_code, 503)

    def test_verification_signature_number_and_duplicate_events(self):
        endpoint = '/api/whatsapp/webhook'
        args = {'hub.mode': 'subscribe', 'hub.verify_token': 'wrong', 'hub.challenge': '001234'}
        self.assertEqual(self.client.get(endpoint, params=args).status_code, 403)
        args['hub.verify_token'] = 'test-verify'
        response = self.client.get(endpoint, params=args)
        self.assertEqual(response.text, '001234')
        self.assertEqual(response.headers['content-type'], 'text/plain; charset=utf-8')
        event = self.message()
        self.assertEqual(self.webhook(event, signed=False).status_code, 403)
        self.webhook(self.message(number='other'))
        self.assertEqual(self.outbox(), [])
        self.assertEqual(self.webhook(event).status_code, 200)
        self.assertEqual(self.webhook(event).status_code, 200)
        self.assertEqual(len(self.outbox()), 1)
        self.assertIn('atendimento automático', self.outbox()[0]['body'])
        self.assertEqual(self.webhook({'object': 'whatsapp_business_account', 'entry': [None]}).status_code, 400)

    def test_old_events_cannot_reopen_window_or_trigger_response(self):
        self.webhook(self.message(timestamp=int(time.time()) - 90000))
        self.assertEqual(self.outbox(), [])
        with db(self.request) as conn:
            whatsapp.queue(conn, 'test', '5544999999999', 'reply', 'Texto')
        with patch.object(whatsapp, 'send') as send:
            whatsapp.process_one(self.app)
            send.assert_not_called()
        self.assertEqual(self.outbox()[0]['status'], 'blocked')

    def test_bot_answers_from_real_order_and_never_another_phone(self):
        own = self.order(consent=False)
        other = self.order(consent=False, telephone='11988888888')
        self.webhook(self.message('Consultar pedido ' + own['id']))
        self.assertIn(own['id'], self.outbox()[-1]['body'])
        self.assertIn('em preparo', self.outbox()[-1]['body'])
        self.webhook(self.message('Consultar pedido ' + other['id']))
        self.assertNotIn(other['id'], self.outbox()[-1]['body'])
        self.assertIn('Não encontrei', self.outbox()[-1]['body'])
        self.webhook(self.message('cardápio'))
        self.assertIn('/index.html', self.outbox()[-1]['body'])

    def test_handoff_pauses_bot_and_manual_reply_is_idempotent(self):
        self.webhook(self.message('atendente'))
        self.webhook(self.message('Quero alterar o endereço'))
        self.assertEqual(len(self.outbox()), 1)
        body = {'phone': '5544999999999', 'message': 'Olá, aqui é a equipe!', 'idempotency_key': uuid4().hex}
        for _ in range(2):
            self.assertEqual(self.client.post('/api/admin/whatsapp/reply', json=body, headers=self.headers).status_code, 202)
        self.assertEqual(len(self.outbox()), 2)
        self.assertEqual(self.client.post('/api/admin/whatsapp/reply', json=body | {'message': 'Outro texto'}, headers=self.headers).status_code, 409)
        self.webhook(self.message('MENU'))
        self.assertEqual(len(self.outbox()), 3)
        self.webhook(self.message('horário'))
        self.assertIn('18h às 23h', self.outbox()[-1]['body'])
        with db(self.request) as conn:
            conn.execute('UPDATE whatsapp_contacts SET last_inbound=?', (int(time.time()) - 90000,))
        self.assertEqual(self.client.post('/api/admin/whatsapp/reply', json=body | {'idempotency_key': uuid4().hex}, headers=self.headers).status_code, 409)

    def test_order_consent_status_events_and_replays_are_atomic(self):
        self.order(consent=False)
        self.assertEqual(self.outbox(), [])
        key = uuid4().hex
        order = self.order(key=key)
        self.order(key=key)
        self.assertEqual(len(self.outbox()), 1)
        for _ in range(2):
            response = self.client.patch('/api/admin/orders/' + order['id'], json={'status': 'ready'}, headers=self.headers)
            self.assertEqual(response.status_code, 200)
        self.assertEqual(len(self.outbox()), 2)
        self.webhook(self.message('PARAR'))
        with db(self.request) as conn:
            contact = conn.execute('SELECT * FROM whatsapp_contacts').fetchone()
            self.assertTrue(contact['opted_out'])
        self.client.patch('/api/admin/orders/' + order['id'], json={'status': 'out_for_delivery'}, headers=self.headers)
        self.assertEqual(len([row for row in self.outbox() if row['kind'] == 'order']), 2)
        self.assertEqual(self.outbox()[0]['status'], 'skipped')
        self.webhook(self.message('ATIVAR AVISOS'))
        self.client.patch('/api/admin/orders/' + order['id'], json={'status': 'delivered'}, headers=self.headers)
        self.assertEqual(len([row for row in self.outbox() if row['kind'] == 'order']), 3)

    def test_template_outside_window_and_accepted_is_not_delivered(self):
        self.order()
        with patch.object(whatsapp, 'send') as send:
            whatsapp.process_one(self.app)
            send.assert_not_called()
        row = self.outbox()[0]
        self.assertEqual(row['status'], 'blocked')
        with patch.dict(os.environ, {'SAHARA_WHATSAPP_ORDER_TEMPLATE': 'sahara_pedido_status'}):
            self.assertEqual(self.client.post('/api/admin/whatsapp/messages/' + row['id'] + '/retry', headers=self.headers).status_code, 200)
            with patch.object(whatsapp, 'send', return_value='wamid.test') as send:
                whatsapp.process_one(self.app)
            data = send.call_args.args[1]
            self.assertEqual(data['type'], 'template')
            self.assertEqual(len(data['template']['components'][0]['parameters']), 2)
        self.assertEqual(self.outbox()[0]['status'], 'accepted')
        receipt = {'object': 'whatsapp_business_account', 'entry': [{'changes': [{'field': 'messages', 'value': {
            'metadata': {'phone_number_id': '12345'}, 'statuses': [{'id': 'wamid.test', 'status': 'read'}]}}]}]}
        self.webhook(receipt)
        receipt['entry'][0]['changes'][0]['value']['statuses'][0]['status'] = 'sent'
        self.webhook(receipt)
        self.assertEqual(self.outbox()[0]['status'], 'read')
        receipt['entry'][0]['changes'][0]['value']['statuses'][0]['status'] = 'failed'
        self.webhook(receipt)
        self.assertEqual(self.outbox()[0]['status'], 'read')

    def test_session_text_transport_failure_and_rate_limit(self):
        self.webhook(self.message('horário'))
        with patch.object(whatsapp, 'send', side_effect=URLError('unconfirmed')):
            whatsapp.process_one(self.app)
        self.assertEqual(self.outbox()[0]['status'], 'uncertain')
        self.assertEqual(self.client.post('/api/admin/whatsapp/messages/' + self.outbox()[0]['id'] + '/retry', headers=self.headers).status_code, 409)
        self.webhook(self.message('pagamento'))
        with patch.object(whatsapp, 'send', side_effect=HTTPError('https://graph.facebook.com', 429, 'limited', {}, None)) as send:
            whatsapp.process_one(self.app)
            self.assertEqual(send.call_args.args[1]['type'], 'text')
        self.assertEqual(self.outbox()[1]['status'], 'pending')
        self.assertGreater(self.outbox()[1]['next_attempt'], int(time.time()))
        with patch.object(whatsapp, 'send') as send:
            self.assertFalse(whatsapp.process_one(self.app))
            send.assert_not_called()

    def test_schema_migration_preserves_old_orders_and_disables_notices(self):
        order = self.order(consent=False)
        with db(self.request) as conn:
            conn.execute('ALTER TABLE orders DROP COLUMN whatsapp_opt_in')
        create_app(data_dir=Path(self.directory.name) / 'private', admin_password_hash=self.password_hash)
        with db(self.request) as conn:
            row = conn.execute('SELECT * FROM orders WHERE id=?', (order['id'],)).fetchone()
            self.assertEqual(row['total_cents'], order['total_cents'])
            self.assertEqual(row['whatsapp_opt_in'], 0)


if __name__ == '__main__':
    unittest.main()
