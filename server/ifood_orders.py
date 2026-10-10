"""Importação restrita à loja de teste; sem comandos de produção ou recebimentos locais."""
from contextlib import contextmanager
from datetime import datetime, timezone
from decimal import Decimal, InvalidOperation
import hashlib
import json
import os
import secrets
import threading
import time
from types import SimpleNamespace
from urllib.request import Request as URLRequest
import uuid

from fastapi import APIRouter, Depends, Request

from . import ifood
from .core import db, require_admin, utcnow

router = APIRouter()
EVENTS = ifood.HOST + '/events/v1.0'
COOLDOWN = 60
MAX_DETAILS = 3
MESSAGES = {
    'not_ready': 'Confira a configuração e teste a conexão com a loja antes de importar pedidos.',
    'wait': 'Uma consulta está em andamento ou foi feita recentemente. Aguarde um minuto e atualize a configuração.',
    'empty': 'Consulta concluída. Nenhum novo pedido de teste disponível nesta consulta.',
    'imported': 'Pedidos de teste salvos no PDV. Os eventos foram confirmados após a gravação.',
    'partial': 'Consulta parcial. Há eventos pendentes; confira os contadores e o Gestor de Pedidos do iFood.',
}


def initialize(conn):
    conn.execute('''CREATE TABLE IF NOT EXISTS ifood_import_state (
        id INTEGER PRIMARY KEY CHECK(id=1), fingerprint TEXT NOT NULL,
        started_at REAL NOT NULL, result_json TEXT NOT NULL DEFAULT '')''')
    conn.execute('''CREATE TABLE IF NOT EXISTS ifood_imported_orders (
        merchant_id TEXT NOT NULL, external_id TEXT NOT NULL,
        order_id TEXT NOT NULL UNIQUE REFERENCES orders(id),
        PRIMARY KEY(merchant_id, external_id))''')
    conn.execute('''CREATE TABLE IF NOT EXISTS ifood_imported_events (
        merchant_id TEXT NOT NULL, client_id TEXT NOT NULL, event_id TEXT NOT NULL,
        external_order_id TEXT NOT NULL, created_at TEXT NOT NULL,
        acknowledged INTEGER NOT NULL DEFAULT 0,
        PRIMARY KEY(merchant_id, client_id, event_id))''')


def ready(request, cfg):
    if not cfg['ready']:
        return False
    with db(request) as conn:
        row = conn.execute('SELECT * FROM ifood_connection_check WHERE id=1').fetchone()
    return bool(row and row['fingerprint'] == ifood.fingerprint(cfg) and row['result_json']
                and json.loads(row['result_json']).get('state') == 'verified')


def dashboard(request):
    cfg = ifood.config()
    available = ready(request, cfg)
    merchant, client = cfg[ifood.REQUIRED[2]].lower(), cfg[ifood.REQUIRED[0]].lower()
    with db(request) as conn:
        row = conn.execute('SELECT * FROM ifood_import_state WHERE id=1').fetchone()
        pending = conn.execute('SELECT COUNT(*) FROM ifood_imported_events WHERE merchant_id=? AND client_id=? '
                               'AND acknowledged=0', (merchant, client)).fetchone()[0]
    last = json.loads(row['result_json']) if row and row['fingerprint'] == ifood.fingerprint(cfg) and row['result_json'] else None
    return {'import_available': available, 'automatic': available and os.environ.get('SAHARA_IFOOD_IMPORT_ENABLED') == '1',
            'last_sync': last, 'pending_ack': pending}


def text(value, limit=400):
    if value is None:
        return ''
    if not isinstance(value, str) or len(value) > limit:
        raise ValueError()
    return value


def cents(value):
    if isinstance(value, bool) or not isinstance(value, (int, float, str)):
        raise ValueError()
    try:
        number = Decimal(str(value)) * 100
        if not number.is_finite() or not 0 <= number <= 10_000_000_000 or number != number.to_integral_value():
            raise ValueError()
        return int(number)
    except InvalidOperation:
        raise ValueError() from None


def quantity(value):
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise ValueError()
    if not 0 < value <= 100_000:
        raise ValueError()
    return value


def timestamp(value):
    value = text(value, 100)
    parsed = datetime.fromisoformat(value.replace('Z', '+00:00'))
    if parsed.tzinfo is None:
        raise ValueError()
    return parsed.astimezone(timezone.utc).isoformat()


