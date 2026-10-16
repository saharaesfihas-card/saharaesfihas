"""Preparação local e bloqueada da conta nova, separada do importador existente.

Este perfil ainda não fornece transporte nem seleciona credenciais para operações.
Os nomes das variáveis podem ser cadastrados no servidor; seus valores nunca são
persistidos nem retornados por este diagnóstico.
"""
import os

from fastapi import APIRouter, Depends

from . import ifood
from .core import require_admin

router = APIRouter()
CLIENT_ID = 'aceb55bd-caea-4f8a-9cd8-62adb5e40370'
REQUIRED = ('SAHARA_IFOOD_HOMOLOGATION_CLIENT_ID',
            'SAHARA_IFOOD_HOMOLOGATION_CLIENT_SECRET',
            'SAHARA_IFOOD_HOMOLOGATION_MERCHANT_ID')
MESSAGES = {
    'unconfigured': 'O perfil separado da conta nova ainda não foi configurado no servidor.',
    'missing': 'Complete as variáveis do perfil de homologação no servidor, sem enviar segredos pelo chat.',
    'invalid': 'Confira as variáveis do perfil: use a aplicação Teste (C) da conta nova e o UUID da loja de teste.',
    'test_proof_required': 'As variáveis têm o formato esperado. O perfil continua bloqueado até confirmar a renovação do segredo e comprovar que o UUID pertence à loja de teste da conta nova.',
}


def dashboard():
    values = [os.environ.get(name, '').strip() for name in REQUIRED]
    missing = [name for name, value in zip(REQUIRED, values) if not value]
    client_id, secret, merchant_id = values
    valid_fields = (
        ifood.identifier(client_id) and client_id.lower() == CLIENT_ID,
        8 <= len(secret) <= 4096 and not any(ord(char) < 32 or ord(char) == 127 for char in secret),
        ifood.identifier(merchant_id),
    )
    invalid = [name for name, value, valid in zip(REQUIRED, values, valid_fields) if value and not valid]
    configured = not missing and all(valid_fields)
    state = ('unconfigured' if len(missing) == len(REQUIRED) else 'missing' if missing
             else 'invalid' if not configured else 'test_proof_required')
    return {
        'state': state, 'message': MESSAGES[state], 'environment': 'test',
        'configured': configured, 'missing': missing, 'invalid': invalid,
        'required_variables': list(REQUIRED),
        'expected_client_id': CLIENT_ID,
        'configured_merchant_id': merchant_id.lower() if configured else None,
        'connection_verified': False, 'operations_available': False,
        'credential_renewal_confirmed': False, 'test_merchant_confirmed': False,
        'activation_requirements': ['renewed_secret_confirmation', 'positive_test_merchant_proof',
                                    'independent_connection_verification'],
    }


@router.get('/api/admin/ifood/homologation-profile')
def status(admin=Depends(require_admin)):
    return dashboard()
