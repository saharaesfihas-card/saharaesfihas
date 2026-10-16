"""Catálogo privado de homologação, limitado à loja de teste verificada.

As coleções recebidas não descrevem o upload de fotos nem enums. Valores de
template/status são reaproveitados de entidades efetivamente consultadas. Fotos
existentes podem ser reutilizadas; isso não equivale ao cenário de upload.
"""
import hashlib
import json
import re
import time
from uuid import uuid4

from fastapi import APIRouter, Depends, HTTPException, Request
from pydantic import BaseModel, ConfigDict, Field, StrictInt, field_validator

from . import ifood, ifood_merchant, ifood_test_api
from .core import db, require_admin, utcnow

router = APIRouter()
PREFIX = '/api/admin/ifood/test/catalog'
MAX_ENTITIES = 3000
SEND_LEASE = 180
TOKEN = re.compile(r'^[A-Za-z0-9_-]{1,80}$')
MESSAGES = {
    'queried': 'Catálogo da loja de teste consultado.',
    'confirmed': 'Alteração conferida no catálogo da loja de teste.',
    'pending': 'A operação pode ter sido recebida. Consulte o resultado antes de tentar outra alteração.',
    'sending': 'Uma alteração de catálogo está em andamento. Aguarde e consulte o resultado.',
    'failed': 'A alteração não foi confirmada pelo iFood.',
    'contract_pending': 'O contrato recebido não permite concluir esta operação com segurança.',
}


def initialize(conn):
    # The legacy column name "fingerprint" stores stable app/shop ownership scope,
    # not the credential fingerprint used by Session.unchanged().
    conn.execute('''CREATE TABLE IF NOT EXISTS ifood_test_catalog_registry (
        fingerprint TEXT NOT NULL, catalog_id TEXT NOT NULL, kind TEXT NOT NULL,
        remote_id TEXT NOT NULL, item_id TEXT NOT NULL DEFAULT '', owned INTEGER NOT NULL DEFAULT 0,
        data_json TEXT NOT NULL, checked_at TEXT NOT NULL,
        PRIMARY KEY(fingerprint,catalog_id,kind,remote_id))''')
    conn.execute('''CREATE TABLE IF NOT EXISTS ifood_test_catalog_operations (
        fingerprint TEXT NOT NULL, operation_key TEXT NOT NULL, request_hash TEXT NOT NULL,
        kind TEXT NOT NULL, catalog_id TEXT NOT NULL, resource_id TEXT NOT NULL,
        item_id TEXT NOT NULL DEFAULT '', payload_json TEXT NOT NULL, state TEXT NOT NULL,
        created_at TEXT NOT NULL, checked_at TEXT NOT NULL, lease_until REAL NOT NULL DEFAULT 0,
        PRIMARY KEY(fingerprint,operation_key))''')
    if 'lease_until' not in {row[1] for row in conn.execute('PRAGMA table_info(ifood_test_catalog_operations)')}:
        conn.execute('ALTER TABLE ifood_test_catalog_operations ADD COLUMN lease_until REAL NOT NULL DEFAULT 0')


class Model(BaseModel):
    model_config = ConfigDict(extra='forbid', str_strip_whitespace=True, allow_inf_nan=False)


class Empty(Model):
    pass


class CatalogInput(Model):
    catalog_id: str

    @field_validator('catalog_id')
    @classmethod
    def valid_id(cls, value):
        if not ifood.identifier(value):
            raise ValueError('Informe um identificador de catálogo válido.')
        return value.lower()


class CategoryInput(CatalogInput):
    operation_key: str = Field(min_length=16, max_length=100, pattern=r'^[A-Za-z0-9_-]+$')
    template_category_id: str


class ItemInput(CatalogInput):
    operation_key: str = Field(min_length=16, max_length=100, pattern=r'^[A-Za-z0-9_-]+$')
    category_id: str
    template_item_id: str
    price_cents: StrictInt = Field(ge=1, le=100000)


class PriceInput(CatalogInput):
    operation_key: str = Field(min_length=16, max_length=100, pattern=r'^[A-Za-z0-9_-]+$')
    entity_id: str
    price_cents: StrictInt = Field(ge=0, le=100000)


