"""Preparação das três lojas reais em banco temporário, sem acesso ao iFood."""
import os
from pathlib import Path
import sqlite3
from tempfile import TemporaryDirectory
import unittest
from unittest.mock import patch

from fastapi.testclient import TestClient

from server import ifood, ifood_stores
from server.core import hash_password
from server.main import create_app

TEST_MERCHANT = '22222222-2222-4222-8222-222222222222'
REAL_MERCHANTS = (
    '33333333-3333-4333-8333-333333333333',
    '44444444-4444-4444-8444-444444444444',
    '55555555-5555-4555-8555-555555555555',
)
PORTAL_IDS = ('9090001', '9090002', '9090003')


class IFoodStoreTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.password = 'disposable-store-registry-password'
        cls.password_hash = hash_password(cls.password)

    def setUp(self):
        self.directory = TemporaryDirectory()
        self.data_dir = Path(self.directory.name)
        self.env = patch.dict(os.environ, {
            'SAHARA_IFOOD_CLIENT_ID': '11111111-1111-4111-8111-111111111111',
            'SAHARA_IFOOD_CLIENT_SECRET': 'fictional-test-secret',
            'SAHARA_IFOOD_MERCHANT_ID': TEST_MERCHANT,
            'SAHARA_IFOOD_ENABLED': '1', 'SAHARA_IFOOD_ENVIRONMENT': 'test',
            'SAHARA_IFOOD_IMPORT_ENABLED': '0',
            'SAHARA_WHATSAPP_ENABLED': '0', 'SAHARA_AI_ENABLED': '0'})
        self.env.start()
        self.network = patch('server.evolution.open_url',
                             side_effect=AssertionError('Cadastro não deve acessar a rede.'))
        self.external = self.network.start()
        self.app = create_app(data_dir=self.data_dir,
                              admin_password_hash=self.password_hash, secure_cookie=False)
        self.client = TestClient(self.app)
        login = self.client.post('/api/admin/login', json={'password': self.password})
        self.headers = {'X-Sahara-CSRF': login.json()['csrf_token']}

    def tearDown(self):
        self.client.close()
        try:
            self.external.assert_not_called()
        finally:
            self.network.stop()
            self.env.stop()
            self.directory.cleanup()

    def stores(self):
        response = self.client.get('/api/admin/ifood/stores')
        self.assertEqual(response.status_code, 200, response.text)
        return response.json()

    def update(self, store_id, merchant_id):
        return self.patch_store(store_id, {'merchant_id': merchant_id})

    def patch_store(self, store_id, body):
        return self.client.patch('/api/admin/ifood/stores/' + store_id,
                                 json=body, headers=self.headers)

    def other_tables(self):
        with sqlite3.connect(self.app.state.db_path) as conn:
            names = [row[0] for row in conn.execute(
                "SELECT name FROM sqlite_master WHERE type='table' AND name!='ifood_stores'")]
            return {name: conn.execute('SELECT * FROM "' + name.replace('"', '""') + '"').fetchall()
                    for name in names}

    def assert_pending(self, dashboard):
        self.assertEqual(len(dashboard['stores']), 3)
        for store in dashboard['stores']:
            self.assertEqual(store['environment'], 'production')
            self.assertEqual(store['connection_state'], 'production_pending')
            self.assertEqual(store['catalog_state'], 'pending')
            self.assertIs(store['import_available'], False)

    def test_initial_stores_have_separate_pending_catalogs_and_no_assigned_uuid(self):
        response = self.stores()
        self.assertEqual([(store['id'], store['name']) for store in response['stores']],
                         [('esfihas-44', 'Esfihas 44'),
                          ('sahara-esfihas-shawarma', 'Sahara Esfihas e Shawarma'),
                          ('maktub-esfihas', 'Maktub Esfihas')])
        self.assertTrue(all(store['merchant_id'] is None for store in response['stores']))
        self.assertTrue(all(store['portal_id'] is None for store in response['stores']))
        self.assertIn('não ativa a integração', response['message'])
        self.assert_pending(response)

    def test_each_store_uuid_is_independent_and_saving_never_enables_production(self):
        for (identifier, _), merchant in zip(ifood_stores.DEFAULTS, REAL_MERCHANTS):
            response = self.update(identifier, merchant)
            self.assertEqual(response.status_code, 200, response.text)
            self.assert_pending(response.json())
        dashboard = self.stores()
        self.assertEqual([store['merchant_id'] for store in dashboard['stores']], list(REAL_MERCHANTS))
        self.assert_pending(dashboard)
        self.assertTrue(self.client.get('/api/admin/ifood').json()['configured'])
        self.assertEqual(self.client.get('/api/admin/ifood').json()['environment'], 'test')

    def test_canonical_uuid_clear_and_restart_preserve_other_stores(self):
        upper = 'ABCDEFAB-CDEF-4ABC-8DEF-ABCDEFABCDEF'
        self.assertEqual(self.update('esfihas-44', '  ' + upper + '  ').status_code, 200)
        self.assertEqual(self.update('maktub-esfihas', REAL_MERCHANTS[2]).status_code, 200)
        self.assertEqual(self.stores()['stores'][0]['merchant_id'], upper.lower())
        self.assertEqual(self.update('esfihas-44', '   ').status_code, 200)
        self.assertIsNone(self.stores()['stores'][0]['merchant_id'])
        self.assertEqual(self.update('sahara-esfihas-shawarma', REAL_MERCHANTS[1]).status_code, 200)
        self.assertEqual(self.update('sahara-esfihas-shawarma', None).status_code, 200)
        self.assertIsNone(self.stores()['stores'][1]['merchant_id'])
        expected = self.stores()
        restarted = create_app(data_dir=self.data_dir,
                               admin_password_hash=self.password_hash, secure_cookie=False)
        other_client = TestClient(restarted)
        try:
            other_client.post('/api/admin/login', json={'password': self.password})
            self.assertEqual(other_client.get('/api/admin/ifood/stores').json(), expected)
        finally:
            other_client.close()

    def test_duplicate_uuid_is_conflict_and_can_be_reused_after_clearing(self):
        self.assertEqual(self.update('esfihas-44', REAL_MERCHANTS[0]).status_code, 200)
        before = self.stores()
        self.assertEqual(self.update('maktub-esfihas', REAL_MERCHANTS[0]).status_code, 409)
        self.assertEqual(self.stores(), before)
        self.assertEqual(self.update('esfihas-44', None).status_code, 200)
        self.assertEqual(self.update('maktub-esfihas', REAL_MERCHANTS[0]).status_code, 200)
        self.assertIsNone(self.stores()['stores'][0]['merchant_id'])
        self.assertEqual(self.stores()['stores'][2]['merchant_id'], REAL_MERCHANTS[0])

    def test_invalid_test_or_non_string_uuid_and_unexpected_fields_are_rejected(self):
        invalid = [
            {'merchant_id': '12345'}, {'merchant_id': '../other'}, {'merchant_id': 44},
            {'merchant_id': True}, {'merchant_id': []}, {},
            {'merchant_id': REAL_MERCHANTS[0].replace('-', '')},
            {'merchant_id': '{' + REAL_MERCHANTS[0] + '}'},
            {'merchant_id': ifood_stores.TEST_MERCHANT_ID},
            {'merchant_id': TEST_MERCHANT},
            {'merchant_id': REAL_MERCHANTS[0], 'enabled': True},
            {'merchant_id': REAL_MERCHANTS[0], 'name': 'Outra loja'},
            {'merchant_id': REAL_MERCHANTS[0], 'catalog_state': 'ready'},
        ]
        before = self.stores()
        for body in invalid:
            with self.subTest(body=body):
                response = self.client.patch('/api/admin/ifood/stores/esfihas-44',
                                             json=body, headers=self.headers)
                self.assertEqual(response.status_code, 422, response.text)
                self.assertEqual(self.stores(), before)
        self.assertEqual(self.update('unknown-store', REAL_MERCHANTS[0]).status_code, 404)
        self.assertEqual(self.stores(), before)

    def test_login_csrf_and_same_origin_required_for_changes(self):
        before = self.stores()
        anonymous = TestClient(self.app)
        try:
            self.assertEqual(anonymous.get('/api/admin/ifood/stores').status_code, 401)
            self.assertEqual(anonymous.patch('/api/admin/ifood/stores/esfihas-44',
                                             json={'merchant_id': REAL_MERCHANTS[0]}).status_code, 401)
        finally:
            anonymous.close()
        self.assertEqual(self.client.patch('/api/admin/ifood/stores/esfihas-44',
                                           json={'merchant_id': REAL_MERCHANTS[0]}).status_code, 403)
        response = self.client.patch('/api/admin/ifood/stores/esfihas-44',
            json={'merchant_id': REAL_MERCHANTS[0]},
            headers={**self.headers, 'Origin': 'https://untrusted.invalid'})
        self.assertEqual(response.status_code, 403)
        self.assertEqual(self.stores(), before)
        response = self.client.patch('/api/admin/ifood/stores/esfihas-44',
            json={'merchant_id': REAL_MERCHANTS[0]},
            headers={**self.headers, 'Origin': 'http://testserver'})
        self.assertEqual(response.status_code, 200)

    def test_saving_stores_does_not_change_catalog_orders_test_checks_or_configuration(self):
        with sqlite3.connect(self.app.state.db_path) as conn:
            conn.execute("INSERT INTO ifood_connection_check VALUES(1,'keep-test-check',10,'{}')")
            conn.execute("INSERT INTO ifood_import_state VALUES(1,'keep-test-import',20,'{}')")
            conn.execute("INSERT INTO orders(id,created_at,delivery_json,items_json,subtotal_cents,"
                         "total_cents,tracking_token,idempotency_key,request_hash,source,status) "
                         "VALUES('existing-test-order','2026-10-15T00:00:00Z','{}','[]',400,400,"
                         "'private-token','existing-order-key','saved-request','ifood','ifood_received')")
            conn.execute("INSERT INTO ifood_imported_orders VALUES(?,?,'existing-test-order')",
                         (TEST_MERCHANT, 'existing-provider-order'))
            conn.execute("INSERT INTO ifood_imported_events VALUES(?,?,?,?,?,0)",
                         (TEST_MERCHANT, 'test-client', 'test-event', 'existing-provider-order',
                          '2026-10-15T00:00:00Z'))
        before = self.other_tables()
        cfg = ifood.config()
        public_before = self.client.get('/api/catalog').json()
        for (identifier, _), merchant in zip(ifood_stores.DEFAULTS, REAL_MERCHANTS):
            self.assertEqual(self.update(identifier, merchant).status_code, 200)
        self.assertEqual(self.patch_store('esfihas-44', {'portal_id': PORTAL_IDS[0]}).status_code, 200)
        self.assertEqual(self.other_tables(), before)
        self.assertEqual(ifood.config(), cfg)
        self.assertEqual(self.client.get('/api/catalog').json(), public_before)
        self.assert_pending(self.stores())

    def test_known_test_uuid_stays_rejected_with_production_environment(self):
        with patch.dict(os.environ, {'SAHARA_IFOOD_ENVIRONMENT': 'production'}):
            self.assertEqual(self.update('esfihas-44', ifood_stores.TEST_MERCHANT_ID).status_code, 422)
            self.assertEqual(self.update('esfihas-44', REAL_MERCHANTS[0]).status_code, 200)
            self.assert_pending(self.stores())
            current = self.client.get('/api/admin/ifood').json()
            self.assertFalse(current['configured'])
            self.assertEqual(current['configuration_state'], 'mode')

    def test_portal_ids_are_separate_references_and_never_become_merchant_uuids(self):
        for (identifier, _), portal_id in zip(ifood_stores.DEFAULTS, PORTAL_IDS):
            response = self.patch_store(identifier, {'portal_id': portal_id})
            self.assertEqual(response.status_code, 200, response.text)
            self.assert_pending(response.json())
        dashboard = self.stores()
        self.assertEqual([store['portal_id'] for store in dashboard['stores']], list(PORTAL_IDS))
        self.assertTrue(all(store['merchant_id'] is None for store in dashboard['stores']))
        self.assert_pending(dashboard)

    def test_partial_updates_preserve_the_omitted_identifier_and_explicit_null_clears(self):
        response = self.patch_store('esfihas-44', {
            'merchant_id': REAL_MERCHANTS[0], 'portal_id': '  0009  '})
        self.assertEqual(response.status_code, 200, response.text)
        first = response.json()['stores'][0]
        self.assertEqual(first['merchant_id'], REAL_MERCHANTS[0])
        self.assertEqual(first['portal_id'], '0009')
        portal = '9' * 20
        response = self.patch_store('esfihas-44', {'portal_id': portal})
        self.assertEqual(response.status_code, 200, response.text)
        self.assertEqual(response.json()['stores'][0]['merchant_id'], REAL_MERCHANTS[0])
        self.assertEqual(response.json()['stores'][0]['portal_id'], portal)
        response = self.update('esfihas-44', None)
        self.assertEqual(response.status_code, 200, response.text)
        self.assertIsNone(response.json()['stores'][0]['merchant_id'])
        self.assertEqual(response.json()['stores'][0]['portal_id'], portal)
        response = self.update('esfihas-44', REAL_MERCHANTS[1])
        self.assertEqual(response.status_code, 200, response.text)
        self.assertEqual(response.json()['stores'][0]['portal_id'], portal)
        response = self.patch_store('esfihas-44', {'portal_id': '   '})
        self.assertEqual(response.status_code, 200, response.text)
        self.assertIsNone(response.json()['stores'][0]['portal_id'])
        self.assertEqual(response.json()['stores'][0]['merchant_id'], REAL_MERCHANTS[1])
        self.assertEqual(self.patch_store('esfihas-44', {'portal_id': '0'}).status_code, 200)
        response = self.patch_store('esfihas-44', {'portal_id': None})
        self.assertEqual(response.status_code, 200, response.text)
        self.assertIsNone(response.json()['stores'][0]['portal_id'])
        self.assertEqual(response.json()['stores'][0]['merchant_id'], REAL_MERCHANTS[1])

    def test_portal_id_rejects_non_ascii_digits_non_strings_paths_and_uuids(self):
        invalid = [9090001, True, False, 9.1, [], {}, '9' * 21, '9 09',
                   '+909', '../9090001', REAL_MERCHANTS[0], '٩٠٩', '９０９']
        before = self.stores()
        for value in invalid:
            with self.subTest(value=value):
                response = self.patch_store('esfihas-44', {'portal_id': value})
                self.assertEqual(response.status_code, 422, response.text)
                self.assertEqual(self.stores(), before)
        self.assertEqual(self.patch_store('unknown-store', {'portal_id': PORTAL_IDS[0]}).status_code, 404)

    def test_duplicate_portal_id_rolls_back_the_whole_patch_and_survives_restart(self):
        self.assertEqual(self.patch_store('esfihas-44', {'portal_id': PORTAL_IDS[0]}).status_code, 200)
        self.assertEqual(self.patch_store('maktub-esfihas', {'portal_id': PORTAL_IDS[2]}).status_code, 200)
        before = self.stores()
        response = self.patch_store('maktub-esfihas', {
            'portal_id': PORTAL_IDS[0], 'merchant_id': REAL_MERCHANTS[2]})
        self.assertEqual(response.status_code, 409, response.text)
        self.assertEqual(self.stores(), before)
        restarted = create_app(data_dir=self.data_dir,
                               admin_password_hash=self.password_hash, secure_cookie=False)
        other_client = TestClient(restarted)
        try:
            login = other_client.post('/api/admin/login', json={'password': self.password})
            self.assertEqual(other_client.get('/api/admin/ifood/stores').json(), before)
            headers = {'X-Sahara-CSRF': login.json()['csrf_token']}
            response = other_client.patch('/api/admin/ifood/stores/sahara-esfihas-shawarma',
                json={'portal_id': PORTAL_IDS[0]}, headers=headers)
            self.assertEqual(response.status_code, 409)
            response = other_client.patch('/api/admin/ifood/stores/esfihas-44',
                json={'portal_id': None}, headers=headers)
            self.assertEqual(response.status_code, 200)
            response = other_client.patch('/api/admin/ifood/stores/sahara-esfihas-shawarma',
                json={'portal_id': PORTAL_IDS[0]}, headers=headers)
            self.assertEqual(response.status_code, 200)
            self.assertIsNone(response.json()['stores'][0]['portal_id'])
            self.assertEqual(response.json()['stores'][1]['portal_id'], PORTAL_IDS[0])
            self.assertEqual(response.json()['stores'][2]['portal_id'], PORTAL_IDS[2])
            self.assert_pending(response.json())
        finally:
            other_client.close()

    def test_migration_adds_portal_ids_without_resetting_existing_merchant_uuid(self):
        legacy_dir = self.data_dir / 'legacy'
        legacy_dir.mkdir()
        with sqlite3.connect(legacy_dir / 'sahara.sqlite3') as conn:
            conn.execute('''CREATE TABLE ifood_stores (
                id TEXT PRIMARY KEY, name TEXT NOT NULL, merchant_id TEXT UNIQUE,
                catalog_state TEXT NOT NULL DEFAULT 'pending' CHECK(catalog_state='pending'))''')
            conn.execute('INSERT INTO ifood_stores(id,name,merchant_id) VALUES(?,?,?)',
                         ('esfihas-44', 'Esfihas 44', REAL_MERCHANTS[0]))
        legacy_app = create_app(data_dir=legacy_dir,
                                admin_password_hash=self.password_hash, secure_cookie=False)
        legacy_client = TestClient(legacy_app)
        try:
            login = legacy_client.post('/api/admin/login', json={'password': self.password})
            dashboard = legacy_client.get('/api/admin/ifood/stores').json()
            self.assertEqual(dashboard['stores'][0]['merchant_id'], REAL_MERCHANTS[0])
            self.assertTrue(all(store['portal_id'] is None for store in dashboard['stores']))
            self.assertEqual([store['name'] for store in dashboard['stores']],
                             ['Esfihas 44', 'Sahara Esfihas e Shawarma', 'Maktub Esfihas'])
            self.assert_pending(dashboard)
            response = legacy_client.patch('/api/admin/ifood/stores/esfihas-44',
                json={'portal_id': PORTAL_IDS[0]},
                headers={'X-Sahara-CSRF': login.json()['csrf_token']})
            self.assertEqual(response.status_code, 200, response.text)
            self.assertEqual(response.json()['stores'][0]['merchant_id'], REAL_MERCHANTS[0])
            self.assertEqual(response.json()['stores'][0]['portal_id'], PORTAL_IDS[0])
        finally:
            legacy_client.close()
