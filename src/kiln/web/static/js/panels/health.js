// Sticky health line: connection, gateway, an engine not serving, parse errors, last request, and a
// compact copy of the live rate so it stays visible while scrolling.
import { h, setText, setAttr } from '../dom.js';
import * as f from '../format.js';

const CONN_TEXT = {
  connecting: 'Connecting',
  open: 'Live',
  stale: 'No data',
  reconnecting: 'Reconnecting',
};

// Each value sits in a box sized for its widest usual text (css: .hl-<name> .hl-val), so a
// changing number never moves the items after it.
function item(name, label, short = label) {
  const value = h('span', { class: 'hl-val' });
  const el = h('span', { class: `hl-item hl-${name}` },
    h('span', { class: 'hl-label' }, h('span', { class: 'long' }, label), h('span', { class: 'short', 'aria-hidden': 'true' }, short)), value);
  return { el, value };
}

// Red only while parse errors are fresh; older ones in the 24 h window are a note, not an alarm.
const PARSE_FRESH_S = 3600;

export function parseTone(count, lastTs, now) {
  if (!(count > 0)) return null;
  return lastTs != null && now - lastTs > PARSE_FRESH_S ? 'warn' : 'bad';
}

// An engine that is not serving, for the line that stays on screen: the engine card is below the first
// screen on a phone. Stopped is normal (the night window), so it has no colour and a phone leaves it out;
// starting and stopping (a switch) are a warning; failed is an alarm.
export function engineItem(engine) {
  if (!engine || !engine.state || engine.state === 'ready') return null;
  const tone = engine.state === 'failed' ? 'bad' : engine.state === 'stopped' ? null : 'warn';
  return { text: engine.state, tone };
}

export default {
  id: 'health',
  label: 'Health',
  slot: '#health',
  mount(el, { store }) {
    // Only the connection state is a live region; the clock would chatter.
    const conn = h('span', { class: 'hl-conn', 'data-state': 'connecting', role: 'status' },
      h('i', { class: 'hl-dot', 'aria-hidden': 'true' }), h('span'));
    const rate = item('rate', 'Decode');
    const gateway = item('gateway', 'Gateway', 'GW');
    const engine = item('engine', 'Engine');
    const parse = item('parse', 'Parse errors 24h', 'Parse err');
    const last = item('last', 'Last request', 'Last');
    const up = item('up', 'Dash up', 'Up');
    const journal = item('journal', 'Journal');
    const clock = h('time', { class: 'hl-clock' });
    el.append(conn, rate.el, gateway.el, engine.el, parse.el, journal.el, last.el, up.el, clock);
    el.setAttribute('aria-label', 'Health');

    function render() {
      const { conn: c, tick } = store.get();
      const now = store.now();
      conn.dataset.state = c;
      setText(conn.lastChild, CONN_TEXT[c] || c);
      const live = store.isLive() && tick;
      const lv = live ? tick.live : null;
      const approx = lv && !lv.decode_exact && lv.streams.length ? '≈' : '';
      setText(rate.value, lv ? `${approx}${f.rate(lv.decode_tps)} tok/s` : f.DASH);
      setAttr(rate.value, 'title', approx ? lv.decode_note || 'Estimated' : null);

      const hl = tick && tick.health;
      const gw = hl ? hl.gateway : null;
      setText(gateway.value, gw || f.DASH);
      setAttr(gateway.el, 'data-tone', gw === 'up' ? 'ok' : gw ? 'bad' : null);
      const eng = engineItem(store.get().engine);
      engine.el.hidden = !eng;
      setText(engine.value, eng ? eng.text : '');
      setAttr(engine.el, 'data-tone', eng ? eng.tone : null);
      const pe = hl ? hl.parse_errors_24h : null;
      setText(parse.value, f.int(pe));
      setAttr(parse.el, 'data-tone', parseTone(pe, hl && hl.parse_error_last_ts, now));
      setText(last.value, hl ? f.ago(hl.last_request_ts, now) : f.DASH);
      setText(up.value, hl ? f.dur(now - hl.dash_started) : f.DASH);
      // Optional: the engine-log follower's state, when the server reports it.
      journal.el.hidden = !(hl && hl.journal);
      setText(journal.value, hl && hl.journal ? hl.journal : '');
      setAttr(journal.el, 'data-tone', hl && hl.journal && hl.journal !== 'following' ? 'bad' : null);
      setText(clock, f.clock(now));
    }

    store.on('tick', render);
    store.on('conn', render);
    store.on('engine', render);
    setInterval(render, 1000);
    render();
  },
};
