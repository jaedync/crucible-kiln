// Recent requests, newest first. New rows arrive from `request` events; older
// pages load with ?before=.
import { h, setText, panelHead } from '../dom.js';
import * as f from '../format.js';
import { upsertRow } from '../rows.js';
import { fallbackBadge } from '../signal_rows.js';
import { referenceShort } from '../pricing.js';

const PAGE = 25;
const SOURCE_TITLE = {
  gateway: 'Through the Kiln gateway',
  direct: 'Bypassed the gateway; seen in the engine log only',
  agent: "Served on another host's own engine; reported by its Kiln agent",
};

export default {
  id: 'requests',
  label: 'Recent requests',
  mount(el, { store, api }) {
    let rows = [];
    let more = true;
    // Rows shown: one page, plus a page per "Load older". A live request pushes the oldest row
    // out, so the table (and every panel below it) keeps its height.
    let limit = PAGE;
    const tbody = h('tbody');
    const status = h('p', { class: 'panel-status', role: 'status' });
    const moreBtn = h('button', { type: 'button', class: 'btn', onclick: loadOlder }, 'Load older');
    el.append(
      panelHead('Recent requests', h('span', { class: 'panel-sub' }, 'Costs locked at the price in force when each ran')),
      h('div', { class: 'scroll-x', tabindex: '0', role: 'region', 'aria-label': 'Recent requests table' },
        h('table', { class: 'tbl req-tbl' }, header(), tbody)),
      h('div', { class: 'req-foot' }, status, moreBtn));

    function draw(newIds = new Set()) {
      const now = store.now();
      tbody.replaceChildren(...(rows.length
        ? rows.map((r) => row(store, r, now, newIds.has(r.id)))
        : [h('tr', null, h('td', { colspan: 10, class: 'empty' }, 'No requests yet.'))]));
      moreBtn.hidden = !more;
    }

    async function load(before) {
      try {
        const res = await api.getJSON('requests', { limit: PAGE, before });
        const page = res.rows || [];
        more = page.length === PAGE;
        const seen = new Set(rows.map((r) => r.id));
        rows = before ? [...rows, ...page.filter((r) => !seen.has(r.id))] : page;
        limit = before ? Math.max(limit, rows.length) : PAGE;
        setText(status, '');
        draw();
      } catch (err) {
        setText(status, `Couldn't load requests: ${err.message}`);
      }
    }

    function loadOlder() {
      const oldest = rows[rows.length - 1];
      if (oldest) load(oldest.ts);
    }

    // Upsert: an engine-enriched copy of a row replaces it where it stands.
    store.on('request', (r) => {
      if (!r || !r.id) return;
      const res = upsertRow(rows, r, limit);
      if (res.added && rows.length >= limit) more = true; // the pushed-out row is one "Load older" away
      rows = res.rows;
      draw(res.added ? new Set([r.id]) : undefined);
    });
    // A reconnect may have missed request events; reload the first page.
    let snapshots = 0;
    store.on('snapshot', () => { if (snapshots++) load(); });
    load();
  },
};

function header() {
  const cols = [['Time'], ['Device'], ['Model'], ['Prompt', 'num'], ['Output', 'num'], ['TTFT', 'num'],
    ['tok/s', 'num'], ['Finish'], ['Cost', 'num'], ['Source']];
  return h('thead', null, h('tr', null, cols.map(([t, c]) => h('th', { scope: 'col', class: c || null }, t))));
}

function finishChip(r) {
  if (r.error || (r.status && r.status >= 400)) {
    return h('span', { class: 'fin fin-bad', title: r.error || '' }, r.status && r.status !== 200 ? `error ${r.status}` : 'error');
  }
  const { text, bad, title } = f.finishInfo(r);
  // Plain text rather than bordered chips, which read as buttons.
  return h('span', { class: `fin${bad ? ' fin-bad' : ''}`, title }, text);
}

// A request whose tool call came back as text (Crucible's tool_call_fallback), beside its finish.
function fallbackNote(r) {
  const b = fallbackBadge(r);
  return b ? h('span', { class: 'fin-note', title: b.title }, b.text) : null;
}

function two(main, sub, title, subTitle) {
  return [h('span', { class: 'c-main', title }, main), sub ? h('span', { class: 'c-sub', title: subTitle }, sub) : null];
}

function row(store, r, now, isNew) {
  const name = f.clientName(r.client);
  return h('tr', { class: isNew ? 'is-new' : null },
    h('td', { class: 'c-time' }, h('time', { datetime: new Date(r.ts * 1000).toISOString() }, f.when(r.ts, now))),
    h('td', { class: 'who' }, h('i', { class: 'swatch', style: { background: store.colorFor(r.client) } }),
      h('span', { title: r.client.key }, name),
      r.client.label ? h('span', { class: 'c-sub', title: 'Label: the X-Kiln-Client header, the container a direct request went to, or the sender a host named' }, r.client.label) : null),
    h('td', { class: 'c-model' }, r.model),
    h('td', { class: 'num' }, two(f.int(r.prompt), r.cached ? `${f.int(r.cached)} cached` : 'none cached')),
    h('td', { class: 'num' }, two(f.int(r.completion), r.reasoning ? `${f.int(r.reasoning)} reasoning` : null)),
    h('td', { class: 'num' }, f.secs(r.ttft_s)),
    h('td', { class: 'num' }, two(f.estRate(r.decode_tps, r.decode_estimated),
      r.prefill_tps ? `${f.estRate(r.prefill_tps, r.prefill_estimated)} prefill` : null,
      r.decode_estimated ? f.ESTIMATED_RATE : null, r.prefill_estimated ? f.ESTIMATED_RATE : null)),
    h('td', null, finishChip(r), fallbackNote(r)),
    h('td', { class: 'num' }, two(f.usd(r.cost && r.cost.reference), r.cost ? `OR ${f.usd(r.cost.openrouter)}` : null,
      `${referenceShort(store.get().pricing)} API price; OR = OpenRouter list price`)),
    h('td', null, h('span', { class: `src src-${r.source}`, title: SOURCE_TITLE[r.source] || null },
      r.source === 'agent' ? r.host : r.source)));
}
