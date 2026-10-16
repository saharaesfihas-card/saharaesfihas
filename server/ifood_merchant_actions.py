"""Pausas da loja de teste, com intenção persistida antes de cada escrita.

Contratos: coleção Merchant v1.0 recebida do Developer Portal. Nunca escolhe
merchant a partir do navegador. Não repete uma criação após resposta incerta.
"""
from datetime import datetime, timezone
import hashlib
import json
import re
import time
from uuid import UUID, uuid4

from fastapi import APIRouter, Depends, Request
from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from . import ifood, ifood_merchant, ifood_test_api
from .core import db, require_admin, utcnow

router = APIRouter()
PREFIX = '/api/admin/ifood/merchant'
MAX_PAUSES = 1000
LEASE_SECONDS = 180
RESOURCE_ID = re.compile(r'[A-Za-z0-9_-]{1,128}\Z')
MESSAGES = {
    'available': 'Operações disponíveis apenas para a loja de teste verificada.',
    'confirmed': 'Operação confirmada pelo iFood na loja de teste. Confira o reflexo no Portal do Parceiro.',
    'queried': 'Consulta da loja de teste concluída.',
    'processing': 'Operação em andamento. Consulte o resultado antes de enviar outra ação.',
    'uncertain': 'O resultado da escrita não foi confirmado. Consulte novamente; a criação não será reenviada.',
    'rejected': 'A operação não foi executada ou foi recusada pelo iFood.',
    'conflict': 'Esta chave já foi usada com outra operação. Atualize o painel antes de continuar.',
    'unresolved': 'Existe uma escrita pendente de confirmação. Reconcilie seu resultado antes de criar outra pausa.',
    'not_found': 'A pausa não foi criada por este sistema nesta configuração de teste.',
    'contract_missing': 'A coleção recebida não define a unidade de duração e o formato dos dias e horários. A escrita de horários aguarda essa referência oficial.',
}


def initialize(conn):
    # Ownership scope is stable across secret renewal; connection checks still
    # require the full current credential fingerprint before provider calls.
    conn.execute("""CREATE TABLE IF NOT EXISTS ifood_merchant_operations (
        id TEXT PRIMARY KEY, fingerprint TEXT NOT NULL, idempotency_key TEXT NOT NULL,
        kind TEXT NOT NULL, parent_id TEXT, resource_id TEXT, request_json TEXT NOT NULL,
        request_hash TEXT NOT NULL, state TEXT NOT NULL, result_json TEXT NOT NULL DEFAULT '{}',
        created_at TEXT NOT NULL, updated_at TEXT NOT NULL, started_at REAL NOT NULL,
        UNIQUE(fingerprint,idempotency_key))""")
    conn.execute("CREATE INDEX IF NOT EXISTS ifood_merchant_operations_state ON ifood_merchant_operations(fingerprint,state)")


class EmptyInput(BaseModel):
    model_config = ConfigDict(extra='forbid')


class KeyInput(EmptyInput):
    idempotency_key: UUID


class InterruptionInput(KeyInput):
    description: str = Field(min_length=1, max_length=512)
    start: datetime
    end: datetime

    @field_validator('description')
    @classmethod
    def clean_description(cls, value):
        value = value.strip()
        if not value or any(ord(char) < 32 or ord(char) == 127 for char in value):
            raise ValueError('Informe uma descrição sem caracteres de controle.')
        return value

    @field_validator('start', 'end')
    @classmethod
    def timezone_required(cls, value):
        if value.tzinfo is None or value.utcoffset() is None:
            raise ValueError('Informe a data e hora com fuso horário.')
        return value.astimezone(timezone.utc)

    @model_validator(mode='after')
    def ordered_dates(self):
        if self.end <= self.start:
            raise ValueError('O fim precisa ser posterior ao início.')
        return self


def result(state, **extra):
    return {'state': state, 'message': MESSAGES.get(state, ifood_merchant.MESSAGES.get(
        state, ifood.MESSAGES.get(state, ifood.MESSAGES['provider']))), 'checked_at': utcnow(), **extra}


