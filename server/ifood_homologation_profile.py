"""Preparação e descoberta somente leitura da conta nova, separada do importador.

Este perfil não seleciona credenciais para operações de negócio.
Os nomes das variáveis podem ser cadastrados no servidor; seus valores nunca são
persistidos nem retornados por este diagnóstico.
"""
import hashlib
import json
import os
import secrets
import time
from urllib.error import HTTPError, URLError
from urllib.request import Request as URLRequest

from fastapi import APIRouter, Depends, Request
from pydantic import BaseModel, ConfigDict

from . import ifood
from .core import db, require_admin, utcnow

router = APIRouter()
CLIENT_ID = 'aceb55bd-caea-4f8a-9cd8-62adb5e40370'
REQUIRED = ('SAHARA_IFOOD_HOMOLOGATION_CLIENT_ID',
            'SAHARA_IFOOD_HOMOLOGATION_CLIENT_SECRET',
            'SAHARA_IFOOD_HOMOLOGATION_MERCHANT_ID')
ENVIRONMENT = 'SAHARA_IFOOD_HOMOLOGATION_ENVIRONMENT'
COOLDOWN = 30
LEASE = 180
MAX_PAGES = 3
DISCOVERY_SECONDS = 45
MESSAGES = {
    'unconfigured': 'O perfil separado da conta nova ainda não foi configurado no servidor.',
    'missing': 'Complete as variáveis do perfil de homologação no servidor, sem enviar segredos pelo chat.',
    'invalid': 'Confira as variáveis do perfil: use a aplicação Teste (C) da conta nova e o UUID da loja de teste.',
    'test_proof_required': 'As variáveis têm o formato esperado. O perfil continua bloqueado até confirmar a renovação do segredo e comprovar que o UUID pertence à loja de teste da conta nova.',
    'mode': 'A descoberta da conta nova está disponível apenas no ambiente de teste.',
    'wait': 'Uma descoberta da conta nova está em andamento ou foi feita recentemente. Aguarde antes de consultar novamente.',
    'queried': 'UUIDs das lojas de teste confirmados na consulta. O perfil separado continua bloqueado e nenhuma variável foi alterada.',
    'test_proof_missing': 'A resposta não comprovou exclusivamente lojas de teste. Nenhum UUID foi liberado para configuração.',
    'configuration_changed': 'As variáveis da conta nova mudaram durante a consulta. Faça uma nova descoberta após conferir a configuração.',
}


def initialize(conn):
    conn.execute('''CREATE TABLE IF NOT EXISTS ifood_homologation_discovery (
        id INTEGER PRIMARY KEY CHECK(id=1), fingerprint TEXT NOT NULL,
        started_at REAL NOT NULL, lease_token TEXT NOT NULL,
        result_json TEXT NOT NULL DEFAULT '')''')


class DiscoveryInput(BaseModel):
    model_config = ConfigDict(extra='forbid')


def _capture():
    return ([os.environ.get(name, '').strip() for name in REQUIRED],
            os.environ.get(ENVIRONMENT, 'test').strip())


def _fingerprint(captured):
    return hashlib.sha256(json.dumps(captured).encode()).hexdigest()


def dashboard():
    values, environment = _capture()
    missing = [name for name, value in zip(REQUIRED, values) if not value]
    client_id, secret, merchant_id = values
    valid_fields = (
        ifood.identifier(client_id) and client_id.lower() == CLIENT_ID,
        8 <= len(secret) <= 4096 and not any(ord(char) < 32 or ord(char) == 127 for char in secret),
        ifood.identifier(merchant_id),
    )
    invalid = [name for name, value, valid in zip(REQUIRED, values, valid_fields) if value and not valid]
    discovery_available = all(valid_fields[:2]) and environment == 'test'
    configured = not missing and all(valid_fields) and environment == 'test'
    state = ('unconfigured' if len(missing) == len(REQUIRED) else 'missing' if missing
             else 'invalid' if not all(valid_fields) else 'test_proof_required')
    if environment != 'test':
        state = 'mode'
    return {
        'state': state, 'message': MESSAGES[state],
        'environment': environment if environment in ('test', 'production') else 'unknown',
        'configured': configured, 'missing': missing, 'invalid': invalid,
        'discovery_available': discovery_available,
        'required_variables': list(REQUIRED),
        'expected_client_id': CLIENT_ID,
        'configured_merchant_id': merchant_id.lower() if configured else None,
        'connection_verified': False, 'operations_available': False,
        'credential_renewal_confirmed': False, 'test_merchant_confirmed': False,
        'activation_requirements': ['renewed_secret_confirmation', 'positive_test_merchant_proof',
                                    'independent_connection_verification'],
    }


