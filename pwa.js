(() => {
  'use strict';
  const installCard = document.querySelector('#app-install');
  const installButton = document.querySelector('#install-app');
  const installDialog = document.querySelector('#install-dialog');
  const offlineNotice = document.querySelector('#offline-notice');
  const updateNotice = document.querySelector('#update-notice');
  const updateButton = document.querySelector('#update-app');
  const installed = window.matchMedia('(display-mode: standalone)');
  let installPrompt = null;
  let registration = null;
  let updating = false;

  function displayInstall() {
    installCard.hidden = installed.matches || navigator.standalone === true || navigator.userAgent.includes('SaharaAndroid/');
  }
  displayInstall();
  installed.addEventListener('change', displayInstall);
  window.addEventListener('appinstalled', () => {
    installPrompt = null;
    installCard.hidden = true;
    if (installDialog.open) installDialog.close();
  });
  window.addEventListener('beforeinstallprompt', event => {
    event.preventDefault();
    installPrompt = event;
    displayInstall();
  });
  installButton.addEventListener('click', async () => {
    if (installPrompt) {
      const prompt = installPrompt;
      installPrompt = null;
      await prompt.prompt();
      const choice = await prompt.userChoice;
      if (choice.outcome === 'accepted') installCard.hidden = true;
      return;
    }
    const ios = /iPad|iPhone|iPod/.test(navigator.userAgent) ||
      (navigator.platform === 'MacIntel' && navigator.maxTouchPoints > 1);
    document.querySelector('#install-android').hidden = ios;
    document.querySelector('#install-ios').hidden = !ios;
    installDialog.showModal();
  });
  document.querySelector('#close-install').addEventListener('click', () => installDialog.close());

  function connectionStatus() {
    offlineNotice.hidden = navigator.onLine;
  }
  connectionStatus();
  window.addEventListener('online', connectionStatus);
  window.addEventListener('offline', connectionStatus);
  // Impede abrir um pedido sem conexão; a seleção continua salva.
  document.querySelector('#checkout').addEventListener('click', event => {
    if (navigator.onLine) return;
    event.stopImmediatePropagation();
    event.preventDefault();
    offlineNotice.hidden = false;
    offlineNotice.scrollIntoView({ block: 'center' });
    document.querySelector('#announcement').textContent =
      'Conecte-se à internet para continuar seu pedido no WhatsApp. Sua sacola está salva.';
  }, true);

  function showUpdate() {
    if (registration?.waiting && navigator.serviceWorker.controller) updateNotice.hidden = false;
  }
  updateButton.addEventListener('click', () => {
    if (!registration?.waiting || updating) return;
    // Guarda o formulário apenas para a atualização solicitada nesta aba.
    const draft = {};
    document.querySelectorAll('#delivery-form input:not([name="fulfillment"]), #delivery-form select, #notes').forEach(field => {
      draft[field.id || field.value] = ['radio', 'checkbox'].includes(field.type) ? field.checked : field.value;
    });
    try { sessionStorage.setItem('sahara-update-draft', JSON.stringify(draft)); } catch {}
    updating = true;
    updateButton.disabled = true;
    updateButton.textContent = 'Atualizando…';
    registration.waiting.postMessage({ type: 'SKIP_WAITING' });
  });
  try {
    const draft = JSON.parse(sessionStorage.getItem('sahara-update-draft') || 'null');
    if (draft) {
      document.querySelectorAll('#delivery-form input:not([name="fulfillment"]), #delivery-form select, #notes').forEach(field => {
        const value = draft[field.id || field.value];
        if (['radio', 'checkbox'].includes(field.type)) field.checked = value === true;
        else if (typeof value === 'string') field.value = value;
      });
      sessionStorage.removeItem('sahara-update-draft');
      document.dispatchEvent(new Event('sahara:delivery-restored'));
    }
  } catch {}

  if ('serviceWorker' in navigator) {
    navigator.serviceWorker.addEventListener('controllerchange', () => {
      if (updating) location.reload();
    });
    navigator.serviceWorker.addEventListener('message', event => {
      if (event.data?.type === 'CACHED_MENU') offlineNotice.hidden = false;
    });
    navigator.serviceWorker.register('./service-worker.js', { scope: './', updateViaCache: 'none' })
      .then(value => {
        registration = value;
        showUpdate();
        registration.addEventListener('updatefound', () => {
          const worker = registration.installing;
          worker?.addEventListener('statechange', () => {
            if (worker.state === 'installed') showUpdate();
          });
        });
      })
      .catch(() => {
        // O cardápio online permanece disponível se o navegador não permitir instalação.
      });
  }
})();
