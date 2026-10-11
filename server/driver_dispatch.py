"""Comanda ao entregador, na mesma fila privada do WhatsApp da loja."""
from datetime import datetime
import re
import time
from zoneinfo import ZoneInfo

from . import whatsapp


def recipient(driver):
    value = driver['phone'] or ''
    if any(character not in '0123456789+ ()-.' for character in value):
        return ''
    number = whatsapp.phone(value)
    if number.startswith('55') and not re.fullmatch(r'55[1-9][0-9][0-9]{8,9}', number):
        return ''
    return number if re.fullmatch(r'[1-9][0-9]{9,14}', number) else ''


def money(cents):
    return f'R$ {cents / 100:,.2f}'.replace(',', '_').replace('.', ',').replace('_', '.')


def receipt(order, driver, received):
    address = order['delivery']
    address_text = ', '.join(str(value).strip() for value in (
        address.get('street'), address.get('number') or address.get('house_number'),
        address.get('neighborhood'), address.get('city') or 'Maringá, PR',
        address.get('complement'), address.get('reference')) if value)
    created = datetime.fromisoformat(order['created_at']).astimezone(ZoneInfo('America/Sao_Paulo'))
    lines = ['SAHARA ESFIHAS — COMANDA DE ENTREGA', f"Pedido #{order['id']}",
             f"Data: {created:%d/%m/%Y %H:%M}", f"Entregador: {driver['name']}",
             f"Cliente: {order['customer_name'] or 'Não informado'}"]
    if order['customer_phone']:
        lines.append(f"Contato do cliente: {order['customer_phone']}")
    lines.extend([f'Endereço: {address_text}', 'Rota no Google Maps:', order['maps_url'], '', 'ITENS'])
    for item in order['items']:
        amount = item.get('total_price_cents', item.get('price_cents', 0) * item['quantity'])
        lines.append(f"{item['quantity']} × {item['name']} — {money(amount)}")
        if item.get('observations'):
            lines.append(f"  Observação do item: {item['observations']}")
    if order['notes']:
        lines.extend(['', f"Observações do pedido: {order['notes']}"])
    lines.extend(['', f"Subtotal dos produtos: {money(order['subtotal_cents'])}"])
    if order['discount_cents']:
        lines.append(f"Desconto: {money(order['discount_cents'])}")
    total = order['total_cents']
    lines.extend([f'Total dos produtos: {money(total)}',
                  f"Pagamento: {order['payment_method'] or 'A combinar com a loja'}"])
    if order['payment_status'] == 'paid':
        lines.append('PAGO — não cobrar os produtos novamente.')
    elif received:
        lines.extend([f'Já recebido: {money(received)}',
                      f'Valor restante dos produtos: {money(max(0, total - received))}'])
    else:
        lines.append(f'A RECEBER pelos produtos: {money(total)}')
    lines.append('Taxa de entrega: confirmar com a loja.')
    return '\n'.join(lines)


def parts(text, order_id):
    chunks = []
    while len(text) > 3500:
        cut = text.rfind('\n', 0, 3500)
        if cut <= 0:
            cut = 3500
        chunks.append(text[:cut])
        text = text[cut:].lstrip('\n')
    if text:
        chunks.append(text)
    if len(chunks) == 1:
        return chunks
    return [f'Comanda #{order_id} · Parte {index}/{len(chunks)}\n\n{chunk}'
            for index, chunk in enumerate(chunks, 1)]


def previous_messages(conn, order):
    prefix = f"driver:{order['courier_assignment_key']}:"
    return conn.execute('SELECT phone,body,status,error FROM whatsapp_outbox WHERE event_key LIKE ? ORDER BY rowid', (prefix + '%',)).fetchall()


def changed_receipt(conn, order, driver, received):
    previous = previous_messages(conn, order)
    if not previous:
        return False
    if not order['maps_url'] or not recipient(driver):
        return True
    bodies = parts(receipt(order, driver, received), order['id'])
    return ([row['body'] for row in previous] != bodies
            or any(row['phone'] != recipient(driver) for row in previous))


def enqueue(conn, order, driver, received=0):
    if not driver:
        return {'status': 'removed', 'message': 'Entregador removido.'}
    number = recipient(driver)
    if not number:
        return {'status': 'invalid_phone', 'message': 'Entregador atribuído, mas a comanda não foi enviada. Cadastre um WhatsApp válido com DDD para o entregador.'}
    if not order['maps_url']:
        return {'status': 'missing_address', 'message': 'Entregador atribuído, mas a comanda não foi enviada. Complete o endereço ou a localização do pedido.'}
    if not whatsapp.config()['ready']:
        return {'status': 'not_configured', 'message': 'Entregador atribuído, mas a comanda não foi enviada. Configure o WhatsApp da loja em Integrações.'}
    prefix = f"driver:{order['courier_assignment_key']}:"
    previous = previous_messages(conn, order)
    if not previous:
        preceding = ''
        for index, body in enumerate(parts(receipt(order, driver, received), order['id'])):
            event = prefix + str(index)
            whatsapp.queue(conn, event, number, 'driver_dispatch', body, order['id'], driver['id'], after_event=preceding)
            preceding = event
        previous = previous_messages(conn, order)
    statuses = {row['status'] for row in previous}
    status = next((value for value in ('uncertain', 'failed', 'blocked', 'skipped', 'pending', 'sending', 'accepted', 'sent', 'delivered', 'read') if value in statuses), 'pending')
    if status in ('pending', 'sending'):
        message = 'Entregador atribuído. Comanda com Maps na fila do WhatsApp da loja. Confira o envio em Integrações → WhatsApp.'
    elif status in ('accepted', 'sent', 'delivered', 'read'):
        message = 'Entregador atribuído. Esta comanda já teve envio confirmado pelo serviço do WhatsApp. Confira a entrega no histórico de mensagens.'
    else:
        message = 'Entregador atribuído. O envio da comanda precisa de atenção; confira o histórico em Integrações → WhatsApp.'
    return {'status': status, 'message': message}


def stale_reason(conn, row):
    from .core import order_dict
    from .operations import _maps_link, receivable_payment_total
    order = conn.execute('SELECT * FROM orders WHERE id=?', (row['order_id'],)).fetchone()
    if not order or order['status'] in ('delivered', 'cancelled', 'canceled'):
        return 'Pedido encerrado; comanda não enviada.'
    driver = conn.execute('SELECT * FROM drivers WHERE id=?', (order['courier_id'],)).fetchone()
    prefix = f"driver:{order['courier_assignment_key']}:"
    if (not driver or not driver['active'] or row['order_status'] != driver['id']
            or recipient(driver) != row['phone'] or not row['event_key'].startswith(prefix)):
        return 'A atribuição ou o cadastro do entregador foi alterado.'
    order = order_dict(order)
    order['maps_url'] = _maps_link(order)
    if changed_receipt(conn, order, driver, receivable_payment_total(conn, order['id'])):
        return 'O pedido ou pagamento foi atualizado. Atribua novamente para enviar a comanda atualizada.'
    if time.time() - datetime.fromisoformat(row['created_at']).timestamp() > 86400:
        return 'Comanda expirada. Confira a entrega antes de atribuir novamente.'
    return None
