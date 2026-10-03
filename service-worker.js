'use strict';
const CACHE = 'sahara-pwa-20261003-combos-fotos-v1';
const ROOT = new URL('./', self.registration.scope);
const ASSETS = [
  'index.html', 'style.css', 'app.js', 'pwa.css', 'pwa.js', 'manifest.webmanifest',
  'logo-sahara.jpg', 'esfihas.png',
  'icons/icon-192.png', 'icons/icon-512.png', 'icons/icon-maskable-512.png', 'icons/apple-touch-icon.png',
  'images/combo-10.jpg', 'images/combo-15.jpg', 'images/combo-mix-10.jpg', 'images/combo-mix-15.jpg', 'images/combo-familia-20.jpg', 'images/combo-especial.jpg', 'images/coca-600.jpg', 'images/coca-1l.jpg', 'images/coca-2l.jpg', 'images/coca-350.jpg', 'images/coca-zero-350.jpg', 'images/guarana-350.jpg', 'images/agua-500.jpg', 'images/refri-2l.jpg',
  'images/chocolate-profissional.jpg', 'images/doce-leite-profissional.jpg',
  'images/porquinho.jpg', 'images/bacon-queijo.jpg', 'images/bacon.jpg', 'images/brigadeiro.jpg',
  'images/brocolis-queijo.jpg', 'images/calabresa-catupiry.jpg', 'images/calabresa.jpg',
  'images/carne-queijo.png', 'images/carne.jpg', 'images/frango-bacon.jpg',
  'images/frango-catupiry.jpg', 'images/frango.jpg', 'images/pizza.png',
  'images/queijo.jpg', 'images/ricota.png', 'images/sensacao.jpg',
  'images/bacon-cheddar.jpg', 'images/bahiana.jpg', 'images/banoffee.jpg', 'images/beijinho.jpg', 'images/bombom-branco.jpg', 'images/brocolis-bacon-queijo.jpg', 'images/cardapio-doces-2.jpg', 'images/cardapio-especiais-1.jpg', 'images/cardapio-especiais-2.jpg', 'images/cardapio-salgadas-2.jpg', 'images/cardapio-salgadas-3.jpg', 'images/cardapio-shawarma.jpg', 'images/charge.jpg', 'images/leite-ninho.jpg', 'images/palmito-queijo.jpg', 'images/romeu-julieta.jpg', 'images/seducao.jpg', 'images/strogonoff-carne.jpg', 'images/vegetariana.jpg'
];
self.addEventListener('install', event => {
  event.waitUntil(caches.open(CACHE).then(cache => cache.addAll(
    ASSETS.map(path => new Request(new URL(path, ROOT), { cache: 'reload' }))
  )));
});
self.addEventListener('activate', event => {
  event.waitUntil((async () => {
    const keys = await caches.keys();
    await Promise.all(keys.filter(key => key.startsWith('sahara-pwa-') && key !== CACHE)
      .map(key => caches.delete(key)));
    await self.clients.claim();
  })());
});
self.addEventListener('message', event => {
  if (event.data?.type === 'SKIP_WAITING') self.skipWaiting();
});

self.addEventListener('fetch', event => {
  const request = event.request;
  const url = new URL(request.url);
  if (request.method !== 'GET' || url.origin !== ROOT.origin ||
      !url.pathname.startsWith(ROOT.pathname) || url.pathname.endsWith('/service-worker.js')) return;
  const navigation = request.mode === 'navigate';
  if (!navigation && !/\.(?:js|css|webmanifest|png|jpg|jpeg|svg|ico)$/.test(url.pathname)) return;
  const key = navigation ? new URL('index.html', ROOT) : new URL(url.pathname, url.origin);
  // Busca a versão atual primeiro, inclusive preços; usa o cache somente sem resposta da rede.
  event.respondWith((async () => {
    const cache = await caches.open(CACHE);
    try {
      const response = await fetch(new Request(request, { cache: 'no-cache' }));
      if (response.ok && response.type === 'basic') {
        // Falta de espaço no dispositivo não deve impedir abrir a versão online.
        await cache.put(key, response.clone()).catch(() => {});
      }
      if (response.status < 500) return response;
      const saved = await cache.match(key);
      return saved || response;
    } catch {
      const saved = await cache.match(key);
      if (saved && (navigation || url.pathname.endsWith('/app.js'))) {
        const client = await self.clients.get(event.clientId || event.resultingClientId);
        client?.postMessage({ type: 'CACHED_MENU' });
      }
      return saved || Response.error();
    }
  })());
});

