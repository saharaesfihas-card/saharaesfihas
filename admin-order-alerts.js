'use strict';

// Only an authenticated, explicitly activated tab may play or print new orders.
window.SaharaOrderAlerts = {
  create({ api, printOrder, onOrders }) {
    const soundButton = document.getElementById('order-sound');
    const printCheckbox = document.getElementById('order-auto-print');
    const status = document.getElementById('order-monitor-status');
    const message = document.getElementById('order-alert-message');
    let authenticated = false, sound = false, owner = false, busy = false, printFailures = 0;
    let context = null, nextTone = 0, cursor = null, timer = null, release = null, generation = 0, starting = null;

    function controls(text) {
      soundButton.textContent = sound ? 'Pausar som' : 'Ativar som de pedidos';
      soundButton.setAttribute('aria-pressed', String(sound));
      status.textContent = text || (owner ? `Acompanhando novos pedidos${sound ? ' · som ativo' : ''}${printCheckbox.checked ? ' · impressão ativa' : ''}. Mantenha este PDV aberto.` : 'Ative os avisos nesta aba para acompanhar novos pedidos.');
    }

    function closeAudio() {
      if (context) context.close().catch(() => {});
      context = null; nextTone = 0;
    }

    async function tone() {
      if (!context) throw new Error('Som indisponível.');
      await context.resume();
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
      generation++; owner = false; cursor = null;
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
              catch { if (current !== generation) return; sound = false; closeAudio(); controls('O som foi bloqueado. Clique em Ativar som de pedidos para liberar novamente.'); }
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
        if (sound || printCheckbox.checked) controls();
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
      const current = ++generation;
      controls('Ativando acompanhamento de novos pedidos…');
      let answer;
      const ready = new Promise(resolve => { answer = resolve; });
      starting = ready.finally(() => { starting = null; });
      navigator.locks.request('sahara-new-order-monitor', { ifAvailable: true }, async lock => {
        if (!lock) { controls('Os avisos estão ativos em outra aba deste navegador. Use aquela aba ou pause os avisos nela.'); answer(false); return; }
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
          answer(false);
        } finally {
          if (current === generation) { owner = false; release = null; }
        }
      }).catch(() => { controls('Não foi possível ativar os avisos nesta aba. Tente novamente.'); answer(false); });
      return ready;
    }

    soundButton.addEventListener('click', async () => {
      soundButton.disabled = true;
      try {
        if (sound) {
          sound = false; closeAudio(); if (!printCheckbox.checked) stop(); else controls();
        } else {
          const Audio = window.AudioContext || window.webkitAudioContext;
          if (!Audio) throw new Error('Este navegador não oferece áudio para os pedidos. Use Chrome ou Edge atualizado.');
          context = new Audio(); await context.resume();
          if (context.state !== 'running') throw new Error('Não foi possível liberar o áudio. Confira o som do PC e tente novamente.');
          sound = true;
          if (await start()) await tone();
          else { sound = false; closeAudio(); }
        }
      } catch (error) { sound = false; closeAudio(); if (!printCheckbox.checked) stop(); controls(error.message); }
      finally { soundButton.disabled = false; soundButton.textContent = sound ? 'Pausar som' : 'Ativar som de pedidos'; soundButton.setAttribute('aria-pressed', String(sound)); }
    });

    printCheckbox.addEventListener('change', async () => {
      printCheckbox.disabled = true;
      try {
        if (printCheckbox.checked && !await start()) printCheckbox.checked = false;
        else if (!printCheckbox.checked && !sound) stop(); else controls();
      } finally { printCheckbox.disabled = false; }
    });

    return {
      setAuthenticated(value) {
        authenticated = value;
        if (!value) { sound = false; printCheckbox.checked = false; closeAudio(); stop(); message.textContent = ''; }
        controls();
      },
      checkNow() { if (owner && !busy) { clearTimeout(timer); return poll(); } },
    };
  },
};
