'use strict';

// Requires the local FastAPI server; never registers an order or sends a message.
const assert = require('node:assert/strict');
const { chromium } = require('playwright');
const baseURL = process.env.SAHARA_TEST_URL || 'http://127.0.0.1:8010';
const localOrigin = new URL(baseURL).origin;

async function isolateNetwork(context) {
  await context.route('**/*', route => new URL(route.request().url()).origin === localOrigin
    ? route.continue() : route.abort());
}

(async () => {
  const browser = await chromium.launch({ executablePath: '/usr/bin/chromium', headless: true });
  try {
    const context = await browser.newContext({ viewport: { width: 390, height: 844 } });
    await isolateNetwork(context);
    const page = await context.newPage();
    const errors = [];
    page.on('pageerror', error => errors.push(error.message));
    await page.goto(baseURL);
    await page.waitForFunction(() => !!navigator.serviceWorker.controller);
    assert.equal(await page.locator('.product').count(), 56);
    assert.equal(await page.locator('#app-install').isVisible(), true);
    await page.locator('[data-change="carne"][data-delta="1"]').first().click();
    await page.reload();
    assert.equal(await page.locator('#count').innerText(), '1');
    console.log('PASS: cardápio com 56 produtos, service worker e sacola persistente');

    await page.evaluate(() => {
      sessionStorage.setItem('sahara-update-draft', JSON.stringify({
        'order-timing': 'scheduled', 'schedule-date': '2026-10-05', 'schedule-time': '19:30',
        'customer-name': 'Maria', 'customer-phone': '44999998888', 'marketing-opt-in': true,
        'location-latitude': '-23.4253', 'location-longitude': '-51.9386',
        'location-accuracy': '18', 'location-confirmed': 'yes', 'house-number': '123',
        'street': '', 'neighborhood': '', 'notes': 'Sem cebola', 'Pix': true,
      }));
    });
    await page.reload();
    assert.equal(await page.locator('#order-timing, #schedule-fields, #schedule-time').count(), 0);
    assert.equal(await page.locator('#customer-name').inputValue(), 'Maria');
    assert.equal(await page.locator('#customer-phone').inputValue(), '44999998888');
    assert.equal(await page.locator('#marketing-opt-in').isChecked(), true);
    assert.equal(await page.locator('input[value="Pix"]').isChecked(), true);
    assert.equal(await page.locator('#street').getAttribute('required'), null);
    assert.equal(await page.locator('#notes').inputValue(), 'Sem cebola');
    console.log('PASS: atualização restaura contato, consentimento, Pix e GPS e ignora agendamento antigo');

    await page.goto(`${baseURL}/admin.html`);
    await page.goto(baseURL);
    await page.evaluate(() => fetch('/api/health'));
    const cached = await page.evaluate(async () => {
      const names = (await caches.keys()).filter(name => name.startsWith('sahara-pwa-'));
      if (names.length !== 1) throw new Error('Only the current menu cache should remain');
      const cache = await caches.open(names[0]);
      return { urls: (await cache.keys()).map(request => new URL(request.url).pathname),
        html: await (await cache.match(new URL('index.html', location.href))).text() };
    });
    assert.ok(cached.urls.includes('/ordering.js') && cached.urls.includes('/ordering.css'));
    assert.ok(!cached.urls.some(url => /admin|system-config|\/api\//.test(url)));
    assert.match(cached.html, /<title>Saharaesfihas/);
    console.log('PASS: painel, API e configuração ficam fora do cache e preservam o cardápio salvo');

    await context.setOffline(true);
    await page.reload();
    assert.equal(await page.locator('.product').count(), 56);
    assert.equal(await page.locator('#count').innerText(), '1');
    const cdp = await context.newCDPSession(page);
    await cdp.send('Network.emulateNetworkConditions', {
      offline: true, latency: 0, downloadThroughput: 0, uploadThroughput: 0,
    });
    await page.evaluate(() => {
      window.__opened = [];
      window.open = (...args) => { window.__opened.push(args); return null; };
      window.dispatchEvent(new Event('offline'));
    });
    await page.locator('#checkout').click();
    assert.equal(await page.evaluate(() => window.__opened.length), 0);
    assert.equal(await page.locator('#offline-notice').isVisible(), true);
    assert.deepEqual(errors, []);
    console.log('PASS: cardápio abre offline e bloqueia o envio enquanto mantém a sacola');
    await context.close();
    const nativeContext = await browser.newContext({
      userAgent: 'Mozilla/5.0 (Linux; Android 15) AppleWebKit/537.36 Chrome/133.0.0.0 Mobile Safari/537.36 SaharaAndroid/1',
    });
    await isolateNetwork(nativeContext);
    const nativePage = await nativeContext.newPage();
    await nativePage.goto(baseURL);
    assert.equal(await nativePage.locator('#app-install').isVisible(), false);
    assert.equal(await nativePage.locator('.product').count(), 56);
    console.log('PASS: o APK abre o cardápio sem pedir uma segunda instalação');
    await nativeContext.close();
  } finally { await browser.close(); }
})().catch(error => { console.error(error); process.exitCode = 1; });
