(() => {
  'use strict';
  const checkout = window.saharaCheckout;
  const base = window.SaharaSystemConfig?.apiBase;
  if (!checkout || !base) return;
  const status = document.createElement('p');
  status.className = 'catalog-status'; status.setAttribute('role', 'status');
  status.textContent = 'Atualizando cardápio…';
  document.querySelector('#products').before(status);
  let endpoint;
  try {
    const url = new URL(String(base).replace(/\/+$/, '') + '/catalog', location.origin);
    if ((url.protocol !== 'https:' && url.origin !== location.origin) || url.username || url.password || url.search || url.hash) throw new Error();
    endpoint = url.href;
  } catch { status.textContent = 'Não foi possível consultar o cardápio. Fale com a loja pelo WhatsApp.'; return; }
  let pending = false;
  let fingerprint = '';
  let removedNotice = '';
  async function refresh() {
    if (pending) return;
    pending = true;
    const controller = new AbortController();
    const timeout = setTimeout(() => controller.abort(), 8000);
    try {
      const response = await fetch(endpoint, { cache: 'no-store', signal: controller.signal });
      if (!response.ok) throw new Error();
      const data = await response.json();
      if (!Array.isArray(data.products) || data.products.some(product => typeof product.id !== 'string' ||
          !Number.isSafeInteger(product.price_cents) || product.price_cents < 0 ||
          (product.available !== undefined && typeof product.available !== 'boolean'))) throw new Error();
      const next = JSON.stringify(data.products);
      window.SaharaSystemConfig.priceConfirmation = data.price_confirmation === true;
      if (next !== fingerprint) {
        const removed = checkout.applyCatalog(data.products);
        fingerprint = next;
        if (removed.length) removedNotice = `${removed.join(', ')} ${removed.length === 1 ? 'ficou indisponível e foi retirado' : 'ficaram indisponíveis e foram retirados'} do seu pedido. Escolha outro produto.`;
      }
      status.textContent = removedNotice || (data.products.length ? '' : 'Nenhum produto disponível no momento. Consulte a loja pelo WhatsApp.');
      status.hidden = !status.textContent;
    } catch {
      status.hidden = false;
      status.textContent = checkout.ready() ? 'Não foi possível atualizar o cardápio agora. A disponibilidade será conferida ao finalizar o pedido.' : 'Não foi possível carregar o cardápio. Tentando novamente…';
    } finally { clearTimeout(timeout); pending = false; }
  }
  window.saharaCatalogLive = { refresh };
  refresh();
  setInterval(() => { if (!document.hidden) refresh(); }, 3000);
  document.addEventListener('visibilitychange', () => { if (!document.hidden) refresh(); });
  window.addEventListener('focus', refresh);
  window.addEventListener('online', refresh);
})();
