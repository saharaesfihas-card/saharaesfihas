(() => {
  'use strict';
  const checkout = window.saharaCheckout;
  if (!checkout) return;
  const box = document.createElement('section');
  box.className = 'ordering-fields';
  box.setAttribute('aria-label', 'Taxa de entrega');
  const title = document.createElement('strong'); title.textContent = 'Entrega';
  const rules = document.createElement('p'); rules.className = 'ordering-note';
  const amount = document.createElement('p'); amount.setAttribute('aria-live', 'polite');
  const total = document.createElement('p');
  box.append(title, rules, amount, total);
  document.querySelector('.quote-note').insertAdjacentElement('beforebegin', box);
  let quote = null, policy = '', key = null, sequence = 0;
  function render() {
    rules.textContent = policy;
    const known = quote?.status === 'quoted';
    amount.textContent = known ? `Taxa de entrega: ${quote.amount_cents === 0 ? 'grátis' : checkout.money(quote.amount_cents)}.` : 'Taxa a confirmar pelo atendente. Use sua localização e confirme o ponto para calcular.';
    const subtotal = checkout.snapshot().reduce((sum, item) => sum + item.quantity * item.priceCents, 0);
    total.textContent = `${known ? 'Total com entrega' : 'Produtos, sem a taxa de entrega'}: ${checkout.money(subtotal + (known ? quote.amount_cents : 0))}`;
  }
  async function refresh() {
    const location = window.saharaDeliveryLocation?.get() || null;
    const next = JSON.stringify(location);
    if (next === key) { render(); return; }
    key = next; quote = null; render();
    const current = ++sequence;
    const controller = new AbortController();
    const timeout = setTimeout(() => controller.abort(), 10000);
    try {
      const response = await fetch('/api/delivery/quote', { method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify({ location }), signal: controller.signal });
      if (!response.ok) throw new Error('quote');
      const data = await response.json();
      if (current !== sequence) return;
      policy = typeof data.policy === 'string' ? data.policy : '';
      quote = data.fee?.status === 'quoted' && Number.isSafeInteger(data.fee.amount_cents) && data.fee.amount_cents >= 0 ? data.fee : null;
    } catch { if (current === sequence) quote = null; }
    finally { clearTimeout(timeout); if (current === sequence) render(); }
  }
  new MutationObserver(render).observe(document.querySelector('#order-total'), { childList: true, characterData: true, subtree: true });
  document.addEventListener('sahara:location-changed', refresh);
  document.addEventListener('sahara:delivery-restored', refresh);
  window.saharaDeliveryFees = { policy: () => policy };
  refresh();
})();
