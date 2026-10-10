"""Welcome + real flyer, signed events and fake transports, no actual messages."""
import os
import time
import unittest
from unittest.mock import patch
from urllib.error import HTTPError, URLError

from server import evolution, whatsapp, whatsapp_ai as ai
from server.core import db
from server.tests import test_whatsapp_checkout as checkout_fixtures
from server.tests import test_whatsapp as meta_fixtures
from server.main import create_app
from pathlib import Path


class WelcomeMenuTests(unittest.TestCase):
    setUpClass = classmethod(checkout_fixtures.WhatsAppCheckoutTests.setUpClass.__func__)
    inbound = checkout_fixtures.WhatsAppCheckoutTests.inbound
    rows = checkout_fixtures.WhatsAppCheckoutTests.rows
    say = checkout_fixtures.WhatsAppCheckoutTests.say
    cart = checkout_fixtures.WhatsAppCheckoutTests.cart
    review = checkout_fixtures.WhatsAppCheckoutTests.review

    def setUp(self):
        checkout_fixtures.WhatsAppCheckoutTests.setUp(self)
        self.welcome = patch.dict(os.environ, {'SAHARA_WHATSAPP_WELCOME_MENU': '1'})
        self.welcome.start()

    def tearDown(self):
        self.welcome.stop()
        checkout_fixtures.WhatsAppCheckoutTests.tearDown(self)

    def media(self):
        return [r for r in self.rows('whatsapp_outbox') if r['kind'] == 'menu_image']

    def connected(self):
        with db(self.request) as conn:
            whatsapp.cache_evolution_connection(conn, whatsapp.config(), 'open')

    def test_first_greeting_queues_welcome_then_image_without_gemini_and_replay(self):
        with patch.object(ai, 'interpret') as model:
            self.inbound('Olá', identifier='first')
            self.inbound('Olá', identifier='first')
            model.assert_not_called()
        rows = self.rows('whatsapp_outbox')
        self.assertEqual([r['kind'] for r in rows], ['welcome', 'menu_image'])
        self.assertEqual(rows[1]['after_event'], rows[0]['event_key'])
        self.assertEqual(self.rows('whatsapp_ai_jobs'), [])
        with patch.dict(os.environ):
            os.environ.pop('SAHARA_WHATSAPP_WELCOME_MENU')
            self.assertTrue(whatsapp.config()['welcome_menu'])

    def test_image_transport_only_follows_accepted_greeting(self):
        self.inbound('Oi')
        self.connected()
        with patch.object(evolution, 'api', side_effect=[{'key': {'id': 'greeting'}}, {'key': {'id': 'flyer'}}]) as api:
            self.assertTrue(whatsapp.process_one(self.app))
            self.assertTrue(whatsapp.process_one(self.app))
            self.assertFalse(whatsapp.process_one(self.app))
        self.assertEqual(api.call_args_list[0].args[1], '/message/sendText/sahara')
        self.assertEqual(api.call_args_list[1].args[1], '/message/sendMedia/sahara')
        data = api.call_args_list[1].args[2]
        self.assertEqual(data['number'], self.phone)
        self.assertEqual(data['mediatype'], 'image')
        self.assertEqual(data['mimetype'], 'image/png')
        self.assertTrue(data['media'].endswith(whatsapp.MENU_IMAGE))
        self.assertIn('Peça pelo site:', data['caption'])
        self.assertIn(whatsapp.config()['menu'], data['caption'])
        self.assertEqual([r['status'] for r in self.rows('whatsapp_outbox')], ['accepted', 'accepted'])

    def test_second_greeting_does_not_repeat_image_but_cardapio_does(self):
        self.inbound('Oi')
        self.inbound('Boa noite')
        self.assertEqual(len(self.media()), 1)
        self.inbound('CARDÁPIO')
        self.assertEqual(len(self.media()), 2)
        self.assertEqual(self.media()[-1]['after_event'], '')
        self.assertEqual(self.rows('whatsapp_ai_jobs'), [])

    def test_return_after_inactivity_sends_again_but_legacy_phone_is_same_contact(self):
        self.inbound('Oi')
        with db(self.request) as conn:
            conn.execute('UPDATE whatsapp_contacts SET last_inbound=?', (int(time.time()) - 90000,))
        self.inbound('Olá')
        self.assertEqual(len(self.media()), 2)
        with db(self.request) as conn:
            conn.execute('DELETE FROM whatsapp_contacts')
            conn.execute('INSERT INTO whatsapp_contacts VALUES(?,?,?,?)', (self.phone[:4] + self.phone[5:], int(time.time()), 0, 0))
        self.inbound('Olá')
        self.assertEqual(len(self.media()), 2)

    def test_first_real_question_is_still_answered(self):
        self.inbound('Quanto custa a esfiha de carne?')
        self.assertEqual(len(self.media()), 1)
        with patch.object(ai, 'interpret', return_value={'intent': 'products', 'product_ids': ['carne']}):
            self.assertTrue(ai.process_one(self.app))
        reply = self.rows('whatsapp_outbox')[-1]
        self.assertEqual(reply['kind'], 'ai_reply')
        self.assertIn('Carne', reply['body'])
        self.assertIn('R$ 4,00', reply['body'])

    def test_first_message_can_start_order_and_confirmation_still_registers_once(self):
        self.review()
        self.assertEqual(len(self.media()), 1)
        self.say('CONFIRMAR PEDIDO')
        self.say('CONFIRMAR PEDIDO')
        self.assertEqual(len(self.rows('orders')), 1)
        self.assertEqual(self.rows('orders')[0]['total_cents'], 1200)
        self.assertEqual(self.rows('orders')[0]['source'], 'whatsapp')

    def test_human_pause_and_stop_do_not_send_menu_and_menu_can_resume(self):
        self.inbound('ATENDENTE')
        self.inbound('Oi')
        self.assertEqual(self.media(), [])
        self.inbound('MENU')
        self.assertEqual(len(self.media()), 1)
        self.inbound('PARAR', phone='5511999999999')
        self.assertFalse(any(r['phone'] == '5511999999999' for r in self.media()))

    def test_handoff_cancels_queued_welcome_and_image(self):
        self.inbound('Oi')
        self.inbound('ATENDENTE')
        self.connected()
        with patch.object(whatsapp, 'send') as sender:
            self.assertTrue(whatsapp.process_one(self.app))
            self.assertTrue(whatsapp.process_one(self.app))
            sender.assert_not_called()
        self.assertEqual(self.media()[0]['status'], 'skipped')

    def test_rate_limit_waits_for_greeting_retry_and_uncertain_greeting_skips_image(self):
        self.inbound('Oi')
        self.connected()
        with patch.object(whatsapp, 'send', side_effect=HTTPError('https://evolution.example', 429, 'limit', {}, None)):
            self.assertTrue(whatsapp.process_one(self.app))
        with patch.object(whatsapp, 'send') as sender:
            self.assertFalse(whatsapp.process_one(self.app))
            sender.assert_not_called()
        with db(self.request) as conn:
            conn.execute("UPDATE whatsapp_outbox SET next_attempt=0 WHERE kind='welcome'")
        with patch.object(whatsapp, 'send', side_effect=URLError('no confirmation')) as sender:
            self.assertTrue(whatsapp.process_one(self.app))
            self.assertTrue(whatsapp.process_one(self.app))
            self.assertEqual(sender.call_count, 1)
        self.assertEqual(self.media()[0]['status'], 'skipped')

    def test_old_synchronized_events_do_not_send_menu(self):
        self.inbound('Oi', timestamp=int(time.time()) - 90000)
        self.assertEqual(self.rows('whatsapp_outbox'), [])

    def test_real_flyer_is_public_png_and_meta_payload_uses_image(self):
        response = self.client.get(whatsapp.MENU_IMAGE)
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.headers['content-type'], 'image/png')
        self.assertTrue(response.content.startswith(b'\x89PNG\r\n\x1a\n'))
        self.inbound('Oi')
        with db(self.request) as conn:
            conn.execute("UPDATE whatsapp_outbox SET status='accepted' WHERE kind='welcome'")
            cfg = whatsapp.config() | {'provider': 'meta'}
            row = conn.execute("SELECT * FROM whatsapp_outbox WHERE kind='menu_image'").fetchone()
            data, status, _ = whatsapp.payload(conn, row, cfg)
        self.assertIsNone(status)
        self.assertEqual(data['type'], 'image')
        self.assertTrue(data['image']['link'].endswith(whatsapp.MENU_IMAGE))

    def test_meta_webhook_without_ai_also_sends_first_menu_once(self):
        with patch.dict(os.environ, {
            'SAHARA_WHATSAPP_PROVIDER': 'meta', 'SAHARA_AI_ENABLED': '0',
            'SAHARA_WHATSAPP_ACCESS_TOKEN': 'test-token', 'SAHARA_WHATSAPP_PHONE_NUMBER_ID': '12345',
            'SAHARA_WHATSAPP_APP_SECRET': 'test-secret', 'SAHARA_WHATSAPP_VERIFY_TOKEN': 'test-verify',
        }):
            event = meta_fixtures.WhatsAppTests.message(self, 'Olá')
            for _ in range(2):
                self.assertEqual(meta_fixtures.WhatsAppTests.webhook(self, event).status_code, 200)
            self.assertEqual([r['kind'] for r in self.rows('whatsapp_outbox')], ['welcome', 'menu_image'])
            self.assertEqual(self.rows('whatsapp_ai_jobs'), [])

    def test_migration_keeps_old_queued_messages_and_is_repeatable(self):
        with db(self.request) as conn:
            whatsapp.queue(conn, 'old-queue', self.phone, 'reply', 'Mensagem existente')
            conn.execute('ALTER TABLE whatsapp_outbox DROP COLUMN after_event')
        for _ in range(2):
            create_app(data_dir=Path(self.directory.name) / 'data', admin_password_hash=self.password_hash)
        row = self.rows('whatsapp_outbox')[0]
        self.assertEqual(row['event_key'], 'old-queue')
        self.assertEqual(row['body'], 'Mensagem existente')
        self.assertEqual(row['after_event'], '')


if __name__ == '__main__':
    unittest.main()
