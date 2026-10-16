'use strict';

// Real disposable backend and Chromium. Merchant responses below are local fixtures,
// never requests to iFood and never evidence of provider homologation.
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
const endpoint = '/api/admin/ifood/merchant';
const clientId = '11111111-1111-4111-8111-111111111111';
const merchantId = '22222222-2222-4222-8222-222222222222';
const marker = '<img src=x onerror="window.__merchantXSS=1">';
const fixture = {
  state: 'queried', message: 'Consulta da loja de teste concluída.', checked_at: '2026-10-15T18:04:00Z',
  environment: 'test', client_id: clientId, configured_merchant_id: merchantId,
  merchants: [{ id: merchantId, name: marker }, { id: '33333333-3333-4333-8333-333333333333', name: 'Segunda loja de teste' }],
  merchant: { id: merchantId, name: marker, address: { streetName: 'Rua de teste ' + 'x'.repeat(180) }, nested: { providerText: '<script>window.__merchantXSS=2</script>' } },
  availability: [{ operation: 'delivery', state: 'PROVIDER_TEST_VALUE', details: { note: marker } }],
};
let backend, browser, context, page, directory, baseURL;
let readiness = { query_available: false, message: 'Verifique a conexão antes de consultar.', environment: 'test', configured_merchant_id: merchantId };
let queryResponse = fixture, queryHTTP = 200, queryWait, readinessHTTP = 200;
const requests = [], errors = [], external = [];
let checks = 0;
const region = () => page.getByRole('region', { name: 'Consulta da loja de teste no iFood', exact: true });
const queryButton = () => region().getByRole('button', { name: 'Consultar lojas e disponibilidade', exact: true });
const posts = () => requests.filter(request => request.method === 'POST').length;

async function waitFor(predicate, message) {
  const until = Date.now() + 6000;
  while (Date.now() < until) {
    if (await predicate()) return;
    await new Promise(resolve => setTimeout(resolve, 30));
  }
  throw new Error(message);
}
async function check(name, action) { await action(); checks++; process.stdout.write(`✓ ${name}\n`); }
function holdQuery() {
  let release;
  queryWait = new Promise(resolve => { release = resolve; });
  return () => { queryWait = null; release(); };
}
async function view(key) {
  await page.waitForFunction(() => document.getElementById('sidebar').inert === (window.matchMedia('(max-width: 800px)').matches && !document.body.classList.contains('sidebar-open')));
  if (await page.locator('#sidebar').evaluate(node => node.inert)) await page.locator('#menu-toggle').click();
  await page.locator(`#navigation button[data-view="${key}"]`).click();
  await page.locator('#screen[aria-busy="false"]').waitFor();
  if (key === 'integrations') await region().getByRole('status').filter({ hasText: readiness.message || readiness.detail }).waitFor();
}
async function refreshReadiness() {
  await page.getByRole('button', { name: 'Atualizar configuração do iFood', exact: true }).click();
  await region().getByRole('status').filter({ hasText: readiness.message || readiness.detail }).waitFor();
}
async function query() {
  await queryButton().click();
  await region().locator('[role="status"]').filter({ hasText: queryResponse.message || queryResponse.detail }).waitFor();
  await region().locator(':scope[aria-busy="false"]').waitFor();
}
async function start() {
  directory = await mkdtemp(path.join(os.tmpdir(), 'sahara-ifood-merchant-admin-'));
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
    const request = route.request(), url = new URL(request.url());
    if (url.origin !== baseURL) { external.push(url.origin); return route.abort(); }
    const reply = (data, status = 200) => route.fulfill({ status, contentType: 'application/json', body: JSON.stringify(data) });
    if (url.pathname === '/api/admin/ifood' && request.method() === 'GET') return reply({ configured: true, missing: [], message: 'Configuração de teste.', last_result: null });
    if (url.pathname === '/api/admin/ifood/check') return reply({ state: 'verified', message: 'Conexão de teste verificada.' });
    if (url.pathname !== endpoint) return route.continue();
    requests.push({ method: request.method(), data: request.postDataJSON(), csrf: request.headers()['x-sahara-csrf'] });
    if (request.method() === 'GET') return reply(readiness, readinessHTTP);
    const body = queryResponse, status = queryHTTP, wait = queryWait;
    if (wait) await wait;
    return reply(body, status);
  });
  page = await context.newPage(); page.on('pageerror', error => errors.push(error.message));
  await page.addInitScript(() => { window.__merchantXSS = 0; window.print = () => {}; });
  await page.goto(`${baseURL}/admin.html`);
  await page.locator('#login-panel:not([hidden])').waitFor();
  await page.locator('#admin-password').fill(password);
  await page.getByRole('button', { name: 'Entrar no painel', exact: true }).click();
  await page.locator('#workspace:not([hidden])').waitFor();
  await page.locator('#screen[aria-busy="false"]').waitFor();
}

