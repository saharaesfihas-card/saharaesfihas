'use strict';

// Run with: node tests/address.spec.cjs
// No GPS, geocoding service, WhatsApp window, or existing server is required.
const assert = require('node:assert/strict');
const { spawn } = require('node:child_process');
const { once } = require('node:events');
const net = require('node:net');
const path = require('node:path');
const { chromium } = require('playwright');

const root = path.resolve(__dirname, '..');
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

async function cleanup() {
  if (browser) await browser.close();
  if (server && server.exitCode === null) {
    const exited = once(server, 'exit');
    server.kill('SIGTERM');
    await exited;
  }
}

async function newPage(options = {}) {
  const context = await browser.newContext({
    viewport: { width: 390, height: 844 },
    reducedMotion: 'reduce',
    serviceWorkers: 'block',
  });
  await context.route('**/*', route => {
    const url = new URL(route.request().url());
    return url.origin === baseURL ? route.continue() : route.abort();
  });
  const page = await context.newPage();
  const errors = [];
  page.on('pageerror', error => errors.push(error.message));
  await page.addInitScript(config => {
    window.__opened = [];
    window.open = (...args) => { window.__opened.push(args); return null; };
    localStorage.setItem('sahara-cart', JSON.stringify([['carne', 1]]));
    window.__gps = { calls: [], pending: [] };
    const gps = {
      getCurrentPosition(success, failure, settings) {
        window.__gps.calls.push(settings);
        window.__gps.pending.push({ success, failure });
      },
    };
    if (config.unsupported) {
      Object.defineProperty(navigator, 'geolocation', { value: undefined, configurable: true });
    } else {
      Object.defineProperty(navigator, 'geolocation', { value: gps, configurable: true });
    }
    if (config.insecure) {
      Object.defineProperty(window, 'isSecureContext', { value: false, configurable: true });
    }
    window.__restoredEvents = 0;
    document.addEventListener('sahara:delivery-restored', () => { window.__restoredEvents++; });
    window.addEventListener('sahara:delivery-restored', () => { window.__restoredEvents++; });
    if (config.draft) sessionStorage.setItem('sahara-update-draft', JSON.stringify(config.draft));
    if (config.fakeWorker) {
      window.__workerMessages = [];
      const registration = {
        waiting: { postMessage: value => window.__workerMessages.push(value) },
        addEventListener() {},
      };
      Object.defineProperty(navigator, 'serviceWorker', {
        configurable: true,
        value: { controller: {}, register: () => Promise.resolve(registration), addEventListener() {} },
      });
    }
  }, options);
  await page.goto(`${baseURL}/index.html`, { waitUntil: 'load' });
  await page.waitForFunction(() => Boolean(window.saharaDeliveryLocation));
  await page.locator('#use-location').waitFor({ state: 'visible' });
  assert.equal(await page.locator('#location-status').getAttribute('role'), 'status');
  assert.equal(await page.evaluate(() => window.__gps.calls.length), 0, 'GPS must wait for a customer click');
  return { page, context, errors };
}

async function reply(page, response, request = 0) {
  await page.evaluate(({ response, request }) => {
    const pending = window.__gps.pending[request];
    if (!pending) throw new Error('There is no pending GPS request');
    if (response.error) pending.failure({ code: response.error, message: 'Simulated GPS failure' });
    else pending.success({ coords: response, timestamp: Date.now() });
  }, { response, request });
}

const point = { latitude: -23.4253, longitude: -51.9386, accuracy: 18 };
async function locationURL(page, coords) {
  const href = await page.locator('#location-map').getAttribute('href');
  const url = new URL(href);
  assert.equal(url.origin, 'https://www.google.com');
  assert.equal(url.pathname, '/maps');
  const [latitude, longitude] = url.searchParams.get('q').split(',').map(Number);
  assert.equal(latitude, coords.latitude);
  assert.equal(longitude, coords.longitude);
  return href;
}
const readLocation = page => page.evaluate(() => window.saharaDeliveryLocation.get());
const fields = page => page.evaluate(() => Object.fromEntries(
  ['street', 'house-number', 'neighborhood', 'complement', 'notes'].map(id => [id, document.getElementById(id).value]),
));

