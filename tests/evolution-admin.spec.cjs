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
  last_webhook_at: '', messages: [], conversations: [],
  ai: { enabled: false, configured: false, provider: 'gemini', model: 'gemini-2.5-flash-lite', daily_limit: 50, used_today: 0, last_result: null, missing: ['SAHARA_AI_API_KEY'] }
};
let server, browser, context, page, baseURL, authorized = false;
let dashboard = structuredClone(fixture), dashboardWait, dashboardError, connectResult, connectError, connectWait, ordersWait, partialBody, partialResponse;
const requests = [], errors = [], external = [];
let checks = 0;

async function check(name, action) { await action(); checks++; process.stdout.write(`✓ ${name}\n`); }
async function render(waitWhatsApp = true) {
  await page.waitForFunction(() => document.getElementById('sidebar').inert === (window.matchMedia('(max-width: 800px)').matches && !document.body.classList.contains('sidebar-open')));
  if (await page.locator('#sidebar').evaluate(node => node.inert)) await page.locator('#menu-toggle').click();
  await page.locator('#navigation button[data-view="integrations"]').click();
  await page.locator('#screen[aria-busy="false"]').waitFor({ timeout: 1500 });
  if (waitWhatsApp) await page.locator('.whatsapp-section[aria-busy="false"]').waitFor();
  if (waitWhatsApp) assert.equal(await page.locator('#screen .danger-text').count(), 0, await page.locator('#screen').innerText());
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
      if (partialBody && request.url === '/api/admin/whatsapp') {
        partialResponse = response;
        response.writeHead(200, { 'Content-Type': 'application/json' });
        response.write('{"configured":');
        return; // Intentionally incomplete body: the browser must stop waiting.
      }
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
    if (endpoint === '/api/admin/orders') { if (ordersWait) await ordersWait; return reply({ orders: [] }); }
    if (endpoint === '/api/admin/drivers') return reply({ drivers: [] });
    if (endpoint === '/api/integrations') return reply({ integrations: [{ id: 'whatsapp', label: 'WhatsApp · atendimento e avisos de pedidos', available: dashboard.configured, reason: dashboard.configured ? 'Configuração presente. Confira a conexão.' : 'Configure a Evolution API no Render.' }, { id: 'campaigns', label: 'Campanhas', available: false, reason: 'Envio em massa não ativado.' }] });
    if (endpoint === '/api/admin/whatsapp') {
      if (partialBody) return route.continue();
      if (dashboardWait) await dashboardWait;
      if (dashboardError) return reply({ detail: dashboardError }, 503);
      return reply(dashboard);
    }
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
    await page.locator('.whatsapp-setup:not(.whatsapp-ai-setup) summary').click();
    assert.match(await page.locator('.whatsapp-setup:not(.whatsapp-ai-setup)').innerText(), /Instale a Evolution API/);
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
      await page.locator('.whatsapp-setup:not(.whatsapp-ai-setup) summary').click();
      await page.locator('.whatsapp-ai-setup summary').click();
      assert.equal(await page.evaluate(() => document.documentElement.scrollWidth > innerWidth + 1), false, `Pending setup overflows at ${width}`);
      dashboard = { ...dashboard, configured: true, enabled: true, connection: { state: 'close', connected: false, error: '' }, missing: [] };
      await render(); connectResult = { state: 'connecting', connected: false, qrcode: png }; await pair();
      await page.locator('.whatsapp-pairing img').waitFor();
      assert.equal(await page.evaluate(() => document.documentElement.scrollWidth > innerWidth + 1), false, `QR panel overflows at ${width}`);
      const box = await page.locator('.whatsapp-pairing img').boundingBox(); assert.ok(box.width <= width - 36);
      await capture(`qr-${width}`);
    }
  });
  await check('Delivery AI setup exposes guidance without secrets and never claims a verified provider connection', async () => {
    dashboard = structuredClone(fixture); await render();
    await page.getByText('IA para delivery: Aguardando ativação', { exact: true }).waitFor();
    await page.locator('.whatsapp-ai-setup summary').click();
    assert.match(await page.locator('.whatsapp-ai-setup').innerText(), /SAHARA_AI_API_KEY/);
    assert.match(await page.locator('.whatsapp-ai-setup').innerText(), /sem ativar cobrança/);
    assert.equal(await page.getByRole('link', { name: 'Criar chave no Google AI Studio' }).getAttribute('href'), 'https://aistudio.google.com/api-keys');
    assert.equal(await page.locator('#screen input[type="password"]').count(), 0);
    dashboard.ai = { ...dashboard.ai, enabled: true, configured: true, used_today: 50, last_result: { status: 'fallback', reason: 'limit' } };
    await render();
    await page.getByText('IA para delivery: Configuração presente', { exact: true }).waitFor();
    await page.getByText(/Consultas de IA hoje: 50 de 50/).waitFor();
    await page.getByText('Limite de IA atingido. O atendimento básico continua funcionando.', { exact: true }).waitFor();
    assert.equal(await page.locator('.whatsapp-ai-setup').count(), 0);
    dashboard.ai.last_result = { status: 'done', reason: '' }; await render();
    await page.getByText('Última consulta de IA: concluída.', { exact: true }).waitFor();
    dashboard = { ...structuredClone(fixture), configured: true, enabled: true, missing: [], connection: { state: 'close', connected: false, error: '' } };
    await render();
  });
  await check('A stalled WhatsApp check leaves other integrations visible and times out with a local retry', async () => {
    let release; dashboardWait = new Promise(resolve => { release = resolve; });
    const request = page.waitForRequest(request => request.url().endsWith('/api/admin/whatsapp'));
    try {
      await render(false); await request;
      await page.getByRole('heading', { name: 'Disponibilidade dos serviços', exact: true }).waitFor();
      await page.getByText('Consultando a conexão do WhatsApp…', { exact: true }).waitFor();
      await page.clock.fastForward(35001);
      await page.getByRole('heading', { name: 'Não foi possível carregar o WhatsApp', exact: true }).waitFor();
      await page.getByText('O servidor demorou para responder. Confira sua conexão e tente novamente.', { exact: true }).waitFor();
      assert.equal(await page.locator('.whatsapp-section').getAttribute('aria-busy'), 'false');
      assert.equal(requests.filter(request => request.endpoint === '/api/admin/whatsapp').at(-1).method, 'GET');
    } finally { dashboardWait = null; release(); }
    await page.getByRole('button', { name: 'Tentar novamente', exact: true }).click();
    await page.locator('.whatsapp-section[aria-busy="false"]').waitFor();
    await page.getByText('WhatsApp: Desconectado', { exact: true }).waitFor();
  });
  await check('A failed WhatsApp check preserves the integration list and can recover independently', async () => {
    dashboardError = 'A Evolution está indisponível neste momento.';
    await render(false);
    await page.getByRole('heading', { name: 'Não foi possível carregar o WhatsApp', exact: true }).waitFor();
    await page.getByRole('heading', { name: 'Disponibilidade dos serviços', exact: true }).waitFor();
    await page.getByText(dashboardError, { exact: true }).waitFor();
    dashboardError = null;
    await page.getByRole('button', { name: 'Tentar novamente', exact: true }).click();
    await page.locator('.whatsapp-section[aria-busy="false"]').waitFor();
    await page.getByText('WhatsApp: Desconectado', { exact: true }).waitFor();
  });
  await check('A pairing timeout releases the control and never repeats a mutation automatically', async () => {
    let release; connectWait = new Promise(resolve => { release = resolve; });
    const before = requests.filter(request => request.endpoint.endsWith('/whatsapp/connect')).length;
    const request = page.waitForRequest(request => request.url().endsWith('/api/admin/whatsapp/connect'));
    try {
      await page.getByRole('button', { name: 'Gerar QR Code', exact: true }).click(); await request;
      await page.clock.fastForward(100001);
      await page.locator('#notice').filter({ hasText: 'Não foi possível confirmar a operação a tempo. Confira o resultado antes de tentar novamente.' }).waitFor();
      await page.getByText('Conexão não confirmada.', { exact: true }).waitFor();
      assert.equal(await page.getByRole('button', { name: 'Gerar QR Code', exact: true }).isEnabled(), true);
      assert.equal(await page.locator('.whatsapp-pairing img').count(), 0);
      assert.equal(requests.filter(request => request.endpoint.endsWith('/whatsapp/connect')).length, before + 1);
    } finally { connectWait = null; release(); }
  });
  await check('The timeout also covers an incomplete JSON response body', async () => {
    partialBody = true;
    const response = page.waitForResponse(response => response.url().endsWith('/api/admin/whatsapp'));
    try {
      await render(false); await response;
      await page.clock.fastForward(35001);
      await page.getByText('O servidor demorou para responder. Confira sua conexão e tente novamente.', { exact: true }).waitFor();
      await page.getByRole('heading', { name: 'Disponibilidade dos serviços', exact: true }).waitFor();
    } finally { partialBody = false; partialResponse?.end('false}'); }
    await page.getByRole('button', { name: 'Tentar novamente', exact: true }).click();
    await page.locator('.whatsapp-section[aria-busy="false"]').waitFor();
  });
  await check('A late WhatsApp response cannot replace a different screen', async () => {
    let release; dashboardWait = new Promise(resolve => { release = resolve; });
    const request = page.waitForRequest(request => request.url().endsWith('/api/admin/whatsapp'));
    await render(false); await request;
    await page.locator('#navigation button[data-view="orders"]').click();
    await page.locator('#screen[aria-busy="false"]').waitFor();
    const response = page.waitForResponse(response => response.url().endsWith('/api/admin/whatsapp'));
    dashboardWait = null; release(); await response;
    // Let the response body and detached loader complete before inspecting the view.
    await page.evaluate(() => new Promise(resolve => setTimeout(resolve, 0)));
    assert.equal(await page.locator('#screen').getAttribute('data-view'), 'orders');
    assert.equal(await page.locator('#screen-title').innerText(), 'Pedidos e cozinha');
    assert.equal(await page.locator('.whatsapp-section').count(), 0);
  });
  await check('A stalled orders request ends with a usable retry instead of endless loading', async () => {
    let release; ordersWait = new Promise(resolve => { release = resolve; });
    const before = requests.filter(request => request.endpoint === '/api/admin/orders').length;
    const request = page.waitForRequest(request => request.url().endsWith('/api/admin/orders'));
    try {
      await page.locator('#navigation button[data-view="orders"]').click(); await request;
      await page.clock.fastForward(20001);
      await page.locator('#screen[aria-busy="false"]').waitFor();
      await page.getByRole('heading', { name: 'Não foi possível carregar os dados', exact: true }).waitFor();
      await page.getByText('O servidor demorou para responder. Confira sua conexão e tente novamente.', { exact: true }).waitFor();
      assert.equal(requests.filter(request => request.endpoint === '/api/admin/orders').length, before + 1);
    } finally { ordersWait = null; release(); }
    await page.getByRole('button', { name: 'Tentar novamente', exact: true }).click();
    await page.locator('#screen[aria-busy="false"]').waitFor();
    assert.equal(await page.locator('#screen .danger-text').count(), 0);
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
