// Price cards in force now, and every past card. Request costs are computed
// once with the card in force at the request's start and never rewritten.
import { h, setText, panelHead } from '../dom.js';
import * as f from '../format.js';
import { referenceName, referenceShort } from '../pricing.js';
import { pollWhileSeen } from '../poll.js';

// The reference name comes from the snapshot ([pricing.reference] name in kiln.toml).
function sources(pricing) {
  return {
    reference: { name: `${referenceName(pricing)} API`, short: referenceShort(pricing), tone: 'blue',
      note: 'Reference: what the same tokens would cost on a frontier API' },
    openrouter: { name: 'OpenRouter list price', short: 'OpenRouter', tone: 'yellow',
      note: 'Model-level list price of the matching OpenRouter entry' },
  };
}
const REFRESH_MS = 10 * 60 * 1000;
// A card for model "*" applies to every model (the reference price).
const modelName = (m) => (m === '*' ? 'All models' : m);

export default {
  id: 'prices',
  label: 'Prices',
  mount(el, { store, api }) {
    const cards = h('div', { class: 'price-cards' });
    const history = h('tbody');
    const status = h('p', { class: 'panel-status', role: 'status' });
    el.append(
      panelHead('Prices'),
      h('p', { class: 'lock-note' }, h('strong', null, 'Costs are locked. '),
        'Each request is priced once, with the card in force when it started.'),
      cards,
      h('h3', { class: 'kicker hist-title' }, 'Price history'),
      h('div', { class: 'scroll-x', tabindex: '0', role: 'region', 'aria-label': 'Price history' },
        h('table', { class: 'tbl price-tbl' },
          h('thead', null, h('tr', null, ['From', 'Source', 'Model', 'Input', 'Cached', 'Output'].map((t, i) =>
            h('th', { scope: 'col', class: i >= 3 ? 'num' : null }, t)))),
          history)),
      h('p', { class: 'price-unit' }, 'USD per million tokens'),
      status);

    let last = null;
    function render() {
      if (!last) return;
      const src = sources(store.get().pricing);
      renderCards(cards, last.current || [], store.now(), src);
      renderHistory(history, last.history || [], src);
    }

    async function load() {
      try {
        last = await api.getJSON('prices');
        render();
        setText(status, '');
      } catch (err) {
        setText(status, `Couldn't load prices: ${err.message}`);
      }
    }

    store.on('snapshot', render);
    pollWhileSeen(el, load, { periodMs: REFRESH_MS });
  },
};

function renderCards(el, current, now, SOURCE) {
  const sorted = [...current].sort((a, b) => a.source.localeCompare(b.source) * -1 || String(a.model).localeCompare(String(b.model)));
  el.replaceChildren(...sorted.map((c) => {
    const src = SOURCE[c.source] || { name: c.source, tone: 'plain', note: '' };
    return h('article', { class: `price-card tone-${src.tone}` },
      h('p', { class: 'pc-src', title: src.note }, src.name),
      h('p', { class: 'pc-model' }, modelName(c.model), c.provider_model && c.provider_model !== c.model
        ? h('span', { class: 'pc-prov' }, c.provider_model) : null),
      h('dl', { class: 'pc-rates' },
        rate('In', c.input_per_m), rate('Cached', c.cached_per_m), rate('Out', c.output_per_m)),
      h('p', { class: 'pc-when' }, `In force since ${f.date(c.effective_from)} · checked ${f.ago(c.fetched_at, now)}`));
  }));
}

function rate(label, v) {
  return h('div', null, h('dt', null, label), h('dd', null, f.usdPerM(v)));
}

// Newest first; values that changed from the previous card for the same source and model are marked.
function renderHistory(tbody, history, SOURCE) {
  const asc = [...history].sort((a, b) => a.effective_from - b.effective_from);
  const prev = new Map();
  const rows = asc.map((c) => {
    const key = `${c.source}/${c.model}`;
    const p = prev.get(key);
    prev.set(key, c);
    const cell = (field) => h('td', { class: `num${p && p[field] !== c[field] ? ' changed' : ''}`,
      title: p && p[field] !== c[field] ? `was ${f.usdPerM(p[field])}` : null }, f.usdPerM(c[field]));
    return h('tr', null,
      h('td', null, f.date(c.effective_from)),
      h('td', null, (SOURCE[c.source] || { short: c.source }).short),
      h('td', null, modelName(c.model)),
      cell('input_per_m'), cell('cached_per_m'), cell('output_per_m'));
  });
  tbody.replaceChildren(...(rows.length ? rows.reverse() : [h('tr', null, h('td', { colspan: 6, class: 'empty' }, 'No price cards yet.'))]));
}
