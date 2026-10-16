"""Consulta Merchant restrita e somente leitura, com transporte simulado."""
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
from uuid import UUID

from fastapi.testclient import TestClient

from server import ifood, ifood_merchant, ifood_stores
from server.core import hash_password
from server.main import create_app

CLIENT = '11111111-1111-4111-8111-111111111111'
MERCHANT = ifood_stores.TEST_MERCHANT_ID
OTHER = '22222222-2222-4222-8222-222222222222'
SECRET = 'fictional-merchant-secret-for-tests'
TOKEN = 'fictional-merchant-bearer-for-tests'
PATH = '/api/admin/ifood/merchant'


def response(value):
    return BytesIO(json.dumps(value).encode())


class IFoodMerchantTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.password = 'disposable-merchant-password'
        cls.password_hash = hash_password(cls.password)

    def setUp(self):
        self.directory = TemporaryDirectory()
        self.env = patch.dict(os.environ, {
            'SAHARA_IFOOD_CLIENT_ID': CLIENT, 'SAHARA_IFOOD_CLIENT_SECRET': SECRET,
            'SAHARA_IFOOD_MERCHANT_ID': MERCHANT, 'SAHARA_IFOOD_ENABLED': '1',
            'SAHARA_IFOOD_ENVIRONMENT': 'test', 'SAHARA_IFOOD_IMPORT_ENABLED': '0',
            'SAHARA_WHATSAPP_ENABLED': '0', 'SAHARA_AI_ENABLED': '0'})
        self.env.start()
        self.app = create_app(data_dir=Path(self.directory.name),
                              admin_password_hash=self.password_hash, secure_cookie=False)
        self.client = TestClient(self.app)
        login = self.client.post('/api/admin/login', json={'password': self.password})
        self.headers = {'X-Sahara-CSRF': login.json()['csrf_token']}
        self.verify_connection()
        self.merchants = [{'id': MERCHANT, 'name': 'Loja fictícia de teste'},
                          {'id': OTHER, 'name': 'Outra loja vinculada'}]
        self.merchant = {'id': MERCHANT, 'name': 'Loja fictícia de teste',
                         'address': {'city': 'Cidade fictícia'}}
        self.availability = [{'operation': 'delivery', 'salesChannel': 'IFOOD',
                              'state': 'CLOSED', 'validations': [{'state': 'WARNING'}]}]

    def tearDown(self):
        self.client.close()
        self.env.stop()
        self.directory.cleanup()

    def verify_connection(self, state='verified', merchant_id=MERCHANT, digest=None):
        with sqlite3.connect(self.app.state.db_path) as conn:
            conn.execute('INSERT OR REPLACE INTO ifood_connection_check VALUES(1,?,?,?)',
                (digest or ifood.fingerprint(ifood.config()), 10,
                 json.dumps({'state': state, 'merchant_id': merchant_id})))

    def clear_query(self):
        with sqlite3.connect(self.app.state.db_path) as conn:
            conn.execute('DELETE FROM ifood_merchant_query')

    def post(self, **kwargs):
        return self.client.post(PATH, json={}, headers=self.headers, **kwargs)

    def transport(self, request, **kwargs):
        path = request.full_url.removeprefix(ifood.HOST)
        values = {
            '/authentication/v1.0/oauth/token': {'accessToken': TOKEN, 'type': 'Bearer'},
            '/merchant/v1.0/merchants': self.merchants,
            '/merchant/v1.0/merchants/' + MERCHANT: self.merchant,
            '/merchant/v1.0/merchants/' + MERCHANT + '/status': self.availability,
        }
        self.assertIn(path, values)
        return response(values[path])

    def assert_no_payload(self, answer):
        self.assertFalse({'merchants', 'merchant', 'availability', 'client_id',
                          'configured_merchant_id'} & answer.keys())
        self.assertNotIn(SECRET, json.dumps(answer))
        self.assertNotIn(TOKEN, json.dumps(answer))

    def test_local_readiness_never_consults_provider_or_persists_a_query(self):
        with patch('server.ifood.evolution.open_url') as call:
            answer = self.client.get(PATH).json()
        call.assert_not_called()
        self.assertTrue(answer['query_available'])
        self.assertEqual(answer['environment'], 'test')
        self.assertEqual(answer['configured_merchant_id'], MERCHANT)
        self.assertIsNone(answer['last_result'])
        with sqlite3.connect(self.app.state.db_path) as conn:
            self.assertEqual(conn.execute('SELECT COUNT(*) FROM ifood_merchant_query').fetchone()[0], 0)

    def test_login_csrf_origin_and_strict_empty_body(self):
        with patch('server.ifood.evolution.open_url') as call:
            anonymous = TestClient(self.app)
            try:
                self.assertEqual(anonymous.get(PATH).status_code, 401)
                self.assertEqual(anonymous.post(PATH, json={}).status_code, 401)
            finally:
                anonymous.close()
            self.assertEqual(self.client.post(PATH, json={}).status_code, 403)
            self.assertEqual(self.client.post(PATH, json={}, headers={
                **self.headers, 'Origin': 'https://untrusted.invalid'}).status_code, 403)
            for body in ({'merchant_id': OTHER}, {'enabled': True}, {'state': 'OPEN'}, [], None):
                with self.subTest(body=body):
                    self.assertEqual(self.client.post(PATH, json=body, headers=self.headers).status_code, 422)
            self.assertEqual(self.client.post(PATH, headers=self.headers).status_code, 422)
        call.assert_not_called()

    def test_success_reads_only_fixed_urls_and_preserves_raw_availability(self):
        with patch('server.ifood.evolution.open_url', side_effect=self.transport) as call:
            answer = self.post()
        self.assertEqual(answer.status_code, 200)
        result = answer.json()
        self.assertEqual(result['state'], 'queried')
        self.assertEqual(result['merchants'], self.merchants)
        self.assertEqual(result['merchant'], self.merchant)
        self.assertEqual(result['availability'], self.availability)
        self.assertEqual(result['client_id'], CLIENT)
        self.assertEqual(result['configured_merchant_id'], MERCHANT)
        requests = [item.args[0] for item in call.call_args_list]
        self.assertEqual([(item.get_method(), item.full_url) for item in requests], [
            ('POST', ifood.HOST + '/authentication/v1.0/oauth/token'),
            ('GET', ifood.HOST + '/merchant/v1.0/merchants'),
            ('GET', ifood.HOST + '/merchant/v1.0/merchants/' + MERCHANT),
            ('GET', ifood.HOST + '/merchant/v1.0/merchants/' + MERCHANT + '/status')])
        self.assertEqual(parse_qs(requests[0].data.decode()), {
            'grantType': ['client_credentials'], 'clientId': [CLIENT], 'clientSecret': [SECRET]})
        for item in requests[1:]:
            self.assertIsNone(item.data)
            self.assertEqual(item.get_header('Authorization'), 'Bearer ' + TOKEN)
        self.assertTrue(all(item.kwargs['timeout'] == 8 for item in call.call_args_list))
        self.assertNotIn(SECRET, answer.text)
        self.assertNotIn(TOKEN, answer.text)
        last = self.client.get(PATH).json()['last_result']
        self.assertEqual(last['state'], 'queried')
        self.assert_no_payload(last)

    def test_disabled_production_missing_invalid_unverified_and_unallowlisted_never_call(self):
        changes = [
            ({'SAHARA_IFOOD_ENABLED': '0'}, 'not_ready'),
            ({'SAHARA_IFOOD_ENVIRONMENT': 'production'}, 'not_ready'),
            ({'SAHARA_IFOOD_CLIENT_SECRET': ''}, 'not_ready'),
            ({'SAHARA_IFOOD_MERCHANT_ID': '../other'}, 'not_ready'),
            ({'SAHARA_IFOOD_CLIENT_SECRET': 'rotated-test-secret'}, 'not_ready'),
            ({'SAHARA_IFOOD_MERCHANT_ID': OTHER}, 'restricted'),
        ]
        with patch('server.ifood.evolution.open_url') as call:
            for change, expected in changes:
                with self.subTest(change=list(change)), patch.dict(os.environ, change):
                    self.assertEqual(self.post().json()['state'], expected)
                    self.assertFalse(self.client.get(PATH).json()['query_available'])
            for state in ('configured', 'permissions', 'provider'):
                self.verify_connection(state=state)
                self.assertEqual(self.post().json()['state'], 'not_ready')
            self.verify_connection(merchant_id=OTHER)
            self.assertEqual(self.post().json()['state'], 'not_ready')
            self.verify_connection(digest='old-configuration')
            self.assertEqual(self.post().json()['state'], 'not_ready')
            with sqlite3.connect(self.app.state.db_path) as conn:
                conn.execute("UPDATE ifood_connection_check SET fingerprint=?,result_json='not json'",
                             (ifood.fingerprint(ifood.config()),))
            self.assertEqual(self.post().json()['state'], 'not_ready')
            with sqlite3.connect(self.app.state.db_path) as conn:
                conn.execute('DELETE FROM ifood_connection_check')
            self.assertEqual(self.post().json()['state'], 'not_ready')
        call.assert_not_called()

    def test_verified_but_unallowlisted_merchant_is_still_refused(self):
        with patch.dict(os.environ, {'SAHARA_IFOOD_MERCHANT_ID': OTHER}):
            self.verify_connection(merchant_id=OTHER)
            with patch('server.ifood.evolution.open_url') as call:
                self.assertEqual(self.post().json()['state'], 'restricted')
            call.assert_not_called()

    def test_configuration_rotation_during_each_phase_discards_data_and_stops_further_gets(self):
        for rotate_at in range(4):
            with self.subTest(phase=rotate_at):
                self.clear_query()
                cfg = ifood.config()
                calls = []

                def rotated(request, **kwargs):
                    calls.append(request.full_url)
                    value = self.transport(request, **kwargs)
                    if len(calls) - 1 == rotate_at:
                        os.environ['SAHARA_IFOOD_CLIENT_SECRET'] = SECRET + '-rotated'
                    return value

                try:
                    with patch('server.ifood.evolution.open_url', side_effect=rotated):
                        answer = self.post().json()
                    self.assertEqual(answer['state'], 'configuration_changed')
                    self.assert_no_payload(answer)
                    self.assertEqual(len(calls), rotate_at + 1)
                    self.assertIsNone(self.client.get(PATH).json()['last_result'])
                finally:
                    os.environ['SAHARA_IFOOD_CLIENT_SECRET'] = cfg[ifood.REQUIRED[1]]

    def test_revoked_connection_before_first_network_call_is_refused(self):
        original = ifood_merchant.readiness
        count = 0

        def revoke(request, cfg):
            nonlocal count
            count += 1
            if count == 2:
                self.verify_connection(state='permissions')
            return original(request, cfg)

        with patch('server.ifood_merchant.readiness', side_effect=revoke), \
                patch('server.ifood.evolution.open_url') as call:
            answer = self.post().json()
        self.assertEqual(answer['state'], 'configuration_changed')
        self.assert_no_payload(answer)
        call.assert_not_called()

    def test_membership_detail_mismatch_and_invalid_response_never_return_partial_payload(self):
        cases = [
            ('merchants', [], 2), ('merchants', [{'id': OTHER}], 2),
            ('merchants', [{'id': MERCHANT}, {'id': MERCHANT}], 2),
            ('merchants', [{}], 2), ('merchants', [None], 2), ('merchants', {}, 2),
            ('merchant', {'id': OTHER}, 3), ('merchant', {}, 3), ('merchant', [], 3),
            ('availability', {}, 4), ('availability', [None], 4),
            ('availability', [{'state': float('nan')}], 4),
        ]
        for phase, value, expected_calls in cases:
            with self.subTest(phase=phase, value=value):
                self.clear_query()
                attribute = {'merchants': 'merchants', 'merchant': 'merchant', 'availability': 'availability'}[phase]
                old = getattr(self, attribute)
                setattr(self, attribute, value)
                try:
                    with patch('server.ifood.evolution.open_url', side_effect=self.transport) as call:
                        answer = self.post().json()
                    self.assertEqual(answer['state'], 'response')
                    self.assert_no_payload(answer)
                    self.assertEqual(call.call_count, expected_calls)
                finally:
                    setattr(self, attribute, old)

    def test_oversized_deep_or_invalid_json_is_safe(self):
        nested = {'leaf': True}
        for _ in range(22):
            nested = {'nested': nested}
        for raw in (b'x' * 262145, b'not json', json.dumps({'wrong': 'type'}).encode()):
            self.clear_query()
            with patch('server.ifood.evolution.open_url', side_effect=[
                    response({'accessToken': TOKEN}), BytesIO(raw)]):
                answer = self.post().json()
            self.assertEqual(answer['state'], 'response')
            self.assert_no_payload(answer)
        self.clear_query()
        self.availability = [nested]
        with patch('server.ifood.evolution.open_url', side_effect=self.transport):
            answer = self.post().json()
        self.assertEqual(answer['state'], 'response')
        self.assert_no_payload(answer)

    def test_recursive_secrets_and_sensitive_keys_are_scrubbed_without_persisting_raw_data(self):
        self.merchant['nested'] = {
            'accessToken': TOKEN, 'client_secret': SECRET, 'password': 'other-private-value',
            'Authorization': 'Bearer ' + TOKEN, 'api-key': 'private-key',
            'items': [{'note': 'prefix ' + SECRET + ' ' + TOKEN + ' suffix',
                       'safe-key-' + SECRET: 'remaining', 'credential': {'any': 'value'}}]}
        self.availability[0]['note'] = SECRET + ':' + TOKEN
        with patch('server.ifood.evolution.open_url', side_effect=self.transport):
            answer = self.post()
        self.assertEqual(answer.json()['state'], 'queried')
        for forbidden in (SECRET, TOKEN, 'other-private-value', 'private-key', 'accessToken', 'client_secret', 'credential'):
            self.assertNotIn(forbidden, answer.text)
        self.assertIn('[redigido]', answer.text)
        with sqlite3.connect(self.app.state.db_path) as conn:
            saved = conn.execute('SELECT result_json FROM ifood_merchant_query').fetchone()[0]
        self.assertEqual(set(json.loads(saved)), {'state', 'message', 'checked_at'})
        self.assertNotIn('nested', saved)

    def test_numeric_credentials_echoed_as_numbers_are_redacted_but_regular_numbers_remain(self):
        numeric_secret, numeric_token = '12345678', '87654321'
        self.merchant['debug'] = {'integer': int(numeric_secret), 'float': float(numeric_token),
                                  'nested': [int(numeric_token + '0')], 'ordinary': 123,
                                  'decimal': 4.5, 'flag': True}

        def numeric(request, **kwargs):
            if request.full_url.endswith('/oauth/token'):
                return response({'accessToken': numeric_token})
            return self.transport(request, **kwargs)

        with patch.dict(os.environ, {'SAHARA_IFOOD_CLIENT_SECRET': numeric_secret}):
            self.verify_connection()
            with patch('server.ifood.evolution.open_url', side_effect=numeric):
                answer = self.post()
        self.assertEqual(answer.json()['state'], 'queried')
        self.assertNotIn(numeric_secret, answer.text)
        self.assertNotIn(numeric_token, answer.text)
        self.assertEqual(answer.json()['merchant']['debug'], {
            'integer': '[redigido]', 'float': '[redigido]', 'nested': ['[redigido]'],
            'ordinary': 123, 'decimal': 4.5, 'flag': True})

    def test_all_http_errors_and_network_failures_are_safe_and_never_claim_availability(self):
        for phase_index, phase in enumerate(('token', 'merchants', 'merchant', 'availability')):
            for status in (400, 401, 403, 404, 429, 500, 302):
                with self.subTest(phase=phase, status=status):
                    self.clear_query()
                    count = 0

                    def fail(request, **kwargs):
                        nonlocal count
                        count += 1
                        if count - 1 == phase_index:
                            raise HTTPError(request.full_url, status, SECRET, {}, BytesIO((SECRET + TOKEN).encode()))
                        return self.transport(request, **kwargs)

                    with patch('server.ifood.evolution.open_url', side_effect=fail):
                        answer = self.post().json()
                    self.assertNotEqual(answer['state'], 'queried')
                    self.assertEqual(answer['phase'], phase)
                    self.assert_no_payload(answer)
                    self.assertEqual(count, phase_index + 1)
                    self.assertNotIn('CLOSED', json.dumps(answer))
        self.clear_query()
        with patch('server.ifood.evolution.open_url', side_effect=URLError(SECRET)) as call:
            answer = self.post().json()
        self.assertEqual(answer['state'], 'network')
        self.assert_no_payload(answer)
        self.assertEqual(call.call_count, 1)

    def test_cooldown_survives_restart_and_never_replays_private_payload(self):
        with patch('server.ifood.evolution.open_url', side_effect=self.transport) as call:
            self.assertEqual(self.post().json()['state'], 'queried')
            self.assertEqual(self.post().json()['state'], 'wait')
            self.assertEqual(call.call_count, 4)
        restarted = create_app(data_dir=Path(self.directory.name),
                               admin_password_hash=self.password_hash, secure_cookie=False)
        other = TestClient(restarted)
        try:
            login = other.post('/api/admin/login', json={'password': self.password})
            with patch('server.ifood.evolution.open_url') as call:
                answer = other.post(PATH, json={}, headers={'X-Sahara-CSRF': login.json()['csrf_token']}).json()
            self.assertEqual(answer['state'], 'wait')
            self.assert_no_payload(answer)
            call.assert_not_called()
        finally:
            other.close()
        with sqlite3.connect(self.app.state.db_path) as conn:
            conn.execute('UPDATE ifood_merchant_query SET started_at=started_at-31')
        with patch('server.ifood.evolution.open_url', side_effect=self.transport) as call:
            self.assertEqual(self.post().json()['state'], 'queried')
        self.assertEqual(call.call_count, 4)

    def test_running_lease_survives_restart_and_expires_independently_of_connection_check(self):
        with sqlite3.connect(self.app.state.db_path) as conn:
            conn.execute("INSERT INTO ifood_merchant_query VALUES(1,?,strftime('%s','now')-31,'interrupted','')",
                         (ifood.fingerprint(ifood.config()),))
        with patch('server.ifood.evolution.open_url') as call:
            self.assertEqual(self.post().json()['state'], 'wait')
        call.assert_not_called()
        with sqlite3.connect(self.app.state.db_path) as conn:
            conn.execute('UPDATE ifood_merchant_query SET started_at=started_at-150')
        with patch('server.ifood.evolution.open_url', side_effect=self.transport):
            self.assertEqual(self.post().json()['state'], 'queried')

    def test_concurrent_queries_make_only_one_set_of_provider_requests(self):
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
            self.assertEqual(future.result().json()['state'], 'queried')
        self.assertEqual(call.call_count, 4)

    def test_linked_merchants_follow_collection_pagination_without_truncating(self):
        self.merchants = [{'id': MERCHANT, 'name': 'Loja de teste'}] + [
            {'id': str(UUID(int=value)), 'name': 'Loja fictícia'} for value in range(1, 100)]
        def transport(request, **kwargs):
            if request.full_url.endswith('/merchants?page=2&size=100'):
                return response([{'id': str(UUID(int=100)), 'name': 'Última loja fictícia'}])
            return self.transport(request, **kwargs)
        with patch('server.ifood.evolution.open_url', side_effect=transport) as call:
            result = self.post().json()
        self.assertEqual(result['state'], 'queried')
        self.assertEqual(len(result['merchants']), 101)
        self.assertEqual(call.call_count, 5)
        self.assertTrue(any(x.args[0].full_url.endswith('?page=2&size=100') for x in call.call_args_list))

    def test_pagination_bound_refuses_a_potentially_incomplete_linked_shop_list(self):
        self.merchants = [{'id': MERCHANT, 'name': 'Loja de teste'}] + [
            {'id': str(UUID(int=value)), 'name': 'Loja fictícia'} for value in range(1, 100)]
        def transport(request, **kwargs):
            if '/merchants?page=' in request.full_url:
                return response([{'id': str(UUID(int=value)), 'name': 'Loja fictícia'}
                                 for value in range(101, 201)])
            return self.transport(request, **kwargs)
        with patch('server.ifood.evolution.open_url', side_effect=transport) as call:
            result = self.post().json()
        self.assertEqual(result['state'], 'response')
        self.assert_no_payload(result)
        self.assertEqual(call.call_count, 4)

    def test_no_changes_to_business_test_import_queues_or_connection_check(self):
        with sqlite3.connect(self.app.state.db_path) as conn:
            conn.execute("INSERT INTO ifood_import_state VALUES(1,'old-import',10,'{}')")
            conn.execute("INSERT INTO ifood_imported_events VALUES(?,?,?,'fictional-order','2026-10-15T00:00:00Z',0)",
                         (MERCHANT, CLIENT, 'fictional-event'))
            tables = [row[0] for row in conn.execute(
                "SELECT name FROM sqlite_master WHERE type='table' AND name!='ifood_merchant_query'")]
            before = {table: conn.execute('SELECT * FROM "' + table + '"').fetchall() for table in tables}
        with patch('server.ifood.evolution.open_url', side_effect=self.transport):
            self.assertEqual(self.post().json()['state'], 'queried')
        with sqlite3.connect(self.app.state.db_path) as conn:
            after = {table: conn.execute('SELECT * FROM "' + table + '"').fetchall() for table in tables}
        self.assertEqual(before, after)


if __name__ == '__main__':
    unittest.main()
