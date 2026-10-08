'use strict';

// Run: node tests/admin.spec.cjs
// Real backend and Chromium, disposable private SQLite. No external integrations.
const assert = require('node:assert/strict');
const { spawn } = require('node:child_process');
const { once } = require('node:events');
const { randomBytes } = require('node:crypto');
const { mkdtemp, mkdir, rm } = require('node:fs/promises');
const net = require('node:net');
const os = require('node:os');
const path = require('node:path');
const { chromium } = require('playwright');

const root = path.resolve(__dirname, '..');
let backend, browser, directory, baseURL, page;
const password = randomBytes(24).toString('hex');
let csrf, order, customer, flour;
const errors = [];
let checks = 0;

async function waitFor(predicate, message) {
  const until = Date.now() + 6000;
  while (Date.now() < until) {
    if (await predicate()) return;
    await new Promise(resolve => setTimeout(resolve, 50));
  }
  throw new Error(message);
}

async function start() {
  directory = await mkdtemp(path.join(os.tmpdir(), 'sahara-admin-'));
  const probe = net.createServer(); probe.listen(0, '127.0.0.1'); await once(probe, 'listening');
  const port = probe.address().port; await new Promise(resolve => probe.close(resolve));
  const script = 'import os; from server.core import hash_password; os.environ["SAHARA_ADMIN_PASSWORD_HASH"]=hash_password(os.environ.pop("SAHARA_TEST_PASSWORD")); import uvicorn; uvicorn.run("server.main:app",host="127.0.0.1",port=' + port + ',log_level="warning")';
  backend = spawn(process.env.PYTHON || 'python3', ['-u', '-c', script], {
    cwd: root, env: { ...process.env, SAHARA_DATA_DIR: directory, SAHARA_COOKIE_SECURE: '0', SAHARA_TEST_PASSWORD: password }, stdio: ['ignore', 'pipe', 'pipe'],
  });
  let output = ''; backend.stdout.on('data', data => { output += data; }); backend.stderr.on('data', data => { output += data; });
  baseURL = `http://127.0.0.1:${port}`;
  await waitFor(async () => {
    if (backend.exitCode !== null) throw new Error(`Backend stopped: ${output}`);
    try { return (await fetch(`${baseURL}/api/health`)).ok; } catch { return false; }
  }, `Backend did not start: ${output}`);
  browser = await chromium.launch({ executablePath: '/usr/bin/chromium', headless: true, args: ['--no-sandbox'] });
  const context = await browser.newContext({ viewport: { width: 1280, height: 900 }, serviceWorkers: 'block', timezoneId: 'America/Sao_Paulo' });
  await context.route('**/*', route => new URL(route.request().url()).origin === baseURL ? route.continue() : route.abort());
  page = await context.newPage(); page.on('pageerror', error => errors.push(error.message));
  page.on('dialog', dialog => dialog.accept());
  await page.addInitScript(() => { window.__printCalls = 0; window.print = () => { window.__printCalls++; }; });
  await page.goto(`${baseURL}/admin.html`);
}

async function api(route, options = {}) {
  const response = await page.request.fetch(`${baseURL}/api${route}`, {
    method: options.method || 'GET', data: options.data,
    headers: { 'X-Sahara-CSRF': csrf || '', ...options.headers },
  });
  const data = await response.json();
  assert.equal(response.status(), options.status || 200, `${route}: ${JSON.stringify(data)}`);
  return data;
}

async function view(key) {
  await page.waitForFunction(() => {
    const mobile = window.matchMedia('(max-width: 800px)').matches;
    const open = document.body.classList.contains('sidebar-open');
    return document.getElementById('sidebar').inert === (mobile && !open);
  });
  if (await page.locator('#sidebar').evaluate(node => node.inert)) await page.locator('#menu-toggle').click();
  await page.locator(`#navigation button[data-view="${key}"]`).click();
  await page.locator('#screen[aria-busy="false"]').waitFor();
  assert.equal(await page.locator('#screen .danger-text').count(), 0, await page.locator('#screen').innerText());
}

