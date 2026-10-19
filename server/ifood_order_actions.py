"""Comandos manuais para pedidos já importados da loja fixa de teste.

HTTP 202 registra uma solicitação aceita; não altera etapas, estoque ou recebimentos.
Não há reconciliação automática sem os contratos dos eventos de ciclo de vida.
"""
import hashlib
import json
import time
from typing import Literal
import uuid

from fastapi import APIRouter, Depends, HTTPException, Request
from pydantic import BaseModel, ConfigDict, Field, StrictStr, model_validator

from . import ifood, ifood_stores, ifood_test_api, ifood_homologation_profile as test_profile
from .core import db, require_admin, utcnow

router = APIRouter()
LEASE = 180
PREFIX = '/api/admin/ifood/orders/{order_id}'
MESSAGES = {
    'accepted_pending': 'Solicitação aceita pelo iFood para processamento na loja de teste. A etapa ainda não foi confirmada.',
    'in_progress': 'Há uma solicitação em andamento para este pedido de teste. Aguarde antes de enviar outra.',
    'uncertain': 'Não foi possível confirmar o resultado da solicitação. Confira o pedido no Portal do iFood; novos comandos estão bloqueados.',
    'already_requested': 'Essa ação já foi aceita para este pedido de teste. Confira o resultado no Portal do iFood.',
    'refused': 'O iFood não aceitou a solicitação. Confira o pedido e tente novamente com uma nova chave após corrigir o problema.',
    'invalid_target': 'O pedido não corresponde ao pedido de entrega da loja de teste configurada.',
    'invalid_cancellation_code': 'Este motivo de cancelamento não está disponível para o pedido no iFood.',
}


class ActionInput(BaseModel):
    model_config = ConfigDict(extra='forbid')
    action: Literal['confirm', 'startPreparation', 'readyToPickup', 'dispatch', 'requestCancellation']
    idempotency_key: StrictStr = Field(min_length=8, max_length=128, pattern=r'^[A-Za-z0-9][A-Za-z0-9:_-]*$')
    cancellation_code: StrictStr | None = Field(default=None, min_length=1, max_length=100)
    reason: StrictStr | None = Field(default=None, min_length=1, max_length=500)

    @model_validator(mode='after')
    def validate_cancellation(self):
        if self.action == 'requestCancellation':
            if not self.cancellation_code or not self.reason or not self.reason.strip():
                raise ValueError('Informe um motivo atual do iFood e a justificativa do cancelamento.')
            if any(ord(char) < 32 or ord(char) == 127 for char in self.cancellation_code + self.reason):
                raise ValueError('Use texto simples para o cancelamento.')
        elif self.cancellation_code is not None or self.reason is not None:
            raise ValueError('Os campos de cancelamento só são aceitos para solicitar cancelamento.')
        return self


def initialize(conn):
    conn.execute('''CREATE TABLE IF NOT EXISTS ifood_order_action_attempts (
        id TEXT PRIMARY KEY, order_id TEXT NOT NULL REFERENCES orders(id),
        external_order_id TEXT NOT NULL, merchant_id TEXT NOT NULL, client_id TEXT NOT NULL,
        fingerprint TEXT NOT NULL, idempotency_key TEXT NOT NULL, request_hash TEXT NOT NULL,
        action TEXT NOT NULL, state TEXT NOT NULL, started_at REAL NOT NULL,
        updated_at TEXT NOT NULL, http_status INTEGER, error_state TEXT,
        UNIQUE(order_id,idempotency_key))''')
    conn.execute('CREATE INDEX IF NOT EXISTS ifood_order_action_order ON ifood_order_action_attempts(order_id,started_at)')


def target(request, order_id):
    """O ID do provedor vem exclusivamente do mapeamento persistido do importador."""
    with db(request) as conn:
        row = conn.execute('SELECT o.source,o.delivery_json,m.external_id,m.merchant_id FROM orders o '
                           'LEFT JOIN ifood_imported_orders m ON m.order_id=o.id WHERE o.id=?', (order_id,)).fetchone()
    if not row:
        raise HTTPException(404, 'Pedido não encontrado.')
    try:
        metadata = json.loads(row['delivery_json']).get('ifood', {})
        profile = metadata.get('profile_mode', 'legacy')
        if profile not in ('legacy', 'homologation'):
            raise ValueError()
        if profile == 'homologation':
            valid_merchant = row['merchant_id'] == test_profile.TEST_MERCHANT_ID
            valid_client = metadata.get('client_id') == test_profile.CLIENT_ID
        else:
            valid_merchant = row['merchant_id'] == ifood_stores.TEST_MERCHANT_ID
            legacy_cfg = ifood_test_api.configuration(legacy=True)
            valid_client = not metadata.get('client_id') or metadata.get('client_id') == legacy_cfg[ifood.REQUIRED[0]].lower()
        valid = (row['source'] == 'ifood' and valid_merchant and valid_client
                 and ifood.identifier(row['external_id']) and metadata.get('environment') == 'test'
                 and metadata.get('order_id') == row['external_id'])
    except (ValueError, TypeError, AttributeError, RecursionError):
        profile = 'legacy'
        valid = False
    if not valid:
        raise HTTPException(409, MESSAGES['invalid_target'])
    session = ifood_test_api.Session(request, order_ids=(row['external_id'],), legacy=(profile != 'homologation'))
    if session.merchant_id != row['merchant_id']:
        raise HTTPException(409, MESSAGES['invalid_target'])
    return session, row['external_id']


