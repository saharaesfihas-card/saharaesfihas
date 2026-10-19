"""Contratos oficiais de comandos Order; somente transporte e dados fictícios."""
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

from server import ifood, ifood_order_actions as actions, ifood_stores, ifood_homologation_profile as profile
from server.core import hash_password
from server.main import create_app

MERCHANT = ifood_stores.TEST_MERCHANT_ID
CLIENT = '11111111-1111-4111-8111-111111111111'
ORDER = '33333333-3333-4333-8333-333333333333'
LOCAL = 'aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa'
OTHER = '22222222-2222-4222-8222-222222222222'
SECRET = 'fictional-order-action-secret-only'
TOKEN = 'fictional-order-action-access-token-only'
PATH = '/api/admin/ifood/orders/' + LOCAL
REMOTE = '/order/v1.0/orders/' + ORDER


def response(data=None, status=200):
    result = BytesIO(b'' if data is None else json.dumps(data).encode())
    result.status = status
    return result


class IFoodOrderActionTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.password = 'disposable-order-action-password'
        cls.password_hash = hash_password(cls.password)

    def setUp(self):
        self.directory = TemporaryDirectory()
        self.env = patch.dict(os.environ, {
            'SAHARA_IFOOD_CLIENT_ID': CLIENT, 'SAHARA_IFOOD_CLIENT_SECRET': SECRET,
            'SAHARA_IFOOD_MERCHANT_ID': MERCHANT, 'SAHARA_IFOOD_ENABLED': '1',
            'SAHARA_IFOOD_ENVIRONMENT': 'test', 'SAHARA_IFOOD_IMPORT_ENABLED': '0',
            'SAHARA_WHATSAPP_ENABLED': '0', 'SAHARA_AI_ENABLED': '0'})
        self.env.start()
        self.app = create_app(data_dir=Path(self.directory.name), admin_password_hash=self.password_hash, secure_cookie=False)
        # O módulo pode estar sendo integrado ao main em paralelo; anteceder o fallback de arquivos.
        self.app.router.routes[0:0] = actions.router.routes
        with sqlite3.connect(self.app.state.db_path) as conn:
            actions.initialize(conn)
            conn.execute('INSERT OR REPLACE INTO ifood_connection_check VALUES(1,?,?,?)',
                (ifood.fingerprint(ifood.config()), 10, json.dumps({'state': 'verified', 'merchant_id': MERCHANT})))
            conn.execute('INSERT INTO orders '
                '(id,created_at,delivery_json,items_json,subtotal_cents,total_cents,tracking_token,idempotency_key,request_hash,source,status,payment_status) '
                'VALUES(?,?,?,?,?,?,?,?,?,?,?,?)', (LOCAL, '2026-10-15T12:00:00Z',
                json.dumps({'ifood': {'environment': 'test', 'order_id': ORDER}}), '[]', 2000, 2000,
                'private-fake-tracking', 'fake-local-idempotency', 'fake-order-hash', 'ifood', 'ifood_received', 'external_paid'))
            conn.execute('INSERT INTO ifood_imported_orders VALUES(?,?,?)', (MERCHANT, ORDER, LOCAL))
        self.client = TestClient(self.app)
        login = self.client.post('/api/admin/login', json={'password': self.password}).json()
        self.headers = {'X-Sahara-CSRF': login['csrf_token']}
        self.calls = []
        self.details = {'id': ORDER, 'merchant': {'id': MERCHANT}, 'orderType': 'DELIVERY'}
        self.reasons = [{'description': 'Motivo fictício', 'cancelCodeId': 'TEST-CANCEL'}]
        self.command_status = 202

    def tearDown(self):
        self.client.close()
        self.env.stop()
        self.directory.cleanup()

    def transport(self, request, **kwargs):
        self.calls.append(request)
        path = request.full_url.removeprefix(ifood.HOST)
        if path == '/authentication/v1.0/oauth/token':
            return response({'accessToken': TOKEN})
        if path == REMOTE:
            self.assertEqual(request.get_method(), 'GET')
            return response(self.details)
        if path == REMOTE + '/cancellationReasons':
            return response(self.reasons, 204 if self.reasons is None else 200)
        if path in [REMOTE + '/' + action for action in ('confirm', 'startPreparation', 'readyToPickup', 'dispatch', 'requestCancellation')]:
            self.assertEqual(request.get_method(), 'POST')
            return response(status=self.command_status)
        self.fail('Unexpected provider destination: ' + path)

    def post(self, action='confirm', key='test-key-0001', **fields):
        return self.client.post(PATH + '/actions', json={'action': action, 'idempotency_key': key, **fields}, headers=self.headers)

    def snapshot(self):
        with sqlite3.connect(self.app.state.db_path) as conn:
            tables = [row[0] for row in conn.execute("SELECT name FROM sqlite_master WHERE type='table' AND name<>'ifood_order_action_attempts'")]
            return {name: conn.execute('SELECT * FROM "' + name + '" ORDER BY rowid').fetchall() for name in tables}

    def test_manual_commands_exact_url_no_body_202_remains_pending_no_business_side_effects(self):
        before = self.snapshot()
        for name in ('confirm', 'startPreparation', 'readyToPickup', 'dispatch'):
            self.calls.clear()
            with patch('server.ifood.evolution.open_url', side_effect=self.transport):
                answer = self.post(name, 'command-key-' + name)
            self.assertEqual(answer.status_code, 200)
            self.assertEqual(answer.json()['state'], 'accepted_pending')
            self.assertEqual(answer.json()['http_status'], 202)
            self.assertFalse(answer.json()['local_status_changed'])
            self.assertEqual([(call.get_method(), call.full_url) for call in self.calls], [
                ('POST', ifood.HOST + '/authentication/v1.0/oauth/token'),
                ('GET', ifood.HOST + REMOTE), ('POST', ifood.HOST + REMOTE + '/' + name)])
            self.assertIsNone(self.calls[-1].data)
            self.assertNotIn(SECRET, answer.text)
            self.assertNotIn(TOKEN, answer.text)
        self.assertEqual(self.snapshot(), before)
        self.assertTrue(self.client.get(PATH + '/actions').json()['actions_available'])

    def test_details_wrong_order_merchant_or_type_prevent_post(self):
        for attribute, value in [('id', OTHER), ('merchant', {'id': OTHER}), ('orderType', 'TAKEOUT')]:
            with self.subTest(attribute=attribute):
                self.details = {'id': ORDER, 'merchant': {'id': MERCHANT}, 'orderType': 'DELIVERY', attribute: value}
                self.calls.clear()
                with patch('server.ifood.evolution.open_url', side_effect=self.transport):
                    result = self.post(key='wrong-details-' + attribute).json()
                self.assertEqual(result['state'], 'refused')
                self.assertEqual(len(self.calls), 2)

    def test_idempotency_duplicate_and_same_action_new_key_never_repeat_remote_command(self):
        with patch('server.ifood.evolution.open_url', side_effect=self.transport):
            first = self.post().json()
        with patch('server.ifood.evolution.open_url') as call:
            self.assertEqual(self.post().json(), first)
            repeated = self.post(key='another-key-0002').json()
            self.assertEqual(repeated['state'], 'already_requested')
            self.assertEqual(repeated['attempt_id'], first['attempt_id'])
            self.assertEqual(self.post(action='dispatch').status_code, 409)
        call.assert_not_called()

    def test_cancellation_uses_fresh_allowed_reason_exact_payload_and_does_not_cancel_locally(self):
        before = self.snapshot()
        with patch('server.ifood.evolution.open_url', side_effect=self.transport):
            result = self.post(action='requestCancellation', cancellation_code='TEST-CANCEL', reason='  Justificativa fictícia  ').json()
        self.assertEqual(result['state'], 'accepted_pending')
        self.assertEqual([call.full_url for call in self.calls], [ifood.HOST + '/authentication/v1.0/oauth/token',
            ifood.HOST + REMOTE, ifood.HOST + REMOTE + '/cancellationReasons', ifood.HOST + REMOTE + '/requestCancellation'])
        self.assertEqual(json.loads(self.calls[-1].data), {'cancellationCode': 'TEST-CANCEL', 'reason': 'Justificativa fictícia'})
        self.assertEqual(self.snapshot(), before)

    def test_unknown_or_no_current_cancellation_reasons_prevent_write(self):
        for index, reason_list in enumerate(([], None, [{'description': 'Outro motivo', 'cancelCodeId': 'OTHER'}])):
            self.reasons = reason_list
            self.calls.clear()
            with patch('server.ifood.evolution.open_url', side_effect=self.transport):
                result = self.post(action='requestCancellation', key='missing-reason-' + str(index),
                    cancellation_code='TEST-CANCEL', reason='Justificativa').json()
            self.assertEqual(result['state'], 'refused')
            self.assertEqual(result['error_state'], 'invalid_cancellation_code')
            self.assertEqual(len(self.calls), 3)

    def test_reasons_query_is_only_get_after_oauth_and_never_stores_business_data(self):
        before = self.snapshot()
        with patch('server.ifood.evolution.open_url', side_effect=self.transport):
            result = self.client.get(PATH + '/cancellation-reasons').json()
        self.assertEqual(result['reasons'], self.reasons)
        self.assertEqual([(call.get_method(), call.full_url) for call in self.calls[1:]], [
            ('GET', ifood.HOST + REMOTE), ('GET', ifood.HOST + REMOTE + '/cancellationReasons')])
        self.assertEqual(self.snapshot(), before)

    def test_no_network_for_foreign_source_missing_mapping_wrong_meta_or_wrong_merchant(self):
        changes = [("UPDATE orders SET source='web'", ()),
                   ('DELETE FROM ifood_imported_orders', ()),
                   ("UPDATE orders SET delivery_json='{}'", ()),
                   ('UPDATE ifood_imported_orders SET merchant_id=?', (OTHER,))]
        for sql, parameters in changes:
            with self.subTest(sql=sql):
                with sqlite3.connect(self.app.state.db_path) as conn:
                    conn.execute('SAVEPOINT invalid_target')
                    original = conn.execute('SELECT source,delivery_json FROM orders WHERE id=?', (LOCAL,)).fetchone()
                    conn.execute(sql, parameters)
                with patch('server.ifood.evolution.open_url') as call:
                    self.assertEqual(self.post().status_code, 409)
                call.assert_not_called()
                with sqlite3.connect(self.app.state.db_path) as conn:
                    conn.execute('UPDATE orders SET source=?,delivery_json=? WHERE id=?', (*original, LOCAL))
                    conn.execute('DELETE FROM ifood_imported_orders')
                    conn.execute('INSERT INTO ifood_imported_orders VALUES(?,?,?)', (MERCHANT, ORDER, LOCAL))

    def test_production_unverified_secret_rotation_and_unallowlisted_configuration_never_call(self):
        for values in ({'SAHARA_IFOOD_ENVIRONMENT': 'production'}, {'SAHARA_IFOOD_ENABLED': '0'},
                       {'SAHARA_IFOOD_CLIENT_SECRET': SECRET + '-rotated'}, {'SAHARA_IFOOD_MERCHANT_ID': OTHER}):
            with self.subTest(keys=list(values)), patch.dict(os.environ, values), patch('server.ifood.evolution.open_url') as call:
                result = self.post().json()
                self.assertNotEqual(result['state'], 'accepted_pending')
                self.assertFalse(self.client.get(PATH + '/actions').json()['actions_available'])
                call.assert_not_called()
        with sqlite3.connect(self.app.state.db_path) as conn:
            conn.execute("UPDATE ifood_connection_check SET result_json='{}'")
        with patch('server.ifood.evolution.open_url') as call:
            self.assertNotEqual(self.post().json()['state'], 'accepted_pending')
        call.assert_not_called()

    def test_concurrent_command_and_other_action_are_reserved_before_any_provider_write(self):
        entered, release = Event(), Event()
        def delayed(request, **kwargs):
            result = self.transport(request, **kwargs)
            if request.full_url == ifood.HOST + REMOTE:
                entered.set()
                release.wait(5)
            return result
        with patch('server.ifood.evolution.open_url', side_effect=delayed), ThreadPoolExecutor(2) as pool:
            running = pool.submit(self.post)
            self.assertTrue(entered.wait(5))
            try:
                self.assertEqual(self.post(action='dispatch', key='parallel-other-key').json()['state'], 'in_progress')
                self.assertEqual(self.post().json()['state'], 'in_progress')
            finally:
                release.set()
            self.assertEqual(running.result().json()['state'], 'accepted_pending')
        self.assertEqual(sum(call.get_method() == 'POST' and call.full_url.endswith('/confirm') for call in self.calls), 1)

    def test_uncertain_write_blocks_all_new_keys_and_does_not_retry_same_key(self):
        for failure in (URLError('private ' + SECRET + TOKEN), HTTPError(ifood.HOST + REMOTE, 500, SECRET, {}, BytesIO(TOKEN.encode()))):
            with self.subTest(kind=type(failure).__name__):
                with sqlite3.connect(self.app.state.db_path) as conn:
                    conn.execute('DELETE FROM ifood_order_action_attempts')
                def failed(request, **kwargs):
                    if request.full_url.endswith('/confirm'):
                        raise failure
                    return self.transport(request, **kwargs)
                with patch('server.ifood.evolution.open_url', side_effect=failed):
                    answer = self.post()
                self.assertEqual(answer.json()['state'], 'uncertain')
                self.assertNotIn(SECRET, answer.text)
                self.assertNotIn(TOKEN, answer.text)
                with patch('server.ifood.evolution.open_url') as call:
                    self.assertEqual(self.post().json()['state'], 'uncertain')
                    self.assertEqual(self.post('dispatch', key='blocked-new-key').json()['state'], 'uncertain')
                    self.assertFalse(self.client.get(PATH + '/actions').json()['actions_available'])
                call.assert_not_called()

    def test_only_exact_202_is_accepted_and_invalid_write_success_is_uncertain(self):
        for status in (200, 201, 204):
            with self.subTest(status=status):
                with sqlite3.connect(self.app.state.db_path) as conn:
                    conn.execute('DELETE FROM ifood_order_action_attempts')
                self.command_status = status
                with patch('server.ifood.evolution.open_url', side_effect=self.transport):
                    self.assertEqual(self.post().json()['state'], 'uncertain')

    def test_interrupted_lease_expires_to_uncertain_without_retry(self):
        entered, release = Event(), Event()
        def delayed(request, **kwargs):
            result = self.transport(request, **kwargs)
            if request.full_url == ifood.HOST + REMOTE:
                entered.set()
                release.wait(5)
            return result
        with patch('server.ifood.evolution.open_url', side_effect=delayed), ThreadPoolExecutor(2) as pool:
            running = pool.submit(self.post)
            self.assertTrue(entered.wait(5))
            try:
                with sqlite3.connect(self.app.state.db_path) as conn:
                    conn.execute('UPDATE ifood_order_action_attempts SET started_at=0')
                self.assertEqual(self.post('dispatch', key='stale-lease-key').json()['state'], 'uncertain')
            finally:
                release.set()
            self.assertEqual(running.result().json()['state'], 'uncertain')
        self.assertFalse(any(call.full_url.endswith('/confirm') for call in self.calls))

    def test_readiness_is_local_and_requires_authentication_csrf_origin_and_strict_input(self):
        with patch('server.ifood.evolution.open_url') as call:
            self.assertTrue(self.client.get(PATH + '/actions').json()['actions_available'])
            anonymous = TestClient(self.app)
            try:
                self.assertEqual(anonymous.get(PATH + '/actions').status_code, 401)
                self.assertEqual(anonymous.get(PATH + '/cancellation-reasons').status_code, 401)
                self.assertEqual(anonymous.post(PATH + '/actions', json={}).status_code, 401)
            finally:
                anonymous.close()
            body = {'action': 'confirm', 'idempotency_key': 'strict-key-0001'}
            self.assertEqual(self.client.post(PATH + '/actions', json=body).status_code, 403)
            self.assertEqual(self.client.post(PATH + '/actions', json=body, headers={**self.headers, 'Origin': 'https://untrusted.invalid'}).status_code, 403)
            bad = [{**body, 'merchant_id': OTHER}, {**body, 'external_order_id': ORDER},
                   {**body, 'action': 'delivery'}, {**body, 'action': 'cancellationHandshakes'},
                   {**body, 'cancellation_code': 'not-for-confirm'}, {**body, 'reason': 'not-for-confirm'},
                   {**body, 'idempotency_key': '../unsafe'}, {**body, 'idempotency_key': 12345},
                   {'action': 'requestCancellation', 'idempotency_key': 'no-reason-key'}, [], None]
            for value in bad:
                with self.subTest(body=value):
                    self.assertEqual(self.client.post(PATH + '/actions', json=value, headers=self.headers).status_code, 422)
        call.assert_not_called()

    def test_clear_remote_rejection_is_recorded_and_can_be_retried_with_new_key(self):
        def denied(request, **kwargs):
            if request.full_url.endswith('/confirm'):
                raise HTTPError(request.full_url, 400, SECRET, {}, BytesIO(TOKEN.encode()))
            return self.transport(request, **kwargs)
        with patch('server.ifood.evolution.open_url', side_effect=denied):
            self.assertEqual(self.post().json()['state'], 'refused')
        with patch('server.ifood.evolution.open_url', side_effect=self.transport):
            self.assertEqual(self.post(key='fixed-new-key-0002').json()['state'], 'accepted_pending')

    def test_configuration_change_after_write_records_uncertainty_and_stops(self):
        def changed(request, **kwargs):
            result = self.transport(request, **kwargs)
            if request.full_url.endswith('/confirm'):
                os.environ['SAHARA_IFOOD_CLIENT_SECRET'] = SECRET + '-changed'
            return result
        with patch('server.ifood.evolution.open_url', side_effect=changed):
            result = self.post().json()
        self.assertEqual(result['state'], 'uncertain')
        self.assertEqual(result['error_state'], 'configuration_changed')

    def test_imported_order_context_remains_legacy_while_homo_configuration_changes(self):
        before = self.snapshot()
        with patch.dict(os.environ, dict(zip(profile.REQUIRED,
                (profile.CLIENT_ID, 'fictional-new-profile-secret', profile.TEST_MERCHANT_ID)))):
            status = self.client.get(PATH + '/actions').json()
            self.assertTrue(status['actions_available'])
            self.assertEqual(status['profile_mode'], 'legacy')
            self.assertEqual(status['client_id'], CLIENT)
            self.assertEqual(status['configured_merchant_id'], MERCHANT)
            def transport(request, **kwargs):
                value = self.transport(request, **kwargs)
                if request.full_url.endswith(REMOTE):
                    os.environ[profile.REQUIRED[1]] = 'different-new-profile-secret'
                return value
            with patch('server.ifood.evolution.open_url', side_effect=transport):
                result = self.post().json()
            self.assertEqual(result['state'], 'accepted_pending')
            self.assertEqual(result['profile_mode'], 'legacy')
            self.assertEqual(result['client_id'], CLIENT)
            self.assertEqual(result['configured_merchant_id'], MERCHANT)
            self.assertEqual([request.full_url for request in self.calls], [
                ifood.HOST + '/authentication/v1.0/oauth/token',
                ifood.HOST + REMOTE, ifood.HOST + REMOTE + '/confirm'])
        self.assertEqual(before, self.snapshot())

    def test_homologation_order_commands_use_the_new_test_profile(self):
        new_secret = 'fictional-new-order-profile-secret'
        new_merchant = profile.TEST_MERCHANT_ID
        with patch.dict(os.environ, dict(zip(profile.REQUIRED,
                (profile.CLIENT_ID, new_secret, new_merchant))) | {profile.ENVIRONMENT: 'test'}):
            def new_profile_transport(request, **kwargs):
                path = request.full_url.removeprefix(ifood.HOST)
                if path == '/authentication/v1.0/oauth/token':
                    self.calls.append(request)
                    credentials = parse_qs(request.data.decode())
                    self.assertEqual(credentials['clientId'], [profile.CLIENT_ID])
                    self.assertEqual(credentials['clientSecret'], [new_secret])
                    return response({'accessToken': TOKEN})
                if path == '/merchant/v1.0/merchants':
                    self.calls.append(request)
                    return response([{'id': new_merchant}])
                if path == '/merchant/v1.0/merchants/' + new_merchant:
                    self.calls.append(request)
                    return response({'id': new_merchant, 'test': 'TEST'})
                return self.transport(request, **kwargs)

            with patch('server.ifood.evolution.open_url', side_effect=new_profile_transport):
                verified = self.client.post('/api/admin/ifood/homologation-profile/check', json={}, headers=self.headers).json()
            self.assertEqual(verified['state'], 'verified')
            metadata = {'ifood': {'environment': 'test', 'order_id': ORDER, 'profile_mode': 'homologation',
                                  'client_id': profile.CLIENT_ID, 'merchant_id': new_merchant}}
            self.details = {'id': ORDER, 'merchant': {'id': new_merchant}, 'orderType': 'DELIVERY'}
            with sqlite3.connect(self.app.state.db_path) as conn:
                conn.execute('UPDATE orders SET delivery_json=? WHERE id=?', (json.dumps(metadata), LOCAL))
                conn.execute('UPDATE ifood_imported_orders SET merchant_id=? WHERE order_id=?', (new_merchant, LOCAL))

            status = self.client.get(PATH + '/actions').json()
            self.assertTrue(status['actions_available'])
            self.assertEqual((status['profile_mode'], status['client_id'], status['configured_merchant_id']),
                ('homologation', profile.CLIENT_ID, new_merchant))

            self.calls.clear()
            with patch('server.ifood.evolution.open_url', side_effect=new_profile_transport):
                result = self.post(key='new-account-command-0001').json()
            self.assertEqual(result['state'], 'accepted_pending')
            self.assertEqual((result['profile_mode'], result['client_id'], result['configured_merchant_id']),
                ('homologation', profile.CLIENT_ID, new_merchant))
            self.assertEqual([call.full_url for call in self.calls], [ifood.HOST + '/authentication/v1.0/oauth/token',
                ifood.HOST + REMOTE, ifood.HOST + REMOTE + '/confirm'])
            self.assertNotIn(new_secret, json.dumps(result))