async function enterAddress(page) {
  await page.locator('#street').fill('Rua das Flores');
  await page.locator('#house-number').fill('123');
  await page.locator('#neighborhood').fill('Jardim Alvorada');
  await page.locator('#complement').fill('Casa azul');
  await page.locator('#notes').fill('Sem cebola');
}

async function openedMessage(page) {
  const opened = await page.evaluate(() => window.__opened);
  assert.ok(opened.length, 'Checkout should prepare the WhatsApp message');
  const url = new URL(opened.at(-1)[0]);
  assert.equal(url.origin, 'https://wa.me');
  return url.searchParams.get('text');
}

async function confirm(page, coords = point) {
  await page.locator('#use-location').click();
  await reply(page, coords);
  await page.locator('#confirm-location').click();
  assert.equal(await page.locator('#location-confirmed').inputValue(), 'yes');
}

async function test(name, callback, options = {}) {
  const { page, context, errors } = await newPage(options);
  try {
    await callback(page);
    assert.deepEqual(errors, [], 'The address flow must not throw browser errors');
    console.log(`PASS ${name}`);
  } finally {
    await context.close();
  }
}

async function run() {
  await startServer();
  browser = await chromium.launch({
    executablePath: process.env.CHROMIUM_PATH || process.env.PLAYWRIGHT_CHROMIUM_EXECUTABLE_PATH || '/usr/bin/chromium',
    headless: true,
    args: ['--no-sandbox'],
  });

  await test('GPS preview requires confirmation and keeps the typed address', async page => {
    await enterAddress(page);
    const original = await fields(page);
    await page.locator('#use-location').click();
    await reply(page, point);
    assert.equal(await page.locator('#location-card').isVisible(), true);
    await locationURL(page, point);
    assert.deepEqual(await fields(page), original);
    assert.equal(await readLocation(page), null);
    assert.notEqual(await page.locator('#location-confirmed').inputValue(), 'yes');
    assert.equal(await page.locator('#street').evaluate(field => field.required), true);
    assert.equal(await page.locator('#neighborhood').evaluate(field => field.required), true);
    await page.locator('#checkout').click();
    const before = await openedMessage(page);
    assert.match(before, /Rua: Rua das Flores/);
    assert.match(before, /Bairro: Jardim Alvorada/);
    assert.doesNotMatch(before, /Localização de entrega:|google\.com\/maps|\-23\.4253/);
    await page.locator('#confirm-location').click();
    assert.deepEqual(await fields(page), original);
    const location = await readLocation(page);
    assert.ok(location, 'Confirming should expose the delivery point');
    assert.equal(location.latitude, point.latitude);
    assert.equal(location.longitude, point.longitude);
    assert.equal(await page.locator('#street').evaluate(field => field.required), false);
    assert.equal(await page.locator('#neighborhood').evaluate(field => field.required), false);
    assert.equal(await page.locator('#house-number').evaluate(field => field.required), true);
    await page.locator('#checkout').click();
    const after = await openedMessage(page);
    assert.ok(after.includes(`Localização de entrega: ${await locationURL(page, point)}`));
    assert.match(after, /Rua: Rua das Flores/);
    assert.match(after, /Número: 123/);
    assert.match(after, /Bairro: Jardim Alvorada/);
  });

  await test('Confirmed GPS replaces only empty street and neighborhood; house number remains required', async page => {
    await confirm(page);
    await page.locator('#checkout').click();
    assert.equal(await page.evaluate(() => window.__opened.length), 0);
    assert.equal(await page.locator('#house-number').evaluate(field => field.validity.valueMissing), true);
    await page.locator('#house-number').fill('   ');
    await page.locator('#checkout').click();
    assert.equal(await page.evaluate(() => window.__opened.length), 0, 'Whitespace is not a house number');
    await page.locator('#house-number').fill('S/N');
    await page.locator('#checkout').click();
    const message = await openedMessage(page);
    assert.ok(message.includes(`Localização de entrega: ${await locationURL(page, point)}`));
    assert.match(message, /Número: S\/N/);
    assert.doesNotMatch(message, /(?:^|\n)Rua:|(?:^|\n)Bairro:/);
  });

  await test('Coordinates at zero are valid; removal restores the manual address requirements', async page => {
    const zero = { latitude: 0, longitude: 0, accuracy: 0 };
    await confirm(page, zero);
    assert.equal((await readLocation(page)).latitude, 0);
    assert.equal((await readLocation(page)).longitude, 0);
    await locationURL(page, zero);
    await page.locator('#house-number').fill('42');
    await page.locator('#checkout').click();
    assert.ok((await openedMessage(page)).includes(`Localização de entrega: ${await locationURL(page, zero)}`));
    await page.locator('#remove-location').click();
    assert.equal(await readLocation(page), null);
    for (const id of ['location-latitude', 'location-longitude', 'location-accuracy', 'location-confirmed']) {
      assert.equal(await page.locator(`#${id}`).inputValue(), '');
    }
    assert.equal(await page.locator('#location-card').isVisible(), false);
    assert.equal(await page.locator('#street').evaluate(field => field.required), true);
    assert.equal(await page.locator('#neighborhood').evaluate(field => field.required), true);
    await page.locator('#checkout').click();
    assert.equal(await page.evaluate(() => window.__opened.length), 1, 'Removal must require an address before another order');
    await page.locator('#street').fill('Rua Nova');
    await page.locator('#neighborhood').fill('Centro');
    await page.locator('#checkout').click();
    assert.doesNotMatch(await openedMessage(page), /Localização de entrega:|google\.com\/maps/);
  });

  await test('Repeated clicks request GPS once and preserve typed fields', async page => {
    await enterAddress(page);
    const original = await fields(page);
    await page.locator('#use-location').evaluate(button => { button.click(); button.click(); button.click(); });
    assert.equal(await page.evaluate(() => window.__gps.calls.length), 1);
    await reply(page, point);
    assert.deepEqual(await fields(page), original);
    assert.equal(await readLocation(page), null);
  });

  for (const [code, name, message] of [
    [1, 'permission denied', /permis|permit|autoriz|bloque|negad/i],
    [2, 'position unavailable', /dispon|localiza|encontr/i],
    [3, 'timeout', /tempo|demor|novamente|tente/i],
  ]) {
    await test(`GPS ${name} is recoverable without losing the manual address`, async page => {
      await enterAddress(page);
      const original = await fields(page);
      await page.locator('#use-location').click();
      await reply(page, { error: code });
      assert.match(await page.locator('#location-status').innerText(), message);
      assert.equal(await readLocation(page), null);
      assert.equal(await page.locator('#use-location').isEnabled(), true);
      assert.deepEqual(await fields(page), original);
      assert.equal(await page.locator('#street').evaluate(field => field.required), true);
      await page.locator('#checkout').click();
      assert.doesNotMatch(await openedMessage(page), /Localização de entrega:|google\.com\/maps/);
      await page.locator('#use-location').click();
      assert.equal(await page.evaluate(() => window.__gps.calls.length), 2);
      await reply(page, point, 1);
      await page.locator('#confirm-location').click();
      assert.ok(await readLocation(page));
      assert.deepEqual(await fields(page), original);
    });
  }

  for (const [name, options, message] of [
    ['unsupported browser', { unsupported: true }, /navegador|suport|dispon/i],
    ['insecure origin', { insecure: true }, /https|segur/i],
  ]) {
    await test(`Manual checkout remains usable on an ${name}`, async page => {
      await enterAddress(page);
      const original = await fields(page);
      await page.locator('#use-location').click();
      assert.equal(await page.evaluate(() => window.__gps.calls.length), 0);
      assert.match(await page.locator('#location-status').innerText(), message);
      assert.equal(await readLocation(page), null);
      assert.deepEqual(await fields(page), original);
      await page.locator('#checkout').click();
      assert.match(await openedMessage(page), /Rua: Rua das Flores/);
    }, options);
  }

  await test('Invalid coordinates cannot become a delivery point', async page => {
    const invalidPoints = [
      { latitude: 91, longitude: 10, accuracy: 12 },
      { latitude: 10, longitude: -181, accuracy: 12 },
      { latitude: NaN, longitude: 10, accuracy: 12 },
      { latitude: 10, longitude: Infinity, accuracy: 12 },
    ];
    for (const [index, coords] of invalidPoints.entries()) {
      await page.locator('#use-location').click();
      await reply(page, coords, index);
      assert.equal(await readLocation(page), null);
      assert.notEqual(await page.locator('#location-confirmed').inputValue(), 'yes');
      assert.equal(await page.locator('#location-card').isVisible(), false);
      assert.ok((await page.locator('#location-status').innerText()).trim());
      assert.equal(await page.locator('#street').evaluate(field => field.required), true);
      assert.equal(await page.locator('#use-location').isEnabled(), true);
    }
  });

  await test('App update saves and restores confirmed GPS with the other checkout fields', async page => {
    await enterAddress(page);
    await page.locator('input[name="payment"][value="Pix"]').check();
    await confirm(page);
    await page.locator('#update-app').click();
    const draft = await page.evaluate(() => JSON.parse(sessionStorage.getItem('sahara-update-draft')));
    assert.equal(draft['location-latitude'], String(point.latitude));
    assert.equal(draft['location-longitude'], String(point.longitude));
    assert.equal(draft['location-accuracy'], String(point.accuracy));
    assert.equal(draft['location-confirmed'], 'yes');
    assert.equal(draft.street, 'Rua das Flores');
    assert.equal(draft.notes, 'Sem cebola');
    assert.deepEqual(await page.evaluate(() => window.__workerMessages), [{ type: 'SKIP_WAITING' }]);
    await page.reload({ waitUntil: 'load' });
    assert.ok(await readLocation(page));
    assert.equal(await page.locator('#location-confirmed').inputValue(), 'yes');
    await locationURL(page, point);
    assert.equal(await page.locator('#street').inputValue(), 'Rua das Flores');
    assert.equal(await page.locator('#house-number').inputValue(), '123');
    assert.equal(await page.locator('input[name="payment"][value="Pix"]').isChecked(), true);
    assert.equal(await page.locator('#notes').inputValue(), 'Sem cebola');
    assert.equal(await page.locator('#street').evaluate(field => field.required), false);
    assert.ok(await page.evaluate(() => window.__restoredEvents > 0));
    assert.equal(await page.evaluate(() => sessionStorage.getItem('sahara-update-draft')), null);
    assert.equal(await page.evaluate(() => window.__gps.calls.length), 0, 'Restoring must not request GPS again');
    await page.locator('#checkout').click();
    assert.ok((await openedMessage(page)).includes(`Localização de entrega: ${await locationURL(page, point)}`));
  }, { fakeWorker: true });

  await test('A restored unconfirmed point still needs customer confirmation', async page => {
    assert.equal(await readLocation(page), null);
    assert.equal(await page.locator('#street').evaluate(field => field.required), true);
    assert.equal(await page.locator('#neighborhood').evaluate(field => field.required), true);
    assert.notEqual(await page.locator('#location-confirmed').inputValue(), 'yes');
  }, {
    draft: {
      'house-number': '10',
      'location-latitude': String(point.latitude),
      'location-longitude': String(point.longitude),
      'location-accuracy': String(point.accuracy),
      'location-confirmed': '',
    },
  });

  await test('An invalid restored confirmed point cannot bypass address validation', async page => {
    assert.equal(await readLocation(page), null);
    assert.equal(await page.locator('#street').evaluate(field => field.required), true);
    assert.equal(await page.locator('#neighborhood').evaluate(field => field.required), true);
    await page.locator('#checkout').click();
    assert.equal(await page.evaluate(() => window.__opened.length), 0);
  }, {
    draft: { 'house-number': '10', 'location-latitude': '999', 'location-longitude': '0', 'location-confirmed': 'yes' },
  });
}

run().catch(error => { console.error(error); process.exitCode = 1; }).finally(cleanup);
