"""Preparação da conta nova não autentica nem substitui o importador existente."""
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

from server import ifood, ifood_homologation_profile as profile, ifood_stores
from server.core import hash_password
from server.main import create_app

MERCHANT = '22222222-2222-4222-8222-222222222222'
SECRET = 'fictional-new-account-secret'
TOKEN = 'fictional-new-account-token'
PATH = '/api/admin/ifood/homologation-profile'


def response(value, status=200):
    result = BytesIO(json.dumps(value).encode())
    result.status = status
    return result


class IFoodHomologationProfileTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.password = 'disposable-profile-password'
        cls.password_hash = hash_password(cls.password)

    def setUp(self):
        self.directory = TemporaryDirectory()
        self.env = patch.dict(os.environ, {
            **dict.fromkeys(profile.REQUIRED, ''),
            profile.ENVIRONMENT: 'test',
            'SAHARA_IFOOD_CLIENT_ID': '11111111-1111-4111-8111-111111111111',
            'SAHARA_IFOOD_CLIENT_SECRET': 'fictional-existing-importer-secret',
            'SAHARA_IFOOD_MERCHANT_ID': ifood_stores.TEST_MERCHANT_ID,
            'SAHARA_IFOOD_ENABLED': '1', 'SAHARA_IFOOD_ENVIRONMENT': 'test',
            'SAHARA_IFOOD_IMPORT_ENABLED': '0', 'SAHARA_WHATSAPP_ENABLED': '0',
            'SAHARA_AI_ENABLED': '0',
        })
        self.env.start()
        self.app = create_app(data_dir=Path(self.directory.name),
                              admin_password_hash=self.password_hash, secure_cookie=False)
        self.client = TestClient(self.app)
        login = self.client.post('/api/admin/login', json={'password': self.password})
        self.headers = {'X-Sahara-CSRF': login.json()['csrf_token']}

    def tearDown(self):
        self.client.close()
        self.env.stop()
        self.directory.cleanup()

    def configure(self, client_id=profile.CLIENT_ID, secret=SECRET, merchant_id=MERCHANT):
        os.environ.update(dict(zip(profile.REQUIRED, (client_id, secret, merchant_id))))

    def discover(self, **kwargs):
        return self.client.post(PATH + '/discover', json={}, headers=self.headers, **kwargs)

    def transport(self, request, **kwargs):
        self.assertEqual(kwargs['timeout'], 8)
        path = request.full_url.removeprefix(ifood.HOST)
        if path == '/authentication/v1.0/oauth/token':
            self.assertEqual(request.get_method(), 'POST')
            self.assertEqual(parse_qs(request.data.decode()), {'grantType': ['client_credentials'],
                'clientId': [profile.CLIENT_ID], 'clientSecret': [SECRET]})
            return response({'accessToken': TOKEN, 'type': 'Bearer'})
        self.assertEqual(request.get_method(), 'GET')
        self.assertIsNone(request.data)
        self.assertEqual(request.get_header('Authorization'), 'Bearer ' + TOKEN)
        if path == '/merchant/v1.0/merchants':
            return response([{'id': MERCHANT, 'name': 'private-shop-name', 'token': TOKEN}])
        self.assertEqual(path, '/merchant/v1.0/merchants/' + MERCHANT)
        return response({'id': MERCHANT, 'test': 'TEST', 'name': 'private-shop-name',
                         'address': 'private-address', 'clientSecret': SECRET})

    def clear_discovery(self):
        with sqlite3.connect(self.app.state.db_path) as conn:
            conn.execute('DELETE FROM ifood_homologation_discovery')

    def assert_blocked(self, result):
        self.assertFalse(result['connection_verified'])
        self.assertFalse(result['operations_available'])
        self.assertFalse(result['credential_renewal_confirmed'])
        self.assertFalse(result['test_merchant_confirmed'])

    def test_endpoint_requires_admin(self):
        anonymous = TestClient(self.app)
        try:
            self.assertEqual(anonymous.get(PATH).status_code, 401)
        finally:
            anonymous.close()

    def test_unconfigured_profile_stays_local_and_blocked(self):
        with patch('server.ifood.evolution.open_url') as call:
            response = self.client.get(PATH)
        self.assertEqual(response.status_code, 200)
        call.assert_not_called()
        result = response.json()
        self.assertEqual(result['state'], 'unconfigured')
        self.assertEqual(result['missing'], list(profile.REQUIRED))
        self.assertFalse(result['configured'])
        self.assertIsNone(result['configured_merchant_id'])
        self.assert_blocked(result)

    def test_partial_profile_never_falls_back_to_legacy_credentials(self):
        os.environ[profile.REQUIRED[1]] = SECRET
        result = self.client.get(PATH).json()
        self.assertEqual(result['state'], 'missing')
        self.assertEqual(result['missing'], [profile.REQUIRED[0], profile.REQUIRED[2]])
        self.assertNotIn(SECRET, json.dumps(result))
        self.assertIsNone(result['configured_merchant_id'])
        self.assert_blocked(result)

    def test_only_exact_known_new_test_application_is_accepted(self):
        self.configure(client_id=ifood.config()[ifood.REQUIRED[0]])
        result = self.client.get(PATH).json()
        self.assertEqual(result['state'], 'invalid')
        self.assertEqual(result['invalid'], [profile.REQUIRED[0]])
        self.assertIsNone(result['configured_merchant_id'])
        self.assert_blocked(result)

    def test_numeric_portal_id_cannot_be_used_as_merchant_uuid(self):
        self.configure(merchant_id='4172927')
        result = self.client.get(PATH).json()
        self.assertEqual(result['state'], 'invalid')
        self.assertEqual(result['invalid'], [profile.REQUIRED[2]])
        self.assertIsNone(result['configured_merchant_id'])
        self.assert_blocked(result)

    def test_invalid_secret_and_untrusted_values_are_never_reflected(self):
        for secret in ('short', 'control\ninside-secret', 'a' * 4097):
            with self.subTest(secret_length=len(secret)):
                self.configure(secret=secret)
                response = self.client.get(PATH)
                self.assertEqual(response.json()['state'], 'invalid')
                self.assertEqual(response.json()['invalid'], [profile.REQUIRED[1]])
                self.assertNotIn(secret, response.text)
                self.assert_blocked(response.json())
        self.configure(client_id='untrusted-client-value', merchant_id='untrusted-merchant-value')
        response = self.client.get(PATH)
        for value in ('untrusted-client-value', 'untrusted-merchant-value', SECRET):
            self.assertNotIn(value, response.text)

    def test_complete_valid_profile_requires_renewal_and_positive_test_proof(self):
        self.configure(client_id=profile.CLIENT_ID.upper(), merchant_id=MERCHANT.upper())
        with patch('server.ifood.evolution.open_url') as call:
            result = self.client.get(PATH).json()
        call.assert_not_called()
        self.assertEqual(result['state'], 'test_proof_required')
        self.assertTrue(result['configured'])
        self.assertEqual(result['configured_merchant_id'], MERCHANT)
        self.assertEqual(result['missing'], [])
        self.assertEqual(result['invalid'], [])
        self.assertNotIn(SECRET, json.dumps(result))
        self.assert_blocked(result)

    def test_profile_does_not_change_legacy_configuration_checks_or_queues(self):
        legacy = ifood.config()
        digest = ifood.fingerprint(legacy)
        with sqlite3.connect(self.app.state.db_path) as conn:
            conn.execute('INSERT OR REPLACE INTO ifood_connection_check VALUES(1,?,?,?)',
                         (digest, 10, json.dumps({'state': 'verified',
                                                'merchant_id': ifood_stores.TEST_MERCHANT_ID})))
            tables = [row[0] for row in conn.execute("SELECT name FROM sqlite_master WHERE type='table' AND name LIKE 'ifood_%'")]
            before = {name: conn.execute('SELECT * FROM "' + name + '"').fetchall() for name in tables}
        self.configure()
        self.assertEqual(self.client.get(PATH).json()['state'], 'test_proof_required')
        self.assertEqual(ifood.config(), legacy)
        self.assertEqual(ifood.fingerprint(ifood.config()), digest)
        self.assertEqual(self.client.get('/api/admin/ifood').json()['last_result']['state'], 'verified')
        with sqlite3.connect(self.app.state.db_path) as conn:
            after = {name: conn.execute('SELECT * FROM "' + name + '"').fetchall() for name in tables}
        self.assertEqual(before, after)

    def test_discovery_works_without_merchant_uuid_and_returns_only_positive_test_ids(self):
        self.configure(merchant_id='')
        ready = self.client.get(PATH).json()
        self.assertTrue(ready['discovery_available'])
        self.assertFalse(ready['configured'])
        self.assertIsNone(ready['configured_merchant_id'])
        legacy = ifood.config()
        with patch('server.ifood.evolution.open_url', side_effect=self.transport) as call:
            result = self.discover()
        self.assertEqual(result.status_code, 200)
        self.assertEqual(result.json()['state'], 'queried')
        self.assertEqual(result.json()['merchants'], [{'merchant_id': MERCHANT, 'is_test': True}])
        self.assertFalse(result.json()['operations_available'])
        self.assertFalse(result.json()['connection_verified'])
        self.assertEqual(call.call_count, 3)
        for value in (SECRET, TOKEN, 'private-shop-name', 'private-address'):
            self.assertNotIn(value, result.text)
        with sqlite3.connect(self.app.state.db_path) as conn:
            row = conn.execute('SELECT * FROM ifood_homologation_discovery').fetchone()
            self.assertEqual(conn.execute('SELECT COUNT(*) FROM ifood_connection_check').fetchone()[0], 0)
        persisted = json.dumps(row)
        for value in (SECRET, TOKEN, MERCHANT, 'private-shop-name', 'private-address'):
            self.assertNotIn(value, persisted)
        self.assertEqual(ifood.config(), legacy)
        self.assertEqual(os.environ[profile.REQUIRED[2]], '')

    def test_discovery_requires_admin_csrf_origin_and_strict_empty_body(self):
        self.configure(merchant_id='')
        with patch('server.ifood.evolution.open_url') as call:
            anonymous = TestClient(self.app)
            try:
                self.assertEqual(anonymous.post(PATH + '/discover', json={}).status_code, 401)
            finally:
                anonymous.close()
            self.assertEqual(self.client.post(PATH + '/discover', json={}).status_code, 403)
            self.assertEqual(self.client.post(PATH + '/discover', json={}, headers={
                **self.headers, 'Origin': 'https://untrusted.invalid'}).status_code, 403)
            for body in ({'clientSecret': SECRET}, {'merchant_id': MERCHANT}, [], None):
                with self.subTest(body_type=type(body).__name__):
                    self.assertEqual(self.client.post(PATH + '/discover', json=body,
                                                      headers=self.headers).status_code, 422)
            self.assertEqual(self.client.post(PATH + '/discover', headers=self.headers).status_code, 422)
        call.assert_not_called()

    def test_discovery_blocks_partial_wrong_application_and_production_before_oauth(self):
        variants = [('', SECRET, 'test'), (profile.CLIENT_ID, '', 'test'),
                    (ifood.config()[ifood.REQUIRED[0]], SECRET, 'test'),
                    (profile.CLIENT_ID, SECRET, 'production')]
        for client_id, secret, environment in variants:
            with self.subTest(client_matches=client_id == profile.CLIENT_ID,
                              secret_present=bool(secret), environment=environment):
                self.configure(client_id=client_id, secret=secret, merchant_id='')
                os.environ[profile.ENVIRONMENT] = environment
                with patch('server.ifood.evolution.open_url') as call:
                    result = self.discover().json()
                call.assert_not_called()
                self.assertFalse(self.client.get(PATH).json()['discovery_available'])
                self.assertEqual(result['merchants'], [])
                self.assertNotEqual(result['state'], 'queried')
        with sqlite3.connect(self.app.state.db_path) as conn:
            self.assertEqual(conn.execute('SELECT COUNT(*) FROM ifood_homologation_discovery').fetchone()[0], 0)

    def test_discovery_requires_literal_positive_test_marker_without_partial_results(self):
        self.configure(merchant_id='')
        for marker in (True, False, 'test', 'PRODUCTION', None, '<string>'):
            with self.subTest(marker=marker):
                self.clear_discovery()
                def transport(request, **kwargs):
                    if request.full_url.endswith('/merchants/' + MERCHANT):
                        return response({'id': MERCHANT, 'test': marker})
                    return self.transport(request, **kwargs)
                with patch('server.ifood.evolution.open_url', side_effect=transport):
                    result = self.discover().json()
                self.assertEqual(result['state'], 'test_proof_missing')
                self.assertEqual(result['merchants'], [])

    def test_discovery_discards_prior_test_ids_if_another_shop_is_not_test(self):
        self.configure(merchant_id='')
        other = '33333333-3333-4333-8333-333333333333'
        def transport(request, **kwargs):
            if request.full_url.endswith('/merchant/v1.0/merchants'):
                return response([{'id': MERCHANT}, {'id': other}])
            if request.full_url.endswith('/merchants/' + other):
                return response({'id': other, 'test': 'PRODUCTION'})
            return self.transport(request, **kwargs)
        with patch('server.ifood.evolution.open_url', side_effect=transport):
            result = self.discover().json()
        self.assertEqual(result['state'], 'test_proof_missing')
        self.assertEqual(result['merchants'], [])

    def test_discovery_rejects_bad_id_mismatch_duplicates_and_empty_lists(self):
        self.configure(merchant_id='')
        variants = [([{'id': '../outside'}], None),
                    ([{'id': MERCHANT}, {'id': MERCHANT.upper()}], None),
                    ([], None), ([{'id': MERCHANT}], {'id': profile.CLIENT_ID, 'test': 'TEST'})]
        for merchants, merchant in variants:
            with self.subTest(count=len(merchants), detail_present=merchant is not None):
                self.clear_discovery()
                def transport(request, **kwargs):
                    if request.full_url.endswith('/merchant/v1.0/merchants'):
                        return response(merchants)
                    if merchant and request.full_url.endswith('/merchants/' + MERCHANT):
                        return response(merchant)
                    return self.transport(request, **kwargs)
                with patch('server.ifood.evolution.open_url', side_effect=transport):
                    result = self.discover().json()
                self.assertIn(result['state'], ('response', 'test_proof_missing'))
                self.assertEqual(result['merchants'], [])

    def test_discovery_paginates_before_verifying_each_linked_merchant(self):
        self.configure(merchant_id='')
        identifiers = [str(UUID(int=index + 1)) for index in range(101)]
        paths = []
        def transport(request, **kwargs):
            path = request.full_url.removeprefix(ifood.HOST)
            paths.append(path)
            if path == '/authentication/v1.0/oauth/token':
                return self.transport(request, **kwargs)
            if path == '/merchant/v1.0/merchants':
                return response([{'id': value} for value in identifiers[:100]])
            if path == '/merchant/v1.0/merchants?page=2&size=100':
                return response([{'id': identifiers[-1]}])
            merchant_id = path.rsplit('/', 1)[-1]
            self.assertIn(merchant_id, identifiers)
            return response({'id': merchant_id, 'test': 'TEST'})
        with patch('server.ifood.evolution.open_url', side_effect=transport):
            result = self.discover().json()
        self.assertEqual(result['state'], 'queried')
        self.assertEqual([item['merchant_id'] for item in result['merchants']], identifiers)
        self.assertEqual(len(paths), 104)
        self.assertEqual(paths[2], '/merchant/v1.0/merchants?page=2&size=100')

    def test_discovery_full_last_page_fails_instead_of_returning_truncated_results(self):
        self.configure(merchant_id='')
        pages = [[{'id': str(UUID(int=page * 100 + index + 1))} for index in range(100)]
                 for page in range(profile.MAX_PAGES)]
        calls = []
        def transport(request, **kwargs):
            if request.full_url.endswith('/authentication/v1.0/oauth/token'):
                return self.transport(request, **kwargs)
            calls.append(request.full_url)
            self.assertIn('/merchant/v1.0/merchants', request.full_url)
            return response(pages[len(calls) - 1])
        with patch('server.ifood.evolution.open_url', side_effect=transport):
            result = self.discover().json()
        self.assertEqual(result['state'], 'response')
        self.assertEqual(result['merchants'], [])
        self.assertEqual(len(calls), profile.MAX_PAGES)

    def test_configuration_change_after_oauth_prevents_merchant_calls(self):
        self.configure(merchant_id='')
        def transport(request, **kwargs):
            result = self.transport(request, **kwargs)
            os.environ[profile.REQUIRED[1]] = 'different-renewed-secret'
            return result
        with patch('server.ifood.evolution.open_url', side_effect=transport) as call:
            result = self.discover().json()
        self.assertEqual(call.call_count, 1)
        self.assertEqual(result['state'], 'configuration_changed')
        self.assertEqual(result['merchants'], [])

    def test_configuration_change_after_detail_discards_result(self):
        self.configure(merchant_id='')
        def transport(request, **kwargs):
            result = self.transport(request, **kwargs)
            if request.full_url.endswith('/merchants/' + MERCHANT):
                os.environ[profile.REQUIRED[2]] = MERCHANT
            return result
        with patch('server.ifood.evolution.open_url', side_effect=transport):
            result = self.discover().json()
        self.assertEqual(result['state'], 'configuration_changed')
        self.assertEqual(result['merchants'], [])

    def test_expired_discovery_budget_prevents_further_provider_calls(self):
        self.configure(merchant_id='')
        with patch.object(profile, 'DISCOVERY_SECONDS', 0), \
                patch('server.ifood.evolution.open_url', side_effect=self.transport) as call:
            result = self.discover().json()
        self.assertEqual(call.call_count, 1)
        self.assertEqual(result['state'], 'network')
        self.assertEqual(result['merchants'], [])

    def test_persistent_lease_prevents_concurrent_oauth_and_cooldown_prevents_repetition(self):
        self.configure(merchant_id='')
        entered, release = Event(), Event()
        def transport(request, **kwargs):
            if request.full_url.endswith('/authentication/v1.0/oauth/token'):
                entered.set()
                self.assertTrue(release.wait(5))
            return self.transport(request, **kwargs)
        with patch('server.ifood.evolution.open_url', side_effect=transport) as call:
            with ThreadPoolExecutor(max_workers=1) as pool:
                first = pool.submit(self.discover)
                self.assertTrue(entered.wait(5))
                try:
                    self.assertEqual(self.discover().json()['state'], 'wait')
                    self.assertEqual(call.call_count, 1)
                finally:
                    release.set()
                self.assertEqual(first.result().json()['state'], 'queried')
            self.assertEqual(self.discover().json()['state'], 'wait')
            self.assertEqual(call.call_count, 3)

    def test_provider_errors_and_oversized_bodies_never_return_private_data(self):
        self.configure(merchant_id='')
        for mode in ('http', 'network', 'oversized', 'status', 'nested'):
            with self.subTest(mode=mode):
                self.clear_discovery()
                def transport(request, **kwargs):
                    if request.full_url.endswith('/authentication/v1.0/oauth/token'):
                        return self.transport(request, **kwargs)
                    if mode == 'http':
                        raise HTTPError(request.full_url, 403, SECRET, {}, BytesIO(TOKEN.encode()))
                    if mode == 'network':
                        raise URLError(SECRET + TOKEN)
                    if mode == 'oversized':
                        return response([{'id': MERCHANT, 'private': SECRET * 20000}])
                    if mode == 'status':
                        return response([{'id': MERCHANT}], status=202)
                    return BytesIO(('[' * 2000 + 'null' + ']' * 2000).encode())
                with patch('server.ifood.evolution.open_url', side_effect=transport):
                    result = self.discover()
                self.assertIn(result.json()['state'], ('permissions', 'network', 'response'))
                self.assertEqual(result.json()['merchants'], [])
                self.assertNotIn(SECRET, result.text)
                self.assertNotIn(TOKEN, result.text)


if __name__ == '__main__':
    unittest.main()
