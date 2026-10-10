"""Carrinhos privados; somente a confirmação explícita registra um pedido."""
from datetime import datetime
import json
import re
import time
from uuid import uuid4

from fastapi import HTTPException
from pydantic import ValidationError

from .core import utcnow
from . import evolution

TTL = 86400
MAX_ITEMS = 30
PAYMENTS = {'pix': 'Pix', 'dinheiro': 'Dinheiro', 'credito': 'Cartão de crédito',
            'cartao de credito': 'Cartão de crédito', 'debito': 'Cartão de débito',
            'cartao de debito': 'Cartão de débito', 'a combinar': 'A combinar'}
START = {'pedir', 'novo pedido', 'quero pedir', 'quero fazer um pedido', 'montar pedido'}
QUESTIONS = {
    'name': 'Qual é o seu nome para o pedido?',
    'street': 'Qual é o endereço de entrega?',
    'number': 'Qual é o número da casa ou prédio? Se não houver número, envie S/N.',
    'neighborhood': 'Qual é o bairro da entrega em Maringá?',
    'complement': 'Informe complemento ou referência da entrega. Se não houver, envie SEM COMPLEMENTO.',
    'notes': 'Alguma observação para o pedido? Se não houver, envie SEM OBSERVAÇÃO. Para alergias, envie ATENDENTE.',
    'payment': 'Como deseja pagar: PIX, DINHEIRO, CRÉDITO ou DÉBITO? O pagamento será conferido pela loja.',
}


def initialize(conn):
    conn.execute('''CREATE TABLE IF NOT EXISTS whatsapp_carts (
        phone TEXT NOT NULL, provider TEXT NOT NULL, id TEXT UNIQUE NOT NULL,
        stage TEXT NOT NULL, data_json TEXT NOT NULL, revision INTEGER NOT NULL DEFAULT 0,
        order_id TEXT REFERENCES orders(id), updated_at TEXT NOT NULL,
        PRIMARY KEY(phone,provider))''')
    conn.execute('CREATE TABLE IF NOT EXISTS whatsapp_checkout_events (id TEXT PRIMARY KEY REFERENCES whatsapp_inbound(id))')


def load(conn, recipient):
    from . import whatsapp
    row = conn.execute('SELECT * FROM whatsapp_carts WHERE phone=? AND provider=?',
                       (evolution.canonical_phone(recipient), whatsapp.config()['provider'])).fetchone()
    if not row:
        return None
    result = dict(row)
    result['data'] = json.loads(result.pop('data_json'))
    result['expired'] = time.time() - datetime.fromisoformat(row['updated_at']).timestamp() > TTL
    return result


def save(conn, cart):
    cart['revision'] += 1
    cart['updated_at'] = utcnow()
    conn.execute('''UPDATE whatsapp_carts SET stage=?,data_json=?,revision=?,order_id=?,updated_at=?
                    WHERE phone=? AND provider=?''',
                 (cart['stage'], json.dumps(cart['data'], ensure_ascii=False), cart['revision'],
                  cart.get('order_id'), cart['updated_at'], cart['phone'], cart['provider']))


def start(conn, recipient):
    from . import whatsapp
    cart = {'phone': evolution.canonical_phone(recipient), 'provider': whatsapp.config()['provider'],
            'id': uuid4().hex, 'stage': 'items', 'data': {'items': []}, 'revision': 0,
            'order_id': None, 'updated_at': utcnow(), 'expired': False}
    conn.execute('INSERT OR REPLACE INTO whatsapp_carts VALUES(?,?,?,?,?,?,?,?)',
                 (cart['phone'], cart['provider'], cart['id'], cart['stage'],
                  json.dumps(cart['data']), 0, None, cart['updated_at']))
    return cart


def money(cents):
    return f'R$ {cents // 100},{cents % 100:02d}'


def priced_items(conn, cart):
    result = []
    for item in cart['data']['items']:
        product = conn.execute('SELECT id,name,price_cents FROM products WHERE id=?', (item['id'],)).fetchone()
        if not product:
            raise ValueError('Um produto do carrinho saiu do cardápio. Envie ALTERAR CARRINHO para revisar.')
        result.append(dict(product) | {'quantity': item['quantity']})
    return result


