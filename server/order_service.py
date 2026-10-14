"""Registro transacional compartilhado pelo cardápio, PDV e WhatsApp."""
import hashlib
import json
import secrets
import uuid

from fastapi import HTTPException

from . import marketing, operations, whatsapp, delivery_estimates, product_promotions
from .core import utcnow, next_order_number, order_reference


def register_order(conn, body, source='web'):
    from .main import schedule
    raw = body.model_dump(mode='json')
    if raw.get('expected_subtotal_cents') is None:
        raw.pop('expected_subtotal_cents', None)
    # Preserva os identificadores de tentativas anteriores à integração.
    if not raw['customer']['whatsapp_opt_in']:
        raw['customer'].pop('whatsapp_opt_in')
    fingerprint = hashlib.sha256(json.dumps(raw, sort_keys=True, ensure_ascii=False).encode()).hexdigest()
    existing = conn.execute('SELECT * FROM orders WHERE idempotency_key=?', (body.idempotency_key,)).fetchone()
    if existing:
        if existing['request_hash'] != fingerprint:
            raise HTTPException(409, 'Este identificador já foi usado para outro pedido.')
        return {'id': existing['id'], 'order_number': existing['order_number'], 'display_number': order_reference(existing),
                'status': existing['status'], 'total_cents': existing['total_cents'],
                'tracking_token': existing['tracking_token'], 'tracking_url': None, 'replayed': True,
                'subtotal_cents': existing['subtotal_cents'], 'discount_cents': existing['discount_cents'],
                'delivery_fee': json.loads(existing['delivery_json']).get('fee')}
    requested_for = schedule(body.requested_for)
    ids = [item.id for item in body.items]
    if len(ids) != len(set(ids)):
        raise HTTPException(422, 'Há produtos repetidos no pedido.')
    items = []
    pricing_moment = product_promotions.local_now()
    for item in body.items:
        product = product_promotions.product(conn, item.id, pricing_moment)
        if not product:
            raise HTTPException(422, 'Produto não encontrado no cardápio.')
        if not product['available']:
            raise HTTPException(409, f"{product['name']} está indisponível no momento. Retire o item do pedido ou escolha outro produto.")
        items.append({'id': item.id, 'name': product['name'], 'quantity': item.quantity, 'price_cents': product['price_cents']})
    subtotal = sum(item['quantity'] * item['price_cents'] for item in items)
    if body.expected_subtotal_cents is not None and body.expected_subtotal_cents != subtotal:
        raise HTTPException(409, 'Os preços do cardápio mudaram. Confira o valor atualizado e confirme novamente.')
    customer_id = None
    if body.customer.phone:
        customer = conn.execute('SELECT * FROM customers WHERE phone=?', (body.customer.phone,)).fetchone()
        customer_id = customer['id'] if customer else uuid.uuid4().hex
        if customer:
            conn.execute('UPDATE customers SET name=CASE WHEN ?<>\'\' THEN ? ELSE name END, '
                         'marketing_opt_in=CASE WHEN ? THEN 1 ELSE marketing_opt_in END WHERE id=?',
                         (body.customer.name, body.customer.name, body.customer.marketing_opt_in, customer_id))
        else:
            conn.execute('INSERT INTO customers(id,name,phone,marketing_opt_in,created_at) VALUES(?,?,?,?,?)',
                         (customer_id, body.customer.name, body.customer.phone, int(body.customer.marketing_opt_in), utcnow()))
    code = body.coupon_code.strip().upper() if body.coupon_code else None
    discount = marketing.coupon_discount(conn, code, subtotal, customer_id)
    if not 0 <= discount <= subtotal:
        raise HTTPException(422, 'Desconto inválido.')
    delivery = body.delivery.model_dump(mode='json')
    if body.delivery.location:
        point = body.delivery.location
        delivery['location']['url'] = f'https://www.google.com/maps?q={point.latitude:.6f},{point.longitude:.6f}'
    fee = delivery_estimates.fee_quote(conn, delivery.get('location'))
    if fee is not None:
        delivery['fee'] = fee
    total = subtotal - discount + ((fee or {}).get('amount_cents') or 0)
    order_id, tracking = uuid.uuid4().hex, secrets.token_urlsafe(24)
    number = next_order_number(conn)
    conn.execute('''INSERT INTO orders(id,created_at,requested_for,customer_id,customer_name,customer_phone,
        delivery_json,items_json,subtotal_cents,discount_cents,total_cents,coupon_code,payment_method,
        tracking_token,idempotency_key,request_hash,source,notes,status,order_number) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)''',
        (order_id, utcnow(), requested_for, customer_id, body.customer.name, body.customer.phone,
         json.dumps(delivery, ensure_ascii=False), json.dumps(items, ensure_ascii=False), subtotal, discount,
         total, code, body.payment_method, tracking, body.idempotency_key, fingerprint, source, body.notes, 'preparing', number))
    order = conn.execute('SELECT * FROM orders WHERE id=?', (order_id,)).fetchone()
    operations.apply_stock(conn, order)
    marketing.record_coupon_use(conn, order)
    conn.execute('UPDATE orders SET stock_applied=1,confirmed_at=? WHERE id=?', (utcnow(), order_id))
    conn.execute('INSERT INTO analytics_events VALUES(?,?)', ('order_registered', utcnow()))
    conn.execute('INSERT INTO order_events VALUES(?,?,?,?)', (uuid.uuid4().hex, order_id, 'preparing', utcnow()))
    conn.execute('UPDATE orders SET whatsapp_opt_in=? WHERE id=?', (int(body.customer.whatsapp_opt_in), order_id))
    whatsapp.order_notice(conn, conn.execute('SELECT * FROM orders WHERE id=?', (order_id,)).fetchone())
    return {'id': order_id, 'order_number': number, 'display_number': f'{number:03d}',
            'status': 'preparing', 'total_cents': total, 'tracking_token': tracking,
            'tracking_url': None, 'replayed': False, 'subtotal_cents': subtotal,
            'discount_cents': discount, 'delivery_fee': fee}

