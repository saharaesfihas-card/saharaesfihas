'use strict';

// Real disposable backend and Chromium. Only the new iFood workflow endpoints are
// fixtures. No provider requests, credentials, store writes or homologation evidence.
const assert = require('node:assert/strict');
const { spawn } = require('node:child_process');
const { once } = require('node:events');
const { randomBytes } = require('node:crypto');
const { mkdtemp, rm, mkdir } = require('node:fs/promises');
const net = require('node:net');
const os = require('node:os');
const path = require('node:path');
const { chromium } = require('playwright');
const root = path.resolve(__dirname, '..');
const password = randomBytes(24).toString('hex');
const MID = '11111111-1111-4111-8111-111111111111';
const CLIENT = '22222222-2222-4222-8222-222222222222';
const CATALOG = '33333333-3333-4333-8333-333333333333';
const CATEGORY = '44444444-4444-4444-8444-444444444444';
const ITEM = '55555555-5555-4555-8555-555555555555';
const OPTION = '66666666-6666-4666-8666-666666666666';
const NEW_CATEGORY = '77777777-7777-4777-8777-777777777777';
const NEW_ITEM = '88888888-8888-4888-8888-888888888888';
const GROUP = 'bbbbbbbb-bbbb-4bbb-8bbb-bbbbbbbbbbbb';
const OPERATION = '99999999-9999-4999-8999-999999999999';
const marker = '<img src=x onerror="window.__ifoodXSS=1">' + 'x'.repeat(120);
const stamp = '2026-10-15T23:30:00Z';
const metadata = { environment: 'test', profile_mode: 'legacy', client_id: CLIENT, configured_merchant_id: MID, checked_at: stamp };
const NEW_CLIENT = 'aceb55bd-caea-4f8a-9cd8-62adb5e40370';
const NEW_MID = 'dddddddd-dddd-4ddd-8ddd-dddddddddddd';
const newMetadata = { ...metadata, profile_mode: 'homologation', client_id: NEW_CLIENT, configured_merchant_id: NEW_MID };
const merchantPath = '/api/admin/ifood/merchant';
const catalogPath = '/api/admin/ifood/test/catalog';
const orderPath = '/api/admin/ifood/orders/local-ifood-test';
const profilePath = '/api/admin/ifood/homologation-profile';
const fixtureOrder = { id: 'local-ifood-test', source: 'ifood', status: 'ifood_received', created_at: stamp,
  customer_name: 'Cliente fictício de teste', customer_phone: '', total_cents: 500, discount_cents: 0,
  payment_status: 'external_paid', payment_method: 'iFood · CREDIT',
  items: [{ name: 'Produto de teste', quantity: 1, price_cents: 500, total_cents: 500 }],
  delivery: { street: 'Rua fictícia', number: '123', neighborhood: 'Bairro fictício',
    ifood: { display_id: 'UI-TEST', environment: 'test', order_id: ITEM, prepaid_cents: 500, pending_cents: 0, delivery_fee_cents: 0 } } };