def options(values, nested=False):
    if not isinstance(values, list) or len(values) > 100:
        raise ValueError()
    result = []
    for option in values:
        result.append({'name': text(option.get('name')), 'group_name': text(option.get('groupName')),
            'quantity': quantity(option['quantity']), 'unit': text(option.get('unit')),
            'price_cents': cents(option.get('price', 0)), 'unit_price_cents': cents(option.get('unitPrice', 0)),
            'addition_cents': cents(option.get('addition', 0)),
            'customization': [] if nested else options(option.get('customization', []), True)})
    return result


def normalize_order(raw, cfg, external_id):
    merchant = cfg[ifood.REQUIRED[2]].lower()
    if (not isinstance(raw, dict) or not ifood.identifier(raw.get('id')) or raw['id'].lower() != external_id
            or not ifood.identifier(raw.get('merchant', {}).get('id'))
            or raw['merchant']['id'].lower() != merchant or raw.get('orderType') != 'DELIVERY'):
        raise ValueError()
    created = timestamp(raw['createdAt'])
    total = raw['total']
    amounts = {key: cents(total.get(key, 0)) for key in ('subTotal', 'benefits', 'deliveryFee', 'additionalFees')}
    amounts['orderAmount'] = cents(total['orderAmount'])
    items = raw['items']
    if not isinstance(items, list) or not 1 <= len(items) <= 100:
        raise ValueError()
    normalized_items = []
    for index, item in enumerate(items):
        normalized_items.append({'id': 'ifood:' + text(item.get('id') or str(index)),
            'name': text(item['name']), 'quantity': quantity(item['quantity']),
            'price_cents': cents(item['unitPrice']), 'total_cents': cents(item['totalPrice']),
            'options_price_cents': cents(item.get('optionsPrice', 0)), 'unit': text(item.get('unit')),
            'observations': text(item.get('observations'), 2000), 'options': options(item.get('options', []))})
    customer, delivery, payments = raw.get('customer', {}), raw['delivery'], raw['payments']
    address = delivery['deliveryAddress']
    location = None
    if address.get('coordinates') is not None:
        coordinates = address['coordinates']
        latitude, longitude = coordinates['latitude'], coordinates['longitude']
        if (isinstance(latitude, bool) or isinstance(longitude, bool)
                or not isinstance(latitude, (int, float)) or not isinstance(longitude, (int, float))
                or not -90 <= latitude <= 90 or not -180 <= longitude <= 180):
            raise ValueError()
        location = {'latitude': latitude, 'longitude': longitude}
    pending, prepaid = cents(payments['pending']), cents(payments['prepaid'])
    methods = payments['methods']
    if not isinstance(methods, list) or len(methods) > 20:
        raise ValueError()
    normalized_methods = []
    for method in methods:
        currency = text(method.get('currency'))
        if currency and currency != 'BRL':
            raise ValueError()
        normalized_methods.append({'method': text(method.get('method')), 'type': text(method.get('type')),
            'prepaid': method.get('prepaid') is True, 'value_cents': cents(method['value']),
            'brand': text(method.get('card', {}).get('brand')),
            'change_for_cents': cents(method.get('cash', {}).get('changeFor', 0))})
    schedule = raw.get('schedule') or {}
    scheduled = raw.get('orderTiming') == 'SCHEDULED'
    requested = timestamp(schedule['deliveryDateTimeStart']) if scheduled else None
    meta = {'environment': 'test', 'order_id': external_id, 'display_id': text(raw.get('displayId')),
        'delivery_fee_cents': amounts['deliveryFee'], 'additional_fees_cents': amounts['additionalFees'],
        'pending_cents': pending, 'prepaid_cents': prepaid, 'payment_methods': normalized_methods,
        'delivered_by': text(delivery.get('deliveredBy')), 'pickup_code': text(delivery.get('pickupCode')),
        'preparation_start': timestamp(raw['preparationStartDateTime']) if raw.get('preparationStartDateTime') else None,
        'schedule_end': timestamp(schedule['deliveryDateTimeEnd']) if scheduled else None}
    return {'created_at': created, 'requested_for': requested, 'items': normalized_items,
        'customer_name': text(customer.get('name'), 200),
        'customer_phone': text(customer.get('phone', {}).get('number'), 80),
        'delivery': {'street': text(address.get('streetName')), 'number': text(address.get('streetNumber')),
            'neighborhood': text(address.get('neighborhood')), 'complement': text(address.get('complement')),
            'city': text(address.get('city')), 'state': text(address.get('state')), 'postal_code': text(address.get('postalCode')),
            'reference': text(address.get('reference')), 'formatted_address': text(address.get('formattedAddress')),
            'location': location, 'ifood': meta},
        'subtotal_cents': amounts['subTotal'], 'discount_cents': amounts['benefits'],
        'total_cents': amounts['orderAmount'], 'notes': text(raw.get('extraInfo'), 2000),
        'payment_method': 'iFood · ' + ', '.join(m['method'] or m['type'] for m in normalized_methods),
        'payment_status': 'external_paid' if pending == 0 and prepaid >= amounts['orderAmount'] else 'external_pending'}


