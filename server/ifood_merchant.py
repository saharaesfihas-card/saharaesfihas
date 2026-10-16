"""Consulta manual e privada da loja de teste, sem alterar dados no iFood."""
import json
import math
import re
import secrets
import time
from urllib.request import Request as URLRequest

from fastapi import APIRouter, Depends, Request
from pydantic import BaseModel, ConfigDict

from . import ifood, ifood_stores, ifood_homologation_profile as test_profile
from .core import db, require_admin, utcnow

router = APIRouter()
COOLDOWN = 30
LEASE = 180
MAX_RESPONSE = 393216
MESSAGES = {
    'available': 'Consulta manual disponível para a loja de teste verificada. Os dados não alteram a loja.',
    'not_ready': 'Confira a configuração e teste a conexão com a loja de teste antes de consultar o Merchant.',
    'restricted': 'Esta consulta está limitada à loja de teste previamente confirmada. As lojas reais continuam pendentes.',
    'configuration_changed': 'A configuração ou a verificação da conexão mudou. Teste novamente a conexão antes de consultar.',
    'wait': 'Uma consulta Merchant está em andamento ou foi feita recentemente. Aguarde antes de consultar novamente.',
    'queried': 'Consulta da loja de teste concluída. Confira os dados e a disponibilidade retornados pelo iFood.',
}
PHASES = {'token', 'merchants', 'merchant', 'availability'}
SECRET_KEY = re.compile(r'secret|token|password|authorization|credential|apikey|senha', re.I)


def initialize(conn):
    conn.execute('''CREATE TABLE IF NOT EXISTS ifood_merchant_query (
        id INTEGER PRIMARY KEY CHECK(id=1), fingerprint TEXT NOT NULL,
        started_at REAL NOT NULL, lease_token TEXT NOT NULL,
        result_json TEXT NOT NULL DEFAULT '')''')


class ConsultationInput(BaseModel):
    model_config = ConfigDict(extra='forbid')


def result(state, **extra):
    return {'state': state, 'message': MESSAGES.get(state, ifood.MESSAGES.get(state, ifood.MESSAGES['provider'])),
            'checked_at': utcnow(), **extra}


def readiness(request, cfg):
    if cfg.get('profile_mode') == 'homologation':
        return test_profile.readiness(request, cfg)
    if not cfg['ready'] or cfg['environment'] != 'test':
        return 'not_ready'
    merchant_id = cfg[ifood.REQUIRED[2]].lower()
    if merchant_id != ifood_stores.TEST_MERCHANT_ID:
        return 'restricted'
    with db(request) as conn:
        row = conn.execute('SELECT fingerprint,result_json FROM ifood_connection_check WHERE id=1').fetchone()
    try:
        checked = json.loads(row['result_json']) if row and row['result_json'] else None
    except (ValueError, TypeError, RecursionError):
        checked = None
    if (not row or row['fingerprint'] != ifood.fingerprint(cfg) or not isinstance(checked, dict)
            or checked.get('state') != 'verified' or checked.get('merchant_id') != merchant_id):
        return 'not_ready'
    return 'available'


def unchanged(request, cfg, digest):
    if ifood.fingerprint(test_profile.current_config(cfg)) != digest or readiness(request, cfg) != 'available':
        raise ifood.ProviderError('configuration_changed')


def dashboard(request):
    cfg = test_profile.selected_config()
    digest = ifood.fingerprint(cfg)
    state = readiness(request, cfg)
    with db(request) as conn:
        row = conn.execute('SELECT fingerprint,result_json FROM ifood_merchant_query WHERE id=1').fetchone()
    last = json.loads(row['result_json']) if row and row['fingerprint'] == digest and row['result_json'] else None
    if ifood.fingerprint(test_profile.current_config(cfg)) != digest:
        state, last = 'configuration_changed', None
    return {'query_available': state == 'available', 'state': state, 'message': MESSAGES[state],
            'environment': cfg['environment'] if cfg['environment'] in ('test', 'production') else 'unknown',
            'configured_merchant_id': cfg[ifood.REQUIRED[2]].lower() if cfg['valid'] else None,
            'last_result': last, **test_profile.metadata(cfg)}


def sanitize(value, private_values, max_response=MAX_RESPONSE, max_nodes=10000):
    """Retém os dados JSON, removendo segredos inclusive em estruturas desconhecidas."""
    count = 0
    private_values = sorted({item for item in private_values if item}, key=len, reverse=True)

    def clean_text(value):
        for private in private_values:
            value = value.replace(private, '[redigido]')
        return value

    def visit(value, depth=0):
        nonlocal count
        count += 1
        if depth > 20 or count > max_nodes:
            raise ValueError()
        if isinstance(value, dict):
            return {clean_text(key): visit(item, depth + 1) for key, item in value.items()
                    if not SECRET_KEY.search(re.sub(r'[^a-z0-9]', '', key.lower()))}
        if isinstance(value, list):
            return [visit(item, depth + 1) for item in value]
        if isinstance(value, str):
            return clean_text(value)
        if isinstance(value, float) and not math.isfinite(value):
            raise ValueError()
        if isinstance(value, (int, float)) and not isinstance(value, bool):
            encoded = json.dumps(value, allow_nan=False)
            return '[redigido]' if any(private in encoded for private in private_values) else value
        if value is None or isinstance(value, (bool, int, float)):
            return value
        raise ValueError()

    cleaned = visit(value)
    if len(json.dumps(cleaned, ensure_ascii=False, allow_nan=False).encode()) > max_response:
        raise ValueError()
    return cleaned


