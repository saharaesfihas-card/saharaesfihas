'use strict';

// Real isolated backend/browser/audio graph. Printer calls are intercepted; no real messages or prints.
const assert = require('node:assert/strict');
const { spawn } = require('node:child_process');
const { once } = require('node:events');
const { randomBytes } = require('node:crypto');
const { mkdtemp, rm } = require('node:fs/promises');
const net = require('node:net');
const os = require('node:os');
const path = require('node:path');
const { chromium } = require('playwright');
let backend, browser, directory, page, context, base, csrf, checks = 0;
const errors = [];
const password = randomBytes(24).toString('hex');

async function waitFor(predicate) {
  for (let i = 0; i < 150; i++) {
    if (await predicate()) return;
    await new Promise(resolve => setTimeout(resolve, 40));
  }
  throw new Error('Expected state did not arrive');
}

async function api(route, data, method) {
  const r = await page.request.fetch(base + '/api' + route, {
    method: method || (data ? 'POST' : 'GET'), data,
    headers: { 'X-Sahara-CSRF': csrf || '' },
  });
  assert.ok(r.ok(), await r.text()); return r.json();
}

async function order(route = '/orders') {
  return api(route, { items: [{ id: 'kitkat', quantity: 2 }], customer: { name: 'Cliente dos avisos', phone: '44999995555' },
    delivery: { street: 'Rua do cliente', number: '12', neighborhood: 'Centro' }, payment_method: 'Pix', notes: 'Sem talheres', idempotency_key: randomBytes(16).toString('hex') });
}

async function totals(p = page) {
  return p.evaluate(() => ({ notes: window.__notes.length, prints: window.__receipts.length, feeds: window.__feeds }));
}

async function tick() {
  const previous = (await totals()).feeds;
  await page.clock.runFor(6000);
  await waitFor(async () => (await totals()).feeds > previous);
  await page.clock.runFor(50);
}

async function check(name, run) { await run(); checks++; process.stdout.write(`PASS ${name}\n`); }

async function start() {
  directory = await mkdtemp(path.join(os.tmpdir(), 'sahara-order-alerts-'));
  const probe = net.createServer(); probe.listen(0, '127.0.0.1'); await once(probe, 'listening');
  const port = probe.address().port; await new Promise(resolve => probe.close(resolve));
  base = `http://127.0.0.1:${port}`;
  const script = 'import os; from server.core import hash_password; os.environ["SAHARA_ADMIN_PASSWORD_HASH"]=hash_password(os.environ.pop("SAHARA_TEST_PASSWORD")); import uvicorn; uvicorn.run("server.main:app",host="127.0.0.1",port=' + port + ',log_level="warning")';
  backend = spawn(process.env.PYTHON || 'python3', ['-u', '-c', script], { cwd: path.resolve(__dirname, '..'),
    env: { ...process.env, SAHARA_DATA_DIR: directory, SAHARA_COOKIE_SECURE: '0', SAHARA_WHATSAPP_ENABLED: '0', SAHARA_TEST_PASSWORD: password }, stdio: ['ignore', 'pipe', 'pipe'] });
  let output = ''; backend.stdout.on('data', b => output += b); backend.stderr.on('data', b => output += b);
  await waitFor(async () => {
    if (backend.exitCode !== null) throw new Error(output);
    try { return (await fetch(base + '/api/health')).ok; } catch { return false; }
  });
  browser = await chromium.launch({ executablePath: '/usr/bin/chromium', headless: true, args: ['--no-sandbox'] });
  context = await browser.newContext({ viewport: { width: 1280, height: 900 }, serviceWorkers: 'block' });
  await context.route('**/*', r => new URL(r.request().url()).origin === base ? r.continue() : r.abort());
  await context.addInitScript(() => {
    window.__notes = []; window.__receipts = []; window.__feeds = 0;
    const nativeFetch = window.fetch;
    window.fetch = async (...args) => {
      const r = await nativeFetch(...args);
      if (String(args[0]).includes('/orders/incoming?')) {
        await r.clone().text(); window.__feeds++;
      }
      return r;
    };
    const NativeAudio = window.AudioContext;
    window.AudioContext = class extends NativeAudio {
      createOscillator() {
        const node = super.createOscillator(), nativeStart = node.start.bind(node);
        node.start = when => { window.__notes.push({ when, frequency: node.frequency.value }); nativeStart(when); };
        return node;
      }
    };
    window.print = () => { window.__receipts.push(document.getElementById('print-target').textContent); };
  });
  page = await context.newPage(); page.on('pageerror', e => errors.push(e.message));
  await page.clock.install();
  await page.goto(base + '/admin.html');
  await page.locator('#admin-password').fill(password);
  await page.getByRole('button', { name: 'Entrar no painel', exact: true }).click();
  await page.locator('#workspace:not([hidden])').waitFor();
  csrf = (await api('/admin/session')).csrf_token;
}

