// Prompt cache: per conversation, how much of each turn's prompt the engine read
// again, and whether the client or the engine caused it. Kiln stores no content:
// conversations are matched in the gateway's memory and only counts are kept.
import { h, setText, panelHead } from '../dom.js';
import * as f from '../format.js';
import { CAUSE, causeTitle, engineNote } from '../cache_rows.js';
import { pollWhileSeen } from '../poll.js';

// The daily table and the conversation list change slowly: no reload after each request. The list
// carries each conversation's sums; its turns come only for the conversations open here.
const REFRESH_S = 60;
const SESSIONS_SHOWN = 8; // conversations listed before "Show all"

export default {
  id: 'cache',
  label: 'Prompt cache',
  mount(el, { store, api }) {
    const counters = h('p', { class: 'panel-sub cache-counters' });
    const daily = h('tbody');
    const sessions = h('div', { class: 'cache-sessions' });
    const status = h('p', { class: 'panel-status', role: 'status' });
    const sessHead = h('h3', { class: 'kicker cache-sess-title' });
    const moreBtn = h('button', { type: 'button', class: 'btn', onclick: () => { showAll = !showAll; drawSessions(); } });
    el.append(
      panelHead('Prompt cache', h('span', { class: 'panel-sub' }, 'Why turns read their prompt again. No content is stored.')),
      counters,
      h('div', { class: 'scroll-x', tabindex: '0', role: 'region', 'aria-label': 'Re-read tokens per day' },
        h('table', { class: 'tbl cache-daily' }, dailyHead(), daily)),
      sessHead, sessions, h('div', { class: 'req-foot' }, status, moreBtn));

    let open = new Set();
    let list = [];
    let poller = null;
    let showAll = false;

    // Flagged conversations first (newest first within each), then the rest.
    function drawSessions() {
      const flagged = (s) => (s.flagged || s.client_changes || s.refusals ? 1 : 0);
      const ordered = [...list].sort((a, b) => flagged(b) - flagged(a) || lastTs(b) - lastTs(a));
      const shown = showAll ? ordered : ordered.slice(0, SESSIONS_SHOWN);
      const n = ordered.filter(flagged).length;
      setText(sessHead, list.length ? `Conversations, last 24 h · ${f.int(list.length)} · ${f.int(n)} flagged` : '');
      // A rebuild must not drop keyboard focus from the conversation the user is on.
      const had = document.activeElement && document.activeElement.closest('.cache-session');
      const focusConv = had && sessions.contains(had) ? had.dataset.conv : null;
      sessions.replaceChildren(...shown.map((s) => sessionBlock(store, s, open, () => poller.now())));
      if (focusConv) {
        const again = [...sessions.children].find((d) => d.dataset.conv === focusConv);
        if (again) again.querySelector('summary').focus({ preventScroll: true });
      }
      moreBtn.hidden = list.length <= SESSIONS_SHOWN;
      setText(moreBtn, showAll ? 'Show fewer' : `Show all ${list.length}`);
    }

    async function load() {
      try {
        const res = await api.getJSON('cache', { hours: 24, days: 14, open: [...open].join(',') || null });
        daily.replaceChildren(...(res.daily.length ? res.daily.map(dailyRow)
          : [h('tr', null, h('td', { colspan: 8, class: 'empty' }, 'No turns yet.'))]));
        list = res.sessions;
        // Conversations that left the list are closed: the open set stays small (the server takes 30).
        open = new Set([...open].filter((c) => list.some((s) => s.conv === c)));
        drawSessions();
        setText(status, res.sessions.length ? '' : 'No conversations in the last 24 hours.');
      } catch (err) {
        setText(status, `Couldn't load cache data: ${err.message}`);
      }
    }

    function drawCounters() {
      const c = store.get().engine && store.get().engine.cache;
      setText(counters, c ? `Engine cache now: ${f.int(c.entries)} of ${f.int(c.max_entries)} entries · ${f.gib(c.bytes)} · `
        + `${f.int(c.hits)} hits, ${f.int(c.misses)} misses, ${f.int(c.evicted)} evicted, ${f.int(c.refused)} refused since start` : '');
      counters.hidden = !c;
    }

    store.on('tick', drawCounters);
    poller = pollWhileSeen(el, load, { periodMs: REFRESH_S * 1000 });
  },
};

