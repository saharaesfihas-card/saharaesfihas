"""Short customer URL, safe static publication and unchanged private transport origin."""
import os
from pathlib import Path
import subprocess
import unittest
from unittest.mock import patch

from server import evolution, whatsapp
from server.core import ROOT, db
from server.main import create_app
from fastapi.testclient import TestClient
from server.tests import test_whatsapp_ai as fixtures


class MenuURLTests(unittest.TestCase):
    setUpClass = classmethod(fixtures.DeliveryAITests.setUpClass.__func__)
    setUp = fixtures.DeliveryAITests.setUp
    tearDown = fixtures.DeliveryAITests.tearDown

    def test_root_and_legacy_index_keep_query_and_customer_assets(self):
        root = self.client.get('/')
        self.assertEqual(root.status_code, 200)
        self.assertEqual(root.content, (ROOT / 'index.html').read_bytes())
        response = self.client.get('/index.html?app=1&origem=whatsapp', follow_redirects=False)
        self.assertEqual(response.status_code, 308)
        self.assertEqual(response.headers['location'], '/?app=1&origem=whatsapp')
        followed = self.client.get('/index.html?app=1')
        self.assertEqual(followed.status_code, 200); self.assertEqual(followed.url.path, '/')

    def test_separate_menu_does_not_move_evolution_api_origin_or_webhook(self):
        backend = 'https://sahara-esfihas-pdv.onrender.com'
        menu = 'https://sahara-esfihas-cardapio.onrender.com'
        with patch.dict(os.environ, {'SAHARA_PUBLIC_URL': backend, 'SAHARA_MENU_URL': menu + '/index.html/'}):
            cfg = whatsapp.config()
            self.assertEqual(cfg['menu'], menu); self.assertEqual(cfg['public_url'], backend)
            for route in ['/', '/index.html']:
                response = self.client.get(route + '?app=1', follow_redirects=False)
                self.assertEqual(response.headers['location'], menu + '?app=1')
            with patch.object(evolution, 'connection', return_value='open'), patch.object(evolution, 'api') as transport:
                evolution.connect(cfg)
            self.assertEqual(transport.call_args.args[2]['webhook']['url'], backend + '/api/whatsapp/evolution/webhook')

    def test_queued_old_menu_link_is_shortened_without_losing_image_or_query(self):
        with db(self.request) as conn:
            cfg = whatsapp.config()
            original = cfg['public_url'] + '/index.html?origem=whatsapp'
            whatsapp.queue(conn, 'legacy-menu', self.phone, 'menu_image', 'Peça pelo site: ' + original)
            row = conn.execute("SELECT * FROM whatsapp_outbox WHERE event_key='legacy-menu'").fetchone()
            payload, _, _ = whatsapp.payload(conn, row, cfg)
            self.assertIn(cfg['menu'] + '?origem=whatsapp', payload['caption'])
            self.assertNotIn('/index.html', payload['caption'])
            self.assertEqual(payload['media'], cfg['menu'] + whatsapp.MENU_IMAGE)

    def test_static_build_publishes_only_customer_files(self):
        subprocess.run(['node', str(ROOT / 'server/build-public-site.cjs')], check=True, capture_output=True)
        output = ROOT / '.render-cardapio-public'
        self.assertEqual((output / 'index.html').read_bytes(), (ROOT / 'index.html').read_bytes())
        self.assertEqual((output / 'system-config.js').read_bytes(), (ROOT / 'system-config.js').read_bytes())
        self.assertTrue((output / whatsapp.MENU_IMAGE.lstrip('/')).is_file())
        self.assertEqual({p.name for p in output.iterdir() if p.is_dir()}, {'images', 'icons'})
        for forbidden in ['admin.html', 'admin.js', 'admin-order-alerts.js', 'server', '.git', '.env', 'downloads']:
            self.assertFalse((output / forbidden).exists(), forbidden)

    def test_static_menu_origin_can_load_prices_and_submit_validated_orders(self):
        origin = 'https://sahara-esfihas-cardapio.onrender.com'
        with patch.dict(os.environ, {'SAHARA_ALLOWED_ORIGINS': 'https://saharaesfihas-card.github.io,' + origin}):
            app = create_app(data_dir=Path(self.directory.name) / 'cors-check', admin_password_hash=self.password_hash, secure_cookie=False)
        with TestClient(app) as client:
            preflight = client.options('/api/orders', headers={'Origin': origin, 'Access-Control-Request-Method': 'POST', 'Access-Control-Request-Headers': 'content-type'})
            self.assertEqual(preflight.status_code, 200)
            self.assertEqual(preflight.headers['access-control-allow-origin'], origin)
            response = client.get('/api/catalog', headers={'Origin': origin})
            self.assertEqual(response.headers['access-control-allow-origin'], origin)
            self.assertEqual(next(p for p in response.json()['products'] if p['id'] == 'kitkat')['price_cents'], 550)
            response = client.post('/api/orders', json={}, headers={'Origin': origin})
            self.assertEqual(response.status_code, 422)
            self.assertEqual(response.headers['access-control-allow-origin'], origin)