class GroupInput(CatalogInput):
    operation_key: str = Field(min_length=16, max_length=100, pattern=r'^[A-Za-z0-9_-]+$')
    item_id: str
    template_item_id: str
    template_group_id: str
    option1_price_cents: StrictInt = Field(ge=0, le=100000)
    option2_price_cents: StrictInt = Field(ge=0, le=100000)


class StatusInput(CatalogInput):
    operation_key: str = Field(min_length=16, max_length=100, pattern=r'^[A-Za-z0-9_-]+$')
    entity_id: str
    status: str = Field(min_length=1, max_length=80, pattern=r'^[A-Za-z0-9_-]+$')


class ReconcileInput(Model):
    operation_key: str = Field(min_length=16, max_length=100, pattern=r'^[A-Za-z0-9_-]+$')


def answer(state, **extra):
    return {'state': state, 'message': MESSAGES.get(state, ifood.MESSAGES.get(state, ifood.MESSAGES['provider'])),
            'checked_at': utcnow(), **extra}


def capability():
    return {'image_upload': False, 'image_replacement': False, 'bulk_patch': False,
            'single_entity_patch': True, 'group_creation': True,
            'image_contract_message': 'A coleção descreve image/upload apenas como <object>. Falta o formato do arquivo e da resposta.',
            'bulk_contract_message': 'As quatro rotas PATCH documentadas recebem um objeto por entidade, sem contrato de lote.',
            'creation_message': 'Criação depende de um modelo válido consultado na loja de teste. Fotos de modelo são apenas reutilizadas.'}


def _path(session, suffix):
    return '/catalog/v2.0/merchants/' + session.merchant_id + '/' + suffix


def _object_id(value):
    if not isinstance(value, str) or not ifood.identifier(value):
        raise ifood.ProviderError('response', 'catalog')
    return value.lower()


def _text(value, maximum=200):
    return isinstance(value, str) and 0 < len(value) <= maximum and not any(ord(c) < 32 for c in value)


def _lookup(request, session, catalog_id, kind, remote_id, owned=False):
    if not ifood.identifier(remote_id):
        raise HTTPException(422, 'Identificador de entidade inválido.')
    with db(request) as conn:
        row = conn.execute('SELECT * FROM ifood_test_catalog_registry WHERE fingerprint=? AND catalog_id=? '
            'AND kind=? AND remote_id=?', (session.scope, catalog_id, kind, remote_id.lower())).fetchone()
    if not row or (owned and not row['owned']):
        raise HTTPException(409, 'Consulte o catálogo e use uma entidade criada nesta área de homologação.')
    return dict(row), json.loads(row['data_json'])


def _save(request, session, catalog_id, kind, remote_id, value, item_id='', owned=False):
    session.unchanged()
    with db(request) as conn:
        conn.execute('''INSERT INTO ifood_test_catalog_registry VALUES(?,?,?,?,?,?,?,?)
            ON CONFLICT(fingerprint,catalog_id,kind,remote_id) DO UPDATE SET
            item_id=CASE WHEN owned=1 AND excluded.owned=0 THEN item_id ELSE excluded.item_id END,
            data_json=CASE WHEN owned=1 AND excluded.owned=0 THEN data_json ELSE excluded.data_json END,
            checked_at=CASE WHEN owned=1 AND excluded.owned=0 THEN checked_at ELSE excluded.checked_at END,
            owned=MAX(owned,excluded.owned)''',
            (session.scope, catalog_id, kind, remote_id, item_id, int(owned),
             json.dumps(session.sanitized(value), ensure_ascii=False, allow_nan=False), utcnow()))


def _categories(data):
    if isinstance(data, dict):
        data = [data]
    if not isinstance(data, list) or len(data) > MAX_ENTITIES:
        raise ifood.ProviderError('response', 'catalog')
    seen = set()
    for category in data:
        if not isinstance(category, dict):
            raise ifood.ProviderError('response', 'catalog')
        category_id = _object_id(category.get('id'))
        if category_id in seen or not _text(category.get('name')):
            raise ifood.ProviderError('response', 'catalog')
        seen.add(category_id)
        items = category.get('items', [])
        if not isinstance(items, list) or len(items) > MAX_ENTITIES:
            raise ifood.ProviderError('response', 'catalog')
        for item in items:
            if not isinstance(item, dict):
                raise ifood.ProviderError('response', 'catalog')
            _object_id(item.get('id'))
    return data


