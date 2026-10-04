(() => {
  'use strict';

  const form = document.querySelector('#delivery-form');
  const button = document.querySelector('#use-location');
  const status = document.querySelector('#location-status');
  const card = document.querySelector('#location-card');
  const map = document.querySelector('#location-map');
  const precision = document.querySelector('#location-precision');
  const confirm = document.querySelector('#confirm-location');
  const remove = document.querySelector('#remove-location');
  const fields = Object.fromEntries(['latitude', 'longitude', 'accuracy', 'confirmed']
    .map(name => [name, document.querySelector(`#location-${name}`)]));
  let requestId = 0;
  let busy = false;

  function point() {
    if (!fields.latitude.value || !fields.longitude.value) return null;
    const latitude = Number(fields.latitude.value);
    const longitude = Number(fields.longitude.value);
    const accuracy = fields.accuracy.value ? Number(fields.accuracy.value) : null;
    if (!Number.isFinite(latitude) || latitude < -90 || latitude > 90 ||
        !Number.isFinite(longitude) || longitude < -180 || longitude > 180 ||
        (accuracy !== null && (!Number.isFinite(accuracy) || accuracy < 0))) return null;
    return { latitude, longitude, accuracy,
      url: `https://www.google.com/maps?q=${latitude.toFixed(6)},${longitude.toFixed(6)}` };
  }

  function get() {
    return fields.confirmed.value === 'yes' &&
      form.querySelector('[name="fulfillment"]').value === 'delivery' ? point() : null;
  }

  function announce(message) {
    status.textContent = message;
    status.hidden = !message;
  }

  function refresh() {
    const location = point();
    const selected = !!get();
    card.hidden = !location;
    confirm.hidden = selected;
    confirm.disabled = busy;
    if (location) {
      map.href = location.url;
      precision.textContent = location.accuracy === null
        ? 'Confira no mapa se o ponto corresponde ao endereço de entrega.'
        : `Precisão aproximada: ${Math.max(1, Math.round(location.accuracy))} m. Confira se o ponto é o endereço de entrega.`;
    } else {
      map.removeAttribute('href');
    }
    button.textContent = busy ? 'Buscando localização…' : selected ? 'Atualizar localização' : 'Usar minha localização';
    button.disabled = busy;
    button.setAttribute('aria-busy', String(busy));
    const delivery = form.querySelector('[name="fulfillment"]').value === 'delivery';
    for (const id of ['street', 'neighborhood']) {
      document.querySelector(`#${id}`).required = delivery && !selected;
      document.querySelector(`label[for="${id}"] .field-required`).textContent = selected ? '(opcional)' : '(obrigatório)';
    }
  }

  button.addEventListener('click', () => {
    if (busy) return;
    if (!window.isSecureContext) {
      announce('Abra o site pelo endereço seguro (https) para usar a localização. Você também pode digitar seu endereço.');
      return;
    }
    if (!navigator.geolocation) {
      announce('Este navegador não oferece localização. Digite seu endereço nos campos abaixo.');
      return;
    }
    const currentRequest = ++requestId;
    busy = true;
    refresh();
    announce('Permita o acesso à localização no aviso do navegador. Buscando seu ponto de entrega…');
    const fail = error => {
      if (currentRequest !== requestId) return;
      busy = false;
      refresh();
      const messages = {
        1: 'O acesso à localização não foi permitido. Você pode permitir nas configurações do navegador ou digitar seu endereço.',
        2: 'Não foi possível encontrar sua localização. Tente novamente ou digite seu endereço.',
        3: 'A localização demorou para responder. Tente novamente ou digite seu endereço.'
      };
      announce(messages[error?.code] || 'Não foi possível usar a localização. Tente novamente ou digite seu endereço.');
    };
    try {
      navigator.geolocation.getCurrentPosition(position => {
        if (currentRequest !== requestId) return;
        const { latitude, longitude, accuracy } = position.coords || {};
        if (!Number.isFinite(latitude) || latitude < -90 || latitude > 90 ||
            !Number.isFinite(longitude) || longitude < -180 || longitude > 180 ||
            !Number.isFinite(accuracy) || accuracy < 0) {
          fail({ code: 2 });
          return;
        }
        fields.latitude.value = String(latitude);
        fields.longitude.value = String(longitude);
        fields.accuracy.value = String(accuracy);
        fields.confirmed.value = '';
        busy = false;
        refresh();
        announce('Localização encontrada. Confira o ponto no mapa e toque em “Usar este local”.');
        confirm.focus({ preventScroll: true });
      }, fail, { enableHighAccuracy: true, timeout: 15000, maximumAge: 0 });
    } catch {
      fail({});
    }
  });

  confirm.addEventListener('click', () => {
    if (!point()) return;
    fields.confirmed.value = 'yes';
    refresh();
    announce('Localização selecionada para a entrega. Informe o número da casa e, se necessário, um complemento.');
    document.querySelector('#house-number').focus();
  });

  remove.addEventListener('click', () => {
    ++requestId;
    busy = false;
    for (const field of Object.values(fields)) field.value = '';
    refresh();
    announce('Localização removida. Digite a rua, o número e o bairro para a entrega.');
    button.focus({ preventScroll: true });
  });

  form.addEventListener('change', refresh);
  document.addEventListener('sahara:delivery-restored', () => {
    refresh();
    if (get()) announce('Localização recuperada. Confira o ponto de entrega e o número da casa.');
  });
  window.saharaDeliveryLocation = { get };
  refresh();
})();
