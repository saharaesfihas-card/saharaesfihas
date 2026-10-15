"""Cadastro privado das lojas reais; não ativa acesso ou importa pedidos do iFood."""
import os
import re
import sqlite3
from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException, Request
from pydantic import BaseModel, ConfigDict, Field, StrictStr, field_validator, model_validator

from .core import db, require_admin

router = APIRouter()
DEFAULTS = (
    ('esfihas-44', 'Esfihas 44'),
    ('sahara-esfihas-shawarma', 'Sahara Esfihas e Shawarma'),
    ('maktub-esfihas', 'Maktub Esfihas'),
)
TEST_MERCHANT_ID = '42f9aefa-b035-4e01-be92-d1c9a4adf828'
MESSAGE = ('Lojas cadastradas separadamente. Informar os identificadores não ativa a integração. '
           'O ID numérico do Portal é uma referência e não substitui o Merchant UUID. '
           'O acesso de produção e a autorização de cada loja continuam pendentes; '
           'os cardápios e preços de cada loja precisam ser conferidos separadamente.')


def initialize(conn):
    conn.execute('''CREATE TABLE IF NOT EXISTS ifood_stores (
        id TEXT PRIMARY KEY, name TEXT NOT NULL, merchant_id TEXT UNIQUE,
        portal_id TEXT,
        catalog_state TEXT NOT NULL DEFAULT 'pending' CHECK(catalog_state='pending'))''')
    if 'portal_id' not in {row[1] for row in conn.execute('PRAGMA table_info(ifood_stores)')}:
        conn.execute('ALTER TABLE ifood_stores ADD COLUMN portal_id TEXT')
    conn.execute('CREATE UNIQUE INDEX IF NOT EXISTS ifood_stores_portal_id ON ifood_stores(portal_id)')
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
    merchant_id: StrictStr | None = Field(default=None, max_length=36)
    portal_id: StrictStr | None = Field(default=None, max_length=20)

    @field_validator('merchant_id')
    @classmethod
    def real_store(cls, value):
        return canonical_merchant_id(value)

    @field_validator('portal_id')
    @classmethod
    def numeric_portal_id(cls, value):
        if value is None or not value:
            return None
        if not re.fullmatch(r'[0-9]{1,20}', value):
            raise ValueError('Informe somente os dígitos do ID da loja no Portal do Parceiro.')
        return value

    @model_validator(mode='after')
    def supplied_identifier(self):
        if not self.model_fields_set:
            raise ValueError('Informe o ID do Portal ou o Merchant UUID que deseja atualizar.')
        return self


def dashboard(conn):
    rows = {row['id']: row for row in conn.execute(
        'SELECT id,name,merchant_id,portal_id,catalog_state FROM ifood_stores')}
    return {'stores': [
        {'id': identifier, 'name': rows[identifier]['name'],
         'merchant_id': rows[identifier]['merchant_id'], 'environment': 'production',
         'portal_id': rows[identifier]['portal_id'],
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
        fields = [name for name in ('merchant_id', 'portal_id') if name in body.model_fields_set]
        try:
            conn.execute('UPDATE ifood_stores SET ' + ','.join(name + '=?' for name in fields)
                         + ' WHERE id=?', (*[getattr(body, name) for name in fields], store_id))
        except sqlite3.IntegrityError:
            raise HTTPException(409, 'Esse identificador já está cadastrado em outra loja.') from None
        return dashboard(conn)
