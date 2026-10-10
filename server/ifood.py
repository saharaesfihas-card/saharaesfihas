"""Diagnóstico privado de Client Credentials e acesso à loja; sem importar pedidos."""
import hashlib
import json
import os
import re
import time
from urllib.error import HTTPError, URLError
from urllib.parse import urlencode
from urllib.request import Request as URLRequest
from uuid import UUID

from fastapi import APIRouter, Depends, Request

from . import evolution
from .core import db, require_admin, utcnow

router = APIRouter()
HOST = 'https://merchant-api.ifood.com.br'
REQUIRED = ('SAHARA_IFOOD_CLIENT_ID', 'SAHARA_IFOOD_CLIENT_SECRET', 'SAHARA_IFOOD_MERCHANT_ID')
MESSAGES = {
    'configured': 'Variáveis cadastradas. Toque em Testar conexão com o iFood para verificar a autenticação e a loja.',
    'disabled': 'Ative SAHARA_IFOOD_ENABLED=1 no serviço do PDV para testar a conexão.',
    'missing': 'Cadastre as variáveis do iFood no serviço do PDV, sem enviar segredos pelo chat.',
    'invalid': 'Confira os IDs da aplicação e da loja e o segredo nas variáveis do servidor.',
    'mode': 'Este diagnóstico está preparado apenas para a loja de teste. Use SAHARA_IFOOD_ENVIRONMENT=test.',
    'authentication': 'O iFood recusou a autenticação. Confira o clientId e o clientSecret da aplicação centralizada.',
    'permissions': 'O iFood recusou o acesso à loja. Confira as permissões da aplicação.',
    'merchant': 'A loja informada não foi encontrada ou não está acessível por esta aplicação.',
    'quota': 'O iFood limitou as consultas. Aguarde antes de testar novamente.',
    'provider': 'O iFood não concluiu a consulta. Tente novamente mais tarde.',
    'network': 'Não foi possível consultar o iFood. Confira a conexão do servidor.',
    'response': 'O iFood devolveu uma resposta inesperada. A conexão não foi confirmada.',
    'wait': 'Aguarde 30 segundos entre os testes de conexão do iFood.',
    'verified': 'Autenticação e acesso à loja de teste confirmados. O recebimento de pedidos ainda não está ativo.',
}


def initialize(conn):
    conn.execute('''CREATE TABLE IF NOT EXISTS ifood_connection_check (
        id INTEGER PRIMARY KEY CHECK(id=1), fingerprint TEXT NOT NULL,
        started_at REAL NOT NULL, result_json TEXT NOT NULL DEFAULT '')''')


def identifier(value):
    try:
        return str(UUID(value)) == value.lower()
    except (ValueError, TypeError, AttributeError):
        return False


def config():
    values = {name: os.environ.get(name, '').strip() for name in REQUIRED}
    values['enabled'] = os.environ.get('SAHARA_IFOOD_ENABLED') == '1'
    values['environment'] = os.environ.get('SAHARA_IFOOD_ENVIRONMENT', 'test').strip()
    values['missing'] = [name for name in REQUIRED if not values[name]]
    values['valid'] = bool(identifier(values[REQUIRED[0]]) and identifier(values[REQUIRED[2]])
                           and 8 <= len(values[REQUIRED[1]]) <= 4096
                           and not any(ord(char) < 32 or ord(char) == 127 for char in values[REQUIRED[1]]))
    values['ready'] = values['enabled'] and not values['missing'] and values['valid'] and values['environment'] == 'test'
    return values


def fingerprint(cfg):
    return hashlib.sha256(json.dumps([cfg[name] for name in REQUIRED] +
                                    [cfg['enabled'], cfg['environment']]).encode()).hexdigest()


def result(state, **extra):
    return {'state': state, 'message': MESSAGES[state], **extra}


