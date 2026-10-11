"""Delivery estimates use store rules and real points, never model guesses."""
import json
import math
import re
import time
from urllib.parse import parse_qs, urlsplit

from fastapi import APIRouter, Depends, HTTPException, Request
from pydantic import BaseModel, ConfigDict, Field
from typing import Literal

from .core import db, require_admin
from . import evolution

router = APIRouter()
ADDRESS = 'Endereço da loja (uso interno)'
TTL = 86400


def initialize(conn):
    conn.execute('''CREATE TABLE IF NOT EXISTS whatsapp_delivery_points (
        phone TEXT NOT NULL, provider TEXT NOT NULL, point_json TEXT,
        awaiting INTEGER NOT NULL DEFAULT 0, updated_at INTEGER NOT NULL,
        PRIMARY KEY(phone,provider))''')


def point(value):
    if not isinstance(value, dict):
        return None
    lat, lon = value.get('latitude'), value.get('longitude')
    if (isinstance(lat, bool) or isinstance(lon, bool)
            or not isinstance(lat, (int, float)) or not isinstance(lon, (int, float))
            or not math.isfinite(lat) or not math.isfinite(lon)
            or not -90 <= lat <= 90 or not -180 <= lon <= 180):
        return None
    accuracy = value.get('accuracy')
    if accuracy is not None and (isinstance(accuracy, bool) or not isinstance(accuracy, (int, float))
                                 or not math.isfinite(accuracy) or not 0 <= accuracy <= 100):
        return None
    return {'latitude': lat, 'longitude': lon}


def map_point(text):
    # No remote fetching. A map viewport (@lat,lng) is not the delivery pin.
    urls = re.findall(r'https://[^\s<>]+', text)
    if len(urls) != 1:
        return None
    try:
        url = urlsplit(urls[0].rstrip('.,)'))
        if url.hostname not in ('www.google.com', 'google.com', 'maps.google.com', 'www.google.com.br', 'google.com.br') or url.username or url.password:
            return None
        query = parse_qs(url.query)
        match = re.fullmatch(r'(-?\d+(?:\.\d+)?),\s*(-?\d+(?:\.\d+)?)',
                             next((query[key][0] for key in ('q', 'query') if key in query), ''))
        if not match:
            match = re.search(r'!3d(-?\d+(?:\.\d+)?)!4d(-?\d+(?:\.\d+)?)', url.path + url.query)
        return point({'latitude': float(match[1]), 'longitude': float(match[2])}) if match else None
    except (ValueError, IndexError):
        return None


def config(conn):
    from . import whatsapp
    try:
        data = json.loads(whatsapp.setting(conn, 'delivery_estimates', '{}'))
    except (ValueError, TypeError):
        data = {}
    if not isinstance(data, dict):
        data = {}
    return {'address': data.get('address', ADDRESS), 'origin': point(data.get('origin')),
            'five_to_six': data.get('five_to_six') if data.get('five_to_six') in ('50_60', '70') else 'handoff',
            'fees_enabled': data.get('fees_enabled') is True,
            'above_seven': '10' if data.get('above_seven') == '10' else 'consult'}


def minutes(distance, gap='handoff'):
    if not math.isfinite(distance) or distance < 0:
        return None
    if distance <= 3:
        return '30 a 40 minutos'
    if distance <= 4:
        return '40 a 50 minutos'
    if distance <= 5:
        return '50 a 60 minutos'
    if distance <= 6:
        return {'50_60': '50 a 60 minutos', '70': '1 hora e 10 minutos'}.get(gap)
    return '1 hora e 10 minutos'


def radius(origin, destination):
    lat1, lat2 = math.radians(origin['latitude']), math.radians(destination['latitude'])
    delta_lat = lat2 - lat1
    delta_lon = math.radians(destination['longitude'] - origin['longitude'])
    a = math.sin(delta_lat / 2) ** 2 + math.cos(lat1) * math.cos(lat2) * math.sin(delta_lon / 2) ** 2
    return round(6371.0088 * 2 * math.asin(math.sqrt(min(1, max(0, a)))), 6)


def fee_cents(distance, above_seven='consult'):
    if not math.isfinite(distance) or distance < 0:
        return None
    for limit, cents in ((3, 0), (4, 400), (5, 500), (6, 800), (7, 1000)):
        if distance <= limit:
            return cents
    return 1000 if above_seven == '10' else None