def _register_categories(request, session, catalog_id, categories):
    count, seen = 0, set()
    resources = []
    for category in categories:
        category_id = _object_id(category['id'])
        resources.append(('category', category_id, category, ''))
        for item in category.get('items', []):
            item_id = _object_id(item.get('id'))
            if item_id in seen:
                raise ifood.ProviderError('response', 'catalog')
            seen.add(item_id)
            resources.append(('item', item_id, {**item, 'categoryId': category_id}, item_id))
            groups = item.get('optionGroups', [])
            if not isinstance(groups, list) or len(groups) > 100:
                raise ifood.ProviderError('response', 'catalog')
            for group in groups:
                if not isinstance(group, dict):
                    raise ifood.ProviderError('response', 'catalog')
                options = group.get('options', [])
                if isinstance(options, dict):
                    options = [options]
                if not isinstance(options, list) or len(options) > 200:
                    raise ifood.ProviderError('response', 'catalog')
                for option in options:
                    if not isinstance(option, dict):
                        raise ifood.ProviderError('response', 'catalog')
                    resources.append(('option', _object_id(option.get('id')), option, item_id))
            count += 1
            if count > MAX_ENTITIES:
                raise ifood.ProviderError('response', 'catalog')
    session.unchanged()
    for kind, remote_id, value, item_id in resources:
        _save(request, session, catalog_id, kind, remote_id, value, item_id)


def _flat(session, item_id, category_id=None):
    _, flat = session.call('GET', _path(session, 'items/' + item_id + '/flat'), expected=dict)
    item = flat.get('item')
    if not isinstance(item, dict) or _object_id(item.get('id')) != item_id:
        raise ifood.ProviderError('response', 'catalog')
    if category_id and _object_id(item.get('categoryId')) != category_id:
        raise ifood.ProviderError('response', 'catalog')
    for key in ('products', 'optionGroups', 'options'):
        if not isinstance(flat.get(key), list) or len(flat[key]) > 200:
            raise ifood.ProviderError('response', 'catalog')
        ids = [_object_id(value.get('id')) for value in flat[key] if isinstance(value, dict)]
        if len(ids) != len(flat[key]) or len(set(ids)) != len(ids):
            raise ifood.ProviderError('response', 'catalog')
    if _object_id(item.get('productId')) not in {product['id'].lower() for product in flat['products']}:
        raise ifood.ProviderError('response', 'catalog')
    return flat


def _generation(request, session):
    """Fence payloads prepared from mutable reads against concurrent writes."""
    with db(request) as conn:
        return conn.execute('SELECT COUNT(*) FROM ifood_test_catalog_operations WHERE fingerprint=?',
                            (session.scope,)).fetchone()[0]


def _intent(request, session, body, kind, resource_id, payload, item_id='', *, generation):
    request_hash = hashlib.sha256(json.dumps(body.model_dump(), sort_keys=True).encode()).hexdigest()
    stamp = utcnow()
    with db(request) as conn:
        row = conn.execute('SELECT * FROM ifood_test_catalog_operations WHERE fingerprint=? AND operation_key=?',
                           (session.scope, body.operation_key)).fetchone()
        if row:
            if row['request_hash'] != request_hash:
                raise HTTPException(409, 'Esta chave já identifica uma operação diferente.')
            return dict(row), False
        current_generation = conn.execute('SELECT COUNT(*) FROM ifood_test_catalog_operations WHERE fingerprint=?',
                                          (session.scope,)).fetchone()[0]
        if current_generation != generation:
            raise HTTPException(409, 'O catálogo recebeu outra alteração durante a preparação. Consulte o estado atual antes de tentar novamente.')
        # Uma escrita incerta bloqueia outras, evitando mudanças fora de ordem.
        pending = conn.execute("SELECT 1 FROM ifood_test_catalog_operations WHERE fingerprint=? AND state IN ('sending','pending')",
                               (session.scope,)).fetchone()
        if pending:
            raise HTTPException(409, 'Consulte a operação pendente antes de enviar outra alteração de catálogo.')
        conn.execute('INSERT INTO ifood_test_catalog_operations VALUES(?,?,?,?,?,?,?,?,?,?,?,?)',
            (session.scope, body.operation_key, request_hash, kind, body.catalog_id, resource_id, item_id,
             json.dumps(payload, ensure_ascii=False, allow_nan=False), 'sending', stamp, stamp, time.time() + SEND_LEASE))
        row = conn.execute('SELECT * FROM ifood_test_catalog_operations WHERE fingerprint=? AND operation_key=?',
                           (session.scope, body.operation_key)).fetchone()
    return dict(row), True


