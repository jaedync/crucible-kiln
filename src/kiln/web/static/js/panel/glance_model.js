// What the rack panel's glance page says (pure; tools/web_tests/glance.test.mjs). The glance page is read
// from across the room: a colour block for the state, one large number, and one lane per engine slot.
import { DASH, ago, compact, dur, hm, int, rate } from '../format.js';
import { estimatePrefill, holdPrefill, prefillShare } from '../progress.js';
import { streamCount } from '../count.js';
import { streamDraft } from '../draft.js';
import { columns, degrees, leaseInfo, perSecond, splitLabel, watts } from './model.js';

export const HOT_EDGE_C = 90;
// Values grey out after 2.5 s without a tick (ws.js STALE_MS); the red alarm waits longer, so a dashboard
// restart or a short stall is not an alarm, and NO DATA means the dashboard is really gone.
export const CONN_GRACE_S = 20;
// The hero's column chart: the last 10 minutes in 10 s windows, 60 columns of about 9 px.
export const HERO_COLUMNS = 60;
export const HERO_WINDOW_S = 10;
export const HERO_SPAN_S = HERO_COLUMNS * HERO_WINDOW_S;
export const MAX_LANES = 6;  // a 6-slot engine gets a lane for each slot; a seventh would be too thin to read
const FULL_LANES = 4;  // up to this many lanes have full-size text; more are compact (css/panel.css)
const SERVING = new Set(['ready', 'starting', 'stopping', 'failed']);
const missing = (v) => v === null || v === undefined || Number.isNaN(v);

// -- mode and alarm ---------------------------------------------------------------------------------
// The alarms that hold now, most urgent first. NO DATA is this session's own; the others are the
// dashboard's alarm kinds (live/panel_screen.ALARM_KINDS), which a person may mute.
export function alarmKinds({ engine, hw, connDownS }) {
  return [connDownS >= CONN_GRACE_S && 'nodata', engine && engine.state === 'failed' && 'failed',
    hw && hw.edge_c >= HOT_EDGE_C && 'hot'].filter(Boolean);
}

function alarmText(kind, engine, hw, now) {
  if (kind === 'nodata') return { title: 'NO DATA', sub: 'The panel cannot reach the Kiln dashboard' };
  if (kind === 'failed') {
    return { title: 'ENGINE FAILED', sub: `${splitLabel(engine.label || engine.profile)[0]} · ${ago(engine.since, now)}` };
  }
  const fan = missing(hw.fan_rpm) ? '' : ` · fan ${int(hw.fan_rpm)} rpm`;
  return { title: 'GPU TOO HOT', sub: `Edge ${Math.round(hw.edge_c)}°C${fan}` };
}

// The alarm that takes the screen: the first that holds and is not muted (`mute`: the mute in force).
export function alarmInfo({ engine, hw, connDownS, now, mute = null }) {
  const kind = alarmKinds({ engine, hw, connDownS }).find((k) => !mute || k !== mute.kind);
  return kind ? { kind, ...alarmText(kind, engine, hw, now) } : null;
}

export function glanceMode(v) {
  if (alarmInfo({ ...v, now: 0 })) return 'alarm';
  return v.engine && (SERVING.has(v.engine.state) || v.engine.target) ? 'serve' : 'fleet';
}

// The alarm screen's one button. NO DATA has none: the dashboard cannot hold a mute for what only this
// session sees. A session that may not act sees it dashed.
export function muteButton(alarm, editable) {
  if (!alarm || (alarm.kind !== 'hot' && alarm.kind !== 'failed')) return { show: false, kind: null, editable: false, note: '' };
  return { show: true, kind: alarm.kind, editable: Boolean(editable),
    note: !editable ? 'View only: this device\nmay not mute.'
      : alarm.kind === 'hot' ? 'The red screen stops.\nThe red gauge stays.' : 'The red screen stops.\nEngine shows the failure.' };
}

// "Alarm muted · 28 m" while a mute holds: whole minutes, so the tag repaints at most once a minute.
export function muteTag(mute, now) {
  if (!mute || !(mute.until > now)) return '';
  return `Alarm muted · ${Math.ceil((mute.until - now) / 60)} m`;
}

// Screen at night: Dim draws the same boxes in low light, in the night window only and never during an
// alarm (`alarm`: any kind holds, also a muted one, so its red gauge stays bright).
export function dimNow({ settings, engine, alarm }) {
  const sched = engine && engine.schedule;
  return Boolean(settings && settings.night_display === 'dim' && sched && sched.in_day === false && !alarm);
}

