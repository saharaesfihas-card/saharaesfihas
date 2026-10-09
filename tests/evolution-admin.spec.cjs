'use strict';

// Browser contract tests: all API requests are intercepted. No WhatsApp messages,
// provider requests, production credentials or production orders are involved.
const assert = require('node:assert/strict');
const { createServer } = require('node:http');
const { readFile, mkdir } = require('node:fs/promises');
const path = require('node:path');
const { chromium } = require('playwright');

const root = path.resolve(__dirname, '..');
const png = 'data:image/png;base64,iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAYAAAAfFcSJAAAADUlEQVQIHWP4z8DwHwAFgAI/ScLbtAAAAABJRU5ErkJggg==';
const csrf = 'test-only-evolution-csrf';
const fixture = {
  provider: 'evolution', configured: false, enabled: false,
  connection: { state: 'not_configured', connected: false, error: '' },
  missing: ['SAHARA_EVOLUTION_URL', 'SAHARA_EVOLUTION_API_KEY', 'SAHARA_EVOLUTION_INSTANCE', 'SAHARA_EVOLUTION_WEBHOOK_SECRET'],
  last_webhook_at: '', messages: [], conversations: []
};
let server, browser, context, page, baseURL, authorized = false;
let dashboard = structuredClone(fixture), connectResult, connectError, connectWait;
const requests = [], errors = [], external = [];
let checks = 0;

