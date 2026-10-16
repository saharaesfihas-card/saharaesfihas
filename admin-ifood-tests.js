'use strict';

// Private, manual workflows. Provider text is always data, never HTML.
(() => {
  const mounted = new WeakMap();
  const orderMounted = new WeakMap();
  const CAT = '/admin/ifood/test/catalog';
  const MERCHANT = '/admin/ifood/merchant';
  const pendingMerchant = value => ['processing', 'uncertain', 'unresolved'].includes(value);
  const pendingCatalog = value => ['sending', 'pending'].includes(value);
  const labels = {
    confirmed: 'Conferido no iFood', rejected: 'Recusado', failed: 'Não confirmado',
    pending: 'Aguardando conferência', sending: 'Em envio', processing: 'Em andamento', uncertain: 'Resultado incerto',
    accepted_pending: 'Aceito para processamento · etapa pendente', in_progress: 'Em andamento', refused: 'Recusado',
    already_requested: 'Solicitação já aceita · confira no iFood',
  };
  let sequence = 0;
  const rows = value => Array.isArray(value) ? value : [];
  function el(tag, text, className) {
    const node = document.createElement(tag);
    if (text !== undefined && text !== null) node.textContent = String(text);
    if (className) node.className = className;
    return node;
  }
  function button(text, action) {
    const node = el('button', text, 'small-button'); node.type = 'button';
    node.addEventListener('click', () => { void action(); }); return node;
  }
  function field(text, type = 'text') {
    const box = el('div', undefined, 'field');
    const input = el(type === 'select' ? 'select' : type === 'textarea' ? 'textarea' : 'input');
    if (input.tagName === 'INPUT') input.type = type;
    input.id = `ifood-test-field-${++sequence}`;
    const label = el('label', text); label.htmlFor = input.id; box.append(label, input);
    return { box, input };
  }
  function options(select, values, placeholder = 'Selecione') {
    const previous = select.value;
    const empty = el('option', placeholder); empty.value = ''; select.replaceChildren(empty);
    for (const value of values) { const item = el('option', value.label); item.value = value.id; select.append(item); }
    if (values.some(value => value.id === previous)) select.value = previous;
  }
  function jsonDetails(title, value) {
    const details = el('details'); details.append(el('summary', title));
    const pre = el('pre', JSON.stringify(value, null, 2));
    pre.style.whiteSpace = 'pre-wrap'; pre.style.overflowWrap = 'anywhere'; pre.style.maxWidth = '100%';
    details.append(pre); return details;
  }
  function section(title, name) {
    const host = el('section', undefined, 'integration'); host.setAttribute('aria-label', name || title);
    host.style.minWidth = '0'; host.style.overflowWrap = 'anywhere';
    host.setAttribute('aria-busy', 'false');
    const heading = el('h3', title), status = el('p', 'Conferindo a configuração local…', 'help');
    status.setAttribute('role', 'status'); status.setAttribute('aria-live', 'polite');
    host.append(heading, status); return { host, status };
  }
  function context(target, result, formatDate) {
    target.replaceChildren();
    if (result.environment !== 'test') return;
    const values = [result.checked_at ? `Consulta: ${formatDate(result.checked_at)}` : '',
      result.client_id ? `client_id de teste: ${result.client_id}` : '',
      result.configured_merchant_id || result.merchant_id ? `Loja de teste: ${result.configured_merchant_id || result.merchant_id}` : ''];
    target.textContent = values.filter(Boolean).join(' · ');
  }
  function localDate(value) {
    const parsed = new Date(value);
    if (!value || Number.isNaN(parsed.getTime())) throw new Error('Informe o início e o fim da pausa.');
    return parsed.toISOString();
  }
  function priceCents(value) {
    if (!/^\d+(?:[,.]\d{1,2})?$/.test(value.trim())) throw new Error('Informe um preço com até duas casas decimais.');
    const [whole, decimal = ''] = value.trim().replace(',', '.').split('.');
    const cents = Number(whole) * 100 + Number(decimal.padEnd(2, '0'));
    if (!Number.isSafeInteger(cents) || cents > 100000) throw new Error('Informe um preço entre R$ 0,00 e R$ 1.000,00.');
    return cents;
  }
  const fallbackDate = value => value || '—';
  const message = result => result.message || labels[result.state] || 'Confira o resultado da operação.';
  const definitive = error => [400, 401, 403, 404, 409, 422].includes(error.status);

  function homologationProfile({ host, api, isCurrent }) {
    const panel = section('Conta nova · preparação para homologação', 'Preparação da conta nova no iFood'); host.append(panel.host);
    let busy = false, discoveryAvailable = false;
    const refreshButton = button('Atualizar status da conta nova', refresh);
    const discoverButton = button('Consultar UUID da loja de teste da conta nova', discover);
    const results = el('div');
    panel.host.append(el('p', 'Conta nova aguarda configuração segura e confirmação da loja de teste. A consulta do UUID mantém este perfil bloqueado para operações.', 'help'),
      refreshButton, discoverButton, results);
    function update() {
      refreshButton.disabled = busy; discoverButton.disabled = busy || !discoveryAvailable;
      panel.host.setAttribute('aria-busy', String(busy));
    }
    async function refresh() {
      if (!isCurrent() || busy) return;
      busy = true; update(); results.replaceChildren();
      try {
        const value = await api('/admin/ifood/homologation-profile');
        if (!isCurrent()) return;
        discoveryAvailable = value?.environment === 'test' && value.discovery_available === true;
        panel.status.textContent = value?.environment === 'test' && typeof value.message === 'string'
          ? value.message : 'Não foi possível conferir o status da conta nova.';
      } catch {
        if (isCurrent()) { discoveryAvailable = false; panel.status.textContent = 'Não foi possível consultar o status da conta nova. Atualize esta seção para conferir.'; }
      } finally {
        busy = false;
        if (isCurrent()) update();
      }
    }
    async function discover() {
      if (!isCurrent() || busy || !discoveryAvailable) return;
      busy = true; update(); results.replaceChildren(); panel.status.textContent = 'Consultando a loja de teste da conta nova no iFood…';
      try {
        const value = await api('/admin/ifood/homologation-profile/discover', { method: 'POST', body: {} });
        if (!isCurrent()) return;
        panel.status.textContent = value?.environment === 'test' && typeof value.message === 'string'
          ? value.message : 'Não foi possível identificar a loja de teste da conta nova.';
        if (value?.environment === 'test' && value.state === 'queried' && Array.isArray(value.merchants)) {
          const valid = value.merchants.every(merchant => merchant?.is_test === true && typeof merchant.merchant_id === 'string'
            && /^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$/i.test(merchant.merchant_id));
          if (!valid) { panel.status.textContent = 'A consulta não retornou uma lista válida de lojas de teste.'; return; }
          for (const merchant of value.merchants) results.append(el('p', `UUID da loja de teste: ${merchant.merchant_id}`, 'help'));
          results.append(el('p', 'Consulta concluída. O perfil da conta nova continua bloqueado para operações.', 'help'));
        }
      } catch {
        if (isCurrent()) panel.status.textContent = 'Não foi possível consultar a loja de teste da conta nova. Confira a configuração segura e atualize o status.';
      } finally {
        busy = false;
        if (isCurrent()) update();
      }
    }
    void refresh(); return { refresh };
  }

  function merchantPanel({ host, api, isCurrent, date }) {
    const panel = section('Pausas e horários · TESTE', 'Operações Merchant da loja de teste'); host.append(panel.host);
    const metadata = el('p', '', 'help'), resultBox = el('div'), history = el('div');
    const fields = el('div', undefined, 'form-grid');
    const description = field('Descrição da pausa de teste'); description.input.maxLength = 450;
    const start = field('Início da pausa · horário deste computador', 'datetime-local');
    const end = field('Fim da pausa · horário deste computador', 'datetime-local');
    fields.append(description.box, start.box, end.box);
    let readiness = null, busy = false, unknownKey = null;
    let operations = [];
    const current = () => isCurrent();
    const usable = () => readiness?.writes_available === true && readiness.environment === 'test';
    const active = () => operations.some(item => pendingMerchant(item.state)) || Boolean(unknownKey);
    const local = button('Atualizar operações de pausas', refresh);
    const query = button('Consultar pausas no iFood', async () => remote('/interruptions/query', {}, false));
    const hours = button('Consultar horários no iFood', async () => remote('/opening-hours/query', {}, false));
    const create = button('Criar pausa na loja de teste', async () => {
      if (!current() || busy || !usable() || active()) return;
      try {
        if (!description.input.value.trim()) throw new Error('Informe a descrição da pausa.');
        const first = localDate(start.input.value), last = localDate(end.input.value);
        if (new Date(last) <= new Date(first)) throw new Error('O fim precisa ser posterior ao início.');
        const key = crypto.randomUUID();
        await remote('/interruptions', { idempotency_key: key, description: description.input.value.trim(), start: first, end: last }, true, key);
      } catch (error) { if (current()) panel.status.textContent = error.message; }
    });
    const controls = el('div', undefined, 'form-actions'); controls.append(local, query, hours);
    const hoursReason = el('p', 'Alteração de horários indisponível: a coleção não define os dias e a unidade da duração.', 'help');
    panel.host.append(el('p', 'Opera somente na loja de teste configurada no servidor. A integração das três lojas reais continua pendente.', 'help'),
      controls, metadata, fields, create, hoursReason, resultBox, history);
    function update() {
      local.disabled = busy; query.disabled = busy || !usable();
      hours.disabled = busy || !usable();
      create.disabled = busy || !usable() || active();
      for (const input of [description.input, start.input, end.input]) input.disabled = busy || !usable() || active();
      panel.host.setAttribute('aria-busy', String(busy));
      renderHistory();
    }
    function renderHistory() {
      history.replaceChildren(el('h4', 'Operações registradas neste teste'));
      if (unknownKey && !operations.some(operation => operation.idempotency_key === unknownKey)) {
        history.append(el('p', 'Resposta interrompida. Atualize as operações; nenhuma nova pausa será enviada até conferir a tentativa.', 'help'));
      }
      if (!operations.length) history.append(el('p', 'Nenhuma operação de pausa registrada.', 'help'));
      const removed = new Set(operations.filter(operation => operation.kind === 'remove_interruption' && operation.state === 'confirmed').map(operation => operation.parent_id));
      for (const operation of operations) {
        const item = el('div', undefined, 'integration');
        const pause = operation.result?.interruption;
        item.append(el('p', `${operation.kind === 'remove_interruption' ? 'Remoção' : 'Criação'} · ${labels[operation.state] || operation.state} · ${date(operation.updated_at || operation.created_at)}`, 'help'));
        if (pause) item.append(el('p', `${pause.description} · ${date(pause.start)} até ${date(pause.end)}`, 'help'));
        item.append(jsonDetails('Registro da operação de pausa', operation));
        if (pendingMerchant(operation.state)) {
          const reconcile = button('Conferir esta operação no iFood', () => remote(`/operations/${encodeURIComponent(operation.id)}/reconcile`, {}, false));
          reconcile.disabled = busy || !usable(); item.append(reconcile);
        }
        if (operation.kind === 'create_interruption' && operation.state === 'confirmed' && !removed.has(operation.id)) {
          const remove = button('Remover esta pausa de teste', () => {
            const key = crypto.randomUUID();
            return remote(`/interruptions/${encodeURIComponent(operation.id)}/remove`, { idempotency_key: key }, true, key);
          });
          remove.disabled = busy || !usable() || active(); item.append(remove);
        }
        history.append(item);
      }
    }
    async function load() {
      const value = await api(`${MERCHANT}/actions`);
      if (!current()) return;
      readiness = value; operations = rows(value.operations);
      const interrupted = operations.find(operation => operation.idempotency_key === unknownKey);
      if (interrupted && ['confirmed', 'rejected'].includes(interrupted.state)) unknownKey = null;
      context(metadata, value, date); hoursReason.textContent = value.hours_write_reason || hoursReason.textContent;
      return value;
    }
    async function refresh() {
      if (!current() || busy) return;
      busy = true; update();
      try { const value = await load(); if (current() && value) panel.status.textContent = message(value); }
      catch (error) { if (current()) { readiness = null; panel.status.textContent = error.message; } }
      finally { busy = false; if (current()) update(); }
    }
    async function remote(suffix, body, mutation, key) {
      if (!current() || busy || !usable() || (mutation && active())) return;
      busy = true; update(); resultBox.replaceChildren(); panel.status.textContent = mutation ? 'Enviando uma operação à loja de teste…' : 'Consultando o iFood…';
      if (mutation) unknownKey = key;
      try {
        const value = await api(MERCHANT + suffix, { method: 'POST', body });
        if (!current()) return;
        panel.status.textContent = message(value); context(metadata, value, date);
        if (value.operation) {
          operations = [value.operation, ...operations.filter(operation => operation.id !== value.operation.id)];
          if (['confirmed', 'rejected'].includes(value.operation.state) && value.operation.idempotency_key === unknownKey) unknownKey = null;
        } else if (mutation && !pendingMerchant(value.state)) unknownKey = null;
        if (value.provider_response) resultBox.append(jsonDetails('Resposta de pausas fora do formato esperado', value.provider_response));
        if (value.state === 'queried') {
          if (Array.isArray(value.interruptions)) resultBox.append(jsonDetails('Pausas retornadas pelo iFood', value.interruptions));
          if (Array.isArray(value.opening_hours)) resultBox.append(jsonDetails('Horários retornados pelo iFood', value.opening_hours));
        }
        try { await load(); } catch { /* The result remains visible; no remote retry. */ }
      } catch (error) {
        if (current()) {
          if (mutation && definitive(error)) unknownKey = null;
          panel.status.textContent = error.message;
        }
      }
      finally { busy = false; if (current()) update(); }
    }
    void refresh(); return { refresh };
  }

  function catalogPanel({ host, api, isCurrent, date }) {
    const panel = section('Catálogo · TESTE', 'Catálogo da loja de teste'); host.append(panel.host);
    const metadata = el('p', '', 'help'), resultBox = el('div'), history = el('div');
    const catalog = field('Catálogo consultado no iFood', 'select');
    const categoryTemplate = field('Categoria existente para usar como modelo', 'select');
    const itemCategory = field('Categoria criada nesta homologação', 'select');
    const itemTemplate = field('Item existente para usar como modelo', 'select');
    const itemPrice = field('Preço do Produto Teste · R$', 'text'); itemPrice.input.inputMode = 'decimal'; itemPrice.input.value = '5,00';
    const groupItem = field('Item criado que receberá os complementos', 'select');
    const groupTemplate = field('Grupo existente com duas opções para usar como modelo', 'select');
    const groupPrice1 = field('Preço do Complemento Teste 1 · R$', 'text'); groupPrice1.input.inputMode = 'decimal'; groupPrice1.input.value = '1,00';
    const groupPrice2 = field('Preço do Complemento Teste 2 · R$', 'text'); groupPrice2.input.inputMode = 'decimal'; groupPrice2.input.value = '2,00';
    const patchAction = field('Alteração individual', 'select');
    options(patchAction.input, [
      { id: 'items/price', label: 'Preço de um item' }, { id: 'items/status', label: 'Disponibilidade de um item' },
      { id: 'options/price', label: 'Preço de uma opção' }, { id: 'options/status', label: 'Disponibilidade de uma opção' },
    ]);
    const patchEntity = field('Entidade criada nesta homologação', 'select');
    const patchPrice = field('Novo preço · R$', 'text'); patchPrice.input.inputMode = 'decimal';
    const patchStatus = field('Status já observado na loja de teste', 'select');
    let ready = null, catalogs = [], categories = [], inspected = '', busy = false, unknownKey = null;
    let operations = [], resources = [];
    const current = () => isCurrent();
    const active = () => Boolean(unknownKey) || operations.some(operation => pendingCatalog(operation.state));
    const available = () => ready?.available === true && ready?.environment === 'test';
    const local = button('Atualizar operações do catálogo', refresh);
    const discover = button('Consultar catálogos no iFood', async () => remote('/discover', {}, 'POST', false));
    const inspect = button('Consultar categorias e itens', async () => {
      if (catalog.input.value) await remote('/inspect', { catalog_id: catalog.input.value }, 'POST', false);
    });
    const createCategory = button('Criar categoria Teste Homologação', async () => {
      if (!categoryTemplate.input.value || inspected !== catalog.input.value) return;
      await mutation('/categories', { catalog_id: inspected, template_category_id: categoryTemplate.input.value });
    });
    const createItem = button('Criar Produto Teste com foto do modelo', async () => {
      try {
        const cents = priceCents(itemPrice.input.value);
        if (!cents) throw new Error('O Produto Teste precisa ter preço maior que zero.');
        if (!itemCategory.input.value || !itemTemplate.input.value || !inspected) return;
        await mutation('/items', { catalog_id: inspected, category_id: itemCategory.input.value, template_item_id: itemTemplate.input.value, price_cents: cents });
      } catch (error) { if (current()) panel.status.textContent = error.message; }
    });
    const createGroup = button('Criar dois complementos com fotos dos modelos', async () => {
      try {
        const selected = groupChoices().find(choice => choice.id === groupTemplate.input.value);
        if (!selected || !groupItem.input.value || !inspected) return;
        await mutation('/groups', { catalog_id: inspected, item_id: groupItem.input.value,
          template_item_id: selected.item_id, template_group_id: selected.group_id,
          option1_price_cents: priceCents(groupPrice1.input.value), option2_price_cents: priceCents(groupPrice2.input.value) });
      } catch (error) { if (current()) panel.status.textContent = error.message; }
    });
    const patch = button('Aplicar alteração individual no teste', async () => {
      try {
        const action = patchAction.input.value;
        if (!action || !patchEntity.input.value || !inspected) return;
        const data = { catalog_id: inspected, entity_id: patchEntity.input.value };
        if (action.endsWith('/price')) data.price_cents = priceCents(patchPrice.input.value);
        else { if (!patchStatus.input.value) return; data.status = patchStatus.input.value; }
        await mutation('/' + action, data, 'PATCH');
      } catch (error) { if (current()) panel.status.textContent = error.message; }
    });
    const controls = el('div', undefined, 'form-actions'); controls.append(local, discover);
    const discoveryGrid = el('div', undefined, 'form-grid'); discoveryGrid.append(catalog.box);
    const categoryDetails = el('details'); categoryDetails.append(el('summary', 'Criar categoria de homologação'));
    categoryDetails.append(categoryTemplate.box, el('p', 'Reutiliza somente os códigos de template e status de uma categoria realmente consultada.', 'help'), createCategory);
    const itemDetails = el('details'); itemDetails.append(el('summary', 'Criar item de homologação'));
    const itemGrid = el('div', undefined, 'form-grid'); itemGrid.append(itemCategory.box, itemTemplate.box, itemPrice.box);
    itemDetails.append(itemGrid, el('p', 'O Produto Teste reutiliza a foto de um modelo simples existente na loja de teste. Isso não realiza upload de foto e não conclui o cenário de imagens.', 'help'), createItem);
    const groupDetails = el('details'); groupDetails.append(el('summary', 'Criar grupo e duas opções de homologação'));
    const groupGrid = el('div', undefined, 'form-grid'); groupGrid.append(groupItem.box, groupTemplate.box, groupPrice1.box, groupPrice2.box);
    groupDetails.append(groupGrid, el('p', 'Cria Complementos Teste no item desta homologação. Reutiliza fotos de duas opções de um modelo simples consultado. Não realiza upload ou troca de fotos.', 'help'), createGroup);
    const patchDetails = el('details'); patchDetails.append(el('summary', 'Alterar preço ou disponibilidade de uma entidade'));
    const patchGrid = el('div', undefined, 'form-grid'); patchGrid.append(patchAction.box, patchEntity.box, patchPrice.box, patchStatus.box);
    patchDetails.append(patchGrid, el('p', 'Somente entidades criadas nesta área podem ser alteradas. As rotas recebidas descrevem uma entidade por requisição; operações em lote ainda não estão disponíveis.', 'help'), patch);
    panel.host.append(el('p', 'Preparação restrita à loja de teste configurada. Os 57 produtos do site não serão enviados por estes controles.', 'help'), controls, metadata,
      discoveryGrid, inspect, categoryDetails, itemDetails, groupDetails, patchDetails,
      el('p', 'Upload ou troca de fotos e alterações em lote aguardam o contrato completo. Os vídeos de homologação continuam pendentes.', 'help'), resultBox, history);
    const categoriesForTemplate = () => categories.filter(value => typeof value.status === 'string' && typeof value.template === 'string' && /^[A-Za-z0-9_-]{1,80}$/.test(value.status) && /^[A-Za-z0-9_-]{1,80}$/.test(value.template));
    const allItems = () => categories.flatMap(category => rows(category.items));
    const owned = kind => resources.filter(resource => resource.catalog_id === inspected && resource.kind === kind);
    function groupChoices() {
      const choices = [];
      for (const item of allItems()) for (const group of rows(item.optionGroups)) {
        const values = Array.isArray(group.options) ? group.options : group.options ? [group.options] : [];
        if (values.length === 2 && typeof group.id === 'string') choices.push({ id: `${item.id}:${group.id}`,
          label: `${item.name || item.id} · ${group.name || group.id}`, item_id: item.id, group_id: group.id });
      }
      return choices;
    }
    function selections() {
      options(categoryTemplate.input, categoriesForTemplate().map(value => ({ id: value.id, label: value.name || value.id })));
      options(itemCategory.input, owned('category').map(value => ({ id: value.remote_id, label: value.data?.name || `Categoria de homologação · ${value.remote_id}` })));
      options(itemTemplate.input, allItems().filter(item => !rows(item.optionGroups).length).map(item => ({ id: item.id, label: item.name || `Item · ${item.id}` })));
      options(groupItem.input, owned('item').map(value => ({ id: value.remote_id, label: value.data?.name || `Item de homologação · ${value.remote_id}` })));
      options(groupTemplate.input, groupChoices());
      const kind = patchAction.input.value.startsWith('options/') ? 'option' : 'item';
      options(patchEntity.input, owned(kind).map(value => ({ id: value.remote_id, label: value.data?.name || `${kind === 'item' ? 'Item' : 'Opção'} de homologação · ${value.remote_id}` })));
      const statuses = new Set();
      for (const item of allItems()) {
        if (typeof item.status === 'string') statuses.add(item.status);
        for (const group of rows(item.optionGroups)) for (const option of Array.isArray(group.options) ? group.options : group.options ? [group.options] : []) if (typeof option.status === 'string') statuses.add(option.status);
      }
      options(patchStatus.input, [...statuses].map(value => ({ id: value, label: value })));
      patchPrice.box.hidden = !patchAction.input.value.endsWith('/price'); patchStatus.box.hidden = !patchAction.input.value.endsWith('/status');
    }
    function update() {
      const writable = available() && !busy && !active();
      local.disabled = busy; discover.disabled = busy || !available(); catalog.input.disabled = busy || !catalogs.length;
      inspect.disabled = busy || !available() || !catalog.input.value;
      for (const input of [categoryTemplate.input, itemCategory.input, itemTemplate.input, itemPrice.input, groupItem.input, groupTemplate.input, groupPrice1.input, groupPrice2.input, patchAction.input, patchEntity.input, patchPrice.input, patchStatus.input]) input.disabled = !writable;
      createCategory.disabled = !writable || !categoryTemplate.input.value || inspected !== catalog.input.value;
      createItem.disabled = !writable || !itemCategory.input.value || !itemTemplate.input.value || inspected !== catalog.input.value;
      createGroup.disabled = !writable || ready?.capabilities?.group_creation !== true || !groupItem.input.value || !groupTemplate.input.value || inspected !== catalog.input.value;
      patch.disabled = !writable || !patchAction.input.value || !patchEntity.input.value || (patchAction.input.value.endsWith('/status') && !patchStatus.input.value);
      panel.host.setAttribute('aria-busy', String(busy)); renderHistory();
    }
    function renderHistory() {
      history.replaceChildren(el('h4', 'Operações do catálogo registradas'));
      if (unknownKey && !operations.some(operation => operation.operation_key === unknownKey)) history.append(el('p', 'Resposta interrompida. Atualize as operações para conferir a tentativa; novas alterações estão bloqueadas.', 'help'));
      if (!operations.length) history.append(el('p', 'Nenhuma alteração de catálogo registrada.', 'help'));
      for (const operation of operations) {
        const item = el('div', undefined, 'integration');
        item.append(el('p', `${operation.kind} · ${labels[operation.state] || operation.state} · ${date(operation.checked_at)}`, 'help'), jsonDetails('Registro da operação de catálogo', operation));
        if (pendingCatalog(operation.state)) {
          const reconcile = button('Conferir esta alteração no iFood', () => remote('/reconcile', { operation_key: operation.operation_key }, 'POST', false));
          reconcile.disabled = busy || !available(); item.append(reconcile);
        }
        history.append(item);
      }
    }
    async function load() {
      const value = await api(CAT);
      if (!current()) return;
      ready = value; operations = rows(value.operations); resources = rows(value.resources);
      const interrupted = operations.find(operation => operation.operation_key === unknownKey);
      if (interrupted && ['confirmed', 'failed'].includes(interrupted.state)) unknownKey = null;
      selections(); return value;
    }
    async function refresh() {
      if (!current() || busy) return;
      busy = true; update();
      try { const value = await load(); if (current() && value) panel.status.textContent = value.message || (available() ? 'Consultas manuais disponíveis para o catálogo da loja de teste.' : 'Verifique a conexão com a loja de teste antes de consultar o catálogo.'); }
      catch (error) { if (current()) { ready = null; panel.status.textContent = error.message; } }
      finally { busy = false; if (current()) update(); }
    }
    async function mutation(suffix, body, method = 'POST') {
      if (!current() || busy || active() || !available()) return;
      const key = crypto.randomUUID();
      await remote(suffix, { ...body, operation_key: key }, method, true, key);
    }
    async function remote(suffix, body, method, mutates, key) {
      if (!current() || busy || !available() || (mutates && active())) return;
      busy = true; if (mutates) unknownKey = key; update(); resultBox.replaceChildren(); panel.status.textContent = mutates ? 'Enviando uma alteração ao catálogo de teste…' : 'Consultando o catálogo no iFood…';
      try {
        const value = await api(CAT + suffix, { method, body });
        if (!current()) return;
        panel.status.textContent = message(value); context(metadata, value, date);
        if (value.state === 'queried' && value.environment === 'test') {
          if (suffix === '/discover' && Array.isArray(value.catalogs)) {
            catalogs = value.catalogs; options(catalog.input, catalogs.map(item => ({ id: item.catalogId, label: `${item.context || 'Catálogo'} · ${item.catalogId}` })));
            categories = []; inspected = ''; selections();
          } else if (suffix === '/inspect' && value.catalog_id === catalog.input.value && Array.isArray(value.categories)) {
            categories = value.categories; inspected = value.catalog_id; selections();
          }
          resultBox.append(jsonDetails('Dados retornados do catálogo de teste', suffix === '/discover' ? value.catalogs : value.categories));
        }
        if (value.operation_key) {
          operations = [value, ...operations.filter(operation => operation.operation_key !== value.operation_key)];
          if (['confirmed', 'failed'].includes(value.state) && value.operation_key === unknownKey) unknownKey = null;
        } else if (mutates && !pendingCatalog(value.state)) unknownKey = null;
        if (value.photo_source === 'existing_test_template') resultBox.append(el('p', 'Foto do modelo existente reutilizada. Upload de foto não realizado.', 'help'));
        try { await load(); } catch { /* Keep the result; no write retry. */ }
      } catch (error) {
        if (current()) {
          if (mutates && definitive(error)) unknownKey = null;
          panel.status.textContent = error.message;
        }
      }
      finally { busy = false; if (current()) update(); }
    }
    catalog.input.addEventListener('change', () => { categories = []; inspected = ''; resultBox.replaceChildren(); selections(); update(); });
    patchAction.input.addEventListener('change', () => { selections(); update(); });
    for (const input of [categoryTemplate.input, itemCategory.input, itemTemplate.input, groupItem.input, groupTemplate.input, patchEntity.input, patchStatus.input]) input.addEventListener('change', update);
    void refresh(); return { refresh };
  }

  function mount(options) {
    if (!options?.host || typeof options.api !== 'function' || typeof options.isCurrent !== 'function') return;
    if (mounted.has(options.host)) return mounted.get(options.host);
    const config = { date: fallbackDate, ...options };
    const profile = homologationProfile(config), merchant = merchantPanel(config), catalog = catalogPanel(config);
    const controls = { refresh: () => { void profile.refresh(); void merchant.refresh(); void catalog.refresh(); } };
    mounted.set(options.host, controls); return controls;
  }

  function mountOrder({ host, order, api, isCurrent, date = fallbackDate }) {
    if (!host || !order || order.source !== 'ifood' || order.delivery?.ifood?.environment !== 'test' || typeof api !== 'function' || typeof isCurrent !== 'function') return;
    if (orderMounted.has(host)) return orderMounted.get(host);
    const base = `/admin/ifood/orders/${encodeURIComponent(order.id)}`;
    const panel = section('Comandos do pedido · TESTE', 'Comandos iFood do pedido de teste');
    const details = el('details'); details.append(el('summary', 'Solicitações manuais ao iFood · teste'), panel.host); host.append(details);
    const history = el('div'), controls = el('div', undefined, 'form-actions');
    const reason = field('Motivo de cancelamento retornado pelo iFood', 'select');
    const justification = field('Justificativa do cancelamento', 'textarea'); justification.input.maxLength = 500;
    let ready = false, busy = false, unknown = false, unknownKey = null, attempts = [], reasons = [];
    const names = [['confirm', 'Solicitar confirmação'], ['startPreparation', 'Solicitar preparo'], ['readyToPickup', 'Solicitar pronto para retirada'], ['dispatch', 'Solicitar despacho']];
    const buttons = names.map(([action, label]) => ({ action, node: button(label, () => submit(action)) }));
    const refreshButton = button('Atualizar solicitações deste pedido', refresh);
    const reasonButton = button('Consultar motivos de cancelamento', async () => {
      if (!isCurrent() || busy || !ready || unknown) return;
      busy = true; update(); reasons = []; options(reason.input, []);
      try {
        const value = await api(base + '/cancellation-reasons');
        if (!isCurrent()) return;
        if (value.environment === 'test' && value.state === 'queried' && Array.isArray(value.reasons)) {
          reasons = value.reasons; options(reason.input, reasons.map(value => ({ id: value.cancelCodeId, label: value.description || value.cancelCodeId })));
          panel.status.textContent = reasons.length ? 'Motivos atuais consultados. Escolha um motivo e informe a justificativa.' : 'Nenhum motivo de cancelamento disponível para este pedido.';
        } else panel.status.textContent = message(value);
      } catch (error) { if (isCurrent()) panel.status.textContent = error.message; }
      finally { busy = false; if (isCurrent()) update(); }
    });
    const cancel = button('Solicitar cancelamento no iFood', () => submit('requestCancellation'));
    controls.append(refreshButton, ...buttons.map(value => value.node));
    panel.host.append(el('p', 'Cada botão envia uma solicitação à loja de teste configurada. HTTP 202 indica aceite para processamento; a etapa local, o caixa e o estoque permanecem sem confirmação. Confira o resultado no Portal do iFood.', 'help'),
      controls, reasonButton, reason.box, justification.box, cancel, history);
    function update() {
      refreshButton.disabled = busy;
      const available = ready && !busy && !unknown;
      for (const value of buttons) value.node.disabled = !available || attempts.some(attempt => attempt.action === value.action && ['accepted_pending', 'already_requested'].includes(attempt.state));
      reasonButton.disabled = !available;
      reason.input.disabled = !available || !reasons.length; justification.input.disabled = !available;
      cancel.disabled = !available || !reason.input.value || !justification.input.value.trim() || attempts.some(attempt => attempt.action === 'requestCancellation' && ['accepted_pending', 'already_requested'].includes(attempt.state));
      panel.host.setAttribute('aria-busy', String(busy)); history.replaceChildren();
      for (const attempt of attempts) history.append(el('p', `${attempt.action} · ${labels[attempt.state] || attempt.state} · ${date(attempt.checked_at)}`, 'help'));
    }
    async function refresh() {
      if (!isCurrent() || busy) return;
      busy = true; update();
      try {
        const value = await api(base + '/actions');
        if (!isCurrent()) return;
        ready = value.environment === 'test' && value.actions_available === true; attempts = rows(value.attempts);
        const interrupted = attempts.find(attempt => attempt.idempotency_key === unknownKey);
        if (interrupted && ['accepted_pending', 'already_requested', 'refused'].includes(interrupted.state)) {
          unknown = false; unknownKey = null;
        }
        if (attempts.some(attempt => ['in_progress', 'uncertain'].includes(attempt.state))) unknown = true;
        panel.status.textContent = value.message || (unknown ? 'Confira a tentativa pendente no Portal do iFood. Novos comandos estão bloqueados.' : ready ? 'Solicitações manuais disponíveis para este pedido de teste.' : 'Comandos indisponíveis nesta configuração de teste.');
      } catch (error) { if (isCurrent()) { ready = false; panel.status.textContent = error.message; } }
      finally { busy = false; if (isCurrent()) update(); }
    }
    async function submit(action) {
      if (!isCurrent() || busy || !ready || unknown) return;
      if (attempts.some(attempt => attempt.action === action && ['accepted_pending', 'already_requested'].includes(attempt.state))) return;
      const body = { action, idempotency_key: crypto.randomUUID() };
      if (action === 'requestCancellation') {
        if (!reasons.some(value => value.cancelCodeId === reason.input.value) || !justification.input.value.trim()) return;
        body.cancellation_code = reason.input.value; body.reason = justification.input.value.trim();
      }
      busy = true; unknown = true; unknownKey = body.idempotency_key; update(); panel.status.textContent = 'Enviando solicitação ao iFood…';
      try {
        const value = await api(base + '/actions', { method: 'POST', body });
        if (!isCurrent()) return;
        panel.status.textContent = message(value);
        if (value.attempt_id) attempts = [value, ...attempts.filter(attempt => attempt.attempt_id !== value.attempt_id)];
        unknown = ['uncertain', 'in_progress'].includes(value.state);
        if (!['accepted_pending', 'already_requested', 'refused', 'invalid_cancellation_code'].includes(value.state)) unknown = true;
        if (!unknown) unknownKey = null;
      } catch (error) { if (isCurrent()) { if (definitive(error)) { unknown = false; unknownKey = null; } panel.status.textContent = error.message + (unknown ? ' Confira a tentativa antes de enviar outro comando.' : ''); } }
      finally { busy = false; if (isCurrent()) update(); }
    }
    reason.input.addEventListener('change', update); justification.input.addEventListener('input', update);
    const mountedOrder = { refresh }; orderMounted.set(host, mountedOrder); void refresh(); return mountedOrder;
  }
  window.SaharaIFoodTests = Object.freeze({ mount, mountOrder });
})();