def encode(value):
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(',', ':'), allow_nan=False)


def metadata(api):
    return {'environment': 'test', 'client_id': api.cfg[ifood.REQUIRED[0]],
            'configured_merchant_id': api.merchant_id}


def public_operation(row):
    return {'id': row['id'], 'idempotency_key': row['idempotency_key'], 'kind': row['kind'], 'parent_id': row['parent_id'],
            'resource_id': row['resource_id'], 'state': row['state'],
            'created_at': row['created_at'], 'updated_at': row['updated_at'],
            'result': json.loads(row['result_json'])}


def operations(request, digest):
    with db(request) as conn:
        return [public_operation(row) for row in conn.execute(
            'SELECT * FROM ifood_merchant_operations WHERE fingerprint=? ORDER BY created_at DESC LIMIT 30',
            (digest,))]


def reserve(request, api, key, kind, payload, parent_id=None, resource_id=None):
    """Commit the local intention before any provider write or token request."""
    encoded = encode(payload)
    signature = hashlib.sha256(encode([kind, parent_id, payload]).encode()).hexdigest()
    with db(request) as conn:
        old = conn.execute('SELECT * FROM ifood_merchant_operations WHERE fingerprint=? AND idempotency_key=?',
                           (api.scope, str(key))).fetchone()
        if old:
            if old['request_hash'] != signature:
                return None, result('conflict')
            return None, result(old['state'], operation=public_operation(old))
        if kind == 'remove_interruption':
            removed = conn.execute("SELECT * FROM ifood_merchant_operations WHERE fingerprint=? AND kind='remove_interruption' AND parent_id=? AND state='confirmed' ORDER BY created_at DESC LIMIT 1", (api.scope, parent_id)).fetchone()
            if removed:
                return None, result('confirmed', operation=public_operation(removed))
        pending = conn.execute("SELECT * FROM ifood_merchant_operations WHERE fingerprint=? AND state IN ('processing','uncertain') ORDER BY created_at LIMIT 1",
                               (api.scope,)).fetchone()
        if pending:
            return None, result('unresolved', operation=public_operation(pending))
        identifier, stamp = str(uuid4()), utcnow()
        if kind == 'create_interruption':
            payload = {**payload, 'id': identifier}
            encoded = encode(payload)
        conn.execute('INSERT INTO ifood_merchant_operations VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?)',
                     (identifier, api.scope, str(key), kind, parent_id, resource_id, encoded,
                      signature, 'processing', '{}', stamp, stamp, time.time()))
        row = conn.execute('SELECT * FROM ifood_merchant_operations WHERE id=?', (identifier,)).fetchone()
    return dict(row), None


def finish(request, api, row, state, detail=None, resource_id=None):
    clean = api.sanitized(detail or {})
    with db(request) as conn:
        conn.execute('UPDATE ifood_merchant_operations SET state=?,result_json=?,resource_id=COALESCE(?,resource_id),updated_at=? WHERE id=? AND fingerprint=? AND state<>?',
                     (state, encode(clean), resource_id, utcnow(), row['id'], api.scope, 'confirmed'))
        saved = conn.execute('SELECT * FROM ifood_merchant_operations WHERE id=?', (row['id'],)).fetchone()
    return result(saved['state'], operation=public_operation(saved), **metadata(api))


def date_equivalent(first, second):
    try:
        a = datetime.fromisoformat(first.replace('Z', '+00:00'))
        b = datetime.fromisoformat(second.replace('Z', '+00:00'))
        return a.tzinfo is not None and b.tzinfo is not None and a == b
    except (AttributeError, ValueError, TypeError):
        return False