def answer(row, state=None):
    state = state or row['state']
    if state in ('reserved', 'posting'):
        state = 'in_progress'
    return {'state': state, 'message': MESSAGES.get(state, MESSAGES['refused']),
            'environment': 'test', 'attempt_id': row['id'], 'action': row['action'], 'idempotency_key': row['idempotency_key'],
            'checked_at': row['updated_at'], 'http_status': row['http_status'],
            'error_state': row['error_state'], 'local_status_changed': False,
            'profile_mode': 'homologation' if row['client_id'].lower() == test_profile.CLIENT_ID else 'legacy',
            'client_id': row['client_id'],
            'configured_merchant_id': row['merchant_id']}


def order_profile(request, order_id):
    with db(request) as conn:
        row = conn.execute('SELECT delivery_json FROM orders WHERE id=?', (order_id,)).fetchone()
    try:
        return json.loads(row['delivery_json']).get('ifood', {}).get('profile_mode', 'legacy') if row else 'legacy'
    except (ValueError, TypeError, AttributeError, RecursionError):
        return 'legacy'


def expire(conn, order_id):
    # Uma tentativa interrompida pode ter chegado ao provedor. Não repetir automaticamente.
    conn.execute("UPDATE ifood_order_action_attempts SET state='uncertain',updated_at=? "
                 "WHERE order_id=? AND state IN ('reserved','posting') AND started_at<?",
                 (utcnow(), order_id, time.time() - LEASE))


def provider_failure(error, profile='legacy'):
    cfg = ifood_test_api.configuration(legacy=(profile != 'homologation'))
    if profile == 'homologation' and cfg.get('profile_mode') != 'homologation':
        return {'state': error.state, 'message': ifood.MESSAGES.get(error.state, ifood.MESSAGES['provider']),
                'environment': 'test', 'http_status': getattr(error, 'http_status', None),
                'profile_mode': 'homologation', 'client_id': None, 'configured_merchant_id': None}
    return {'state': error.state, 'message': ifood.MESSAGES.get(error.state, ifood.MESSAGES['provider']),
            'environment': 'test', 'http_status': getattr(error, 'http_status', None),
            **ifood_test_api.metadata(cfg)}


def details(session, external_id):
    _, data = session.call('GET', '/order/v1.0/orders/' + external_id, statuses=(200,), expected=dict)
    if (data.get('id') != external_id or not isinstance(data.get('merchant'), dict)
            or data['merchant'].get('id') != session.merchant_id or data.get('orderType') != 'DELIVERY'):
        raise ifood.ProviderError('response', 'order')


def reasons(session, external_id):
    status, data = session.call('GET', '/order/v1.0/orders/' + external_id + '/cancellationReasons',
                                statuses=(200, 204), expected=(list, type(None)))
    if status == 204:
        return []
    if not isinstance(data, list) or len(data) > 200:
        raise ifood.ProviderError('response', 'order')
    result = []
    codes = set()
    for value in data:
        if not isinstance(value, dict):
            raise ifood.ProviderError('response', 'order')
        code, description = value.get('cancelCodeId'), value.get('description')
        if (not isinstance(code, str) or not 1 <= len(code) <= 100 or code in codes
                or not isinstance(description, str) or len(description) > 2000):
            raise ifood.ProviderError('response', 'order')
        codes.add(code)
        result.append({'cancelCodeId': code, 'description': description})
    return result


def finish(request, attempt_id, state, http_status=None, error_state=None):
    with db(request) as conn:
        conn.execute('UPDATE ifood_order_action_attempts SET state=?,updated_at=?,http_status=?,error_state=? '
                     "WHERE id=? AND state IN ('reserved','posting')",
                     (state, utcnow(), http_status, error_state, attempt_id))
        return answer(conn.execute('SELECT * FROM ifood_order_action_attempts WHERE id=?', (attempt_id,)).fetchone())


