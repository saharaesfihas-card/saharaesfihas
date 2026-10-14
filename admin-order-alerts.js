'use strict';

// Only an authenticated tab may monitor orders; browser audio permission remains authoritative.
window.SaharaOrderAlerts = {
  create({ api, printOrder, onOrders }) {
    const soundButton = document.getElementById('order-sound');
    const printCheckbox = document.getElementById('order-auto-print');
    const status = document.getElementById('order-monitor-status');
    const message = document.getElementById('order-alert-message');
    const preferenceKey = 'sahara-order-sound-enabled';
    let soundWanted = true;
    try { soundWanted = localStorage.getItem(preferenceKey) !== '0'; } catch { /* Storage can be unavailable. */ }
    let authenticated = false, sound = false, blocked = false, owner = false, busy = false, printFailures = 0, missedTone = false;
    let context = null, nextTone = 0, cursor = null, timer = null, release = null, generation = 0, starting = null;

    function controls(text) {
      soundButton.textContent = sound ? 'Pausar som' : blocked && soundWanted ? 'Liberar som' : 'Ativar som de pedidos';
      soundButton.setAttribute('aria-pressed', String(sound));
      status.textContent = text || (owner ? `Acompanhando novos pedidos${sound ? ' · som ativo' : blocked && soundWanted ? ' · som autorizado: clique em qualquer lugar do PDV para liberar o áudio' : ''}${printCheckbox.checked ? ' · impressão ativa' : ''}. Mantenha este PDV aberto.` : 'Ative os avisos nesta aba para acompanhar novos pedidos.');
    }

    function savePreference() {
      try { localStorage.setItem(preferenceKey, soundWanted ? '1' : '0'); } catch { /* Keep the current tab functional. */ }
    }

    function closeAudio() {
      if (context) context.close().catch(() => {});
      context = null; nextTone = 0;
    }

    function prepareAudio(preview = false) {
      if (!authenticated || !owner || !soundWanted) return;
      const Audio = window.AudioContext || window.webkitAudioContext;
      if (!Audio) { blocked = true; controls('Este navegador não oferece áudio para os pedidos. Use Chrome ou Edge atualizado.'); return; }
      if (!context) {
        try { context = new Audio(); }
        catch { blocked = true; controls('Não foi possível iniciar o áudio. Confira o som do PC e tente novamente.'); return; }
      }
      const current = generation, audio = context;
      sound = audio.state === 'running'; blocked = !sound; controls();
      // resume() can stay pending until a gesture. Never let it stop order polling or printing.
      audio.resume().then(() => {
        if (current !== generation || audio !== context || !authenticated || !owner || !soundWanted) return;
        sound = audio.state === 'running'; blocked = !sound; controls();
        if (sound && (preview || missedTone)) {
          missedTone = false;
          tone().catch(() => { if (current === generation) { sound = false; blocked = true; controls(); } });
        }
      }).catch(() => { if (current === generation && audio === context) { sound = false; blocked = true; controls(); } });
    }

    async function tone() {
      if (!context) throw new Error('Som indisponível.');
      if (context.state !== 'running') throw new Error('Som bloqueado.');
      const start = Math.max(context.currentTime + 0.03, nextTone);
      // An original short bell pattern, repeated three times for a delivery alert.
      for (let repeat = 0; repeat < 3; repeat++) {
        [988, 1319, 1175, 1568].forEach((frequency, index) => {
          const at = start + repeat * 0.82 + index * 0.16;
          const oscillator = context.createOscillator(), gain = context.createGain();
          oscillator.type = 'triangle'; oscillator.frequency.setValueAtTime(frequency, at);
          gain.gain.setValueAtTime(0, at); gain.gain.linearRampToValueAtTime(0.18, at + 0.012);
          gain.gain.exponentialRampToValueAtTime(0.001, at + 0.13);
          oscillator.connect(gain); gain.connect(context.destination);
          oscillator.onended = () => { oscillator.disconnect(); gain.disconnect(); };
          oscillator.start(at); oscillator.stop(at + 0.15);
        });
      }
      nextTone = start + 2.5;
    }

    function stop() {
      generation++; owner = false; cursor = null; blocked = false; missedTone = false;
      clearTimeout(timer); timer = null;
      if (release) release(); release = null;
      controls();
    }

    async function poll() {
      if (!authenticated || !owner) return;
      if (busy) { timer = setTimeout(poll, 500); return; }
      const current = generation;
      busy = true;
      try {
        let more = true;
        // Drain batches in order; slow/cancelled print dialogs never overwrite the next receipt.
        for (let batch = 0; more && batch < 10; batch++) {
          const data = await api(`/admin/orders/incoming?cursor=${cursor}`);
          if (current !== generation || !authenticated || !owner) return;
          if (!Array.isArray(data.orders) || !Number.isSafeInteger(data.cursor) || data.cursor < cursor || typeof data.has_more !== 'boolean') throw new Error('Resposta inválida.');
          if (data.has_more && data.cursor === cursor) throw new Error('Resposta inválida.');
          const received = data.orders.filter(order => !['cancelled', 'delivered'].includes(order.status));
          for (const order of received) {
            if (current !== generation || !authenticated || !owner) return;
            const number = order.delivery?.ifood?.display_id || order.display_number || order.id.slice(0, 8).toUpperCase();
            message.textContent = `Novo pedido #${number} recebido no PDV.`;
            if (sound) {
              try { await tone(); }
              catch { if (current !== generation) return; sound = false; blocked = true; missedTone = true; closeAudio(); prepareAudio(); }
            } else if (soundWanted) {
              missedTone = true;
              prepareAudio();
            }
            if (printCheckbox.checked && current === generation && authenticated) {
              try { await printOrder(order); }
              catch { printFailures++; }
            }
            if (printFailures) message.textContent += ` ${printFailures} comanda(s) não abriram para impressão. Confira os pedidos e use Imprimir comanda.`;
          }
          if (current !== generation || !authenticated || !owner) return;
          cursor = data.cursor; more = data.has_more;
          if (received.length) await onOrders(received);
        }
        if (soundWanted || printCheckbox.checked) controls();
        else stop();
      } catch {
        if (current === generation && authenticated) controls('Sem conexão com o PDV. Tentando novamente; os pedidos serão conferidos ao reconectar.');
      } finally {
        busy = false;
        if (current === generation && authenticated && owner) timer = setTimeout(poll, 5000);
      }
    }

    async function start() {
      if (owner) return true;
      if (starting) return starting;
      if (!authenticated) return false;
      if (!navigator.locks) {
        controls('Use o Chrome ou Edge atualizado para ativar os avisos de pedidos.'); return false;
      }
      clearTimeout(timer);
      const current = ++generation;
      controls('Ativando acompanhamento de novos pedidos…');
      let answer;
      const ready = new Promise(resolve => { answer = resolve; });
      starting = ready.finally(() => { starting = null; });
      navigator.locks.request('sahara-new-order-monitor', { ifAvailable: true }, async lock => {
        if (!lock) { controls('Os avisos estão ativos em outra aba deste navegador. Esta aba assume quando a outra parar.'); retryStart(current); answer(false); return; }
        const held = new Promise(resolve => { release = resolve; });
        try {
          const data = await api('/admin/orders/incoming');
          if (current !== generation || !authenticated) { answer(false); return; }
          if (!Number.isSafeInteger(data.cursor) || data.cursor < 0) throw new Error('Resposta inválida.');
          // Establish a fresh baseline: never ring or print historical orders at login/reload.
          cursor = data.cursor; printFailures = 0; owner = true; controls();
          timer = setTimeout(poll, 1000); answer(true);
          await held;
        } catch {
          if (current === generation && authenticated) controls('Não foi possível ativar os avisos. Confira a conexão e tente novamente.');
          retryStart(current);
          answer(false);
        } finally {
          if (current === generation) { owner = false; release = null; }
        }
      }).catch(() => { controls('Não foi possível ativar os avisos nesta aba. Tente novamente.'); answer(false); });
      return ready;
    }

    function retryStart(current) {
      if (current !== generation || !authenticated || !(soundWanted || printCheckbox.checked)) return;
      clearTimeout(timer);
      timer = setTimeout(() => start().then(active => { if (active) prepareAudio(); }), 5000);
    }

    soundButton.addEventListener('click', async () => {
      soundButton.disabled = true;
      try {
        if (sound) {
          soundWanted = false; savePreference(); sound = false; blocked = false; closeAudio(); if (!printCheckbox.checked) stop(); else controls();
        } else {
          soundWanted = true; savePreference();
          if (await start()) prepareAudio(true);
          else { sound = false; closeAudio(); }
        }
      } catch (error) { sound = false; closeAudio(); if (!printCheckbox.checked) stop(); controls(error.message); }
      finally { soundButton.disabled = false; soundButton.textContent = sound ? 'Pausar som' : blocked && soundWanted ? 'Liberar som' : 'Ativar som de pedidos'; soundButton.setAttribute('aria-pressed', String(sound)); }
    });

    printCheckbox.addEventListener('change', async () => {
      printCheckbox.disabled = true;
      try {
        if (printCheckbox.checked && !await start()) printCheckbox.checked = false;
        else if (!printCheckbox.checked && !soundWanted) stop(); else controls();
      } finally { printCheckbox.disabled = false; }
    });

    // A normal click or key in the PDV may legitimately unlock audio after a reload.
    function unlockOnInteraction(event) {
      if (event.isTrusted && authenticated && owner && soundWanted && !sound && event.target !== soundButton) prepareAudio();
    }
    document.addEventListener('pointerdown', unlockOnInteraction);
    document.addEventListener('keydown', unlockOnInteraction);
    window.addEventListener('storage', event => {
      if (event.key !== preferenceKey) return;
      soundWanted = event.newValue !== '0';
      if (!soundWanted) {
        sound = false; blocked = false; closeAudio();
        if (!printCheckbox.checked) stop(); else controls();
      } else if (authenticated) start().then(active => { if (active) prepareAudio(); });
    });

    return {
      setAuthenticated(value) {
        const entered = value && !authenticated;
        authenticated = value;
        if (!value) { sound = false; printCheckbox.checked = false; closeAudio(); stop(); message.textContent = ''; }
        controls();
        if (entered && soundWanted) {
          savePreference();
          start().then(active => { if (active) prepareAudio(); });
        }
      },
      checkNow() { if (owner && !busy) { clearTimeout(timer); return poll(); } },
    };
  },
};