function dailyHead() {
  const cols = [['Day'], ['Profile'], ['Turns', 'num'], ['Computed', 'num'], ['Re-read', 'num'],
    ['Engine misses', 'num'], ['Client changes', 'num'], ['Save refusals', 'num']];
  return h('thead', null, h('tr', null, cols.map(([t, c]) => h('th', { scope: 'col', class: c || null }, t))));
}

function dailyRow(d) {
  return h('tr', null, h('td', null, d.date), h('td', null, d.profile || f.DASH), h('td', { class: 'num' }, f.int(d.turns)),
    h('td', { class: 'num' }, f.compact(d.computed)),
    h('td', { class: 'num', title: 'Prompt tokens the engine read again although the client kept them' }, f.compact(d.reread)),
    h('td', { class: 'num' }, f.int(d.engine_misses)), h('td', { class: 'num' }, f.int(d.client_changes)),
    h('td', { class: 'num' }, f.int(d.refusals)));
}

const lastTs = (s) => s.last || 0;

// fetchTurns: load again, now with this conversation open, when it opens without its turns.
function sessionBlock(store, s, open, fetchTurns) {
  const summary = h('summary', null,
    h('time', { class: 'cs-time' }, s.last ? f.when(s.last, store.now()) : f.DASH),
    h('i', { class: 'swatch', style: { background: s.client.color || store.colorFor(s.client) } }),
    h('span', { class: 'cs-who' }, s.client.name),
    h('span', { class: 'cs-meta' }, `${f.plural(s.n_turns, 'turn')} · last prompt ${f.compact(s.last_prompt)} · `
      + `re-read ${f.compact(s.reread)}`),
    s.flagged ? h('span', { class: 'cs-flag cs-bad' }, `${s.flagged} engine ${s.flagged === 1 ? 'miss' : 'misses'}`) : null,
    s.client_changes ? h('span', { class: 'cs-flag cs-warn' }, `${s.client_changes} client ${s.client_changes === 1 ? 'change' : 'changes'}`) : null,
    s.refusals ? h('span', { class: 'cs-flag cs-bad' }, `${s.refusals} save ${s.refusals === 1 ? 'refusal' : 'refusals'}`) : null);
  const details = h('details', { class: 'cache-session', 'data-conv': s.conv, open: open.has(s.conv) || null,
    ontoggle: (e) => {
      if (!e.target.open) open.delete(s.conv);
      else if (!open.has(s.conv)) {
        open.add(s.conv);
        if (!s.turns) fetchTurns();
      }
    } },
  summary, s.turns ? turnsTable(store, s.turns) : h('p', { class: 'panel-status' }, 'Loading turns…'));
  return details;
}

function turnsTable(store, turns) {
  const now = store.now();
  const head = h('thead', null, h('tr', null, [['Time'], ['Prompt', 'num'], ['Cached', 'num'], ['Added', 'num'],
    ['Re-read', 'num'], ['TTFT', 'num'], ['Cause'], ['Engine']].map(([t, c]) => h('th', { scope: 'col', class: c || null }, t))));
  const rows = turns.slice().reverse().map((t) => {
    const cause = CAUSE[t.cause] || CAUSE.unknown;
    return h('tr', null,
      h('td', { class: 'c-time' }, f.when(t.ts_start, now)),
      h('td', { class: 'num' }, f.int(t.prompt)), h('td', { class: 'num' }, f.int(t.cached)),
      h('td', { class: 'num', title: 'Estimate: this prompt minus the previous prompt' },
        t.appended_tokens == null ? f.DASH : `≈${f.int(t.appended_tokens)}`),
      h('td', { class: 'num' }, t.reread_tokens == null ? f.DASH : `≈${f.int(t.reread_tokens)}`),
      h('td', { class: 'num' }, f.secs(t.ttft_s)),
      h('td', null, h('span', { class: `cause cause-${cause.tone || 'none'}`, title: causeTitle(t) }, cause.text)),
      h('td', { class: 'c-sub' }, engineNote(t)));
  });
  return h('div', { class: 'scroll-x' }, h('table', { class: 'tbl cache-turns' }, head, h('tbody', null, rows)));
}
