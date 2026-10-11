"""Private delivery origin stays internal; all transports are simulated."""
import json
import math
import unittest
from unittest.mock import patch

from server import customer_privacy as privacy, delivery_estimates as delivery, whatsapp, whatsapp_ai as ai
from server.core import db
from server.tests import test_whatsapp_ai as fixtures


class CustomerPrivacyTests(unittest.TestCase):
    setUpClass = classmethod(fixtures.DeliveryAITests.setUpClass.__func__)
    tearDown = fixtures.DeliveryAITests.tearDown
    inbound = fixtures.DeliveryAITests.inbound
    rows = fixtures.DeliveryAITests.rows
    origin = {'latitude': -23.4, 'longitude': -51.9}

    def setUp(self):
        fixtures.DeliveryAITests.setUp(self)
        with db(self.request) as conn:
            whatsapp.put_setting(conn, 'delivery_estimates', json.dumps({
                'address': 'Rua Reservada, 112 — Maringá, PR', 'origin': self.origin,
                'five_to_six': 'handoff'}))

    def test_store_location_requests_use_local_delivery_only_reply_without_ai(self):
        with patch.object(ai, 'interpret') as model:
            for question in ('Onde fica a loja?', 'Onde vocês ficam?', 'Qual o endereço?',
                             'Qual seu endereço?', 'Me manda a localização', 'Mande o pino da Sahara',
                             'Você fica em que rua?', 'Posso retirar na loja?'):
                self.inbound(question)
                self.assertEqual(self.rows('whatsapp_outbox')[-1]['body'], privacy.response())
            self.assertEqual(self.rows('whatsapp_ai_jobs'), [])
            model.assert_not_called()
        for text in ('Rua do cliente, 23', 'Meu endereço é Rua teste', 'Quanto tempo a Lua demora?'):
            self.assertFalse(privacy.store_question(text))

    def test_model_cannot_disclose_configured_street_or_origin_coordinates(self):
        with db(self.request) as conn:
            for reply in ('Estamos na Rua Reservada, 112.', 'A rua é Reservada.',
                          'Latitude -23.400000 e longitude -51.900000.', 'O ponto é -23,40, -51,90.'):
                result = {'intent': 'conversation', 'product_ids': [], 'reply': reply}
                self.assertEqual(ai.render(conn, self.phone, result), privacy.response())
            result = {'intent': 'products', 'product_ids': ['abacaxi-canela'],
                      'reply': '{{name:abacaxi-canela}} custa {{price:abacaxi-canela}}.'}
            self.assertEqual(ai.render(conn, self.phone, result), 'Abacaxi com canela custa R$ 6,00.')

    def test_existing_queued_reply_is_protected_for_evolution_and_meta(self):
        self.inbound('Oi')
        private = 'Estamos na Rua Reservada, 112.'
        with db(self.request) as conn:
            whatsapp.queue(conn, 'privacy-check', self.phone, 'ai_reply', private)
            row = conn.execute("SELECT * FROM whatsapp_outbox WHERE event_key='privacy-check'").fetchone()
            self.assertEqual(row['body'], privacy.response())
            # Simulate a reply generated before the new protection was published.
            conn.execute('UPDATE whatsapp_outbox SET body=? WHERE id=?', (private, row['id']))
            row = conn.execute('SELECT * FROM whatsapp_outbox WHERE id=?', (row['id'],)).fetchone()
            for provider in ('evolution', 'meta'):
                data, status, _ = whatsapp.payload(conn, row, whatsapp.config() | {'provider': provider})
                self.assertIsNone(status)
                text = data['text'] if provider == 'evolution' else data['text']['body']
                self.assertEqual(text, privacy.response())
            # A customer's own checkout address must still be shown for review.
            whatsapp.queue(conn, 'customer-review', self.phone, 'checkout_reply', private)
            row = conn.execute("SELECT * FROM whatsapp_outbox WHERE event_key='customer-review'").fetchone()
            data, _, _ = whatsapp.payload(conn, row, whatsapp.config())
            self.assertEqual(data['text'], private)

    def test_origin_stays_private_and_delivery_radius_still_works(self):
        self.assertEqual(self.client.get('/api/admin/whatsapp').status_code, 401)
        self.assertEqual(self.client.get('/server/delivery_estimates.py').status_code, 404)
        for old in ('/images/cardapio-panfleto-20261011.png',
                    '/images/cardapio-panfleto-20261011-abacaxi.png'):
            self.assertEqual(self.client.get(old).status_code, 404)
        self.assertEqual(self.client.get(whatsapp.MENU_IMAGE).status_code, 200)
        with db(self.request) as conn:
            point = self.origin | {'latitude': self.origin['latitude'] + math.degrees(2 / 6371.0088)}
            reply = delivery.response(conn, self.phone, location=point)
            self.assertIn('30 a 40 minutos', reply)
            self.assertFalse(privacy.contains_origin(conn, reply))
            self.assertEqual(delivery.config(conn)['origin'], self.origin)


if __name__ == '__main__':
    unittest.main()