async function check(name, action) { await action(); checks++; process.stdout.write(`✓ ${name}\n`); }
async function render() {
  await page.waitForFunction(() => document.getElementById('sidebar').inert === (window.matchMedia('(max-width: 800px)').matches && !document.body.classList.contains('sidebar-open')));
  if (await page.locator('#sidebar').evaluate(node => node.inert)) await page.locator('#menu-toggle').click();
  await page.locator('#navigation button[data-view="integrations"]').click();
  await page.locator('#screen[aria-busy="false"]').waitFor();
  assert.equal(await page.locator('#screen .danger-text').count(), 0, await page.locator('#screen').innerText());
}
async function capture(name) {
  if (!process.env.SAHARA_EVOLUTION_CAPTURE_DIR) return;
  const directory = path.resolve(process.env.SAHARA_EVOLUTION_CAPTURE_DIR); await mkdir(directory, { recursive: true });
  await page.evaluate(async () => { await Promise.all(document.getAnimations().map(animation => animation.finished.catch(() => {}))); });
  await page.screenshot({ path: path.join(directory, `${name}.png`), fullPage: false });
}
async function pair() {
  const response = page.waitForResponse(response => response.url().endsWith('/api/admin/whatsapp/connect'));
  await page.getByRole('button', { name: 'Gerar QR Code', exact: true }).click();
  await response;
  await page.getByRole('button', { name: /Gerar QR Code|Verificar conexão/, exact: true }).waitFor();
  await page.waitForFunction(() => !document.querySelector('#screen button[aria-busy="true"]'));
}
async function start() {
  server = createServer(async (request, response) => {
    try {
      const filename = path.resolve(root, '.' + new URL(request.url, 'http://localhost').pathname);
      if (!filename.startsWith(root + path.sep)) throw new Error('Outside fixture root');
      const content = await readFile(filename);
      const types = { '.html': 'text/html', '.js': 'text/javascript', '.css': 'text/css', '.svg': 'image/svg+xml', '.png': 'image/png' };
      response.writeHead(200, { 'Content-Type': types[path.extname(filename)] || 'application/octet-stream' }); response.end(content);
    } catch { response.writeHead(404); response.end('Not found'); }
  });
  await new Promise(resolve => server.listen(0, '127.0.0.1', resolve));
  baseURL = `http://127.0.0.1:${server.address().port}`;
  browser = await chromium.launch({ executablePath: '/usr/bin/chromium', headless: true, args: ['--no-sandbox'] });
  context = await browser.newContext({ viewport: { width: 1280, height: 900 }, serviceWorkers: 'block' });
  await context.route('**/*', async route => {
    const request = route.request(); const url = new URL(request.url());
    if (url.origin !== baseURL) { external.push(url.origin); return route.abort(); }
    if (!url.pathname.startsWith('/api/')) return route.continue();
    const method = request.method(); const endpoint = url.pathname;
    const data = request.postData() ? request.postDataJSON() : undefined;
    requests.push({ endpoint, method, data, headers: request.headers() });
    const reply = (body, status = 200) => route.fulfill({ status, contentType: 'application/json', body: JSON.stringify(body) });
    if (endpoint === '/api/health') return reply({ ready: true, admin_configured: true });
    if (endpoint === '/api/admin/login') { authorized = true; return reply({ csrf_token: csrf }); }
    if (endpoint.startsWith('/api/admin/') && !authorized) return reply({ detail: 'Entre no painel.' }, 401);
    if (endpoint === '/api/admin/session') return reply({ csrf_token: csrf });
    if (endpoint === '/api/admin/orders') return reply({ orders: [] });
    if (endpoint === '/api/admin/drivers') return reply({ drivers: [] });
    if (endpoint === '/api/integrations') return reply({ integrations: [{ id: 'whatsapp', label: 'WhatsApp · atendimento e avisos de pedidos', available: dashboard.configured, reason: dashboard.configured ? 'Configuração presente. Confira a conexão.' : 'Configure a Evolution API no Render.' }, { id: 'campaigns', label: 'Campanhas', available: false, reason: 'Envio em massa não ativado.' }] });
    if (endpoint === '/api/admin/whatsapp') return reply(dashboard);
    if (method !== 'GET') assert.equal(request.headers()['x-sahara-csrf'], csrf, `${endpoint} must use the authenticated CSRF token`);
    if (endpoint === '/api/admin/whatsapp/connect') {
      assert.equal(method, 'POST'); assert.deepEqual(data, {});
      if (connectWait) await connectWait;
      if (connectError) return reply({ detail: connectError }, 502);
      return reply(connectResult);
    }
    if (endpoint === '/api/admin/whatsapp/reply' || /\/resume$|\/retry$/.test(endpoint)) return reply({ ok: true }, endpoint.endsWith('/reply') ? 202 : 200);
    return reply({ detail: 'Unmocked fixture API' }, 404);
  });
  page = await context.newPage(); page.on('pageerror', error => errors.push(error.message));
  page.on('dialog', dialog => dialog.accept());
  await page.goto(`${baseURL}/admin.html`);
}
async function run() {
  await start();
  await check('WhatsApp integration stays behind the private admin session', async () => {
    await page.locator('#login-panel:not([hidden])').waitFor();
    assert.equal(requests.some(request => request.endpoint === '/api/admin/whatsapp'), false);
    await page.locator('#admin-password').fill('test-only-password');
    await page.getByRole('button', { name: 'Entrar no painel', exact: true }).click();
    await page.locator('#workspace:not([hidden])').waitFor();
    await page.locator('#screen[aria-busy="false"]').waitFor();
    assert.equal(await page.locator('#admin-password').inputValue(), '');
  });
  await check('No Evolution server leaves configuration pending with a safe setup checklist', async () => {
    await render();
    await page.getByText('WhatsApp: Configuração pendente', { exact: true }).waitFor();
    assert.equal(await page.getByRole('button', { name: 'Gerar QR Code', exact: true }).isDisabled(), true);
    assert.equal(await page.locator('.whatsapp-pairing img').count(), 0);
    assert.equal(await page.locator('#screen input[type="password"]').count(), 0);
    await page.locator('.whatsapp-setup summary').click();
    assert.match(await page.locator('.whatsapp-setup').innerText(), /Instale a Evolution API/);
    assert.deepEqual(await page.locator('.whatsapp-missing code').allTextContents(), fixture.missing);
    assert.equal(requests.filter(request => request.endpoint.endsWith('/whatsapp/connect')).length, 0);
    await capture('pending-desktop');
  });
  await check('Credentials alone do not claim that WhatsApp is connected', async () => {
    dashboard = { ...structuredClone(fixture), configured: true, enabled: true, missing: [], connection: { state: 'close', connected: false, error: '' } };
    await render();
    await page.getByText('WhatsApp: Desconectado', { exact: true }).waitFor();
    assert.equal(await page.getByRole('button', { name: 'Gerar QR Code', exact: true }).isEnabled(), true);
    assert.equal(await page.locator('.whatsapp-pairing img').count(), 0);
    assert.equal(requests.filter(request => request.endpoint.endsWith('/whatsapp/connect')).length, 0);
  });
  await check('Explicit pairing uses CSRF and accessible loading feedback, then shows only a bounded PNG QR', async () => {
    let resolveConnection; connectWait = new Promise(resolve => { resolveConnection = resolve; });
    connectResult = { state: 'connecting', connected: false, qrcode: png };
    const action = pair();
    await page.locator('#screen button[aria-busy="true"]').waitFor();
    assert.equal(await page.getByRole('button', { name: 'Gerar QR Code', exact: true }).isDisabled(), true);
    await page.getByText('Preparando conexão…', { exact: true }).waitFor();
    resolveConnection(); await action; connectWait = null;
    await page.getByAltText('QR Code para conectar o WhatsApp da Sahara').waitFor();
    assert.equal(await page.locator('.whatsapp-pairing img').getAttribute('src'), png);
    await page.getByText(/No WhatsApp Business, abra Aparelhos conectados/).waitFor();
    await capture('qr-desktop');
  });
  await check('Provider failure removes the previous QR and restores a usable action', async () => {
    connectError = 'Não foi possível preparar a conexão. Confira a Evolution API e suas credenciais no Render.';
    await pair();
    assert.equal(await page.locator('.whatsapp-pairing img').count(), 0);
    await page.locator('#notice').filter({ hasText: 'Não foi possível preparar a conexão' }).waitFor();
    await page.getByText('Conexão não confirmada.', { exact: true }).waitFor();
    assert.equal(await page.getByRole('button', { name: 'Gerar QR Code', exact: true }).isEnabled(), true);
    connectError = null;
  });
  await check('Unsafe, oversized and non-PNG QR responses stay out of the DOM', async () => {
    for (const qrcode of ['data:image/svg+xml;base64,PHN2Zz48L3N2Zz4=', 'data:image/png;base64,' + 'A'.repeat(100001), 'data:image/png;base64,PHN2Zz48L3N2Zz4=']) {
      connectResult = { state: 'connecting', connected: false, qrcode };
      await pair();
      assert.equal(await page.locator('.whatsapp-pairing img').count(), 0);
      await page.locator('#notice').filter({ hasText: 'QR Code inválido' }).waitFor();
    }
  });
  await check('A missing QR explains the wait and an expired QR can be generated again', async () => {
    connectResult = { state: 'connecting', connected: false, qrcode: null };
    await pair(); await page.getByText(/O QR Code ainda não está pronto/).waitFor();
    await page.clock.install();
    connectResult = { state: 'connecting', connected: false, qrcode: png };
    await pair(); await page.locator('.whatsapp-pairing img').waitFor();
    await page.clock.fastForward(120001);
    await page.getByText(/Este QR Code pode ter expirado/).waitFor();
    assert.equal(await page.locator('.whatsapp-pairing img').count(), 0);
    assert.equal(await page.getByRole('button', { name: 'Gerar QR Code', exact: true }).isEnabled(), true);
  });
  await check('Only an actual connected response updates the connection label', async () => {
    connectResult = { state: 'open', connected: true, qrcode: null };
    await pair();
    await page.getByText('WhatsApp: Conectado', { exact: true }).waitFor();
    assert.equal(await page.locator('.whatsapp-pairing img').count(), 0);
    assert.equal(await page.getByRole('button', { name: 'Verificar conexão', exact: true }).isEnabled(), true);
    await page.locator('#notice').filter({ hasText: 'WhatsApp conectado.' }).waitFor();
  });
  await check('An instance connected to the wrong number stays blocked with a clear correction', async () => {
    dashboard = { ...dashboard, connection: { state: 'wrong_number', connected: false, error: 'Conecte o WhatsApp da loja.' } };
    await render();
    await page.getByText('WhatsApp: Número conectado diferente do WhatsApp da loja', { exact: true }).waitFor();
    connectResult = { state: 'wrong_number', connected: false, qrcode: null };
    await pair();
    await page.getByText(/A instância está conectada a outro número/).waitFor();
    assert.equal(await page.locator('.whatsapp-pairing img').count(), 0);
    assert.equal(await page.getByText('WhatsApp: Conectado', { exact: true }).count(), 0);
  });
  await check('Inbox controls and delivery confirmations retain their existing meaning', async () => {
    dashboard = { ...dashboard, connection: { state: 'open', connected: true, error: '' }, conversations: [{ phone: '5544999999999', last_message: 'Mensagem de teste', human_until: Date.now() / 1000 + 3600, opted_out: false }], messages: ['accepted', 'sent', 'delivered', 'uncertain', 'failed'].map((status, index) => ({ id: `fixture-${index}`, phone: '5544999999999', body: `Mensagem ${index}`, status, error: '', created_at: '2026-10-09T10:00:00Z' })) };
    await render();
    for (const label of ['Aceita pela API', 'Enviada', 'Entregue', 'Sem confirmação', 'Falhou']) await page.getByRole('cell', { name: label, exact: true }).waitFor();
    assert.equal(await page.getByRole('button', { name: 'Retomar robô', exact: true }).isEnabled(), true);
    await page.locator('#field-integrations-whatsapp_message').fill('Resposta fictícia do teste');
    const replyResponse = page.waitForResponse(response => response.url().endsWith('/api/admin/whatsapp/reply'));
    await page.getByRole('button', { name: 'Enviar resposta', exact: true }).click(); await replyResponse;
    const reply = requests.find(request => request.endpoint.endsWith('/whatsapp/reply'));
    assert.equal(reply.data.phone, '5544999999999'); assert.equal(reply.data.message, 'Resposta fictícia do teste');
    assert.match(reply.data.idempotency_key, /^[0-9a-f-]{36}$/);
    await page.locator('#screen[aria-busy="false"]').waitFor();
  });
  await check('Setup and pairing fit 350px, 390px and desktop widths', async () => {
    for (const width of [350, 390, 1280]) {
      await page.setViewportSize({ width, height: 900 });
      dashboard = structuredClone(fixture); await render();
      await page.locator('.whatsapp-setup summary').click();
      assert.equal(await page.evaluate(() => document.documentElement.scrollWidth > innerWidth + 1), false, `Pending setup overflows at ${width}`);
      dashboard = { ...dashboard, configured: true, enabled: true, connection: { state: 'close', connected: false, error: '' }, missing: [] };
      await render(); connectResult = { state: 'connecting', connected: false, qrcode: png }; await pair();
      await page.locator('.whatsapp-pairing img').waitFor();
      assert.equal(await page.evaluate(() => document.documentElement.scrollWidth > innerWidth + 1), false, `QR panel overflows at ${width}`);
      const box = await page.locator('.whatsapp-pairing img').boundingBox(); assert.ok(box.width <= width - 36);
      await capture(`qr-${width}`);
    }
  });
  assert.deepEqual(external, [], 'No external network requests');
  assert.deepEqual(errors, [], 'No browser runtime errors');
  assert.equal(requests.some(request => /\/orders$/.test(request.endpoint) && request.method !== 'GET'), false, 'No orders created');
  process.stdout.write(`${checks} Evolution admin browser checks passed\n`);
}
run().catch(error => { process.stderr.write(`${error.stack}\n`); process.exitCode = 1; }).finally(async () => {
  if (browser) await browser.close();
  if (server) await new Promise(resolve => server.close(resolve));
});
