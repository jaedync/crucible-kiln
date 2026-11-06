// Other hosts: one card per remote inference host (tick.hosts), next to the
// local engine card. Each host's agent reports about once a second; a host
// that stops reporting turns "stale" and its last figures are greyed.
import { h, setText, setAttr, toggleClass, panelHead } from '../dom.js';
import * as f from '../format.js';
import { referenceShort } from '../pricing.js';

const STATE_TEXT = { serving: 'Serving', standby: 'Standby', down: 'Down', stale: 'Stale' };
const PHASE_TEXT = { prefill: 'Prefilling', decode: 'Decoding', idle: 'Idle' };

export default {
  id: 'hosts',
  label: 'Other hosts',
  mount(el, { store }) {
    const list = h('div', { class: 'hosts' });
    const empty = h('p', { class: 'panel-sub' }, 'No other host is configured.');
    el.append(panelHead('Other hosts'), list, empty);
    const cards = new Map();

    function render() {
      const { tick } = store.get();
      const hosts = (tick && tick.hosts) || {};
      const now = store.now();
      for (const [name, v] of Object.entries(hosts)) {
        if (!cards.has(name)) {
          const card = hostCard();
          cards.set(name, card);
          list.append(card.el);
        }
        cards.get(name).render(v, now, store.isLive(), referenceShort(store.get().pricing));
      }
      empty.hidden = Object.keys(hosts).length > 0;
      el.hidden = !tick;
    }

    store.on('tick', render);
    store.on('conn', render);
    setInterval(render, 1000); // keeps elapsed and "N s ago" honest between ticks
    render();
  },
};

function hostCard() {
  const word = h('span', { class: 'eng-word' });
  const label = h('p', { class: 'eng-label' });
  const detail = h('p', { class: 'eng-since' });
  const state = h('div', { class: 'eng-state host-state', 'data-state': 'stale' }, word, label, detail);

  const approx = h('span', { class: 'approx', tabindex: '0', 'aria-label': 'estimated' }, '≈');
  const rate = h('span', { class: 'host-rate-v' });
  const phase = h('p', { class: 'host-phase' });
  const live = h('div', { class: 'host-live' }, h('p', { class: 'kicker' }, 'Decode · tokens per second'),
    h('p', { class: 'host-rate' }, approx, rate), phase);

  const vram = meter('VRAM');
  const cells = { gtt: cell('GTT'), busy: cell('GPU busy'), temp: cell('Junction'), power: cell('Power') };
  const hw = h('div', { class: 'host-hw' }, vram.el, h('div', { class: 'host-cells' }, Object.values(cells).map((c) => c.el)));

  const last = h('p', { class: 'host-line' });
  const today = h('p', { class: 'host-line' });
  const el = h('article', { class: 'host-card' }, state, live, hw,
    h('div', { class: 'host-sums' }, h('p', { class: 'kicker' }, 'Last request'), last,
      h('p', { class: 'kicker' }, 'Today on this host'), today));

  function render(v, now, connected, refShort) {
    const stale = !connected || v.state === 'stale';
    el.dataset.state = v.state;
    toggleClass(el, 'is-stale', stale);
    state.dataset.state = v.state;
    setText(word, STATE_TEXT[v.state] || v.state);
    setText(label, v.label || v.name);
    setText(detail, v.detail || `${v.engine} · ${v.model} · ${f.compact(v.context)} ctx · seen ${f.ago(v.seen, now)}`);

    const l = v.live || {};
    setText(rate, stale ? f.DASH : f.rate(l.decode_tps));
    approx.hidden = stale || l.decode_exact !== false || l.decode_tps == null;
    setAttr(approx, 'title', l.decode_note);
    setText(phase, phaseText(l, now, stale));

    const g = v.hw || {};
    vram.render(g.vram_used_bytes, g.vram_total_bytes);
    cells.gtt.render(f.gib(g.gtt_used_bytes), g.gtt_total_bytes != null ? `of ${f.gib(g.gtt_total_bytes, 0)}` : '');
    cells.busy.render(g.gpu_busy_pct == null ? f.DASH : `${Math.round(g.gpu_busy_pct)}%`, 'gfx activity');
    cells.temp.render(f.celsius(g.junction_c), g.edge_c != null ? `edge ${f.celsius(g.edge_c)}` : '');
    cells.power.render(g.power_w == null ? f.DASH : `${Math.round(g.power_w)} W`, 'board');

    setText(last, lastText(v.last, now));
    setText(today, todayText(v.today, refShort));
  }

  return { el, render };
}

function phaseText(l, now, stale) {
  if (stale || !l.phase) return stale ? 'No live data from this host.' : 'Phase not known yet.';
  if (l.phase === 'idle') return 'Idle. Nothing is in flight.';
  const parts = [PHASE_TEXT[l.phase] || l.phase];
  if (l.started) parts.push(f.dur(now - l.started));
  if (l.phase === 'decode' && l.tokens != null) parts.push(`${f.int(l.tokens)} tokens so far`);
  return `${parts.join(' · ')} · ${l.in_flight} / ${l.slots} slot`;
}

function lastText(r, now) {
  if (!r) return 'None yet.';
  // Ledger lines written before the host reported token counts have none.
  if (r.prompt == null && r.completion == null) return `${f.ago(r.ts, now)} · token counts not recorded`;
  const rates = r.decode_tps != null ? ` · ${f.estRate(r.decode_tps, r.decode_estimated)} tok/s` : '';
  return `${f.ago(r.ts, now)} · ${f.int(r.prompt)} prompt (${f.int(r.cached)} cached) · ${f.int(r.completion)} out${rates}`;
}

function todayText(t, refShort) {
  if (!t) return f.DASH;
  return `${f.plural(t.requests, 'request')} · ${f.compact(t.prompt)} prompt · ${f.compact(t.completion)} out · ` +
    `${f.usd(t.cost.reference)} ${refShort} · ${f.usd(t.cost.openrouter)} OpenRouter`;
}

function meter(name) {
  const value = h('span', { class: 'hw-v' });
  const sub = h('span', { class: 'hw-sub' });
  const bar = h('span', { class: 'bar' }, h('i'));
  const el = h('div', { class: 'host-meter' }, h('span', { class: 'kicker' }, name), h('span', { class: 'hw-vrow' }, value, sub), bar);
  return {
    el,
    render(used, total) {
      setText(value, f.gib(used));
      setText(sub, total ? `of ${f.gib(total)} · ${f.pct(used / total)}` : '');
      bar.firstChild.style.width = used != null && total ? `${Math.min(100, (100 * used) / total)}%` : '0%';
    },
  };
}

function cell(name) {
  const value = h('span', { class: 'host-cell-v' });
  const sub = h('span', { class: 'hw-sub' });
  const el = h('div', { class: 'host-cell' }, h('span', { class: 'kicker' }, name), value, sub);
  return { el, render(v, s) { setText(value, v); setText(sub, s); } };
}
