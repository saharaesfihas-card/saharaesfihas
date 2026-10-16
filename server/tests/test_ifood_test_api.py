"""Verified, server-bound homologation transport; all provider I/O is mocked."""
from io import BytesIO
import json
import os
from pathlib import Path
import sqlite3
from tempfile import TemporaryDirectory
from types import SimpleNamespace
import unittest
from unittest.mock import patch
from urllib.error import HTTPError, URLError

from server import ifood, ifood_stores, ifood_test_api
from server.core import hash_password
from server.main import create_app

CLIENT = '11111111-1111-4111-8111-111111111111'
MERCHANT = ifood_stores.TEST_MERCHANT_ID
OTHER = '22222222-2222-4222-8222-222222222222'
ORDER = '33333333-3333-4333-8333-333333333333'
SECRET = 'fictional-transport-secret'
TOKEN = 'fictional-transport-token'
MERCHANT_PATH = '/merchant/v1.0/merchants/' + MERCHANT
CATALOG_PATH = '/catalog/v2.0/merchants/' + MERCHANT
ORDER_PATH = '/order/v1.0/orders/' + ORDER


class Response(BytesIO):
    def __init__(self, value=None, *, raw=None, status=200):
        super().__init__(raw if raw is not None else json.dumps(value).encode())
        self.status = status


