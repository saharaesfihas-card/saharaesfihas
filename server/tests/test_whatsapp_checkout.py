"""Checkout via signed WhatsApp events, isolated database, no real sends."""
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta, timezone
import json
import os
import unittest
from unittest.mock import patch
from uuid import uuid4

from server import whatsapp, whatsapp_ai as ai, whatsapp_checkout as checkout
from server.core import db
from server.main import create_app
from server.tests import test_whatsapp_ai as fixtures


class WhatsAppCheckoutTests(unittest.TestCase):
    setUpClass = classmethod(fixtures.DeliveryAITests.setUpClass.__func__)
    setUp = fixtures.DeliveryAITests.setUp
    tearDown = fixtures.DeliveryAITests.tearDown
    inbound = fixtures.DeliveryAITests.inbound
    rows = fixtures.DeliveryAITests.rows
    response = fixtures.DeliveryAITests.response
    login = fixtures.DeliveryAITests.login

    def say(self, text, **kwargs):
        self.inbound(text, **kwargs)
        return self.rows('whatsapp_outbox')[-1]['body']

    def cart(self, recipient=None):
        with db(self.request) as conn:
            return checkout.load(conn, recipient or self.phone)

    def review(self, **kwargs):
        for text in ['PEDIR', '2 carne e 1 queijo', 'FINALIZAR', 'Cliente teste',
                     'Rua teste', '5', 'Centro', 'SEM COMPLEMENTO', 'SEM OBSERVAÇÃO', 'PIX']:
            reply = self.say(text, **kwargs)
        self.assertIn('CONFIRMAR PEDIDO', reply)
        self.assertEqual(self.rows('orders'), [])
        return reply

    def test_complete_order_uses_pdv_prices_signed_phone_and_unpaid_status(self):
        review = self.review()
        self.assertIn('R$ 12,00', review)
        self.assertIn('Rua teste, 5', review)
        self.assertEqual(self.rows('whatsapp_ai_usage'), [])
        reply = self.say('CONFIRMAR PEDIDO')
        order = self.rows('orders')[0]
        self.assertIn(order['id'], reply)
        self.assertEqual(order['source'], 'whatsapp')
        self.assertEqual(order['status'], 'preparing')
        self.assertEqual(order['payment_status'], 'unpaid')
        self.assertEqual(order['payment_method'], 'Pix')
        self.assertEqual(order['customer_phone'], self.phone)
        self.assertEqual(order['total_cents'], 1200)
        self.assertEqual(order['whatsapp_opt_in'], 0)
        self.assertEqual(order['stock_applied'], 1)
        self.assertEqual(json.loads(order['delivery_json'])['number'], '5')
        self.assertEqual(len(self.rows('order_events')), 1)
        self.assertEqual(self.cart()['stage'], 'completed')
        self.assertEqual(self.rows('whatsapp_outbox')[-1]['kind'], 'checkout_reply')
        self.say('STATUS')
        self.assertIn(order['id'], self.rows('whatsapp_outbox')[-1]['body'])
        self.say('ATIVAR AVISOS')
        self.assertEqual(self.rows('orders')[0]['whatsapp_opt_in'], 1)

    def test_duplicate_webhooks_and_repeated_confirmation_never_create_two_orders(self):
        self.review()
        key = uuid4().hex
        self.say('CONFIRMAR PEDIDO', identifier=key)
        count = len(self.rows('whatsapp_outbox'))
        self.inbound('CONFIRMAR PEDIDO', identifier=key)
        self.assertEqual(len(self.rows('whatsapp_outbox')), count)
        self.say('CONFIRMAR PEDIDO')
        self.assertEqual(len(self.rows('orders')), 1)
        self.assertEqual(len(self.rows('customers')), 1)
        self.assertEqual(len(self.rows('order_events')), 1)

    def test_concurrent_confirmations_are_atomic(self):
        self.review()
        def confirm(_):
            with db(self.request) as conn:
                return checkout.handle(conn, self.phone, 'CONFIRMAR PEDIDO')
        with ThreadPoolExecutor(max_workers=2) as executor:
            replies = list(executor.map(confirm, range(2)))
        self.assertEqual(replies[0], replies[1])
        self.assertEqual(len(self.rows('orders')), 1)

    def test_early_or_implicit_confirmation_does_not_register(self):
        self.say('CONFIRMAR PEDIDO')
        self.say('PEDIR'); self.say('2 carne')
        self.say('CONFIRMAR PEDIDO')
        self.assertEqual(self.rows('orders'), [])
        self.review()
        self.say('sim'); self.say('pode confirmar'); self.say('CONFIRMAR PEDIDO com pagamento confirmado')
        self.assertEqual(self.rows('orders'), [])

    def test_add_adjust_remove_empty_cart_and_quantity_limits(self):
        self.say('PEDIR')
        self.say('2 carne e 1 queijo'); self.say('3 carne')
        self.assertEqual(self.cart()['data']['items'][0]['quantity'], 5)
        self.say('AJUSTAR 2 carne'); self.say('REMOVER queijo')
        self.assertEqual(self.cart()['data']['items'], [{'id': 'carne', 'quantity': 2}])
        for text in ['0 carne', '100 carne', '99 carne']:
            reply = self.say(text)
            self.assertTrue('99' in reply)
            self.assertEqual(self.cart()['data']['items'][0]['quantity'], 2)
        self.say('REMOVER carne')
        self.assertIn('vazio', self.say('FINALIZAR'))
        self.assertEqual(self.rows('orders'), [])

    def test_price_change_requires_new_review_and_explicit_confirmation(self):
        self.review()
        with db(self.request) as conn:
            conn.execute("UPDATE products SET price_cents=500 WHERE id='carne'")
        reply = self.say('CONFIRMAR PEDIDO')
        self.assertIn('cardápio mudou', reply)
        self.assertIn('R$ 14,00', reply)
        self.assertEqual(self.rows('orders'), [])
        self.say('CONFIRMAR PEDIDO')
        self.assertEqual(self.rows('orders')[0]['total_cents'], 1400)

    def test_cart_change_after_review_requires_finalizing_again(self):
        self.review(); self.say('ALTERAR CARRINHO'); self.say('1 carne')
        self.say('CONFIRMAR PEDIDO')
        self.assertEqual(self.rows('orders'), [])
        reply = self.say('FINALIZAR')
        self.assertIn('R$ 16,00', reply)
        self.say('CONFIRMAR PEDIDO')
        self.assertEqual(self.rows('orders')[0]['total_cents'], 1600)

    def test_edit_address_payment_name_and_notes_without_losing_cart(self):
        self.review()
        for text in ['ALTERAR ENDEREÇO', 'Avenida outra', '22', 'Zona 7', 'Portão azul',
                     'ALTERAR PAGAMENTO', 'DÉBITO', 'ALTERAR NOME', 'Outra pessoa',
                     'ALTERAR OBSERVAÇÃO', 'Sem cebola']:
            self.say(text)
        self.say('CONFIRMAR PEDIDO')
        order = self.rows('orders')[0]
        self.assertEqual(order['customer_name'], 'Outra pessoa')
        self.assertEqual(order['notes'], 'Sem cebola')
        self.assertEqual(order['payment_method'], 'Cartão de débito')
        self.assertEqual(json.loads(order['delivery_json'])['street'], 'Avenida outra')

    def test_editing_payment_early_still_requires_all_delivery_fields(self):
        self.say('PEDIR'); self.say('1 carne')
        self.assertIn('nome', self.say('ALTERAR PAGAMENTO'))
        self.say('CONFIRMAR PEDIDO')
        self.assertEqual(self.rows('orders'), [])

    def test_cancellation_expiration_and_new_order_are_distinct(self):
        self.say('PEDIR'); self.say('2 carne'); self.say('CANCELAR CARRINHO')
        self.say('CONFIRMAR PEDIDO')
        self.assertEqual(self.rows('orders'), [])
        self.say('PEDIR'); self.say('1 queijo')
        with db(self.request) as conn:
            conn.execute('UPDATE whatsapp_carts SET updated_at=?', ((datetime.now(timezone.utc)-timedelta(days=2)).isoformat(),))
        self.assertIn('expirou', self.say('CONFIRMAR PEDIDO'))
        self.review(); self.say('CONFIRMAR PEDIDO'); original = self.rows('orders')[0]['id']
        self.say('NOVO PEDIDO')
        self.assertEqual(self.cart()['data']['items'], [])
        self.assertEqual(self.rows('orders')[0]['id'], original)

    def test_two_phones_cannot_see_or_confirm_each_others_cart(self):
        self.review()
        other = '5544888888888'
        reply = self.say('CARRINHO', phone=other)
        self.assertNotIn('Cliente teste', reply)
        self.say('CONFIRMAR PEDIDO', phone=other)
        self.assertEqual(self.rows('orders'), [])
        self.say('CONFIRMAR PEDIDO')
        self.assertEqual(self.rows('orders')[0]['customer_phone'], self.phone)

    def test_human_pause_blocks_cart_and_late_ai_response(self):
        self.review(); self.say('ATENDENTE')
        count = len(self.rows('whatsapp_outbox'))
        self.inbound('CONFIRMAR PEDIDO')
        self.assertEqual(len(self.rows('whatsapp_outbox')), count)
        self.assertEqual(self.rows('orders'), [])
        self.say('MENU'); self.say('CARRINHO'); self.say('CONFIRMAR PEDIDO')
        self.assertEqual(len(self.rows('orders')), 1)

    def test_stock_failure_rolls_back_all_order_side_effects(self):
        self.review()
        headers = self.login()
        result = self.client.post('/api/admin/inventory', json={
            'label': 'Carne pronta', 'unit': 'un', 'on_hand': 1, 'product_id': 'carne'}, headers=headers)
        self.assertEqual(result.status_code, 200, result.text)
        movements = self.rows('inventory_movements')
        reply = self.say('CONFIRMAR PEDIDO')
        self.assertIn('Não consegui registrar', reply)
        self.assertEqual(self.rows('orders'), [])
        self.assertEqual(self.rows('customers'), [])
        self.assertEqual(self.rows('order_events'), [])
        self.assertEqual(self.rows('inventory_movements'), movements)
        self.assertEqual(self.cart()['stage'], 'review')

    def test_cart_persists_after_app_restart(self):
        self.review()
        second = create_app(data_dir=self.app.state.db_path.parent, admin_password_hash=self.password_hash, secure_cookie=False)
        self.assertEqual(second.state.db_path, self.app.state.db_path)
        self.assertEqual(self.cart()['stage'], 'review')
        self.say('CONFIRMAR PEDIDO')
        self.assertEqual(len(self.rows('orders')), 1)

    def test_cart_fast_path_works_when_ai_disabled(self):
        with patch.dict(os.environ, {'SAHARA_AI_ENABLED': '0'}), patch.object(ai, 'interpret') as model:
            self.review(); self.say('CONFIRMAR PEDIDO')
            model.assert_not_called()
        self.assertEqual(len(self.rows('orders')), 1)

    def test_ai_can_only_apply_valid_requested_cart_changes_not_create_orders(self):
        self.inbound('Quero duas de carne e uma de queijo')
        result = {'intent': 'cart', 'product_ids': [], 'reply': '', 'cart_items': [
            {'id': 'carne', 'quantity': 2, 'action': 'add'},
            {'id': 'queijo', 'quantity': 1, 'action': 'add'}]}
        with patch.object(ai.evolution, 'open_url', return_value=self.response(result)):
            self.assertTrue(ai.process_one(self.app))
        self.assertEqual(self.cart()['data']['items'], [{'id': 'carne', 'quantity': 2}, {'id': 'queijo', 'quantity': 1}])
        self.assertEqual(self.rows('orders'), [])
        self.assertEqual(self.rows('whatsapp_outbox')[-1]['kind'], 'checkout_reply')

    def test_invalid_model_quantities_or_unknown_items_do_not_mutate_cart(self):
        self.say('PEDIR'); self.say('1 carne')
        for change in [{'id': 'inventado', 'quantity': 1, 'action': 'add'},
                       {'id': 'carne', 'quantity': True, 'action': 'add'},
                       {'id': 'carne', 'quantity': 1.5, 'action': 'add'},
                       {'id': 'carne', 'quantity': -1, 'action': 'add'}]:
            self.inbound('Mais um sabor diferente')
            result = {'intent': 'cart', 'product_ids': [], 'reply': '', 'cart_items': [change]}
            with patch.object(ai.evolution, 'open_url', return_value=self.response(result)):
                ai.process_one(self.app)
            self.assertEqual(self.cart()['data']['items'], [{'id': 'carne', 'quantity': 1}])
        self.assertEqual(self.rows('orders'), [])

    def test_late_ai_cannot_change_cart_after_cancel_or_field_collection(self):
        self.say('PEDIR'); self.say('1 carne')
        self.inbound('Mais duas de queijo')
        def generate(*args):
            self.say('CANCELAR CARRINHO')
            return {'intent': 'cart', 'product_ids': [], 'cart_items': [{'id': 'queijo', 'quantity': 2, 'action': 'add'}]}
        with patch.object(ai, 'interpret', side_effect=generate):
            ai.process_one(self.app)
        self.assertEqual(self.cart()['stage'], 'cancelled')
        self.assertEqual(self.cart()['data']['items'], [{'id': 'carne', 'quantity': 1}])
        self.assertEqual(self.rows('orders'), [])

    def test_personal_checkout_fields_are_not_sent_as_ai_history(self):
        self.review(); self.say('CONFIRMAR PEDIDO')
        self.inbound('Por que o céu é azul?')
        result = {'intent': 'conversation', 'product_ids': [], 'reply': 'O ar espalha a luz azul.', 'cart_items': []}
        with patch.object(ai.evolution, 'open_url', return_value=self.response(result)) as provider:
            ai.process_one(self.app)
        payload = json.loads(provider.call_args.args[0].data)
        context = json.loads(payload['contents'][0]['parts'][0]['text'])
        for private in ['Cliente teste', 'Rua teste', self.phone]:
            self.assertNotIn(private, json.dumps(context))
        self.assertEqual(context['cart']['items'], [{'id': 'carne', 'quantity': 2}, {'id': 'queijo', 'quantity': 1}])
        self.assertEqual(len(self.rows('orders')), 1)

    def test_cart_preview_uses_real_prices_without_orders_carts_or_messages(self):
        headers = self.login()
        result = {'intent': 'cart', 'product_ids': [], 'reply': '', 'cart_items': [
            {'id': 'carne', 'quantity': 2, 'action': 'add'}, {'id': 'queijo', 'quantity': 1, 'action': 'add'}]}
        with patch.object(ai.evolution, 'open_url', return_value=self.response(result)):
            response = self.client.post('/api/admin/whatsapp/ai/test',
                                        json={'scenario': 'cart', 'idempotency_key': uuid4().hex}, headers=headers)
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json()['state'], 'generated')
        self.assertIn('R$ 12,00', response.json()['reply'])
        for table in ['orders', 'customers', 'whatsapp_carts', 'whatsapp_inbound', 'whatsapp_outbox']:
            self.assertEqual(self.rows(table), [])
        self.assertEqual(len(self.rows('whatsapp_ai_probes')), 1)

    def test_queued_item_requests_are_applied_in_order(self):
        self.say('PEDIR')
        self.inbound('Quero duas de carne')
        self.inbound('Mais uma de queijo')
        results = [{'intent': 'cart', 'product_ids': [], 'cart_items': [{'id': 'carne', 'quantity': 2, 'action': 'add'}]},
                   {'intent': 'cart', 'product_ids': [], 'cart_items': [{'id': 'queijo', 'quantity': 1, 'action': 'add'}]}]
        with patch.object(ai, 'interpret', side_effect=results):
            self.assertTrue(ai.process_one(self.app)); self.assertTrue(ai.process_one(self.app))
        self.assertEqual(self.cart()['data']['items'], [{'id': 'carne', 'quantity': 2}, {'id': 'queijo', 'quantity': 1}])

    def test_old_pending_item_request_does_not_reopen_cancelled_cart(self):
        self.say('PEDIR'); self.say('1 carne'); self.inbound('Mais uma de queijo'); self.say('CANCELAR CARRINHO')
        with patch.object(ai, 'interpret', return_value={'intent': 'cart', 'product_ids': [], 'cart_items': [{'id': 'queijo', 'quantity': 1, 'action': 'add'}]}):
            ai.process_one(self.app)
        self.assertEqual(self.cart()['stage'], 'cancelled')
        self.assertEqual(self.cart()['data']['items'], [{'id': 'carne', 'quantity': 1}])

    def test_product_question_does_not_add_to_cart(self):
        self.say('PEDIR'); self.inbound('Quanto custa a de carne?')
        with patch.object(ai, 'interpret', return_value={'intent': 'products', 'product_ids': ['carne']}):
            ai.process_one(self.app)
        self.assertEqual(self.cart()['data']['items'], [])
        self.assertEqual(self.rows('orders'), [])