// The state block has no kicker line: the power and temperature cells under it took that height.
export function stateTile(engine, now) {
  const name = splitLabel(engine && (engine.label || engine.profile))[0];
  switch (engine && engine.state) {
    case 'ready': return { word: 'READY', name, sub: `up ${dur(now - engine.since)}`, tone: 'ready' };
    case 'starting': return { word: 'STARTING', name: engine.target || name,
      sub: engine.target ? 'switching' : 'starting', tone: 'busy' };
    case 'stopping': return { word: 'STOPPING', name, sub: 'stopping', tone: 'busy' };
    case 'failed': return { word: 'FAILED', name, sub: ago(engine.since, now), tone: 'bad' };
    default: break;
  }
  const sched = engine && engine.schedule;
  if (sched && sched.in_day === false) {
    return { word: 'FLEET', name: `until ${hm(sched.next_change)}`, sub: 'night · engine stopped', tone: 'maint' };
  }
  return { word: 'STOPPED', name: 'no engine runs', sub: '', tone: 'maint' };
}

// Package power and two temperatures. The CPU cores are on their own dies, so Tctl can be 30 °C above the
// GPU edge (the hot alarm's sensor): one number alone would say a hot machine is cool.
// Each cell fills from the left to its level on a scale set by the hardware's limits, and the fill's colour
// is its zone. Power is never hot: a high draw is work, not a fault.
export const VITAL_SCALES = {
  power: { lo: 0, hi: 140, warm: 120, hot: null },  // Framework Desktop: 120 W sustained, 140 W boost
  gpu: { lo: 30, hi: HOT_EDGE_C, warm: 80, hot: 85 },  // at HOT_EDGE_C the alarm takes the whole screen
  cpu: { lo: 30, hi: 100, warm: 85, hot: 95 },  // Tjmax 100 °C
};

function gauge(v, text, { lo, hi, warm, hot }) {
  if (missing(v)) return { text, pct: 0, zone: null };
  const shown = Math.round(v);  // the zone follows the number on screen
  const pct = Math.round(100 * Math.min(1, Math.max(0, (v - lo) / (hi - lo))));
  return { text, pct, zone: hot !== null && shown >= hot ? 'hot' : shown >= warm ? 'warm' : 'ok' };
}

export const vitals = (hw = {}) => ({
  power: gauge(hw.power_w, watts(hw.power_w), VITAL_SCALES.power),
  gpu: gauge(hw.edge_c, degrees(hw.edge_c), VITAL_SCALES.gpu),
  cpu: gauge(hw.tctl_c, degrees(hw.tctl_c), VITAL_SCALES.cpu),
});

// -- prefill progress ------------------------------------------------------------------------------
// "45K", "4.1K", "812": prompt sizes in a lane, at most 4 characters.
export function shortK(n) {
  if (missing(n)) return DASH;
  if (n < 1000) return String(Math.round(n));
  if (n < 10000) return `${(n / 1000).toFixed(1)}K`;
  return `${Math.round(n / 1000)}K`;
}

// One stream's progress: the tokens the engine computed (a cached start is not work), of how many, and
// the time left at the stream's own rate (progress.js). `entry` (from the memo) holds the count shown
// so far, so the bar never moves back. Before the engine's first report the prompt size is not known:
// the view has a count only (pct null).
function prefillView(s, now, entry) {
  let est = estimatePrefill(s.prefill_progress, now);
  if (!est) return { view: null, entry: null };
  ({ est, memo: entry } = holdPrefill(est, entry || null));
  if (est.of == null) {
    return { view: { pct: null, done: shortK(est.read), total: null, cached: null, left: null, estimated: true }, entry };
  }
  const pct = Math.round(100 * prefillShare(est.read, est.of));
  const left = est.rate && est.read < est.of ? dur((est.of - est.read) / est.rate) : null;
  return {
    view: { pct, done: shortK(est.read), total: shortK(est.of), cached: est.cached ? shortK(est.cached) : null,
      left, estimated: est.estimated },
    entry,
  };
}

// -- lanes ------------------------------------------------------------------------------------------
const byStart = (a, b) => (a.started || 0) - (b.started || 0);
const free = { kind: 'free', name: '', tok: '', approx: false, val: '', pct: null, draft: null };

// A lane's token count in whole tokens up to 99,999, so it visibly climbs as it streams (count.js);
// "123K" above that.
export function laneTokens(n) {
  return n < 100000 ? int(n) : compact(n);
}

function laneRow(s, live, now, memo, next, tickTs) {
  const name = s.client ? s.client.name || s.client.key : DASH;
  const old = memo.get(s.id) || {};
  if (s.phase === 'decode') {
    const count = streamCount(s, tickTs, now, old.count || null);
    next.set(s.id, { count });
    return { kind: 'dec', name, tok: laneTokens(count.count), approx: live.decode_exact === false,
      val: String(Math.round(s.tps || 0)), pct: null, draft: streamDraft(s) };
  }
  const { view, entry } = prefillView(s, now, old.pre);
  if (entry) next.set(s.id, { pre: entry });
  if (!view || view.pct == null) {
    return { kind: 'wait', name, tok: view ? view.done : '', approx: Boolean(view), val: dur(now - s.started), pct: null,
      draft: null };
  }
  return { kind: 'pre', name, tok: `${view.done} / ${view.total}`, approx: false, val: `${view.pct}%`, pct: view.pct,
    draft: null };
}

