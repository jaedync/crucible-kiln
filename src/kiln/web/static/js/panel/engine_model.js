// What the rack panel's Engine screen says (pure; tools/web_tests/panel_engine.test.mjs). It replaced the
// Live and GPU pages and has no buttons: the engine's state, the model and its memory
// while it serves, the steps of a switch or a start, why no engine runs, or why it failed. The right part
// keeps the GPU page's useful numbers.
import { DASH, ago, dur, hm, int, secs } from '../format.js';
import { HOT_EDGE_C } from './glance_model.js';
import { degrees, gibBig, leaseInfo, mhz, splitLabel, trendStats, ttftOld, waiting, watts } from './model.js';

const GIB = 1024 ** 3;
const CHANGING = new Set(['starting', 'stopping']);
// The manager's steps (engine.phase), in its order: the old engine stops, the fleet's GPU jobs end and the
// memory comes back, the new engine loads, and a test generation must answer.
export const STEPS = [['stop', 'Stop old'], ['drain', 'Drain'], ['load', 'Load'], ['test', 'Test']];
const STEP_TEXT = { stop: 'stopping the old engine', drain: 'waiting for the fleet', load: 'loading the weights',
  test: 'testing an answer' };
const missing = (v) => v === null || v === undefined || Number.isNaN(v);
const DAY_FMT = new Intl.DateTimeFormat('en-US', { month: 'short', day: 'numeric' });

// A clock time alone reads as today: an engine stopped last evening said "since 05:10 PM" the next day.
export function sinceText(ts, now) {
  if (missing(ts)) return DASH;
  const day = (t) => new Date(t * 1000).setHours(0, 0, 0, 0);
  const days = Math.round((day(now) - day(ts)) / 86400000);
  if (days <= 0) return hm(ts);
  return `${days === 1 ? 'yesterday' : DAY_FMT.format(ts * 1000)} ${hm(ts)}`;
}

export function engineMode(engine) {
  if (!engine) return 'none';
  if (engine.state === 'failed') return 'failed';
  if (CHANGING.has(engine.state) || engine.target) return 'switch';
  return engine.state === 'ready' ? 'serve' : 'stopped';
}

const profileOf = (profiles, name) => (profiles || []).find((p) => p.name === name) || null;
const clockOf = (s) => `${Math.floor(s / 60)}:${String(Math.floor(s % 60)).padStart(2, '0')}`;

function names(engine, profiles, name) {
  const p = profileOf(profiles, name);
  const label = p ? p.label : name === engine.profile ? engine.label : null;
  return { profile: p, short: splitLabel(label || name)[0], model: splitLabel(label)[1] };
}

// -- serving --------------------------------------------------------------------------------------------
function serveView({ engine, hw, live, last, profiles, now }) {
  const n = names(engine, profiles, engine.profile);
  const sched = engine.schedule;
  const morning = sched && sched.day_profile === engine.profile ? ' · morning profile' : '';
  const p = n.profile;
  const kv = engine.kv;
  const pool = kv && kv.pool_total ? `${Math.round((100 * kv.pool_used) / kv.pool_total)} %` : DASH;
  const wait = waiting(live);
  const slots = live && !missing(live.slots)
    ? ` · ${Math.min(live.in_flight, live.slots)} of ${live.slots} slots${wait ? ` · ${wait} wait` : ''}` : '';
  const mem = missing(hw.gtt_used_bytes) ? DASH
    : `${gibBig(hw.gtt_used_bytes)} of ${missing(hw.gtt_total_bytes) ? DASH : Math.round(hw.gtt_total_bytes / GIB)} GiB`;
  return {
    state: { word: 'READY', name: n.short, sub: `up ${dur(now - engine.since)}${morning}`, tone: 'ready' },
    mid: {
      kicker: 'Profile', meta: p ? `${p.slots} slots · ${Math.round(p.context / 1024)}K ctx` : '',
      title: n.model || engine.model || DASH, note: '', steps: [],
      facts: [{ label: `KV pool${slots}`, value: pool, tone: null }, { label: 'GPU memory', value: mem, tone: null },
        { label: 'Last TTFT', value: secs(last ? last.ttft_s : null), tone: ttftOld(last, now) ? 'old' : null }],
    },
  };
}

// -- a switch, a start or a stop ------------------------------------------------------------------------
function currentStep(engine) {
  if (STEPS.some(([id]) => id === engine.phase)) return engine.phase;
  if (engine.state === 'stopping') return 'stop';
  return engine.profile ? 'load' : 'drain';
}

function step(id, label, state, { engine, hw, profile, now }) {
  if (state === 'done') return { id, label, state, value: 'OK', note: '', fill: null };
  if (state === 'wait') return { id, label, state, value: DASH, note: '', fill: null };
  const t = clockOf(Math.max(0, now - (engine.phase_since ?? engine.since)));
  if (id !== 'load' || missing(hw.gtt_used_bytes)) return { id, label, state, value: t, note: '', fill: null };
  const gib = hw.gtt_used_bytes / GIB;
  const size = profile && profile.memory_gib > 0 ? profile.memory_gib : null;
  return { id, label, state, value: `${Math.round(gib)} GiB`, note: size ? `of ${size} · ${t}` : t,
    fill: size ? Math.round(1000 * Math.min(1, gib / size)) / 10 : null };
}