def valid_pause(value):
    if not isinstance(value, dict):
        raise ifood.ProviderError('response', 'merchant_action')
    if (not isinstance(value.get('id'), str) or not RESOURCE_ID.fullmatch(value['id'])
            or not isinstance(value.get('description'), str) or len(value['description']) > 2000
            or not date_equivalent(value.get('start'), value.get('start'))
            or not date_equivalent(value.get('end'), value.get('end'))):
        raise ifood.ProviderError('response', 'merchant_action')
    return {key: value[key] for key in ('id', 'description', 'start', 'end')}


def matching_pause(value, intended):
    return (value['description'] == intended['description']
            and date_equivalent(value['start'], intended['start'])
            and date_equivalent(value['end'], intended['end']))


def provider(api, method, suffix, **kwargs):
    _, data = api.call(method, '/merchant/v1.0/merchants/' + api.merchant_id + suffix, **kwargs)
    return data


def pause_list(api):
    data = provider(api, 'GET', '/interruptions', expected=(dict, list))
    try:
        if not isinstance(data, list) or len(data) > MAX_PAUSES:
            raise ifood.ProviderError('response', 'merchant_action')
        pauses = [valid_pause(item) for item in data]
        if len({item['id'] for item in pauses}) != len(pauses):
            raise ifood.ProviderError('response', 'merchant_action')
        return pauses
    except ifood.ProviderError as error:
        # Manual private diagnosis retains only already-sanitized bounded JSON.
        # Reconciliation still fails closed; it never adopts malformed pauses.
        error.pause_response = data
        raise


def failed_write(request, api, row, error):
    state = 'uncertain' if getattr(error, 'uncertain', False) else 'rejected'
    return finish(request, api, row, state, {'provider_state': error.state})


def start_api(request):
    return ifood_test_api.Session(request)


def query_pauses(request):
    try:
        api = start_api(request)
        pauses = pause_list(api)
        api.unchanged()
        return api.sanitized(result('queried', interruptions=pauses, **metadata(api)))
    except ifood.ProviderError as error:
        diagnostic = {'provider_response': error.pause_response} if hasattr(error, 'pause_response') else {}
        return result(error.state, **diagnostic)


def create_pause(request, body):
    try:
        api = start_api(request)
        payload = {'description': body.description, 'start': body.start.isoformat(), 'end': body.end.isoformat()}
        row, previous = reserve(request, api, body.idempotency_key, 'create_interruption', payload)
        if previous:
            return previous
    except ifood.ProviderError as error:
        return result(error.state)
    returned = False
    try:
        api.unchanged()
        data = provider(api, 'POST', '/interruptions', payload=json.loads(row['request_json']),
                        statuses=(201,), expected=dict)
        returned = True
        created = valid_pause(data)
        if not matching_pause(created, payload):
            error = ifood.ProviderError('response', 'merchant_action')
            error.uncertain = True
            raise error
        api.unchanged()
        return finish(request, api, row, 'confirmed', {'interruption': created}, created['id'])
    except ifood.ProviderError as error:
        if returned:
            error.uncertain = True
        return failed_write(request, api, row, error)


def remove_pause(request, operation_id, body):
    try:
        api = start_api(request)
        with db(request) as conn:
            parent = conn.execute("SELECT * FROM ifood_merchant_operations WHERE id=? AND fingerprint=? AND kind='create_interruption' AND state='confirmed'",
                                  (str(operation_id), api.scope)).fetchone()
        if not parent or not RESOURCE_ID.fullmatch(parent['resource_id'] or ''):
            return result('not_found')
        payload = {'resource_id': parent['resource_id']}
        row, previous = reserve(request, api, body.idempotency_key, 'remove_interruption', payload,
                                parent_id=parent['id'], resource_id=parent['resource_id'])
        if previous:
            return previous
    except ifood.ProviderError as error:
        return result(error.state)
    returned = False
    try:
        api.unchanged()
        provider(api, 'DELETE', '/interruptions/' + row['resource_id'], statuses=(204,), expected=None)
        returned = True
        api.unchanged()
        return finish(request, api, row, 'confirmed', {'removed': True})
    except ifood.ProviderError as error:
        if returned:
            error.uncertain = True
        return failed_write(request, api, row, error)