def summary(conn, cart):
    items = priced_items(conn, cart)
    lines = [f"• {item['quantity']} × {item['name']} — {money(item['quantity'] * item['price_cents'])}"
             for item in items]
    return 'Seu carrinho:\n' + ('\n'.join(lines) if lines else 'Ainda não há produtos.') + '\nValor dos produtos: ' + money(sum(i['quantity'] * i['price_cents'] for i in items))


def prompt(conn, cart):
    if cart['stage'] == 'items':
        return summary(conn, cart) + '\nEnvie itens, por exemplo: 2 carne e 1 queijo. Para retirar: REMOVER carne. Para escolher quantidades: AJUSTAR 3 carne. Quando terminar, envie FINALIZAR. Para descartar, CANCELAR CARRINHO.'
    if cart['stage'] == 'review':
        data = cart['data']
        return (summary(conn, cart) + f"\nNome: {data['name']}\nEntrega: {data['street']}, {data['number']} — {data['neighborhood']}, Maringá"
                + (f"\nComplemento: {data['complement']}" if data.get('complement') else '')
                + (f"\nObservações: {data['notes']}" if data.get('notes') else '')
                + f"\nPagamento: {data['payment_method']} (a conferir)."
                + '\nA loja confirma disponibilidade, atendimento do endereço, eventual taxa e prazo de entrega. Atendemos das 18h às 23h.'
                + '\nRevise os dados. Para registrar no PDV, envie CONFIRMAR PEDIDO. Para mudar itens, ALTERAR CARRINHO; entrega, ALTERAR ENDEREÇO; pagamento, ALTERAR PAGAMENTO. Para descartar, CANCELAR CARRINHO.')
    return QUESTIONS[cart['stage']]


def advance(conn, cart):
    fields = [('name', 'name'), ('street', 'street'), ('number', 'number'),
              ('neighborhood', 'neighborhood'), ('complement', 'complement'),
              ('notes', 'notes'), ('payment_method', 'payment')]
    cart['stage'] = next((stage for key, stage in fields if key not in cart['data']), 'review')
    if cart['stage'] == 'review':
        cart['data']['quote'] = priced_items(conn, cart)


def receipt(cart):
    return ('Pedido #' + cart['order_id'] + ' registrado no PDV da Sahara. O pagamento ainda será conferido pela loja.'
            '\nA equipe confirma a entrega e o prazo. Para acompanhar, envie STATUS; para receber avisos, ATIVAR AVISOS; para falar com a loja, ATENDENTE. Para outro pedido, envie NOVO PEDIDO.')


def parse_items(conn, text):
    """Fast path for exact catalog names; ambiguous requests go to the AI."""
    from .whatsapp import normalize
    message = normalize(text)
    action = 'add'
    for prefix, operation in [('remover ', 'remove'), ('retirar ', 'remove'), ('ajustar ', 'set')]:
        if message.startswith(prefix):
            message, action = message[len(prefix):], operation
            break
    for prefix in ('quero ', 'adicionar ', 'colocar '):
        if message.startswith(prefix):
            message = message[len(prefix):]
            break
    names = {}
    for row in conn.execute('SELECT id,name FROM products'):
        for name in (normalize(row['name']), normalize(row['id']).replace('-', ' ')):
            names.setdefault(name, set()).add(row['id'])
    changes = []
    for part in re.split(r'\s+e\s+|\s*[,;\n]\s*', message):
        match = re.fullmatch(r'(?:(\d{1,3})\s*(?:x\s*)?)?(.+)', part.strip())
        if not match:
            return None
        quantity, name = match.groups()
        matches = names.get(name.strip(' .!'), set())
        if len(matches) != 1 or (quantity is None and action != 'remove'):
            return None
        changes.append({'id': next(iter(matches)), 'quantity': 0 if action == 'remove' else int(quantity), 'action': action})
    return changes or None