def _existing(request, session, body):
    """Repetições consultam a intenção já gravada, sem repetir HTTP nem criar IDs."""
    with db(request) as conn:
        row = conn.execute('SELECT * FROM ifood_test_catalog_operations WHERE fingerprint=? AND operation_key=?',
                           (session.scope, body.operation_key)).fetchone()
    if row is None:
        return None
    digest = hashlib.sha256(json.dumps(body.model_dump(), sort_keys=True).encode()).hexdigest()
    if row['request_hash'] != digest:
        raise HTTPException(409, 'Esta chave já identifica uma operação diferente.')
    return {**answer(row['state'], operation_key=row['operation_key'], kind=row['kind'],
                    resource_id=row['resource_id'], catalog_id=row['catalog_id'], environment='test', **ifood_test_api.metadata(session.cfg)),
            'checked_at': row['checked_at'], 'replayed': True}


def _finish(request, session, operation, state):
    with db(request) as conn:
        conn.execute("UPDATE ifood_test_catalog_operations SET state=?,checked_at=? WHERE fingerprint=? AND operation_key=? AND state<>'confirmed'",
                     (state, utcnow(), session.scope, operation['operation_key']))
        current = conn.execute('SELECT state,checked_at FROM ifood_test_catalog_operations WHERE fingerprint=? AND operation_key=?',
                               (session.scope, operation['operation_key'])).fetchone()
    return {**answer(current['state'], operation_key=operation['operation_key'], kind=operation['kind'],
                     resource_id=operation['resource_id'], catalog_id=operation['catalog_id'], environment='test', **ifood_test_api.metadata(session.cfg)),
            'checked_at': current['checked_at']}


def _compare(actual, expected):
    if isinstance(expected, dict):
        return isinstance(actual, dict) and all(key in actual and _compare(actual[key], value) for key, value in expected.items())
    if isinstance(expected, list):
        return isinstance(actual, list) and len(actual) == len(expected) and all(_compare(a, b) for a, b in zip(actual, expected))
    return type(actual) is type(expected) and actual == expected or (
        isinstance(actual, (int, float)) and not isinstance(actual, bool)
        and isinstance(expected, (int, float)) and not isinstance(expected, bool) and actual == expected)


def _readback(request, session, operation):
    payload = json.loads(operation['payload_json'])
    kind, catalog_id, resource_id = operation['kind'], operation['catalog_id'], operation['resource_id']
    if kind == 'category':
        _, category = session.call('GET', _path(session, 'catalogs/' + catalog_id + '/categories/' + resource_id), expected=dict)
        if _compare(category, payload):
            _save(request, session, catalog_id, 'category', resource_id, category, owned=True)
            return True
        return False
    flat = _flat(session, operation['item_id'])
    if kind in ('item', 'group'):
        expected = payload
        # Comparar os campos enviados; o provedor pode acrescentar metadados.
        matched = _compare(flat, expected)
        if matched:
            item_id = flat['item']['id'].lower()
            _save(request, session, catalog_id, 'item', item_id, flat['item'], item_id, owned=True)
            for product in flat['products']:
                _save(request, session, catalog_id, 'product', product['id'].lower(), product, item_id, owned=True)
            for option in flat['options']:
                _save(request, session, catalog_id, 'option', option['id'].lower(), option, item_id, owned=True)
            for group in flat['optionGroups']:
                _save(request, session, catalog_id, 'group', group['id'].lower(), group, item_id, owned=True)
        return matched
    entity_kind, field = kind.split('_', 1)
    entity = flat['item'] if entity_kind == 'item' else next((value for value in flat['options'] if value['id'].lower() == resource_id), None)
    if not isinstance(entity, dict):
        return False
    expected = {field: payload[field]}
    matched = _compare(entity, expected)
    if matched:
        # A PATCH proves only its field. Preserve the original owned references
        # even if another client reassigned a product in the provider response.
        row, owned_entity = _lookup(request, session, catalog_id, entity_kind, resource_id, owned=True)
        owned_entity[field] = entity[field]
        _save(request, session, catalog_id, entity_kind, resource_id, owned_entity, row['item_id'], owned=True)
    return matched


