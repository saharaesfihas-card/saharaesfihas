"""Registro transacional compartilhado pelo cardápio, PDV e WhatsApp."""
import hashlib
import json
import secrets
import uuid

from fastapi import HTTPException

from . import marketing, operations, whatsapp
from .core import utcnow


def register_order(conn, body, source='web'):
    from .main import schedule
    raw = body.model_dump(mode='json')
    # Preserva os identificadores de tentativas anteriores à integração.
    if not raw['customer']['whatsapp_opt_in']:
        raw['customer'].pop('whatsapp_opt_in')
    fingerprint = hashlib.sha256(json.dumps(raw, sort_keys=True, ensure_ascii=False).encode()).hexdigest()
    existing = conn.execute('SELECT * FROM orders WHERE idempotency_key=?', (body.idempotency_key,)).fetchone()
    if existing:
        if existing['request_hash'] != fingerprint:
            raise HTTPException(409, 'Este identificador já foi usado para outro pedido.')
        return {'id': existing['id'], 'status': existing['status'], 'total_cents': existing['total_cents'],
                'tracking_token': existing['tracking_token'], 'tracking_url': None, 'replayed': True}
    requested_for = schedule(body.requested_for)
    ids = [item.id for item in body.items]
    if len(ids) != len(set(ids)):
        raise HTTPException(422, 'Há produtos repetidos no pedido.')
    items = []
    for item in body.items:
        product = conn.execute('SELECT * FROM products WHERE id=?', (item.id,)).fetchone()
        if not product:
            raise HTTPException(422, 'Produto não encontrado no cardápio.')
        items.append({'id': item.id, 'name': product['name'], 'quantity': item.quantity, 'price_cents': product['price_cents']})
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
    subtotal = sum(item['quantity'] * item['price_cents'] for item in items)
    code = body.coupon_code.strip().upper() if body.coupon_code else None
    discount = marketing.coupon_discount(conn, code, subtotal, customer_id)
    if not 0 <= discount <= subtotal:
        raise HTTPException(422, 'Desconto inválido.')
    delivery = body.delivery.model_dump(mode='json')
    if body.delivery.location:
        point = body.delivery.location
        delivery['location']['url'] = f'https://www.google.com/maps?q={point.latitude:.6f},{point.longitude:.6f}'
    order_id, tracking = uuid.uuid4().hex, secrets.token_urlsafe(24)
    conn.execute('''INSERT INTO orders(id,created_at,requested_for,customer_id,customer_name,customer_phone,
        delivery_json,items_json,subtotal_cents,discount_cents,total_cents,coupon_code,payment_method,
        tracking_token,idempotency_key,request_hash,source,notes,status) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)''',
        (order_id, utcnow(), requested_for, customer_id, body.customer.name, body.customer.phone,
         json.dumps(delivery, ensure_ascii=False), json.dumps(items, ensure_ascii=False), subtotal, discount,
         subtotal - discount, code, body.payment_method, tracking, body.idempotency_key, fingerprint, source, body.notes, 'preparing'))
    order = conn.execute('SELECT * FROM orders WHERE id=?', (order_id,)).fetchone()
    operations.apply_stock(conn, order)
    marketing.record_coupon_use(conn, order)
    conn.execute('UPDATE orders SET stock_applied=1,confirmed_at=? WHERE id=?', (utcnow(), order_id))
    conn.execute('INSERT INTO analytics_events VALUES(?,?)', ('order_registered', utcnow()))
    conn.execute('INSERT INTO order_events VALUES(?,?,?,?)', (uuid.uuid4().hex, order_id, 'preparing', utcnow()))
    conn.execute('UPDATE orders SET whatsapp_opt_in=? WHERE id=?', (int(body.customer.whatsapp_opt_in), order_id))
    whatsapp.order_notice(conn, conn.execute('SELECT * FROM orders WHERE id=?', (order_id,)).fetchone())
    return {'id': order_id, 'status': 'preparing', 'total_cents': subtotal - discount, 'tracking_token': tracking,
            'tracking_url': None, 'replayed': False}

