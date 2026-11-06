// The rack panel (/panel): live data from the dashboard's WebSocket, the fleet's GPU queue from
// GET /api/v1/panel. It draws when a tick arrives (twice a second) and once a second without ticks.
// It runs on the machine that runs the engine, in software rendering, beside the engine: every frame is CPU and memory traffic
// the engine shares. Drawing counts between ticks (ten frames a second) cost about 7% of a core; counts
// now move twice a second. No CSS animation, which would draw 60 frames a second.
// The page shown is the rack's shared screen (sync_model.js): a swipe or a tab here moves every following
// session, and the dashboard does the idle return and the alarm jump once for all of them. The tab strip
// (tabs.js) sits under every page but Home; on Home a tap shows it for 10 s.
import { createStore } from '../store.js';
import { connectLive } from '../ws.js';
import { getJSON } from '../api.js';
import { h, setText } from '../dom.js';
import { CONN_GRACE_S, HERO_SPAN_S, alarmInfo, alarmKinds, busyColumns, decodeColumns, dimNow } from './glance_model.js';
import * as m from './model.js';
import { PAGES } from './pages.js';
import * as sync from './sync_model.js';
import { tabStrip } from './tabs.js';
import * as tm from './tabs_model.js';
import { refit } from './view.js';

const TREND_S = 120;
const IDLE_DRAW_MS = 1000;  // the clock, elapsed times and the no-data alarm move without ticks
const POLL_MS = 1000;
const USAGE_POLL_MS = 15000;  // today's clients: a REST view, not in the live feed
const FETCH_TIMEOUT_MS = 3000;  // a dashboard that accepts but never answers must not pile up requests
const CLOCK_FMT = new Intl.DateTimeFormat([], { hour: '2-digit', minute: '2-digit' });

