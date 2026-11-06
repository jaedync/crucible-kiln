// Crucible's speculative decoding: how often the target accepted a draft at each position, as
// accepted_i / tested_i over the chosen period. The counters come from the engine's own turn lines,
// so an engine that does not speculate, or a turn without speculation, shows nothing here.
import { h, setText, segmented, panelHead } from '../dom.js';
import * as f from '../format.js';
import { pollWhileSeen } from '../poll.js';

const PERIODS = [['today', 'Today'], ['7d', '7 days'], ['30d', '30 days'], ['all', 'All']];
const REFRESH_MS = 60000;  // day-level sums: no reload after each request
const DEPTHS = 7;  // K = 7 draft positions, the length of every draft_pos_* list

export default {
  id: 'speculation',
  label: 'Speculation',
  mount(el, { api }) {
    let period = 'today';
    let data = null;
    const body = h('tbody');
    const status = h('p', { class: 'panel-status', role: 'status' });
    const notice = h('p', { class: 'panel-sub' });
    const periodSeg = segmented('Speculation period', PERIODS, period, (v) => { period = v; load(); });
    el.append(
      panelHead('Speculation', periodSeg.el),
      h('div', { class: 'scroll-x', tabindex: '0', role: 'region', 'aria-label': 'Draft acceptance per position' },
        h('table', { class: 'tbl spec-tbl' }, head(), body)),
      notice, status);

    async function load() {
      const asked = period;
      try {
        const res = await api.getJSON('speculation', { period });
        if (asked !== period) return;  // the reader picked another period while this one ran
        data = res;
        setText(status, '');
        render();
      } catch (err) {
        setText(status, `Couldn't load speculation: ${err.message}`);
      }
    }

    function render() {
      const rows = data ? data.rows : [];
      const depths = data ? data.positions : DEPTHS;
      body.replaceChildren(...(rows.length ? rows.map(row)
        : [h('tr', null, h('td', { colspan: depths + 4, class: 'empty' },
          'No per-position draft counters stored yet.'))]));
      setText(notice, rows.length
        ? 'Each depth: the drafts the engine proposed there, those the target tested, and its accept rate.'
        : '');
      notice.hidden = !rows.length;
    }

    pollWhileSeen(el, load, { periodMs: REFRESH_MS });
  },
};

function head() {
  const cols = [['Profile'], ['Requests', 'num'],
    ...Array.from({ length: DEPTHS }, (_, i) => [`Depth ${i + 1}`, 'num',
      'Drafts the engine proposed at this position, the ones the target tested (every earlier position was'
      + ' accepted) and the share it accepted.']),
    ['Draft ms/token', 'num', 'Head pass wall time per token the engine generated.'],
    ['Passes', 'num', 'MTP head passes the requests took part in.']];
  return h('thead', null, h('tr', null, cols.map(([t, c, title]) =>
    h('th', { scope: 'col', class: c || null, title: title || null }, t))));
}

function rateCell(rate, tested, accepted, proposed) {
  const title = `${f.int(proposed)} proposed, ${f.int(tested)} tested, ${f.int(accepted)} accepted at this depth`;
  if (tested) return h('td', { class: 'num', title }, f.pct(rate));
  return h('td', { class: 'num', title: 'Nothing was tested at this depth' }, f.DASH);
}

function row(r) {
  return h('tr', null,
    h('td', { class: 'c-model' }, r.profile),
    h('td', { class: 'num' }, f.int(r.requests)),
    ...r.tested.map((tested, i) => rateCell(r.accept_rate[i], tested, r.accepted[i], r.proposed[i])),
    h('td', { class: 'num', title: `${f.int(r.draft_ms)} ms of head passes for ${f.int(r.generated_tokens)} generated tokens` },
      r.ms_per_token === null ? f.DASH : f.one(r.ms_per_token)),
    h('td', { class: 'num' }, f.int(r.passes)));
}