def persist(request, cfg, event, normalized=None):
    """Pedido e evento são gravados juntos antes de qualquer acknowledgment."""
    merchant, client = cfg[ifood.REQUIRED[2]].lower(), cfg[ifood.REQUIRED[0]].lower()
    with db(request) as conn:
        previous = conn.execute('SELECT * FROM ifood_imported_events WHERE merchant_id=? AND client_id=? AND event_id=?',
                                (merchant, client, event['id'])).fetchone()
        if previous:
            if previous['external_order_id'] != event['orderId']:
                raise ValueError()
            return False
        existing = conn.execute('SELECT order_id FROM ifood_imported_orders WHERE merchant_id=? AND external_id=?',
                                (merchant, event['orderId'])).fetchone()
        imported = existing is None
        if imported:
            if normalized is None:
                raise ValueError()
            identifier = uuid.uuid4().hex
            values = {'id': identifier, 'tracking_token': secrets.token_urlsafe(24),
                'idempotency_key': 'ifood:' + identifier,
                'request_hash': hashlib.sha256(json.dumps(normalized, sort_keys=True).encode()).hexdigest(),
                'delivery_json': json.dumps(normalized['delivery'], ensure_ascii=False),
                'items_json': json.dumps(normalized['items'], ensure_ascii=False),
                **{key: normalized[key] for key in ('created_at', 'requested_for', 'customer_name', 'customer_phone',
                    'subtotal_cents', 'discount_cents', 'total_cents', 'payment_method', 'payment_status', 'notes')}}
            conn.execute('INSERT INTO orders (' + ','.join(values) + ',source,status) VALUES ('
                         + ','.join('?' for _ in values) + ", 'ifood', 'ifood_received')", tuple(values.values()))
            conn.execute('INSERT INTO ifood_imported_orders VALUES(?,?,?)', (merchant, event['orderId'], identifier))
        conn.execute('INSERT INTO ifood_imported_events VALUES(?,?,?,?,?,0)',
                     (merchant, client, event['id'], event['orderId'], event['createdAt']))
    return imported


