"""Delivery policy, real signed locations and human fallback; no real sends."""
import json
import math
import time
import unittest
from unittest.mock import patch
from uuid import uuid4

from server import delivery_estimates as delivery, evolution, whatsapp, whatsapp_ai as ai, whatsapp_checkout as checkout
from server.core import db
from server.main import create_app
from server.tests import test_whatsapp_ai as fixtures


class DeliveryEstimateTests(unittest.TestCase):
    setUpClass = classmethod(fixtures.DeliveryAITests.setUpClass.__func__)
    setUp = fixtures.DeliveryAITests.setUp
    tearDown = fixtures.DeliveryAITests.tearDown
    inbound = fixtures.DeliveryAITests.inbound
    rows = fixtures.DeliveryAITests.rows

    origin = {'latitude': -23.4656, 'longitude': -51.9108}  # fictional test reference

    def configure(self, gap='handoff'):
        with db(self.request) as conn:
            whatsapp.put_setting(conn, 'delivery_estimates', json.dumps({'origin': self.origin, 'five_to_six': gap}))

    def destination(self, km):
        return self.origin | {'latitude': self.origin['latitude'] + math.degrees(km / 6371.0088)}

    def location(self, point=None, identifier=None, timestamp=None, wrapper=False, from_me=False):
        position = point or self.destination(2)
        message = {'locationMessage': {'degreesLatitude': position['latitude'], 'degreesLongitude': position['longitude']}}
        if wrapper:
            message = {'ephemeralMessage': {'message': message}}
        data = {'key': {'remoteJid': self.phone + '@s.whatsapp.net', 'id': identifier or uuid4().hex, 'fromMe': from_me},
                'messageTimestamp': timestamp or int(time.time()), 'message': message}
        response = self.client.post('/api/whatsapp/evolution/webhook',
                                    json={'event': 'messages.upsert', 'instance': 'sahara', 'data': data},
                                    headers={'X-Sahara-Webhook-Secret': 'webhook-private-test'})
        self.assertEqual(response.status_code, 200, response.text)

    def last_reply(self):
        return self.rows('whatsapp_outbox')[-1]['body']

    def test_exact_policy_boundaries_and_unconfigured_gap(self):
        for km, expected in [(0.5, '30 a 40 minutos'), (1, '30 a 40 minutos'), (3, '30 a 40 minutos'),
                             (3.001, '40 a 50 minutos'), (4, '40 a 50 minutos'), (4.001, '50 a 60 minutos'),
                             (5, '50 a 60 minutos'), (5.001, None), (6, None), (6.001, '1 hora e 10 minutos')]:
            with self.subTest(km=km):
                self.assertEqual(delivery.minutes(km), expected)
        self.assertEqual(delivery.minutes(5.5, '70'), '1 hora e 10 minutos')
        self.assertEqual(delivery.minutes(6, '50_60'), '50 a 60 minutos')
        for value in (-1, math.inf, math.nan):
            self.assertIsNone(delivery.minutes(value))

    def test_native_location_computes_real_radius_without_ai_and_deduplicates(self):
        self.configure()
        identifier = uuid4().hex
        with patch.object(ai, 'interpret') as model:
            self.location(identifier=identifier, wrapper=True)
            self.location(identifier=identifier, wrapper=True)
            model.assert_not_called()
        self.assertEqual(len(self.rows('whatsapp_outbox')), 1)
        self.assertIn('2,0 km', self.last_reply())
        self.assertIn('30 a 40 minutos', self.last_reply())
        self.inbound('Qual o prazo de entrega?')
        self.assertIn('30 a 40 minutos', self.last_reply())
        self.assertEqual(self.rows('whatsapp_ai_jobs'), [])
        self.assertEqual(self.rows('orders'), [])

    def test_calculated_radius_preserves_band_boundaries(self):
        for km in (1, 3, 4, 5, 6):
            self.assertEqual(delivery.radius(self.origin, self.destination(km)), km)

    def test_missing_store_pin_hands_off_and_handoff_message_can_be_sent(self):
        self.location()
        self.assertIn('atendente', self.last_reply())
        self.assertNotIn('30 a 40 minutos', self.last_reply())
        self.assertGreater(self.rows('whatsapp_contacts')[0]['human_until'], time.time())
        self.assertEqual(self.rows('whatsapp_outbox')[0]['kind'], 'reply')
        with patch.object(evolution, 'connection', return_value='open'), patch.object(evolution, 'api', return_value={'key': {'id': 'estimate-test'}}):
            self.assertTrue(whatsapp.process_one(self.app))
        self.assertEqual(self.rows('whatsapp_outbox')[0]['status'], 'accepted')

    def test_unlocated_address_hands_off_instead_of_using_previous_point(self):
        self.configure()
        self.location()
        self.inbound('Qual o prazo para Rua Outra, 900, Centro?')
        self.assertIn('Não consegui', self.last_reply())
        self.assertNotIn('30 a 40 minutos', self.last_reply())
        with db(self.request) as conn:
            self.assertIsNone(delivery.state(conn, self.phone)['point_json'])

    def test_delay_of_existing_order_does_not_get_a_new_delivery_promise(self):
        self.configure()
        self.location()
        self.inbound('Meu pedido está atrasado, quando chega?')
        self.assertIn('atendente', self.last_reply())
        self.assertNotIn('30 a 40 minutos', self.last_reply())

    def test_delivery_question_then_manual_address_goes_to_human(self):
        self.configure()
        self.inbound('Qual o prazo de entrega?')
        self.assertIn('envie a localização', self.last_reply())
        self.inbound('Rua Teste, 100')
        self.assertIn('estipular o prazo', self.last_reply())
        count = len(self.rows('whatsapp_outbox'))
        self.inbound('Tenho outra pergunta')
        self.location()
        self.assertEqual(len(self.rows('whatsapp_outbox')), count)
        self.inbound('MENU')
        self.location()
        self.assertIn('30 a 40 minutos', self.last_reply())

    def test_map_pin_accepted_but_viewport_and_untrusted_links_never_guessed(self):
        self.configure()
        position = self.destination(4.5)
        self.inbound(f"https://www.google.com/maps?q={position['latitude']},{position['longitude']}")
        self.assertIn('50 a 60 minutos', self.last_reply())
        self.assertIsNone(delivery.map_point('https://www.google.com/maps/@-23.4,-51.9,15z'))
        self.assertIsNone(delivery.map_point('https://maps.app.goo.gl/short'))
        self.assertIsNone(delivery.map_point('https://example.com/?q=-23.4,-51.9'))
        self.assertIsNone(delivery.map_point('https://google.com@localhost/?q=-23.4,-51.9'))
        self.assertIsNotNone(delivery.map_point('https://www.google.com/maps/place/Loja/data=!3d-23.4!4d-51.9'))

    def test_invalid_coordinates_and_inaccurate_site_point_not_used(self):
        for position in ({'latitude': True, 'longitude': -51}, {'latitude': math.nan, 'longitude': -51},
                         {'latitude': 91, 'longitude': -51}, {'latitude': -23, 'longitude': 181},
                         self.origin | {'accuracy': 101}, self.origin | {'accuracy': -1}):
            self.assertIsNone(delivery.point(position))

    def test_old_and_own_location_messages_are_not_processed(self):
        self.configure()
        self.location(timestamp=int(time.time()) - 86401)
        self.location(from_me=True)
        self.assertEqual(self.rows('whatsapp_outbox'), [])
        self.assertEqual(self.rows('whatsapp_delivery_points'), [])

    def test_conditional_customer_distance_uses_policy_without_claiming_location(self):
        self.inbound('Estou a 4 km da loja, qual o prazo de entrega?')
        self.assertIn('raio informado', self.last_reply())
        self.assertIn('40 a 50 minutos', self.last_reply())
        self.assertNotIn('raio aproximado', self.last_reply())
        self.inbound('Estou a 5,5 km, qual o prazo?')
        self.assertIn('estipular o prazo', self.last_reply())

    def test_gps_cart_collects_house_number_and_confirms_explicitly(self):
        self.configure()
        for message in ('PEDIR', '2 carne', 'FINALIZAR', 'Cliente teste'):
            self.inbound(message)
        self.location()
        with db(self.request) as conn:
            cart = checkout.load(conn, self.phone)
            self.assertEqual(cart['stage'], 'number')
        for message in ('15', 'SEM COMPLEMENTO', 'SEM OBSERVAÇÃO', 'PIX'):
            self.inbound(message)
        self.assertIn('30 a 40 minutos', self.last_reply())
        self.assertEqual(self.rows('orders'), [])
        self.inbound('CONFIRMAR PEDIDO')
        orders = self.rows('orders')
        self.assertEqual(len(orders), 1)
        address = json.loads(orders[0]['delivery_json'])
        self.assertEqual(address['number'], '15')
        self.assertAlmostEqual(address['location']['latitude'], self.destination(2)['latitude'])

    def test_site_order_location_belongs_only_to_authenticated_sender(self):
        self.configure()
        response = self.client.post('/api/orders', json={
            'items': [{'id': 'carne', 'quantity': 1}], 'customer': {'phone': self.phone, 'name': 'Teste'},
            'delivery': {'number': '112', 'location': self.destination(4.5)},
            'idempotency_key': uuid4().hex})
        self.assertEqual(response.status_code, 201, response.text)
        self.inbound('Qual o prazo de entrega do meu pedido?')
        self.assertIn('#001', self.last_reply())
        self.assertIn('50 a 60 minutos', self.last_reply())
        self.inbound('Qual o prazo de entrega do meu pedido?', phone='5511988887777')
        self.assertNotIn('#001', self.last_reply())
        self.assertNotIn('raio aproximado', self.last_reply())

    def test_stale_point_not_used_and_general_timing_question_stays_with_ai(self):
        self.configure()
        self.location()
        with db(self.request) as conn:
            conn.execute('UPDATE whatsapp_delivery_points SET updated_at=?', (int(time.time()) - 86401,))
        self.inbound('Qual o prazo de entrega?')
        self.assertIn('envie a localização', self.last_reply())
        self.inbound('Quanto tempo a Lua leva para orbitar a Terra?')
        self.assertEqual(len(self.rows('whatsapp_ai_jobs')), 1)
        self.inbound('Qual a previsão do tempo para amanhã?')
        self.assertEqual(len(self.rows('whatsapp_ai_jobs')), 2)

    def test_admin_settings_require_login_csrf_and_reject_ambiguous_pin(self):
        url = '/api/admin/whatsapp/delivery'
        body = {'origin': self.origin, 'five_to_six': '70'}
        self.assertEqual(self.client.put(url, json=body).status_code, 401)
        login = self.client.post('/api/admin/login', json={'password': 'test-only-password'})
        headers = {'X-Sahara-CSRF': login.json()['csrf_token']}
        self.assertEqual(self.client.put(url, json=body).status_code, 403)
        saved = self.client.put(url, json=body, headers=headers)
        self.assertEqual(saved.status_code, 200, saved.text)
        self.assertEqual(saved.json()['origin'], self.origin)
        self.assertEqual(self.client.put(url, json={'map_url': 'https://www.google.com/maps/@-23.4,-51.9,15z'}, headers=headers).status_code, 422)
        with db(self.request) as conn:
            self.assertEqual(delivery.config(conn)['origin'], self.origin)
            self.assertEqual(delivery.config(conn)['five_to_six'], '70')

    def test_store_reference_and_conversation_point_survive_restart(self):
        self.configure('70')
        self.location()
        create_app(data_dir=self.app.state.db_path.parent, admin_password_hash=self.password_hash, secure_cookie=False)
        with db(self.request) as conn:
            self.assertEqual(delivery.config(conn)['origin'], self.origin)
            self.assertEqual(delivery.config(conn)['five_to_six'], '70')
            self.assertEqual(json.loads(delivery.state(conn, self.phone)['point_json']), self.destination(2))

    def test_ai_delivery_intent_uses_official_policy_and_queues_human_reply(self):
        self.inbound('Vocês enviam para minha casa?')
        with patch.object(ai, 'interpret', return_value={'intent': 'delivery', 'product_ids': [], 'reply': 'Entregamos em 5 minutos.'}) as model:
            self.assertTrue(ai.process_one(self.app))
            self.assertIn('30 a 40 minutos', model.call_args.args[2]['delivery_policy'])
        self.assertNotIn('5 minutos', self.last_reply())
        self.inbound('MENU')
        self.inbound('Pode enviar para Rua Teste, 50?')
        with patch.object(ai, 'interpret', return_value={'intent': 'delivery', 'product_ids': []}):
            self.assertTrue(ai.process_one(self.app))
        self.assertEqual(self.rows('whatsapp_outbox')[-1]['kind'], 'reply')
        self.assertIn('atendente', self.last_reply())