def reconcile(request, operation_id):
    try:
        api = start_api(request)
        with db(request) as conn:
            row = conn.execute('SELECT * FROM ifood_merchant_operations WHERE id=? AND fingerprint=?',
                               (str(operation_id), api.scope)).fetchone()
        if not row:
            return result('not_found')
        if row['state'] in ('confirmed', 'rejected'):
            return result(row['state'], operation=public_operation(row), **metadata(api))
        # Avoid racing a request that might still be executing. This request is read-only.
        if row['state'] == 'processing' and time.time() - row['started_at'] < LEASE_SECONDS:
            return result('processing', operation=public_operation(row), **metadata(api))
        pauses = pause_list(api)
        api.unchanged()
        intended = json.loads(row['request_json'])
        if row['kind'] == 'create_interruption':
            matches = [item for item in pauses if item['id'] == intended['id'] and matching_pause(item, intended)]
            if len(matches) == 1:
                return finish(request, api, row, 'confirmed', {'interruption': matches[0], 'reconciled': True}, matches[0]['id'])
        elif row['kind'] == 'remove_interruption' and not any(item['id'] == row['resource_id'] for item in pauses):
            return finish(request, api, row, 'confirmed', {'removed': True, 'reconciled': True})
        return finish(request, api, row, 'uncertain', {'reconciled': True})
    except ifood.ProviderError as error:
        return result(error.state)


def query_hours(request):
    try:
        api = start_api(request)
        hours = provider(api, 'GET', '/opening-hours', expected=(dict, list))
        # Read-only display retains the provider's bounded JSON. Some responses
        # use an object envelope; no schedule units or write schema are inferred.
        hours = [hours] if isinstance(hours, dict) else hours
        if len(hours) > 1000 or any(not isinstance(item, dict) for item in hours):
            raise ifood.ProviderError('response', 'merchant_action')
        api.unchanged()
        return api.sanitized(result('queried', opening_hours=hours, **metadata(api)))
    except ifood.ProviderError as error:
        return result(error.state)


@router.get(PREFIX + '/actions')
def dashboard(request: Request, admin=Depends(require_admin)):
    cfg = ifood.config()
    state = ifood_merchant.readiness(request, cfg)
    digest = ifood.fingerprint(cfg)
    rows = operations(request, ifood_test_api.scope(cfg)) if state == 'available' else []
    if ifood.fingerprint(ifood.config()) != digest:
        state, rows = 'configuration_changed', []
    extra = {'environment': cfg['environment'] if cfg['environment'] in ('test', 'production') else 'unknown'}
    if state == 'available':
        extra.update(client_id=cfg[ifood.REQUIRED[0]], configured_merchant_id=cfg[ifood.REQUIRED[2]].lower())
    return result(state, writes_available=state == 'available', hours_write_available=False,
                  hours_write_reason=MESSAGES['contract_missing'], operations=rows, **extra)


@router.post(PREFIX + '/interruptions/query')
def list_interruptions(body: EmptyInput, request: Request, admin=Depends(require_admin)):
    return query_pauses(request)


@router.post(PREFIX + '/interruptions')
def create_interruption(body: InterruptionInput, request: Request, admin=Depends(require_admin)):
    return create_pause(request, body)


@router.post(PREFIX + '/interruptions/{operation_id}/remove')
def remove_interruption(operation_id: UUID, body: KeyInput, request: Request, admin=Depends(require_admin)):
    return remove_pause(request, operation_id, body)


@router.post(PREFIX + '/operations/{operation_id}/reconcile')
def reconcile_operation(operation_id: UUID, body: EmptyInput, request: Request, admin=Depends(require_admin)):
    return reconcile(request, operation_id)


@router.post(PREFIX + '/opening-hours/query')
def get_hours(body: EmptyInput, request: Request, admin=Depends(require_admin)):
    return query_hours(request)