def _write(request, session, operation, method, path, statuses):
    accepted = False
    try:
        session.unchanged()
        session.call(method, path, json.loads(operation['payload_json']), statuses=statuses, expected=None)
        accepted = True
        confirmed = _readback(request, session, operation)
        session.unchanged()
        return _finish(request, session, operation, 'confirmed' if confirmed else 'pending')
    except ifood.ProviderError as error:
        # A partir do envio, erro de leitura também deixa a escrita incerta.
        state = 'pending' if accepted or getattr(error, 'uncertain', False) else 'failed'
        return _finish(request, session, operation, state)


def _guarded(action):
    try:
        return action()
    except ifood.ProviderError as error:
        return answer(error.state)


@router.get(PREFIX)
def dashboard(request: Request, admin=Depends(require_admin)):
    cfg = ifood_test_api.configuration()
    credential_digest = ifood.fingerprint(cfg)
    digest = ifood_test_api.scope(cfg)
    with db(request) as conn:
        rows = conn.execute('SELECT operation_key,kind,catalog_id,resource_id,state,checked_at FROM '
            'ifood_test_catalog_operations WHERE fingerprint=? ORDER BY created_at DESC LIMIT 20', (digest,)).fetchall()
        resources = conn.execute("SELECT catalog_id,kind,remote_id,item_id,data_json FROM ifood_test_catalog_registry "
                                 "WHERE fingerprint=? AND owned=1", (digest,)).fetchall()
    state = ifood_merchant.readiness(request, cfg)
    if ifood.fingerprint(ifood_test_api.current_config(cfg)) != credential_digest:
        state = 'configuration_changed'
    if state != 'available':
        rows, resources = [], []
    return {'environment': 'test', 'state': state, 'available': state == 'available',
            **ifood_test_api.metadata(cfg),
            'capabilities': capability(), 'operations': [dict(row) for row in rows],
            'resources': [{**{key: row[key] for key in ('catalog_id', 'kind', 'remote_id', 'item_id')},
                           'data': json.loads(row['data_json'])} for row in resources]}


@router.post(PREFIX + '/discover')
def discover(body: Empty, request: Request, admin=Depends(require_admin)):
    def action():
        session = ifood_test_api.Session(request)
        _, catalogs = session.call('GET', _path(session, 'catalogs'), expected=list)
        if len(catalogs) > 100:
            raise ifood.ProviderError('response', 'catalog')
        seen = set()
        for catalog in catalogs:
            if not isinstance(catalog, dict):
                raise ifood.ProviderError('response', 'catalog')
            catalog_id = _object_id(catalog.get('catalogId'))
            if catalog_id in seen:
                raise ifood.ProviderError('response', 'catalog')
            seen.add(catalog_id)
        for catalog in catalogs:
            catalog_id = catalog['catalogId'].lower()
            _save(request, session, catalog_id, 'catalog', catalog_id, catalog)
        return answer('queried', catalogs=catalogs, merchant_id=session.merchant_id,
                      environment='test', capabilities=capability(), **ifood_test_api.metadata(session.cfg))
    return _guarded(action)


@router.post(PREFIX + '/inspect')
def inspect(body: CatalogInput, request: Request, admin=Depends(require_admin)):
    def action():
        session = ifood_test_api.Session(request)
        _lookup(request, session, body.catalog_id, 'catalog', body.catalog_id)
        _, data = session.call('GET', _path(session, 'catalogs/' + body.catalog_id + '/categories?includeItems=true'), expected=(dict, list))
        categories = _categories(data)
        _register_categories(request, session, body.catalog_id, categories)
        return answer('queried', catalog_id=body.catalog_id, categories=categories, environment='test', capabilities=capability(), **ifood_test_api.metadata(session.cfg))
    return _guarded(action)