function switchView(v) {
  const { engine, profiles } = v;
  const plainStop = engine.state === 'stopping' && !engine.target;
  const n = names(engine, profiles, engine.target || engine.profile);
  const steps = plainStop ? STEPS.slice(0, 1) : STEPS;
  const cur = currentStep(engine);
  const at = steps.findIndex(([id]) => id === cur);
  const ctx = { ...v, profile: n.profile };
  let word = 'STARTING';
  let kicker = `Start ${n.short}`;
  if (plainStop) [word, kicker] = ['STOPPING', `Stop ${n.short}`];
  else if (engine.state === 'stopping') [word, kicker] = ['SWITCHING', `Switch to ${n.short}`];
  return {
    state: { word, name: n.short, sub: plainStop ? 'stopping' : STEP_TEXT[cur], tone: 'busy' },
    mid: {
      kicker, meta: steps.length > 1 ? `step ${at + 1} of ${steps.length}` : '', title: '', note: engine.detail || '',
      facts: [],
      steps: steps.map(([id, label], i) => step(id, label, i < at ? 'done' : i === at ? 'now' : 'wait', ctx)),
    },
  };
}

// -- no engine ------------------------------------------------------------------------------------------
function fleetFacts(fleet) {
  const li = leaseInfo(fleet);
  const l = fleet && !fleet.error ? fleet.lease : null;
  const kind = l && l.kind && l.kind !== 'other' && !l.daily ? ` · ${l.kind}` : '';
  const lease = l && !l.daily && !missing(l.elapsed_s) ? `${l.agent} · ${dur(l.elapsed_s)}` : li.text;
  return [{ label: `GPU lease${kind}`, value: lease, tone: null },
    { label: 'Fleet queue', value: li.queued == null ? DASH : `${li.queued} waiting`, tone: null }];
}

function stoppedView({ engine, profiles, fleet, now }) {
  const sched = engine.schedule;
  const facts = sched ? fleetFacts(fleet) : [];  // no [schedule]: no fleet, so no lease or queue to show
  if (sched && sched.in_day === false) {
    const day = sched.day_profile ? names(engine, profiles, sched.day_profile).short : 'The day engine';
    return {
      state: { word: 'FLEET', name: `until ${hm(sched.next_change)}`, sub: 'night · engine stopped', tone: 'maint' },
      mid: { kicker: 'At night', meta: '', title: 'The GPU is for the fleet', note: `${day} starts at ${hm(sched.next_change)}`,
        facts, steps: [] },
    };
  }
  return {
    state: { word: 'STOPPED', name: 'no engine runs', sub: `since ${sinceText(engine.since, now)}`,  // the kicker beside it says "By day"
      tone: 'maint' },
    mid: { kicker: sched ? 'By day, no engine' : 'No engine', meta: '',
      title: engine.blocked ? 'An engine cannot start now' : 'No engine runs',
      note: engine.blocked || engine.detail || '', facts, steps: [] },
  };
}

function failedView({ engine, profiles, now }) {
  return {
    state: { word: 'FAILED', name: names(engine, profiles, engine.profile).short, sub: `since ${sinceText(engine.since, now)}`,
      tone: 'bad' },
    mid: { kicker: 'Why it failed', meta: ago(engine.since, now), title: '',
      note: engine.detail || 'No detail was given.', facts: [], steps: [] },
  };
}

const NONE = {
  state: { word: 'NO DATA', name: '', sub: 'Dashboard not reachable', tone: 'bad' },
  mid: { kicker: 'Engine', meta: '', title: '', note: 'The panel has no engine data.', facts: [], steps: [] },
};

// v: { engine, hw, live, last, profiles, fleet, now }. Returns { mode, state, mid }.
export function engineView(v) {
  const mode = engineMode(v.engine);
  const args = { ...v, hw: v.hw || {} };
  const views = { serve: serveView, switch: switchView, stopped: stoppedView, failed: failedView, none: () => NONE };
  return { mode, ...views[mode](args) };
}

// -- the GPU numbers (the old GPU page) -------------------------------------------------------------------
// Each cell: a label with a short note beside it (meta), the value, and under it a trend line (spark: the
// view's 2 minute series) or a line of text (sub).
export function gpuCells(hw = {}, busy = [], power = []) {
  const mean = trendStats(busy).mean;
  const peak = trendStats(power).peak;
  return [
    { id: 'busy', label: 'GPU busy', meta: mean == null ? '' : `mean ${Math.round(mean)}%`, unit: '%',
      value: missing(hw.gpu_busy_pct) ? DASH : String(Math.round(hw.gpu_busy_pct)), sub: '', spark: 'busy', tone: null },
    { id: 'power', label: 'Power', meta: peak == null ? '' : `peak ${watts(peak)} W`, unit: 'W', value: watts(hw.power_w),
      sub: '', spark: 'power', tone: null },
    { id: 'clock', label: 'Shader clock', meta: '', unit: 'MHz', value: mhz(hw.sclk_mhz),
      sub: hw.profile ? `profile ${hw.profile}` : '', spark: null, tone: null },
    { id: 'temp', label: 'GPU temp', meta: '', unit: '°C', value: degrees(hw.edge_c),
      sub: missing(hw.fan_rpm) ? 'fan —' : `fan ${int(hw.fan_rpm)} rpm`, spark: null,
      tone: hw.edge_c >= HOT_EDGE_C ? 'hot' : null },  // red where the alarm starts
  ];
}