def validate_changes(changes, allowed):
    if not isinstance(changes, list) or not 1 <= len(changes) <= MAX_ITEMS:
        raise ValueError('Informe os produtos e as quantidades para o carrinho.')
    seen = set()
    for item in changes:
        if not isinstance(item, dict) or set(item) != {'id', 'quantity', 'action'}:
            raise ValueError('Informe os produtos e as quantidades para o carrinho.')
        if not isinstance(item['id'], str) or item['id'] not in allowed or item['id'] in seen:
            raise ValueError('Não identifiquei esse produto com segurança. Informe o nome completo do cardápio.')
        seen.add(item['id'])
        quantity = item['quantity']
        if type(quantity) is not int or item['action'] not in ('add', 'set', 'remove') or not (0 if item['action'] == 'remove' else 1) <= quantity <= 99 or (item['action'] == 'remove' and quantity != 0):
            raise ValueError('Use quantidades inteiras de 1 a 99 por produto.')


def change_items(conn, cart, changes):
    validate_changes(changes, {row[0] for row in conn.execute('SELECT id FROM products')})
    items = {item['id']: item['quantity'] for item in cart['data']['items']}
    for item in changes:
        identifier, quantity, action = item['id'], item['quantity'], item['action']
        if action == 'remove':
            if identifier not in items:
                raise ValueError('Esse produto não está no carrinho. Envie CARRINHO para conferir.')
            items.pop(identifier)
        else:
            items[identifier] = quantity + items.get(identifier, 0) if action == 'add' else quantity
            if items[identifier] > 99:
                raise ValueError('O limite é de 99 unidades por produto. Envie AJUSTAR com a quantidade desejada.')
    if len(items) > MAX_ITEMS:
        raise ValueError('O carrinho aceita até 30 sabores ou produtos diferentes. Para pedidos maiores, envie ATENDENTE.')
    cart['data']['items'] = [{'id': key, 'quantity': value} for key, value in items.items()]
    cart['data'].pop('quote', None)
    cart['stage'] = 'items'
    save(conn, cart)
    return prompt(conn, cart)


def apply_model(conn, recipient, changes, version):
    cart = load(conn, recipient)
    actual = (cart['id'], cart['revision']) if cart else None
    if actual != version:
        return 'Seu carrinho foi atualizado durante a resposta. Envie CARRINHO para conferir antes de continuar.'
    if cart and cart['expired']:
        return 'O carrinho expirou. Envie NOVO PEDIDO para começar novamente.'
    if cart and cart['stage'] == 'completed':
        return 'Esse pedido já foi registrado. Para alterar o pedido, envie ATENDENTE. Para montar outro, envie NOVO PEDIDO.'
    if cart and cart['stage'] not in ('items', 'completed', 'cancelled'):
        return 'Para alterar os itens, envie ALTERAR CARRINHO. ' + prompt(conn, cart)
    try:
        validate_changes(changes, {row[0] for row in conn.execute('SELECT id FROM products')})
        if not cart or cart['stage'] == 'cancelled':
            cart = start(conn, recipient)
        return change_items(conn, cart, changes)
    except ValueError as error:
        return str(error)


def confirm(conn, cart):
    from .main import OrderInput
    from .order_service import register_order
    if priced_items(conn, cart) != cart['data'].get('quote'):
        cart['data']['quote'] = priced_items(conn, cart)
        save(conn, cart)
        return 'O cardápio mudou. Confira os preços atualizados e confirme novamente.\n' + prompt(conn, cart)
    data = cart['data']
    try:
        body = OrderInput.model_validate({
            'items': data['items'], 'customer': {'name': data['name'], 'phone': cart['phone']},
            'delivery': {key: data[key] for key in ('street', 'number', 'neighborhood', 'complement')},
            'payment_method': data['payment_method'], 'notes': data.get('notes', ''),
            'idempotency_key': 'whatsapp-cart:' + cart['id']})
        # Roll back stock/customer writes if the service rejects registration.
        conn.execute('SAVEPOINT whatsapp_order')
        try:
            order = register_order(conn, body, source='whatsapp')
        except Exception:
            conn.execute('ROLLBACK TO whatsapp_order')
            conn.execute('RELEASE whatsapp_order')
            raise
        conn.execute('RELEASE whatsapp_order')
    except (HTTPException, ValidationError) as error:
        detail = error.detail if isinstance(error, HTTPException) else 'Confira os dados de entrega e as quantidades.'
        return f'Não consegui registrar o pedido: {detail} Para ajuda, envie ATENDENTE. Nenhum pedido novo foi criado.'
    cart['stage'], cart['order_id'] = 'completed', order['id']
    save(conn, cart)
    return receipt(cart)