class IFoodTestAPITests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.password_hash = hash_password('disposable-transport-password')

    def setUp(self):
        self.directory = TemporaryDirectory()
        self.env = patch.dict(os.environ, {
            'SAHARA_IFOOD_CLIENT_ID': CLIENT, 'SAHARA_IFOOD_CLIENT_SECRET': SECRET,
            'SAHARA_IFOOD_MERCHANT_ID': MERCHANT, 'SAHARA_IFOOD_ENABLED': '1',
            'SAHARA_IFOOD_ENVIRONMENT': 'test', 'SAHARA_IFOOD_IMPORT_ENABLED': '0',
            'SAHARA_WHATSAPP_ENABLED': '0', 'SAHARA_AI_ENABLED': '0'})
        self.env.start()
        app = create_app(data_dir=Path(self.directory.name),
                         admin_password_hash=self.password_hash, secure_cookie=False)
        self.request = SimpleNamespace(app=app)
        self.verify()

    def tearDown(self):
        self.env.stop()
        self.directory.cleanup()

    def verify(self, *, state='verified', merchant_id=MERCHANT, fingerprint=None):
        with sqlite3.connect(self.request.app.state.db_path) as conn:
            conn.execute('INSERT OR REPLACE INTO ifood_connection_check VALUES(1,?,?,?)',
                         (fingerprint or ifood.fingerprint(ifood.config()), 10,
                          json.dumps({'state': state, 'merchant_id': merchant_id})))

    def session(self, **kwargs):
        return ifood_test_api.Session(self.request, **kwargs)

    def assert_safe_error(self, context, state, uncertain=False):
        error = context.exception
        self.assertIsInstance(error, ifood.ProviderError)
        self.assertEqual(error.state, state)
        self.assertEqual(error.uncertain, uncertain)
        exposed = json.dumps({'args': error.args, 'attributes': vars(error)})
        self.assertNotIn(SECRET, exposed)
        self.assertNotIn(TOKEN, exposed)

    def test_current_verified_test_merchant_is_required_even_before_oauth(self):
        changes = [
            ({'SAHARA_IFOOD_ENABLED': '0'}, 'not_ready'),
            ({'SAHARA_IFOOD_ENVIRONMENT': 'production'}, 'not_ready'),
            ({'SAHARA_IFOOD_CLIENT_SECRET': ''}, 'not_ready'),
            ({'SAHARA_IFOOD_MERCHANT_ID': '../other'}, 'not_ready'),
            ({'SAHARA_IFOOD_CLIENT_SECRET': SECRET + '-rotated'}, 'not_ready'),
        ]
        with patch('server.ifood.access_token') as token, patch('server.ifood.evolution.open_url') as send:
            for change, state in changes:
                with self.subTest(change=list(change)), patch.dict(os.environ, change):
                    with self.assertRaises(ifood.ProviderError) as caught:
                        self.session()
                    self.assert_safe_error(caught, state)
            for state in ('configured', 'authentication', 'permissions'):
                self.verify(state=state)
                with self.subTest(check=state), self.assertRaises(ifood.ProviderError) as caught:
                    self.session()
                self.assert_safe_error(caught, 'not_ready')
            self.verify(merchant_id=OTHER)
            with self.assertRaises(ifood.ProviderError) as caught:
                self.session()
            self.assert_safe_error(caught, 'not_ready')
            self.verify(fingerprint='previous-configuration')
            with self.assertRaises(ifood.ProviderError) as caught:
                self.session()
            self.assert_safe_error(caught, 'not_ready')
            with sqlite3.connect(self.request.app.state.db_path) as conn:
                conn.execute('DELETE FROM ifood_connection_check')
            with self.assertRaises(ifood.ProviderError) as caught:
                self.session()
            self.assert_safe_error(caught, 'not_ready')
        token.assert_not_called()
        send.assert_not_called()

    def test_even_a_verified_different_test_merchant_is_not_allowlisted(self):
        with patch.dict(os.environ, {'SAHARA_IFOOD_MERCHANT_ID': OTHER}):
            self.verify(merchant_id=OTHER)
            with patch('server.ifood.access_token') as token:
                with self.assertRaises(ifood.ProviderError) as caught:
                    self.session()
            self.assert_safe_error(caught, 'restricted')
            token.assert_not_called()

    def test_get_uses_fixed_host_authorization_and_has_no_payload_or_json_content_type(self):
        session = self.session()
        with patch('server.ifood.access_token', return_value=TOKEN) as token, \
                patch('server.ifood.evolution.open_url', return_value=Response({'state': 'CLOSED'})) as send:
            self.assertEqual(session.call('get', MERCHANT_PATH + '/status'), (200, {'state': 'CLOSED'}))
        token.assert_called_once_with(session.cfg)
        request = send.call_args.args[0]
        self.assertEqual(request.full_url, ifood.HOST + MERCHANT_PATH + '/status')
        self.assertEqual(request.get_method(), 'GET')
        self.assertIsNone(request.data)
        self.assertEqual(request.get_header('Authorization'), 'Bearer ' + TOKEN)
        self.assertEqual(request.get_header('Accept'), 'application/json')
        self.assertIsNone(request.get_header('Content-type'))
        self.assertEqual(send.call_args.kwargs, {'timeout': 8})

    def test_writes_encode_json_and_send_each_requested_method_to_the_bound_merchant(self):
        for method, path, payload in (
                ('POST', MERCHANT_PATH + '/interruptions', {'description': 'Pausa fictícia'}),
                ('PUT', CATALOG_PATH + '/items', {'item': {'id': ORDER}}),
                ('PATCH', CATALOG_PATH + '/items/price', {'itemId': ORDER, 'price': {'value': 4.5}}),
                ('DELETE', MERCHANT_PATH + '/interruptions/local-test-pause', None)):
            with self.subTest(method=method):
                session = self.session()
                with patch('server.ifood.access_token', return_value=TOKEN), \
                        patch('server.ifood.evolution.open_url', return_value=Response({}, status=200)) as send:
                    session.call(method, path, payload)
                request = send.call_args.args[0]
                self.assertEqual(request.get_method(), method)
                self.assertEqual(request.full_url, ifood.HOST + path)
                if payload is None:
                    self.assertIsNone(request.data)
                else:
                    self.assertEqual(json.loads(request.data), payload)
                    self.assertEqual(request.get_header('Content-type'), 'application/json')

    def test_list_and_catalog_include_items_query_are_permitted(self):
        for path in ('/merchant/v1.0/merchants',
                     '/merchant/v1.0/merchants?page=1&size=100',
                     '/merchant/v1.0/merchants?size=1&page=2',
                     CATALOG_PATH + '/catalogs/' + ORDER + '/categories?includeItems=true'):
            with self.subTest(path=path):
                with patch('server.ifood.access_token', return_value=TOKEN), \
                        patch('server.ifood.evolution.open_url', return_value=Response([])) as send:
                    self.assertEqual(self.session().call('GET', path, expected=list), (200, []))
                self.assertEqual(send.call_args.args[0].full_url, ifood.HOST + path)

    def test_unbound_orders_foreign_merchants_urls_and_traversal_never_start_oauth(self):
        session = self.session(order_ids=(ORDER,))
        invalid = [
            ('GET', '/merchant/v1.0/merchants/' + OTHER),
            ('POST', '/catalog/v2.0/merchants/' + OTHER + '/items'),
            ('GET', MERCHANT_PATH + OTHER),
            ('GET', '/merchant/v1.0/merchants/' + MERCHANT + 'suffix/status'),
            ('GET', '/order/v1.0/orders/' + OTHER),
            ('POST', ORDER_PATH + OTHER + '/confirm'),
            ('GET', 'https://untrusted.invalid' + MERCHANT_PATH),
            ('GET', '//untrusted.invalid' + MERCHANT_PATH),
            ('GET', MERCHANT_PATH + '/../' + OTHER),
            ('GET', MERCHANT_PATH + '/%2e%2e/' + OTHER),
            ('GET', MERCHANT_PATH + '/%2F' + OTHER),
            ('GET', MERCHANT_PATH + '//status'),
            ('GET', MERCHANT_PATH + '\\status'),
            ('GET', MERCHANT_PATH + '/status#fragment'),
            ('GET', MERCHANT_PATH + '/status\n'),
            ('GET', '/merchant/v1.0/merchants?merchantId=' + OTHER),
            ('GET', MERCHANT_PATH + '/status?merchantId=' + OTHER),
            ('GET', CATALOG_PATH + '/catalogs/' + ORDER + '/categories?includeItems=true&merchantId=' + OTHER),
            ('GET', CATALOG_PATH + '/catalogs/' + ORDER + '/categories?includeItems=false'),
            ('GET', '/merchant/v1.0/merchants?page=1&page=2'),
            ('GET', '/merchant/v1.0/merchants?page=0'),
            ('GET', '/merchant/v1.0/merchants?size=101'),
            ('GET', '/merchant/v1.0/merchants?page=1%26merchantId=' + OTHER),
            ('POST', '/merchant/v1.0/merchants'),
            ('DELETE', MERCHANT_PATH),
            ('POST', MERCHANT_PATH + '/opening-hours'),
            ('PUT', MERCHANT_PATH + '/opening-hours'),
            ('DELETE', CATALOG_PATH + '/items/' + ORDER),
            ('GET', CATALOG_PATH + '/catalogs/' + ORDER + '/categories/' + ORDER + '/version-migration'),
            ('GET', ORDER_PATH + '/confirm'),
            ('POST', ORDER_PATH + '/unknown'),
            ('HEAD', MERCHANT_PATH),
            ('GET', None),
        ]
        with patch('server.ifood.access_token') as token, patch('server.ifood.evolution.open_url') as send:
            for method, path in invalid:
                with self.subTest(method=method, path=path):
                    with self.assertRaises(ifood.ProviderError) as caught:
                        session.call(method, path)
                    self.assert_safe_error(caught, 'request')
        token.assert_not_called()
        send.assert_not_called()

    def test_only_server_bound_order_ids_can_be_called(self):
        for order_ids in ('not-a-list', (None,), ('../foreign',), {ORDER}):
            with self.subTest(order_ids=order_ids), self.assertRaises(ifood.ProviderError) as caught:
                self.session(order_ids=order_ids)
            self.assert_safe_error(caught, 'request')
        with patch('server.ifood.access_token') as token:
            with self.assertRaises(ifood.ProviderError) as caught:
                self.session().call('POST', ORDER_PATH + '/confirm', statuses=(202,), expected=None)
            self.assert_safe_error(caught, 'request')
        token.assert_not_called()
        with patch('server.ifood.access_token', return_value=TOKEN), \
                patch('server.ifood.evolution.open_url', return_value=Response(raw=b'', status=202)):
            self.assertEqual(self.session(order_ids=(ORDER,)).call(
                'POST', ORDER_PATH + '/confirm', statuses=(202,), expected=None), (202, None))

    def test_get_body_bad_status_contract_and_non_json_payload_never_start_oauth(self):
        recursive = []
        recursive.append(recursive)
        cases = [
            ('GET', {'payload': {}}), ('GET', {'payload': []}),
            ('POST', {'payload': {'number': float('nan')}}),
            ('POST', {'payload': {'number': float('inf')}}),
            ('POST', {'payload': {'unserializable': object()}}),
            ('POST', {'payload': recursive}),
            ('POST', {'payload': {'huge': 'x' * ifood_test_api.MAX_RESPONSE}}),
            ('POST', {'statuses': ()}), ('POST', {'statuses': (400,)}),
            ('POST', {'statuses': (200, '202')}), ('POST', {'statuses': 202}),
        ]
        with patch('server.ifood.access_token') as token, patch('server.ifood.evolution.open_url') as send:
            for method, kwargs in cases:
                with self.subTest(method=method, kind=list(kwargs)):
                    with self.assertRaises(ifood.ProviderError) as caught:
                        self.session().call(method, MERCHANT_PATH + '/interruptions', **kwargs)
                    self.assert_safe_error(caught, 'request')
        token.assert_not_called()
        send.assert_not_called()

    def test_configuration_or_verification_revocation_before_oauth_stops_all_io(self):
        for revoke in ('config', 'verification'):
            with self.subTest(revoke=revoke):
                self.verify()
                session = self.session()
                with patch.dict(os.environ):
                    if revoke == 'config':
                        os.environ['SAHARA_IFOOD_CLIENT_SECRET'] = SECRET + '-changed'
                    else:
                        self.verify(state='permissions')
                    with patch('server.ifood.access_token') as token, patch('server.ifood.evolution.open_url') as send:
                        with self.assertRaises(ifood.ProviderError) as caught:
                            session.call('POST', MERCHANT_PATH + '/interruptions', {})
                    self.assert_safe_error(caught, 'configuration_changed')
                    token.assert_not_called()
                    send.assert_not_called()

    def test_configuration_change_during_oauth_prevents_business_write_and_is_not_uncertain(self):
        session = self.session()

        def rotate(_cfg):
            os.environ['SAHARA_IFOOD_CLIENT_SECRET'] = SECRET + '-changed'
            return TOKEN

        with patch('server.ifood.access_token', side_effect=rotate), patch('server.ifood.evolution.open_url') as send:
            with self.assertRaises(ifood.ProviderError) as caught:
                session.call('POST', MERCHANT_PATH + '/interruptions', {})
        self.assert_safe_error(caught, 'configuration_changed')
        send.assert_not_called()

    def test_configuration_change_after_io_discards_response_and_marks_only_writes_uncertain(self):
        for method in ('GET', 'POST'):
            with self.subTest(method=method), patch.dict(os.environ):
                self.verify()
                session = self.session()

                def rotate(request, **kwargs):
                    os.environ['SAHARA_IFOOD_CLIENT_SECRET'] = SECRET + '-changed'
                    return Response({'privateResult': 'must be discarded'})

                with patch('server.ifood.access_token', return_value=TOKEN), \
                        patch('server.ifood.evolution.open_url', side_effect=rotate) as send:
                    with self.assertRaises(ifood.ProviderError) as caught:
                        session.call(method, MERCHANT_PATH + '/interruptions', {} if method != 'GET' else None)
                self.assert_safe_error(caught, 'configuration_changed', uncertain=method != 'GET')
                self.assertNotIn('privateResult', repr(caught.exception.args))
                self.assertEqual(send.call_count, 1)

    def test_oauth_failure_never_claims_uncertain_business_write(self):
        for state in ('network', 'authentication', 'response'):
            error = ifood.ProviderError(state, 'token', 401 if state == 'authentication' else None)
            with self.subTest(state=state), patch('server.ifood.access_token', side_effect=error), \
                    patch('server.ifood.evolution.open_url') as send:
                with self.assertRaises(ifood.ProviderError) as caught:
                    self.session().call('POST', MERCHANT_PATH + '/interruptions', {})
                self.assert_safe_error(caught, state)
            send.assert_not_called()

    def test_status_only_202_returns_no_body_even_when_provider_returns_json_or_invalid_text(self):
        for raw in (b'', b'not JSON', json.dumps({'token': TOKEN, 'secret': SECRET}).encode()):
            with self.subTest(body=bool(raw)), patch('server.ifood.access_token', return_value=TOKEN), \
                    patch('server.ifood.evolution.open_url', return_value=Response(raw=raw, status=202)):
                self.assertEqual(self.session(order_ids=(ORDER,)).call('POST', ORDER_PATH + '/confirm',
                    statuses=(202,), expected=None), (202, None))

    def test_even_status_only_commands_read_at_most_limit_plus_one_and_reject_oversized_body(self):
        body = Response(raw=b'x' * (ifood_test_api.MAX_RESPONSE + 5), status=202)
        with patch.object(body, 'read', wraps=body.read) as read, \
                patch('server.ifood.access_token', return_value=TOKEN), \
                patch('server.ifood.evolution.open_url', return_value=body):
            with self.assertRaises(ifood.ProviderError) as caught:
                self.session(order_ids=(ORDER,)).call('POST', ORDER_PATH + '/confirm', statuses=(202,), expected=None)
        self.assert_safe_error(caught, 'response', uncertain=True)
        read.assert_called_once_with(ifood_test_api.MAX_RESPONSE + 1)

    def test_empty_204_is_list_only_when_requested_as_list(self):
        for expected, result in ((list, []), ((dict, list), []), (None, None)):
            with self.subTest(expected=expected), patch('server.ifood.access_token', return_value=TOKEN), \
                    patch('server.ifood.evolution.open_url', return_value=Response(raw=b'', status=204)):
                self.assertEqual(self.session().call('GET', MERCHANT_PATH + '/interruptions',
                    statuses=(204,), expected=expected), (204, result))
        with patch('server.ifood.access_token', return_value=TOKEN), \
                patch('server.ifood.evolution.open_url', return_value=Response(raw=b'', status=204)):
            with self.assertRaises(ifood.ProviderError) as caught:
                self.session().call('GET', MERCHANT_PATH + '/interruptions', statuses=(204,), expected=dict)
        self.assert_safe_error(caught, 'response')

    def test_explicitly_nullable_expected_type_accepts_bounded_empty_success_and_json_null(self):
        for raw, status in ((b'', 200), (b'', 201), (b'null', 200)):
            with self.subTest(status=status, raw=raw), patch('server.ifood.access_token', return_value=TOKEN), \
                    patch('server.ifood.evolution.open_url', return_value=Response(raw=raw, status=status)):
                self.assertEqual(self.session().call('POST', MERCHANT_PATH + '/interruptions', {},
                    statuses=(200, 201), expected=(dict, list, type(None))), (status, None))

    def test_unexpected_success_status_is_not_mistaken_for_confirmation(self):
        for method in ('GET', 'POST'):
            with self.subTest(method=method), patch('server.ifood.access_token', return_value=TOKEN), \
                    patch('server.ifood.evolution.open_url', return_value=Response({'echo': SECRET}, status=201)):
                with self.assertRaises(ifood.ProviderError) as caught:
                    self.session().call(method, MERCHANT_PATH + '/interruptions', statuses=(200,))
                self.assert_safe_error(caught, 'response', uncertain=method != 'GET')
                self.assertEqual(caught.exception.http_status, 201)

    def test_invalid_utf8_json_type_depth_node_count_and_non_finite_values_are_discarded(self):
        nested = {'leaf': True}
        for _ in range(22):
            nested = {'nested': nested}
        raws = (b'not json', b'\xff', b'[]', b'', b'{"bad":NaN}', b'{"bad":Infinity}',
                json.dumps(nested).encode(), json.dumps({'nodes': [True] * 50001}).encode(),
                b'x' * (ifood_test_api.MAX_RESPONSE + 1))
        for method in ('GET', 'POST'):
            for raw in raws:
                with self.subTest(method=method, size=len(raw)), \
                        patch('server.ifood.access_token', return_value=TOKEN), \
                        patch('server.ifood.evolution.open_url', return_value=Response(raw=raw)):
                    with self.assertRaises(ifood.ProviderError) as caught:
                        self.session().call(method, MERCHANT_PATH + '/interruptions')
                    self.assert_safe_error(caught, 'response', uncertain=method != 'GET')

    def test_private_strings_nested_keys_and_numeric_credentials_are_redacted(self):
        numeric_secret, numeric_token = '12345678', '87654321'
        with patch.dict(os.environ, {'SAHARA_IFOOD_CLIENT_SECRET': numeric_secret}):
            self.verify()
            value = {'safe': [{'client_secret': 'hidden', 'accessToken': 'hidden',
                              'auth-orization': 'hidden', 'api-key': 'hidden',
                              'credential': {'data': 'hidden'},
                              'k-' + numeric_secret: 'still present',
                              'note': numeric_secret + ':' + numeric_token,
                              'number': int(numeric_secret), 'decimal': float(numeric_token),
                              'larger': int(numeric_token + '0'), 'regular': 123,
                              'flag': True, 'empty': None}]}
            with patch('server.ifood.access_token', return_value=numeric_token), \
                    patch('server.ifood.evolution.open_url', return_value=Response(value)):
                status, data = self.session().call('GET', MERCHANT_PATH)
        self.assertEqual(status, 200)
        encoded = json.dumps(data)
        for forbidden in (numeric_secret, numeric_token, 'hidden', 'client_secret', 'accessToken', 'credential'):
            self.assertNotIn(forbidden, encoded)
        self.assertEqual(data, {'safe': [{'k-[redigido]': 'still present', 'note': '[redigido]:[redigido]',
            'number': '[redigido]', 'decimal': '[redigido]', 'larger': '[redigido]',
            'regular': 123, 'flag': True, 'empty': None}]})

    def test_http_errors_close_provider_body_and_do_not_expose_it(self):
        states = {400: 'request', 401: 'authentication', 403: 'permissions',
                  404: 'provider', 409: 'request', 422: 'request', 429: 'quota',
                  500: 'provider', 503: 'provider', 302: 'provider', 408: 'provider'}
        for method in ('GET', 'POST'):
            for status, state in states.items():
                body = BytesIO((SECRET + TOKEN).encode())
                error = HTTPError(ifood.HOST + MERCHANT_PATH, status, SECRET + TOKEN, {}, body)
                with self.subTest(method=method, status=status), patch('server.ifood.access_token', return_value=TOKEN), \
                        patch('server.ifood.evolution.open_url', side_effect=error) as send:
                    with self.assertRaises(ifood.ProviderError) as caught:
                        self.session().call(method, MERCHANT_PATH + '/interruptions')
                    self.assert_safe_error(caught, state, uncertain=method != 'GET' and
                                           (status >= 500 or status in (302, 408)))
                    self.assertEqual(caught.exception.http_status, status)
                self.assertTrue(body.closed)
                self.assertEqual(send.call_count, 1)

    def test_network_errors_mark_only_sent_writes_uncertain_and_do_not_retry(self):
        for method in ('GET', 'POST'):
            for error in (URLError(SECRET + TOKEN), TimeoutError(SECRET), OSError(TOKEN)):
                with self.subTest(method=method, error=type(error).__name__), \
                        patch('server.ifood.access_token', return_value=TOKEN), \
                        patch('server.ifood.evolution.open_url', side_effect=error) as send:
                    with self.assertRaises(ifood.ProviderError) as caught:
                        self.session().call(method, MERCHANT_PATH + '/interruptions')
                    self.assert_safe_error(caught, 'network', uncertain=method != 'GET')
                self.assertEqual(send.call_count, 1)


if __name__ == '__main__':
    unittest.main()