async function run() {
  await start();
  await check('Opening and refreshing the panel only check local readiness; unavailable queries stay disabled', async () => {
    await view('integrations');
    assert.equal(await queryButton().isDisabled(), true);
    assert.equal(posts(), 0);
    const before = requests.length;
    await refreshReadiness();
    await waitFor(() => requests.length > before, 'Readiness refresh missing');
    assert.equal(posts(), 0);
    readiness = { ...readiness, query_available: true, message: 'Consulta manual disponível.' };
    await refreshReadiness();
    assert.equal(await queryButton().isEnabled(), true);
    assert.equal(posts(), 0);
  });
  await check('Manual queries disable duplicate clicks and render complete test metadata without executing provider HTML', async () => {
    const release = holdQuery(), before = posts();
    await queryButton().click();
    await waitFor(() => posts() === before + 1, 'Manual query missing');
    assert.equal(await queryButton().isDisabled(), true);
    assert.equal(await region().getAttribute('aria-busy'), 'true');
    await queryButton().evaluate(button => button.click());
    assert.equal(posts(), before + 1);
    release();
    await region().getByRole('status').filter({ hasText: fixture.message }).waitFor();
    await waitFor(() => queryButton().isEnabled(), 'Query button did not become enabled');
    assert.equal(await region().getAttribute('aria-busy'), 'false');
    assert.match(await region().innerText(), new RegExp(clientId));
    assert.match(await region().innerText(), /15\/10\/2026, 15:04/);
    assert.match(await region().innerText(), /Segunda loja de teste/);
    assert.equal(await region().locator('li').count(), 2);
    assert.equal(await region().locator('img, script').count(), 0);
    assert.equal(await page.evaluate(() => window.__merchantXSS), 0);
    assert.ok((await region().locator('pre').nth(0).textContent()).includes('providerText'));
    assert.ok((await region().locator('pre').nth(1).textContent()).includes('PROVIDER_TEST_VALUE'));
    assert.match(await region().innerText(), /Homologação e integração das lojas reais pendentes/);
    const posted = requests.filter(request => request.method === 'POST').at(-1);
    assert.deepEqual(posted.data, {});
    assert.ok(posted.csrf);
  });
  await check('Provider and HTTP failures remove old details and allow retry without exposing success', async () => {
    queryResponse = { state: 'quota', message: 'O iFood limitou as consultas.' };
    await query();
    assert.equal(await region().locator('pre').count(), 0);
    assert.equal(await queryButton().isEnabled(), true);
    queryHTTP = 403; queryResponse = { detail: 'O iFood recusou o acesso à loja.' };
    await query();
    assert.equal(await region().locator('pre').count(), 0);
    assert.equal(await queryButton().isEnabled(), true);
    queryHTTP = 200; queryResponse = fixture;
    await query();
    assert.equal(await region().locator('pre').count(), 2);
  });
  await check('Late successes and errors after navigation do not change the current screen or notices', async () => {
    for (const failure of [false, true]) {
      const release = holdQuery(), before = posts();
      queryHTTP = failure ? 503 : 200;
      queryResponse = failure ? { detail: 'Erro atrasado que não deve aparecer.' } : fixture;
      await queryButton().click();
      await waitFor(() => posts() === before + 1, 'Pending query missing');
      await view('orders');
      const noticeBefore = await page.locator('#notice').textContent();
      const received = page.waitForResponse(response => new URL(response.url()).pathname === endpoint && response.request().method() === 'POST');
      release(); await received;
      await page.evaluate(() => new Promise(resolve => requestAnimationFrame(() => requestAnimationFrame(resolve))));
      assert.equal(await page.locator('.ifood-merchant-panel').count(), 0);
      assert.equal(await page.locator('#notice').textContent(), noticeBefore);
      await view('integrations');
      assert.equal(await region().locator('pre').count(), 0);
      assert.equal(posts(), before + 1, 'Returning to integrations must not query the provider.');
    }
    queryHTTP = 200; queryResponse = fixture;
  });
  await check('Connection checks refresh local readiness, malformed data fails safely, and mobile details fit the viewport', async () => {
    const before = requests.length, providerBefore = posts();
    await page.getByRole('button', { name: 'Testar conexão com o iFood', exact: true }).click();
    await waitFor(() => requests.length > before, 'Connection check did not refresh Merchant readiness');
    assert.equal(posts(), providerBefore);
    queryResponse = { ...fixture, environment: 'production' };
    await queryButton().click();
    await region().getByRole('status').filter({ hasText: 'consulta de lojas inesperada' }).waitFor();
    assert.equal(await region().locator('pre').count(), 0);
    queryResponse = fixture;
    await page.setViewportSize({ width: 390, height: 844 });
    await query();
    for (const summary of await region().locator('summary').all()) await summary.click();
    assert.equal(await page.evaluate(() => document.documentElement.scrollWidth <= window.innerWidth), true);
    if (process.env.SAHARA_IFOOD_MERCHANT_CAPTURE_DIR) {
      const target = path.resolve(process.env.SAHARA_IFOOD_MERCHANT_CAPTURE_DIR); await mkdir(target, { recursive: true });
      await region().screenshot({ path: path.join(target, 'merchant-mobile-fixture.png') });
    }
    readinessHTTP = 503; readiness = { detail: 'Consulta local temporariamente indisponível.' };
    await refreshReadiness();
    assert.equal(await queryButton().isDisabled(), true);
    assert.equal(await region().locator('pre').count(), 0);
  });
  assert.deepEqual(errors, []); assert.deepEqual(external, []);
  process.stdout.write(`\n${checks} Merchant interface checks passed; no external provider was contacted.\n`);
}

run().catch(error => { process.stderr.write(`${error.stack}\n`); process.exitCode = 1; }).finally(async () => {
  if (browser) await browser.close();
  if (backend && backend.exitCode === null) { const stopped = once(backend, 'exit'); backend.kill('SIGTERM'); await stopped; }
  if (directory) await rm(directory, { recursive: true, force: true });
});
