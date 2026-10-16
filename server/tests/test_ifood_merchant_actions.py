"""Pausas de homologação isoladas, com transporte do iFood simulado."""
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta
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
from uuid import uuid4

from fastapi.testclient import TestClient

from server import ifood, ifood_stores
from server.core import hash_password
from server.main import create_app

CLIENT = '11111111-1111-4111-8111-111111111111'
MERCHANT = ifood_stores.TEST_MERCHANT_ID
OTHER = '22222222-2222-4222-8222-222222222222'
SECRET = 'fictional-merchant-actions-secret'
TOKEN = 'fictional-merchant-actions-token'
PREFIX = '/api/admin/ifood/merchant'
REMOTE = '/merchant/v1.0/merchants/' + MERCHANT


def response(value=None, status=200):
    data = BytesIO(b'' if value is None else json.dumps(value).encode())
    data.status = status
    return data


class MerchantActionsTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.password = 'disposable-actions-password'
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
        self.verify()
        self.created = None
        self.pauses = []
        self.calls = []
        self.hours = [{'shifts': [{'id': OTHER, 'dayOfWeek': 'UNCHANGED_PROVIDER_DAY',
                                  'start': 'PROVIDER_TIME_FORMAT', 'duration': 300}]}]
        with sqlite3.connect(self.app.state.db_path) as conn:
            self.business_before = {table: conn.execute('SELECT * FROM ' + table).fetchall()
                                    for table in ('products', 'orders', 'customers')}

    def tearDown(self):
        with sqlite3.connect(self.app.state.db_path) as conn:
            self.assertEqual(self.business_before,
                             {table: conn.execute('SELECT * FROM ' + table).fetchall()
                              for table in self.business_before})
        self.client.close()
        self.env.stop()
        self.directory.cleanup()

    def verify(self, state='verified', merchant=MERCHANT):
        with sqlite3.connect(self.app.state.db_path) as conn:
            conn.execute('INSERT OR REPLACE INTO ifood_connection_check VALUES(1,?,?,?)',
                         (ifood.fingerprint(ifood.config()), 1,
                          json.dumps({'state': state, 'merchant_id': merchant})))

    def body(self, key=None):
        return {'idempotency_key': str(key or uuid4()), 'description': 'Pausa de homologação',
                'start': '2030-01-01T18:00:00-03:00', 'end': '2030-01-01T18:30:00-03:00'}

    def post(self, suffix, body=None):
        return self.client.post(PREFIX + suffix, json={} if body is None else body, headers=self.headers)

    def transport(self, request, **kwargs):
        self.assertEqual(kwargs['timeout'], 8)
        path = request.full_url.removeprefix(ifood.HOST)
        self.calls.append((request.get_method(), path))
        if path == '/authentication/v1.0/oauth/token':
            return response({'accessToken': TOKEN, 'type': 'Bearer'})
        self.assertEqual(request.get_header('Authorization'), 'Bearer ' + TOKEN)
        self.assertTrue(path.startswith(REMOTE + '/'))
        if path == REMOTE + '/interruptions':
            if request.get_method() == 'GET':
                self.assertIsNone(request.data)
                return response(self.pauses)
            self.assertEqual(request.get_method(), 'POST')
            self.created = json.loads(request.data)
            self.assertEqual(set(self.created), {'id', 'description', 'start', 'end'})
            with sqlite3.connect(self.app.state.db_path) as conn:
                row = conn.execute('SELECT state,request_json FROM ifood_merchant_operations WHERE id=?',
                                   (self.created['id'],)).fetchone()
            self.assertEqual(row[0], 'processing')
            self.assertEqual(json.loads(row[1]), self.created)
            self.pauses = [self.created]
            return response(self.created, 201)
        if path.startswith(REMOTE + '/interruptions/'):
            self.assertEqual(request.get_method(), 'DELETE')
            self.assertEqual(path.rsplit('/', 1)[1], self.created['id'])
            self.assertIsNone(request.data)
            self.pauses = []
            return response(status=204)
        if path == REMOTE + '/opening-hours':
            self.assertEqual(request.get_method(), 'GET')
            return response(self.hours)
        self.fail('Unexpected provider path: ' + path)

    def create(self, body=None):
        with patch('server.ifood.evolution.open_url', side_effect=self.transport):
            return self.post('/interruptions', body or self.body()).json()

    def test_auth_csrf_strict_bodies_and_fixed_target(self):
        with patch('server.ifood.evolution.open_url') as call:
            anonymous = TestClient(self.app)
            try:
                self.assertEqual(anonymous.get(PREFIX + '/actions').status_code, 401)
                self.assertEqual(anonymous.post(PREFIX + '/interruptions', json=self.body()).status_code, 401)
            finally:
                anonymous.close()
            self.assertEqual(self.client.post(PREFIX + '/interruptions', json=self.body()).status_code, 403)
            self.assertEqual(self.client.post(PREFIX + '/interruptions', json=self.body(), headers={
                **self.headers, 'Origin': 'https://untrusted.invalid'}).status_code, 403)
            for extra in ({'merchant_id': OTHER}, {'environment': 'production'}, {'id': OTHER}):
                self.assertEqual(self.post('/interruptions', {**self.body(), **extra}).status_code, 422)
            for body in ({'merchant_id': OTHER}, {'url': 'https://untrusted.invalid'}):
                self.assertEqual(self.post('/interruptions/query', body).status_code, 422)
            bad_dates = [dict(start='2030-01-01T00:00:00'), dict(end='2029-01-01T00:00:00Z'),
                         dict(description='\n\t'), dict(idempotency_key='invalid')]
            for changes in bad_dates:
                self.assertEqual(self.post('/interruptions', {**self.body(), **changes}).status_code, 422)
        call.assert_not_called()

    def test_guards_production_wrong_merchant_unverified_rotation_never_write(self):
        cases = [({'SAHARA_IFOOD_ENVIRONMENT': 'production'}, 'not_ready'),
                 ({'SAHARA_IFOOD_ENABLED': '0'}, 'not_ready'),
                 ({'SAHARA_IFOOD_MERCHANT_ID': OTHER}, 'restricted'),
                 ({'SAHARA_IFOOD_CLIENT_SECRET': SECRET + '-rotated'}, 'not_ready')]
        with patch('server.ifood.evolution.open_url') as call:
            for change, state in cases:
                with self.subTest(change=change), patch.dict(os.environ, change):
                    self.assertEqual(self.post('/interruptions', self.body()).json()['state'], state)
            self.verify(state='provider')
            self.assertEqual(self.post('/interruptions', self.body()).json()['state'], 'not_ready')
        call.assert_not_called()
        self.assertEqual(self.client.get(PREFIX + '/actions').json()['operations'], [])

    def test_create_persists_intention_exact_post_and_idempotent_replay(self):
        body = self.body()
        result = self.create(body)
        self.assertEqual(result['state'], 'confirmed')
        self.assertEqual(result['operation']['resource_id'], self.created['id'])
        self.assertEqual(result['environment'], 'test')
        self.assertEqual(self.created['start'], '2030-01-01T21:00:00+00:00')
        self.assertEqual(self.calls, [('POST', '/authentication/v1.0/oauth/token'),
                                      ('POST', REMOTE + '/interruptions')])
        with patch('server.ifood.evolution.open_url') as call:
            repeated = self.post('/interruptions', body).json()
            conflict = self.post('/interruptions', {**body, 'description': 'Outra descrição'}).json()
        call.assert_not_called()
        self.assertEqual(repeated['operation']['id'], result['operation']['id'])
        self.assertEqual(conflict['state'], 'conflict')
        self.assertNotIn(SECRET, json.dumps(result))
        self.assertNotIn(TOKEN, json.dumps(result))

    def test_query_and_remove_only_local_confirmed_pause_once(self):
        result = self.create()
        identifier = result['operation']['id']
        with patch('server.ifood.evolution.open_url', side_effect=self.transport):
            listing = self.post('/interruptions/query').json()
            removed = self.post('/interruptions/' + identifier + '/remove',
                                {'idempotency_key': str(uuid4())}).json()
        self.assertEqual(listing['interruptions'], [self.created])
        self.assertEqual(removed['state'], 'confirmed')
        self.assertTrue(removed['operation']['result']['removed'])
        self.assertEqual(self.calls[-1], ('DELETE', REMOTE + '/interruptions/' + self.created['id']))
        with patch('server.ifood.evolution.open_url') as call:
            repeat = self.post('/interruptions/' + identifier + '/remove',
                               {'idempotency_key': str(uuid4())}).json()
            unknown = self.post('/interruptions/' + str(uuid4()) + '/remove',
                                {'idempotency_key': str(uuid4())}).json()
        call.assert_not_called()
        self.assertEqual(repeat['operation']['id'], removed['operation']['id'])
        self.assertEqual(unknown['state'], 'not_found')

    def test_timeout_after_create_is_uncertain_no_retry_and_reconciles(self):
        body = self.body()
        def timeout(request, **kwargs):
            value = self.transport(request, **kwargs)
            if request.get_method() == 'POST' and request.full_url.endswith('/interruptions'):
                raise URLError('Transport lost after write ' + TOKEN)
            return value
        with patch('server.ifood.evolution.open_url', side_effect=timeout):
            uncertain = self.post('/interruptions', body).json()
        self.assertEqual(uncertain['state'], 'uncertain')
        with patch('server.ifood.evolution.open_url') as call:
            self.assertEqual(self.post('/interruptions', body).json()['state'], 'uncertain')
            self.assertEqual(self.post('/interruptions', self.body()).json()['state'], 'unresolved')
        call.assert_not_called()
        before = len(self.calls)
        with patch('server.ifood.evolution.open_url', side_effect=self.transport):
            checked = self.post('/operations/' + uncertain['operation']['id'] + '/reconcile').json()
        self.assertEqual(checked['state'], 'confirmed')
        self.assertEqual(self.calls[before:], [('POST', '/authentication/v1.0/oauth/token'),
                                             ('GET', REMOTE + '/interruptions')])

    def test_provider_naive_millisecond_dates_match_own_utc_intention_and_remain_raw_in_list(self):
        def naive_dates(request, **kwargs):
            value = self.transport(request, **kwargs)
            if request.get_method() == 'POST' and request.full_url.endswith('/interruptions'):
                actual = dict(self.created)
                for field in ('start', 'end'):
                    actual[field] = datetime.fromisoformat(actual[field]).replace(tzinfo=None).isoformat(timespec='milliseconds')
                self.pauses = [actual]
                return response(actual, 201)
            return value
        body = {**self.body(), 'start': '2030-01-01T18:00:00.123456-03:00',
                'end': '2030-01-01T18:30:00.789654-03:00'}
        with patch('server.ifood.evolution.open_url', side_effect=naive_dates):
            created = self.post('/interruptions', body).json()
            listed = self.post('/interruptions/query').json()
        self.assertEqual(created['state'], 'confirmed')
        pause = created['operation']['result']['interruption']
        self.assertEqual(pause['start'], '2030-01-01T21:00:00.123456+00:00')
        self.assertEqual(self.created['start'], pause['start'])
        self.assertEqual(pause['provider_start'], '2030-01-01T21:00:00.123')
        self.assertEqual(listed['interruptions'][0]['start'], pause['provider_start'])

    def test_naive_provider_dates_with_different_utc_wall_clock_remain_uncertain(self):
        def shifted(request, **kwargs):
            value = self.transport(request, **kwargs)
            if request.get_method() == 'POST' and request.full_url.endswith('/interruptions'):
                actual = dict(self.created)
                for field in ('start', 'end'):
                    actual[field] = (datetime.fromisoformat(actual[field]).replace(tzinfo=None) + timedelta(hours=3)).isoformat()
                return response(actual, 201)
            return value
        with patch('server.ifood.evolution.open_url', side_effect=shifted):
            created = self.post('/interruptions', self.body()).json()
        self.assertEqual(created['state'], 'uncertain')

    def test_provider_assigned_id_reconciles_by_marker_and_removes_only_that_resource(self):
        remote_id = str(uuid4())
        def assigned_id_then_lost(request, **kwargs):
            value = self.transport(request, **kwargs)
            if request.get_method() == 'POST' and request.full_url.endswith('/interruptions'):
                self.created = {**self.created, 'id': remote_id}
                self.pauses = [self.created]
                raise TimeoutError()
            return value
        with patch('server.ifood.evolution.open_url', side_effect=assigned_id_then_lost):
            pending = self.post('/interruptions', self.body()).json()
        local_id = pending['operation']['id']
        self.assertNotEqual(local_id, remote_id)
        self.assertIn(local_id, self.created['description'])
        with patch('server.ifood.evolution.open_url', side_effect=self.transport):
            checked = self.post('/operations/' + local_id + '/reconcile').json()
            self.assertEqual(checked['state'], 'confirmed')
            self.assertEqual(checked['operation']['resource_id'], remote_id)
            self.assertEqual(checked['operation']['result']['correlation'], 'description_marker')
            removed = self.post('/interruptions/' + local_id + '/remove', {'idempotency_key': str(uuid4())}).json()
        self.assertEqual(removed['state'], 'confirmed')
        self.assertEqual(self.pauses, [])

    def test_ambiguous_readback_does_not_adopt_either_provider_pause(self):
        def lost(request, **kwargs):
            value = self.transport(request, **kwargs)
            if request.get_method() == 'POST' and request.full_url.endswith('/interruptions'):
                self.pauses = [{**self.created, 'id': str(uuid4())} for _ in range(2)]
                raise TimeoutError()
            return value
        with patch('server.ifood.evolution.open_url', side_effect=lost):
            pending = self.post('/interruptions', self.body()).json()
        with patch('server.ifood.evolution.open_url', side_effect=self.transport):
            checked = self.post('/operations/' + pending['operation']['id'] + '/reconcile').json()
        self.assertEqual(checked['state'], 'uncertain')
        self.assertIsNone(checked['operation']['resource_id'])
        self.assertFalse(any(method == 'DELETE' for method, path in self.calls))

    def test_secret_rotation_cannot_resubmit_an_uncertain_create(self):
        body = self.body()
        def timeout(request, **kwargs):
            value = self.transport(request, **kwargs)
            if request.get_method() == 'POST' and request.full_url.endswith('/interruptions'):
                raise TimeoutError()
            return value
        with patch('server.ifood.evolution.open_url', side_effect=timeout):
            original = self.post('/interruptions', body).json()
        self.assertEqual(original['state'], 'uncertain')
        os.environ['SAHARA_IFOOD_CLIENT_SECRET'] = SECRET + '-renewed'
        self.verify()
        with patch('server.ifood.evolution.open_url') as provider:
            same = self.post('/interruptions', body).json()
            another = self.post('/interruptions', self.body()).json()
        provider.assert_not_called()
        self.assertEqual(same['operation']['id'], original['operation']['id'])
        self.assertEqual(same['state'], 'uncertain')
        self.assertEqual(another['state'], 'unresolved')
        with patch('server.ifood.evolution.open_url', side_effect=self.transport):
            reconciled = self.post('/operations/' + original['operation']['id'] + '/reconcile').json()
        self.assertEqual(reconciled['state'], 'confirmed')
        self.assertEqual(sum(method == 'POST' and path.endswith('/interruptions') for method, path in self.calls), 1)

    def test_slow_stale_reconciliation_cannot_downgrade_a_confirmed_pause(self):
        def lost_write(request, **kwargs):
            value = self.transport(request, **kwargs)
            if request.get_method() == 'POST' and request.full_url.endswith('/interruptions'):
                raise TimeoutError()
            return value
        with patch('server.ifood.evolution.open_url', side_effect=lost_write):
            uncertain = self.post('/interruptions', self.body()).json()
        route = '/operations/' + uncertain['operation']['id'] + '/reconcile'
        entered, release = Event(), Event()
        def stale_read(request, **kwargs):
            if request.get_method() == 'GET' and request.full_url.endswith('/interruptions') and not entered.is_set():
                entered.set()
                self.assertTrue(release.wait(10))
                return response([])
            return self.transport(request, **kwargs)
        with patch('server.ifood.evolution.open_url', side_effect=stale_read):
            with ThreadPoolExecutor(max_workers=2) as pool:
                stale = pool.submit(self.post, route)
                self.assertTrue(entered.wait(5))
                try:
                    self.assertEqual(self.post(route).json()['state'], 'confirmed')
                finally:
                    release.set()
                self.assertEqual(stale.result().json()['state'], 'confirmed')
        self.assertEqual(self.client.get(PREFIX + '/actions').json()['operations'][0]['state'], 'confirmed')

    def test_absent_uncertain_create_never_declared_rejected_or_resent(self):
        def timeout(request, **kwargs):
            if request.full_url.endswith('/interruptions'):
                raise TimeoutError()
            return self.transport(request, **kwargs)
        with patch('server.ifood.evolution.open_url', side_effect=timeout):
            uncertain = self.post('/interruptions', self.body()).json()
        with patch('server.ifood.evolution.open_url', side_effect=self.transport):
            checked = self.post('/operations/' + uncertain['operation']['id'] + '/reconcile').json()
        self.assertEqual(checked['state'], 'uncertain')
        self.assertEqual(self.post('/interruptions', self.body()).json()['state'], 'unresolved')

    def test_invalid_successful_creation_is_uncertain(self):
        def malformed(request, **kwargs):
            if request.full_url.endswith('/interruptions'):
                return response({'id': '../untrusted', 'description': SECRET}, 201)
            return self.transport(request, **kwargs)
        with patch('server.ifood.evolution.open_url', side_effect=malformed):
            result = self.post('/interruptions', self.body()).json()
        self.assertEqual(result['state'], 'uncertain')
        self.assertNotIn(SECRET, json.dumps(result))
        self.assertNotIn(TOKEN, json.dumps(result))

    def test_definitive_provider_rejection_and_oauth_failure_not_uncertain(self):
        for phase in ('token', 'interruptions'):
            def rejected(request, **kwargs):
                if request.full_url.endswith('/' + phase):
                    raise HTTPError(request.full_url, 403, SECRET, {}, BytesIO(TOKEN.encode()))
                return self.transport(request, **kwargs)
            with self.subTest(phase=phase), patch('server.ifood.evolution.open_url', side_effect=rejected):
                result = self.post('/interruptions', self.body()).json()
            self.assertEqual(result['state'], 'rejected')
            self.assertNotIn(SECRET, json.dumps(result))
            self.assertNotIn(TOKEN, json.dumps(result))

    def test_config_rotation_during_write_marks_uncertain_and_hides_old_operations(self):
        def rotate(request, **kwargs):
            value = self.transport(request, **kwargs)
            if request.full_url.endswith('/interruptions'):
                os.environ['SAHARA_IFOOD_CLIENT_SECRET'] = SECRET + '-rotated'
            return value
        with patch('server.ifood.evolution.open_url', side_effect=rotate):
            result = self.post('/interruptions', self.body()).json()
        self.assertEqual(result['state'], 'uncertain')
        self.assertFalse(self.client.get(PREFIX + '/actions').json()['writes_available'])
        self.assertEqual(self.client.get(PREFIX + '/actions').json()['operations'], [])
        self.assertNotIn(SECRET, json.dumps(result))

    def test_concurrent_creation_writes_once(self):
        entered, release = Event(), Event()
        def blocking(request, **kwargs):
            if request.full_url.endswith('/interruptions'):
                entered.set()
                self.assertTrue(release.wait(10))
            return self.transport(request, **kwargs)
        with patch('server.ifood.evolution.open_url', side_effect=blocking):
            with ThreadPoolExecutor(max_workers=2) as pool:
                first = pool.submit(self.post, '/interruptions', self.body())
                self.assertTrue(entered.wait(5))
                second = self.post('/interruptions', self.body()).json()
                release.set()
                self.assertEqual(first.result().json()['state'], 'confirmed')
        self.assertEqual(second['state'], 'unresolved')
        self.assertEqual(sum(path.endswith('/interruptions') and method == 'POST'
                             for method, path in self.calls), 1)

    def test_processing_lease_does_not_race_live_create(self):
        def interruption(request, **kwargs):
            if request.full_url.endswith('/interruptions'):
                raise KeyboardInterrupt('simulated worker interruption')
            return self.transport(request, **kwargs)
        with patch('server.ifood.evolution.open_url', side_effect=interruption):
            with self.assertRaises(KeyboardInterrupt):
                self.post('/interruptions', self.body())
        identifier = self.client.get(PREFIX + '/actions').json()['operations'][0]['id']
        with patch('server.ifood.evolution.open_url') as call:
            checked = self.post('/operations/' + identifier + '/reconcile').json()
        call.assert_not_called()
        self.assertEqual(checked['state'], 'processing')
        with sqlite3.connect(self.app.state.db_path) as conn:
            conn.execute('UPDATE ifood_merchant_operations SET started_at=0 WHERE id=?', (identifier,))
        with patch('server.ifood.evolution.open_url', side_effect=self.transport):
            checked = self.post('/operations/' + identifier + '/reconcile').json()
        self.assertEqual(checked['state'], 'uncertain')

    def test_uncertain_removal_is_not_resent_and_reconciles_absent_resource(self):
        created = self.create()
        identifier = created['operation']['id']
        body = {'idempotency_key': str(uuid4())}
        def lost_delete(request, **kwargs):
            value = self.transport(request, **kwargs)
            if request.get_method() == 'DELETE':
                raise TimeoutError()
            return value
        with patch('server.ifood.evolution.open_url', side_effect=lost_delete):
            lost = self.post('/interruptions/' + identifier + '/remove', body).json()
        self.assertEqual(lost['state'], 'uncertain')
        with patch('server.ifood.evolution.open_url') as call:
            self.assertEqual(self.post('/interruptions/' + identifier + '/remove', body).json()['state'], 'uncertain')
        call.assert_not_called()
        before = len(self.calls)
        with patch('server.ifood.evolution.open_url', side_effect=self.transport):
            resolved = self.post('/operations/' + lost['operation']['id'] + '/reconcile').json()
        self.assertEqual(resolved['state'], 'confirmed')
        self.assertTrue(resolved['operation']['result']['reconciled'])
        self.assertEqual(self.calls[before:], [('POST', '/authentication/v1.0/oauth/token'),
                                             ('GET', REMOTE + '/interruptions')])

    def test_provider_internal_error_is_uncertain_and_malformed_query_is_discarded(self):
        def internal(request, **kwargs):
            if request.full_url.endswith('/interruptions'):
                raise HTTPError(request.full_url, 500, SECRET, {}, BytesIO(TOKEN.encode()))
            return self.transport(request, **kwargs)
        with patch('server.ifood.evolution.open_url', side_effect=internal):
            uncertain = self.post('/interruptions', self.body()).json()
        self.assertEqual(uncertain['state'], 'uncertain')
        self.pauses = [{'id': '../untrusted', 'description': SECRET}]
        with patch('server.ifood.evolution.open_url', side_effect=self.transport):
            malformed = self.post('/interruptions/query').json()
        self.assertEqual(malformed['state'], 'response')
        self.assertNotIn('interruptions', malformed)
        self.assertEqual(malformed['provider_response'][0]['id'], '../untrusted')
        self.assertNotIn(SECRET, json.dumps(malformed['provider_response']))
        self.assertNotIn(SECRET, json.dumps([uncertain, malformed]))
        self.assertNotIn(TOKEN, json.dumps([uncertain, malformed]))

    def test_hours_object_envelope_is_displayed_without_guessing_units(self):
        self.hours = {'storeId': MERCHANT, 'shifts': [{'dayOfWeek': 'PROVIDER_DAY',
                      'start': 'PROVIDER_TIME', 'duration': 999}]}
        with patch('server.ifood.evolution.open_url', side_effect=self.transport):
            result = self.post('/opening-hours/query').json()
        self.assertEqual(result['state'], 'queried')
        self.assertEqual(result['opening_hours'], [self.hours])
        self.assertFalse(any(method in ('PUT', 'PATCH', 'POST') and '/opening-hours' in path
                             for method, path in self.calls))

    def test_hours_are_read_only_without_inventing_schedule_semantics(self):
        with patch('server.ifood.evolution.open_url', side_effect=self.transport):
            result = self.post('/opening-hours/query').json()
        self.assertEqual(result['opening_hours'], self.hours)
        dashboard = self.client.get(PREFIX + '/actions').json()
        self.assertFalse(dashboard['hours_write_available'])
        self.assertIn('unidade', dashboard['hours_write_reason'])
        self.assertIn(self.client.put(PREFIX + '/opening-hours', json={}, headers=self.headers).status_code, (404, 405))
        self.assertFalse(any(method == 'PUT' for method, _ in self.calls))


if __name__ == '__main__':
    unittest.main()