def query(request):
    cfg = test_profile.selected_config()
    state = readiness(request, cfg)
    if state != 'available':
        return result(state)
    stamp, digest, lease_token = time.time(), ifood.fingerprint(cfg), secrets.token_hex(16)
    with db(request) as conn:
        row = conn.execute('SELECT started_at,result_json FROM ifood_merchant_query WHERE id=1').fetchone()
        interval = COOLDOWN if row and row['result_json'] else LEASE
        if row and stamp - row['started_at'] < interval:
            return result('wait')
        conn.execute("INSERT INTO ifood_merchant_query VALUES(1,?,?,?,'') ON CONFLICT(id) DO UPDATE SET "
                     "fingerprint=excluded.fingerprint,started_at=excluded.started_at,"
                     "lease_token=excluded.lease_token,result_json=''", (digest, stamp, lease_token))
    token = None
    try:
        unchanged(request, cfg, digest)
        token = ifood.access_token(cfg)
        merchant_id = cfg[ifood.REQUIRED[2]].lower()
        headers = {'Authorization': 'Bearer ' + token, 'Accept': 'application/json'}

        def read(path, phase, expected, limit=65536):
            unchanged(request, cfg, digest)
            return ifood.provider_json(URLRequest(ifood.HOST + path, headers=headers), phase, expected, limit)

        merchants = []
        for page in range(1, 4):
            suffix = '' if page == 1 else '?page=' + str(page) + '&size=100'
            batch = read('/merchant/v1.0/merchants' + suffix, 'merchants', list, 262144)
            merchants.extend(batch)
            if len(batch) < 100:
                break
        else:
            # Never present a bounded, truncated list as all linked shops.
            raise ifood.ProviderError('response', 'merchants')
        if len(merchants) > 1000 or any(not isinstance(item, dict) or not ifood.identifier(item.get('id'))
                                         for item in merchants):
            raise ifood.ProviderError('response', 'merchants')
        identifiers = [item['id'].lower() for item in merchants]
        if merchant_id not in identifiers or len(identifiers) != len(set(identifiers)):
            raise ifood.ProviderError('response', 'merchants')
        merchant = read('/merchant/v1.0/merchants/' + merchant_id, 'merchant', dict)
        if not isinstance(merchant.get('id'), str) or merchant['id'].lower() != merchant_id:
            raise ifood.ProviderError('response', 'merchant')
        if cfg['profile_mode'] == 'homologation' and merchant.get('test') != 'TEST':
            raise ifood.ProviderError('response', 'merchant')
        availability = read('/merchant/v1.0/merchants/' + merchant_id + '/status', 'availability', list)
        if len(availability) > 100 or any(not isinstance(item, dict) for item in availability):
            raise ifood.ProviderError('response', 'availability')
        unchanged(request, cfg, digest)
        answer = sanitize(result('queried', environment='test', client_id=cfg[ifood.REQUIRED[0]],
            configured_merchant_id=merchant_id, merchants=merchants, merchant=merchant, availability=availability,
            profile_mode=cfg['profile_mode']),
            (cfg[ifood.REQUIRED[1]], token))
    except ifood.ProviderError as error:
        answer = result(error.state, **({'phase': error.phase} if error.phase in PHASES else {}))
    except (ValueError, TypeError, RecursionError, OverflowError):
        answer = result('response')
    if ifood.fingerprint(test_profile.current_config(cfg)) != digest or readiness(request, cfg) != 'available':
        answer = result('configuration_changed')
    # Somente o estado da consulta é persistido; listas e dados da loja ficam na resposta privada.
    saved = {key: value for key, value in answer.items() if key in ('state', 'message', 'checked_at', 'phase')}
    with db(request) as conn:
        conn.execute('UPDATE ifood_merchant_query SET result_json=? WHERE id=1 AND fingerprint=? AND lease_token=?',
                     (json.dumps(saved, ensure_ascii=False), digest, lease_token))
    return answer


@router.get('/api/admin/ifood/merchant')
def status(request: Request, admin=Depends(require_admin)):
    return dashboard(request)


@router.post('/api/admin/ifood/merchant')
def consult(body: ConsultationInput, request: Request, admin=Depends(require_admin)):
    return query(request)
