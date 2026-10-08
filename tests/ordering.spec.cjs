'use strict';

// Run with: node tests/ordering.spec.cjs
// Uses the real menu with simulated time/API responses; sends no orders or messages.
const assert = require('node:assert/strict');
const { spawn } = require('node:child_process');
const { once } = require('node:events');
const net = require('node:net');
const path = require('node:path');
const { chromium } = require('playwright');

const root = path.resolve(__dirname, '..');
const frozenTime = '2026-10-04T20:00:00Z'; // Sunday, 17:00 in Maringá.
let browser;
let server;
let baseURL;

async function startServer() {
  const probe = net.createServer();
  probe.listen(0, '127.0.0.1');
  await once(probe, 'listening');
  const port = probe.address().port;
  await new Promise(resolve => probe.close(resolve));
  server = spawn(process.env.PYTHON || 'python3', [
    '-u', '-m', 'http.server', String(port), '--bind', '127.0.0.1',
  ], { cwd: root, stdio: ['ignore', 'pipe', 'pipe'] });
  let output = '';
  server.stdout.on('data', data => { output += data; });
  server.stderr.on('data', data => { output += data; });
  server.on('error', error => { output += error.message; });
  baseURL = `http://127.0.0.1:${port}`;
  for (let attempt = 0; attempt < 100; attempt++) {
    if (server.exitCode !== null) throw new Error(`Local server stopped: ${output}`);
    try {
      const response = await fetch(`${baseURL}/index.html`);
      if (response.ok) return;
    } catch {}
    await new Promise(resolve => setTimeout(resolve, 50));
  }
  throw new Error(`Local server did not start: ${output}`);
}

async function newPage(options = {}) {
  const context = await browser.newContext({
    viewport: { width: 390, height: 844 },
    reducedMotion: 'reduce',
    serviceWorkers: 'block',
    timezoneId: options.timezoneId || 'Pacific/Auckland',
  });
  await context.route('**/*', route => {
    const url = new URL(route.request().url());
    return url.origin === baseURL ? route.continue() : route.abort();
  });
  const page = await context.newPage();
  const errors = [];
  page.on('pageerror', error => errors.push(error.message));
  await page.clock.install({ time: new Date(frozenTime) });
  await page.addInitScript(() => {
    localStorage.setItem('sahara-cart', JSON.stringify([['carne', 1]]));
    window.__opened = [];
    window.__navigated = [];
    window.__closed = 0;
    window.__popup = {
      location: { replace(url) { window.__navigated.push(url); } },
      close() { window.__closed++; },
      closed: false,
    };
    window.open = (...args) => {
      window.__opened.push(args);
      return window.__popup;
    };
  });
  await context.route('**/system-config.js*', route => route.fulfill({ status: 200, contentType: 'application/javascript', body: `window.SaharaSystemConfig = { apiBase: ${JSON.stringify(options.apiBase || '')} };` }));
  await page.goto(`${baseURL}/index.html`, { waitUntil: 'load' });
  if (!await page.evaluate(() => Boolean(window.saharaOrdering))) {
    await page.addScriptTag({ url: `${baseURL}/ordering.js` });
  }
  if (!await page.locator('link[href*="ordering.css"]').count()) {
    await page.addStyleTag({ url: `${baseURL}/ordering.css` });
  }
  await page.waitForFunction(() => Boolean(window.saharaOrdering && window.saharaCheckout));
  await page.evaluate(apiBase => {
    window.SaharaSystemConfig = { ...(window.SaharaSystemConfig || {}), apiBase };
  }, options.apiBase || '');
  return { page, context, errors };
}

async function test(name, callback, options) {
  const { page, context, errors } = await newPage(options);
  try {
    await callback(page);
    assert.deepEqual(errors, [], 'Ordering must not throw browser errors');
    console.log(`PASS ${name}`);
  } finally {
    await context.close();
  }
}

