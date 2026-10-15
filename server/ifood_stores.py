"""Cadastro privado das lojas reais; não ativa acesso ou importa pedidos do iFood."""
import os
import sqlite3
from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException, Request
from pydantic import BaseModel, ConfigDict, Field, StrictStr, field_validator

from .core import db, require_admin

router = APIRouter()
DEFAULTS = (
    ('esfihas-44', 'Esfihas 44'),
    ('sahara-esfihas-shawarma', 'Sahara Esfihas e Shawarma'),
    ('maktub-esfihas', 'Maktub Esfihas'),
)
TEST_MERCHANT_ID = '42f9aefa-b035-4e01-be92-d1c9a4adf828'
MESSAGE = ('Lojas cadastradas separadamente. Informar o UUID não ativa a integração. '
           'O acesso de produção e a autorização de cada loja continuam pendentes; '
           'os cardápios e preços de cada loja precisam ser conferidos separadamente.')


def initialize(conn):
    conn.execute('''CREATE TABLE IF NOT EXISTS ifood_stores (
        id TEXT PRIMARY KEY, name TEXT NOT NULL, merchant_id TEXT UNIQUE,
        catalog_state TEXT NOT NULL DEFAULT 'pending' CHECK(catalog_state='pending'))''')
    for identifier, name in DEFAULTS:
        conn.execute('INSERT OR IGNORE INTO ifood_stores(id,name) VALUES(?,?)',
                     (identifier, name))


def canonical_merchant_id(value):
    if value is None or not value:
        return None
    try:
        canonical = str(UUID(value))
    except (ValueError, TypeError, AttributeError):
        raise ValueError('Informe o UUID da loja real no formato completo.') from None
    if value.lower() != canonical:
        raise ValueError('Informe o UUID da loja real no formato completo.')
    test_ids = {TEST_MERCHANT_ID}
    if os.environ.get('SAHARA_IFOOD_ENVIRONMENT', 'test').strip() == 'test':
        test_ids.add(os.environ.get('SAHARA_IFOOD_MERCHANT_ID', '').strip().lower())
    if canonical in test_ids:
        raise ValueError('Esse UUID pertence à loja de teste. Informe o UUID da loja real.')
    return canonical


class StoreInput(BaseModel):
    model_config = ConfigDict(extra='forbid', str_strip_whitespace=True)
    merchant_id: StrictStr | None = Field(max_length=36)

    @field_validator('merchant_id')
    @classmethod
    def real_store(cls, value):
        return canonical_merchant_id(value)


def dashboard(conn):
    rows = {row['id']: row for row in conn.execute(
        'SELECT id,name,merchant_id,catalog_state FROM ifood_stores')}
    return {'stores': [
        {'id': identifier, 'name': rows[identifier]['name'],
         'merchant_id': rows[identifier]['merchant_id'], 'environment': 'production',
         'connection_state': 'production_pending',
         'catalog_state': rows[identifier]['catalog_state'], 'import_available': False}
        for identifier, _ in DEFAULTS
    ], 'message': MESSAGE}


@router.get('/api/admin/ifood/stores')
def stores(request: Request, admin=Depends(require_admin)):
    with db(request) as conn:
        return dashboard(conn)


@router.patch('/api/admin/ifood/stores/{store_id}')
def update_store(store_id: str, body: StoreInput, request: Request,
                 admin=Depends(require_admin)):
    with db(request) as conn:
        if not conn.execute('SELECT 1 FROM ifood_stores WHERE id=?', (store_id,)).fetchone():
            raise HTTPException(404, 'Loja não encontrada.')
        try:
            conn.execute('UPDATE ifood_stores SET merchant_id=? WHERE id=?',
                         (body.merchant_id, store_id))
        except sqlite3.IntegrityError:
            raise HTTPException(409, 'Esse UUID já está cadastrado em outra loja.') from None
        return dashboard(conn)