def fee_policy(conn):
    cfg = config(conn)
    if not cfg['fees_enabled']:
        return 'Taxa de entrega: consultar com a loja.'
    return ('Taxas por raio da loja: até 3 km, grátis; acima de 3 até 4 km, R$ 4,00; '
            'acima de 4 até 5 km, R$ 5,00; acima de 5 até 6 km, R$ 8,00; '
            'acima de 6 até 7 km, R$ 10,00; acima de 7 km, '
            + ('R$ 10,00.' if cfg['above_seven'] == '10' else 'consultar com o atendente.')
            + ' Sem localização confiável, o atendente confirma a taxa.')


def fee_quote(conn, location):
    cfg = config(conn)
    if not cfg['fees_enabled']:
        return None
    destination = point(location)
    amount = fee_cents(radius(cfg['origin'], destination), cfg['above_seven']) if cfg['origin'] and destination else None
    return {'status': 'quoted' if amount is not None else 'pending', 'amount_cents': amount}


def fee_text(quote):
    if not quote or quote['status'] == 'pending':
        return 'Taxa de entrega: o atendente confirma o valor antes do pagamento.'
    from .whatsapp_checkout import money
    return 'Taxa de entrega: ' + ('grátis.' if quote['amount_cents'] == 0 else money(quote['amount_cents']) + '.')


def pending_fee(order):
    return json.loads(order['delivery_json']).get('fee', {}).get('status') == 'pending'


def policy(conn):
    cfg = config(conn)
    gap = minutes(6, cfg['five_to_six']) or 'atendente define'
    return ('Prazos estimados por raio da loja: até 3 km, 30 a 40 minutos; acima de 3 até 4 km, '
            '40 a 50 minutos; acima de 4 até 5 km, 50 a 60 minutos; acima de 5 até 6 km, '
            + gap + '; acima de 6 km, 1 hora e 10 minutos. Sem localização confiável, o atendente define o prazo.'
            + '\n' + fee_policy(conn))


def label(conn, location):
    cfg, destination = config(conn), point(location)
    estimate = minutes(radius(cfg['origin'], destination), cfg['five_to_six']) if cfg['origin'] and destination else None
    return ('Prazo estimado de entrega: ' + estimate + '.' if estimate else 'Prazo de entrega: o atendente confere a localização e define o prazo.')


def state(conn, recipient):
    from . import whatsapp
    return conn.execute('''SELECT * FROM whatsapp_delivery_points WHERE phone=? AND provider=? AND updated_at>?''',
                        (evolution.canonical_phone(recipient), whatsapp.config()['provider'], int(time.time()) - TTL)).fetchone()


def reset_waiting(conn, recipient):
    from . import whatsapp
    conn.execute('UPDATE whatsapp_delivery_points SET awaiting=0 WHERE phone=? AND provider=?',
                 (evolution.canonical_phone(recipient), whatsapp.config()['provider']))


def save(conn, recipient, location=None, awaiting=False):
    from . import whatsapp
    if not evolution.canonical_phone(recipient):
        return
    conn.execute('''INSERT INTO whatsapp_delivery_points VALUES(?,?,?,?,?)
        ON CONFLICT(phone,provider) DO UPDATE SET point_json=excluded.point_json,awaiting=excluded.awaiting,updated_at=excluded.updated_at''',
        (evolution.canonical_phone(recipient), whatsapp.config()['provider'], json.dumps(location) if location else None,
         int(awaiting), int(time.time())))


def handoff(conn, recipient):
    from . import whatsapp
    save(conn, recipient)
    if recipient:
        whatsapp.update_contact_identity(conn, recipient, 'human_until', int(time.time()) + TTL)
    return ('Não consegui definir o prazo com segurança para esse endereço. Encaminhei a conversa para a equipe '
            'no PDV: o atendente vai conferir a localização e estipular o prazo de entrega. '
            'O atendimento automático está pausado. Para retomar, envie MENU.')


def fee_request(text):
    return bool(re.search(r'\b(taxa|frete|custo da entrega|valor da entrega)\b', text)
                or re.search(r'\bgratis\b', text) and re.search(r'\bentrega\b', text))