// Lanes in start order so they never swap places. Returns the view and the next memo (a new Map): per
// stream, the prefill memo or the decode count shown. tickTs: the tick's time, where counts stream from.
export function lanes(live, now, memo, tickTs = now) {
  const streams = [...((live && live.streams) || [])].sort(byStart);
  const slots = (live && live.slots) || 0;
  const n = slots > 0 ? Math.min(slots, MAX_LANES) : Math.max(1, Math.min(streams.length, MAX_LANES));
  const next = new Map();
  const rows = streams.slice(0, n).map((s) => laneRow(s, live, now, memo, next, tickTs));
  while (rows.length < n) rows.push(free);
  const waiting = slots > 0 && live.in_flight > slots ? live.in_flight - slots : 0;
  // Requests beyond the slots wait in the engine's queue: they count as waiting, not as streams. Running
  // streams without a lane (more slots than lanes) are counted too, apart from the queue.
  const running = slots > 0 ? Math.min(streams.length, slots) : streams.length;
  const hidden = Math.max(0, running - n);
  const more = [hidden ? `+${int(hidden)} more` : '', waiting ? `+${int(waiting)} waiting` : ''].filter(Boolean).join(' · ');
  const label = slots > 0 ? `Streams ${running} of ${slots}` : `Streams ${running}`;
  const drafts = rows.some((r) => r.draft);
  return { view: { rows, label, more, compact: n > FULL_LANES, drafts }, memo: next };
}

// -- the large number -------------------------------------------------------------------------------
export function heroServe(live, now, memo) {
  const streams = [...((live && live.streams) || [])].sort(byStart);
  const decoding = streams.some((s) => s.phase === 'decode');
  if (decoding || !streams.length) {
    // No stream decoding means exactly 0 (ARCHITECTURE.md rule 2), so 0 never carries the ≈ mark.
    const tps = live ? live.decode_tps : null;
    return { kind: 'decode', value: rate(tps), approx: Boolean(live) && live.decode_exact === false && tps > 0 };
  }
  let counting = null;  // a prompt the engine has not reported yet, counted at the recent rate
  for (const s of streams) {
    const { view } = prefillView(s, now, (memo.get(s.id) || {}).pre);
    if (view && view.pct != null) return { kind: 'prefill', ...view };
    counting = counting || view;
  }
  // Seconds, then minutes, as a number and a small unit: "2m 05s" would not fit the large type.
  const waited = Math.max(0, now - streams[0].started);
  const [value, unit] = waited < 100 ? [Math.floor(waited), 's'] : [Math.floor(waited / 60), 'min'];
  return { kind: 'wait', value: String(value), unit, count: streams.length, read: counting ? counting.done : null };
}

// -- the fleet's lease and queue (night) -----------------------------------------------------------
export const QUEUE_ROWS = 4;  // the queue tile's height; the rest are counted
const PLAN_STARTED = new Set(['smoke', 'running']);  // the plan's states that mean the GPU is on that item

// "45s", "16m", "1.5h": a usual hold or a hard limit, rounded (a usual hold is good to about a minute).
export function shortDur(s) {
  if (missing(s)) return DASH;
  if (s < 90) return `${Math.round(s)}s`;
  if (s < 5400) return `${Math.round(s / 60)}m`;
  return `${(s / 3600).toFixed(1)}h`;
}

// "19:02", "1:02:41": a running lease's clock.
export function clockText(s) {
  const t = Math.max(0, Math.floor(s));
  const mm = String(Math.floor((t % 3600) / 60)).padStart(2, '0');
  const ss = String(t % 60).padStart(2, '0');
  return t < 3600 ? `${Math.floor(t / 60)}:${ss}` : `${Math.floor(t / 3600)}:${mm}:${ss}`;
}

