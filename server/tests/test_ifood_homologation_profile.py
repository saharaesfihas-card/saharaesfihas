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
from types import SimpleNamespace

from fastapi.testclient import TestClient

from server import ifood, ifood_homologation_profile as profile, ifood_stores, ifood_test_api
from server.core import hash_password
from server.main import create_app

MERCHANT = profile.TEST_MERCHANT_ID
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

    def check(self):
        return self.client.post(PATH + '/check', json={}, headers=self.headers)

    def clear_check(self):
        with sqlite3.connect(self.app.state.db_path) as conn:
            conn.execute('DELETE FROM ifood_homologation_check')

    def verify_legacy(self):
        with sqlite3.connect(self.app.state.db_path) as conn:
            conn.execute('INSERT OR REPLACE INTO ifood_connection_check VALUES(1,?,?,?)',
                (ifood.fingerprint(ifood.config()), 10,
                 json.dumps({'state': 'verified', 'merchant_id': ifood_stores.TEST_MERCHANT_ID})))

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

    def test_check_verifies_current_new_profile_without_touching_legacy_tables(self):
        self.configure()
        self.verify_legacy()
        legacy = ifood.config()
        with sqlite3.connect(self.app.state.db_path) as conn:
            tables = [row[0] for row in conn.execute("SELECT name FROM sqlite_master WHERE type='table' AND name LIKE 'ifood_%' AND name <> 'ifood_homologation_check'")]
            before = {name: conn.execute('SELECT * FROM "' + name + '"').fetchall() for name in tables}
        with patch('server.ifood.evolution.open_url', side_effect=self.transport) as call:
            result = self.check()
        self.assertEqual(result.status_code, 200)
        value = result.json()
        self.assertEqual(value['state'], 'verified')
        self.assertTrue(value['connection_verified'])
        self.assertTrue(value['operations_available'])
        self.assertTrue(value['test_merchant_confirmed'])
        self.assertEqual(value['configured_merchant_id'], MERCHANT)
        self.assertEqual(value['client_id'], profile.CLIENT_ID)
        self.assertEqual(value['profile_mode'], 'homologation')
        self.assertEqual(call.call_count, 3)
        with patch('server.ifood.evolution.open_url') as call:
            status = self.client.get(PATH).json()
        call.assert_not_called()
        self.assertEqual(status['state'], 'verified')
        self.assertTrue(status['check_available'])
        self.assertTrue(status['selected_for_manual_scenarios'])
        with sqlite3.connect(self.app.state.db_path) as conn:
            after = {name: conn.execute('SELECT * FROM "' + name + '"').fetchall() for name in tables}
            proof = conn.execute('SELECT * FROM ifood_homologation_check').fetchone()
        self.assertEqual(before, after)
        self.assertEqual(ifood.config(), legacy)
        for value in (SECRET, TOKEN, 'private-shop-name', 'private-address'):
            self.assertNotIn(value, result.text)
            self.assertNotIn(value, json.dumps(proof))
        for path in ('/api/admin/ifood/merchant', '/api/admin/ifood/merchant/actions',
                     '/api/admin/ifood/test/catalog'):
            data = self.client.get(path).json()
            self.assertEqual(data['state'], 'available')
            self.assertEqual(data['configured_merchant_id'], MERCHANT)
            self.assertEqual(data['profile_mode'], 'homologation')

    def test_discovery_does_not_activate_even_when_exact_test_shop_is_configured(self):
        self.configure()
        with patch('server.ifood.evolution.open_url', side_effect=self.transport):
            self.assertEqual(self.discover().json()['state'], 'queried')
        status = self.client.get(PATH).json()
        self.assertFalse(status['operations_available'])
        self.assertFalse(status['connection_verified'])
        self.assertTrue(status['check_available'])
        self.assertEqual(self.client.get('/api/admin/ifood/merchant/actions').json()['state'], 'not_ready')

    def test_check_requires_admin_csrf_origin_and_strict_empty_body(self):
        self.configure()
        with patch('server.ifood.evolution.open_url') as call:
            anonymous = TestClient(self.app)
            try:
                self.assertEqual(anonymous.post(PATH + '/check', json={}).status_code, 401)
            finally:
                anonymous.close()
            self.assertEqual(self.client.post(PATH + '/check', json={}).status_code, 403)
            self.assertEqual(self.client.post(PATH + '/check', json={}, headers={
                **self.headers, 'Origin': 'https://untrusted.invalid'}).status_code, 403)
            for body in ({'merchant_id': MERCHANT}, {'clientSecret': SECRET}, None, []):
                self.assertEqual(self.client.post(PATH + '/check', json=body,
                                                  headers=self.headers).status_code, 422)
        call.assert_not_called()

    def test_check_rejects_real_or_unobserved_merchant_and_wrong_app_before_oauth(self):
        for client_id, merchant_id, environment in (
                (profile.CLIENT_ID, 'bba9d647-16da-4586-b9e3-39e38633c01c', 'test'),
                (profile.CLIENT_ID, ifood_stores.TEST_MERCHANT_ID, 'test'),
                (profile.CLIENT_ID, '22222222-2222-4222-8222-222222222222', 'test'),
                (profile.CLIENT_ID, MERCHANT, 'production'),
                (ifood.config()[ifood.REQUIRED[0]], MERCHANT, 'test')):
            self.configure(client_id=client_id, merchant_id=merchant_id)
            os.environ[profile.ENVIRONMENT] = environment
            with patch('server.ifood.evolution.open_url') as call:
                result = self.check().json()
            call.assert_not_called()
            self.assertFalse(result['operations_available'])
            self.assertFalse(self.client.get(PATH).json()['check_available'])

    def test_check_requires_current_membership_and_literal_test_marker(self):
        self.configure()
        other = '33333333-3333-4333-8333-333333333333'
        for mode in ('missing', 'production', 'boolean', 'lowercase', 'mismatch'):
            self.clear_check()
            def transport(request, **kwargs):
                if mode == 'missing':
                    if request.full_url.endswith('/merchant/v1.0/merchants'):
                        return response([{'id': other}])
                    if request.full_url.endswith('/merchants/' + other):
                        return response({'id': other, 'test': 'TEST'})
                if request.full_url.endswith('/merchants/' + MERCHANT):
                    return response({'id': other if mode == 'mismatch' else MERCHANT,
                        'test': {'production': 'PRODUCTION', 'boolean': True,
                                 'lowercase': 'test'}.get(mode, 'TEST')})
                return self.transport(request, **kwargs)
            with patch('server.ifood.evolution.open_url', side_effect=transport):
                result = self.check().json()
            self.assertNotEqual(result['state'], 'verified')
            self.assertFalse(result['operations_available'])
            self.assertFalse(self.client.get(PATH).json()['connection_verified'])

    def test_check_lease_cooldown_and_secret_change_never_release_stale_proof(self):
        self.configure()
        entered, release = Event(), Event()
        def transport(request, **kwargs):
            if request.full_url.endswith('/authentication/v1.0/oauth/token'):
                entered.set()
                self.assertTrue(release.wait(5))
            return self.transport(request, **kwargs)
        with patch('server.ifood.evolution.open_url', side_effect=transport) as call:
            with ThreadPoolExecutor(max_workers=1) as pool:
                first = pool.submit(self.check)
                self.assertTrue(entered.wait(5))
                try:
                    self.assertEqual(self.check().json()['state'], 'wait')
                    self.assertFalse(self.client.get(PATH).json()['operations_available'])
                    self.assertEqual(call.call_count, 1)
                finally:
                    release.set()
                self.assertEqual(first.result().json()['state'], 'verified')
            self.assertEqual(self.check().json()['state'], 'wait')
            self.assertEqual(call.call_count, 3)
        os.environ[profile.REQUIRED[1]] = SECRET + '-renewed-again'
        status = self.client.get(PATH).json()
        self.assertFalse(status['operations_available'])
        self.assertIsNone(status['last_result'])
        self.assertEqual(self.client.get('/api/admin/ifood/test/catalog').json()['resources'], [])

    def test_check_discards_configuration_changed_after_oauth_or_detail(self):
        self.configure()
        for phase in ('oauth', 'detail'):
            self.clear_check()
            os.environ[profile.REQUIRED[1]] = SECRET
            def transport(request, **kwargs):
                result = self.transport(request, **kwargs)
                if request.full_url.endswith('/authentication/v1.0/oauth/token') and phase == 'oauth' or (
                        request.full_url.endswith('/merchants/' + MERCHANT) and phase == 'detail'):
                    os.environ[profile.REQUIRED[1]] = SECRET + '-changed'
                return result
            with patch('server.ifood.evolution.open_url', side_effect=transport) as call:
                result = self.check().json()
            self.assertEqual(result['state'], 'configuration_changed')
            self.assertFalse(result['operations_available'])
            self.assertEqual(call.call_count, 1 if phase == 'oauth' else 3)
            self.assertFalse(self.client.get(PATH).json()['connection_verified'])

    def test_manual_profile_switches_and_legacy_order_session_stays_bound(self):
        # No HOMO setting: manual workflows use the existing verified test profile.
        for name in (*profile.REQUIRED, profile.ENVIRONMENT):
            os.environ.pop(name, None)
        self.verify_legacy()
        request = SimpleNamespace(app=self.app)
        manual_before = ifood_test_api.Session(request)
        order_before = ifood_test_api.Session(request, legacy=True)
        self.configure()
        with self.assertRaises(ifood.ProviderError) as caught:
            manual_before.unchanged()
        self.assertEqual(caught.exception.state, 'configuration_changed')
        order_before.unchanged()
        with self.assertRaises(ifood.ProviderError):
            ifood_test_api.Session(request)
        with patch('server.ifood.evolution.open_url', side_effect=self.transport):
            self.assertEqual(self.check().json()['state'], 'verified')
        manual = ifood_test_api.Session(request)
        order = ifood_test_api.Session(request, legacy=True)
        self.assertEqual(manual.profile_mode, 'homologation')
        self.assertEqual(manual.merchant_id, MERCHANT)
        self.assertEqual(manual.client_id, profile.CLIENT_ID)
        self.assertEqual(order.profile_mode, 'legacy')
        self.assertEqual(order.merchant_id, ifood_stores.TEST_MERCHANT_ID)
        self.assertNotEqual(manual.scope, order.scope)
        self.assertFalse(manual._allowed('GET', '/merchant/v1.0/merchants/' + order.merchant_id))
        self.assertFalse(order._allowed('GET', '/catalog/v2.0/merchants/' + MERCHANT + '/catalogs'))
        oldscope = manual.scope
        os.environ[profile.REQUIRED[1]] = SECRET + '-rotated'
        with self.assertRaises(ifood.ProviderError):
            manual.unchanged()
        self.assertEqual(ifood_test_api.scope(profile.selected_config()), oldscope)
        order.unchanged()

    def test_partial_homo_environment_cannot_fall_back_to_verified_legacy_manual_workflows(self):
        self.verify_legacy()
        for setting in (*profile.REQUIRED, profile.ENVIRONMENT):
            with patch.dict(os.environ, {**dict.fromkeys(profile.REQUIRED, ''), profile.ENVIRONMENT: ''}):
                os.environ[setting] = 'test' if setting == profile.ENVIRONMENT else SECRET
                with patch('server.ifood.evolution.open_url') as call:
                    for path in ('/api/admin/ifood/merchant', '/api/admin/ifood/merchant/actions',
                                 '/api/admin/ifood/test/catalog'):
                        result = self.client.get(path).json()
                        self.assertEqual(result['state'], 'not_ready')
                        self.assertEqual(result['profile_mode'], 'homologation')
                call.assert_not_called()
                self.assertEqual(ifood_test_api.Session(SimpleNamespace(app=self.app), legacy=True).profile_mode, 'legacy')

    def test_empty_homo_settings_still_select_blocked_new_profile(self):
        self.verify_legacy()
        for value in ('', '   '):
            with patch.dict(os.environ, {**dict.fromkeys(profile.REQUIRED, value), profile.ENVIRONMENT: value}):
                status = self.client.get('/api/admin/ifood/merchant/actions').json()
                self.assertEqual(status['profile_mode'], 'homologation')
                self.assertFalse(status['writes_available'])
                self.assertEqual(status['state'], 'not_ready')
                self.assertEqual(ifood_test_api.Session(SimpleNamespace(app=self.app), legacy=True).profile_mode, 'legacy')

    def test_empty_homo_setting_appearing_invalidates_manual_fallback_only(self):
        for name in (*profile.REQUIRED, profile.ENVIRONMENT):
            os.environ.pop(name, None)
        self.verify_legacy()
        request = SimpleNamespace(app=self.app)
        manual = ifood_test_api.Session(request)
        order = ifood_test_api.Session(request, legacy=True)
        os.environ[profile.REQUIRED[1]] = ''
        with self.assertRaises(ifood.ProviderError) as caught:
            manual.unchanged()
        self.assertEqual(caught.exception.state, 'configuration_changed')
        order.unchanged()

    def test_actual_manual_writes_and_registry_use_separate_new_app_shop_scope(self):
        for name in (*profile.REQUIRED, profile.ENVIRONMENT):
            os.environ.pop(name, None)
        self.verify_legacy()
        legacy = ifood.config()
        legacy_merchant = ifood_stores.TEST_MERCHANT_ID
        catalog = '44444444-4444-4444-8444-444444444444'
        body = {'idempotency_key': '55555555-5555-4555-8555-555555555555',
                'description': 'Cenário fictício', 'start': '2030-01-01T12:00:00Z',
                'end': '2030-01-01T12:01:00Z'}
        writes = []
        oauth_clients = []
        def business_transport(request, **kwargs):
            path = request.full_url.removeprefix(ifood.HOST)
            if path == '/authentication/v1.0/oauth/token':
                credentials = parse_qs(request.data.decode())
                oauth_clients.append(credentials['clientId'][0])
                expected_secret = SECRET if credentials['clientId'][0] == profile.CLIENT_ID else legacy[ifood.REQUIRED[1]]
                self.assertEqual(credentials['clientSecret'], [expected_secret])
                return response({'accessToken': TOKEN})
            if path.endswith('/interruptions'):
                self.assertEqual(request.get_method(), 'POST')
                self.assertIn(path, ('/merchant/v1.0/merchants/' + legacy_merchant + '/interruptions',
                                     '/merchant/v1.0/merchants/' + MERCHANT + '/interruptions'))
                payload = json.loads(request.data)
                writes.append(path)
                return response(payload, status=201)
            self.assertEqual(request.get_method(), 'GET')
            self.assertIn(path, ('/catalog/v2.0/merchants/' + legacy_merchant + '/catalogs',
                                 '/catalog/v2.0/merchants/' + MERCHANT + '/catalogs'))
            return response([{'catalogId': catalog, 'status': 'OBSERVED'}])
        with sqlite3.connect(self.app.state.db_path) as conn:
            before = {table: conn.execute('SELECT * FROM ' + table).fetchall()
                      for table in ('products', 'orders', 'customers', 'ifood_connection_check', 'ifood_imported_orders')}
        with patch('server.ifood.evolution.open_url', side_effect=business_transport):
            old = self.client.post('/api/admin/ifood/merchant/interruptions', json=body, headers=self.headers).json()
            self.assertEqual(old['state'], 'confirmed')
            self.assertEqual(old['profile_mode'], 'legacy')
            self.assertEqual(self.client.post('/api/admin/ifood/test/catalog/discover', json={}, headers=self.headers).json()['state'], 'queried')
        self.configure()
        with patch('server.ifood.evolution.open_url', side_effect=self.transport):
            self.assertEqual(self.check().json()['state'], 'verified')
        self.assertEqual(self.client.get('/api/admin/ifood/merchant/actions').json()['operations'], [])
        with patch('server.ifood.evolution.open_url', side_effect=business_transport):
            new = self.client.post('/api/admin/ifood/merchant/interruptions', json=body, headers=self.headers).json()
            discovered = self.client.post('/api/admin/ifood/test/catalog/discover', json={}, headers=self.headers).json()
        self.assertEqual(new['state'], 'confirmed')
        self.assertEqual(new['profile_mode'], 'homologation')
        self.assertEqual(new['configured_merchant_id'], MERCHANT)
        self.assertNotEqual(old['operation']['id'], new['operation']['id'])
        self.assertEqual(discovered['configured_merchant_id'], MERCHANT)
        self.assertEqual(writes, ['/merchant/v1.0/merchants/' + legacy_merchant + '/interruptions',
                                  '/merchant/v1.0/merchants/' + MERCHANT + '/interruptions'])
        self.assertEqual(oauth_clients, [legacy[ifood.REQUIRED[0]], legacy[ifood.REQUIRED[0]],
                                         profile.CLIENT_ID, profile.CLIENT_ID])
        with patch('server.ifood.evolution.open_url') as call:
            rejected = self.client.post('/api/admin/ifood/merchant/interruptions/' + old['operation']['id'] + '/remove',
                json={'idempotency_key': '66666666-6666-4666-8666-666666666666'}, headers=self.headers).json()
        call.assert_not_called()
        self.assertEqual(rejected['state'], 'not_found')
        with sqlite3.connect(self.app.state.db_path) as conn:
            after = {table: conn.execute('SELECT * FROM ' + table).fetchall() for table in before}
            namespaces = conn.execute('SELECT DISTINCT fingerprint FROM ifood_test_catalog_registry').fetchall()
        self.assertEqual(before, after)
        self.assertEqual(len(namespaces), 2)


if __name__ == '__main__':
    unittest.main()
