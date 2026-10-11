"""Fee boundaries and transactional totals; temporary databases, no real messages."""
import json
import math
import unittest
from uuid import uuid4
from server import delivery_estimates as delivery, whatsapp
from server.core import db
from server.tests import test_core as core_fixtures, test_whatsapp_checkout as chat_fixtures


class DeliveryFeeTests(unittest.TestCase):
    setUpClass = classmethod(core_fixtures.CoreTests.setUpClass.__func__)
    setUp = core_fixtures.CoreTests.setUp
    tearDown = core_fixtures.CoreTests.tearDown
    body = core_fixtures.CoreTests.body
    order = core_fixtures.CoreTests.order
    connection = core_fixtures.CoreTests.connection
    origin = {'latitude': -23.4656, 'longitude': -51.9108}

    def configure(self, **changes):
        response = self.client.put('/api/admin/whatsapp/delivery', headers=self.headers,
            json={'origin': self.origin, 'fees_enabled': True} | changes)
        self.assertEqual(response.status_code, 200, response.text)

    def location(self, km):
        return self.origin | {'latitude': self.origin['latitude'] + math.degrees(km / 6371.0088)}

    def test_quote_script_is_public_without_exposing_server_code(self):
        self.assertEqual(self.client.get('/delivery-fees.js').status_code,200)
        self.assertEqual(self.client.get('/server/delivery_estimates.py').status_code,404)

    def test_all_boundaries(self):
        for km, amount in [(0, 0), (1, 0), (3, 0), (3.000001, 400), (4, 400), (4.01, 500),
                           (5, 500), (5.01, 800), (6, 800), (6.01, 1000), (7, 1000), (7.01, None)]:
            self.assertEqual(delivery.fee_cents(km), amount)
        self.assertIsNone(delivery.fee_cents(float('nan')))
        self.assertIsNone(delivery.fee_cents(-1))

    def test_private_origin_and_precise_quote(self):
        self.configure()
        for km, amount in [(3,0), (4,400), (5,500), (6,800), (7,1000), (8,None)]:
            r = self.client.post('/api/delivery/quote', json={'location': self.location(km)})
            self.assertEqual(r.status_code, 200)
            self.assertEqual(r.json()['fee']['amount_cents'], amount)
            self.assertNotIn(str(self.origin['longitude']), r.text)
            self.assertNotIn('origin', r.text)
        self.assertIsNone(self.client.post('/api/delivery/quote', json={'location': self.location(2) | {'accuracy': 500}}).json()['fee']['amount_cents'])

    def test_all_fees_included_web_and_replay_frozen(self):
        self.configure()
        for km, fee in [(2,0),(3.5,400),(4.5,500),(5.5,800),(6.5,1000)]:
            body = self.body(); body['delivery']['location'] = self.location(km)
            order = self.order(body)
            self.assertEqual(order['subtotal_cents'], 800)
            self.assertEqual(order['total_cents'], 800 + fee)
            self.assertEqual(order['delivery_fee']['amount_cents'], fee)
            self.configure(fees_enabled=False)
            replay = self.order(body)
            self.assertTrue(replay['replayed'])
            self.assertEqual(replay['total_cents'], 800 + fee)
            self.assertEqual(replay['delivery_fee'], order['delivery_fee'])
            self.configure()

    def test_unknown_fee_cannot_be_paid_until_staff_resolves(self):
        self.configure()
        for point in (None, self.location(8)):
            body = self.body()
            if point: body['delivery']['location'] = point
            order = self.order(body); oid = order['id']
            self.assertEqual(order['delivery_fee'], {'status':'pending','amount_cents':None})
            self.assertEqual(self.client.post(f'/api/admin/orders/{oid}/payment', json={'status':'paid'}, headers=self.headers).status_code, 409)
            self.assertEqual(self.client.put(f'/api/admin/orders/{oid}/delivery-fee', json={'amount_cents':1200}).status_code,403)
            self.assertEqual(self.client.put(f'/api/admin/orders/{oid}/delivery-fee', json={'amount_cents':-1}, headers=self.headers).status_code,422)
            resolved = self.client.put(f'/api/admin/orders/{oid}/delivery-fee', json={'amount_cents':1200}, headers=self.headers)
            self.assertEqual(resolved.status_code,200,resolved.text)
            self.assertEqual(resolved.json()['order']['total_cents'],2000)
            self.assertEqual(self.client.post(f'/api/admin/orders/{oid}/payment', json={'status':'paid'}, headers=self.headers).status_code,200)
            self.assertEqual(self.client.put(f'/api/admin/orders/{oid}/delivery-fee', json={'amount_cents':0}, headers=self.headers).status_code,409)

    def test_driver_receipt_includes_fee_and_full_total(self):
        self.configure()
        body = self.body(); body['delivery']['location'] = self.location(6.5)
        oid = self.order(body)['id']
        from server.core import order_dict
        from server.driver_dispatch import receipt
        with self.connection() as conn:
            order = order_dict(conn.execute('SELECT * FROM orders WHERE id=?',(oid,)).fetchone())
        order['maps_url'] = 'https://www.google.com/maps?q=-23.4,-51.9'
        text = receipt(order, {'name':'Entregador Teste'}, 0)
        self.assertIn('Taxa de entrega: R$ 10,00',text)
        self.assertIn('Total do pedido: R$ 18,00',text)
        self.assertIn('A RECEBER pelo pedido: R$ 18,00',text)

    def test_old_orders_unchanged_and_untrusted_fee_rejected(self):
        old = self.order()
        self.configure()
        replay = self.client.get('/api/admin/orders').json()['orders']
        self.assertEqual(replay[0]['total_cents'],800)
        self.assertNotIn('fee', replay[0]['delivery'])
        self.assertEqual(self.client.put(f"/api/admin/orders/{old['id']}/delivery-fee", json={'amount_cents':400}, headers=self.headers).status_code,409)
        body = self.body(); body['delivery']['fee'] = {'amount_cents':0}
        self.assertEqual(self.client.post('/api/orders',json=body).status_code,422)

    def test_coupon_applies_only_to_products_and_debt_requires_defined_fee(self):
        self.configure()
        coupon = self.client.post('/api/admin/coupons', headers=self.headers,
            json={'code':'FEE10','kind':'percent','value':10})
        self.assertEqual(coupon.status_code,201,coupon.text)
        body = self.body(coupon_code='FEE10'); body['delivery']['location'] = self.location(5.5)
        order = self.order(body)
        self.assertEqual(order['discount_cents'],80)
        self.assertEqual(order['total_cents'],1520)
        pending = self.order()
        debt = self.client.post('/api/admin/receivables', headers=self.headers,
            json={'customer_name':'Cliente Teste','phone':'44999999999','amount_cents':800,'order_id':pending['id']})
        self.assertEqual(debt.status_code,409,debt.text)

    def test_preserve_settings_and_above_seven_option(self):
        self.configure(above_seven='10')
        self.client.put('/api/admin/whatsapp/delivery', headers=self.headers, json={'five_to_six':'70'})
        response = self.client.post('/api/delivery/quote', json={'location':self.location(9)})
        self.assertEqual(response.json()['fee']['amount_cents'],1000)
        self.assertIn('R$ 10,00',response.json()['policy'])


