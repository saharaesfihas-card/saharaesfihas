"""Preparação da conta nova não autentica nem substitui o importador existente."""
import json
import os
from pathlib import Path
import sqlite3
from tempfile import TemporaryDirectory
import unittest
from unittest.mock import patch

from fastapi.testclient import TestClient

from server import ifood, ifood_homologation_profile as profile, ifood_stores
from server.core import hash_password
from server.main import create_app

MERCHANT = '22222222-2222-4222-8222-222222222222'
SECRET = 'fictional-new-account-secret'
PATH = '/api/admin/ifood/homologation-profile'


class IFoodHomologationProfileTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.password = 'disposable-profile-password'
        cls.password_hash = hash_password(cls.password)

    def setUp(self):
        self.directory = TemporaryDirectory()
        self.env = patch.dict(os.environ, {
            **dict.fromkeys(profile.REQUIRED, ''),
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
        self.client.post('/api/admin/login', json={'password': self.password})

    def tearDown(self):
        self.client.close()
        self.env.stop()
        self.directory.cleanup()

    def configure(self, client_id=profile.CLIENT_ID, secret=SECRET, merchant_id=MERCHANT):
        os.environ.update(dict(zip(profile.REQUIRED, (client_id, secret, merchant_id))))

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


if __name__ == '__main__':
    unittest.main()
