// Crucible server signals per day and engine: server errors and tool calls that came back as text.
// Counts only. Kiln stores no message text for either.
import { h, setText, panelHead } from '../dom.js';
import * as f from '../format.js';
import { daySummary, errorTitle, hasSignal, quietSummary } from '../signal_rows.js';
import { pollWhileSeen } from '../poll.js';

const REFRESH_S = 60;  // day-level counts: no reload after each request
const DAYS = 14;

export default {
  id: 'signals',
  label: 'Server signals',
  mount(el, { store, api }) {
    const body = h('tbody');
    const status = h('p', { class: 'panel-status', role: 'status' });
    const summary = h('p', { class: 'panel-sub sig-summary' });
    const toggle = h('button', { type: 'button', class: 'btn', onclick: () => { all = !all; render(); } });
    const wrap = h('div', { class: 'scroll-x', tabindex: '0', role: 'region', 'aria-label': 'Server signals per day' },
      h('table', { class: 'tbl signals-tbl' }, head(), body));
    el.append(
      panelHead('Server signals', h('span', { class: 'panel-sub' }, 'Errors and tool calls returned as text. Counts only.')),
      summary, wrap, h('div', { class: 'req-foot' }, status, toggle));

    let rows = [];
    let all = false; // quiet days (every count zero) are hidden until asked for

    function render() {
      const shown = all ? rows : rows.filter(hasSignal);
      setText(summary, all ? '' : quietSummary(rows, DAYS));
      summary.hidden = all || !rows.length;
      body.replaceChildren(...(shown.length ? shown.map(row)
        : [h('tr', null, h('td', { colspan: 7, class: 'empty' }, rows.length ? 'Nothing to show.' : 'No requests yet.'))]));
      wrap.hidden = !all && rows.length > 0 && !shown.length;
      toggle.hidden = rows.length === shown.length && !all;
      setText(toggle, all ? 'Hide quiet days' : `Show all ${rows.length} rows`);
    }

    async function load() {
      try {
        const res = await api.getJSON('signals', { days: DAYS });
        rows = res.rows || [];
        render();
        setText(status, '');
      } catch (err) {
        setText(status, `Couldn't load server signals: ${err.message}`);
      }
    }

    pollWhileSeen(el, load, { periodMs: REFRESH_S * 1000 });
  },
};

function head() {
  const cols = [['Day'], ['Engine'], ['Requests', 'num'], ['Errors', 'num', errorTitle()],
    ['Stream errors', 'num', 'Errors that ended a stream after HTTP 200 was sent (a part of Errors).'],
    ['Tool calls as text', 'num', 'Requests whose model output was not a valid tool call and came back as content.'],
    ['Client errors', 'num', 'Requests the server refused with HTTP 400-499 before the stream, for example a path'
      + ' it does not have. Not a part of Errors.']];
  return h('thead', null, h('tr', null, cols.map(([t, c, title]) =>
    h('th', { scope: 'col', class: c || null, title: title || null }, t))));
}

function count(c) {
  return h('td', { class: 'num' }, c.tone ? h('span', { class: `sig sig-${c.tone}` }, c.text) : c.text);
}

function row(d) {
  const s = daySummary(d);
  return h('tr', null,
    h('td', null, d.date), h('td', null, d.engine), h('td', { class: 'num' }, f.int(d.requests)),
    count(s.errors), count(s.streamErrors), count(s.fallbacks), count(s.clientErrors));
}
