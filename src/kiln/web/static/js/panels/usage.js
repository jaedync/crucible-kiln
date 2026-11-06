// Usage: what every host did over a period (this host and the hosts whose agents report), who used it, and what that would have
// cost from an API. Costs come from the server, priced once per request.
import { h, svg, setText, segmented, panelHead } from '../dom.js';
import * as f from '../format.js';
import { referenceName, referenceShort } from '../pricing.js';
import { pollWhileSeen } from '../poll.js';
import { usageBars } from '../usage_bars.js';

const PERIODS = [['today', 'Today'], ['7d', '7 days'], ['30d', '30 days'], ['all', 'All']];
const GROUPS = [['device', 'Device'], ['model', 'Model'], ['profile', 'Profile'], ['host', 'Host']];
const REFRESH_MS = 60000;
const AFTER_REQUEST_MS = 1500;
const MIN_GAP_MS = 5000;  // agents end a request every few seconds: one reload per 5 s is enough
const RING_MIN_SHARE = 0.005; // smaller shares join one "Others" line

export default {
  id: 'usage',
  label: 'Usage and cost',
  mount(el, { store, api }) {
    let period = 'today', group = 'device', data = null;
    const head = headline();
    const table = h('table', { class: 'tbl usage-tbl' });
    const ring = shareRing();
    const bars = usageBars();
    const status = h('p', { class: 'panel-status', role: 'status' });
    const periodSeg = segmented('Usage period', PERIODS, period, (v) => { period = v; load(); });
    const groupSeg = segmented('Group usage by', GROUPS, group, (v) => { group = v; load(); });
    el.append(
      panelHead('Usage', periodSeg.el),
      h('div', { class: 'usage-body' },
        head.el,
        h('div', { class: 'usage-table' }, h('div', { class: 'usage-tools' }, h('span', { class: 'kicker' }, 'By'), groupSeg.el),
          h('div', { class: 'scroll-x', tabindex: '0', role: 'region', 'aria-label': 'Usage table' }, table), status),
        ring.el, bars.el));

    async function load() {
      const asked = `${period}/${group}`;
      try {
        const res = await api.getJSON('usage', { period, group });
        if (asked !== `${period}/${group}`) return;
        data = res;
        setText(status, '');
        render();
      } catch (err) {
        setText(status, `Couldn't load usage: ${err.message}`);
      }
    }

    function color(row, i) {
      return group === 'device' ? store.colorFor(row.key, row.color) : store.palette[i % store.palette.length];
    }

    function render() {
      if (!data) return;
      const rows = [...data.rows].sort((a, b) => b.cost.reference - a.cost.reference);
      const pricing = store.get().pricing;
      head.render(data, period, pricing);
      renderTable(table, rows, data.totals, group, color, referenceShort(pricing));
      ring.render(rows, data.totals, color);
      bars.render(data.buckets, rows, color, store.now(), data.period);
    }

    const poller = pollWhileSeen(el, load, { periodMs: REFRESH_MS, minGapMs: MIN_GAP_MS });
    store.on('request', () => poller.poke(AFTER_REQUEST_MS));
    store.on('clients', render);
    store.on('snapshot', render); // brings the reference price name
  },
};

function headline() {
  const big = h('p', { class: 'cost-big' });
  const alt = h('p', { class: 'cost-alt' });
  const meta = h('p', { class: 'cost-meta' });
  const when = h('p', { class: 'kicker' });
  const src = h('p', { class: 'cost-src' });
  const el = h('div', { class: 'cost-head' }, when, big, src, alt, meta);

  function render(data, period, pricing) {
    const t = data.totals;
    // Every host's work, unlike the masthead's today figures (this host only).
    setText(when, `What ${period === 'today' ? 'today' : period === 'all' ? 'all of it' : `the last ${period}`} would have cost via API · all hosts`);
    setText(src, `at ${referenceName(pricing)} API prices`);
    const price = f.usd(t.cost.reference);
    setText(big, price);
    big.style.setProperty('--chars', String(price.length));  // kiln.css sizes the price to fit its column
    setText(alt, `${f.usd(t.cost.openrouter)} at OpenRouter list price for the same model`);
    setText(meta, `${f.plural(t.requests, 'request')} · ${f.compact(t.prompt + t.completion)} tokens · ${f.dur(t.busy_s)} busy`);
  }

  return { el, render };
}