def dashboard(request):
    cfg = config()
    with db(request) as conn:
        row = conn.execute('SELECT * FROM ifood_connection_check WHERE id=1').fetchone()
    last = json.loads(row['result_json']) if row and row['fingerprint'] == fingerprint(cfg) and row['result_json'] else None
    state = ('disabled' if not cfg['enabled'] else 'missing' if cfg['missing'] else
             'invalid' if not cfg['valid'] else 'mode' if cfg['environment'] != 'test' else 'configured')
    return {'enabled': cfg['enabled'], 'configured': cfg['ready'], 'environment': cfg['environment'],
            'configuration_state': state, 'message': MESSAGES[state],
            'missing': cfg['missing'], 'last_result': last, 'orders_enabled': False}


class ProviderError(Exception):
    def __init__(self, state):
        self.state = state
        super().__init__(state)


def provider_json(request, phase):
    try:
        with evolution.open_url(request, timeout=8) as response:
            raw = response.read(65537)
        if len(raw) > 65536:
            raise ValueError()
        data = json.loads(raw)
        if not isinstance(data, dict):
            raise ValueError()
        return data
    except HTTPError as error:
        state = {400: 'authentication' if phase == 'token' else 'merchant',
                 401: 'authentication', 403: 'permissions', 404: 'merchant' if phase == 'merchant' else 'provider',
                 429: 'quota'}.get(error.code, 'provider')
        error.close()
        raise ProviderError(state) from None
    except (URLError, TimeoutError, OSError):
        raise ProviderError('network') from None
    except (ValueError, UnicodeError, RecursionError):
        raise ProviderError('response') from None


def check(request):
    cfg = config()
    if not cfg['enabled']:
        return result('disabled')
    if cfg['missing']:
        return result('missing', missing=cfg['missing'])
    if not cfg['valid']:
        return result('invalid')
    if cfg['environment'] != 'test':
        return result('mode')
    stamp, digest = time.time(), fingerprint(cfg)
    with db(request) as conn:
        previous = conn.execute('SELECT started_at FROM ifood_connection_check WHERE id=1').fetchone()
        if previous and stamp - previous['started_at'] < 30:
            return result('wait')
        conn.execute('INSERT INTO ifood_connection_check VALUES(1,?,?,\'\') '
                     'ON CONFLICT(id) DO UPDATE SET fingerprint=excluded.fingerprint, '
                     'started_at=excluded.started_at,result_json=\'\'', (digest, stamp))
    try:
        payload = urlencode({'grantType': 'client_credentials', 'clientId': cfg[REQUIRED[0]],
                             'clientSecret': cfg[REQUIRED[1]]}).encode()
        auth = provider_json(URLRequest(HOST + '/authentication/v1.0/oauth/token', data=payload,
            headers={'Content-Type': 'application/x-www-form-urlencoded', 'Accept': 'application/json'}), 'token')
        token = auth.get('accessToken')
        if (not isinstance(token, str) or not re.fullmatch(r'[!-~]{1,4096}', token)
                or str(auth.get('type', 'Bearer')).lower() != 'bearer'):
            raise ProviderError('response')
        merchant = provider_json(URLRequest(HOST + '/merchant/v1.0/merchants/' + cfg[REQUIRED[2]].lower(),
            headers={'Authorization': 'Bearer ' + token, 'Accept': 'application/json'}), 'merchant')
        if not isinstance(merchant.get('id'), str) or merchant['id'].lower() != cfg[REQUIRED[2]].lower():
            raise ProviderError('response')
        answer = result('verified', merchant_id=cfg[REQUIRED[2]].lower(), checked_at=utcnow())
    except ProviderError as error:
        answer = result(error.state, checked_at=utcnow())
    with db(request) as conn:
        conn.execute('UPDATE ifood_connection_check SET result_json=? WHERE id=1 AND fingerprint=? AND started_at=?',
                     (json.dumps(answer, ensure_ascii=False), digest, stamp))
    return answer


@router.get('/api/admin/ifood')
def status(request: Request, admin=Depends(require_admin)):
    return dashboard(request)


@router.post('/api/admin/ifood/check')
def check_connection(request: Request, admin=Depends(require_admin)):
    return check(request)
