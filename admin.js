'use strict';

(() => {
  const $ = id => document.getElementById(id);
  const state = { csrf: null, view: 'orders', catalog: [], customers: [], drivers: [], orderFilter: 'active', generation: 0, fieldIds: new Map() };
  const statuses = { new: 'Novo', confirmed: 'Confirmado', preparing: 'Em preparo', out_for_delivery: 'Saiu para entrega', delivered: 'Entregue', cancelled: 'Cancelado' };
  const transitions = { new: ['confirmed', 'cancelled'], confirmed: ['preparing', 'cancelled'], preparing: ['out_for_delivery', 'cancelled'], out_for_delivery: ['delivered', 'cancelled'], delivered: [], cancelled: [] };
  const views = {
    orders: ['Pedidos e cozinha', 'Acompanhe os pedidos de delivery, prepare as comandas e organize a entrega.'],
    pos: ['Novo pedido · PDV', 'Registre um pedido recebido por telefone ou WhatsApp usando os preços do cardápio.'],
    dashboard: ['Resultados', 'Acompanhe os pedidos e as vendas com pagamento recebido.'],
    inventory: ['Estoque', 'Registre insumos e entradas ou saídas para acompanhar a disponibilidade.'],
    cash: ['Caixa', 'Abra e feche o caixa e registre suas movimentações financeiras.'],
    receivables: ['Fiado', 'Acompanhe valores a receber e registre os pagamentos recebidos.'],
    customers: ['Clientes e RFV', 'Histórico, recência, frequência e valor de compra dos clientes da loja.'],
    coupons: ['Cupons', 'Crie benefícios com limites de uso, validade e valor mínimo de pedido.'],
    loyalty: ['Fidelidade', 'Cadastre benefícios e registre o resgate dos pontos dos clientes.'],
    campaigns: ['Campanhas', 'Prepare mensagens para clientes que autorizaram contato. O envio exige uma integração ativa.'],
    drivers: ['Entregadores', 'Cadastre os entregadores para atribuí-los aos pedidos.'],
    reviews: ['Avaliações', 'Acompanhe os comentários e as notas recebidas dos clientes.'],
    integrations: ['Integrações', 'Confira quais serviços estão disponíveis e o que falta para ativá-los.']
  };
  const money = cents => new Intl.NumberFormat('pt-BR', { style: 'currency', currency: 'BRL' }).format((Number(cents) || 0) / 100);
  const date = value => value && !Number.isNaN(new Date(value).getTime()) ? new Intl.DateTimeFormat('pt-BR', { dateStyle: 'short', timeStyle: 'short', timeZone: 'America/Sao_Paulo' }).format(new Date(value)) : '—';
  const numeric = value => Number.isFinite(Number(value)) ? Number(value) : 0;
  const cents = value => Math.round(numeric(value) * 100);
  const list = (data, key) => Array.isArray(data?.[key]) ? data[key] : [];

  function el(tag, text, className) {
    const node = document.createElement(tag);
    if (text !== undefined && text !== null) node.textContent = String(text);
    if (className) node.className = className;
    return node;
  }

  function showNotice(message, error = false) {
    $('notice').textContent = message;
    $('notice').className = `notice${error ? ' error' : ''}`;
    $('notice').hidden = !message;
  }

  async function api(path, options = {}) {
    const method = options.method || 'GET';
    const headers = { Accept: 'application/json' };
    if (options.body !== undefined) headers['Content-Type'] = 'application/json';
    if (!['GET', 'HEAD'].includes(method) && state.csrf) headers['X-Sahara-CSRF'] = state.csrf;
    const response = await fetch(`/api${path}`, { method, headers, credentials: 'same-origin', cache: 'no-store', body: options.body === undefined ? undefined : JSON.stringify(options.body) });
    let data;
    try { data = await response.json(); } catch { throw new Error('O servidor não respondeu como esperado. Confira se o backend está ativo.'); }
    if (!response.ok) {
      if (response.status === 401 && path !== '/admin/login' && path !== '/admin/session') {
        state.csrf = null;
        setAccess('login');
      }
      let detail = typeof data.detail === 'string' ? data.detail : (Array.isArray(data.detail) ? data.detail.map(item => typeof item.msg === 'string' ? item.msg : 'Campo inválido.').join(' ') : (typeof data.error === 'string' ? data.error : `Não foi possível concluir a operação (${response.status}). Confira os campos e tente novamente.`));
      if (Array.isArray(data.fields)) detail += ` ${data.fields.map(item => `${item.field || 'Campo'}: ${item.message || 'inválido'}`).join(' ')}`;
      throw new Error(detail);
    }
    return data;
  }

  function setAccess(mode) {
    $('activation').hidden = mode !== 'activation';
    $('login-panel').hidden = mode !== 'login';
    $('workspace').hidden = mode !== 'workspace';
    $('logout').hidden = mode !== 'workspace';
  }

  async function connect() {
    setAccess('activation');
    $('retry').hidden = true;
    $('activation-title').textContent = 'Conectando ao sistema';
    $('activation-message').textContent = 'Verificando a disponibilidade do servidor seguro.';
    try {
      const health = await api('/health');
      if (!health.ready) throw new Error('O servidor de gestão está indisponível. Verifique a configuração e tente novamente.');
      if (!health.admin_configured) {
        $('activation-title').textContent = 'Ativação da gestão pendente';
        $('activation-message').textContent = 'O servidor está ativo, mas a senha administrativa ainda precisa ser configurada pelo responsável pela hospedagem. Nenhum dado de gestão foi carregado.';
        $('retry').hidden = false;
        return;
      }
      try {
        const session = await api('/admin/session');
        if (!session.csrf_token) throw new Error('A sessão não pôde ser validada. Entre novamente.');
        state.csrf = session.csrf_token;
        setAccess('workspace');
        await renderView();
      } catch {
        state.csrf = null;
        setAccess('login');
      }
    } catch {
      $('activation-title').textContent = 'Ativação da gestão pendente';
      $('activation-message').textContent = 'Este painel precisa do servidor de gestão seguro na mesma hospedagem, em /api. O GitHub Pages serve somente o cardápio e não executa esse servidor. Os pedidos e os dados da loja não estão disponíveis nesta página até a ativação.';
      $('retry').hidden = false;
    }
  }

  function panel(title, help) {
    const node = el('section', undefined, 'panel');
    node.append(el('h2', title));
    if (help) node.append(el('p', help, 'help'));
    return node;
  }

  function badge(text, style = '') { return el('span', text, `badge ${style}`); }

  function button(label, handler, style = 'small-button') {
    const node = el('button', label, style);
    node.type = 'button';
    node.addEventListener('click', () => perform(node, handler));
    return node;
  }

  async function perform(control, action) {
    if (control.disabled) return;
    control.disabled = true;
    try { await action(); } catch (error) { showNotice(error.message || 'Não foi possível concluir a operação.', true); }
    finally { control.disabled = false; }
  }

  function table(title, columns, rows, emptyMessage) {
    const wrapper = el('div', undefined, 'table-scroll');
    if (!rows.length) {
      wrapper.append(el('p', emptyMessage || 'Nenhum registro por enquanto.', 'empty'));
      return wrapper;
    }
    wrapper.tabIndex = 0;
    wrapper.setAttribute('role', 'region');
    wrapper.setAttribute('aria-label', title);
    const node = el('table');
    const caption = el('caption', title); caption.className = 'sr-only';
    const head = el('thead'); const headRow = el('tr');
    columns.forEach(column => { const th = el('th', column[0]); th.scope = 'col'; headRow.append(th); });
    head.append(headRow);
    const body = el('tbody');
    for (const row of rows) {
      const tr = el('tr');
      for (const column of columns) {
        const td = el('td');
        const value = column[1](row);
        if (value instanceof Node) td.append(value); else td.textContent = value == null ? '—' : String(value);
        tr.append(td);
      }
      body.append(tr);
    }
    node.append(caption, head, body); wrapper.append(node); return wrapper;
  }

  function field(form, name, label, options = {}) {
    const wrap = el('div', undefined, `field${options.wide ? ' wide' : ''}`);
    const baseId = `field-${state.view}-${name}`;
    const count = (state.fieldIds.get(baseId) || 0) + 1; state.fieldIds.set(baseId, count);
    const id = count === 1 ? baseId : `${baseId}-${count}`;
    let input;
    if (options.type === 'select') {
      input = el('select');
      for (const choice of options.choices || []) {
        const option = el('option', choice[1]); option.value = choice[0]; input.append(option);
      }
    } else if (options.type === 'textarea') { input = el('textarea'); input.rows = 3; }
    else { input = el('input'); input.type = options.type || 'text'; }
    input.id = id; input.name = name;
    if (options.required) input.required = true;
    if (options.value !== undefined) input.value = String(options.value);
    if (options.min !== undefined) input.min = String(options.min);
    if (options.max !== undefined) input.max = String(options.max);
    if (options.step !== undefined) input.step = String(options.step);
    if (options.maxLength !== undefined) input.maxLength = options.maxLength;
    if (options.placeholder) input.placeholder = options.placeholder;
    if (options.type === 'checkbox') {
      const labelNode = el('label', undefined, 'checkbox-label'); labelNode.htmlFor = id;
      input.checked = Boolean(options.checked); labelNode.append(input, el('span', label)); wrap.append(labelNode);
    } else {
      const labelNode = el('label', label); labelNode.htmlFor = id; wrap.append(labelNode, input);
    }
    form.append(wrap);
    return input;
  }

  function createForm(label, submit, help) {
    const form = el('form');
    const grid = el('div', undefined, 'form-grid');
    const actions = el('div', undefined, 'form-actions');
    const control = el('button', label, 'primary'); control.type = 'submit';
    actions.append(control);
    if (help) actions.append(el('p', help));
    form.append(grid, actions);
    form.addEventListener('submit', event => {
      event.preventDefault();
      if (!form.reportValidity()) return;
      perform(control, () => submit(new FormData(form), form));
    });
    return { form, grid };
  }

  function get(data, name) { return String(data.get(name) || '').trim(); }

  async function saved(path, body, message, method = 'POST') {
    await api(path, { method, body });
    showNotice(message);
    await renderView();
  }

  function selection(label, choices, onChange) {
    const select = el('select'); select.setAttribute('aria-label', label);
    choices.forEach(([value, text]) => { const option = el('option', text); option.value = value; select.append(option); });
    if (onChange) select.addEventListener('change', onChange);
    return select;
  }

  async function loadOrders(screen) {
    const [ordersData, driverData] = await Promise.all([api('/admin/orders'), api('/admin/drivers')]);
    state.drivers = list(driverData, 'drivers');
    const all = list(ordersData, 'orders');
    const filter = el('div', undefined, 'order-filter');
    const select = selection('Filtrar pedidos por situação', [['active', 'Em andamento'], ['all', 'Todos os pedidos'], ...Object.entries(statuses)], () => { state.orderFilter = select.value; renderView(); });
    select.value = state.orderFilter;
    filter.append(el('span', `${all.length} pedido${all.length === 1 ? '' : 's'} registrado${all.length === 1 ? '' : 's'}`), select);
    screen.append(filter);
    const orders = all.filter(order => state.orderFilter === 'all' || (state.orderFilter === 'active' ? !['delivered', 'cancelled'].includes(order.status) : order.status === state.orderFilter));
    orders.sort((a, b) => new Date(a.requested_for || a.created_at) - new Date(b.requested_for || b.created_at));
    if (!orders.length) { screen.append(el('p', 'Nenhum pedido nesta situação. Pedidos registrados no servidor aparecerão aqui.', 'empty')); return; }
    const cards = el('div', undefined, 'cards');
    for (const order of orders) {
      const card = el('article', undefined, 'order-card');
      const top = el('div', undefined, 'order-top');
      top.append(el('h2', `Pedido #${order.id}`), badge(statuses[order.status] || order.status)); card.append(top);
      card.append(el('p', `${order.customer_name || 'Cliente'}${order.customer_phone ? ` · ${order.customer_phone}` : ''}`));
      card.append(el('p', `Recebido: ${date(order.created_at)}`, 'order-meta'));
      if (order.requested_for) card.append(el('p', `Agendado: ${date(order.requested_for)}`, 'badge warning'));
      const items = el('ul');
      for (const item of order.items || []) items.append(el('li', `${item.quantity} × ${item.name} · ${money(item.price_cents * item.quantity)}`));
      card.append(items);
      const address = order.delivery || {};
      card.append(el('p', [address.street, address.number, address.neighborhood, address.complement].filter(Boolean).join(', ') || 'Endereço não informado', 'order-address'));
      if (address.location && Number.isFinite(Number(address.location.latitude)) && Number.isFinite(Number(address.location.longitude))) {
        const link = el('a', 'Abrir ponto de entrega ↗'); link.href = `https://www.google.com/maps?q=${encodeURIComponent(address.location.latitude)},${encodeURIComponent(address.location.longitude)}`; link.target = '_blank'; link.rel = 'noopener noreferrer'; card.append(link);
      }
      if (order.notes) card.append(el('p', `Observação: ${order.notes}`));
      if (order.discount_cents) card.append(el('p', `Desconto: ${money(order.discount_cents)}`, 'order-meta'));
      const total = el('p', undefined, 'order-total'); total.append(el('span', 'Total'), el('strong', money(order.total_cents))); card.append(total);
      card.append(el('p', `${order.payment_method || 'Pagamento'} · ${order.payment_status === 'paid' ? 'Pago' : 'A receber'}`, 'order-meta'));
      const actions = el('div', undefined, 'order-actions');
      const nextStatuses = transitions[order.status] || [];
      if (nextStatuses.length) {
        const statusSelect = selection(`Próxima situação do pedido ${order.id}`, nextStatuses.map(key => [key, statuses[key]]));
        actions.append(statusSelect, button('Atualizar situação', async () => {
          if (statusSelect.value === 'cancelled' && !window.confirm(`Cancelar o pedido #${order.id}?`)) return;
          await saved(`/admin/orders/${encodeURIComponent(order.id)}`, { status: statusSelect.value }, 'Situação do pedido atualizada.', 'PATCH');
        }));
      }
      if (order.payment_status !== 'paid' && order.status !== 'cancelled') actions.append(button('Registrar pagamento', async () => {
        if (!window.confirm(`Confirmar que o pagamento de ${money(order.total_cents)} do pedido #${order.id} foi recebido?`)) return;
        await saved(`/admin/orders/${encodeURIComponent(order.id)}/payment`, { status: 'paid' }, 'Pagamento recebido e registrado.');
      }));
      actions.append(button('Imprimir comanda', () => printOrder(order)));
      const drivers = selection(`Entregador do pedido ${order.id}`, [['', 'Escolher entregador'], ...state.drivers.filter(driver => driver.active !== false).map(driver => [driver.id, driver.name])]);
      drivers.value = String(order.courier_id || '');
      if (state.drivers.length && !['delivered', 'cancelled'].includes(order.status)) actions.append(drivers, button('Atribuir entrega', async () => {
        await saved(`/admin/orders/${encodeURIComponent(order.id)}/driver`, { driver_id: drivers.value || null }, drivers.value ? 'Entregador atribuído.' : 'Entregador removido.', 'PATCH');
      }));
      card.append(actions); cards.append(card);
    }
    screen.append(cards);
  }

  function printOrder(order) {
    const target = $('print-target'); target.replaceChildren();
    target.append(el('h1', 'SAHARA ESFIHAS'), el('p', 'Somente delivery · (44) 99174-8318'), el('hr'), el('h2', `Pedido #${order.id}`), el('p', date(order.created_at)));
    if (order.requested_for) target.append(el('p', `Agendado: ${date(order.requested_for)}`));
    target.append(el('p', `${order.customer_name || 'Cliente'} · ${order.customer_phone || ''}`));
    const address = order.delivery || {};
    target.append(el('p', [address.street, address.number, address.neighborhood, address.complement].filter(Boolean).join(', ')), el('hr'));
    if (address.location && Number.isFinite(address.location.latitude) && Number.isFinite(address.location.longitude)) target.append(el('p', `Localização: https://www.google.com/maps?q=${address.location.latitude},${address.location.longitude}`));
    for (const item of order.items || []) { const line = el('p', undefined, 'receipt-line'); line.append(el('span', `${item.quantity} × ${item.name}`), el('span', money(item.quantity * item.price_cents))); target.append(line); }
    if (order.notes) target.append(el('p', `Observação: ${order.notes}`));
    if (order.discount_cents) target.append(el('p', `Desconto: ${money(order.discount_cents)}`));
    target.append(el('hr'), el('strong', `TOTAL ${money(order.total_cents)}`), el('p', `${order.payment_method} · ${order.payment_status === 'paid' ? 'Pago' : 'A receber'}`));
    window.print();
  }

  async function loadPos(screen) {
    const data = await api('/catalog'); state.catalog = list(data, 'products');
    if (!state.catalog.length) { screen.append(el('p', 'O cardápio não tem produtos disponíveis. Verifique o cadastro antes de registrar pedidos.', 'empty')); return; }
    const p = panel('Registrar pedido de delivery');
    const quantityInputs = [];
    let idempotencyKey = crypto.randomUUID(); let orderFingerprint = '';
    const { form, grid } = createForm('Registrar pedido', async data => {
      const items = quantityInputs.map(([product, input]) => ({ id: product.id, quantity: Number(input.value) })).filter(item => item.quantity > 0);
      if (!items.length) throw new Error('Adicione ao menos um produto ao pedido.');
      const payload = { items, customer: { name: get(data, 'name'), phone: get(data, 'phone'), marketing_opt_in: false }, delivery: { street: get(data, 'street'), number: get(data, 'number'), neighborhood: get(data, 'neighborhood'), complement: get(data, 'complement'), location: null }, payment_method: get(data, 'payment_method'), notes: get(data, 'notes'), requested_for: null, coupon_code: get(data, 'coupon_code') || null };
      const fingerprint = JSON.stringify(payload);
      if (orderFingerprint && orderFingerprint !== fingerprint) idempotencyKey = crypto.randomUUID();
      orderFingerprint = fingerprint;
      await api('/admin/orders', { method: 'POST', body: { ...payload, idempotency_key: idempotencyKey } });
      idempotencyKey = crypto.randomUUID();
      showNotice('Pedido registrado. Acompanhe a preparação em Pedidos e cozinha.');
      state.view = 'orders'; state.orderFilter = 'active'; await renderView();
    }, 'Registre o pagamento recebido separadamente na tela de pedidos.');
    field(grid, 'name', 'Nome do cliente', { required: true, maxLength: 100 });
    field(grid, 'phone', 'Telefone do cliente', { type: 'tel', required: true, maxLength: 25 });
    field(grid, 'street', 'Rua', { required: true, maxLength: 160 });
    field(grid, 'number', 'Número', { required: true, maxLength: 20 });
    field(grid, 'neighborhood', 'Bairro', { required: true, maxLength: 100 });
    field(grid, 'complement', 'Complemento', { maxLength: 160 });
    field(grid, 'payment_method', 'Forma de pagamento', { type: 'select', choices: [['Pix', 'Pix combinado com a loja'], ['Dinheiro', 'Dinheiro na entrega'], ['Cartão de crédito', 'Cartão de crédito na entrega'], ['Cartão de débito', 'Cartão de débito na entrega']] });
    field(grid, 'coupon_code', 'Cupom (opcional)', { maxLength: 40 });
    field(grid, 'notes', 'Observações', { type: 'textarea', wide: true, maxLength: 500 });
    const products = el('div', undefined, 'field wide'); products.append(el('h3', 'Produtos'));
    const total = el('div', undefined, 'product-summary'); const totalValue = el('strong', money(0)); total.append(el('span', 'Subtotal dos produtos'), totalValue);
    for (const product of state.catalog) {
      const row = el('div', undefined, 'product-entry'); const input = el('input');
      input.type = 'number'; input.min = '0'; input.max = '99'; input.step = '1'; input.value = '0'; input.id = `product-${quantityInputs.length}`;
      const labelNode = el('label', product.name); labelNode.htmlFor = input.id; labelNode.append(el('small', `${product.category} · ${money(product.price_cents)}`));
      quantityInputs.push([product, input]);
      input.addEventListener('input', () => { totalValue.textContent = money(quantityInputs.reduce((sum, [item, control]) => sum + Math.max(0, numeric(control.value)) * item.price_cents, 0)); });
      row.append(labelNode, input); products.append(row);
    }
    products.append(total); grid.append(products); p.append(form); screen.append(p);
  }

  async function loadDashboard(screen) {
    const data = await api('/admin/dashboard');
    const metrics = el('div', undefined, 'metrics');
    for (const [label, value] of [['Pedidos registrados', data.orders_count || 0], ['Vendas', money(data.sales_cents)], ['Ticket médio', money(data.ticket_cents)]]) { const card = el('div', undefined, 'metric'); card.append(el('p', label), el('strong', value)); metrics.append(card); }
    screen.append(metrics);
    const statusPanel = panel('Pedidos por situação'); statusPanel.append(table('Pedidos por situação', [['Situação', row => statuses[row.status] || row.status], ['Pedidos', row => row.count]], list(data, 'by_status')));
    const daily = panel('Vendas por dia'); daily.append(table('Vendas por dia', [['Data', row => row.date], ['Vendas', row => money(row.total_cents)]], list(data, 'daily_sales')));
    screen.append(statusPanel, daily);
    if ('conversion_rate' in data) {
      const p = panel('Conversão do cardápio', 'Proporção de pedidos pelo site em relação às visitas registradas.');
      p.append(table('Conversão do cardápio', [['Indicador', row => row[0]], ['Valor', row => row[1]]], [['Visitas registradas', data.views || 0], ['Conversão', data.conversion_rate == null ? 'Sem visitas registradas' : `${(numeric(data.conversion_rate) * 100).toFixed(1).replace('.', ',')}%`]])); screen.append(p);
    }
    if (data.conversion && typeof data.conversion === 'object') { const p = panel('Conversão do cardápio', 'Os números dependem dos eventos efetivamente registrados no sistema.'); p.append(table('Conversão', [['Indicador', row => row[0]], ['Valor', row => row[1] ?? '—']], Object.entries(data.conversion))); screen.append(p); }
  }

  async function loadInventory(screen) {
    const [data, catalog, recipeData] = await Promise.all([api('/admin/inventory'), api('/catalog'), api('/admin/recipes')]);
    const items = list(data, 'items');
    const p = panel('Itens em estoque', 'Use uma quantidade negativa para registrar saída e positiva para entrada. Informe sempre o motivo.');
    p.append(table('Estoque', [['Insumo', row => row.label], ['Quantidade', row => `${row.on_hand} ${row.unit}`], ['Limite mínimo', row => row.low_threshold], ['Situação', row => badge(numeric(row.on_hand) <= numeric(row.low_threshold) ? 'Repor estoque' : 'Disponível', numeric(row.on_hand) <= numeric(row.low_threshold) ? 'warning' : 'success')]], items));
    screen.append(p);
    const add = panel('Cadastrar item');
    const create = createForm('Cadastrar item', data => saved('/admin/inventory', { label: get(data, 'label'), unit: get(data, 'unit'), on_hand: numeric(get(data, 'on_hand')), low_threshold: numeric(get(data, 'low_threshold')), product_id: get(data, 'product_id') || null }, 'Item cadastrado.'));
    field(create.grid, 'label', 'Nome do insumo ou produto', { required: true, maxLength: 100 }); field(create.grid, 'unit', 'Unidade', { type: 'select', choices: [['un', 'Unidade'], ['g', 'Grama'], ['kg', 'Quilograma'], ['ml', 'Mililitro'], ['l', 'Litro']] });
    field(create.grid, 'on_hand', 'Quantidade inicial', { type: 'number', min: 0, step: '0.001', value: 0, required: true }); field(create.grid, 'low_threshold', 'Avisar reposição abaixo de', { type: 'number', min: 0, step: '0.001', value: 0, required: true });
    field(create.grid, 'product_id', 'Vincular a um produto (opcional)', { type: 'select', wide: true, choices: [['', 'Sem vínculo'], ...list(catalog, 'products').map(product => [product.id, product.name])] }); add.append(create.form); screen.append(add);
    if (items.length) {
      const adjust = panel('Ajustar estoque'); const f = createForm('Registrar movimentação', data => saved(`/admin/inventory/${encodeURIComponent(get(data, 'id'))}/adjust`, { quantity: numeric(get(data, 'quantity')), reason: get(data, 'reason') }, 'Estoque atualizado.'));
      field(f.grid, 'id', 'Item', { type: 'select', choices: items.map(item => [item.id, `${item.label} (${item.unit})`]) }); field(f.grid, 'quantity', 'Quantidade (+ entrada / − saída)', { type: 'number', step: '0.001', required: true }); field(f.grid, 'reason', 'Motivo', { required: true, wide: true, maxLength: 300 }); adjust.append(f.form); screen.append(adjust);
      const recipePanel = panel('Fichas técnicas', 'Informe o consumo de cada ingrediente para uma unidade do produto. A ficha substitui o estoque direto do produto quando o pedido é confirmado. Quantidade zero remove o ingrediente da ficha.');
      const recipes = list(recipeData, 'recipes');
      recipePanel.append(table('Fichas técnicas', [['Produto', row => list(catalog, 'products').find(product => product.id === row.product_id)?.name || row.product_id], ['Ingredientes por unidade', row => row.components.map(component => `${component.label}: ${component.quantity} ${component.unit}`).join('; ')]], recipes, 'Nenhuma ficha técnica cadastrada.'));
      const components = [];
      const recipeForm = createForm('Salvar ficha técnica', data => saved('/admin/recipes', { product_id: get(data, 'recipe_product_id'), components: components.map(([item, input]) => ({ inventory_id: item.id, quantity: Number(input.value) })).filter(component => component.quantity > 0) }, 'Ficha técnica salva.'));
      const productSelect = field(recipeForm.grid, 'recipe_product_id', 'Produto da ficha técnica', { type: 'select', wide: true, choices: list(catalog, 'products').map(product => [product.id, product.name]) });
      for (const item of items) {
        const input = field(recipeForm.grid, `recipe-${item.id}`, `${item.label} (${item.unit} por unidade)`, { type: 'number', min: 0, step: '0.001', value: 0, required: true });
        components.push([item, input]);
      }
      const restoreRecipe = () => {
        const recipe = recipes.find(row => row.product_id === productSelect.value);
        components.forEach(([item, input]) => { input.value = String(recipe?.components.find(component => component.inventory_id === item.id)?.quantity || 0); });
      };
      productSelect.addEventListener('change', restoreRecipe); restoreRecipe();
      recipePanel.append(recipeForm.form); screen.append(recipePanel);
    }
  }

  async function loadCash(screen) {
    const [sessionData, entryData] = await Promise.all([api('/admin/cash/sessions'), api('/admin/cash/entries')]);
    const sessions = list(sessionData, 'sessions'); const entries = list(entryData, 'entries');
    const open = sessions.find(session => !session.closed_at && session.status !== 'closed');
    const sessionPanel = panel(open ? 'Caixa aberto' : 'Caixa fechado');
    const f = createForm(open ? 'Fechar caixa' : 'Abrir caixa', async data => {
      if (open && !window.confirm('Confirmar o fechamento do caixa com o valor contado?')) return;
      await saved('/admin/cash/sessions', open ? { action: 'close', session_id: open.id, closing_cents: cents(get(data, 'amount')) } : { action: 'open', opening_cents: cents(get(data, 'amount')) }, open ? 'Caixa fechado.' : 'Caixa aberto.');
    });
    field(f.grid, 'amount', open ? 'Valor contado no fechamento (R$)' : 'Saldo inicial (R$)', { type: 'number', min: 0, step: '0.01', value: 0, required: true }); sessionPanel.append(f.form); screen.append(sessionPanel);
    const movements = panel('Movimentações do caixa');
    movements.append(table('Movimentações', [['Data', row => date(row.created_at)], ['Tipo', row => ({ credit: 'Entrada', debit: 'Saída', supply: 'Suprimento', withdrawal: 'Sangria' }[row.kind] || row.kind)], ['Descrição', row => row.description], ['Valor', row => money(row.amount_cents)], ['Caixa', row => row.session_id || 'Sem sessão']], entries)); screen.append(movements);
    if (open) {
      const add = panel('Registrar movimentação'); const entry = createForm('Registrar no caixa', data => saved('/admin/cash/entries', { kind: get(data, 'kind'), amount_cents: cents(get(data, 'amount')), description: get(data, 'description'), session_id: open.id }, 'Movimentação registrada.'));
      field(entry.grid, 'kind', 'Tipo', { type: 'select', choices: [['credit', 'Entrada'], ['debit', 'Saída'], ['supply', 'Suprimento'], ['withdrawal', 'Sangria']] }); field(entry.grid, 'amount', 'Valor (R$)', { type: 'number', min: '0.01', step: '0.01', required: true }); field(entry.grid, 'description', 'Descrição', { required: true, wide: true, maxLength: 300 }); add.append(entry.form); screen.append(add);
    }
    const history = panel('Histórico de caixas'); history.append(table('Histórico de caixas', [['Caixa', row => `#${row.id}`], ['Abertura', row => date(row.opened_at || row.created_at)], ['Fechamento', row => date(row.closed_at)], ['Inicial', row => money(row.opening_cents)], ['Contado', row => row.closing_cents == null ? '—' : money(row.closing_cents)]], sessions)); screen.append(history);
  }

  async function loadReceivables(screen) {
    const [data, orderData] = await Promise.all([api('/admin/receivables'), api('/admin/orders')]); const rows = list(data, 'receivables');
    const availableOrders = list(orderData, 'orders').filter(order => order.status !== 'cancelled' && order.payment_status !== 'paid' && !rows.some(row => row.order_id === order.id));
    const p = panel('Valores a receber'); p.append(table('Fiado', [['Cliente', row => row.customer_name], ['Descrição', row => row.description], ['Valor', row => money(row.amount_cents)], ['Recebido', row => money(row.paid_cents)], ['Em aberto', row => money(row.balance_cents ?? row.amount_cents - (row.paid_cents || 0))]], rows)); screen.append(p);
    const add = panel('Registrar fiado'); const f = createForm('Registrar valor a receber', data => saved('/admin/receivables', { customer_name: get(data, 'customer_name'), phone: get(data, 'phone'), description: get(data, 'description'), amount_cents: cents(get(data, 'amount')), due_date: get(data, 'due_date') || null, order_id: get(data, 'order_id') || null }, 'Valor a receber registrado.'));
    field(f.grid, 'customer_name', 'Nome do cliente', { required: true, maxLength: 100 }); field(f.grid, 'phone', 'Telefone', { type: 'tel', maxLength: 25 }); field(f.grid, 'amount', 'Valor (R$)', { type: 'number', min: '0.01', step: '0.01', required: true }); field(f.grid, 'due_date', 'Vencimento (opcional)', { type: 'date' }); field(f.grid, 'description', 'Descrição', { required: true, wide: true, maxLength: 300 }); add.append(f.form); screen.append(add);
    const orderSelect = field(f.grid, 'order_id', 'Vincular a um pedido (opcional)', { type: 'select', wide: true, choices: [['', 'Fiado sem vínculo com pedido'], ...availableOrders.map(order => [order.id, `#${order.id} · ${order.customer_name || 'Cliente'} · ${money(order.total_cents)}`])] });
    orderSelect.addEventListener('change', () => {
      const order = availableOrders.find(row => row.id === orderSelect.value);
      if (!order) return;
      f.form.elements.namedItem('customer_name').value = order.customer_name || '';
      f.form.elements.namedItem('phone').value = order.customer_phone || '';
      f.form.elements.namedItem('amount').value = (order.total_cents / 100).toFixed(2);
      f.form.elements.namedItem('description').value = `Pedido #${order.id}`;
    });
    const unpaid = rows.filter(row => numeric(row.balance_cents ?? row.amount_cents - (row.paid_cents || 0)) > 0);
    if (unpaid.length) {
      const payments = panel('Registrar recebimento');
      let receiptKey = crypto.randomUUID(); let receiptFingerprint = '';
      const payment = createForm('Registrar pagamento recebido', data => {
        const payload = { amount_cents: cents(get(data, 'amount')), payment_method: get(data, 'payment_method'), description: get(data, 'description') };
        const fingerprint = JSON.stringify([get(data, 'id'), payload]);
        if (receiptFingerprint && receiptFingerprint !== fingerprint) receiptKey = crypto.randomUUID();
        receiptFingerprint = fingerprint;
        return saved(`/admin/receivables/${encodeURIComponent(get(data, 'id'))}/payments`, { ...payload, idempotency_key: receiptKey }, 'Recebimento registrado no fiado e no caixa.');
      });
      field(payment.grid, 'id', 'Fiado', { type: 'select', choices: unpaid.map(row => [row.id, `${row.customer_name} · ${money(row.balance_cents ?? row.amount_cents - (row.paid_cents || 0))}`]) }); field(payment.grid, 'amount', 'Valor recebido (R$)', { type: 'number', min: '0.01', step: '0.01', required: true }); field(payment.grid, 'payment_method', 'Forma de pagamento', { type: 'select', choices: [['dinheiro', 'Dinheiro'], ['pix', 'Pix'], ['cartao', 'Cartão'], ['outro', 'Outro']] }); field(payment.grid, 'description', 'Observação', { maxLength: 300 }); payments.append(payment.form); screen.append(payments);
    }
  }

  async function loadCustomers(screen) {
    const [data, rfvData] = await Promise.all([api('/admin/customers'), api('/admin/rfv')]); state.customers = list(data, 'customers');
    const p = panel('Clientes'); p.append(table('Clientes da loja', [['Nome', row => row.name], ['Telefone', row => row.phone], ['Contato promocional', row => row.marketing_opt_in ? 'Autorizado' : 'Não autorizado'], ['Pontos', row => row.points || 0]], state.customers)); screen.append(p);
    const add = panel('Cadastrar cliente'); const f = createForm('Cadastrar cliente', data => saved('/admin/customers', { name: get(data, 'name'), phone: get(data, 'phone'), marketing_opt_in: data.has('marketing_opt_in') }, 'Cliente cadastrado.'));
    field(f.grid, 'name', 'Nome', { required: true, maxLength: 120 }); field(f.grid, 'phone', 'Telefone', { type: 'tel', required: true, maxLength: 25 }); field(f.grid, 'marketing_opt_in', 'O cliente autorizou receber mensagens promocionais pelo WhatsApp.', { type: 'checkbox', wide: true }); add.append(f.form); screen.append(add);
    if (state.customers.length) {
      const edit = panel('Atualizar cliente', 'Registre a autorização de contato somente quando o cliente permitir. Desmarque a opção para revogar essa autorização.');
      const update = createForm('Salvar cliente', data => saved(`/admin/customers/${encodeURIComponent(get(data, 'edit_id'))}`, { name: get(data, 'edit_name'), phone: get(data, 'edit_phone'), marketing_opt_in: data.has('edit_marketing_opt_in') }, 'Cliente atualizado.', 'PATCH'));
      const selected = field(update.grid, 'edit_id', 'Cliente a atualizar', { type: 'select', wide: true, choices: state.customers.map(customer => [customer.id, `${customer.name} · ${customer.phone}`]) });
      const name = field(update.grid, 'edit_name', 'Nome atualizado', { required: true, maxLength: 120 });
      const phone = field(update.grid, 'edit_phone', 'Telefone atualizado', { type: 'tel', required: true, maxLength: 25 });
      const consent = field(update.grid, 'edit_marketing_opt_in', 'O cliente autorizou receber mensagens promocionais pelo WhatsApp.', { type: 'checkbox', wide: true });
      const restore = () => { const customer = state.customers.find(row => row.id === selected.value); name.value = customer?.name || ''; phone.value = customer?.phone || ''; consent.checked = Boolean(customer?.marketing_opt_in); };
      selected.addEventListener('change', restore); restore(); edit.append(update.form); screen.append(edit);
    }
    const rfv = panel('Matriz RFV', 'Recência: dias desde a última compra. Frequência: número de compras. Valor: total comprado.');
    rfv.append(table('Matriz RFV', [['Cliente', row => row.name || row.customer_name], ['Recência (dias)', row => row.recency_days ?? 'Sem compras'], ['Compras', row => row.frequency], ['Valor', row => money(row.monetary_cents)], ['Segmento', row => ({ vip: 'VIP', recorrente: 'Recorrente', novo: 'Novo', em_risco: 'Em risco', inativo: 'Inativo', sem_compras: 'Sem compras' }[row.segment] || row.segment)], ['Pontos', row => row.points || 0]], list(rfvData, 'customers'))); screen.append(rfv);
  }

  async function loadCoupons(screen) {
    const data = await api('/admin/coupons');
    const p = panel('Cupons cadastrados'); p.append(table('Cupons', [['Código', row => row.code], ['Benefício', row => row.kind === 'percent' ? `${row.value}%` : money(row.value)], ['Pedido mínimo', row => money(row.min_subtotal_cents)], ['Usos', row => `${row.uses || row.use_count || 0}${row.usage_limit ? ` / ${row.usage_limit}` : ''}`], ['Validade', row => date(row.expires_at)], ['Situação', row => row.active ? 'Ativo' : 'Inativo']], list(data, 'coupons'))); screen.append(p);
    const add = panel('Criar cupom'); const f = createForm('Criar cupom', data => {
      const kind = get(data, 'kind'); const value = numeric(get(data, 'value'));
      if (kind === 'percent' && (!Number.isInteger(value) || value > 100)) throw new Error('O desconto percentual deve ser um número inteiro de 1 a 100%.');
      return saved('/admin/coupons', { code: get(data, 'code').toUpperCase(), kind, value: kind === 'fixed' ? cents(value) : value, min_subtotal_cents: cents(get(data, 'minimum')), max_discount_cents: get(data, 'maximum') ? cents(get(data, 'maximum')) : null, usage_limit: get(data, 'usage_limit') ? numeric(get(data, 'usage_limit')) : null, per_customer_limit: get(data, 'per_customer_limit') ? numeric(get(data, 'per_customer_limit')) : null, expires_at: get(data, 'expires_at') ? new Date(get(data, 'expires_at')).toISOString() : null, active: data.has('active') }, 'Cupom criado.');
    }, 'A data de validade usa o fuso horário deste dispositivo.');
    field(f.grid, 'code', 'Código', { required: true, maxLength: 40 }); field(f.grid, 'kind', 'Tipo', { type: 'select', choices: [['percent', 'Percentual (%)'], ['fixed', 'Valor fixo (R$)']] }); field(f.grid, 'value', 'Desconto (% ou R$, conforme o tipo)', { type: 'number', min: '0.01', step: '0.01', required: true }); field(f.grid, 'minimum', 'Pedido mínimo (R$)', { type: 'number', min: 0, step: '0.01', value: 0 }); field(f.grid, 'maximum', 'Desconto máximo (R$, opcional)', { type: 'number', min: '0.01', step: '0.01' }); field(f.grid, 'usage_limit', 'Limite de usos (opcional)', { type: 'number', min: 1, step: 1 }); field(f.grid, 'per_customer_limit', 'Limite por cliente (opcional)', { type: 'number', min: 1, step: 1 }); field(f.grid, 'expires_at', 'Validade (opcional)', { type: 'datetime-local' }); field(f.grid, 'active', 'Cupom ativo', { type: 'checkbox', checked: true }); add.append(f.form); screen.append(add);
  }

  async function loadLoyalty(screen) {
    const [data, customers] = await Promise.all([api('/admin/loyalty/rewards'), api('/admin/customers')]); const rewards = list(data, 'rewards'); const customerList = list(customers, 'customers');
    const p = panel('Benefícios de fidelidade', data.points_rule || 'Um resgate desconta pontos e cria um registro do benefício.'); p.append(table('Benefícios', [['Benefício', row => row.name], ['Pontos necessários', row => row.points_cost]], rewards)); screen.append(p);
    const add = panel('Cadastrar benefício'); const f = createForm('Cadastrar benefício', data => saved('/admin/loyalty/rewards', { name: get(data, 'name'), points_cost: numeric(get(data, 'points_cost')) }, 'Benefício cadastrado.'));
    field(f.grid, 'name', 'Benefício', { required: true, maxLength: 120 }); field(f.grid, 'points_cost', 'Custo em pontos', { type: 'number', min: 1, step: 1, required: true }); add.append(f.form); screen.append(add);
    if (rewards.length && customerList.length) {
      const redeem = panel('Resgatar benefício'); const redemptionKey = crypto.randomUUID(); const redemption = createForm('Registrar resgate', async data => {
        if (!window.confirm('Confirmar o resgate e o desconto dos pontos deste cliente?')) return;
        await saved('/admin/loyalty/redeem', { customer_id: get(data, 'customer_id'), reward_id: get(data, 'reward_id'), idempotency_key: redemptionKey }, 'Resgate registrado.');
      });
      field(redemption.grid, 'customer_id', 'Cliente', { type: 'select', choices: customerList.map(customer => [customer.id, `${customer.name} · ${customer.points || 0} pontos`]) }); field(redemption.grid, 'reward_id', 'Benefício', { type: 'select', choices: rewards.map(reward => [reward.id, `${reward.name} · ${reward.points_cost} pontos`]) }); redeem.append(redemption.form); screen.append(redeem);
    }
  }

  async function loadCampaigns(screen) {
    const data = await api('/admin/campaigns');
    const p = panel('Rascunhos de campanhas', 'As mensagens ficam salvas como rascunho. Não há envio automático sem a ativação do provedor oficial do WhatsApp. Somente clientes com autorização de contato podem receber campanhas.');
    p.append(table('Campanhas', [['Título', row => row.name], ['Mensagem', row => row.message], ['Situação', () => 'Rascunho · não enviado'], ['Criado em', row => date(row.created_at)]], list(data, 'campaigns'))); screen.append(p);
    const add = panel('Preparar campanha'); const f = createForm('Salvar rascunho', data => saved('/admin/campaigns', { name: get(data, 'name'), kind: get(data, 'kind'), message: get(data, 'message') }, 'Rascunho salvo. Nenhuma mensagem foi enviada.'));
    field(f.grid, 'name', 'Título para organização', { required: true, maxLength: 120 }); field(f.grid, 'kind', 'Objetivo da campanha', { type: 'select', choices: [['promotion', 'Promoção'], ['loyalty', 'Fidelidade'], ['recovery', 'Reconquistar clientes']] }); field(f.grid, 'message', 'Mensagem para clientes com autorização', { type: 'textarea', required: true, wide: true, maxLength: 2000 }); add.append(f.form); screen.append(add);
  }

  async function loadDrivers(screen) {
    const data = await api('/admin/drivers');
    const p = panel('Entregadores', 'As rotas abrem no Google Maps. A lista mostra pedidos atribuídos em andamento e não acompanha a localização do entregador.');
    p.append(table('Entregadores da loja', [['Nome', row => row.name], ['Telefone', row => row.phone], ['Situação', row => row.active === false ? 'Inativo' : 'Ativo'], ['Entregas em andamento', row => {
      const deliveries = el('div');
      for (const order of row.orders || []) {
        const line = el('p', `#${order.id} · ${statuses[order.status] || order.status}`);
        if (order.maps_url) {
          const link = el('a', 'Abrir rota ↗'); link.href = order.maps_url; link.target = '_blank'; link.rel = 'noopener noreferrer'; line.append(el('br'), link);
        }
        deliveries.append(line);
      }
      if (!deliveries.childElementCount) deliveries.textContent = 'Sem entregas atribuídas';
      return deliveries;
    }]], list(data, 'drivers'))); screen.append(p);
    const add = panel('Cadastrar entregador'); const f = createForm('Cadastrar entregador', data => saved('/admin/drivers', { name: get(data, 'name'), phone: get(data, 'phone') }, 'Entregador cadastrado. Atribua as entregas na tela de pedidos.'));
    field(f.grid, 'name', 'Nome', { required: true, maxLength: 100 }); field(f.grid, 'phone', 'Telefone', { type: 'tel', required: true, maxLength: 25 }); add.append(f.form); screen.append(add);
  }

  async function loadReviews(screen) {
    const data = await api('/admin/reviews'); const p = panel('Avaliações recebidas');
    p.append(table('Avaliações dos clientes', [['Data', row => date(row.created_at)], ['Pedido', row => `#${row.order_id}`], ['Cliente', row => row.customer_name || 'Cliente'], ['Nota', row => `${row.rating} / 5`], ['Comentário', row => row.comment || 'Sem comentário']], list(data, 'reviews'), 'Nenhuma avaliação recebida por enquanto.')); screen.append(p);
  }

  async function loadIntegrations(screen) {
    const data = await api('/integrations'); const p = panel('Disponibilidade dos serviços', 'O status vem do servidor. Serviços externos exigem contas, credenciais e aprovação dos respectivos fornecedores.');
    const integrations = list(data, 'integrations');
    if (!integrations.length) p.append(el('p', 'Nenhuma integração informada pelo servidor.', 'empty'));
    for (const integration of integrations) {
      const item = el('article', undefined, 'integration'); const heading = el('h3', integration.label || integration.id);
      heading.append(badge(integration.available ? 'Disponível' : 'Ativação pendente', integration.available ? 'success' : 'warning'));
      item.append(heading, el('p', integration.reason || 'Consulte a configuração do serviço.')); p.append(item);
    }
    screen.append(p);
  }

  const loaders = { orders: loadOrders, pos: loadPos, dashboard: loadDashboard, inventory: loadInventory, cash: loadCash, receivables: loadReceivables, customers: loadCustomers, coupons: loadCoupons, loyalty: loadLoyalty, campaigns: loadCampaigns, drivers: loadDrivers, reviews: loadReviews, integrations: loadIntegrations };

  async function renderView() {
    if (!state.csrf) return;
    const generation = ++state.generation;
    const view = state.view;
    state.fieldIds.clear();
    $('screen-title').textContent = views[view][0]; $('screen-description').textContent = views[view][1];
    document.querySelectorAll('#navigation button').forEach(control => { if (control.dataset.view === view) control.setAttribute('aria-current', 'page'); else control.removeAttribute('aria-current'); });
    $('screen').replaceChildren(el('p', 'Carregando dados da loja…', 'loading')); $('screen').setAttribute('aria-busy', 'true');
    const fragment = document.createDocumentFragment();
    try {
      await loaders[view](fragment);
      if (generation === state.generation && state.csrf) $('screen').replaceChildren(fragment);
    } catch (error) {
      if (generation === state.generation) { const p = panel('Não foi possível carregar os dados'); p.append(el('p', error.message, 'danger-text'), button('Tentar novamente', renderView)); $('screen').replaceChildren(p); }
    } finally { if (generation === state.generation) $('screen').setAttribute('aria-busy', 'false'); }
  }

  for (const [key, [label]] of Object.entries(views)) {
    const nav = el('button', label); nav.type = 'button'; nav.dataset.view = key;
    nav.addEventListener('click', () => { state.view = key; showNotice(''); renderView(); $('content').focus({ preventScroll: true }); }); $('navigation').append(nav);
  }

  $('login-form').addEventListener('submit', event => {
    event.preventDefault(); const control = event.currentTarget.querySelector('button');
    perform(control, async () => {
      const password = $('admin-password').value;
      try {
        const data = await api('/admin/login', { method: 'POST', body: { password } });
        if (!data.csrf_token) throw new Error('O servidor não validou a sessão. Tente novamente.');
        state.csrf = data.csrf_token; showNotice(''); setAccess('workspace'); await renderView(); $('content').focus();
      } finally { $('admin-password').value = ''; }
    });
  });
  $('logout').addEventListener('click', () => perform($('logout'), async () => {
    await api('/admin/logout', { method: 'POST', body: {} }); state.csrf = null; state.generation++; $('screen').replaceChildren(); showNotice('Sessão encerrada.'); setAccess('login'); $('admin-password').focus();
  }));
  $('refresh').addEventListener('click', () => perform($('refresh'), renderView));
  $('retry').addEventListener('click', connect);
  window.addEventListener('online', () => { if (!$('activation').hidden) connect(); });
  connect();
})();