def response(conn, recipient, text='', location=None):
    from . import whatsapp, whatsapp_checkout
    cfg = config(conn)
    normalized = whatsapp.normalize(text)
    fee_intent = fee_request(normalized)
    address = re.search(r'\b(rua|avenida|av\.?|travessa|bairro|cep)\b', normalized)
    requested = re.search(r'\b(prazo|demora|tempo|previsao|quando chega)\b', normalized)
    if re.search(r'\b(atrasad[oa]|atraso|ja pedi|ja fiz|quanto falta|cade meu pedido)\b', normalized):
        return handoff(conn, recipient)
    destination = point(location) or map_point(text)
    previous = state(conn, recipient)
    cart = whatsapp_checkout.load(conn, recipient)
    if not destination and address:
        if fee_intent:
            save(conn, recipient, awaiting=True)
            return fee_policy(conn) + '\nEnvie a localização de entrega para confirmar o valor. Se não conseguir, envie ATENDENTE.'
        return handoff(conn, recipient)
    if not destination and previous and previous['point_json']:
        destination = point(json.loads(previous['point_json']))
    if not destination and cart and not cart['expired']:
        destination = point(cart['data'].get('location'))
    order = None
    if not destination and recipient:
        aliases = whatsapp.order_phone_aliases(recipient)
        placeholders = ','.join('?' for _ in aliases)
        # Only an active order belonging to this authenticated sender.
        if aliases:
            from datetime import datetime, timezone
            cutoff = datetime.fromtimestamp(time.time() - TTL, timezone.utc).isoformat()
            order = conn.execute(f'''SELECT * FROM orders WHERE customer_phone IN ({placeholders})
                AND status NOT IN ('delivered','cancelled','canceled') AND created_at>=?
                ORDER BY created_at DESC,rowid DESC LIMIT 1''', (*aliases, cutoff)).fetchone()
        if order:
            destination = point(json.loads(order['delivery_json']).get('location'))
            if not destination:
                return handoff(conn, recipient)
    if not destination:
        # A stated distance is conditional information, not a located address.
        distances = re.findall(r'(?<![\d.,-])(\d{1,3}(?:[.,]\d{1,2})?)\s*km\b', normalized)
        if len(distances) == 1 and not re.search(r'\b(entre|ate|acima)\b|\d+\s*(?:a|-)\s*\d+\s*km', normalized):
            distance = float(distances[0].replace(',', '.'))
            estimate = minutes(distance, cfg['five_to_six'])
            if fee_intent and cfg['fees_enabled']:
                amount = fee_cents(distance, cfg['above_seven'])
                return ('Para o raio informado, ' + fee_text({'status': 'quoted', 'amount_cents': amount} if amount is not None else None)
                        + ' O valor depende da localização de entrega confirmada.')
            return ('Para um raio informado de ' + distances[0] + ' km, o prazo estimado é de ' + estimate
                    + '. ' + (fee_text({'status': 'quoted', 'amount_cents': fee_cents(distance, cfg['above_seven'])})
                    if cfg['fees_enabled'] and fee_cents(distance, cfg['above_seven']) is not None else fee_text(None))
                    + ' O valor depende da localização confirmada.') if estimate else handoff(conn, recipient)
        if previous and previous['awaiting'] and not requested:
            return handoff(conn, recipient)
        save(conn, recipient, awaiting=True)
        return policy(conn) + '\nPara estimar para seu endereço, envie a localização de entrega pelo WhatsApp. Se não conseguir, o atendente define o prazo.'
    if not cfg['origin']:
        return handoff(conn, recipient)
    distance = radius(cfg['origin'], destination)
    estimate = minutes(distance, cfg['five_to_six'])
    if not estimate:
        if cfg['fees_enabled']:
            return fee_text(fee_quote(conn, destination)) + '\n' + handoff(conn, recipient)
        return handoff(conn, recipient)
    save(conn, recipient, destination)
    prefix = ''
    if order:
        from .core import order_reference
        prefix = f"Para o endereço do pedido #{order_reference(order)}, "
    distance_text = f'{distance:.1f}'.replace('.', ',')
    return prefix + ('o' if prefix else 'O') + f" raio aproximado é de {distance_text} km e o prazo estimado de entrega é de {estimate}. " + fee_text(fee_quote(conn, destination))


def handle(conn, recipient, text):
    from . import whatsapp
    normalized = whatsapp.normalize(text)
    waiting = state(conn, recipient)
    location = map_point(text)
    address = re.search(r'\b(rua|avenida|av\.?|travessa|bairro|cep)\b', normalized)
    business = re.search(r'\b(entrega|entregar|pedido|chegar)\b', normalized) or address
    business = business or re.search(r'\bkm\b', normalized) and re.search(r'\bprazo\b', normalized)
    timing = re.search(r'\b(prazo|previsao|quando chega|quanto tempo|demora|atraso|atrasad[oa])\b', normalized)
    short_question = re.fullmatch(r'(?:(?:qual(?: e)? (?:o |a )?)?prazo|quanto tempo|demora|quando chega)[ ?!.]*', normalized)
    intent = re.search(r'\bcade meu pedido\b', normalized) or timing and (business or short_question)
    if location:
        return receive(conn, recipient, location)
    fee_intent = fee_request(normalized)
    if intent or fee_intent or (waiting and waiting['awaiting'] and (address or 'maps' in normalized)):
        return response(conn, recipient, text, location)
    return None