def handle(conn, recipient, text):
    """Called inside the authenticated inbound transaction, before keyword replies."""
    from .whatsapp import normalize
    normalized = normalize(text)
    cart = load(conn, recipient)
    if normalized in START:
        if not cart or cart['expired'] or cart['stage'] in ('completed', 'cancelled'):
            cart = start(conn, recipient)
        return 'Vamos montar seu pedido pelo WhatsApp.\n' + prompt(conn, cart)
    if normalized == 'confirmar pedido' and cart and cart['stage'] == 'completed':
        return receipt(cart)
    if cart and cart['expired'] and cart['stage'] not in ('completed', 'cancelled'):
        cart['stage'] = 'cancelled'
        save(conn, cart)
        return 'O carrinho expirou após 24 horas. Nenhum pedido foi registrado. Envie NOVO PEDIDO para começar novamente.'
    active = cart and cart['stage'] not in ('completed', 'cancelled')
    if normalized == 'cancelar carrinho' and active:
        cart['stage'] = 'cancelled'
        save(conn, cart)
        return 'Carrinho descartado. Nenhum pedido foi registrado. Para começar outro, envie NOVO PEDIDO.'
    if normalized == 'confirmar pedido':
        return confirm(conn, cart) if active and cart['stage'] == 'review' else 'Antes de confirmar, monte o carrinho e confira os dados de entrega. Envie PEDIR para começar.'
    if normalized in ('carrinho', 'meu carrinho'):
        if active:
            return prompt(conn, cart) if cart['stage'] in ('items', 'review') else summary(conn, cart) + '\n' + prompt(conn, cart)
        return 'Você ainda não tem um carrinho aberto. Envie PEDIR para começar.'
    if not active:
        return None
    if normalized == 'limpar carrinho':
        cart['data']['items'] = []
        cart['data'].pop('quote', None)
        cart['stage'] = 'items'
        save(conn, cart)
        return prompt(conn, cart)
    if normalized in ('alterar carrinho', 'alterar endereco', 'alterar pagamento', 'alterar nome', 'alterar observacao'):
        if normalized == 'alterar carrinho':
            cart['stage'] = 'items'
        else:
            keys = {'alterar endereco': ('street', 'number', 'neighborhood', 'complement'),
                    'alterar pagamento': ('payment_method',), 'alterar nome': ('name',),
                    'alterar observacao': ('notes',)}[normalized]
            for key in keys:
                cart['data'].pop(key, None)
            advance(conn, cart)
        cart['data'].pop('quote', None)
        save(conn, cart)
        return prompt(conn, cart)
    if normalized in ('finalizar', 'finalizar pedido'):
        if not cart['data']['items']:
            return 'O carrinho está vazio. Envie, por exemplo: 2 carne e 1 queijo.'
        advance(conn, cart)
        save(conn, cart)
        return prompt(conn, cart)
    if cart['stage'] == 'items':
        changes = parse_items(conn, text)
        if changes is None:
            return None
        try:
            return change_items(conn, cart, changes)
        except ValueError as error:
            return str(error)
    if cart['stage'] == 'review':
        return prompt(conn, cart)
    stage = cart['stage']
    value = text.strip()
    maximum = {'name': 100, 'street': 160, 'number': 20, 'neighborhood': 100,
               'complement': 160, 'notes': 500, 'payment': 40}[stage]
    if not value or len(value) > maximum or any(ord(character) < 32 and character not in '\n\t' for character in value):
        return f'Envie uma resposta com até {maximum} caracteres. ' + QUESTIONS[stage]
    if stage == 'payment':
        if normalized not in PAYMENTS:
            return QUESTIONS['payment']
        cart['data']['payment_method'] = PAYMENTS[normalized]
    else:
        if stage == 'complement' and normalized in ('sem complemento', 'nenhum', 'nao', 'sem'):
            value = ''
        if stage == 'notes' and normalized in ('sem observacao', 'nenhuma', 'nenhum', 'nao', 'sem'):
            value = ''
        cart['data'][stage] = value
    advance(conn, cart)
    save(conn, cart)
    return prompt(conn, cart)
