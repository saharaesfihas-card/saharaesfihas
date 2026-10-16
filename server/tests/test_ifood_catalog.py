"""Contratos Catalog reais recebidos, com transporte HTTP simulado e DB isolado."""
from concurrent.futures import ThreadPoolExecutor
from copy import deepcopy
from io import BytesIO
import json
import os
from pathlib import Path
import sqlite3
from tempfile import TemporaryDirectory
from threading import Event
import unittest
from unittest.mock import patch
from urllib.error import HTTPError, URLError

from fastapi.testclient import TestClient

from server import ifood, ifood_catalog, ifood_stores, ifood_test_api
from server.core import hash_password
from server.main import create_app

CLIENT = '11111111-1111-4111-8111-111111111111'
MERCHANT = ifood_stores.TEST_MERCHANT_ID
OTHER = '22222222-2222-4222-8222-222222222222'
CATALOG = '33333333-3333-4333-8333-333333333333'
CATEGORY = '44444444-4444-4444-8444-444444444444'
ITEM = '55555555-5555-4555-8555-555555555555'
PRODUCT = '66666666-6666-4666-8666-666666666666'
OPTION = '77777777-7777-4777-8777-777777777777'
GROUP_TEMPLATE = '88888888-8888-4888-8888-888888888888'
GROUP = '99999999-9999-4999-8999-999999999999'
OPTION_PRODUCT1 = 'aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa'
OPTION_PRODUCT2 = 'bbbbbbbb-bbbb-4bbb-8bbb-bbbbbbbbbbbb'
SECRET = 'fictional-catalog-secret'
TOKEN = 'fictional-catalog-token'
PATH = ifood_catalog.PREFIX
BASE = '/catalog/v2.0/merchants/' + MERCHANT + '/'


class Response(BytesIO):
    def __init__(self, data=None, status=200):
        super().__init__(b'' if data is None else json.dumps(data).encode())
        self.status = status


class CatalogTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.password = 'disposable-catalog-password'
        cls.password_hash = hash_password(cls.password)

    def setUp(self):
        self.directory = TemporaryDirectory()
        self.env = patch.dict(os.environ, {
            'SAHARA_IFOOD_CLIENT_ID': CLIENT, 'SAHARA_IFOOD_CLIENT_SECRET': SECRET,
            'SAHARA_IFOOD_MERCHANT_ID': MERCHANT, 'SAHARA_IFOOD_ENABLED': '1',
            'SAHARA_IFOOD_ENVIRONMENT': 'test', 'SAHARA_IFOOD_IMPORT_ENABLED': '0',
            'SAHARA_WHATSAPP_ENABLED': '0', 'SAHARA_AI_ENABLED': '0'})
        self.env.start()
        self.app = create_app(data_dir=Path(self.directory.name),
                              admin_password_hash=self.password_hash, secure_cookie=False)
        # Root registers the module in main; isolated tests also work before that edit.
        if not any(getattr(route, 'path', None) == PATH for route in self.app.routes):
            self.app.include_router(ifood_catalog.router)
        with sqlite3.connect(self.app.state.db_path) as conn:
            ifood_catalog.initialize(conn)
        self.client = TestClient(self.app)
        login = self.client.post('/api/admin/login', json={'password': self.password})
        self.headers = {'X-Sahara-CSRF': login.json()['csrf_token']}
        self.verify()
        self.catalogs = [{'catalogId': CATALOG, 'status': 'OBSERVED_CATALOG', 'context': ['OBSERVED_CONTEXT']}]
        self.categories = [{'id': CATEGORY, 'name': 'Modelo existente de teste', 'template': 'OBSERVED_TEMPLATE',
                            'status': 'OBSERVED_ACTIVE', 'items': [{'id': ITEM, 'name': 'Modelo de teste',
                            'productId': PRODUCT, 'status': 'OBSERVED_ACTIVE', 'price': {'value': 5}, 'optionGroups': []}]}]
        self.flats = {ITEM: {'item': {'id': ITEM, 'type': 'OBSERVED_SIMPLE', 'status': 'OBSERVED_ACTIVE',
                                    'productId': PRODUCT, 'categoryId': CATEGORY, 'price': {'value': 5}},
                            'products': [{'id': PRODUCT, 'name': 'Modelo', 'serving': 'OBSERVED_SERVING',
                                          'imagePath': MERCHANT + '/existing.jpg'}], 'options': [], 'optionGroups': []}}
        self.remote_categories = {CATEGORY: self.categories[0]}
        self.calls = []
        with sqlite3.connect(self.app.state.db_path) as conn:
            self.public_products = conn.execute('SELECT * FROM products ORDER BY id').fetchall()

    def tearDown(self):
        self.client.close()
        self.env.stop()
        self.directory.cleanup()

    def verify(self):
        with sqlite3.connect(self.app.state.db_path) as conn:
            conn.execute('INSERT OR REPLACE INTO ifood_connection_check VALUES(1,?,?,?)',
                         (ifood.fingerprint(ifood.config()), 10, json.dumps({'state': 'verified', 'merchant_id': MERCHANT})))

    def post(self, suffix, body):
        return self.client.post(PATH + suffix, json=body, headers=self.headers)

    def transport(self, request, **kwargs):
        path = request.full_url.removeprefix(ifood.HOST)
        method = request.get_method()
        self.calls.append((method, path, json.loads(request.data) if request.data and path != '/authentication/v1.0/oauth/token' else None))
        if path == '/authentication/v1.0/oauth/token':
            return Response({'accessToken': TOKEN, 'type': 'Bearer'})
        self.assertTrue(path.startswith(BASE), path)
        self.assertEqual(request.get_header('Authorization'), 'Bearer ' + TOKEN)
        suffix = path.removeprefix(BASE)
        if method == 'GET':
            if suffix == 'catalogs':
                return Response(self.catalogs)
            if suffix == 'catalogs/' + CATALOG + '/categories?includeItems=true':
                return Response(self.categories)
            if suffix.startswith('catalogs/' + CATALOG + '/categories/'):
                value = self.remote_categories.get(suffix.rsplit('/', 1)[1])
                if value is None:
                    raise HTTPError(request.full_url, 404, 'absent', {}, None)
                return Response(value)
            if suffix.startswith('items/') and suffix.endswith('/flat'):
                return Response(self.flats[suffix.split('/')[1]])
        payload = json.loads(request.data)
        if method == 'POST' and suffix == 'catalogs/' + CATALOG + '/categories':
            self.remote_categories[payload['id']] = deepcopy(payload)
            return Response(payload, 201)
        if method == 'PUT' and suffix == 'items':
            self.flats[payload['item']['id']] = deepcopy(payload)
            return Response(payload)
        if method == 'PATCH':
            entity_kind, field = suffix.split('/')
            identifier = payload['itemId' if entity_kind == 'items' else 'optionId']
            for flat in self.flats.values():
                entities = [flat['item']] if entity_kind == 'items' else flat['options']
                for entity in entities:
                    if entity['id'] == identifier:
                        entity[field] = deepcopy(payload[field])
            return Response(status=204)
        self.fail((method, path))

    def discover(self):
        self.assertEqual(self.post('/discover', {}).json()['state'], 'queried')
        self.assertEqual(self.post('/inspect', {'catalog_id': CATALOG}).json()['state'], 'queried')

    def seed_resource(self, kind, identifier, value, owned=True):
        with sqlite3.connect(self.app.state.db_path) as conn:
            conn.execute('INSERT OR REPLACE INTO ifood_test_catalog_registry VALUES(?,?,?,?,?,?,?,?)',
                (ifood_test_api.scope(ifood.config()), CATALOG, kind, identifier, ITEM, int(owned), json.dumps(value), 'now'))

    def create_category(self, key='category-operation-001'):
        result = self.post('/categories', {'catalog_id': CATALOG, 'template_category_id': CATEGORY, 'operation_key': key}).json()
        self.assertEqual(result['state'], 'confirmed', result)
        return result['resource_id']

    def add_group_template(self):
        flat = deepcopy(self.flats[ITEM])
        flat['item']['id'] = GROUP_TEMPLATE
        flat['products'][0]['optionGroups'] = [{'id': GROUP, 'min': 0, 'max': 2}]
        flat['products'].extend([
            {'id': OPTION_PRODUCT1, 'name': 'Modelo 1', 'serving': 'OBSERVED_SERVING',
             'imagePath': MERCHANT + '/option1.jpg', 'optionGroups': []},
            {'id': OPTION_PRODUCT2, 'name': 'Modelo 2', 'serving': 'OBSERVED_SERVING',
             'imagePath': MERCHANT + '/option2.jpg', 'optionGroups': []}])
        flat['optionGroups'] = [{'id': GROUP, 'name': 'Modelo', 'status': 'OBSERVED_ACTIVE',
                                 'optionGroupType': 'OBSERVED_GROUP_TYPE', 'optionIds': [OPTION, OTHER]}]
        flat['options'] = [
            {'id': OPTION, 'productId': OPTION_PRODUCT1, 'status': 'OBSERVED_ACTIVE', 'price': {'value': 1}},
            {'id': OTHER, 'productId': OPTION_PRODUCT2, 'status': 'OBSERVED_ACTIVE', 'price': {'value': 2}}]
        self.flats[GROUP_TEMPLATE] = flat
        self.categories[0]['items'].append({**flat['item'], 'name': 'Modelo com dois complementos',
            'optionGroups': [{**flat['optionGroups'][0], 'options': deepcopy(flat['options'])}]})

    def create_item(self, category_id):
        result = self.post('/items', {'catalog_id': CATALOG, 'category_id': category_id,
            'template_item_id': ITEM, 'price_cents': 550, 'operation_key': 'create-item-operation-001'}).json()
        self.assertEqual(result['state'], 'confirmed', result)
        return result['resource_id']

    def test_login_csrf_origin_and_strict_bodies(self):
        with patch('server.ifood.evolution.open_url') as call:
            anonymous = TestClient(self.app)
            try:
                self.assertEqual(anonymous.get(PATH).status_code, 401)
                self.assertEqual(anonymous.post(PATH + '/discover', json={}).status_code, 401)
            finally:
                anonymous.close()
            self.assertEqual(self.client.post(PATH + '/discover', json={}).status_code, 403)
            self.assertEqual(self.client.post(PATH + '/discover', json={}, headers={**self.headers,
                             'Origin': 'https://untrusted.invalid'}).status_code, 403)
            self.assertEqual(self.post('/discover', {'merchant_id': OTHER}).status_code, 422)
            self.assertEqual(self.post('/inspect', {'catalog_id': '../escape'}).status_code, 422)
            self.assertEqual(self.post('/inspect', {'catalog_id': CATALOG, 'base_url': 'https://untrusted.invalid'}).status_code, 422)
        call.assert_not_called()

    def test_local_dashboard_has_no_http_and_honest_blockers(self):
        with patch('server.ifood.evolution.open_url') as call:
            result = self.client.get(PATH).json()
        call.assert_not_called()
        self.assertTrue(result['available'])
        self.assertFalse(result['capabilities']['image_upload'])
        self.assertFalse(result['capabilities']['bulk_patch'])
        self.assertEqual(result['operations'], [])

    def test_discovery_and_inspection_exact_paths_registry_and_single_object_category(self):
        with patch('server.ifood.evolution.open_url', side_effect=self.transport):
            result = self.post('/discover', {}).json()
            self.assertEqual(result['catalogs'], self.catalogs)
            self.categories = self.categories[0]
            result = self.post('/inspect', {'catalog_id': CATALOG}).json()
        self.assertEqual(result['categories'], [self.categories])
        self.assertEqual([row[:2] for row in self.calls], [
            ('POST', '/authentication/v1.0/oauth/token'), ('GET', BASE + 'catalogs'),
            ('POST', '/authentication/v1.0/oauth/token'), ('GET', BASE + 'catalogs/' + CATALOG + '/categories?includeItems=true')])
        self.assertNotIn(SECRET, json.dumps(result))
        self.assertNotIn(TOKEN, json.dumps(result))

    def test_unregistered_catalog_or_entity_has_zero_business_http(self):
        with patch('server.ifood.evolution.open_url') as call:
            self.assertEqual(self.post('/inspect', {'catalog_id': CATALOG}).status_code, 409)
            self.assertEqual(self.post('/categories', {'catalog_id': CATALOG, 'template_category_id': CATEGORY,
                                                     'operation_key': 'unknown-category-001'}).status_code, 409)
        call.assert_not_called()

    def test_wrong_target_production_unverified_and_rotated_configuration_never_call(self):
        with patch('server.ifood.evolution.open_url') as call:
            for change in ({'SAHARA_IFOOD_ENVIRONMENT': 'production'}, {'SAHARA_IFOOD_MERCHANT_ID': OTHER},
                           {'SAHARA_IFOOD_ENABLED': '0'}, {'SAHARA_IFOOD_CLIENT_SECRET': SECRET + '-changed'}):
                with self.subTest(change=change), patch.dict(os.environ, change):
                    self.assertNotEqual(self.post('/discover', {}).json()['state'], 'queried')
            with sqlite3.connect(self.app.state.db_path) as conn:
                conn.execute('DELETE FROM ifood_connection_check')
            self.assertNotEqual(self.post('/discover', {}).json()['state'], 'queried')
        call.assert_not_called()

    def test_category_derives_enums_generates_ids_and_duplicate_key_never_repeats_write(self):
        with patch('server.ifood.evolution.open_url', side_effect=self.transport):
            self.discover()
            category_id = self.create_category()
            again = self.create_category()
        self.assertEqual(category_id, again)
        writes = [value for value in self.calls if value[0] == 'POST' and value[1].startswith(BASE)]
        self.assertEqual(len(writes), 1)
        payload = writes[0][2]
        self.assertEqual(payload['status'], 'OBSERVED_ACTIVE')
        self.assertEqual(payload['template'], 'OBSERVED_TEMPLATE')
        self.assertEqual(payload['name'], 'Teste Homologação')
        self.assertNotEqual(payload['id'], CATEGORY)
        self.assertTrue(ifood.identifier(payload['id']))
        resources = self.client.get(PATH).json()['resources']
        self.assertEqual(resources[0]['remote_id'], category_id)

    def test_missing_category_enum_blocks_without_business_write(self):
        self.categories[0].pop('template')
        with patch('server.ifood.evolution.open_url', side_effect=self.transport):
            self.discover()
            result = self.post('/categories', {'catalog_id': CATALOG, 'template_category_id': CATEGORY,
                                              'operation_key': 'missing-template-001'}).json()
        self.assertEqual(result['state'], 'contract_pending')
        self.assertFalse(any(method != 'GET' and path.startswith(BASE) for method, path, _ in self.calls))

    def test_item_creation_new_owned_ids_reuses_existing_photo_and_never_mutates_template(self):
        original = deepcopy(self.flats[ITEM])
        with patch('server.ifood.evolution.open_url', side_effect=self.transport):
            self.discover()
            category_id = self.create_category()
            result = self.post('/items', {'catalog_id': CATALOG, 'category_id': category_id,
                'template_item_id': ITEM, 'price_cents': 550, 'operation_key': 'create-item-operation-001'}).json()
        self.assertEqual(result['state'], 'confirmed', result)
        self.assertFalse(result['image_upload_completed'])
        self.assertEqual(result['photo_source'], 'existing_test_template')
        payload = next(value[2] for value in self.calls if value[0] == 'PUT')
        self.assertEqual(payload['item']['categoryId'], category_id)
        self.assertNotEqual(payload['item']['id'], ITEM)
        self.assertNotEqual(payload['products'][0]['id'], PRODUCT)
        self.assertEqual(payload['products'][0]['imagePath'], original['products'][0]['imagePath'])
        self.assertEqual(payload['item']['price'], {'value': 5.5})
        self.assertEqual(self.flats[ITEM], original)

    def test_read_template_does_not_authorize_patch_of_existing_item(self):
        with patch('server.ifood.evolution.open_url', side_effect=self.transport):
            self.discover()
            self.calls.clear()
            result = self.client.patch(PATH + '/items/price', json={'catalog_id': CATALOG, 'entity_id': ITEM,
                'operation_key': 'not-owned-resource-001', 'price_cents': 100}, headers=self.headers)
        self.assertEqual(result.status_code, 409)
        self.assertEqual(self.calls, [])

    def test_group_clones_two_options_all_new_ids_and_relationships_without_touching_source(self):
        self.add_group_template()
        source = deepcopy(self.flats[GROUP_TEMPLATE])
        with patch('server.ifood.evolution.open_url', side_effect=self.transport):
            self.discover()
            category_id = self.create_category()
            item_id = self.create_item(category_id)
            original_item = deepcopy(self.flats[item_id])
            body = {'catalog_id': CATALOG, 'item_id': item_id, 'template_item_id': GROUP_TEMPLATE,
                    'template_group_id': GROUP, 'option1_price_cents': 125, 'option2_price_cents': 250,
                    'operation_key': 'create-two-options-operation'}
            result = self.post('/groups', body).json()
            self.assertEqual(result['state'], 'confirmed', result)
            self.assertFalse(result['image_upload_completed'])
            result2 = self.post('/groups', body).json()
            self.assertEqual(result2['resource_id'], result['resource_id'])
            # A newly created option can now use the exact option PATCH contract.
            option_id = self.flats[item_id]['options'][0]['id']
            result = self.client.patch(PATH + '/options/price', json={'catalog_id': CATALOG, 'entity_id': option_id,
                'operation_key': 'new-owned-option-price-001', 'price_cents': 299}, headers=self.headers).json()
            self.assertEqual(result['state'], 'confirmed', result)
        payloads = [row[2] for row in self.calls if row[0] == 'PUT']
        self.assertEqual(len(payloads), 2)
        payload = payloads[1]
        self.assertEqual(payload['item'], original_item['item'])
        self.assertEqual(payload['products'][0]['id'], original_item['products'][0]['id'])
        new_group = payload['optionGroups'][0]
        new_ids = {new_group['id']} | {option['id'] for option in payload['options']} | {product['id'] for product in payload['products'][1:]}
        old_ids = {GROUP, GROUP_TEMPLATE, OPTION, OTHER, OPTION_PRODUCT1, OPTION_PRODUCT2, PRODUCT, ITEM}
        self.assertFalse(new_ids & old_ids)
        self.assertEqual(len(new_ids), 5)
        self.assertEqual(payload['products'][0]['optionGroups'], [{'id': new_group['id'], 'min': 0, 'max': 2}])
        self.assertEqual(new_group['optionIds'], [option['id'] for option in payload['options']])
        self.assertEqual([option['productId'] for option in payload['options']], [product['id'] for product in payload['products'][1:]])
        self.assertEqual([product['imagePath'] for product in payload['products'][1:]], [MERCHANT + '/option1.jpg', MERCHANT + '/option2.jpg'])
        self.assertEqual(self.flats[GROUP_TEMPLATE], source)
        resources = self.client.get(PATH).json()['resources']
        self.assertEqual(len([row for row in resources if row['kind'] == 'option']), 2)

    def test_ambiguous_nested_group_template_or_foreign_product_is_blocked_before_put(self):
        self.add_group_template()
        with patch('server.ifood.evolution.open_url', side_effect=self.transport):
            self.discover()
            category_id = self.create_category()
            item_id = self.create_item(category_id)
            body = {'catalog_id': CATALOG, 'item_id': item_id, 'template_item_id': GROUP_TEMPLATE,
                    'template_group_id': GROUP, 'option1_price_cents': 125, 'option2_price_cents': 250,
                    'operation_key': 'blocked-two-options-operation'}
            self.calls.clear()
            for field, value in (('contextModifiers', [{'parentOptionId': OTHER}]), ('fractions', ['1/2'])):
                self.flats[GROUP_TEMPLATE]['options'][0][field] = value
                result = self.post('/groups', body).json()
                self.assertEqual(result['state'], 'contract_pending', result)
                del self.flats[GROUP_TEMPLATE]['options'][0][field]
            self.flats[item_id]['item']['productId'] = PRODUCT
            self.flats[item_id]['products'] = [deepcopy(self.flats[ITEM]['products'][0])]
            result = self.post('/groups', body).json()
            self.assertEqual(result['state'], 'contract_pending', result)
        self.assertFalse(any(row[0] == 'PUT' for row in self.calls))

    def test_four_single_patch_payloads_and_readback_do_not_send_arrays(self):
        self.seed_resource('item', ITEM, self.flats[ITEM]['item'])
        self.flats[ITEM]['options'] = [{'id': OPTION, 'productId': OTHER, 'status': 'OBSERVED_ACTIVE', 'price': {'value': 1}}]
        self.seed_resource('option', OPTION, self.flats[ITEM]['options'][0])
        # An observed paused code can come from another queried test entity.
        self.seed_resource('item', OTHER, {'id': OTHER, 'status': 'OBSERVED_PAUSED'}, owned=False)
        self.seed_resource('option', PRODUCT, {'id': PRODUCT, 'status': 'OBSERVED_PAUSED'}, owned=False)
        with patch('server.ifood.evolution.open_url', side_effect=self.transport):
            for kind, identifier in (('items', ITEM), ('options', OPTION)):
                for field, value in (('price', 725), ('status', 'OBSERVED_PAUSED')):
                    body = {'catalog_id': CATALOG, 'entity_id': identifier, 'operation_key': f'{kind}-{field}-operation-001',
                            'price_cents' if field == 'price' else 'status': value}
                    result = self.client.patch(PATH + '/' + kind + '/' + field, json=body, headers=self.headers).json()
                    self.assertEqual(result['state'], 'confirmed', result)
        writes = [row for row in self.calls if row[0] == 'PATCH']
        self.assertEqual([row[1] for row in writes], [BASE + suffix for suffix in (
            'items/price', 'items/status', 'options/price', 'options/status')])
        self.assertEqual(writes[0][2], {'itemId': ITEM, 'price': {'value': 7.25}})
        self.assertEqual(writes[1][2], {'itemId': ITEM, 'status': 'OBSERVED_PAUSED'})
        self.assertEqual(writes[2][2], {'optionId': OPTION, 'price': {'value': 7.25}})
        self.assertEqual(writes[3][2], {'optionId': OPTION, 'status': 'OBSERVED_PAUSED'})

    def test_unknown_status_and_noninteger_price_block_before_write(self):
        self.seed_resource('item', ITEM, self.flats[ITEM]['item'])
        with patch('server.ifood.evolution.open_url', side_effect=self.transport):
            body = {'catalog_id': CATALOG, 'entity_id': ITEM, 'operation_key': 'unknown-status-operation', 'status': 'INVENTED'}
            result = self.client.patch(PATH + '/items/status', json=body, headers=self.headers).json()
            self.assertEqual(result['state'], 'contract_pending')
            for value in (True, 1.5, -1, 100001, '299'):
                body.pop('status', None)
                body['price_cents'] = value
                self.assertEqual(self.client.patch(PATH + '/items/price', json=body, headers=self.headers).status_code, 422)
        self.assertFalse(any(row[0] == 'PATCH' for row in self.calls))

    def test_timeout_after_remote_create_is_pending_and_reconcile_only_reads(self):
        lost = True
        def transport(request, **kwargs):
            nonlocal lost
            result = self.transport(request, **kwargs)
            if lost and request.get_method() == 'POST' and request.full_url.startswith(ifood.HOST + BASE):
                lost = False
                raise URLError('response lost ' + SECRET)
            return result
        body = {'catalog_id': CATALOG, 'template_category_id': CATEGORY, 'operation_key': 'uncertain-create-operation'}
        with patch('server.ifood.evolution.open_url', side_effect=transport):
            self.discover()
            result = self.post('/categories', body).json()
            self.assertEqual(result['state'], 'pending')
            again = self.post('/categories', body).json()
            self.assertEqual(again['resource_id'], result['resource_id'])
            self.assertEqual(again['state'], 'pending')
            another = self.post('/categories', {**body, 'operation_key': 'another-create-operation'})
            self.assertEqual(another.status_code, 409)
            # A lost response retains the send lease; a read cannot release it early.
            self.assertEqual(self.post('/reconcile', {'operation_key': body['operation_key']}).json()['state'], 'pending')
            with sqlite3.connect(self.app.state.db_path) as conn:
                conn.execute('UPDATE ifood_test_catalog_operations SET lease_until=0')
            result = self.post('/reconcile', {'operation_key': body['operation_key']}).json()
        self.assertEqual(result['state'], 'confirmed')
        self.assertEqual(sum(method == 'POST' and path.startswith(BASE) for method, path, _ in self.calls), 1)
        self.assertNotIn(SECRET, json.dumps(result))

    def test_accepted_write_with_failed_readback_remains_pending(self):
        def transport(request, **kwargs):
            if request.get_method() == 'GET' and '/categories/' in request.full_url:
                raise HTTPError(request.full_url, 403, 'read denied', {}, None)
            return self.transport(request, **kwargs)
        with patch('server.ifood.evolution.open_url', side_effect=transport):
            self.discover()
            result = self.post('/categories', {'catalog_id': CATALOG, 'template_category_id': CATEGORY,
                                              'operation_key': 'accepted-readback-denied'}).json()
        self.assertEqual(result['state'], 'pending')

    def test_secret_rotation_keeps_uncertain_intent_and_owned_resource_namespace(self):
        def lost_response(request, **kwargs):
            value = self.transport(request, **kwargs)
            if request.get_method() == 'POST' and request.full_url.endswith('/categories'):
                raise TimeoutError()
            return value
        body = {'catalog_id': CATALOG, 'template_category_id': CATEGORY,
                'operation_key': 'rotation-preserves-test-category'}
        with patch('server.ifood.evolution.open_url', side_effect=lost_response):
            self.discover()
            original = self.post('/categories', body).json()
        self.assertEqual(original['state'], 'pending')
        os.environ['SAHARA_IFOOD_CLIENT_SECRET'] = SECRET + '-renewed'
        self.verify()
        with patch('server.ifood.evolution.open_url') as provider:
            same = self.post('/categories', body).json()
            blocked = self.post('/categories', {**body, 'operation_key': 'rotation-must-not-duplicate'})
        provider.assert_not_called()
        self.assertEqual(same['state'], 'pending')
        self.assertEqual(blocked.status_code, 409)
        with sqlite3.connect(self.app.state.db_path) as conn:
            conn.execute('UPDATE ifood_test_catalog_operations SET lease_until=0')
        with patch('server.ifood.evolution.open_url', side_effect=self.transport):
            final = self.post('/reconcile', {'operation_key': body['operation_key']}).json()
        self.assertEqual(final['state'], 'confirmed')
        self.assertEqual(sum(method == 'POST' and path.endswith('/categories') for method, path, payload in self.calls), 1)
        owned = self.client.get(PATH).json()['resources']
        self.assertTrue(any(value['remote_id'] == final['resource_id'] for value in owned))

    def test_concurrent_same_key_has_one_remote_create(self):
        entered, release = Event(), Event()
        def transport(request, **kwargs):
            if request.get_method() == 'POST' and request.full_url.startswith(ifood.HOST + BASE):
                entered.set()
                self.assertTrue(release.wait(5))
            return self.transport(request, **kwargs)
        body = {'catalog_id': CATALOG, 'template_category_id': CATEGORY, 'operation_key': 'concurrent-category-create'}
        with patch('server.ifood.evolution.open_url', side_effect=transport):
            self.discover()
            with ThreadPoolExecutor(max_workers=2) as pool:
                first = pool.submit(self.post, '/categories', body)
                self.assertTrue(entered.wait(5))
                second = pool.submit(self.post, '/categories', body)
                result = second.result(timeout=5).json()
                self.assertEqual(result['state'], 'sending')
                release.set()
                self.assertEqual(first.result(timeout=5).json()['state'], 'confirmed')
        self.assertEqual(sum(method == 'POST' and path.startswith(BASE) for method, path, _ in self.calls), 1)

    def test_stale_group_snapshot_cannot_replace_a_concurrently_created_group(self):
        self.add_group_template()
        entered, release = Event(), Event()
        original_intent = ifood_catalog._intent

        def delayed_intent(request, session, body, kind, resource_id, payload, item_id='', **kwargs):
            if body.operation_key == 'slow-group-snapshot-operation':
                entered.set()
                self.assertTrue(release.wait(5))
            return original_intent(request, session, body, kind, resource_id, payload, item_id, **kwargs)

        with patch('server.ifood.evolution.open_url', side_effect=self.transport):
            self.discover()
            category_id = self.create_category()
            item_id = self.create_item(category_id)
            body = {'catalog_id': CATALOG, 'item_id': item_id, 'template_item_id': GROUP_TEMPLATE,
                    'template_group_id': GROUP, 'option1_price_cents': 125, 'option2_price_cents': 250,
                    'operation_key': 'slow-group-snapshot-operation'}
            with patch('server.ifood_catalog._intent', side_effect=delayed_intent):
                with ThreadPoolExecutor(max_workers=2) as pool:
                    slow = pool.submit(self.post, '/groups', body)
                    self.assertTrue(entered.wait(5))
                    first = self.post('/groups', {**body, 'operation_key': 'fast-group-snapshot-operation'}).json()
                    self.assertEqual(first['state'], 'confirmed', first)
                    saved = deepcopy(self.flats[item_id])
                    release.set()
                    rejected = slow.result(timeout=5)
            self.assertEqual(rejected.status_code, 409)
            self.assertEqual(self.flats[item_id], saved)
        # One item creation and one group update; no stale second group PUT.
        self.assertEqual(sum(method == 'PUT' for method, path, payload in self.calls), 2)

    def test_reconcile_cannot_unlock_active_write_even_when_preexisting_value_matches(self):
        self.seed_resource('item', ITEM, self.flats[ITEM]['item'])
        entered, release = Event(), Event()
        def transport(request, **kwargs):
            if request.get_method() == 'PATCH':
                entered.set()
                self.assertTrue(release.wait(5))
            return self.transport(request, **kwargs)
        body = {'catalog_id': CATALOG, 'entity_id': ITEM, 'operation_key': 'active-same-price-operation', 'price_cents': 500}
        with patch('server.ifood.evolution.open_url', side_effect=transport):
            with ThreadPoolExecutor(max_workers=2) as pool:
                first = pool.submit(self.client.patch, PATH + '/items/price', json=body, headers=self.headers)
                self.assertTrue(entered.wait(5))
                before = len(self.calls)
                result = self.post('/reconcile', {'operation_key': body['operation_key']}).json()
                self.assertEqual(result['state'], 'sending')
                self.assertGreater(result['retry_after_seconds'], 0)
                self.assertEqual(len(self.calls), before)
                second = self.client.patch(PATH + '/items/price', json={**body,
                    'operation_key': 'second-different-price-operation', 'price_cents': 700}, headers=self.headers)
                self.assertEqual(second.status_code, 409)
                release.set()
                self.assertEqual(first.result(timeout=5).json()['state'], 'confirmed')
        self.assertEqual(sum(row[0] == 'PATCH' for row in self.calls), 1)

    def test_late_stale_reconciliation_cannot_revert_confirmed_operation(self):
        lost, read_count = True, 0
        entered, release = Event(), Event()
        def transport(request, **kwargs):
            nonlocal lost, read_count
            if request.get_method() == 'GET' and '/categories/' in request.full_url:
                read_count += 1
                if read_count == 1:
                    captured = deepcopy(self.remote_categories[request.full_url.rsplit('/', 1)[1]])
                    captured['name'] = 'Resposta antiga que não corresponde'
                    entered.set()
                    self.assertTrue(release.wait(5))
                    return Response(captured)
            response = self.transport(request, **kwargs)
            if lost and request.get_method() == 'POST' and request.full_url.startswith(ifood.HOST + BASE):
                lost = False
                raise URLError('lost')
            return response
        body = {'catalog_id': CATALOG, 'template_category_id': CATEGORY, 'operation_key': 'concurrent-readback-operation'}
        with patch('server.ifood.evolution.open_url', side_effect=transport):
            self.discover()
            self.assertEqual(self.post('/categories', body).json()['state'], 'pending')
            with sqlite3.connect(self.app.state.db_path) as conn:
                conn.execute('UPDATE ifood_test_catalog_operations SET lease_until=0')
            with ThreadPoolExecutor(max_workers=2) as pool:
                first = pool.submit(self.post, '/reconcile', {'operation_key': body['operation_key']})
                self.assertTrue(entered.wait(5))
                second = self.post('/reconcile', {'operation_key': body['operation_key']})
                self.assertEqual(second.json()['state'], 'confirmed')
                release.set()
                self.assertEqual(first.result(timeout=5).json()['state'], 'confirmed')
        operation = self.client.get(PATH).json()['operations'][0]
        self.assertEqual(operation['state'], 'confirmed')

    def test_refresh_cannot_adopt_a_foreign_product_under_an_owned_item(self):
        self.add_group_template()
        with patch('server.ifood.evolution.open_url', side_effect=self.transport):
            self.discover()
            category_id = self.create_category()
            item_id = self.create_item(category_id)
            original_product_id = self.flats[item_id]['item']['productId']
            self.flats[item_id]['item']['productId'] = PRODUCT
            self.flats[item_id]['products'] = [deepcopy(self.flats[ITEM]['products'][0])]
            self.categories.append({**self.remote_categories[category_id], 'items': [deepcopy(self.flats[item_id]['item'])]})
            self.assertEqual(self.post('/inspect', {'catalog_id': CATALOG}).json()['state'], 'queried')
            resources = self.client.get(PATH).json()['resources']
            own_item = next(row for row in resources if row['kind'] == 'item' and row['remote_id'] == item_id)
            self.assertEqual(own_item['data']['productId'], original_product_id)
            self.calls.clear()
            result = self.post('/groups', {'catalog_id': CATALOG, 'item_id': item_id, 'template_item_id': GROUP_TEMPLATE,
                'template_group_id': GROUP, 'option1_price_cents': 100, 'option2_price_cents': 200,
                'operation_key': 'reject-reassigned-owned-product'}).json()
        self.assertEqual(result['state'], 'contract_pending')
        self.assertFalse(any(row[0] == 'PUT' for row in self.calls))

    def test_patch_readback_cannot_adopt_another_owned_items_product(self):
        self.add_group_template()
        with patch('server.ifood.evolution.open_url', side_effect=self.transport):
            self.discover()
            category_id = self.create_category()
            item_id = self.create_item(category_id)
            original_product_id = self.flats[item_id]['item']['productId']
            other = self.post('/items', {'catalog_id': CATALOG, 'category_id': category_id,
                'template_item_id': ITEM, 'price_cents': 550,
                'operation_key': 'second-owned-item-operation'}).json()
            self.assertEqual(other['state'], 'confirmed', other)
            other_item = self.flats[other['resource_id']]
            # A different provider client reassigns this item's product.
            self.flats[item_id]['item']['productId'] = other_item['item']['productId']
            self.flats[item_id]['products'] = deepcopy(other_item['products'])
            price = self.client.patch(PATH + '/items/price', json={'catalog_id': CATALOG,
                'entity_id': item_id, 'operation_key': 'changed-product-price-operation',
                'price_cents': 600}, headers=self.headers).json()
            self.assertEqual(price['state'], 'confirmed', price)
            own_item = next(row for row in self.client.get(PATH).json()['resources']
                            if row['kind'] == 'item' and row['remote_id'] == item_id)
            self.assertEqual(own_item['data']['productId'], original_product_id)
            self.assertEqual(own_item['data']['price'], {'value': 6})
            self.calls.clear()
            result = self.post('/groups', {'catalog_id': CATALOG, 'item_id': item_id,
                'template_item_id': GROUP_TEMPLATE, 'template_group_id': GROUP,
                'option1_price_cents': 100, 'option2_price_cents': 200,
                'operation_key': 'reject-reassigned-owned-product'}).json()
        self.assertEqual(result['state'], 'contract_pending', result)
        self.assertFalse(any(method == 'PUT' for method, path, payload in self.calls))

    def test_conflicting_idempotency_key_and_configuration_bound_registry(self):
        with patch('server.ifood.evolution.open_url', side_effect=self.transport):
            self.discover()
            self.create_category()
            result = self.post('/categories', {'catalog_id': CATALOG, 'template_category_id': OTHER,
                                              'operation_key': 'category-operation-001'})
            self.assertEqual(result.status_code, 409)
        with patch.dict(os.environ, {'SAHARA_IFOOD_CLIENT_ID': OTHER}):
            self.verify()
            with patch('server.ifood.evolution.open_url') as call:
                self.assertEqual(self.post('/inspect', {'catalog_id': CATALOG}).status_code, 409)
            call.assert_not_called()
            self.assertEqual(self.client.get(PATH).json()['resources'], [])

    def test_public_products_and_real_store_registry_are_untouched(self):
        with patch('server.ifood.evolution.open_url', side_effect=self.transport):
            self.discover()
            self.create_category()
        with sqlite3.connect(self.app.state.db_path) as conn:
            self.assertEqual(conn.execute('SELECT * FROM products ORDER BY id').fetchall(), self.public_products)
            self.assertEqual(conn.execute('SELECT COUNT(*) FROM ifood_test_catalog_registry WHERE owned=1').fetchone()[0], 1)
            self.assertEqual(conn.execute('SELECT COUNT(*) FROM ifood_stores WHERE merchant_id IS NOT NULL').fetchone()[0], 0)


if __name__ == '__main__':
    unittest.main()