const COLS = [
  ['requests', 'Requests', (r) => f.int(r.requests)],
  ['computed', 'Computed', (r) => f.compact(r.computed), (r) => f.int(r.computed)],
  ['cached', 'Cached', (r) => f.compact(r.cached), (r) => f.int(r.cached)],
  ['completion', 'Output', (r) => f.compact(r.completion), (r) => f.int(r.completion)],
  ['reasoning', 'Reasoning', (r) => f.compact(r.reasoning), (r) => f.int(r.reasoning)],
  ['busy', 'Busy', (r) => f.dur(r.busy_s)],
  ['ref', null, (r) => f.usd(r.cost.reference)],  // header: the reference source's short name
  ['or', 'OpenRouter', (r) => f.usd(r.cost.openrouter)],
];

function renderTable(table, rows, totals, group, color, refShort) {
  const label = GROUPS.find((g) => g[0] === group)[1];
  const thead = h('thead', null,
    h('tr', { class: 'th-group' }, h('th', { scope: 'col' }), h('th', { scope: 'col' }),
      h('th', { scope: 'colgroup', colspan: '2' }, 'Prompt tokens'), h('th', { scope: 'col', colspan: '3' }),
      h('th', { scope: 'colgroup', colspan: '2' }, 'API cost')),
    h('tr', null, h('th', { scope: 'col' }, label), ...COLS.map(([k, t]) => h('th', { scope: 'col', class: `num c-${k}` }, t || refShort))));
  const body = h('tbody', null, rows.length ? rows.map((r, i) => h('tr', null,
    h('th', { scope: 'row', class: 'who' }, h('i', { class: 'swatch', style: { background: color(r, i) } }),
      h('span', { title: r.key }, r.name || r.key),
      r.errors ? h('span', { class: 'err-badge', title: `${r.errors} failed requests` }, `${r.errors} err`) : null),
    ...COLS.map(([k, , fn, full]) => h('td', { class: `num c-${k}`, title: full ? full(r) : null }, fn(r)))))
    : h('tr', null, h('td', { colspan: COLS.length + 1, class: 'empty' }, 'No requests in this period.')));
  const foot = h('tfoot', null, h('tr', null, h('th', { scope: 'row' }, 'Total'),
    ...COLS.map(([k, , fn, full]) => h('td', { class: `num c-${k}`, title: full ? full(totals) : null }, fn(totals)))));
  table.replaceChildren(thead, body, foot);
}

function shareRing() {
  const R = 52, C = 2 * Math.PI * R;
  const g = svg('g', { transform: 'rotate(-90 64 64)' });
  const center = svg('text', { x: 64, y: 62, 'text-anchor': 'middle', class: 'ring-num' });
  const centerSub = svg('text', { x: 64, y: 80, 'text-anchor': 'middle', class: 'ring-sub' }, 'busy');
  const chart = svg('svg', { viewBox: '0 0 128 128', class: 'ring', role: 'img' }, g, center, centerSub);
  const legend = h('ul', { class: 'ring-legend' });
  const el = h('div', { class: 'usage-ring' }, h('p', { class: 'kicker' }, 'Share of busy time'),
    h('div', { class: 'ring-body' }, chart, legend));

  function render(rows, totals, color) {
    const sum = rows.reduce((a, r) => a + r.busy_s, 0);
    let offset = 0;
    const arcs = [svg('circle', { cx: 64, cy: 64, r: R, fill: 'none', class: 'ring-track', 'stroke-width': 20 })];
    const items = [];
    let small = 0, smallN = 0;
    rows.forEach((r, i) => {
      const share = sum ? r.busy_s / sum : 0;
      if (share > 0) {
        arcs.push(svg('circle', { cx: 64, cy: 64, r: R, fill: 'none', stroke: color(r, i), 'stroke-width': 20,
          'stroke-dasharray': `${share * C} ${C}`, 'stroke-dashoffset': -offset * C }));
      }
      offset += share;
      if (share < RING_MIN_SHARE) {
        small += share;
        smallN += 1;
        return;
      }
      items.push(h('li', null, h('i', { class: 'swatch', style: { background: color(r, i) } }),
        h('span', { class: 'rl-name', title: r.name || r.key }, r.name || r.key), h('span', { class: 'rl-pct' }, f.pct(share))));
    });
    if (smallN) {
      items.push(h('li', { class: 'rl-other' }, h('i', { class: 'swatch', style: { background: 'var(--paper-2)' } }),
        h('span', { class: 'rl-name' }, `${f.plural(smallN, 'other')}`), h('span', { class: 'rl-pct' }, small > 0 ? '<1%' : '0%')));
    }
    g.replaceChildren(...arcs);
    center.textContent = f.dur(totals.busy_s);
    chart.setAttribute('aria-label', `Busy time share: ${rows.map((r) => `${r.name || r.key} ${f.pct(sum ? r.busy_s / sum : 0)}`).join(', ')}`);
    legend.replaceChildren(...items);
  }

  return { el, render };
}