def _result(state, merchants=None):
    return {'state': state, 'message': MESSAGES.get(state, ifood.MESSAGES.get(state, ifood.MESSAGES['provider'])),
            'merchants': merchants or [], 'checked_at': utcnow(), 'environment': 'test',
            'connection_verified': False, 'operations_available': False}


def discover(request):
    captured = _capture()
    values, environment = captured
    readiness = dashboard()
    if not readiness['discovery_available']:
        return _result(readiness['state'])
    digest, lease_token, stamp = _fingerprint(captured), secrets.token_hex(16), time.time()

    def unchanged():
        if _fingerprint(_capture()) != digest:
            raise ifood.ProviderError('configuration_changed')

    if _fingerprint(_capture()) != digest:
        return _result('configuration_changed')
    with db(request) as conn:
        previous = conn.execute('SELECT started_at,result_json FROM ifood_homologation_discovery WHERE id=1').fetchone()
        interval = COOLDOWN if previous and previous['result_json'] else LEASE
        if previous and stamp - previous['started_at'] < interval:
            return _result('wait')
        conn.execute("INSERT INTO ifood_homologation_discovery VALUES(1,?,?,?,'') ON CONFLICT(id) DO UPDATE SET "
                     "fingerprint=excluded.fingerprint,started_at=excluded.started_at,"
                     "lease_token=excluded.lease_token,result_json=''", (digest, stamp, lease_token))
    deadline = time.monotonic() + DISCOVERY_SECONDS
    try:
        unchanged()
        # Only the canonical OAuth helper is reused; business transport and the
        # legacy connection check/importer are never selected or changed.
        cfg = dict(zip(ifood.REQUIRED, values))
        token = ifood.access_token(cfg)
        unchanged()

        def read(path, expected, limit):
            unchanged()
            if time.monotonic() >= deadline:
                raise ifood.ProviderError('network')
            try:
                with ifood.evolution.open_url(URLRequest(ifood.HOST + path,
                        headers={'Authorization': 'Bearer ' + token, 'Accept': 'application/json'}), timeout=8) as response:
                    status = getattr(response, 'status', 200)
                    raw = response.read(limit + 1)
                unchanged()
                if status != 200 or len(raw) > limit:
                    raise ValueError()
                result = json.loads(raw)
                if not isinstance(result, expected):
                    raise ValueError()
                return result
            except HTTPError as error:
                state = {401: 'authentication', 403: 'permissions', 404: 'merchant',
                         429: 'quota'}.get(error.code, 'provider')
                error.close()
                raise ifood.ProviderError(state) from None
            except (URLError, TimeoutError, OSError):
                raise ifood.ProviderError('network') from None
            except (ValueError, TypeError, UnicodeError, RecursionError):
                raise ifood.ProviderError('response') from None

        identifiers = []
        for page in range(1, MAX_PAGES + 1):
            suffix = '' if page == 1 else '?page=' + str(page) + '&size=100'
            batch = read('/merchant/v1.0/merchants' + suffix, list, 262144)
            if len(batch) > 100 or any(not isinstance(item, dict) or not ifood.identifier(item.get('id')) for item in batch):
                raise ifood.ProviderError('response')
            identifiers.extend(item['id'].lower() for item in batch)
            if len(identifiers) != len(set(identifiers)):
                raise ifood.ProviderError('response')
            if len(batch) < 100:
                break
        else:
            # A full final page is an incomplete list, never a successful discovery.
            raise ifood.ProviderError('response')
        if not identifiers:
            raise ifood.ProviderError('test_proof_missing')
        merchants = []
        for merchant_id in identifiers:
            merchant = read('/merchant/v1.0/merchants/' + merchant_id, dict, 65536)
            if not ifood.identifier(merchant.get('id')) or merchant['id'].lower() != merchant_id:
                raise ifood.ProviderError('response')
            if merchant.get('test') != 'TEST':
                raise ifood.ProviderError('test_proof_missing')
            merchants.append({'merchant_id': merchant_id, 'is_test': True})
        unchanged()
        answer = _result('queried', merchants)
    except ifood.ProviderError as error:
        answer = _result(error.state)
    except (ValueError, TypeError, RecursionError, OverflowError):
        answer = _result('response')
    if _fingerprint(_capture()) != digest:
        answer = _result('configuration_changed')
    # Only bounded state metadata is retained, never bodies, IDs, or credentials.
    saved = {key: answer[key] for key in ('state', 'checked_at')}
    with db(request) as conn:
        conn.execute('UPDATE ifood_homologation_discovery SET result_json=? WHERE id=1 AND fingerprint=? AND lease_token=?',
                     (json.dumps(saved), digest, lease_token))
    return answer


@router.get('/api/admin/ifood/homologation-profile')
def status(admin=Depends(require_admin)):
    return dashboard()


@router.post('/api/admin/ifood/homologation-profile/discover')
def discover_test_merchants(body: DiscoveryInput, request: Request, admin=Depends(require_admin)):
    return discover(request)