@router.post(PREFIX + '/categories')
def create_category(body: CategoryInput, request: Request, admin=Depends(require_admin)):
    def action():
        session = ifood_test_api.Session(request)
        existing = _existing(request, session, body)
        if existing:
            return existing
        generation = _generation(request, session)
        _, template = _lookup(request, session, body.catalog_id, 'category', body.template_category_id)
        if not all(isinstance(template.get(key), str) and TOKEN.fullmatch(template[key]) for key in ('status', 'template')):
            return answer('contract_pending', missing=['category.status', 'category.template'])
        payload = {'id': str(uuid4()), 'name': 'Teste Homologação', 'status': template['status'],
                   'template': template['template'], 'externalCode': 'sahara-homologacao-' + uuid4().hex, 'index': 0}
        operation, fresh = _intent(request, session, body, 'category', payload['id'], payload, generation=generation)
        if not fresh:
            return _finish(request, session, operation, operation['state'])
        return _write(request, session, operation, 'POST', _path(session, 'catalogs/' + body.catalog_id + '/categories'), (201,))
    return _guarded(action)


@router.post(PREFIX + '/items')
def create_item(body: ItemInput, request: Request, admin=Depends(require_admin)):
    def action():
        session = ifood_test_api.Session(request)
        existing = _existing(request, session, body)
        if existing:
            return existing
        generation = _generation(request, session)
        _lookup(request, session, body.catalog_id, 'category', body.category_id, owned=True)
        _, template_row = _lookup(request, session, body.catalog_id, 'item', body.template_item_id)
        template = _flat(session, body.template_item_id.lower(), template_row.get('categoryId'))
        source_item = template['item']
        product = next(value for value in template['products'] if value['id'].lower() == source_item['productId'].lower())
        if (template['optionGroups'] or template['options']
                or not all(isinstance(source_item.get(key), str) and TOKEN.fullmatch(source_item[key]) for key in ('type', 'status'))
                or not isinstance(product.get('serving'), str) or not TOKEN.fullmatch(product['serving'])
                or not _text(product.get('imagePath'), 2048)):
            return answer('contract_pending', missing=['simple_item_template_with_existing_photo_and_enums'])
        item_id, product_id = str(uuid4()), str(uuid4())
        payload = {'item': {'id': item_id, 'type': source_item['type'], 'status': source_item['status'],
            'productId': product_id, 'categoryId': body.category_id, 'price': {'value': body.price_cents / 100},
            'externalCode': 'sahara-homologacao-' + uuid4().hex, 'index': 0},
            'products': [{'id': product_id, 'name': 'Produto Teste', 'serving': product['serving'],
                'description': 'Produto exclusivo de homologação. Não entregar.', 'imagePath': product['imagePath'],
                'externalCode': 'sahara-homologacao-' + uuid4().hex, 'optionGroups': []}],
            'optionGroups': [], 'options': []}
        operation, fresh = _intent(request, session, body, 'item', item_id, payload, item_id, generation=generation)
        if not fresh:
            return _finish(request, session, operation, operation['state'])
        result = _write(request, session, operation, 'PUT', _path(session, 'items'), (200,))
        result['photo_source'] = 'existing_test_template'
        result['image_upload_completed'] = False
        return result
    return _guarded(action)


