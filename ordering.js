(() => {
  'use strict';

  const checkout = window.saharaCheckout;
  const form = document.querySelector('#delivery-form');
  if (!checkout || !form) return;
  const payment = form.querySelector('.payment-methods');
  const timingSection = document.createElement('fieldset');
  timingSection.className = 'ordering-fields';
  timingSection.innerHTML = `
    <legend>Quando deseja receber?</legend>
    <label for="order-timing">Horário do pedido</label>
    <select id="order-timing" name="order-timing" aria-describedby="schedule-help">
      <option value="now">Assim que possível</option>
      <option value="scheduled">Solicitar agendamento</option>
    </select>
    <div id="schedule-fields" hidden>
      <label for="schedule-date">Data da entrega</label>
      <input id="schedule-date" name="schedule-date" type="date" disabled>
      <label for="schedule-time">Horário da entrega</label>
      <input id="schedule-time" name="schedule-time" type="time" min="18:00" max="23:00" step="60" disabled>
    </div>
    <p id="schedule-help" class="ordering-note">Delivery de segunda a domingo, das 18h às 23h, no horário de Maringá. O agendamento é uma solicitação e depende da confirmação da loja.</p>`;
  form.insertBefore(timingSection, payment);

  const contactSection = document.createElement('fieldset');
  contactSection.className = 'ordering-fields';
  contactSection.innerHTML = `
    <legend>Contato para o pedido (opcional)</legend>
    <label for="customer-name">Seu nome</label>
    <input id="customer-name" name="customer-name" type="text" autocomplete="name" maxlength="100">
    <label for="customer-phone">Telefone com DDD</label>
    <input id="customer-phone" name="customer-phone" type="tel" autocomplete="tel" inputmode="tel" maxlength="24" placeholder="Ex.: (44) 99999-9999">
    <label class="ordering-consent" for="marketing-opt-in"><input id="marketing-opt-in" name="marketing-opt-in" type="checkbox"><span>Quero receber ofertas da Sahara no WhatsApp (opcional).</span></label>
    <p class="ordering-note">O pedido funciona sem criar uma conta. A preferência por ofertas não envia mensagens automaticamente.</p>`;
  form.insertBefore(contactSection, payment);

  const status = document.createElement('p');
  status.id = 'order-status';
  status.className = 'ordering-status';
  status.setAttribute('role', 'status');
  status.setAttribute('aria-live', 'polite');
  status.hidden = true;
  const button = document.querySelector('#checkout');
  button.insertAdjacentElement('afterend', status);
  const links = document.createElement('div');
  links.className = 'ordering-links';
  links.innerHTML = '<a id="order-whatsapp" target="_blank" rel="noopener noreferrer" hidden>Revisar a solicitação no WhatsApp ↗</a><a id="order-tracking" target="_blank" rel="noopener noreferrer" hidden>Acompanhar a solicitação ↗</a>';
  status.insertAdjacentElement('afterend', links);
  const whatsappLink = links.querySelector('#order-whatsapp');
  const trackingLink = links.querySelector('#order-tracking');
  const timing = document.querySelector('#order-timing');
  const dateField = document.querySelector('#schedule-date');
  const timeField = document.querySelector('#schedule-time');
  const dateFormatter = new Intl.DateTimeFormat('en-CA', {
    timeZone: 'America/Sao_Paulo', year: 'numeric', month: '2-digit', day: '2-digit',
  });
  let pending = false;
  let attempt = null;

  function businessDate(value = new Date()) {
    const parts = Object.fromEntries(dateFormatter.formatToParts(value).map(part => [part.type, part.value]));
    return `${parts.year}-${parts.month}-${parts.day}`;
  }
  function plusDays(value, days) {
    const [year, month, day] = value.split('-').map(Number);
    return new Date(Date.UTC(year, month - 1, day + days)).toISOString().slice(0, 10);
  }
  function updateSchedule(now = new Date()) {
    const scheduled = timing.value === 'scheduled';
    document.querySelector('#schedule-fields').hidden = !scheduled;
    for (const field of [dateField, timeField]) {
      field.disabled = !scheduled;
      field.required = scheduled;
      field.setCustomValidity('');
    }
    dateField.min = businessDate(now);
    dateField.max = plusDays(dateField.min, 7);
  }
  function validateSchedule(now = new Date()) {
    updateSchedule(now);
    if (timing.value !== 'scheduled') return { requestedFor: null, label: '', error: '' };
    const fail = (field, error) => {
      field.setCustomValidity(error);
      return { requestedFor: null, label: '', error };
    };
    if (!dateField.value) return fail(dateField, 'Escolha a data desejada para a entrega.');
    if (!timeField.value) return fail(timeField, 'Escolha o horário desejado para a entrega.');
    if (dateField.value < dateField.min || dateField.value > dateField.max) {
      return fail(dateField, 'Escolha uma data entre hoje e os próximos 7 dias.');
    }
    if (timeField.value < '18:00' || timeField.value > '23:00') {
      return fail(timeField, 'Escolha um horário entre 18h e 23h, no horário de Maringá.');
    }
    // Maringá uses UTC−03:00 throughout the supported seven-day window.
    // Confirm the resulting local day with Intl instead of using the device timezone.
    const requested = new Date(`${dateField.value}T${timeField.value}:00-03:00`);
    if (!Number.isFinite(requested.getTime()) || businessDate(requested) !== dateField.value) {
      return fail(dateField, 'Escolha uma data válida para a entrega.');
    }
    if (requested.getTime() <= now.getTime()) {
      return fail(timeField, 'Escolha uma data e um horário futuros para solicitar o agendamento.');
    }
    const [year, month, day] = dateField.value.split('-');
    return {
      requestedFor: requested.toISOString(), label: `${day}/${month}/${year} às ${timeField.value}`, error: '',
    };
  }
  timing.addEventListener('change', () => updateSchedule());
  for (const field of [dateField, timeField]) field.addEventListener('input', () => field.setCustomValidity(''));
  updateSchedule();
  document.addEventListener('sahara:delivery-restored', () => updateSchedule());

  function announce(message, failed = false) {
    status.textContent = message;
    status.hidden = !message;
    status.classList.toggle('is-error', failed);
  }
  function apiConfiguration() {
    const value = window.SaharaSystemConfig?.apiBase;
    if (!value) return { url: '', error: '' };
    if (typeof value !== 'string') return { url: '', error: 'O serviço de pedidos está com uma configuração inválida. Fale com a loja pelo WhatsApp.' };
    const base = value.trim().replace(/\/+$/, '');
    if (base === '/api') return { url: base, error: '' };
    try {
      const url = new URL(base);
      if (url.protocol === 'https:' && !url.username && !url.password && !url.search && !url.hash) {
        return { url: base, error: '' };
      }
    } catch {}
    return { url: '', error: 'O serviço de pedidos está com uma configuração inválida. Fale com a loja pelo WhatsApp.' };
  }
  function idempotencyKey() {
    if (typeof crypto.randomUUID === 'function') return crypto.randomUUID();
    const bytes = crypto.getRandomValues(new Uint8Array(16));
    bytes[6] = (bytes[6] & 15) | 64;
    bytes[8] = (bytes[8] & 63) | 128;
    const hex = [...bytes].map(value => value.toString(16).padStart(2, '0')).join('');
    return `${hex.slice(0, 8)}-${hex.slice(8, 12)}-${hex.slice(12, 16)}-${hex.slice(16, 20)}-${hex.slice(20)}`;
  }
  function validResponse(order) {
    return order && typeof order.id === 'string' && order.id.length > 0 && order.id.length <= 100 &&
      Number.isSafeInteger(order.total_cents) && order.total_cents >= 0 &&
      ['new', 'confirmed', 'preparing', 'out_for_delivery', 'delivered', 'cancelled'].includes(order.status);
  }
  function trackingURL(order, apiBase) {
    if (typeof order.tracking_url !== 'string' || !order.tracking_url) return '';
    try {
      const url = new URL(order.tracking_url, new URL(`${apiBase}/`, location.origin));
      const trustedOrigin = new URL(apiBase, location.origin).origin;
      return url.origin === trustedOrigin && (url.protocol === 'https:' || url.origin === location.origin) ? url.href : '';
    } catch { return ''; }
  }
  function setPending(value) {
    pending = value;
    button.disabled = value || checkout.snapshot().length === 0;
    button.setAttribute('aria-busy', String(value));
    button.textContent = value ? 'Registrando solicitação…' : 'Continuar no WhatsApp ↗';
    // The cart render also sets disabled. Preserve this guard during its mutations.
  }
  new MutationObserver(() => {
    if (pending && !button.disabled) button.disabled = true;
  }).observe(button, { attributes: true, attributeFilter: ['disabled'] });

  async function submitOrder() {
    if (pending) return;
    const schedule = validateSchedule();
    if (schedule.error) {
      announce(schedule.error, true);
      form.reportValidity();
      return;
    }
    const prepared = checkout.prepare(schedule);
    if (!prepared) return;
    const configuration = apiConfiguration();
    if (configuration.error) {
      announce(configuration.error, true);
      return;
    }
    if (!configuration.url) {
      announce('');
      checkout.openWhatsApp(prepared.message);
      return;
    }
    const payload = {
      items: prepared.items, customer: prepared.customer, delivery: prepared.delivery,
      payment_method: prepared.paymentMethod, notes: prepared.notes,
      requested_for: prepared.requestedFor, coupon_code: null,
    };
    const fingerprint = JSON.stringify({ apiBase: configuration.url, payload });
    if (!attempt || attempt.fingerprint !== fingerprint) {
      attempt = { fingerprint, key: idempotencyKey(), response: null };
    }
    const currentAttempt = attempt;
    setPending(true);
    announce('Registrando sua solicitação. A loja ainda precisa confirmar disponibilidade, entrega e pagamento.');
    whatsappLink.hidden = true;
    trackingLink.hidden = true;
    // Reserve the customer-initiated tab before awaiting the request.
    let popup = null;
    try { popup = window.open('about:blank', '_blank'); } catch {}
    if (popup) {
      try { popup.opener = null; } catch {}
    }
    const controller = new AbortController();
    const timeout = setTimeout(() => controller.abort(), 15000);
    try {
      let order = currentAttempt.response;
      if (!order) {
        const response = await fetch(`${configuration.url}/orders`, {
          method: 'POST', headers: { 'Content-Type': 'application/json' },
          body: JSON.stringify({ ...payload, idempotency_key: currentAttempt.key }),
          signal: controller.signal,
        });
        if (!response.ok) throw new Error('registration-failed');
        order = await response.json();
        if (!validResponse(order)) throw new Error('invalid-response');
        currentAttempt.response = order;
      }
      const url = 'https://wa.me/5544991748318?text=' + encodeURIComponent(checkout.message(prepared, order));
      whatsappLink.href = url;
      whatsappLink.hidden = false;
      const tracking = trackingURL(order, configuration.url);
      if (tracking) {
        trackingLink.href = tracking;
        trackingLink.hidden = false;
      }
      announce(`Solicitação ${order.id} registrada. Valor dos produtos: ${checkout.money(order.total_cents)}. Revise a mensagem no WhatsApp; a loja precisa confirmar disponibilidade, entrega e pagamento.`);
      if (popup) {
        try { popup.location.replace(url); } catch { try { popup.close(); } catch {} }
      }
    } catch {
      if (popup) { try { popup.close(); } catch {} }
      announce('Não foi possível registrar a solicitação. Nenhum pagamento foi realizado. Confira a conexão e tente novamente; seus itens e endereço foram preservados.', true);
    } finally {
      clearTimeout(timeout);
      setPending(false);
    }
  }

  const normalize = text => text.toLocaleLowerCase('pt-BR').normalize('NFD').replace(/[\u0300-\u036f]/g, '').replace(/[^a-z0-9 ]/g, ' ').replace(/\s+/g, ' ').trim();
  function answer(question) {
    const input = normalize(question);
    const catalog = checkout.catalog();
    const product = [...catalog].sort((a, b) => b.name.length - a.name.length).find(item => {
      const name = normalize(item.name);
      return (` ${input} `).includes(` ${name} `);
    });
    if (product) {
      return `${product.name}: ${checkout.money(product.priceCents)}.${product.description ? ' ' + product.description : ''} A loja confirma a disponibilidade.`;
    }
    if (/horario|funciona|aberto|abre|fecha|hours/.test(input)) {
      return 'A Sahara atende somente por delivery, de segunda a domingo, das 18h às 23h, no horário de Maringá. Você pode solicitar um agendamento para os próximos 7 dias; a loja confirma a data e o horário.';
    }
    if (/pagar|pagamento|pix|cartao|dinheiro|payment/.test(input)) {
      return 'Você pode indicar Pix, dinheiro, cartão de crédito ou cartão de débito. Para Pix, combine os dados com a loja pelo WhatsApp. O cardápio não cobra seu cartão nem confirma pagamento online.';
    }
    if (/entrega|endereco|localizacao|delivery|frete|taxa/.test(input)) {
      return 'Atendemos somente por delivery, com entrega grátis na região de Maringá. Digite rua, número e bairro ou use sua localização, confira o ponto no mapa e confirme. Informe sempre o número da casa. A loja confirma se atende o endereço e o prazo.';
    }
    if (/pedido|pedir|sacola|agendar|agendamento|order/.test(input)) {
      return 'Toque em + para escolher os produtos e revise a sacola. Informe o endereço ou confirme sua localização, escolha como deseja pagar e toque em Continuar no WhatsApp. Para agendar, escolha uma data e um horário entre 18h e 23h. Revise e envie a mensagem; a loja confirma a solicitação. Você não precisa criar uma conta.';
    }
    if (/cardapio|sabores|menu|preco|valor|combos|bebidas/.test(input)) {
      const categories = [...new Set(catalog.map(item => item.category))].join(', ');
      const favorites = ['carne', 'frango', 'calabresa', 'queijo'].map(id => catalog.find(item => item.id === id)).filter(Boolean).map(item => `${item.name}: ${checkout.money(item.priceCents)}`).join('; ');
      return `O cardápio tem ${catalog.length} produtos em ${categories}. ${favorites}. Para ver outro preço, escreva o nome do produto. A loja confirma a disponibilidade.`;
    }
    return 'Posso ajudar com o cardápio e os preços, horário, entrega, pagamento ou como montar o pedido. Para outras dúvidas e confirmação do pedido, toque em Falar com a loja no WhatsApp.';
  }

  const helpButton = document.createElement('button');
  helpButton.id = 'open-order-help';
  helpButton.type = 'button';
  helpButton.className = 'ordering-help-open';
  helpButton.setAttribute('aria-haspopup', 'dialog');
  helpButton.setAttribute('aria-controls', 'help-dialog');
  helpButton.textContent = 'Precisa de ajuda com o pedido?';
  document.querySelector('.quote-note').insertAdjacentElement('beforebegin', helpButton);
  const dialog = document.createElement('dialog');
  dialog.id = 'help-dialog';
  dialog.className = 'ordering-help-dialog';
  dialog.setAttribute('aria-labelledby', 'help-title');
  dialog.innerHTML = `
    <div class="ordering-help-heading"><h2 id="help-title">Ajuda com seu pedido</h2><button id="close-order-help" type="button" aria-label="Fechar ajuda">×</button></div>
    <p class="ordering-note">Respostas rápidas sobre o cardápio. A loja confirma pedidos e disponibilidade pelo WhatsApp.</p>
    <div class="ordering-help-topics" aria-label="Dúvidas frequentes">
      <button type="button" data-help-topic="menu">Cardápio e preços</button>
      <button type="button" data-help-topic="hours">Horário</button>
      <button type="button" data-help-topic="delivery">Entrega e endereço</button>
      <button type="button" data-help-topic="payment">Pagamento</button>
      <button type="button" data-help-topic="order">Como pedir</button>
    </div>
    <div id="help-responses" class="ordering-help-responses" role="log" aria-live="polite" aria-relevant="additions"></div>
    <form id="help-form"><label for="help-question">Sua dúvida</label><div class="ordering-help-input"><input id="help-question" type="text" maxlength="240" required autofocus placeholder="Ex.: Quanto custa a carne?"><button type="submit">Perguntar</button></div></form>
    <a class="ordering-human-help" href="https://wa.me/5544991748318" target="_blank" rel="noopener noreferrer">Falar com a loja no WhatsApp ↗</a>`;
  document.body.append(dialog);
  let previousFocus = null;
  helpButton.addEventListener('click', () => {
    previousFocus = document.activeElement;
    dialog.showModal();
  });
  dialog.querySelector('#close-order-help').addEventListener('click', () => dialog.close());
  dialog.addEventListener('close', () => previousFocus?.focus({ preventScroll: true }));
  dialog.addEventListener('click', event => {
    if (event.target !== dialog) return;
    const rectangle = dialog.getBoundingClientRect();
    if (event.clientX < rectangle.left || event.clientX > rectangle.right || event.clientY < rectangle.top || event.clientY > rectangle.bottom) dialog.close();
  });
  const responses = dialog.querySelector('#help-responses');
  function showAnswer(question) {
    const exchange = document.createElement('div');
    exchange.className = 'ordering-help-exchange';
    const prompt = document.createElement('p');
    prompt.className = 'ordering-help-prompt';
    prompt.textContent = question;
    const reply = document.createElement('p');
    reply.textContent = answer(question);
    exchange.append(prompt, reply);
    responses.append(exchange);
    while (responses.children.length > 6) responses.firstElementChild.remove();
    exchange.scrollIntoView({ behavior: 'instant', block: 'nearest' });
  }
  const topics = { menu: 'Cardápio e preços', hours: 'Horário', delivery: 'Entrega e endereço', payment: 'Pagamento', order: 'Como fazer um pedido' };
  dialog.querySelectorAll('[data-help-topic]').forEach(topic => topic.addEventListener('click', () => showAnswer(topics[topic.dataset.helpTopic])));
  const questionField = dialog.querySelector('#help-question');
  dialog.querySelector('#help-form').addEventListener('submit', event => {
    event.preventDefault();
    const question = questionField.value.trim();
    if (!question) {
      questionField.setCustomValidity('Escreva sua dúvida.');
      questionField.reportValidity();
      return;
    }
    showAnswer(question);
    questionField.value = '';
    questionField.focus({ preventScroll: true });
  });
  questionField.addEventListener('input', () => questionField.setCustomValidity(''));

  window.saharaOrdering = { checkout: submitOrder, validateSchedule, businessDate, answer };
})();