class WhatsAppFeeTests(unittest.TestCase):
    setUpClass = classmethod(chat_fixtures.WhatsAppCheckoutTests.setUpClass.__func__)
    setUp = chat_fixtures.WhatsAppCheckoutTests.setUp
    tearDown = chat_fixtures.WhatsAppCheckoutTests.tearDown
    inbound = chat_fixtures.WhatsAppCheckoutTests.inbound
    rows = chat_fixtures.WhatsAppCheckoutTests.rows
    say = chat_fixtures.WhatsAppCheckoutTests.say
    cart = chat_fixtures.WhatsAppCheckoutTests.cart

    def configure(self):
        with db(self.request) as conn:
            whatsapp.put_setting(conn,'delivery_estimates',json.dumps({'origin': {'latitude':-23.4656,'longitude':-51.9108},'fees_enabled':True}))

    def test_fee_question_uses_official_policy(self):
        self.configure()
        reply = self.say('Qual é a taxa de entrega?')
        for amount in ['grátis', 'R$ 4,00', 'R$ 5,00', 'R$ 8,00', 'R$ 10,00']:
            self.assertIn(amount,reply)
        self.assertNotIn('-51.9108',reply)
        repeated = self.say('A entrega é grátis?')
        self.assertIn('R$ 4,00',repeated)
        self.assertNotIn('automático está pausado',repeated)

    def test_cart_fee_review_and_confirmation_after_changed_policy(self):
        self.configure()
        for text in ['PEDIR','2 carne','FINALIZAR','Cliente Teste','Rua Teste','5','Centro','SEM COMPLEMENTO','SEM OBSERVAÇÃO','PIX']:
            reply = self.say(text)
        self.assertIn('Total provisório',reply)
        self.assertIn('atendente confirma',reply)
        with db(self.request) as conn:
            from server import whatsapp_checkout as checkout
            cart = checkout.load(conn, self.phone)
            cart['data']['location'] = {'latitude':-23.430, 'longitude':-51.9108}
            checkout.save(conn,cart)
        reply = self.say('CONFIRMAR PEDIDO')
        self.assertIn('taxa de entrega mudou',reply)
        self.assertIn('R$ 4,00',reply)
        self.assertEqual(len(self.rows('orders')),0)
        reply = self.say('CONFIRMAR PEDIDO')
        self.assertIn('registrado no PDV',reply)
        order = self.rows('orders')[0]
        self.assertEqual(order['total_cents'],1200)
        self.assertEqual(json.loads(order['delivery_json'])['fee']['amount_cents'],400)