// The running lease. Its clock counts on from the poll (`panel.ts`), so it ticks between polls; the bar
// runs toward the hard limit, where the lease wrapper kills the job. The daily engine holds the GPU lock itself, so
// its lease has no start and no limit: there is no clock to count and no bar to fill.
export function leaseNow(panel, now) {
  const fleet = panel && panel.fleet;
  const li = leaseInfo(fleet);
  const l = fleet && !fleet.error ? fleet.lease : null;
  if (l && l.daily) return { name: l.agent, meta: 'by day', clock: '', of: '', pct: null, tone: 'busy' };
  if (!l) return { name: li.text, meta: li.sub, clock: '', of: '', pct: null, tone: li.tone };
  const elapsed = (l.elapsed_s || 0) + Math.max(0, now - panel.ts);
  const meta = [l.kind && l.kind !== 'other' ? l.kind : null, `p${l.prio ?? DASH}`,
    missing(l.typical_s) ? null : `usually ${shortDur(l.typical_s)}`].filter(Boolean).join(' · ');
  const pct = l.limit_s ? Math.round(1000 * Math.min(1, elapsed / l.limit_s)) / 10 : null;
  return { name: l.agent, meta, clock: clockText(elapsed), of: l.limit_s ? `of ${shortDur(l.limit_s)}` : '', pct,
    tone: li.tone };
}

// What waits, in the lease wrapper's order: its kind ("other" says nothing), its usual hold (none without history)
// and its hard limit. With a plan the tile shows the plan instead (planView).
export function queueView(fleet) {
  if (!fleet || fleet.error) return { label: 'Queue', more: '', rows: [], empty: 'No fleet data' };
  const plan = fleet.plan && fleet.plan.items;
  if (plan && plan.length) {
    const p = planView(fleet.plan, QUEUE_ROWS);
    return {
      label: 'Night plan', more: p.more, empty: p.open ? '' : 'Night plan done',
      // The time spent and the expectation go in the usual and max columns: in one 66 px column
      // "12m of 35m" wrapped to three lines and pushed the panel past its 280 px.
      rows: p.rows.map((r) => ({ name: r.label, kind: r.state, usual: r.spent || r.expect,
        max: r.spent && r.expect ? `of ${r.expect}` : '' })),
    };
  }
  const all = fleet.queue || [];
  const rows = all.slice(0, QUEUE_ROWS).map((j) => ({
    name: j.agent, kind: j.kind && j.kind !== 'other' ? j.kind : '',
    usual: missing(j.typical_s) ? '' : `≈${shortDur(j.typical_s)}`, max: missing(j.limit_s) ? '' : `max ${shortDur(j.limit_s)}`,
  }));
  if (!all.length) return { label: 'Queue', more: '', rows, empty: 'Nothing waits for the GPU' };
  return { label: `Queue · ${int(all.length)} waiting`, more: all.length > QUEUE_ROWS ? `+${int(all.length - QUEUE_ROWS)} more` : '',
    rows, empty: '' };
}

// A night driver runs its own items one at a time, so the lease wrapper sees one lease and an empty queue. Its plan
// says what the GPU runs now and what comes next; ended items are counted beside the label instead, because
// a finished item is no answer to "what waits". `height` is the tile's rows: the glance tile and the Fleet
// page read this one helper, so both list the same items in the same order.
export function planView(plan, height) {
  const items = (plan && plan.items) || [];
  const started = items.filter((it) => PLAN_STARTED.has(it.state));
  const waiting = items.filter((it) => it.state === 'waiting');
  const rows = [...started, ...waiting].slice(0, height).map(planRow);
  const count = (state) => items.filter((it) => it.state === state).length;
  const parts = [[count('pass'), 'done'], [count('fail'), 'failed'], [count('skipped'), 'skipped']]
    .filter(([n]) => n > 0).map(([n, word]) => `${int(n)} ${word}`);
  const rest = waiting.length - waiting.slice(0, Math.max(0, height - started.length)).length;
  if (rest > 0) parts.push(`${int(rest)} more`);
  return { rows, more: parts.join(' · '), open: started.length + waiting.length };
}

// "35m": the minutes an item expects, which the plan's writer put in 0-1440.
const expectText = (it) => (missing(it.expect_min) ? '' : `${int(it.expect_min)}m`);

// A started item shows the time in it of its expectation ("12m of 35m"); a waiting one its expectation.
function planRow(it) {
  const expect = expectText(it);
  const spent = PLAN_STARTED.has(it.state) && !missing(it.elapsed_s) ? `${int(Math.round(it.elapsed_s / 60))}m` : '';
  const expected = spent && expect ? `${spent} of ${expect}` : spent || expect;
  return { id: it.id, label: it.label, state: it.state, expected, spent, expect };
}

// -- the hero's columns ---------------------------------------------------------------------------
// Decode tok/s for each window, and whether the window also prefilled (a yellow mark at the column's foot).
export function decodeColumns(hist) {
  const cols = (values) => columns(perSecond(hist.last, values), HERO_WINDOW_S, HERO_COLUMNS);
  return { decode: cols(hist.values), prefill: cols(hist.prefill).map((v) => v > 0) };
}

// The mean GPU busy % of each window, from timed hardware readings.
export const busyColumns = (samples) => columns(samples, HERO_WINDOW_S, HERO_COLUMNS);