def execute(request, order_id, body):
    profile = order_profile(request, order_id)
    try:
        session, external_id = target(request, order_id)
        session.unchanged()
    except ifood.ProviderError as error:
        return provider_failure(error, profile)
    digest = hashlib.sha256(json.dumps(body.model_dump(), sort_keys=True, ensure_ascii=False).encode()).hexdigest()
    attempt_id = uuid.uuid4().hex
    with db(request) as conn:
        expire(conn, order_id)
        previous = conn.execute('SELECT * FROM ifood_order_action_attempts WHERE order_id=? AND idempotency_key=?',
                                (order_id, body.idempotency_key)).fetchone()
        if previous:
            if previous['request_hash'] != digest:
                raise HTTPException(409, 'Esta chave já foi usada para outra solicitação deste pedido.')
            return answer(previous)
        active = conn.execute("SELECT * FROM ifood_order_action_attempts WHERE order_id=? "
                              "AND state IN ('reserved','posting','uncertain') ORDER BY started_at DESC LIMIT 1", (order_id,)).fetchone()
        if active:
            return answer(active)
        accepted = conn.execute("SELECT * FROM ifood_order_action_attempts WHERE order_id=? AND action=? "
                                "AND state='accepted_pending' ORDER BY started_at DESC LIMIT 1", (order_id, body.action)).fetchone()
        if accepted:
            return answer(accepted, 'already_requested')
        conn.execute('INSERT INTO ifood_order_action_attempts VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?)',
                     (attempt_id, order_id, external_id, session.merchant_id, session.client_id,
                      session.digest, body.idempotency_key, digest, body.action, 'reserved', time.time(), utcnow(), None, None))
    post_started = False
    try:
        details(session, external_id)
        payload = None
        if body.action == 'requestCancellation':
            available = reasons(session, external_id)
            if body.cancellation_code not in {value['cancelCodeId'] for value in available}:
                return finish(request, attempt_id, 'refused', error_state='invalid_cancellation_code')
            payload = {'cancellationCode': body.cancellation_code, 'reason': body.reason.strip()}
        session.unchanged()
        with db(request) as conn:
            conn.execute("UPDATE ifood_order_action_attempts SET state='posting',updated_at=? WHERE id=? AND state='reserved'",
                         (utcnow(), attempt_id))
            current = conn.execute('SELECT state FROM ifood_order_action_attempts WHERE id=?', (attempt_id,)).fetchone()
            if not current or current['state'] != 'posting':
                return answer(conn.execute('SELECT * FROM ifood_order_action_attempts WHERE id=?', (attempt_id,)).fetchone())
        post_started = True
        status, _ = session.call('POST', '/order/v1.0/orders/' + external_id + '/' + body.action,
                                 payload=payload, statuses=(202,), expected=None)
        return finish(request, attempt_id, 'accepted_pending', http_status=status)
    except ifood.ProviderError as error:
        uncertain = post_started and getattr(error, 'uncertain',
            error.state in ('network', 'response', 'configuration_changed')
            or (getattr(error, 'http_status', None) or 0) >= 500)
        return finish(request, attempt_id, 'uncertain' if uncertain else 'refused',
                      http_status=getattr(error, 'http_status', None), error_state=error.state)
    except Exception:
        return finish(request, attempt_id, 'uncertain' if post_started else 'refused', error_state='provider')


@router.get(PREFIX + '/actions')
def status(order_id: str, request: Request, admin=Depends(require_admin)):
    profile = order_profile(request, order_id)
    try:
        session, _ = target(request, order_id)
        session.unchanged()
    except ifood.ProviderError as error:
        return {**provider_failure(error, profile), 'actions_available': False, 'attempts': []}
    with db(request) as conn:
        expire(conn, order_id)
        rows = conn.execute('SELECT * FROM ifood_order_action_attempts WHERE order_id=? ORDER BY started_at DESC LIMIT 50',
                            (order_id,)).fetchall()
        blocked = conn.execute("SELECT 1 FROM ifood_order_action_attempts WHERE order_id=? AND state IN ('reserved','posting','uncertain') LIMIT 1",
                               (order_id,)).fetchone() is not None
    return {'environment': 'test', 'actions_available': not blocked, 'attempts': [answer(row) for row in rows],
            **ifood_test_api.metadata(session.cfg),
            'local_status_changed': False}


@router.get(PREFIX + '/cancellation-reasons')
def cancellation_reasons(order_id: str, request: Request, admin=Depends(require_admin)):
    profile = order_profile(request, order_id)
    try:
        session, external_id = target(request, order_id)
        details(session, external_id)
        values = reasons(session, external_id)
        session.unchanged()
        return {'environment': 'test', 'state': 'queried', 'reasons': values, 'checked_at': utcnow(),
                **ifood_test_api.metadata(session.cfg)}
    except ifood.ProviderError as error:
        return provider_failure(error, profile)


@router.post(PREFIX + '/actions')
def action(order_id: str, body: ActionInput, request: Request, admin=Depends(require_admin)):
    return execute(request, order_id, body)