def sync(request, stop=None):
    cfg = ifood.config()
    if not ready(request, cfg):
        return {'state': 'not_ready', 'message': MESSAGES['not_ready']}
    stamp, digest = time.time(), ifood.fingerprint(cfg)
    with db(request) as conn:
        row = conn.execute('SELECT * FROM ifood_import_state WHERE id=1').fetchone()
        interval = COOLDOWN if row and row['result_json'] else 180
        previous = json.loads(row['result_json']) if row and row['result_json'] else {}
        if previous.get('state') == 'quota' or previous.get('error') == ifood.MESSAGES['quota']:
            interval = 600
        if row and stamp - row['started_at'] < interval:
            return {'state': 'wait', 'message': MESSAGES['wait']}
        conn.execute("INSERT INTO ifood_import_state VALUES(1,?,?,'') ON CONFLICT(id) DO UPDATE SET "
                     "fingerprint=excluded.fingerprint,started_at=excluded.started_at,result_json=''", (digest, stamp))
    counts = dict(imported=0, replayed=0, acknowledged=0, unsupported=0, failed=0, remaining=0)
    state, error_state = 'empty', None
    merchant, client = cfg[ifood.REQUIRED[2]].lower(), cfg[ifood.REQUIRED[0]].lower()
    try:
        token = ifood.access_token(cfg)
        headers = {'Authorization': 'Bearer ' + token, 'Accept': 'application/json'}
        events = ifood.provider_json(URLRequest(EVENTS + '/events:polling', headers=headers), 'events', list, 1_048_576)
        if len(events) > 2000:
            raise ifood.ProviderError('response')
        fetched = 0
        for raw in events:
            if stop and stop.is_set():
                counts['remaining'] += 1
                continue
            try:
                if not isinstance(raw, dict) or not ifood.identifier(raw.get('id')) or not ifood.identifier(raw.get('orderId')):
                    raise ValueError()
                if raw.get('fullCode') != 'PLACED' or raw.get('code') not in (None, 'PLC'):
                    counts['unsupported'] += 1
                    continue  # Eventos não implementados continuam pendentes no provedor.
                event = {'id': raw['id'].lower(), 'orderId': raw['orderId'].lower(), 'createdAt': timestamp(raw['createdAt'])}
                with db(request) as conn:
                    seen = conn.execute('SELECT 1 FROM ifood_imported_orders WHERE merchant_id=? AND external_id=?',
                                        (merchant, event['orderId'])).fetchone()
                normalized = None
                if not seen:
                    if fetched >= MAX_DETAILS:
                        counts['remaining'] += 1
                        continue
                    fetched += 1
                    raw_order = ifood.provider_json(URLRequest(ifood.HOST + '/order/v1.0/orders/' + event['orderId'],
                                                               headers=headers), 'order', limit=1_048_576)
                    normalized = normalize_order(raw_order, cfg, event['orderId'])
                if persist(request, cfg, event, normalized):
                    counts['imported'] += 1
                else:
                    counts['replayed'] += 1
            except (ValueError, KeyError, TypeError, AttributeError, OverflowError):
                counts['failed'] += 1
            except ifood.ProviderError as error:
                counts['failed'] += 1
                error_state = error.state
                break
        if not (stop and stop.is_set()):
            with db(request) as conn:
                pending = [row['event_id'] for row in conn.execute('SELECT event_id FROM ifood_imported_events '
                    'WHERE merchant_id=? AND client_id=? AND acknowledged=0 ORDER BY created_at LIMIT 100', (merchant, client))]
            if pending:
                ifood.provider_json(URLRequest(EVENTS + '/events/acknowledgment',
                    data=json.dumps([{'id': identifier} for identifier in pending]).encode(),
                    headers={**headers, 'Content-Type': 'application/json'}), 'ack', acknowledgement=True)
                with db(request) as conn:
                    conn.executemany('UPDATE ifood_imported_events SET acknowledged=1 WHERE merchant_id=? AND client_id=? AND event_id=?',
                                     [(merchant, client, identifier) for identifier in pending])
                counts['acknowledged'] = len(pending)
        if counts['imported'] or counts['acknowledged']:
            state = 'imported'
        if error_state or any(counts[k] for k in ('failed', 'unsupported', 'remaining')):
            state = 'partial'
    except ifood.ProviderError as error:
        state = 'partial' if counts['imported'] else error.state
        error_state = error.state
    answer = {'state': state, 'message': MESSAGES.get(state, ifood.MESSAGES.get(state, ifood.MESSAGES['response'])),
              **counts, 'checked_at': utcnow()}
    if error_state:
        answer['error'] = ifood.MESSAGES[error_state]
    with db(request) as conn:
        answer['pending_ack'] = conn.execute('SELECT COUNT(*) FROM ifood_imported_events WHERE merchant_id=? AND client_id=? '
                                            'AND acknowledged=0', (merchant, client)).fetchone()[0]
        conn.execute('UPDATE ifood_import_state SET result_json=? WHERE id=1 AND fingerprint=? AND started_at=?',
                     (json.dumps(answer, ensure_ascii=False), digest, stamp))
    return answer


@contextmanager
def worker(app):
    stop = threading.Event()
    def run():
        while not stop.is_set():
            outcome = None
            if os.environ.get('SAHARA_IFOOD_IMPORT_ENABLED') == '1':
                try:
                    outcome = sync(SimpleNamespace(app=app), stop)
                except Exception:
                    pass  # Lease expira; pedidos/eventos já gravados continuam no disco.
            stop.wait(600 if outcome and (outcome.get('state') == 'quota' or outcome.get('error') == ifood.MESSAGES['quota']) else 60)
    thread = threading.Thread(target=run, name='sahara-ifood-test', daemon=True)
    thread.start()
    try:
        yield
    finally:
        stop.set()
        thread.join(timeout=10)


@router.get('/api/admin/ifood/import')
def status(request: Request, admin=Depends(require_admin)):
    return dashboard(request)


@router.post('/api/admin/ifood/import')
def import_orders(request: Request, admin=Depends(require_admin)):
    return sync(request)