def receive(conn, recipient, location):
    from . import whatsapp_checkout
    destination = point(location)
    if not destination:
        return handoff(conn, recipient)
    save(conn, recipient, destination)
    cart = whatsapp_checkout.load(conn, recipient)
    if cart and not cart['expired'] and cart['stage'] not in ('completed', 'cancelled'):
        cart['data']['location'] = destination
        cart['data']['street'], cart['data']['neighborhood'] = '', ''
        for key in ('number', 'complement'):
            cart['data'].pop(key, None)
        if cart['stage'] not in ('items', 'name'):
            whatsapp_checkout.advance(conn, cart)
        whatsapp_checkout.save(conn, cart)
    reply = response(conn, recipient, location=destination)
    from . import whatsapp
    contact = whatsapp.contact_identity(conn, recipient)
    if cart and cart['stage'] not in ('completed', 'cancelled') and not (contact and contact['human_until'] > time.time()):
        reply += '\n' + whatsapp_checkout.prompt(conn, cart)
    return reply


class DeliverySettings(BaseModel):
    model_config = ConfigDict(extra='forbid', str_strip_whitespace=True)
    address: str = Field(default=ADDRESS, min_length=5, max_length=240)
    map_url: str = Field(default='', max_length=2000)
    origin: dict | None = None
    five_to_six: Literal['50_60', '70', 'handoff'] = 'handoff'
    fees_enabled: bool | None = None
    above_seven: Literal['consult', '10'] | None = None


@router.put('/api/admin/whatsapp/delivery', dependencies=[Depends(require_admin)])
def configure(body: DeliverySettings, request: Request):
    from . import whatsapp
    with db(request) as conn:
        old = config(conn)
        origin = map_point(body.map_url) if body.map_url else point(body.origin) if body.origin is not None else old['origin']
        if (body.map_url or body.origin is not None) and not origin:
            raise HTTPException(422, 'Use um link do Google Maps com o pino em coordenadas. O centro da tela do mapa não identifica o endereço.')
        value = {'address': body.address if 'address' in body.model_fields_set else old['address'], 'origin': origin,
                 'five_to_six': body.five_to_six if 'five_to_six' in body.model_fields_set else old['five_to_six'],
                 'fees_enabled': old['fees_enabled'] if body.fees_enabled is None else body.fees_enabled,
                 'above_seven': body.above_seven or old['above_seven']}
        whatsapp.put_setting(conn, 'delivery_estimates', json.dumps(value))
        return value | {'policy': policy(conn)}


class QuoteInput(BaseModel):
    model_config = ConfigDict(extra='forbid')
    location: dict | None = None


@router.post('/api/delivery/quote')
def quote_endpoint(body: QuoteInput, request: Request):
    with db(request) as conn:
        return {'fee': fee_quote(conn, body.location), 'policy': fee_policy(conn),
                'estimate': label(conn, body.location)}


class FeeInput(BaseModel):
    model_config = ConfigDict(extra='forbid')
    amount_cents: int = Field(strict=True, ge=0, le=100000)


@router.put('/api/admin/orders/{order_id}/delivery-fee', dependencies=[Depends(require_admin)])
def resolve_fee(order_id: str, body: FeeInput, request: Request):
    from .core import order_dict
    with db(request) as conn:
        order = conn.execute('SELECT * FROM orders WHERE id=?', (order_id,)).fetchone()
        if not order:
            raise HTTPException(404, 'Pedido não encontrado.')
        delivery = json.loads(order['delivery_json'])
        if (order['source'] == 'ifood' or order['payment_status'] == 'paid' or order['status'] in ('cancelled', 'canceled')
                or 'fee' not in delivery or conn.execute('SELECT 1 FROM receivables WHERE order_id=?', (order_id,)).fetchone()):
            raise HTTPException(409, 'Não é possível alterar a taxa deste pedido.')
        delivery['fee'] = {'status': 'quoted', 'amount_cents': body.amount_cents, 'confirmed_by': 'staff'}
        conn.execute('UPDATE orders SET delivery_json=?,total_cents=? WHERE id=?',
                     (json.dumps(delivery, ensure_ascii=False), order['subtotal_cents'] - order['discount_cents'] + body.amount_cents, order_id))
        return {'order': order_dict(conn.execute('SELECT * FROM orders WHERE id=?', (order_id,)).fetchone())}