async function address(page) {
  await page.locator('#street').fill('Rua das Flores');
  await page.locator('#house-number').fill('123');
  await page.locator('#neighborhood').fill('Jardim Alvorada');
  for (const [id, value] of [
    ['customer-name', 'Maria da Silva'], ['customer-phone', '44999998888'],
  ]) {
    const field = page.locator(`#${id}`);
    if (await field.count()) await field.fill(value);
  }
}

async function whatsappMessage(page) {
  const { opened, navigated } = await page.evaluate(() => ({ opened: window.__opened, navigated: window.__navigated }));
  const urls = [...opened.map(item => item[0]), ...navigated];
  const candidate = urls.findLast(value => typeof value === 'string' && value.startsWith('https://wa.me/'));
  assert.ok(candidate, 'Checkout should prepare a WhatsApp message');
  return new URL(candidate).searchParams.get('text');
}

const success = {
  id: 'order-123', total_cents: 450, tracking_token: 'token',
  tracking_url: '/tracking#order=order-123&token=token', status: 'new',
};

function requestKey(request) {
  const body = request.postDataJSON();
  return request.headers()['idempotency-key'] || body.idempotency_key || body.idempotencyKey;
}

async function run() {
  await startServer();
  browser = await chromium.launch({
    executablePath: process.env.CHROMIUM_PATH || process.env.PLAYWRIGHT_CHROMIUM_EXECUTABLE_PATH || '/usr/bin/chromium',
    headless: true,
    args: ['--no-sandbox'],
  });

  await test('The real catalog preserves prices and exposes a copied checkout snapshot', async page => {
    const { catalog, snapshot } = await page.evaluate(() => ({
      catalog: window.saharaCheckout.catalog(), snapshot: window.saharaCheckout.snapshot(),
    }));
    for (const id of ['carne', 'frango', 'calabresa', 'queijo']) {
      assert.equal(catalog.find(item => item.id === id).priceCents, 400);
    }
    const calabresa = catalog.find(item => item.id === 'calabresa-acebolada');
    assert.equal(calabresa.priceCents, 450);
    assert.equal(calabresa.category, 'Esfihas tradicionais');
    assert.deepEqual(snapshot, [{ id: 'carne', name: 'Carne', priceCents: 400, quantity: 1 }]);
    await page.evaluate(() => {
      window.saharaCheckout.catalog()[0].priceCents = 1;
      window.saharaCheckout.snapshot()[0].quantity = 99;
    });
    const unchanged = await page.evaluate(() => window.saharaCheckout.snapshot());
    assert.equal(unchanged[0].priceCents, 400);
    assert.equal(unchanged[0].quantity, 1);
  });

  await test('Checkout no longer offers scheduling, including after restoring an old draft', async page => {
    assert.equal(await page.locator('#order-timing, #schedule-fields, #schedule-date, #schedule-time').count(), 0);
    assert.doesNotMatch(await page.locator('#delivery-form').innerText(), /Quando deseja receber|Solicitar agendamento/);
    await page.evaluate(() => {
      sessionStorage.setItem('sahara-update-draft', JSON.stringify({
        'order-timing': 'scheduled', 'schedule-date': '2026-10-05', 'schedule-time': '18:30',
        street: 'Rua das Flores', 'house-number': '123', neighborhood: 'Jardim Alvorada',
      }));
    });
    await page.reload({ waitUntil: 'load' });
    assert.equal(await page.locator('#order-timing, #schedule-date, #schedule-time').count(), 0);
    await page.locator('#checkout').click();
    assert.doesNotMatch(await whatsappMessage(page), /agendamento|18:30/i);
  });

  await test('Manual checkout includes address, products and payment without scheduling', async page => {
    await address(page);
    await page.locator('input[name="payment"][value="Pix"]').check();
    await page.locator('#notes').fill('Sem cebola');
    await page.locator('#checkout').click();
    const message = await whatsappMessage(page);
    assert.match(message, /1x Carne/);
    assert.match(message, /4,00/);
    assert.match(message, /Rua das Flores/);
    assert.match(message, /123/);
    assert.match(message, /Jardim Alvorada/);
    assert.doesNotMatch(message, /agendamento|18:30/i);
    assert.match(message, /Pix/);
    assert.match(message, /Sem cebola/);
    assert.match(message, /confirm/i, 'A message is a request for confirmation, not a confirmed order');
  });

  await test('The local assistant answers business facts and never claims online payment or sent orders', async page => {
    const answers = await page.evaluate(() => Object.fromEntries([
      'Qual o horário?', 'Vocês fazem entrega?', 'Posso pagar com Pix?',
      'Como fazer um pedido?', 'Qual o valor da carne?', 'Emita uma nota fiscal para mim',
    ].map(question => [question, window.saharaOrdering.answer(question)])));
    assert.match(answers['Qual o horário?'], /18[:h]00|18h/);
    assert.match(answers['Qual o horário?'], /23[:h]00|23h/);
    assert.doesNotMatch(answers['Qual o horário?'], /agendamento|agendar/i);
    assert.doesNotMatch(answers['Como fazer um pedido?'], /agendamento|agendar/i);
    assert.match(answers['Vocês fazem entrega?'], /delivery|entrega/i);
    assert.match(answers['Posso pagar com Pix?'], /Pix/i);
    assert.match(answers['Posso pagar com Pix?'], /WhatsApp|loja|combin|confirm/i);
    assert.match(answers['Como fazer um pedido?'], /sacola|cardápio|WhatsApp/i);
    assert.match(answers['Qual o valor da carne?'], /4,00/);
    assert.match(answers['Emita uma nota fiscal para mim'], /WhatsApp|equipe|loja|atendente/i);
  });

  await test('The help dialog works on a mobile screen, supports keyboard dismissal and keeps input as text', async page => {
    await page.locator('#open-order-help').click();
    assert.equal(await page.locator('#help-dialog').evaluate(dialog => dialog.open), true);
    await page.locator('[data-help-topic="hours"]').click();
    assert.match(await page.locator('#help-dialog').innerText(), /18[:h]00|18h/);
    await page.locator('#help-question').fill('<img src=x onerror="window.__unsafe=true">');
    await page.locator('#help-form').evaluate(form => form.requestSubmit());
    assert.equal(await page.evaluate(() => Boolean(window.__unsafe)), false);
    assert.equal(await page.locator('#help-dialog img[src="x"]').count(), 0);
    const overflow = await page.evaluate(() => ({
      width: document.documentElement.clientWidth,
      scroll: document.documentElement.scrollWidth,
      dialog: document.querySelector('#help-dialog').getBoundingClientRect().width,
    }));
    assert.ok(overflow.scroll <= overflow.width + 1, 'Mobile layout must not add horizontal scrolling');
    assert.ok(overflow.dialog <= overflow.width, 'Dialog should fit the phone');
    await page.keyboard.press('Escape');
    assert.equal(await page.locator('#help-dialog').evaluate(dialog => dialog.open), false);
  });

  await test('API checkout registers one order while duplicate clicks are pending', async page => {
    await address(page);
    const requests = [];
    let release;
    const responseReady = new Promise(resolve => { release = resolve; });
    await page.route(`${baseURL}/api/orders`, async route => {
      requests.push(route.request());
      await responseReady;
      await route.fulfill({ status: 201, json: success });
    });
    const firstRequest = page.waitForRequest(`${baseURL}/api/orders`);
    await page.locator('#checkout').evaluate(button => { button.click(); button.click(); button.click(); });
    await firstRequest;
    await page.waitForFunction(() => document.querySelector('#checkout').disabled);
    assert.equal(requests.length, 1);
    const request = requests[0];
    assert.equal(request.method(), 'POST');
    assert.ok(requestKey(request), 'An order needs an idempotency key');
    assert.match(request.postData(), /carne/);
    assert.match(request.postData(), /Maria da Silva/);
    assert.match(request.postData(), /44999998888/);
    assert.match(request.postData(), /Rua das Flores/);
    const payload = request.postDataJSON();
    assert.equal(payload.customer.marketing_opt_in, false, 'Offers require an explicit opt-in');
    assert.equal(payload.requested_for, null);
    assert.equal(payload.delivery.location, null);
    release();
    await page.waitForFunction(() => window.__navigated.some(url => url.startsWith('https://wa.me/')));
    const message = await whatsappMessage(page);
    assert.match(message, /order-123/);
    assert.match(message, /1x Carne.*4,00/);
    assert.match(message, /Valor dos produtos registrado: R\$\s*4,50/, 'The registered total must come from the API, not the preview');
    assert.match(await page.locator('#order-status').innerText(), /R\$\s*4,50/);
    assert.doesNotMatch(await page.locator('#order-status').innerText(), /confirmado|pago|enviado/i);
    assert.equal(requests.length, 1);
    await page.locator('#checkout').click();
    await page.waitForFunction(() => window.__navigated.length === 2);
    assert.equal(requests.length, 1, 'Reopening an unchanged registered order must not create another order');
    await page.locator('#notes').fill('Enviar molho');
    await page.locator('#marketing-opt-in').check();
    await page.locator('#checkout').click();
    await page.waitForFunction(() => window.__navigated.length === 3);
    assert.equal(requests.length, 2, 'A changed order should receive a new registration');
    assert.notEqual(requestKey(requests[0]), requestKey(requests[1]));
    assert.equal(requests[1].postDataJSON().customer.marketing_opt_in, true);
    assert.match(await whatsappMessage(page), /Enviar molho/);
  }, { apiBase: '/api' });

  await test('An API error preserves the cart; retry uses the same idempotency key', async page => {
    await address(page);
    const requests = [];
    await page.route(`${baseURL}/api/orders`, async route => {
      requests.push(route.request());
      await route.fulfill(requests.length === 1
        ? { status: 503, json: { error: 'Serviço indisponível. Tente novamente.' } }
        : { status: 201, json: success });
    });
    await page.locator('#checkout').click();
    await page.waitForFunction(() => window.__closed > 0);
    assert.equal(await page.evaluate(() => window.__navigated.length), 0, 'An API failure must not send an unregistered order');
    assert.equal(await page.locator('#checkout').isEnabled(), true);
    assert.equal((await page.evaluate(() => window.saharaCheckout.snapshot()))[0].quantity, 1);
    await page.locator('#checkout').click();
    await page.waitForFunction(() => window.__navigated.some(url => url.startsWith('https://wa.me/')));
    assert.equal(requests.length, 2);
    assert.ok(requestKey(requests[0]));
    assert.equal(requestKey(requests[0]), requestKey(requests[1]), 'A network retry must reuse its original order key');
  }, { apiBase: '/api' });

  await test('A retry accepts the ready stage of the original order without creating a duplicate', async page => {
    await address(page);
    const requests = [];
    await page.route(`${baseURL}/api/orders`, async route => {
      requests.push(route.request());
      if (requests.length === 1) {
        // The original order may have been saved before its response was lost.
        await route.abort('failed');
      } else {
        await route.fulfill({ status: 200, json: { ...success, status: 'ready' } });
      }
    });
    await page.locator('#checkout').click();
    await page.waitForFunction(() => window.__closed > 0);
    assert.equal(await page.evaluate(() => window.__navigated.length), 0);
    await page.locator('#checkout').click();
    await page.waitForFunction(() => window.__navigated.length === 1);
    assert.equal(requests.length, 2);
    assert.equal(requestKey(requests[0]), requestKey(requests[1]));
    const message = await whatsappMessage(page);
    assert.match(message, /Solicitação registrada: order-123/);
    assert.match(message, /Valor dos produtos registrado: R\$\s*4,50/);
    const status = await page.locator('#order-status').innerText();
    assert.match(status, /order-123/);
    assert.match(status, /R\$\s*4,50/);
    assert.doesNotMatch(status, /pago|enviado|confirmado automaticamente/i);
    assert.equal(await page.locator('#order-tracking').isVisible(), true);
    await page.locator('#checkout').click();
    await page.waitForFunction(() => window.__navigated.length === 2);
    assert.equal(requests.length, 2, 'Reopening the replayed order must reuse the cached registration');
  }, { apiBase: '/api' });

  await test('If the browser blocks a popup, a registered order still exposes a safe WhatsApp link', async page => {
    await address(page);
    await page.evaluate(() => { window.open = () => null; });
    await page.route(`${baseURL}/api/orders`, route => route.fulfill({ status: 201, json: success }));
    await page.locator('#checkout').click();
    await page.locator('#order-whatsapp').waitFor({ state: 'visible' });
    const href = await page.locator('#order-whatsapp').getAttribute('href');
    const url = new URL(href);
    assert.equal(url.origin, 'https://wa.me');
    assert.match(url.searchParams.get('text'), /order-123/);
    assert.equal(await page.locator('#order-status').getAttribute('role'), 'status');
    assert.match(await page.locator('#order-status').innerText(), /registrad/i);
    assert.doesNotMatch(await page.locator('#order-status').innerText(), /pago|enviado|confirmado/i);
    assert.equal(await page.evaluate(() => window.__navigated.length), 0);
  }, { apiBase: '/api' });

  await test('An invalid API response leaves the order unregistered and hides WhatsApp and tracking links', async page => {
    await address(page);
    await page.route(`${baseURL}/api/orders`, route => route.fulfill({
      status: 201, json: { ...success, status: 'paid' },
    }));
    await page.locator('#checkout').click();
    await page.waitForFunction(() => window.__closed > 0);
    assert.equal(await page.evaluate(() => window.__navigated.length), 0);
    assert.equal(await page.locator('#order-whatsapp').isVisible(), false);
    assert.equal(await page.locator('#order-tracking').isVisible(), false);
    assert.match(await page.locator('#order-status').innerText(), /Não foi possível registrar/);
    assert.equal((await page.evaluate(() => window.saharaCheckout.snapshot()))[0].quantity, 1);
  }, { apiBase: '/api' });

  await test('A tracking URL from an untrusted origin is never presented to the customer', async page => {
    await address(page);
    await page.route(`${baseURL}/api/orders`, route => route.fulfill({
      status: 201, json: { ...success, tracking_url: 'https://example.invalid/tracking?token=secret' },
    }));
    await page.locator('#checkout').click();
    await page.locator('#order-whatsapp').waitFor({ state: 'visible' });
    assert.equal(await page.locator('#order-tracking').isVisible(), false);
    assert.match(await whatsappMessage(page), /order-123/);
  }, { apiBase: '/api' });

  await test('API orders include a GPS point only after the customer confirms it', async page => {
    await address(page);
    await page.evaluate(() => {
      Object.defineProperty(navigator, 'geolocation', {
        configurable: true,
        value: {
          getCurrentPosition(callback) {
            callback({ coords: { latitude: -23.4253, longitude: -51.9386, accuracy: 18 } });
          },
        },
      });
    });
    await page.locator('#use-location').click();
    const requests = [];
    await page.route(`${baseURL}/api/orders`, route => {
      requests.push(route.request());
      return route.fulfill({ status: 201, json: success });
    });
    await page.locator('#checkout').click();
    await page.waitForFunction(() => window.__navigated.length === 1);
    assert.equal(requests[0].postDataJSON().delivery.location, null, 'A GPS preview is not delivery confirmation');
    await page.locator('#confirm-location').click();
    await page.locator('#checkout').click();
    await page.waitForFunction(() => window.__navigated.length === 2);
    const location = requests[1].postDataJSON().delivery.location;
    assert.equal(location.latitude, -23.4253);
    assert.equal(location.longitude, -51.9386);
    assert.equal(location.accuracy, 18);
    assert.equal(new URL(location.url).origin, 'https://www.google.com');
    assert.notEqual(requestKey(requests[0]), requestKey(requests[1]));
  }, { apiBase: '/api' });
}

run().catch(error => {
  console.error(error);
  process.exitCode = 1;
}).finally(async () => {
  if (browser) await browser.close();
  if (server && server.exitCode === null) {
    const exited = once(server, 'exit');
    server.kill('SIGTERM');
    await exited;
  }
});