@router.post(PREFIX + '/groups')
def create_group(body: GroupInput, request: Request, admin=Depends(require_admin)):
    def action():
        session = ifood_test_api.Session(request)
        existing = _existing(request, session, body)
        if existing:
            return existing
        generation = _generation(request, session)
        _, owned_item = _lookup(request, session, body.catalog_id, 'item', body.item_id, owned=True)
        _, source_item = _lookup(request, session, body.catalog_id, 'item', body.template_item_id)
        current = _flat(session, body.item_id.lower(), owned_item.get('categoryId'))
        source = _flat(session, body.template_item_id.lower(), source_item.get('categoryId'))
        if not ifood.identifier(body.template_group_id):
            raise HTTPException(422, 'Grupo de modelo inválido.')
        group = next((value for value in source['optionGroups'] if value['id'].lower() == body.template_group_id.lower()), None)
        if group is None:
            raise HTTPException(409, 'O grupo não pertence ao item de modelo consultado.')
        source_product = next(value for value in source['products'] if value['id'].lower() == source['item']['productId'].lower())
        links = source_product.get('optionGroups', [])
        if not isinstance(links, list) or not any(isinstance(link, dict) and
                str(link.get('id', '')).lower() == body.template_group_id.lower() for link in links):
            return answer('contract_pending', missing=['template_group_association'])
        ids = group.get('optionIds')
        missing = ['simple_two_option_template_with_existing_photos_and_enums']
        if (current['optionGroups'] or current['options'] or len(current['products']) != 1
                or current['item'].get('productId', '').lower() != owned_item.get('productId', '').lower()
                or source['item'].get('type') != current['item'].get('type')
                or not isinstance(ids, list) or len(ids) != 2 or any(not ifood.identifier(value) for value in ids)
                or len(set(value.lower() for value in ids)) != 2
                or not all(isinstance(group.get(key), str) and TOKEN.fullmatch(group[key])
                           for key in ('status', 'optionGroupType'))):
            return answer('contract_pending', missing=missing)
        options = {value['id'].lower(): value for value in source['options']}
        products = {value['id'].lower(): value for value in source['products']}
        pairs = []
        for option_id in ids:
            option = options.get(option_id.lower())
            if not option or not ifood.identifier(option.get('productId')):
                return answer('contract_pending', missing=missing)
            product = products.get(option['productId'].lower())
            if (not product or product.get('optionGroups') or option.get('contextModifiers') or option.get('fractions')
                    or not isinstance(option.get('status'), str) or not TOKEN.fullmatch(option['status'])
                    or not isinstance(product.get('serving'), str) or not TOKEN.fullmatch(product['serving'])
                    or not _text(product.get('imagePath'), 2048)):
                return answer('contract_pending', missing=missing)
            pairs.append((option, product))
        # O único produto já existente que será atualizado também foi criado aqui.
        current_product = current['products'][0]
        product_row, _ = _lookup(request, session, body.catalog_id, 'product', current_product['id'], owned=True)
        if (product_row['item_id'] != body.item_id.lower()
                or current_product['id'].lower() != current['item']['productId'].lower()
                or current_product.get('optionGroups')):
            return answer('contract_pending', missing=['owned_simple_product_without_existing_groups'])
        allowed_item = {'type', 'status', 'externalCode', 'productId', 'id', 'categoryId', 'price', 'index', 'shifts', 'tags', 'contextModifiers'}
        allowed_product = {'name', 'serving', 'id', 'externalCode', 'description', 'additionalInformation',
                           'imagePath', 'ean', 'dietaryRestrictions', 'tags', 'quantity', 'optionGroups'}
        payload = {'item': {key: value for key, value in current['item'].items() if key in allowed_item},
                   'products': [{key: value for key, value in current_product.items() if key in allowed_product}],
                   'optionGroups': [], 'options': []}
        group_id = str(uuid4())
        payload['products'][0]['optionGroups'] = [{'id': group_id, 'min': 0, 'max': 2}]
        group_payload = {'id': group_id, 'name': 'Complementos Teste', 'status': group['status'],
                         'optionGroupType': group['optionGroupType'], 'externalCode': 'sahara-homologacao-' + uuid4().hex,
                         'index': 0, 'optionIds': []}
        payload['optionGroups'].append(group_payload)
        for index, ((option, product), cents) in enumerate(zip(pairs, (body.option1_price_cents, body.option2_price_cents))):
            option_id, product_id = str(uuid4()), str(uuid4())
            group_payload['optionIds'].append(option_id)
            product_payload = {key: value for key, value in product.items() if key in allowed_product}
            product_payload.update({'id': product_id, 'name': 'Complemento Teste ' + str(index + 1),
                                    'externalCode': 'sahara-homologacao-' + uuid4().hex, 'optionGroups': []})
            payload['products'].append(product_payload)
            payload['options'].append({'id': option_id, 'productId': product_id, 'status': option['status'],
                                       'price': {'value': cents / 100}, 'index': index,
                                       'externalCode': 'sahara-homologacao-' + uuid4().hex})
        operation, fresh = _intent(request, session, body, 'group', group_id, payload, body.item_id.lower(), generation=generation)
        if not fresh:
            return _finish(request, session, operation, operation['state'])
        result = _write(request, session, operation, 'PUT', _path(session, 'items'), (200,))
        result['photo_source'] = 'existing_test_template'
        result['image_upload_completed'] = False
        return result
    return _guarded(action)