async function submit(label, route, method = 'POST') {
  const response = page.waitForResponse(response => response.url().endsWith(`/api${route}`) && response.request().method() === method);
  await page.getByRole('button', { name: label, exact: true }).click();
  const result = await response;
  assert.ok(result.ok(), `${route}: ${await result.text()}`);
  await page.locator('#screen[aria-busy="false"]').waitFor();
  return result.json();
}

async function capture(name) {
  if (!process.env.SAHARA_LAYOUT_CAPTURE_DIR) return;
  const target = path.resolve(process.env.SAHARA_LAYOUT_CAPTURE_DIR);
  await mkdir(target, { recursive: true });
  await page.evaluate(async () => {
    await new Promise(resolve => requestAnimationFrame(() => requestAnimationFrame(resolve)));
    await Promise.all(document.getAnimations().map(animation => animation.finished.catch(() => {})));
  });
  await page.screenshot({ path: path.join(target, `${name}.png`), fullPage: false });
}

async function check(name, action) { await action(); checks++; process.stdout.write(`✓ ${name}\n`); }

async function run() {
  await start();
  await check('Restricted access, real login and ephemeral session', async () => {
    await page.locator('#login-panel:not([hidden])').waitFor();
    await capture('login-desktop');
    await api('/admin/orders', { status: 401 });
    await page.locator('#admin-password').fill('incorrect-test-password');
    const denied = page.waitForResponse(response => response.url().endsWith('/api/admin/login'));
    await page.getByRole('button', { name: 'Entrar no painel', exact: true }).click();
    assert.equal((await denied).status(), 401);
    await page.locator('#notice').filter({ hasText: 'Senha incorreta' }).waitFor();
    await page.locator('#admin-password').fill(password);
    await submit('Entrar no painel', '/admin/login');
    await page.locator('#workspace:not([hidden])').waitFor();
    assert.equal(await page.locator('#admin-password').inputValue(), '');
    const storage = await page.evaluate(() => [localStorage.length, sessionStorage.length]);
    assert.deepEqual(storage, [0, 0]);
    csrf = (await api('/admin/session')).csrf_token;
    await api('/admin/cash/sessions', { method: 'POST', data: { action: 'open' }, headers: { 'X-Sahara-CSRF': '' }, status: 403 });
  });
  await check('Coupon rules include per-customer usage limit', async () => {
    await view('coupons');
    await page.locator('#field-coupons-code').fill('TESTE10');
    await page.locator('#field-coupons-value').fill('10');
    await page.locator('#field-coupons-usage_limit').fill('5');
    await page.locator('#field-coupons-per_customer_limit').fill('2');
    await submit('Criar cupom', '/admin/coupons');
    const coupon = (await api('/admin/coupons')).coupons[0];
    assert.equal(coupon.value, 10); assert.equal(coupon.per_customer_limit, 2);
  });
  await check('Inventory adjustments and real product recipe', async () => {
    await view('inventory');
    await page.locator('#field-inventory-label').fill('Esfiha de carne pronta');
    await page.locator('#field-inventory-on_hand').fill('20');
    await page.locator('#field-inventory-product_id').selectOption('carne');
    await submit('Cadastrar item', '/admin/inventory');
    const direct = (await api('/admin/inventory')).items[0];
    await page.locator('#field-inventory-quantity').fill('5'); await page.locator('#field-inventory-reason').fill('Produção do dia');
    await submit('Registrar movimentação', `/admin/inventory/${direct.id}/adjust`);
    await page.locator('#field-inventory-label').fill('Farinha');
    await page.locator('#field-inventory-unit').selectOption('g'); await page.locator('#field-inventory-on_hand').fill('1000');
    await submit('Cadastrar item', '/admin/inventory');
    flour = (await api('/admin/inventory')).items.find(item => item.label === 'Farinha');
    await page.locator('#field-inventory-recipe_product_id').selectOption('carne');
    await page.locator(`[id="field-inventory-recipe-${flour.id}"]`).fill('10');
    await submit('Salvar ficha técnica', '/admin/recipes');
    const recipe = (await api('/admin/recipes')).recipes.find(row => row.product_id === 'carne');
    assert.equal(recipe.components[0].quantity, 10);
    assert.equal((await api('/admin/inventory')).items.find(row => row.id === direct.id).on_hand, 25);
  });
  await check('Cash opening and movement use integer cents', async () => {
    await view('cash'); await page.locator('#field-cash-amount').fill('50');
    await submit('Abrir caixa', '/admin/cash/sessions');
    const entry = page.locator('form').filter({ has: page.getByRole('button', { name: 'Registrar no caixa', exact: true }) });
    await entry.locator('[name="kind"]').selectOption('supply'); await entry.locator('[name="amount"]').fill('10'); await entry.locator('[name="description"]').fill('Troco');
    await submit('Registrar no caixa', '/admin/cash/entries');
    assert.equal((await api('/admin/cash/sessions')).active_session.balance_cents, 6000);
  });
  await check('Driver can be registered before assigning the delivery', async () => {
    await view('drivers'); await page.locator('#field-drivers-name').fill('Entregador teste'); await page.locator('#field-drivers-phone').fill('44999992222'); await submit('Cadastrar entregador', '/admin/drivers');
    assert.equal((await api('/admin/drivers')).drivers.length, 1);
  });
  await check('Product search and category preserve client, quantities and calculated summary', async () => {
    await view('pos');
    const catalog = (await api('/catalog')).products;
    const carne = page.locator(`#product-${catalog.findIndex(product => product.id === 'carne')}`);
    const coca = page.locator(`#product-${catalog.findIndex(product => product.id === 'coca-350')}`);
    await page.locator('#field-pos-name').fill('Rascunho do cliente'); await carne.fill('2');
    await page.getByRole('searchbox', { name: 'Buscar produtos', exact: true }).fill('Coca');
    await page.getByRole('combobox', { name: 'Filtrar produtos por categoria', exact: true }).selectOption('Bebidas');
    assert.equal(await carne.isVisible(), false); assert.equal(await carne.inputValue(), '2');
    await coca.fill('1');
    assert.match(await page.locator('.pos-customer .product-summary').innerText(), /14,00/);
    assert.match(await page.locator('.selected-items').innerText(), /2× Carne/);
    assert.match(await page.locator('.selected-items').innerText(), /1× Coca-Cola lata 350 ml/);
    await page.getByRole('searchbox', { name: 'Buscar produtos', exact: true }).fill('cArNe');
    await page.getByRole('combobox', { name: 'Filtrar produtos por categoria', exact: true }).selectOption('Esfihas tradicionais');
    assert.equal(await carne.isVisible(), true); assert.equal(await coca.isVisible(), false);
    assert.equal(await page.locator('#field-pos-name').inputValue(), 'Rascunho do cliente');
    assert.match(await page.locator('.pos-customer .product-summary').innerText(), /14,00/);
    assert.equal((await api('/admin/orders')).orders.length, 0);
  });
  await check('Empty product search preserves selected items and can be cleared', async () => {
    const search = page.getByRole('searchbox', { name: 'Buscar produtos', exact: true });
    await search.fill('zzzz-sem-produto');
    assert.equal(await page.locator('.product-entry:visible').count(), 0);
    assert.equal(await page.getByText('Nenhum produto encontrado. Tente outra busca ou categoria.', { exact: true }).isVisible(), true);
    assert.match(await page.locator('.pos-customer .product-summary').innerText(), /14,00/);
    assert.equal(await page.locator('#field-pos-name').inputValue(), 'Rascunho do cliente');
    await search.fill(''); await page.getByRole('combobox', { name: 'Filtrar produtos por categoria', exact: true }).selectOption('');
    assert.ok(await page.locator('.product-entry:visible').count() > 10);
    assert.equal(await page.getByText('Nenhum produto encontrado. Tente outra busca ou categoria.', { exact: true }).isVisible(), false);
  });
  await check('Collapsed required sections open before native validation focuses the field', async () => {
    await view('pos');
    const catalog = (await api('/catalog')).products;
    await page.locator(`#product-${catalog.findIndex(product => product.id === 'carne')}`).fill('1');
    await page.locator('.pos-section').evaluateAll(sections => sections.forEach(section => { section.open = false; }));
    await page.getByRole('button', { name: 'Registrar pedido', exact: true }).click();
    assert.equal(await page.locator('#field-pos-name').evaluate(field => field.closest('details').open), true);
    assert.equal(await page.evaluate(() => document.activeElement.id), 'field-pos-name');
    assert.equal((await api('/admin/orders')).orders.length, 0);
    await page.locator('#field-pos-name').fill('Cliente em rascunho'); await page.locator('#field-pos-phone').fill('44999994444');
    await page.locator('#field-pos-street').evaluate(field => { field.closest('details').open = false; });
    await page.getByRole('button', { name: 'Registrar pedido', exact: true }).click();
    assert.equal(await page.locator('#field-pos-street').evaluate(field => field.closest('details').open), true);
    assert.equal(await page.evaluate(() => document.activeElement.id), 'field-pos-street');
    assert.equal((await api('/admin/orders')).orders.length, 0);
  });
  await check('A filtered invalid quantity reappears for native validation without creating an order', async () => {
    await view('pos');
    const catalog = (await api('/catalog')).products;
    const invalid = page.locator(`#product-${catalog.findIndex(product => product.id === 'carne')}`);
    for (const [field, value] of Object.entries({ name: 'Cliente em rascunho', phone: '44999994444', street: 'Rua de teste', number: '10', neighborhood: 'Centro' })) await page.locator(`#field-pos-${field}`).fill(value);
    await invalid.fill('-1');
    await page.getByRole('combobox', { name: 'Filtrar produtos por categoria', exact: true }).selectOption('Bebidas');
    assert.equal(await invalid.isVisible(), false);
    await page.getByRole('button', { name: 'Registrar pedido', exact: true }).click();
    assert.equal(await invalid.isVisible(), true);
    assert.equal(await page.getByRole('combobox', { name: 'Filtrar produtos por categoria', exact: true }).inputValue(), '');
    assert.equal(await page.evaluate(() => document.activeElement.id), await invalid.getAttribute('id'));
    assert.equal((await api('/admin/orders')).orders.length, 0);
  });
  await check('PDV uses catalog prices and server coupon total', async () => {
    await view('pos');
    for (const [field, value] of Object.entries({ name: 'Cliente teste', phone: '44999991111', street: 'Rua de teste', number: '10', neighborhood: 'Centro', coupon_code: 'TESTE10' })) await page.locator(`#field-pos-${field}`).fill(value);
    const catalog = (await api('/catalog')).products;
    const index = catalog.findIndex(product => product.id === 'carne'); assert.ok(index >= 0);
    await page.locator(`#product-${index}`).fill('2');
    await page.evaluate(() => window.scrollTo(0, 0));
    await page.locator('.pos-details').evaluate(node => { node.scrollTop = 0; });
    const submitBox = await page.getByRole('button', { name: 'Registrar pedido', exact: true }).boundingBox();
    assert.ok(submitBox && submitBox.y + submitBox.height <= 900, 'Desktop submit stays visible beside the product catalog');
    await capture('pdv-desktop');
    const receipt = await submit('Registrar pedido', '/admin/orders');
    assert.equal(receipt.total_cents, 720);
    order = (await api('/admin/orders')).orders[0]; assert.equal(order.source, 'pdv'); assert.equal(order.items[0].price_cents, 400);
    assert.match(await page.locator('.order-total').innerText(), /7,20/);
    assert.equal(order.status, 'preparing');
    assert.equal(await page.locator('.order-progress .order-stage').count(), 4);
    await capture('orders-desktop');
    customer = (await api('/admin/customers')).customers.find(row => row.phone === '44999991111');
    assert.equal(customer.points, 0);
  });
  await check('Order search filters client, phone and short ID locally while preserving full identifiers', async () => {
    const requests = [];
    const capture = request => { if (new URL(request.url()).pathname.startsWith('/api/')) requests.push(request.url()); };
    page.on('request', capture);
    try {
      const heading = page.locator('.order-card h2');
      assert.match(await heading.innerText(), new RegExp(order.id.slice(0, 8).toUpperCase()));
      assert.ok([await heading.getAttribute('title'), await heading.getAttribute('aria-label')].some(value => value?.includes(order.id)));
      const overview = label => page.locator('.overview-card').filter({ has: page.getByText(label, { exact: true }) }).locator('.overview-value');
      assert.equal(await overview('Em preparo na cozinha').innerText(), '1');
      assert.equal(await overview('Pedido pronto').innerText(), '0');
      const search = page.getByRole('searchbox', { name: 'Buscar pedidos', exact: true });
      for (const query of ['Cliente teste', '44999991111', order.id.slice(0, 8)]) {
        await search.fill(query); assert.equal(await page.locator('.order-card:visible').count(), 1);
      }
      await search.fill('zzzz-sem-pedido'); assert.equal(await page.locator('.order-card:visible').count(), 0);
      assert.equal(await page.locator('.empty-state').isVisible(), true);
      await search.fill(''); assert.equal(await page.locator('.order-card:visible').count(), 1);
      assert.deepEqual(requests, [], 'Searching existing orders must not send API requests');
    } finally { page.off('request', capture); }
  });
  await check('Preparation consumes ingredients once, ready stage and print use real order', async () => {
    assert.equal((await api('/admin/orders')).orders[0].status, 'preparing');
    assert.equal(await page.locator('.order-progress [aria-current="step"] .stage-label').innerText(), 'Em preparo na cozinha');
    await submit('Atualizar situação', `/admin/orders/${order.id}`, 'PATCH');
    assert.equal((await api('/admin/orders')).orders[0].status, 'ready');
    assert.equal(await page.locator('.order-progress [aria-current="step"] .stage-label').innerText(), 'Pedido pronto');
    assert.equal((await api('/admin/inventory')).items.find(row => row.id === flour.id).on_hand, 980);
    await page.getByRole('button', { name: 'Imprimir comanda', exact: true }).click();
    assert.equal(await page.evaluate(() => window.__printCalls), 1); assert.match(await page.locator('#print-target').textContent(), /Cliente teste/);
    const driver = (await api('/admin/drivers')).drivers[0];
    await page.getByRole('combobox', { name: `Entregador do pedido ${order.id}`, exact: true }).selectOption(driver.id);
    await submit('Atribuir entrega', `/admin/orders/${order.id}/driver`, 'PATCH');
    assert.equal((await api('/admin/orders')).orders[0].courier_id, driver.id);
    const delivery = (await api('/admin/drivers')).drivers[0].orders[0]; assert.equal(delivery.id, order.id); assert.match(delivery.maps_url, /^https:\/\/www\.google\.com\/maps\/dir\//);
  });
  await check('Fiado links to order and partial receipt credits cash once', async () => {
    await view('receivables'); await page.locator('#field-receivables-order_id').selectOption(order.id);
    assert.equal(await page.locator('#field-receivables-amount').first().inputValue(), '7.20');
    await submit('Registrar valor a receber', '/admin/receivables');
    const receivable = (await api('/admin/receivables')).receivables[0]; assert.equal(receivable.order_id, order.id);
    const form = page.locator('form').filter({ has: page.getByRole('button', { name: 'Registrar pagamento recebido', exact: true }) });
    await form.locator('[name="amount"]').fill('2');
    await submit('Registrar pagamento recebido', `/admin/receivables/${receivable.id}/payments`);
    assert.equal((await api('/admin/receivables')).receivables[0].balance_cents, 520);
    assert.equal((await api('/admin/cash/sessions')).active_session.balance_cents, 6200);
  });
  await check('Partial receipts prevent cancellation and delivery updates remain real', async () => {
    await view('orders');
    const choice = page.getByRole('combobox', { name: `Próxima situação do pedido ${order.id}`, exact: true });
    await choice.selectOption('cancelled');
    const response = page.waitForResponse(response => response.url().endsWith(`/api/admin/orders/${order.id}`) && response.request().method() === 'PATCH');
    await page.getByRole('button', { name: 'Atualizar situação', exact: true }).click();
    assert.equal((await response).status(), 409);
    for (const status of ['out_for_delivery', 'delivered']) {
      await page.getByRole('combobox', { name: `Próxima situação do pedido ${order.id}`, exact: true }).selectOption(status);
      await submit('Atualizar situação', `/admin/orders/${order.id}`, 'PATCH');
      if (status === 'delivered') await page.getByRole('combobox', { name: 'Filtrar pedidos por situação', exact: true }).selectOption('all');
      await page.locator('#screen[aria-busy="false"]').waitFor();
      const currentLabel = status === 'delivered' ? 'Entregue' : 'A caminho do endereço';
      assert.equal(await page.locator('.order-progress [aria-current="step"] .stage-label').innerText(), currentLabel);
    }
    assert.equal((await api('/admin/orders')).orders[0].status, 'delivered');
    assert.equal((await api('/admin/customers')).customers.find(row => row.id === customer.id).points, 0);
  });
  await check('Full fiado settlement awards loyalty and avoids duplicate sale', async () => {
    await view('receivables'); const receivable = (await api('/admin/receivables')).receivables[0];
    const form = page.locator('form').filter({ has: page.getByRole('button', { name: 'Registrar pagamento recebido', exact: true }) });
    await form.locator('[name="amount"]').fill('5.20');
    await submit('Registrar pagamento recebido', `/admin/receivables/${receivable.id}/payments`);
    assert.equal((await api('/admin/orders')).orders[0].payment_status, 'paid');
    assert.equal((await api('/admin/customers')).customers.find(row => row.id === customer.id).points, 7);
    assert.equal((await api('/admin/cash/sessions')).active_session.balance_cents, 6720);
  });
  await check('CRM consent and RFV derive from delivered paid orders', async () => {
    await view('customers');
    await page.locator('#field-customers-edit_id').selectOption(customer.id); await page.locator('#field-customers-edit_marketing_opt_in').check();
    await submit('Salvar cliente', `/admin/customers/${customer.id}`, 'PATCH');
    const rfv = (await api('/admin/rfv')).customers.find(row => row.id === customer.id); assert.equal(rfv.frequency, 1); assert.equal(rfv.monetary_cents, 720);
    assert.equal((await api('/admin/customers')).customers.find(row => row.id === customer.id).marketing_opt_in, true);
  });
  await check('Real reward redemption subtracts points', async () => {
    await view('loyalty'); await page.locator('#field-loyalty-name').fill('Benefício de teste'); await page.locator('#field-loyalty-points_cost').fill('2');
    await submit('Cadastrar benefício', '/admin/loyalty/rewards');
    await page.locator('#field-loyalty-customer_id').selectOption(customer.id); await submit('Registrar resgate', '/admin/loyalty/redeem');
    assert.equal((await api('/admin/customers')).customers.find(row => row.id === customer.id).points, 5);
  });
  await check('Campaign is a saved consent-based draft with no send action', async () => {
    await view('campaigns'); await page.locator('#field-campaigns-name').fill('Campanha de teste'); await page.locator('#field-campaigns-message').fill('Mensagem preparada para envio futuro.');
    const data = await submit('Salvar rascunho', '/admin/campaigns'); assert.equal(data.sending_available, false); assert.equal(data.campaign.status, 'draft'); assert.deepEqual(data.campaign.customer_ids, [customer.id]);
    assert.equal(await page.getByRole('button', { name: /Enviar campanha/ }).count(), 0);
  });
  await check('Unavailable integrations and empty reviews load truthfully', async () => {
    await view('reviews'); assert.match(await page.locator('#screen').innerText(), /Nenhuma avaliação/);
    await view('integrations'); assert.ok(await page.locator('.badge').filter({ hasText: 'Ativação pendente' }).count() >= 8); assert.equal(await page.locator('.badge').filter({ hasText: 'Disponível' }).count(), 0);
  });
  await check('Dashboard reflects real totals, cash closes and mobile does not overflow', async () => {
    await view('dashboard'); const dashboard = await api('/admin/dashboard'); assert.equal(dashboard.sales_cents, 720); assert.equal(dashboard.orders_count, 1); assert.equal(dashboard.conversion_rate, null);
    await view('cash'); const closeForm = page.locator('form').filter({ has: page.getByRole('button', { name: 'Fechar caixa', exact: true }) }); await closeForm.locator('[name="amount"]').fill('67.20'); await submit('Fechar caixa', '/admin/cash/sessions');
    assert.equal((await api('/admin/cash/sessions')).active_session, null);
    await page.setViewportSize({ width: 390, height: 844 }); await view('integrations');
    assert.equal(await page.evaluate(() => document.documentElement.scrollWidth <= window.innerWidth), true);
    const ids = await page.locator('[id]').evaluateAll(nodes => nodes.map(node => node.id)); assert.equal(new Set(ids).size, ids.length);
    if (process.env.SAHARA_TEST_SCREENSHOT) await page.screenshot({ path: process.env.SAHARA_TEST_SCREENSHOT, fullPage: true });
  });
  await check('Mobile drawer closes with Escape, overlay and navigation while restoring focus', async () => {
    for (const width of [390, 800]) {
      await page.setViewportSize({ width, height: 844 });
      if (width === 390) { await view('orders'); await capture('orders-mobile'); }
      await page.locator('#menu-toggle').click();
      assert.equal(await page.locator('#menu-toggle').getAttribute('aria-expanded'), 'true');
      assert.equal(await page.locator('#sidebar-overlay').isVisible(), true);
      assert.equal(await page.locator('#content').evaluate(node => node.inert), true);
      if (width === 390) await capture('drawer-mobile');
      assert.equal(await page.evaluate(() => document.activeElement.id), 'menu-close');
      const firstLink = page.locator('#sidebar a').first();
      await firstLink.focus(); await page.keyboard.press('Shift+Tab');
      assert.equal(await page.evaluate(() => document.activeElement.dataset.view), 'integrations');
      await page.keyboard.press('Tab');
      assert.equal(await firstLink.evaluate(node => document.activeElement === node), true);
      await page.keyboard.press('Escape');
      assert.equal(await page.locator('#menu-toggle').getAttribute('aria-expanded'), 'false');
      assert.equal(await page.locator('#sidebar-overlay').isVisible(), false);
      assert.equal(await page.locator('#sidebar').evaluate(node => node.inert), true);
      assert.equal(await page.evaluate(() => document.activeElement.id), 'menu-toggle');
      await page.locator('#menu-toggle').click();
      await page.mouse.click(width - 12, 150);
      assert.equal(await page.locator('#menu-toggle').getAttribute('aria-expanded'), 'false');
      assert.equal(await page.evaluate(() => document.activeElement.id), 'menu-toggle');
      await view('dashboard');
      assert.equal(await page.locator('#menu-toggle').getAttribute('aria-expanded'), 'false');
      assert.equal(await page.evaluate(() => document.activeElement.id), 'content');
      assert.equal(await page.locator('#navigation [data-view="dashboard"]').getAttribute('aria-current'), 'page');
      assert.equal(await page.evaluate(() => document.documentElement.scrollWidth <= window.innerWidth), true, `Body overflow at ${width}px`);
    }
    await page.setViewportSize({ width: 1280, height: 900 });
    await page.waitForFunction(() => !document.getElementById('sidebar').inert);
    assert.equal(await page.locator('#sidebar').evaluate(node => node.inert), false);
    assert.equal(await page.locator('#sidebar-overlay').isVisible(), false);
  });
  await check('Every management module loads without document overflow at 390px and 800px', async () => {
    const modules = ['orders', 'pos', 'dashboard', 'inventory', 'cash', 'receivables', 'customers', 'coupons', 'loyalty', 'campaigns', 'drivers', 'reviews', 'integrations'];
    for (const width of [390, 800]) {
      await page.setViewportSize({ width, height: 844 });
      for (const module of modules) {
        await view(module);
        if (width === 390 && module === 'pos') {
          const catalog = (await api('/catalog')).products;
          await page.locator(`#product-${catalog.findIndex(product => product.id === 'carne')}`).fill('2');
          await page.evaluate(() => window.scrollTo(0, 0));
          await page.locator('.product-list').evaluate(node => { node.scrollTop = 0; });
          await capture('pdv-mobile');
          await page.locator('.pos-customer').scrollIntoViewIfNeeded();
          await capture('pdv-mobile-details');
        }
        assert.equal(await page.evaluate(() => document.documentElement.scrollWidth <= window.innerWidth), true, `${module}: body overflow at ${width}px`);
        const ids = await page.locator('[id]').evaluateAll(nodes => nodes.map(node => node.id));
        assert.equal(new Set(ids).size, ids.length, `${module}: duplicate field IDs`);
      }
    }
    await page.setViewportSize({ width: 1280, height: 900 });
  });
  await check('Public menu registers one real order and retries without duplicate or WhatsApp send', async () => {
    const context = await browser.newContext({ viewport: { width: 390, height: 844 }, serviceWorkers: 'block', timezoneId: 'America/Sao_Paulo' });
    await context.route('**/*', route => new URL(route.request().url()).origin === baseURL ? route.continue() : route.abort());
    const client = await context.newPage(); client.on('pageerror', error => errors.push(error.message));
    await client.addInitScript(() => {
      window.__whatsappUrls = [];
      window.open = () => ({ opener: null, location: { replace(url) { window.__whatsappUrls.push(url); } }, close() {} });
    });
    try {
      const before = (await api('/admin/orders')).orders;
      await client.goto(`${baseURL}/index.html`);
      assert.equal(await client.evaluate(() => window.SaharaSystemConfig.apiBase), '/api');
      await client.locator('#products [data-change="carne"][data-delta="1"]').click();
      for (const [field, value] of Object.entries({ street: 'Rua do cliente', 'house-number': '22', neighborhood: 'Centro', 'customer-name': 'Cliente do cardápio', 'customer-phone': '44999993333' })) await client.locator(`#${field}`).fill(value);
      await client.locator('input[name="payment"][value="Pix"]').check();
      const response = client.waitForResponse(response => response.url().endsWith('/api/orders') && response.request().method() === 'POST');
      await client.locator('#checkout').click();
      const received = await response; assert.equal(received.status(), 201); const registered = await received.json(); assert.equal(registered.total_cents, 400);
      await client.locator('#checkout[aria-busy="false"]').waitFor();
      assert.match(await client.locator('#order-status').innerText(), new RegExp(registered.id));
      assert.match(await client.locator('#order-status').innerText(), /4,00/);
      const after = (await api('/admin/orders')).orders; assert.equal(after.length, before.length + 1);
      const actual = after.find(row => row.id === registered.id); assert.ok(actual); assert.equal(actual.source, 'web'); assert.equal(actual.payment_status, 'unpaid'); assert.equal(actual.items[0].price_cents, 400); assert.equal(actual.customer_name, 'Cliente do cardápio'); assert.equal(actual.delivery.number, '22');
      await client.locator('#checkout').click(); await client.locator('#checkout[aria-busy="false"]').waitFor();
      assert.equal((await api('/admin/orders')).orders.length, after.length);
      const opened = await client.evaluate(() => window.__whatsappUrls); assert.equal(opened.length, 2); assert.match(opened[0], /^https:\/\/wa\.me\/5544991748318\?text=/);
      assert.match(decodeURIComponent(opened[0]), new RegExp(registered.id));
      assert.equal(await client.locator('#order-whatsapp').isVisible(), true);
    } finally { await context.close(); }
  });
  await check('Logout destroys access and static hosting displays activation gate', async () => {
    await page.locator('#logout').click(); await page.locator('#login-panel:not([hidden])').waitFor(); await api('/admin/orders', { status: 401 });
    await page.route('**/api/health', route => route.fulfill({ status: 404, contentType: 'text/html', body: 'Static hosting' }));
    await page.reload(); await page.getByRole('heading', { name: 'Ativação da gestão pendente', exact: true }).waitFor();
    assert.match(await page.locator('#activation-message').innerText(), /GitHub Pages/); assert.equal(await page.locator('#workspace').isVisible(), false);
    assert.deepEqual(errors, []);
  });
  process.stdout.write(`${checks} admin browser checks passed.\n`);
}

async function cleanup() {
  if (browser) await browser.close();
  if (backend && backend.exitCode === null) { const stopped = once(backend, 'exit'); backend.kill('SIGTERM'); await stopped; }
  if (directory) await rm(directory, { recursive: true, force: true });
}

run().catch(error => { console.error(error); process.exitCode = 1; }).finally(cleanup);