let backend, browser, context, page, directory, baseURL;
const requests = [], errors = [], external = [];
let merchantAvailable = true, catalogAvailable = true, createState = 'confirmed', catalogPatchState = 'confirmed';
let merchantOperations = [], catalogOperations = [], orderAttempts = [];
let orderFailure = false, orderLostState = null, merchantFailure = null, catalogFailure = null, held, heldPath, captured;
let profileHTTP = 200, profileMessage = 'O perfil separado da conta nova ainda não foi configurado no servidor.';
let profileDiscoveryAvailable = false, profileDiscoverState = 'queried';
let profileDiscoverMessage = 'UUID da loja de teste consultado. O perfil permanece bloqueado.';
let profileCheckAvailable = false, profileVerified = false, profileCheckState = 'verified', profileCheckHTTP = 200;
let profileSelected = false;
const activeMetadata = () => profileSelected ? newMetadata : metadata;
let resources = [
  { catalog_id: CATALOG, kind: 'category', remote_id: CATEGORY, data: { name: 'Categoria criada no teste' } },
  { catalog_id: CATALOG, kind: 'item', remote_id: ITEM, item_id: ITEM, data: { name: 'Item criado no teste' } },
  { catalog_id: CATALOG, kind: 'option', remote_id: OPTION, item_id: ITEM, data: { name: 'Opção criada no teste' } },
];
let checks = 0;
const merchant = () => page.getByRole('region', { name: 'Operações Merchant da loja de teste', exact: true });
const catalog = () => page.getByRole('region', { name: 'Catálogo da loja de teste', exact: true });
const commands = () => page.getByRole('region', { name: 'Comandos iFood do pedido de teste', exact: true, includeHidden: true });
const profile = () => page.getByRole('region', { name: 'Preparação da conta nova no iFood', exact: true });
const writes = () => requests.filter(request => ['POST', 'PATCH'].includes(request.method));
const count = (suffix, method = 'POST') => requests.filter(request => request.path.endsWith(suffix) && request.method === method).length;
const clone = value => JSON.parse(JSON.stringify(value));
async function waitFor(predicate, message) {
  const until = Date.now() + 8000;
  while (Date.now() < until) { if (await predicate()) return; await new Promise(resolve => setTimeout(resolve, 30)); }
  throw new Error(message);
}
async function check(name, task) { await task(); checks++; process.stdout.write(`✓ ${name}\n`); }
async function view(key) {
  await page.waitForFunction(() => document.getElementById('sidebar').inert === (window.matchMedia('(max-width: 800px)').matches && !document.body.classList.contains('sidebar-open')));
  if (await page.locator('#sidebar').evaluate(node => node.inert)) await page.locator('#menu-toggle').click();
  await page.locator(`#navigation button[data-view="${key}"]`).click();
  await page.locator('#screen[aria-busy="false"]').waitFor();
  if (key === 'integrations') {
    await merchant().getByRole('status').filter({ hasText: merchantAvailable ? 'Operações de teste disponíveis.' : 'Loja de teste indisponível.' }).waitFor();
    await catalog().getByRole('status').filter({ hasText: catalogAvailable ? 'Catálogo de teste disponível.' : 'Catálogo indisponível.' }).waitFor();
  }
}
function hold(pathname) {
  let release;
  heldPath = pathname; held = new Promise(resolve => { release = resolve; }); captured = false;
  return () => { heldPath = undefined; const done = held; held = undefined; release(); return done; };
}
function merchantReadiness() {
  return { ...activeMetadata(), state: merchantAvailable ? 'available' : 'not_ready', message: merchantAvailable ? 'Operações de teste disponíveis.' : 'Loja de teste indisponível.',
    writes_available: merchantAvailable, hours_write_available: false, hours_write_reason: 'Escrita de horários bloqueada: contrato incompleto.', operations: clone(merchantOperations) };
}
function catalogReadiness() {
  return { ...activeMetadata(), available: catalogAvailable, state: catalogAvailable ? 'available' : 'not_ready',
    message: catalogAvailable ? 'Catálogo de teste disponível.' : 'Catálogo indisponível.',
    capabilities: { image_upload: false, image_replacement: false, bulk_patch: false, single_entity_patch: true, group_creation: true },
    operations: clone(catalogOperations), resources: clone(resources) };
}
function pauseOperation(request, state, kind = 'create_interruption', parent) {
  return { id: `${OPERATION.slice(0, -2)}${String(merchantOperations.length).padStart(2, '0')}`, kind, parent_id: parent || null, idempotency_key: request.idempotency_key,
    resource_id: 'provider-pause-id', state, created_at: stamp, updated_at: stamp,
    result: kind === 'create_interruption' ? { interruption: { id: 'provider-pause-id', description: request.description, start: request.start, end: request.end } } : { removed: state === 'confirmed' } };
}
async function start() {
  directory = await mkdtemp(path.join(os.tmpdir(), 'sahara-ifood-workflows-'));
  const probe = net.createServer(); probe.listen(0, '127.0.0.1'); await once(probe, 'listening');
  const port = probe.address().port; await new Promise(resolve => probe.close(resolve));
  const script = 'import os; from server.core import hash_password; os.environ["SAHARA_ADMIN_PASSWORD_HASH"]=hash_password(os.environ.pop("SAHARA_TEST_PASSWORD")); import uvicorn; uvicorn.run("server.main:app",host="127.0.0.1",port=' + port + ',log_level="warning")';
  backend = spawn(process.env.PYTHON || '.venv/bin/python', ['-u', '-c', script], { cwd: root,
    env: { ...process.env, SAHARA_DATA_DIR: directory, SAHARA_COOKIE_SECURE: '0', SAHARA_TEST_PASSWORD: password,
      SAHARA_WHATSAPP_ENABLED: '0', SAHARA_IFOOD_ENABLED: '0', SAHARA_IFOOD_IMPORT_ENABLED: '0',
      SAHARA_IFOOD_CLIENT_ID: '', SAHARA_IFOOD_CLIENT_SECRET: '', SAHARA_IFOOD_MERCHANT_ID: '' }, stdio: ['ignore', 'pipe', 'pipe'] });
  let output = ''; backend.stdout.on('data', data => { output += data; }); backend.stderr.on('data', data => { output += data; });
  baseURL = `http://127.0.0.1:${port}`;
  await waitFor(async () => {
    if (backend.exitCode !== null) throw new Error(`Disposable backend stopped: ${output}`);
    try { return (await fetch(`${baseURL}/api/health`)).ok; } catch { return false; }
  }, `Disposable backend did not start: ${output}`);
  browser = await chromium.launch({ executablePath: '/usr/bin/chromium', headless: true, args: ['--no-sandbox'] });
  context = await browser.newContext({ viewport: { width: 1280, height: 900 }, timezoneId: 'America/Sao_Paulo', serviceWorkers: 'block' });
  await context.route('**/*', async route => {
    const request = route.request(), url = new URL(request.url()), path = url.pathname, method = request.method();
    if (url.origin !== baseURL) { external.push(url.origin); return route.abort(); }
    const reply = (data, status = 200) => route.fulfill({ status, contentType: 'application/json', body: JSON.stringify(data) });
    if (path === '/api/admin/orders' && method === 'GET') return reply({ orders: [fixtureOrder] });
    if (!(path.startsWith(merchantPath + '/') || path.startsWith(catalogPath) || path.startsWith(orderPath) || path.startsWith(profilePath))) return route.continue();
    const body = request.postDataJSON(); requests.push({ path, method, body, csrf: request.headers()['x-sahara-csrf'] });
    if (heldPath === path) { captured = true; await held; }
    if (path === profilePath) return reply({ environment: 'test', ...(profileCheckAvailable ? newMetadata : { profile_mode: 'legacy' }), state: profileVerified ? 'verified' : 'unconfigured', message: profileMessage,
      discovery_available: profileDiscoveryAvailable, check_available: profileCheckAvailable,
      connection_verified: profileVerified, operations_available: profileVerified,
      selected_for_manual_scenarios: profileSelected, required_variables: ['SERVER_ONLY_VARIABLE'], missing: ['SERVER_ONLY_VARIABLE'] }, profileHTTP);
    if (path === profilePath + '/discover') return reply({ environment: 'test', state: profileDiscoverState,
      message: profileDiscoverMessage, operations_available: false, connection_verified: false,
      merchants: profileDiscoverState === 'queried' ? [{ merchant_id: MID, is_test: true, name: 'IGNORED_PROVIDER_FIELD' }] : [] });
    if (path === profilePath + '/check') {
      if (profileCheckHTTP !== 200) return reply({ detail: 'Verificação indisponível.' }, profileCheckHTTP);
      profileVerified = profileCheckState === 'verified'; profileSelected = true;
      merchantAvailable = profileVerified; catalogAvailable = profileVerified;
      return reply({ ...newMetadata, state: profileCheckState, message: profileVerified ? 'Conexão da conta nova verificada.' : 'A conta nova não foi verificada.',
        connection_verified: profileVerified, operations_available: profileVerified });
    }
    if (path === merchantPath + '/actions') return reply(merchantReadiness());
    if (path === merchantPath + '/interruptions/query') return reply({ ...metadata, state: 'queried', message: 'Pausas consultadas.', interruptions: [{ id: 'provider-pause-id', description: marker, start: stamp, end: '2026-10-16T01:00:00Z' }] });
    if (path === merchantPath + '/opening-hours/query') return reply({ ...metadata, state: 'queried', message: 'Horários consultados.', opening_hours: [{ shifts: [{ marker }] }] });
    if (path === merchantPath + '/interruptions') {
      if (merchantFailure === 'validation') return reply({ detail: 'Descrição rejeitada antes do envio.' }, 422);
      const operation = pauseOperation(body, createState); merchantOperations.unshift(operation);
      if (merchantFailure === 'response_lost') return route.abort();
      return reply({ ...metadata, state: createState, message: createState === 'confirmed' ? 'Pausa confirmada no teste.' : 'Resultado da pausa incerto.', operation: clone(operation) });
    }
    if (path.startsWith(merchantPath + '/operations/')) {
      const operation = merchantOperations.find(value => path.includes(value.id)); operation.state = 'confirmed';
      return reply({ ...metadata, state: 'confirmed', message: 'Pausa conferida sem reenvio.', operation: clone(operation) });
    }
    if (path.includes('/remove')) {
      const parent = path.split('/').at(-2);
      const operation = pauseOperation(body, 'confirmed', 'remove_interruption', parent); merchantOperations.unshift(operation);
      return reply({ ...metadata, state: 'confirmed', message: 'Remoção confirmada.', operation: clone(operation) });
    }
    if (path === catalogPath && method === 'GET') return reply(catalogReadiness());
    if (path === catalogPath + '/discover') return reply({ ...metadata, state: 'queried', message: 'Catálogos consultados.', catalogs: [{ catalogId: CATALOG, context: 'Teste' }], merchant_id: MID });
    if (path === catalogPath + '/inspect') return reply({ environment: 'test', state: 'queried', catalog_id: CATALOG, message: 'Categorias consultadas.', categories: [
      { id: CATEGORY, name: marker, template: 'PROVIDER_TEMPLATE', status: 'OBSERVED_ACTIVE', items: [
        { id: ITEM, name: 'Modelo simples existente', status: 'OBSERVED_ACTIVE', price: { value: 5 }, optionGroups: [] },
        { id: 'aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa', name: 'Modelo com opção', status: 'OBSERVED_PAUSED', optionGroups: [{ id: GROUP, name: 'Grupo existente de teste', options: [{ id: OPTION, name: 'Opção', status: 'OBSERVED_ACTIVE' }, { id: 'cccccccc-cccc-4ccc-8ccc-cccccccccccc', name: 'Opção 2', status: 'OBSERVED_PAUSED' }] }] },
      ] },
    ] });
    if (path === catalogPath + '/reconcile') {
      const operation = catalogOperations.find(value => value.operation_key === body.operation_key); operation.state = 'confirmed';
      return reply({ ...operation, environment: 'test', message: 'Alteração conferida sem reenvio.' });
    }
    if (path.startsWith(catalogPath + '/')) {
      if (catalogFailure === 'validation') return reply({ detail: 'Preço rejeitado antes do envio.' }, 422);
      let kind, resource;
      if (path.endsWith('/categories')) {
        kind = 'category'; resource = NEW_CATEGORY;
        resources.push({ catalog_id: CATALOG, kind, remote_id: resource, data: { name: 'Teste Homologação' } });
      } else if (path.endsWith('/items')) {
        kind = 'item'; resource = NEW_ITEM;
        resources.push({ catalog_id: CATALOG, kind, remote_id: resource, item_id: resource, data: { name: 'Produto Teste' } });
      } else if (path.endsWith('/groups')) { kind = 'group'; resource = GROUP;
      } else { kind = path.split('/').slice(-2).join('_').replace('items_', 'item_').replace('options_', 'option_'); resource = body.entity_id; }
      const state = path.endsWith('/price') || path.endsWith('/status') ? catalogPatchState : 'confirmed';
      const operation = { operation_key: body.operation_key, kind, resource_id: resource, catalog_id: CATALOG, state, checked_at: stamp };
      catalogOperations.unshift(operation);
      if (catalogFailure === 'response_lost') return route.abort();
      return reply({ ...operation, environment: 'test', message: state === 'pending' ? 'Alteração pendente de conferência.' : 'Alteração de teste conferida.',
        ...(['item', 'group'].includes(kind) ? { photo_source: 'existing_test_template', image_upload_completed: false } : {}) });
    }
    if (path === orderPath + '/cancellation-reasons') return reply({ ...metadata, state: 'queried', reasons: [{ cancelCodeId: 'CURRENT-CODE', description: marker }] });
    if (path === orderPath + '/actions' && method === 'GET') return reply({ environment: 'test', actions_available: !orderFailure,
      attempts: clone(orderAttempts), local_status_changed: false });
    if (path === orderPath + '/actions' && method === 'POST') {
      if (orderFailure) return route.abort();
      const attempt = { state: orderLostState || 'accepted_pending', message: 'Solicitação aceita pelo iFood para processamento. A etapa ainda não foi confirmada.',
        action: body.action, idempotency_key: body.idempotency_key, attempt_id: `attempt-${orderAttempts.length}`, environment: 'test', checked_at: stamp, http_status: 202, local_status_changed: false };
      orderAttempts.unshift(attempt);
      if (orderLostState) return route.abort();
      return reply(attempt);
    }
    throw new Error(`Unexpected workflow request: ${method} ${path}`);
  });
  page = await context.newPage(); page.setDefaultTimeout(8000); page.on('pageerror', error => errors.push(error.message));
  await page.addInitScript(() => { window.__ifoodXSS = 0; window.print = () => {}; });
  await page.goto(`${baseURL}/admin.html`); await page.locator('#login-panel:not([hidden])').waitFor();
  await page.locator('#admin-password').fill(password); await page.getByRole('button', { name: 'Entrar no painel', exact: true }).click();
  await page.locator('#workspace:not([hidden])').waitFor(); await page.locator('#screen[aria-busy="false"]').waitFor();
}
async function readyCatalog() {
  await catalog().getByRole('button', { name: 'Consultar catálogos no iFood', exact: true }).click();
  await catalog().getByRole('status').filter({ hasText: 'Catálogos consultados.' }).waitFor();
  await catalog().getByLabel('Catálogo consultado no iFood', { exact: true }).selectOption(CATALOG);
  await catalog().getByRole('button', { name: 'Consultar categorias e itens', exact: true }).click();
  await catalog().getByRole('status').filter({ hasText: 'Categorias consultadas.' }).waitFor();
}
async function fillPause() {
  await merchant().getByLabel('Descrição da pausa de teste', { exact: true }).fill('Pausa fictícia de homologação');
  await merchant().getByLabel('Início da pausa · horário deste computador', { exact: true }).fill('2026-10-16T10:00');
  await merchant().getByLabel('Fim da pausa · horário deste computador', { exact: true }).fill('2026-10-16T10:30');
}
async function run() {
  await start();
  await check('Mount performs local readiness GET only, including imported order commands', async () => {
    await commands().getByRole('status', { includeHidden: true }).filter({ hasText: 'Solicitações manuais disponíveis' }).waitFor({ state: 'attached' });
    await view('integrations');
    assert.equal(writes().length, 0); assert.equal(count('/cancellation-reasons', 'GET'), 0);
    assert.match(await merchant().innerText(), /três lojas reais continua pendente/);
    assert.match(await catalog().innerText(), /57 produtos do site não serão enviados/);
    assert.match(await catalog().innerText(), /Upload ou troca de fotos/);
    assert.match(await merchant().innerText(), /Escrita de horários bloqueada/);
  });
  await check('New account preparation is informational, renders plain text and fails independently', async () => {
    await profile().getByRole('status').filter({ hasText: profileMessage }).waitFor();
    assert.match(await profile().innerText(), /A consulta do UUID apenas identifica a loja de teste/);
    assert.equal(await profile().locator('input, select, textarea').count(), 0);
    assert.equal(await profile().getByRole('button').count(), 3);
    assert.equal(await profile().getByRole('button', { name: 'Consultar UUID da loja de teste da conta nova', exact: true }).isDisabled(), true);
    assert.equal(await profile().getByRole('button', { name: 'Verificar conexão da conta nova', exact: true }).isDisabled(), true);
    assert.equal(count(profilePath + '/check'), 0);
    assert.doesNotMatch(await profile().innerText(), /SERVER_ONLY_VARIABLE/);
    assert.equal(requests.filter(request => request.path === profilePath).every(request => request.method === 'GET'), true);
    const before = writes().length; profileHTTP = 503;
    await profile().getByRole('button', { name: 'Atualizar status da conta nova', exact: true }).click();
    await profile().getByRole('status').filter({ hasText: 'Não foi possível consultar o status da conta nova' }).waitFor();
    assert.equal(await merchant().getByRole('button', { name: 'Consultar pausas no iFood', exact: true }).isEnabled(), true);
    assert.equal(await catalog().getByRole('button', { name: 'Consultar catálogos no iFood', exact: true }).isEnabled(), true);
    profileHTTP = 200; profileMessage = marker;
    await profile().getByRole('button', { name: 'Atualizar status da conta nova', exact: true }).click();
    await profile().getByRole('status').filter({ hasText: marker }).waitFor();
    assert.equal(await profile().locator('img, script').count(), 0); assert.equal(await page.evaluate(() => window.__ifoodXSS), 0);
    assert.equal(writes().length, before);
  });
  await check('New account UUID discovery is manual, deduplicated and never activates the profile', async () => {
    const discover = profile().getByRole('button', { name: 'Consultar UUID da loja de teste da conta nova', exact: true });
    assert.equal(count(profilePath + '/discover'), 0);
    await discover.evaluate(node => node.click()); assert.equal(count(profilePath + '/discover'), 0);
    profileDiscoveryAvailable = true;
    await profile().getByRole('button', { name: 'Atualizar status da conta nova', exact: true }).click();
    await profile().locator(':scope[aria-busy="false"]').waitFor(); assert.equal(await discover.isEnabled(), true);
    assert.equal(count(profilePath + '/discover'), 0);
    const release = hold(profilePath + '/discover');
    await discover.click(); await waitFor(() => captured, 'Missing manual new-account discovery');
    await discover.evaluate(node => node.click()); assert.equal(count(profilePath + '/discover'), 1); assert.equal(await discover.isDisabled(), true);
    release(); await profile().getByRole('status').filter({ hasText: profileDiscoverMessage }).waitFor();
    assert.match(await profile().innerText(), new RegExp(`UUID da loja de teste: ${MID}`));
    assert.match(await profile().innerText(), /Esta consulta não verifica nem ativa a conexão/);
    assert.match(await profile().innerText(), /Conexão da conta nova ainda não verificada/);
    assert.equal(count(profilePath + '/check'), 0);
    assert.doesNotMatch(await profile().innerText(), /IGNORED_PROVIDER_FIELD|SERVER_ONLY_VARIABLE/);
    const request = requests.find(value => value.path === profilePath + '/discover'); assert.deepEqual(request.body, {}); assert.ok(request.csrf);
    assert.equal(await profile().locator('input, select, textarea, img, script').count(), 0);
    profileDiscoverState = 'wait'; profileDiscoverMessage = 'Aguarde antes de consultar novamente.';
    await discover.click(); await profile().getByRole('status').filter({ hasText: profileDiscoverMessage }).waitFor();
    assert.doesNotMatch(await profile().innerText(), new RegExp(MID));
    profileDiscoveryAvailable = false;
    await profile().getByRole('button', { name: 'Atualizar status da conta nova', exact: true }).click();
    await profile().locator(':scope[aria-busy="false"]').waitFor(); assert.equal(await discover.isDisabled(), true);
    const before = count(profilePath + '/discover'); await discover.evaluate(node => node.click());
    assert.equal(count(profilePath + '/discover'), before);
  });
  await check('Remote read queries are explicit, deduplicated and display provider text safely', async () => {
    const release = hold(merchantPath + '/interruptions/query');
    const control = merchant().getByRole('button', { name: 'Consultar pausas no iFood', exact: true });
    await control.click(); await waitFor(() => captured, 'Missing manual interruption query');
    await control.evaluate(node => node.click()); assert.equal(count('/interruptions/query'), 1); assert.equal(await control.isDisabled(), true);
    release(); await merchant().getByRole('status').filter({ hasText: 'Pausas consultadas.' }).waitFor();
    const returned = merchant().getByText('Pausas retornadas pelo iFood', { exact: true }); await returned.click();
    assert.equal(JSON.parse(await merchant().locator('pre').textContent())[0].description, marker);
    assert.equal(await merchant().locator('img, script').count(), 0);
    assert.equal(await page.evaluate(() => window.__ifoodXSS), 0);
    assert.match(await merchant().innerText(), new RegExp(CLIENT));
    assert.ok(writes().at(-1).csrf); assert.deepEqual(writes().at(-1).body, {});
    await merchant().getByRole('button', { name: 'Consultar horários no iFood', exact: true }).click();
    await merchant().getByRole('status').filter({ hasText: 'Horários consultados.' }).waitFor();
    assert.equal(count('/opening-hours/query'), 1);
  });
  await check('Pausas validate date order and send explicit UTC times with an idempotency key', async () => {
    await fillPause(); await merchant().getByLabel('Fim da pausa · horário deste computador', { exact: true }).fill('2026-10-16T09:30');
    await merchant().getByRole('button', { name: 'Criar pausa na loja de teste', exact: true }).click();
    await merchant().getByRole('status').filter({ hasText: 'O fim precisa ser posterior' }).waitFor(); assert.equal(count('/interruptions'), 0);
    await merchant().getByLabel('Fim da pausa · horário deste computador', { exact: true }).fill('2026-10-16T10:30');
    await merchant().getByRole('button', { name: 'Criar pausa na loja de teste', exact: true }).click();
    await merchant().getByRole('status').filter({ hasText: 'Pausa confirmada no teste.' }).waitFor();
    await merchant().locator(':scope[aria-busy="false"]').waitFor();
    const request = writes().find(value => value.path === merchantPath + '/interruptions');
    assert.equal(request.body.start, '2026-10-16T13:00:00.000Z'); assert.equal(request.body.end, '2026-10-16T13:30:00.000Z');
    assert.match(request.body.idempotency_key, /^[a-f0-9-]{36}$/); assert.equal('merchant_id' in request.body, false);
    assert.equal(await merchant().getByRole('button', { name: 'Remover esta pausa de teste', exact: true }).count(), 1);
  });
  await check('Removing a pause uses the local operation ID, never its provider ID', async () => {
    const created = merchantOperations[0];
    await merchant().getByRole('button', { name: 'Remover esta pausa de teste', exact: true }).click();
    await merchant().getByRole('status').filter({ hasText: 'Remoção confirmada.' }).waitFor();
    await merchant().locator(':scope[aria-busy="false"]').waitFor();
    const request = writes().at(-1); assert.equal(request.path, `${merchantPath}/interruptions/${created.id}/remove`);
    assert.notEqual(created.id, created.resource_id); assert.match(request.body.idempotency_key, /^[a-f0-9-]{36}$/);
    assert.equal(await merchant().getByRole('button', { name: 'Remover esta pausa de teste', exact: true }).count(), 0);
  });
  await check('Uncertain pause blocks new writes until manual readback and never replays creation', async () => {
    createState = 'uncertain'; await fillPause();
    await merchant().getByRole('button', { name: 'Criar pausa na loja de teste', exact: true }).click();
    await merchant().getByRole('status').filter({ hasText: 'Resultado da pausa incerto.' }).waitFor();
    assert.equal(await merchant().getByRole('button', { name: 'Criar pausa na loja de teste', exact: true }).isDisabled(), true);
    const before = count('/interruptions');
    await merchant().getByRole('button', { name: 'Conferir esta operação no iFood', exact: true }).click();
    await merchant().getByRole('status').filter({ hasText: 'Pausa conferida sem reenvio.' }).waitFor();
    await merchant().locator(':scope[aria-busy="false"]').waitFor();
    assert.equal(count('/interruptions'), before); assert.equal(await merchant().getByRole('button', { name: 'Criar pausa na loja de teste', exact: true }).isEnabled(), true);
    createState = 'confirmed';
  });
  await check('Pause validation errors release the form; a lost response requires its persisted operation', async () => {
    await fillPause(); merchantFailure = 'validation';
    const create = merchant().getByRole('button', { name: 'Criar pausa na loja de teste', exact: true });
    await create.click(); await merchant().getByRole('status').filter({ hasText: 'Descrição rejeitada antes do envio.' }).waitFor();
    assert.equal(await create.isEnabled(), true);
    merchantFailure = 'response_lost'; const before = count('/interruptions');
    await create.click(); await merchant().getByRole('status').filter({ hasText: 'conexão foi interrompida' }).waitFor();
    assert.equal(await create.isDisabled(), true);
    merchantFailure = null;
    await merchant().getByRole('button', { name: 'Atualizar operações de pausas', exact: true }).click();
    await merchant().locator(':scope[aria-busy="false"]').waitFor();
    assert.equal(await create.isEnabled(), true); assert.equal(count('/interruptions'), before + 1);
    assert.equal(merchantOperations[0].idempotency_key, writes().findLast(value => value.path === merchantPath + '/interruptions').body.idempotency_key);
  });
  await check('Catalog discovery and inspection require separate clicks and observed resource choices', async () => {
    const before = count('/inspect');
    await catalog().getByRole('button', { name: 'Consultar catálogos no iFood', exact: true }).click();
    await catalog().getByRole('status').filter({ hasText: 'Catálogos consultados.' }).waitFor();
    await catalog().getByLabel('Catálogo consultado no iFood', { exact: true }).selectOption(CATALOG);
    assert.equal(count('/inspect'), before);
    await catalog().getByRole('button', { name: 'Consultar categorias e itens', exact: true }).click();
    await catalog().getByRole('status').filter({ hasText: 'Categorias consultadas.' }).waitFor();
    assert.deepEqual(writes().at(-1).body, { catalog_id: CATALOG });
    await catalog().getByText('Criar categoria de homologação', { exact: true }).click();
    await catalog().getByLabel('Categoria existente para usar como modelo', { exact: true }).selectOption(CATEGORY);
    assert.equal(await catalog().locator('img, script').count(), 0);
  });
  await check('Fixed test category and product writes use selected models and never claim image upload', async () => {
    await catalog().getByRole('button', { name: 'Criar categoria Teste Homologação', exact: true }).click();
    await catalog().getByRole('status').filter({ hasText: 'Alteração de teste conferida.' }).waitFor();
    let request = writes().at(-1); assert.equal(request.body.template_category_id, CATEGORY); assert.equal(request.body.catalog_id, CATALOG);
    assert.equal('name' in request.body, false); assert.match(request.body.operation_key, /^[a-f0-9-]{36}$/);
    await catalog().getByText('Criar item de homologação', { exact: true }).click();
    await catalog().getByLabel('Categoria criada nesta homologação', { exact: true }).selectOption(NEW_CATEGORY);
    await catalog().getByLabel('Item existente para usar como modelo', { exact: true }).selectOption(ITEM);
    await catalog().getByLabel('Preço do Produto Teste · R$', { exact: true }).fill('5,55');
    await catalog().getByRole('button', { name: 'Criar Produto Teste com foto do modelo', exact: true }).click();
    await catalog().getByText('Foto do modelo existente reutilizada. Upload de foto não realizado.', { exact: true }).waitFor();
    request = writes().at(-1); assert.equal(request.body.price_cents, 555); assert.equal(request.body.category_id, NEW_CATEGORY); assert.equal(request.body.template_item_id, ITEM);
    assert.equal('image' in request.body, false); assert.match(await catalog().innerText(), /não conclui o cenário de imagens/);
  });
  await check('Group cloning selects a real two-option model and only an owned destination item', async () => {
    await catalog().getByText('Criar grupo e duas opções de homologação', { exact: true }).click();
    await catalog().getByLabel('Item criado que receberá os complementos', { exact: true }).selectOption(NEW_ITEM);
    await catalog().getByLabel('Grupo existente com duas opções para usar como modelo', { exact: true }).selectOption(`aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa:${GROUP}`);
    await catalog().getByLabel('Preço do Complemento Teste 1 · R$', { exact: true }).fill('1,01');
    await catalog().getByLabel('Preço do Complemento Teste 2 · R$', { exact: true }).fill('2,02');
    await catalog().getByRole('button', { name: 'Criar dois complementos com fotos dos modelos', exact: true }).click();
    await catalog().getByText('Foto do modelo existente reutilizada. Upload de foto não realizado.', { exact: true }).waitFor();
    const request = writes().at(-1);
    assert.equal(request.body.item_id, NEW_ITEM); assert.equal(request.body.template_item_id, 'aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa');
    assert.equal(request.body.template_group_id, GROUP); assert.equal(request.body.option1_price_cents, 101); assert.equal(request.body.option2_price_cents, 202);
    assert.equal(request.body.catalog_id, CATALOG); assert.equal('merchant_id' in request.body, false);
  });
  await check('Four PATCH controls use owned IDs, exact integer cents and only observed statuses', async () => {
    await catalog().getByText('Alterar preço ou disponibilidade de uma entidade', { exact: true }).click();
    for (const action of ['items/price', 'items/status', 'options/price', 'options/status']) {
      await catalog().getByLabel('Alteração individual', { exact: true }).selectOption(action);
      await catalog().getByLabel('Entidade criada nesta homologação', { exact: true }).selectOption(action.startsWith('items') ? ITEM : OPTION);
      if (action.endsWith('price')) await catalog().getByLabel('Novo preço · R$', { exact: true }).fill('4,01');
      else await catalog().getByLabel('Status já observado na loja de teste', { exact: true }).selectOption('OBSERVED_PAUSED');
      const before = count('/' + action, 'PATCH');
      await catalog().getByRole('button', { name: 'Aplicar alteração individual no teste', exact: true }).click();
      await waitFor(() => count('/' + action, 'PATCH') === before + 1, 'Missing patch ' + action);
      await catalog().locator(':scope[aria-busy="false"]').waitFor();
      const request = writes().at(-1); assert.equal(request.path, catalogPath + '/' + action); assert.equal(request.method, 'PATCH');
      assert.equal(request.body.entity_id, action.startsWith('items') ? ITEM : OPTION);
      assert.equal(request.body.catalog_id, CATALOG); assert.ok(request.csrf);
      if (action.endsWith('price')) assert.equal(request.body.price_cents, 401); else assert.equal(request.body.status, 'OBSERVED_PAUSED');
      assert.equal('merchant_id' in request.body, false);
    }
  });
  await check('Catalog pending state blocks writes, reconciliation makes no repeated PATCH', async () => {
    catalogPatchState = 'pending';
    await catalog().getByLabel('Alteração individual', { exact: true }).selectOption('items/price');
    await catalog().getByLabel('Entidade criada nesta homologação', { exact: true }).selectOption(ITEM);
    await catalog().getByLabel('Novo preço · R$', { exact: true }).fill('4,02');
    await catalog().getByRole('button', { name: 'Aplicar alteração individual no teste', exact: true }).click();
    await catalog().getByRole('status').filter({ hasText: 'Alteração pendente de conferência.' }).waitFor();
    assert.equal(await catalog().getByRole('button', { name: 'Criar categoria Teste Homologação', exact: true, includeHidden: true }).isDisabled(), true);
    assert.equal(await catalog().getByRole('button', { name: 'Aplicar alteração individual no teste', exact: true }).isDisabled(), true);
    const before = count('/items/price', 'PATCH');
    await catalog().getByRole('button', { name: 'Conferir esta alteração no iFood', exact: true }).click();
    await catalog().getByRole('status').filter({ hasText: 'Alteração conferida sem reenvio.' }).waitFor();
    await catalog().locator(':scope[aria-busy="false"]').waitFor();
    assert.equal(count('/items/price', 'PATCH'), before); assert.equal(await catalog().getByRole('button', { name: 'Aplicar alteração individual no teste', exact: true }).isEnabled(), true);
    catalogPatchState = 'confirmed';
  });
  await check('Catalog validation errors release the form; lost responses recover from the exact operation key', async () => {
    const patch = catalog().getByRole('button', { name: 'Aplicar alteração individual no teste', exact: true });
    catalogFailure = 'validation';
    await patch.click(); await catalog().getByRole('status').filter({ hasText: 'Preço rejeitado antes do envio.' }).waitFor();
    assert.equal(await patch.isEnabled(), true);
    catalogFailure = 'response_lost'; const before = count('/items/price', 'PATCH');
    await patch.click(); await catalog().getByRole('status').filter({ hasText: 'conexão foi interrompida' }).waitFor();
    assert.equal(await patch.isDisabled(), true);
    catalogFailure = null;
    await catalog().getByRole('button', { name: 'Atualizar operações do catálogo', exact: true }).click();
    await catalog().locator(':scope[aria-busy="false"]').waitFor();
    assert.equal(await patch.isEnabled(), true); assert.equal(count('/items/price', 'PATCH'), before + 1);
    assert.equal(catalogOperations[0].operation_key, writes().findLast(value => value.path === catalogPath + '/items/price').body.operation_key);
  });
  await check('Late results after navigation cannot alter the current view or notifications', async () => {
    const release = hold(merchantPath + '/opening-hours/query');
    await merchant().getByRole('button', { name: 'Consultar horários no iFood', exact: true }).click();
    await waitFor(() => captured, 'Missing held query'); await view('orders'); const before = await page.locator('#notice').textContent();
    release(); await page.waitForTimeout(100);
    assert.equal(await merchant().count(), 0); assert.equal(await page.locator('#notice').textContent(), before);
    await commands().getByRole('status', { includeHidden: true }).filter({ hasText: 'Solicitações manuais disponíveis' }).waitFor({ state: 'attached' });
  });
  await check('Order commands are manual and HTTP 202 remains accepted/pending without changing local stage', async () => {
    await page.getByText('Solicitações manuais ao iFood · teste', { exact: true }).click();
    assert.equal(count('/cancellation-reasons', 'GET'), 0);
    const release = hold(orderPath + '/actions');
    const control = commands().getByRole('button', { name: 'Solicitar confirmação', exact: true });
    await control.click(); await waitFor(() => captured, 'Missing manual order action');
    await control.evaluate(node => node.click()); assert.equal(count(orderPath + '/actions'), 1);
    release(); await commands().getByRole('status').filter({ hasText: 'A etapa ainda não foi confirmada' }).waitFor();
    assert.equal(await control.isDisabled(), true); assert.equal(count(orderPath + '/actions'), 1);
    assert.match(await commands().innerText(), /Aceito para processamento · etapa pendente/);
    assert.equal(await page.locator('.order-card').getAttribute('data-status'), 'ifood_received');
    const request = writes().at(-1); assert.deepEqual(Object.keys(request.body).sort(), ['action', 'idempotency_key']); assert.equal(request.body.action, 'confirm');
    assert.equal(await page.getByRole('button', { name: /Registrar pagamento|Atualizar situação|Atribuir entrega/ }).count(), 0);
  });
  await check('Cancellation requires an explicitly queried current reason and manual justification', async () => {
    const cancel = commands().getByRole('button', { name: 'Solicitar cancelamento no iFood', exact: true });
    assert.equal(await cancel.isDisabled(), true);
    await commands().getByRole('button', { name: 'Consultar motivos de cancelamento', exact: true }).click();
    await commands().getByRole('status').filter({ hasText: 'Motivos atuais consultados' }).waitFor();
    assert.equal(count('/cancellation-reasons', 'GET'), 1);
    assert.equal(await cancel.isDisabled(), true);
    await commands().getByLabel('Motivo de cancelamento retornado pelo iFood', { exact: true }).selectOption('CURRENT-CODE');
    await commands().getByLabel('Justificativa do cancelamento', { exact: true }).fill('Somente cenário fictício de homologação');
    await cancel.click(); await commands().getByRole('status').filter({ hasText: 'A etapa ainda não foi confirmada' }).waitFor();
    const request = writes().at(-1); assert.equal(request.body.cancellation_code, 'CURRENT-CODE'); assert.equal(request.body.reason, 'Somente cenário fictício de homologação');
    assert.equal(request.body.action, 'requestCancellation'); assert.equal(await page.evaluate(() => window.__ifoodXSS), 0);
    assert.equal(await commands().locator('img, script').count(), 0);
  });
  await check('Lost Order responses recover only when the same idempotency key is terminal in the ledger', async () => {
    const dispatch = commands().getByRole('button', { name: 'Solicitar despacho', exact: true });
    const pickup = commands().getByRole('button', { name: 'Solicitar pronto para retirada', exact: true });
    const refresh = commands().getByRole('button', { name: 'Atualizar solicitações deste pedido', exact: true });
    const before = count(orderPath + '/actions'); orderLostState = 'accepted_pending';
    await pickup.click(); await commands().getByRole('status').filter({ hasText: 'Confira a tentativa antes' }).waitFor();
    assert.equal(await dispatch.isDisabled(), true); orderLostState = null;
    const actualKey = orderAttempts[0].idempotency_key; orderAttempts[0].idempotency_key = 'unrelated-key';
    await refresh.click(); await commands().locator(':scope[aria-busy="false"]').waitFor();
    assert.equal(await dispatch.isDisabled(), true);
    orderAttempts[0].idempotency_key = actualKey;
    await refresh.click(); await commands().locator(':scope[aria-busy="false"]').waitFor();
    assert.equal(await dispatch.isEnabled(), true); assert.equal(await pickup.isDisabled(), true);
    assert.equal(count(orderPath + '/actions'), before + 1);
  });
  await check('A lost refusal can be recovered from the same key without sending another command', async () => {
    const prepare = commands().getByRole('button', { name: 'Solicitar preparo', exact: true });
    const dispatch = commands().getByRole('button', { name: 'Solicitar despacho', exact: true });
    const before = count(orderPath + '/actions'); orderLostState = 'refused';
    await prepare.click(); await commands().getByRole('status').filter({ hasText: 'Confira a tentativa antes' }).waitFor();
    assert.equal(await dispatch.isDisabled(), true); orderLostState = null;
    await commands().getByRole('button', { name: 'Atualizar solicitações deste pedido', exact: true }).click();
    await commands().locator(':scope[aria-busy="false"]').waitFor();
    assert.equal(await prepare.isEnabled(), true); assert.equal(await dispatch.isEnabled(), true);
    assert.equal(count(orderPath + '/actions'), before + 1);
  });
  await check('Interrupted order command blocks another command and never auto-retries', async () => {
    orderFailure = true; const before = count(orderPath + '/actions');
    await commands().getByRole('button', { name: 'Solicitar preparo', exact: true }).click();
    await commands().getByRole('status').filter({ hasText: 'Confira a tentativa antes' }).waitFor();
    assert.equal(count(orderPath + '/actions'), before + 1);
    assert.equal(await commands().getByRole('button', { name: 'Solicitar despacho', exact: true }).isDisabled(), true);
    await commands().getByRole('button', { name: 'Atualizar solicitações deste pedido', exact: true }).click();
    await commands().locator(':scope[aria-busy="false"]').waitFor();
    assert.equal(count(orderPath + '/actions'), before + 1);
    assert.equal(await commands().getByRole('button', { name: 'Solicitar despacho', exact: true }).isDisabled(), true);
    orderFailure = false;
  });
  await check('Unavailable configuration disables workflows; mobile expanded content fits viewport', async () => {
    await page.setViewportSize({ width: 390, height: 844 }); await view('integrations');
    await readyCatalog();
    for (const summary of await catalog().locator('summary').all()) await summary.evaluate(node => { node.parentElement.open = true; });
    for (const summary of await merchant().locator('summary').all()) await summary.evaluate(node => { node.parentElement.open = true; });
    assert.equal(await page.evaluate(() => document.documentElement.scrollWidth <= window.innerWidth), true);
    if (process.env.SAHARA_IFOOD_WORKFLOW_CAPTURE_DIR) {
      const target = path.resolve(process.env.SAHARA_IFOOD_WORKFLOW_CAPTURE_DIR); await mkdir(target, { recursive: true });
      await catalog().screenshot({ path: path.join(target, 'catalog-mobile-fixture.png') });
    }
    merchantAvailable = false; catalogAvailable = false;
    await merchant().getByRole('button', { name: 'Atualizar operações de pausas', exact: true }).click();
    await merchant().getByRole('status').filter({ hasText: 'Loja de teste indisponível.' }).waitFor();
    assert.equal(await merchant().getByRole('button', { name: 'Criar pausa na loja de teste', exact: true }).isDisabled(), true);
    await catalog().getByRole('button', { name: 'Atualizar operações do catálogo', exact: true }).click();
    await catalog().getByRole('status').filter({ hasText: 'Catálogo indisponível.' }).waitFor();
    assert.equal(await catalog().getByRole('button', { name: 'Consultar catálogos no iFood', exact: true }).isDisabled(), true);
  });
  await check('A local profile change clears legacy catalog choices and cannot start provider queries', async () => {
    merchantAvailable = true; catalogAvailable = true;
    await view('integrations'); await readyCatalog();
    const before = writes().length;
    merchantAvailable = false; catalogAvailable = false; profileCheckAvailable = true; profileSelected = true;
    profileMessage = 'Aguardando verificação independente da conta nova.';
    await profile().getByRole('button', { name: 'Atualizar status da conta nova', exact: true }).click();
    await profile().locator(':scope[aria-busy="false"]').waitFor();
    await merchant().getByRole('status').filter({ hasText: 'Loja de teste indisponível.' }).waitFor();
    assert.equal(await catalog().getByLabel('Catálogo consultado no iFood', { exact: true }).inputValue(), '');
    assert.equal(await catalog().getByLabel('Categoria existente para usar como modelo', { exact: true }).locator('option').count(), 1);
    assert.equal(await catalog().getByRole('button', { name: 'Consultar categorias e itens', exact: true }).isDisabled(), true);
    assert.equal(writes().length, before);
    assert.match(await merchant().innerText(), new RegExp(NEW_MID));
  });
  await check('New-account verification is a separate manual request and failure never activates scenarios', async () => {
    const verify = profile().getByRole('button', { name: 'Verificar conexão da conta nova', exact: true });
    await waitFor(() => verify.isEnabled(), 'Configured profile did not expose manual verification');
    const before = count(profilePath + '/check');
    const providerBefore = requests.filter(value => value.method !== 'GET' && !value.path.startsWith(profilePath)).length;
    assert.equal(count(profilePath + '/check'), before);
    profileCheckState = 'test_proof_missing';
    const release = hold(profilePath + '/check');
    await verify.click(); await waitFor(() => captured, 'Missing manual verification request');
    await verify.evaluate(node => node.click());
    assert.equal(count(profilePath + '/check'), before + 1); assert.equal(await verify.isDisabled(), true);
    assert.equal(await merchant().count(), 0); assert.equal(await catalog().count(), 0);
    release(); await profile().getByRole('status').filter({ hasText: 'A conta nova não foi verificada.' }).waitFor();
    await merchant().getByRole('status').filter({ hasText: 'Loja de teste indisponível.' }).waitFor();
    assert.equal(await merchant().getByRole('button', { name: 'Criar pausa na loja de teste', exact: true }).isDisabled(), true);
    assert.equal(await catalog().getByRole('button', { name: 'Consultar catálogos no iFood', exact: true }).isDisabled(), true);
    assert.match(await profile().innerText(), /Conexão da conta nova ainda não verificada/);
    assert.equal(profileSelected, true); assert.equal(profileVerified, false);
    const sent = writes().findLast(value => value.path === profilePath + '/check');
    assert.deepEqual(sent.body, {}); assert.ok(sent.csrf);
    assert.equal(requests.filter(value => value.method !== 'GET' && !value.path.startsWith(profilePath)).length, providerBefore);
    assert.equal(await profile().locator('input, select, textarea').count(), 0);
  });
  await check('Successful verification replaces manual context, clears old catalog choices and ignores late reads', async () => {
    profileCheckState = 'verified';
    const before = count(profilePath + '/check');
    await profile().getByRole('button', { name: 'Verificar conexão da conta nova', exact: true }).click();
    await profile().getByRole('status').filter({ hasText: 'Conexão da conta nova verificada.' }).waitFor();
    await merchant().getByRole('status').filter({ hasText: 'Operações de teste disponíveis.' }).waitFor();
    assert.match(await merchant().innerText(), new RegExp(NEW_CLIENT));
    assert.match(await merchant().innerText(), new RegExp(NEW_MID));
    assert.match(await catalog().innerText(), new RegExp(NEW_CLIENT));
    await readyCatalog();
    const release = hold(catalogPath + '/inspect');
    await catalog().getByRole('button', { name: 'Consultar categorias e itens', exact: true }).click();
    await waitFor(() => captured, 'Missing catalog read before account verification');
    const discoveryBefore = count(catalogPath + '/discover');
    await profile().getByRole('button', { name: 'Verificar conexão da conta nova', exact: true }).click();
    await profile().getByRole('status').filter({ hasText: 'Conexão da conta nova verificada.' }).waitFor();
    await merchant().getByRole('status').filter({ hasText: 'Operações de teste disponíveis.' }).waitFor();
    assert.match(await merchant().innerText(), new RegExp(NEW_CLIENT));
    assert.match(await merchant().innerText(), new RegExp(NEW_MID));
    assert.match(await catalog().innerText(), new RegExp(NEW_CLIENT));
    assert.match(await catalog().innerText(), /Conexão da conta nova · Teste \(C\)/);
    assert.equal(await catalog().getByLabel('Catálogo consultado no iFood', { exact: true }).inputValue(), '');
    assert.equal(await catalog().getByRole('button', { name: 'Consultar categorias e itens', exact: true }).isDisabled(), true);
    release(); await page.waitForTimeout(100);
    assert.equal(await catalog().getByLabel('Categoria existente para usar como modelo', { exact: true }).locator('option').count(), 1);
    assert.equal(await catalog().getByRole('button', { name: 'Criar categoria Teste Homologação', exact: true, includeHidden: true }).isDisabled(), true);
    assert.equal(count(profilePath + '/check'), before + 2); assert.equal(count(catalogPath + '/discover'), discoveryBefore);
    assert.match(await profile().innerText(), /comandos dos pedidos importados continuam na conexão anterior/);
  });
  await check('Interrupted verification refreshes local state once and never retries an external request', async () => {
    const before = count(profilePath + '/check');
    profileCheckHTTP = 503;
    await profile().getByRole('button', { name: 'Verificar conexão da conta nova', exact: true }).click();
    await profile().getByRole('status').filter({ hasText: 'A resposta da verificação foi interrompida.' }).waitFor();
    await profile().locator(':scope[aria-busy="false"]').waitFor();
    assert.equal(count(profilePath + '/check'), before + 1);
    assert.match(await profile().innerText(), /Conexão da conta nova verificada/);
    profileCheckHTTP = 200;
  });
  await check('Late verification after navigation leaves the view untouched and Order retains legacy context', async () => {
    const release = hold(profilePath + '/check');
    await profile().getByRole('button', { name: 'Verificar conexão da conta nova', exact: true }).click();
    await waitFor(() => captured, 'Missing held new-account verification');
    await view('orders'); const notice = await page.locator('#notice').textContent();
    release(); await page.waitForTimeout(100);
    assert.equal(await profile().count(), 0); assert.equal(await merchant().count(), 0); assert.equal(await catalog().count(), 0);
    assert.equal(await page.locator('#notice').textContent(), notice);
    await page.getByText('Solicitações manuais ao iFood · teste', { exact: true }).click();
    assert.match(await commands().innerText(), /Este pedido usa a conexão anterior do importador/);
    assert.doesNotMatch(await commands().innerText(), new RegExp(NEW_CLIENT));
  });
  await check('Initial manual workflow readiness starts only after the first profile context is known', async () => {
    const merchantBefore = count(merchantPath + '/actions', 'GET');
    const catalogBefore = count(catalogPath, 'GET');
    const release = hold(profilePath);
    if (await page.locator('#sidebar').evaluate(node => node.inert)) await page.locator('#menu-toggle').click();
    await page.locator('#navigation button[data-view="integrations"]').click();
    await waitFor(() => captured, 'Missing initial profile context request');
    await page.locator('#screen[aria-busy="false"]').waitFor();
    assert.equal(count(merchantPath + '/actions', 'GET'), merchantBefore);
    assert.equal(count(catalogPath, 'GET'), catalogBefore);
    assert.equal(await merchant().count(), 0); assert.equal(await catalog().count(), 0);
    release();
    await merchant().getByRole('status').filter({ hasText: 'Operações de teste disponíveis.' }).waitFor();
    await catalog().getByRole('status').filter({ hasText: 'Catálogo de teste disponível.' }).waitFor();
    assert.match(await merchant().innerText(), new RegExp(NEW_CLIENT));
    assert.match(await catalog().innerText(), new RegExp(NEW_MID));
  });
  assert.deepEqual(errors, []); assert.deepEqual(external, []);
  process.stdout.write(`\n${checks} workflow interface checks passed; no external provider was contacted.\n`);
}
run().catch(async error => { process.stderr.write(`${error.stack}\n`); if (page) process.stderr.write(`Screen: ${(await page.locator('#screen').innerText().catch(() => '')).slice(-2500)}\nErrors: ${JSON.stringify(errors)}\n`); process.exitCode = 1; }).finally(async () => {
  if (browser) await browser.close();
  if (backend && backend.exitCode === null) { const stopped = once(backend, 'exit'); backend.kill('SIGTERM'); await stopped; }
  if (directory) await rm(directory, { recursive: true, force: true });
});