// link: a stand-in for the live socket ({ send }), for the demo build; pinHash false: the hash names no pin.
export function boot({ store, live = true, panel = null, usage = null, link: demoLink = null, pinHash = true }) {
  const root = document.getElementById('pages');
  const status = document.getElementById('status');
  const pages = PAGES.map((make) => make());
  const ids = pages.map((p) => p.id);
  // #fleet (a page id) pins that page in this session only: it never sends and never follows the rack, so
  // screenshots (tools/shoot_panel.py) and a look around on a laptop do not move the rack.
  const pinned = () => (pinHash && ids.includes(location.hash.slice(1)) ? location.hash.slice(1) : null);
  // Monotonic time (performance.now) for the sync timers: a clock step must not end them early or late.
  let sess = sync.initial({ pin: pinned(), solo: !live && !demoLink });
  let link = demoLink;  // { send } once the live socket starts
  let page = 0;  // the index drawn last: paging turns from it
  let stripUntil = 0;  // on Home, the strip shows until then after a tap (monotonic ms)
  let stripOn = false;  // as drawn last: a tap on Home first shows the strip, a second one opens a tab
  let lastTick = performance.now();  // "no data" counts from the last tick, not from when the feed went stale
  // Per-second decode and prefill for the glance page's 10 minutes of columns.
  let decode = { last: null, values: [], prefill: [] };
  let heroCols = decodeColumns(decode);
  let busy = [];
  let busyTimed = [];  // [[hw.ts, busy %]]: the night columns are clock windows
  let busyCols = busyColumns(busyTimed);
  let power = [];
  let hwTs = null;
  let panelData = panel;
  let usageData = usage;

  const strip = tabStrip(status);
  root.append(...pages.map((p) => p.el));
  // A page that throws must not leave the last good frame looking live.
  const faultText = h('span');
  const fault = h('div', { class: 'fault', hidden: true }, h('b', null, 'PANEL ERROR'), faultText);
  document.body.append(fault);

  store.on('snapshot', (snap) => {
    sess = sync.snapshot(sess, snap);
    lastTick = performance.now();
    decode = m.seedSeconds(snap.series_1s, HERO_SPAN_S);
    heroCols = decodeColumns(decode);
  });
  store.on('tick', (tick) => {
    lastTick = performance.now();
    decode = m.addSeconds(decode, tick.seconds, HERO_SPAN_S);
    heroCols = decodeColumns(decode);
    const hw = tick.hw || {};
    if (hw.ts && hw.ts !== hwTs) {
      hwTs = hw.ts;
      busy = m.pushSample(busy, hw.gpu_busy_pct ?? null, TREND_S);
      busyTimed = m.pushTimed(busyTimed, hw.ts, hw.gpu_busy_pct ?? null, HERO_SPAN_S);
      busyCols = busyColumns(busyTimed);
      power = m.pushSample(power, hw.power_w ?? null, TREND_S);
    }
    draw();  // as soon as data arrives: a fixed timer showed it up to a second late, by uneven amounts
    schedule();
  });

  store.on('panel', (msg) => {
    sess = sync.received(sess, msg, performance.now());
    draw();  // at once: another session's change must not wait for the next tick
  });
  store.on('panel.refused', (msg) => {
    sess = sync.refused(sess, msg, performance.now());
    draw();
  });
  window.addEventListener('hashchange', () => {
    sess = sync.setPin(sess, pinned());
    draw();
  });

  // One timer, restarted by every draw a tick makes: while ticks come (2 Hz) it never fires; without
  // ticks it keeps the clock and the alarm going.
  let timer = null;
  function schedule() {
    clearTimeout(timer);
    timer = setTimeout(() => { draw(); schedule(); }, IDLE_DRAW_MS);
  }

  function draw() {
    try {
      paint();
      fault.hidden = true;
    } catch (err) {
      console.error('panel draw failed', err);
      setText(faultText, String((err && err.message) || err).slice(0, 120));
      fault.hidden = false;
    }
  }

  function paint() {
    const s = store.get();
    const now = store.now();
    const mono = performance.now();
    const connDownS = s.conn === 'open' ? 0 : (mono - lastTick) / 1000;
    sess = sync.expire(sess, mono);
    const was = page;
    const tabs = tm.tabsShown(tm.fleetShown(s.engine));
    page = Math.max(0, ids.indexOf(tm.screenShown(sync.shown(sess, { noData: connDownS >= CONN_GRACE_S }), tabs)));
    if (page !== was && pages[was].leave) pages[was].leave();  // a sub-view (Settings rules) is not kept
    const id = ids[page];
    const settings = sync.settingsShown(sess);
    const mute = sync.muteShown(sess, now);
    const hw = (s.tick && s.tick.hw) || {};
    const alarmIn = { engine: s.engine, hw, connDownS };
    const view = { tick: s.tick, engine: s.engine, now, heroCols, busy, fleet: tabs.includes('fleet'),
      busyCols, power, panel: panelData, usage: usageData, pricing: s.pricing, profiles: s.profiles, connDownS,
      settings, canEdit: sync.canEdit(sess), mute, alarm: alarmInfo({ ...alarmIn, now, mute }) };
    // Screen at night: Dim is one class on the body, so it costs no draw of its own. Never during an alarm,
    // a muted one included: its red gauge stays bright.
    document.body.classList.toggle('is-dim', dimNow({ settings, engine: s.engine, alarm: alarmKinds(alarmIn).length > 0 }));
    // On Home the strip lies over the page's foot while shown; elsewhere it has its own 60 px row.
    stripOn = tm.stripShown({ screen: id, stripUntil, note: sess.note }, mono);
    document.body.classList.toggle('is-stale', s.conn !== 'open');
    document.body.classList.toggle('is-full', Boolean(pages[page].full));
    document.body.classList.toggle('strip-over', Boolean(pages[page].full) && stripOn);
    document.body.dataset.page = id;
    strip.update({ conn: s.conn, screen: id, tabs, flags: tm.tabFlags(sync.flagsShown(sess)), mark: sync.mark(sess),
      cell: tm.syncCell(sess, id, now),
      clock: CLOCK_FMT.format(new Date()) });
    pages.forEach((p, i) => { p.el.hidden = i !== page; });
    pages[page].update(view);
  }

  // Swipe left or right to page; a tap on a tab opens its screen. Pointer events cover touch and mouse.
  function apply({ state, send }) {
    sess = state;
    if (send && link) link.send(send);
  }
  function turnPage(dir) {
    const tabs = tm.tabsShown(tm.fleetShown(store.get().engine));
    apply(sync.act(sess, tm.turnTo(ids[page], tabs, dir), performance.now()));
    draw();
  }
  // A tap: the element under the finger at its start, and whether the strip was on screen then.
  function tap(target, shown) {
    const now = performance.now();
    const tabEl = target && target.closest ? target.closest('[data-tab]') : null;
    const a = tm.tapAction({ shown, tab: tabEl ? tabEl.dataset.tab : null, current: ids[page] });
    const done = a.open ? null : pages[page].tap ? pages[page].tap(target) : null;
    if (a.open) {
      stripUntil = 0;
      apply(sync.act(sess, a.open, now));
    } else if (done && done.settings) {
      apply(sync.actSettings(sess, done.settings, now));
    } else if (done && 'mute' in done) {
      apply(sync.actMute(sess, done.mute, now, store.now()));
    } else {
      if (a.same && pages[page].leave) pages[page].leave();
      if (a.strip) stripUntil = tm.stripAfterTap(now);
      apply(sync.input(sess, now));  // a person is here: the rack's idle time restarts
    }
    draw();
  }
  let start = null;
  document.addEventListener('pointerdown', (e) => {
    start = { x: e.clientX, y: e.clientY, moved: 0, target: e.target, shown: stripOn };
  });
  document.addEventListener('pointermove', (e) => {  // no draw: only how far the finger went
    if (start) start.moved = Math.max(start.moved, Math.hypot(e.clientX - start.x, e.clientY - start.y));
  });
  document.addEventListener('pointerup', (e) => {
    if (!start) return;
    const g = m.gesture(start, { x: e.clientX, y: e.clientY }, start.moved);
    const { target, shown } = start;
    start = null;
    if (g.dir) turnPage(g.dir);
    else if (g.tap) tap(target, shown);
    else apply(sync.input(sess, performance.now()));
  });
  document.addEventListener('pointercancel', () => { start = null; });
  document.addEventListener('keydown', (e) => {  // for a keyboard during setup
    if (e.key === 'ArrowRight' || e.key === 'ArrowLeft') turnPage(e.key === 'ArrowRight' ? 1 : -1);
    else apply(sync.input(sess, performance.now()));
  });

  if (live) {
    link = connectLive(store);
    let polling = false;
    const poll = async () => {
      if (polling) return;
      polling = true;
      try {
        panelData = await getJSON('panel', {}, { timeoutMs: FETCH_TIMEOUT_MS });
      } catch (err) {
        panelData = null;  // the tile then says "no fleet data" instead of keeping an old lease
      } finally {
        polling = false;
      }
    };
    poll();
    setInterval(poll, POLL_MS);
    const pollUsage = async () => {
      const host = store.get().tick && store.get().tick.host;
      if (!host) return;  // tick.today is this host only, so the client list must be too
      try {
        usageData = await getJSON('usage', { period: 'today', group: 'device', host }, { timeoutMs: FETCH_TIMEOUT_MS });
      } catch (err) {
        usageData = null;
      }
    };
    store.on('snapshot', pollUsage);
    setInterval(pollUsage, USAGE_POLL_MS);
  }
  draw();
  schedule();
  // Text fitted before the fonts loaded was measured with a fallback font: measure it again.
  // A transform, not a new layout: the text fitting (view.js fit) measures layout sizes, which it keeps.
  const fitWindow = () => {
    const f = m.fitScreen(window.innerWidth, window.innerHeight);
    document.body.style.transform = f ? `translate(${f.x}px, ${f.y}px) scale(${f.s})` : '';
    document.documentElement.classList.toggle('is-scaled', Boolean(f));
  };
  fitWindow();
  window.addEventListener('resize', fitWindow);
  if (document.fonts) document.fonts.ready.then(() => { refit(); draw(); });
  return { draw, pages };
}

// The demo build (tools/build_panel_demo.py) sets window.KILN_PANEL_DEMO and boots with fixed data.
if (!globalThis.KILN_PANEL_DEMO) boot({ store: createStore() });