def _change(body, request, kind, field):
    def action():
        session = ifood_test_api.Session(request)
        existing = _existing(request, session, body)
        if existing:
            return existing
        generation = _generation(request, session)
        row, entity = _lookup(request, session, body.catalog_id, kind, body.entity_id, owned=True)
        flat = _flat(session, row['item_id'])
        values = [flat['item']] + flat['options']
        if field == 'status':
            # Não inferir os códigos AVAILABLE/UNAVAILABLE a partir de placeholders.
            with db(request) as conn:
                rows = conn.execute('SELECT data_json FROM ifood_test_catalog_registry WHERE fingerprint=? AND catalog_id=? '
                    'AND kind=?', (session.scope, body.catalog_id, kind)).fetchall()
            observed = {json.loads(value['data_json']).get('status') for value in rows}
            if body.status not in observed:
                return answer('contract_pending', missing=['requested_status_not_observed_in_test_catalog'])
            payload = {kind + 'Id': body.entity_id.lower(), 'status': body.status}
        else:
            payload = {kind + 'Id': body.entity_id.lower(), 'price': {'value': body.price_cents / 100}}
        actual = flat['item'] if kind == 'item' else next((value for value in values if value.get('id', '').lower() == body.entity_id.lower()), None)
        if not actual or actual['id'].lower() != body.entity_id.lower():
            raise ifood.ProviderError('response', 'catalog')
        operation, fresh = _intent(request, session, body, kind + '_' + field, body.entity_id.lower(), payload, row['item_id'], generation=generation)
        if not fresh:
            return _finish(request, session, operation, operation['state'])
        return _write(request, session, operation, 'PATCH', _path(session, kind + 's/' + field), (200, 204))
    return _guarded(action)


@router.patch(PREFIX + '/items/price')
def item_price(body: PriceInput, request: Request, admin=Depends(require_admin)):
    return _change(body, request, 'item', 'price')


@router.patch(PREFIX + '/items/status')
def item_status(body: StatusInput, request: Request, admin=Depends(require_admin)):
    return _change(body, request, 'item', 'status')


@router.patch(PREFIX + '/options/price')
def option_price(body: PriceInput, request: Request, admin=Depends(require_admin)):
    return _change(body, request, 'option', 'price')


@router.patch(PREFIX + '/options/status')
def option_status(body: StatusInput, request: Request, admin=Depends(require_admin)):
    return _change(body, request, 'option', 'status')


@router.post(PREFIX + '/reconcile')
def reconcile(body: ReconcileInput, request: Request, admin=Depends(require_admin)):
    def action():
        session = ifood_test_api.Session(request)
        with db(request) as conn:
            row = conn.execute('SELECT * FROM ifood_test_catalog_operations WHERE fingerprint=? AND operation_key=?',
                               (session.scope, body.operation_key)).fetchone()
        if not row:
            raise HTTPException(404, 'Operação de catálogo não encontrada nesta configuração.')
        operation = dict(row)
        if operation['state'] in ('sending', 'pending') and operation['lease_until'] > time.time():
            return {**answer(operation['state'], operation_key=body.operation_key, kind=operation['kind'],
                             resource_id=operation['resource_id'], catalog_id=operation['catalog_id'], environment='test', **ifood_test_api.metadata(session.cfg)),
                    'checked_at': operation['checked_at'], 'retry_after_seconds': max(1, int(operation['lease_until'] - time.time()) + 1)}
        if operation['state'] == 'confirmed':
            return _finish(request, session, operation, 'confirmed')
        try:
            matched = _readback(request, session, operation)
            session.unchanged()
        except ifood.ProviderError:
            return _finish(request, session, operation, 'pending')
        return _finish(request, session, operation, 'confirmed' if matched else 'pending')
    return _guarded(action)