async function run() {
  await start();
  await check('First activation previews the real audio graph and ignores historical orders', async () => {
    await order();
    assert.equal(await page.locator('#order-auto-print').isChecked(), false);
    await page.locator('#order-sound').click();
    await waitFor(async () => (await totals()).notes === 12);
    await page.locator('#order-auto-print').check();
    await tick();
    assert.equal((await totals()).prints, 0);
    assert.equal(await page.locator('#order-sound').getAttribute('aria-pressed'), 'true');
  });
  let received;
  await check('A new website order rings once, prints its complete receipt and appears without refresh', async () => {
    const before = await totals(); received = await order();
    await tick();
    await waitFor(async () => (await totals()).prints === before.prints + 1);
    assert.equal((await totals()).notes, before.notes + 12);
    const receipt = await page.evaluate(() => window.__receipts.at(-1));
    const copies = await page.locator('#print-target .receipt-copy').allTextContents();
    assert.equal(copies.length, 2); assert.equal(copies[0], copies[1]);
    assert.match(copies[0], new RegExp(`#${received.display_number}`));
    assert.match(receipt, new RegExp(`#${received.display_number}`));
    for (const value of ['KitKat', '11,00', 'Cliente dos avisos', 'Rua do cliente', 'Sem talheres', 'Pix']) assert.ok(receipt.includes(value), value);
    await waitFor(async () => (await page.locator('#screen').innerText()).includes(`#${received.display_number}`));
  });
  await check('Rechecking the feed and changing order status never ring or print again', async () => {
    const before = await totals();
    await api('/admin/orders/' + received.id, { status: 'ready' }, 'PATCH');
    await tick();
    assert.equal((await totals()).notes, before.notes); assert.equal((await totals()).prints, before.prints);
  });
  await check('Manual orders alert while the operator keeps unsaved fields on another screen', async () => {
    await page.locator('#new-order').click();
    await page.locator('#field-pos-name').fill('Rascunho preservado');
    const next = await order('/admin/orders'); const before = await totals();
    await tick();
    await waitFor(async () => (await totals()).prints === before.prints + 1);
    assert.equal(await page.locator('#field-pos-name').inputValue(), 'Rascunho preservado');
    assert.match(await page.locator('#order-alert-message').innerText(), new RegExp(`#${next.display_number}`));
  });
  await check('Two active tabs in one browser cannot duplicate sound or print', async () => {
    const second = await context.newPage(); second.on('pageerror', e => errors.push(e.message));
    try {
      await second.goto(base + '/admin.html'); await second.locator('#workspace:not([hidden])').waitFor();
      await second.locator('#order-sound').click();
      await waitFor(async () => (await second.locator('#order-monitor-status').innerText()).includes('outra aba'));
      assert.equal(await second.locator('#order-sound').getAttribute('aria-pressed'), 'false');
      await page.bringToFront(); const before = await totals(); await order(); await tick();
      await waitFor(async () => (await totals()).prints === before.prints + 1);
      assert.equal((await totals(second)).prints, 0); assert.equal((await totals(second)).notes, 0);
    } finally { await second.close(); }
  });
  await check('A burst waits for each print to finish before replacing the next receipt', async () => {
    await page.evaluate(() => {
      window.print = () => new Promise(resolve => {
        window.__receipts.push(document.getElementById('print-target').textContent);
        window.__finishPrint = resolve;
      });
    });
    const first = await order(), second = await order('/admin/orders'), before = await totals();
    await tick(); await waitFor(async () => (await totals()).prints === before.prints + 1);
    assert.match(await page.locator('#print-target').innerText(), new RegExp(`#${first.display_number}`));
    await page.evaluate(() => window.__finishPrint());
    await waitFor(async () => (await totals()).prints === before.prints + 2);
    assert.match(await page.locator('#print-target').innerText(), new RegExp(`#${second.display_number}`));
    await page.evaluate(() => {
      window.__finishPrint();
      window.print = () => window.__receipts.push(document.getElementById('print-target').textContent);
    });
  });
  await check('Muting sound keeps automatic printing active; disabling both releases the monitor', async () => {
    await page.locator('#order-sound').click();
    const before = await totals(); await order(); await tick();
    await waitFor(async () => (await totals()).prints === before.prints + 1);
    assert.equal((await totals()).notes, before.notes);
    await page.locator('#order-auto-print').uncheck();
    const paused = await totals(); await order(); await page.clock.runFor(10000);
    assert.equal((await totals()).feeds, paused.feeds); assert.equal((await totals()).prints, paused.prints);
    await page.locator('#order-auto-print').check(); await tick();
    assert.equal((await totals()).prints, paused.prints);
  });
  await check('A temporary polling failure keeps the cursor and catches all orders on reconnection', async () => {
    let fail = true;
    await page.route('**/api/admin/orders/incoming?*', route => {
      if (fail) { fail = false; return route.fulfill({ status: 503, contentType: 'application/json', body: '{"detail":"Temporary test failure"}' }); }
      return route.continue();
    });
    const first = await order(); const before = await totals(); await tick();
    await waitFor(async () => (await page.locator('#order-monitor-status').innerText()).includes('Sem conexão'));
    const second = await order(); await tick();
    await waitFor(async () => (await totals()).prints === before.prints + 2);
    const receipts = await page.evaluate(() => window.__receipts.slice(-2));
    assert.match(receipts[0], new RegExp(`#${first.display_number}`)); assert.match(receipts[1], new RegExp(`#${second.display_number}`));
    await page.unroute('**/api/admin/orders/incoming?*');
  });
  await check('A printer error is visible and the same order is not printed repeatedly', async () => {
    await page.evaluate(() => { window.print = () => { throw new Error('Printer unavailable'); }; });
    await order(); await tick();
    await waitFor(async () => (await page.locator('#order-alert-message').innerText()).includes('não abriram'));
    const before = await totals(); await tick(); assert.equal((await totals()).prints, before.prints);
  });
  await check('Logout cancels in-flight arrivals and releases the tab lock', async () => {
    const newOrder = await order();
    const payload = await api('/admin/orders/incoming?cursor=0');
    let held;
    await page.route('**/api/admin/orders/incoming?*', route => { held = route; });
    await page.clock.runFor(6000); await waitFor(() => Boolean(held));
    const before = await totals();
    await page.locator('#logout').click(); await page.locator('#login-panel:not([hidden])').waitFor();
    await held.fulfill({ status: 200, contentType: 'application/json', body: JSON.stringify(payload) });
    await page.clock.runFor(10000);
    assert.equal((await totals()).prints, before.prints);
    assert.equal(await page.locator('#order-auto-print').isChecked(), false);
    assert.equal(await page.locator('#order-sound').getAttribute('aria-pressed'), 'false');
    assert.ok(payload.orders.some(o => o.id === newOrder.id));
    assert.deepEqual(errors, []);
  });
  process.stdout.write(`${checks} order alert browser checks passed.\n`);
}

(async () => {
  try { await run(); }
  catch (error) { process.stderr.write((error.stack || String(error)) + '\n'); process.exitCode = 1; }
  finally {
    if (browser) await browser.close();
    if (backend && backend.exitCode === null) { backend.kill('SIGTERM'); await once(backend, 'exit'); }
    if (directory) await rm(directory, { recursive: true, force: true });
  }
})();
