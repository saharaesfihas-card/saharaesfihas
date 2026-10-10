"""Conexão iFood com transporte simulado; nenhum pedido ou segredo real."""
from concurrent.futures import ThreadPoolExecutor
from io import BytesIO
import json
import os
from pathlib import Path
import sqlite3
from tempfile import TemporaryDirectory
from threading import Event
import unittest
from unittest.mock import patch
from urllib.error import HTTPError, URLError
from urllib.parse import parse_qs

from fastapi.testclient import TestClient

from server import ifood
from server.core import hash_password
from server.main import create_app

CLIENT = '11111111-1111-4111-8111-111111111111'
MERCHANT = '22222222-2222-4222-8222-222222222222'
SECRET = 'fictional-secret-for-tests-only'
TOKEN = 'fictional-bearer-token-for-tests-only'


class IFoodTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.password = 'disposable-test-password'
        cls.password_hash = hash_password(cls.password)

    def setUp(self):
        self.directory = TemporaryDirectory()
        self.env = patch.dict(os.environ, {
            'SAHARA_IFOOD_CLIENT_ID': CLIENT, 'SAHARA_IFOOD_CLIENT_SECRET': SECRET,
            'SAHARA_IFOOD_MERCHANT_ID': MERCHANT, 'SAHARA_IFOOD_ENABLED': '1',
            'SAHARA_IFOOD_ENVIRONMENT': 'test', 'SAHARA_WHATSAPP_ENABLED': '0', 'SAHARA_AI_ENABLED': '0'})
        self.env.start()
        self.app = create_app(data_dir=Path(self.directory.name),
                              admin_password_hash=self.password_hash, secure_cookie=False)
        self.client = TestClient(self.app)
        self.login = self.client.post('/api/admin/login', json={'password': self.password}).json()
        self.headers = {'X-Sahara-CSRF': self.login['csrf_token']}

    def tearDown(self):
        self.client.close()
        self.env.stop()
        self.directory.cleanup()

    def post(self):
        return self.client.post('/api/admin/ifood/check', json={}, headers=self.headers)

    def clear(self):
        with sqlite3.connect(self.app.state.db_path) as conn:
            conn.execute('DELETE FROM ifood_connection_check')

    def transport(self, request, **kwargs):
        if request.full_url.endswith('/oauth/token'):
            return BytesIO(json.dumps({'accessToken': TOKEN, 'type': 'bearer'}).encode())
        return BytesIO(json.dumps({'id': MERCHANT, 'name': 'Private merchant data', 'ignored': SECRET}).encode())

    def test_login_csrf_and_origin_required(self):
        with patch('server.ifood.evolution.open_url') as call:
            self.assertEqual(self.client.post('/api/admin/ifood/check', json={}).status_code, 403)
            self.assertEqual(self.client.post('/api/admin/ifood/check', json={}, headers={
                **self.headers, 'Origin': 'https://untrusted.invalid'}).status_code, 403)
            anonymous = TestClient(self.app)
            self.assertEqual(anonymous.get('/api/admin/ifood').status_code, 401)
            self.assertEqual(anonymous.post('/api/admin/ifood/check', json={}).status_code, 401)
            anonymous.close()
            call.assert_not_called()

    def test_success_fixed_urls_form_secret_and_ephemeral_token(self):
        with patch('server.ifood.evolution.open_url', side_effect=self.transport) as call:
            response = self.post()
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json()['state'], 'verified')
        first, second = [entry.args[0] for entry in call.call_args_list]
        self.assertEqual(first.full_url, ifood.HOST + '/authentication/v1.0/oauth/token')
        self.assertEqual(parse_qs(first.data.decode()), {
            'grantType': ['client_credentials'], 'clientId': [CLIENT], 'clientSecret': [SECRET]})
        self.assertEqual(first.get_method(), 'POST')
        self.assertEqual(second.full_url, ifood.HOST + '/merchant/v1.0/merchants/' + MERCHANT)
        self.assertEqual(second.get_header('Authorization'), 'Bearer ' + TOKEN)
        self.assertTrue(all(entry.kwargs['timeout'] == 8 for entry in call.call_args_list))
        status = self.client.get('/api/admin/ifood').json()
        self.assertEqual(status['last_result']['state'], 'verified')
        self.assertFalse(status['orders_enabled'])
        self.assertNotIn(SECRET, response.text + json.dumps(status))
        self.assertNotIn(TOKEN, response.text + json.dumps(status))
        with sqlite3.connect(self.app.state.db_path) as conn:
            row = conn.execute('SELECT result_json FROM ifood_connection_check').fetchone()[0]
            self.assertNotIn(SECRET, row)
            self.assertNotIn(TOKEN, row)
            for table in ('orders', 'customers', 'order_events', 'whatsapp_outbox'):
                self.assertEqual(conn.execute('SELECT COUNT(*) FROM ' + table).fetchone()[0], 0)
        entry = next(p for p in self.client.get('/api/integrations').json()['integrations'] if p['id'] == 'ifood')
        self.assertFalse(entry['available'])

    def test_missing_disabled_and_invalid_config_never_call_provider(self):
        changes = [({'SAHARA_IFOOD_ENABLED': '0'}, 'disabled'),
                   ({'SAHARA_IFOOD_CLIENT_SECRET': ''}, 'missing'),
                   ({'SAHARA_IFOOD_MERCHANT_ID': '../../other-host'}, 'invalid'),
                   ({'SAHARA_IFOOD_CLIENT_ID': 'chosen-password'}, 'invalid'),
                   ({'SAHARA_IFOOD_CLIENT_SECRET': 'secret\nheader'}, 'invalid'),
                   ({'SAHARA_IFOOD_ENVIRONMENT': 'production'}, 'mode')]
        with patch('server.ifood.evolution.open_url') as call:
            for change, expected in changes:
                with self.subTest(expected=expected), patch.dict(os.environ, change):
                    self.assertEqual(self.post().json()['state'], expected)
                    self.assertFalse(self.client.get('/api/admin/ifood').json()['configured'])
            call.assert_not_called()

    def test_http_errors_are_safe_and_do_not_leak_provider_body(self):
        for code, state in [(400, 'authentication'), (401, 'authentication'), (403, 'permissions'),
                            (404, 'provider'), (429, 'quota'), (500, 'provider'), (302, 'provider')]:
            with self.subTest(code=code):
                self.clear()
                error = HTTPError(ifood.HOST, code, SECRET, {}, BytesIO((TOKEN + SECRET).encode()))
                with patch('server.ifood.evolution.open_url', side_effect=error) as call:
                    response = self.post()
                self.assertEqual(response.json()['state'], state)
                self.assertNotIn(SECRET, response.text)
                self.assertNotIn(TOKEN, response.text)
                self.assertEqual(call.call_count, 1)

    def test_missing_merchant_classified_after_token(self):
        error = HTTPError(ifood.HOST, 404, SECRET, {}, BytesIO(SECRET.encode()))
        with patch('server.ifood.evolution.open_url', side_effect=[
                BytesIO(json.dumps({'accessToken': TOKEN}).encode()), error]):
            self.assertEqual(self.post().json()['state'], 'merchant')

    def test_invalid_json_size_token_or_merchant_never_verified(self):
        cases = [[BytesIO(b'not json')], [BytesIO(b'x' * 65537)],
                 [BytesIO(json.dumps({'accessToken': 'invalid\nheader'}).encode())],
                 [BytesIO(json.dumps({'accessToken': TOKEN}).encode()),
                  BytesIO(json.dumps({'id': CLIENT}).encode())],
                 [BytesIO(json.dumps({'accessToken': TOKEN}).encode()), BytesIO(b'[]')]]
        for responses in cases:
            with self.subTest(responses=len(responses)):
                self.clear()
                with patch('server.ifood.evolution.open_url', side_effect=responses):
                    self.assertEqual(self.post().json()['state'], 'response')

    def test_network_failure_no_retry(self):
        with patch('server.ifood.evolution.open_url', side_effect=URLError(SECRET)) as call:
            response = self.post()
        self.assertEqual(response.json()['state'], 'network')
        self.assertNotIn(SECRET, response.text)
        self.assertEqual(call.call_count, 1)

    def test_persistent_cooldown_and_configuration_change(self):
        with patch('server.ifood.evolution.open_url', side_effect=self.transport) as call:
            self.assertEqual(self.post().json()['state'], 'verified')
            self.assertEqual(self.post().json()['state'], 'wait')
            with patch.dict(os.environ, {'SAHARA_IFOOD_CLIENT_SECRET': SECRET + '-rotated'}):
                self.assertIsNone(self.client.get('/api/admin/ifood').json()['last_result'])
                self.assertEqual(self.post().json()['state'], 'wait')
        self.assertEqual(call.call_count, 2)

    def test_concurrent_check_only_one_authentication(self):
        started, release = Event(), Event()
        def delayed(request, **kwargs):
            if request.full_url.endswith('/oauth/token'):
                started.set()
                if not release.wait(5):
                    raise TimeoutError()
            return self.transport(request, **kwargs)
        with patch('server.ifood.evolution.open_url', side_effect=delayed) as call, ThreadPoolExecutor(2) as pool:
            future = pool.submit(self.post)
            self.assertTrue(started.wait(5))
            try:
                self.assertEqual(self.post().json()['state'], 'wait')
            finally:
                release.set()
            self.assertEqual(future.result().json()['state'], 'verified')
        self.assertEqual(call.call_count, 2)
