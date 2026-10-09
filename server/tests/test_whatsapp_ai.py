"""Delivery AI contracts: fake Gemini, private temporary database, no real sends."""
from concurrent.futures import ThreadPoolExecutor
import io
import json
import os
from pathlib import Path
from tempfile import TemporaryDirectory
import threading
import time
from types import SimpleNamespace
import unittest
from unittest.mock import patch
from urllib.error import HTTPError
from uuid import uuid4

from fastapi.testclient import TestClient

from server import evolution, whatsapp, whatsapp_ai as ai
from server.core import db, hash_password
from server.main import create_app


class DeliveryAITests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.password_hash = hash_password('test-only-password')

    def setUp(self):
        self.environment = patch.dict(os.environ, {
            'SAHARA_WHATSAPP_PROVIDER': 'evolution', 'SAHARA_WHATSAPP_ENABLED': '1',
            'SAHARA_EVOLUTION_URL': 'https://evolution.example', 'SAHARA_EVOLUTION_API_KEY': 'evolution-private-test',
            'SAHARA_EVOLUTION_INSTANCE': 'sahara', 'SAHARA_EVOLUTION_WEBHOOK_SECRET': 'webhook-private-test',
            'SAHARA_AI_ENABLED': '1', 'SAHARA_AI_API_KEY': 'gemini-private-test', 'SAHARA_AI_DAILY_LIMIT': '50',
        })
        self.environment.start()
        self.connection = patch.object(evolution, 'connection', return_value='close')
        self.connection.start()
        self.directory = TemporaryDirectory()
        self.app = create_app(data_dir=Path(self.directory.name) / 'data', admin_password_hash=self.password_hash, secure_cookie=False)
        self.request = SimpleNamespace(app=self.app)
        self.client = TestClient(self.app)
        self.phone = '5544999999999'

    def tearDown(self):
        self.client.close()
        self.directory.cleanup()
        self.connection.stop()
        self.environment.stop()

    def inbound(self, text='Que opções salgadas você sugere?', phone=None, identifier=None, timestamp=None):
        key = identifier or uuid4().hex
        message = {'key': {'remoteJid': (phone or self.phone) + '@s.whatsapp.net', 'id': key, 'fromMe': False},
                   'messageTimestamp': timestamp or int(time.time()), 'message': {'conversation': text}}
        response = self.client.post('/api/whatsapp/evolution/webhook',
                                    json={'event': 'messages.upsert', 'instance': 'sahara', 'data': message},
                                    headers={'X-Sahara-Webhook-Secret': 'webhook-private-test'})
        self.assertEqual(response.status_code, 200, response.text)
        return 'evolution:sahara:' + key

    def rows(self, table):
        with db(self.request) as conn:
            return [dict(row) for row in conn.execute('SELECT * FROM ' + table)]

    def response(self, result, finish='STOP'):
        return io.BytesIO(json.dumps({'candidates': [{'finishReason': finish, 'content': {'parts': [{'text': json.dumps(result)}]}}]}).encode())

    def test_free_text_is_queued_without_provider_call_and_duplicates_use_one_job(self):
        identifier = uuid4().hex
        with patch.object(ai, 'interpret') as model:
            self.inbound(identifier=identifier); self.inbound(identifier=identifier)
            model.assert_not_called()
        self.assertEqual(len(self.rows('whatsapp_ai_jobs')), 1)
        self.assertEqual(self.rows('whatsapp_outbox'), [])
        with patch.object(ai, 'interpret', return_value={'intent': 'products', 'product_ids': ['carne']}) as model:
            self.assertTrue(ai.process_one(self.app)); self.assertFalse(ai.process_one(self.app))
            model.assert_called_once()
        self.inbound(identifier=identifier)
        self.assertEqual(len(self.rows('whatsapp_outbox')), 1)
        self.assertEqual(len(self.rows('whatsapp_ai_usage')), 1)

    def test_disabled_or_missing_key_keeps_basic_service_and_never_calls_ai(self):
        for variable, value in [('SAHARA_AI_ENABLED', '0'), ('SAHARA_AI_API_KEY', '')]:
            with patch.dict(os.environ, {variable: value}), patch.object(ai, 'interpret') as model:
                self.inbound('Quero uma esfiha de carne')
                self.assertFalse(ai.process_one(self.app)); model.assert_not_called()
        self.assertEqual(self.rows('whatsapp_ai_jobs'), [])
        self.assertTrue(all('cardápio' in row['body'] for row in self.rows('whatsapp_outbox')))

    def test_commands_remain_local_and_human_pause_prevents_new_jobs(self):
        for text in ['MENU', 'PARAR', 'ATIVAR AVISOS', 'Qual o horário?', 'Como pagar?', 'Status do pedido', '1', 'ATENDENTE', 'Me sugira sabores']:
            self.inbound(text)
        self.assertEqual(self.rows('whatsapp_ai_jobs'), [])
        self.assertEqual(len(self.rows('whatsapp_outbox')), 8)

    def test_new_order_question_can_use_ai_without_being_mistaken_for_existing_order(self):
        self.inbound('Quero fazer um pedido, quais opções salgadas você sugere?')
        self.assertEqual(len(self.rows('whatsapp_ai_jobs')), 1)
        self.assertEqual(self.rows('whatsapp_outbox'), [])

    def test_transport_is_bounded_structured_redacted_and_has_no_customer_identity(self):
        self.inbound('Sugira sabores. Meu email é pessoa@example.com e telefone 44999999999, rua Particular 123')
        with patch.object(evolution, 'open_url', return_value=self.response({'intent': 'products', 'product_ids': ['carne', 'queijo']})) as transport:
            self.assertTrue(ai.process_one(self.app))
        request = transport.call_args.args[0]
        self.assertEqual(transport.call_args.kwargs['timeout'], 12)
        self.assertEqual(request.full_url, f'https://generativelanguage.googleapis.com/v1beta/models/{ai.MODEL}:generateContent')
        self.assertEqual(request.get_header('X-goog-api-key'), 'gemini-private-test')
        payload = json.loads(request.data)
        user = json.loads(payload['contents'][0]['parts'][0]['text'])
        self.assertEqual(set(user), {'message', 'catalog'})
        for secret in [self.phone, '44999999999', 'pessoa@example.com', 'Particular', 'gemini-private-test', 'evolution-private-test']:
            self.assertNotIn(secret, request.data.decode())
        self.assertEqual(payload['generationConfig']['maxOutputTokens'], 256)
        self.assertEqual(payload['generationConfig']['responseMimeType'], 'application/json')
        reply = self.rows('whatsapp_outbox')[0]['body']
        self.assertIn('R$ 4,00', reply); self.assertIn('cardápio', reply)
        self.assertEqual(self.rows('whatsapp_ai_jobs')[0]['status'], 'done')

    def test_prices_are_read_again_after_provider_response_outside_database_transaction(self):
        self.inbound()
        def interpret(*args):
            # A second immediate transaction proves the external call holds no DB lock.
            with db(self.request) as conn:
                conn.execute("UPDATE products SET price_cents=450 WHERE id='carne'")
            return {'intent': 'products', 'product_ids': ['carne']}
        with patch.object(ai, 'interpret', side_effect=interpret):
            self.assertTrue(ai.process_one(self.app))
        self.assertIn('R$ 4,50', self.rows('whatsapp_outbox')[0]['body'])

    def test_invalid_intents_products_free_text_and_truncated_results_fall_back(self):
        invalid = [({'intent': 'products', 'product_ids': ['inventado']}, 'STOP'),
                   ({'intent': 'execute', 'product_ids': []}, 'STOP'),
                   ({'intent': 'products', 'product_ids': ['carne'], 'price': '0,01'}, 'STOP'),
                   ({'intent': 'products', 'product_ids': []}, 'STOP'),
                   ({'intent': 'products', 'product_ids': ['carne'] * 4}, 'STOP'),
                   ({'intent': 'menu', 'product_ids': []}, 'MAX_TOKENS')]
        for index, (result, finish) in enumerate(invalid):
            self.inbound(phone='55119999999' + str(index))
            with patch.object(evolution, 'open_url', return_value=self.response(result, finish)):
                self.assertTrue(ai.process_one(self.app))
        self.assertTrue(all(row['status'] == 'fallback' for row in self.rows('whatsapp_ai_jobs')))
        self.assertTrue(all('atendimento automático' in row['body'] for row in self.rows('whatsapp_outbox')))

    def test_provider_failure_never_leaks_key_or_retries_and_basic_reply_survives(self):
        self.inbound()
        error = HTTPError('https://provider.example', 429, 'gemini-private-test', {}, None)
        with patch.object(ai, 'interpret', side_effect=error) as model:
            self.assertTrue(ai.process_one(self.app)); self.assertFalse(ai.process_one(self.app))
            model.assert_called_once()
        self.assertEqual(self.rows('whatsapp_ai_jobs')[0]['reason'], 'quota')
        self.assertNotIn('gemini-private-test', json.dumps(self.rows('whatsapp_outbox')))
        self.assertEqual(len(self.rows('whatsapp_ai_usage')), 1)

    def test_daily_limit_cooldown_and_per_customer_limit_use_basic_reply_without_charge(self):
        with patch.dict(os.environ, {'SAHARA_AI_DAILY_LIMIT': '1'}), patch.object(ai, 'interpret', return_value={'intent': 'menu', 'product_ids': []}) as model:
            self.inbound(); ai.process_one(self.app)
            self.inbound(phone='5511999999999'); ai.process_one(self.app)
            model.assert_called_once()
        self.assertEqual(self.rows('whatsapp_ai_jobs')[-1]['reason'], 'limit')
        with patch.object(ai, 'interpret') as model:
            self.inbound(); ai.process_one(self.app); model.assert_not_called()
        with db(self.request) as conn:
            for index in range(9):
                key = uuid4().hex
                conn.execute('INSERT INTO whatsapp_inbound VALUES(?,?,?,?)', (key, self.phone, 'teste', '2026-10-09'))
                ai.enqueue(conn, key, self.phone, 'evolution')
                conn.execute("UPDATE whatsapp_ai_jobs SET status='done' WHERE id=?", (key,))
                conn.execute('INSERT INTO whatsapp_ai_usage VALUES(?,?,?,?)', (key, self.phone, ai.day(), int(time.time()) - 31))
            conn.execute('UPDATE whatsapp_ai_usage SET timestamp=? WHERE phone=?', (int(time.time()) - 31, self.phone))
        with patch.object(ai, 'interpret') as model:
            self.inbound(); ai.process_one(self.app); model.assert_not_called()
        self.assertEqual(len(self.rows('whatsapp_ai_usage')), 10)

    def test_handoff_during_model_request_discards_the_late_response(self):
        self.inbound()
        def interpret(*args):
            with db(self.request) as conn:
                whatsapp.answer(conn, self.phone, 'atendente')
            return {'intent': 'products', 'product_ids': ['carne']}
        with patch.object(ai, 'interpret', side_effect=interpret):
            ai.process_one(self.app)
        self.assertEqual(self.rows('whatsapp_ai_jobs')[0]['status'], 'skipped')
        self.assertEqual(self.rows('whatsapp_outbox'), [])

    def test_generated_reply_is_skipped_if_team_takes_over_before_send(self):
        self.inbound()
        with patch.object(ai, 'interpret', return_value={'intent': 'products', 'product_ids': ['carne']}):
            ai.process_one(self.app)
        with db(self.request) as conn:
            whatsapp.answer(conn, self.phone, 'atendente')
            row = conn.execute('SELECT * FROM whatsapp_outbox').fetchone()
            data, status, _ = whatsapp.payload(conn, row, whatsapp.config())
        self.assertIsNone(data); self.assertEqual(status, 'skipped')

    def test_ai_handoff_pauses_only_this_customer_and_still_queues_confirmation(self):
        self.inbound('Preciso resolver um problema com a compra')
        with patch.object(ai, 'interpret', return_value={'intent': 'handoff', 'product_ids': []}):
            ai.process_one(self.app)
        with db(self.request) as conn:
            self.assertGreater(whatsapp.contact_identity(conn, self.phone)['human_until'], time.time())
        reply = self.rows('whatsapp_outbox')[0]
        self.assertEqual(reply['kind'], 'reply'); self.assertIn('pausado', reply['body'])

    def test_food_restrictions_are_handed_to_team_without_any_model_request(self):
        self.inbound('Tenho alergia e restrição alimentar, pode confirmar os ingredientes?')
        with patch.object(ai, 'interpret') as model:
            self.assertFalse(ai.process_one(self.app)); model.assert_not_called()
        self.assertEqual(self.rows('whatsapp_ai_jobs'), [])
        self.assertIn('equipe', self.rows('whatsapp_outbox')[0]['body'])

    def test_stale_jobs_and_stale_generated_replies_are_not_processed_or_sent(self):
        key = self.inbound()
        with db(self.request) as conn:
            conn.execute("UPDATE whatsapp_ai_jobs SET created_at='2020-01-01T00:00:00+00:00' WHERE id=?", (key,))
        with patch.object(ai, 'interpret') as model:
            ai.process_one(self.app); model.assert_not_called()
        self.assertEqual(self.rows('whatsapp_ai_jobs')[0]['reason'], 'stale')
        with db(self.request) as conn:
            whatsapp.queue(conn, 'stale-test', self.phone, 'ai_reply', 'Resposta antiga')
            conn.execute("UPDATE whatsapp_outbox SET created_at='2020-01-01T00:00:00+00:00'")
            row = conn.execute('SELECT * FROM whatsapp_outbox').fetchone()
            data, status, _ = whatsapp.payload(conn, row, whatsapp.config())
        self.assertIsNone(data); self.assertEqual(status, 'skipped')

    def test_restart_after_claim_preserves_usage_and_does_not_call_model_again(self):
        key = self.inbound()
        with db(self.request) as conn:
            conn.execute("UPDATE whatsapp_ai_jobs SET status='processing' WHERE id=?", (key,))
            conn.execute('INSERT INTO whatsapp_ai_usage VALUES(?,?,?,?)', (key, self.phone, ai.day(), int(time.time())))
            ai.recover(conn)
        with patch.object(ai, 'interpret') as model:
            ai.process_one(self.app); model.assert_not_called()
        self.assertEqual(len(self.rows('whatsapp_ai_usage')), 1)
        self.assertEqual(self.rows('whatsapp_ai_jobs')[0]['reason'], 'restart')
        self.assertEqual(len(self.rows('whatsapp_outbox')), 1)

    def test_concurrent_workers_cannot_claim_the_same_question_twice(self):
        self.inbound()
        entered, release = threading.Event(), threading.Event()
        def interpret(*args):
            entered.set(); self.assertTrue(release.wait(3))
            return {'intent': 'menu', 'product_ids': []}
        with patch.object(ai, 'interpret', side_effect=interpret) as model, ThreadPoolExecutor(max_workers=2) as pool:
            first = pool.submit(ai.process_one, self.app)
            try:
                self.assertTrue(entered.wait(3))
                self.assertFalse(pool.submit(ai.process_one, self.app).result(timeout=3))
            finally:
                release.set()
            self.assertTrue(first.result(timeout=3)); model.assert_called_once()
        self.assertEqual(len(self.rows('whatsapp_outbox')), 1)

    def test_pause_or_provider_change_while_queued_does_not_use_model(self):
        self.inbound()
        with patch.dict(os.environ, {'SAHARA_AI_ENABLED': '0'}), patch.object(ai, 'interpret') as model:
            ai.process_one(self.app); model.assert_not_called()
        self.inbound(phone='5511999999999')
        with patch.dict(os.environ, {'SAHARA_WHATSAPP_PROVIDER': 'meta', 'SAHARA_WHATSAPP_ACCESS_TOKEN': 'test',
                                    'SAHARA_WHATSAPP_PHONE_NUMBER_ID': '123', 'SAHARA_WHATSAPP_APP_SECRET': 'test',
                                    'SAHARA_WHATSAPP_VERIFY_TOKEN': 'test'}), patch.object(ai, 'interpret') as model:
            ai.process_one(self.app); model.assert_not_called()
        self.assertEqual(self.rows('whatsapp_ai_jobs')[-1]['status'], 'skipped')

    def test_dashboard_is_private_and_reports_configuration_not_connection_or_key(self):
        self.assertEqual(self.client.get('/api/admin/whatsapp').status_code, 401)
        self.client.post('/api/admin/login', json={'password': 'test-only-password'})
        response = self.client.get('/api/admin/whatsapp')
        state = response.json()['ai']
        self.assertTrue(state['configured']); self.assertIsNone(state['last_result'])
        self.assertEqual(state['daily_limit'], 50)
        self.assertNotIn('gemini-private-test', response.text)
        integrations = self.client.get('/api/integrations')
        self.assertTrue(next(item for item in integrations.json()['integrations'] if item['id'] == 'ai')['available'])
        self.assertNotIn('gemini-private-test', integrations.text)

    def test_historical_messages_do_not_use_ai_and_limits_are_bounded(self):
        self.inbound(timestamp=int(time.time()) - 90000)
        self.assertEqual(self.rows('whatsapp_ai_jobs'), [])
        for value, limit in [('99999', 200), ('0', 1), ('invalid', 50)]:
            with patch.dict(os.environ, {'SAHARA_AI_DAILY_LIMIT': value}):
                self.assertEqual(ai.config()['limit'], limit)

    def test_structured_status_intent_cannot_choose_another_customer(self):
        self.inbound('Pode verificar para mim?')
        with patch.object(ai, 'interpret', return_value={'intent': 'order_status', 'product_ids': []}):
            ai.process_one(self.app)
        self.assertIn('Não encontrei um pedido associado ao seu número', self.rows('whatsapp_outbox')[0]['body'])

    def login(self):
        response = self.client.post('/api/admin/login', json={'password': 'test-only-password'})
        return {'X-Sahara-CSRF': response.json()['csrf_token']}

    def test_configuration_check_is_private_csrf_protected_and_does_not_generate_or_send(self):
        with patch.object(evolution, 'open_url') as transport:
            self.assertEqual(self.client.post('/api/admin/whatsapp/ai/check', json={}).status_code, 401)
            headers = self.login()
            self.assertEqual(self.client.post('/api/admin/whatsapp/ai/check', json={}).status_code, 403)
            transport.assert_not_called()
        metadata = {'name': 'models/' + ai.MODEL, 'supportedGenerationMethods': ['generateContent', 'countTokens']}
        with patch.object(evolution, 'open_url', return_value=io.BytesIO(json.dumps(metadata).encode())) as transport:
            response = self.client.post('/api/admin/whatsapp/ai/check', json={}, headers=headers)
        self.assertEqual(response.json()['state'], 'available')
        request = transport.call_args.args[0]
        self.assertEqual(request.get_method(), 'GET'); self.assertIsNone(request.data)
        self.assertNotIn('generateContent', request.full_url)
        self.assertEqual(transport.call_args.kwargs['timeout'], 12)
        for table in ['whatsapp_ai_usage', 'whatsapp_outbox', 'whatsapp_ai_jobs']:
            self.assertEqual(self.rows(table), [])
        dashboard = self.client.get('/api/admin/whatsapp').json()['ai']
        self.assertEqual(dashboard['check']['state'], 'available')
        self.assertIsNone(dashboard['last_result'])
        self.assertNotIn('gemini-private-test', json.dumps(dashboard))

    def test_check_distinguishes_google_errors_without_returning_upstream_content(self):
        headers = self.login()
        cases = [(400, 'API_KEY_INVALID', 'authentication'), (400, 'API_KEY_EXPIRED', 'authentication'),
                 (403, 'SERVICE_DISABLED', 'service_disabled'), (403, '', 'permissions'),
                 (404, '', 'model'), (429, '', 'quota'), (500, '', 'provider'), (400, '', 'invalid_request')]
        for code, detail, reason in cases:
            body = {'error': {'message': 'gemini-private-test', 'details': [{'reason': detail}]}}
            error = HTTPError('https://provider.example', code, 'private-upstream-content', {}, io.BytesIO(json.dumps(body).encode()))
            with patch.object(evolution, 'open_url', side_effect=error):
                response = self.client.post('/api/admin/whatsapp/ai/check', json={}, headers=headers)
            self.assertEqual(response.status_code, 200)
            self.assertEqual(response.json()['state'], reason)
            for private in ['gemini-private-test', 'private-upstream-content', 'https://provider.example']:
                self.assertNotIn(private, response.text)

    def test_missing_configuration_check_does_not_call_google(self):
        headers = self.login()
        for variable, value, reason in [('SAHARA_AI_ENABLED', '0', 'disabled'), ('SAHARA_AI_API_KEY', '', 'missing_key')]:
            with patch.dict(os.environ, {variable: value}), patch.object(evolution, 'open_url') as transport:
                response = self.client.post('/api/admin/whatsapp/ai/check', json={}, headers=headers)
                self.assertEqual(response.json()['state'], reason); transport.assert_not_called()

    def test_old_verification_does_not_validate_a_new_key_or_disabled_ai(self):
        metadata = {'name': 'models/' + ai.MODEL, 'supportedGenerationMethods': ['generateContent']}
        with patch.object(evolution, 'open_url', return_value=io.BytesIO(json.dumps(metadata).encode())):
            ai.check_configuration(self.request)
        with db(self.request) as conn:
            self.assertEqual(ai.dashboard(conn)['check']['state'], 'available')
        for variable, value in [('SAHARA_AI_API_KEY', 'another-private-key'), ('SAHARA_AI_ENABLED', '0')]:
            with patch.dict(os.environ, {variable: value}), db(self.request) as conn:
                self.assertIsNone(ai.dashboard(conn)['check'])

    def test_generation_failure_stores_actionable_reason_and_keeps_basic_response(self):
        self.inbound()
        error = HTTPError('https://provider.example', 400, 'private', {},
                          io.BytesIO(json.dumps({'error': {'details': [{'reason': 'API_KEY_INVALID'}]}}).encode()))
        with patch.object(evolution, 'open_url', side_effect=error):
            ai.process_one(self.app)
        self.assertEqual(self.rows('whatsapp_ai_jobs')[0]['reason'], 'authentication')
        with db(self.request) as conn:
            self.assertIn('O Google recusou a chave', ai.dashboard(conn)['last_result']['message'])
        self.assertIn('atendimento automático', self.rows('whatsapp_outbox')[0]['body'])


if __name__ == '__main__':
    unittest.main()
