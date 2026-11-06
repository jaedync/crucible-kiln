// What the rack panel's Fleet screen says (pure; tools/web_tests/panel_fleet_today.test.mjs). At night with a
// night plan it answers "what is left of the night": the item that runs, a strip with one box per item and
// the time the plan should end. By day, or without a plan, it shows the lease, the queue and the last leases.
import { DASH, dur, hm, int } from '../format.js';
import { shortDur } from './glance_model.js';

export const MAX_BOXES = 8;  // the strip is about 690 px: 8 boxes keep a label of 6 or more letters readable
export const MIN_GROW = 8;  // the width of an item without an expectation, in minutes
// Floors in px, so a box never cuts what it must say: the running box its name (about 12 letters) and
// "12M OF 20M", an ended box "OK" or "FAIL". RUN + 7 others + the rules still fit the strip (about 690 px).
export const RUN_MIN_PX = 184;
export const ENDED_MIN_PX = 66;
export const BOX_MIN_PX = 60;
// An ended item counts this share of its minutes: the past gives up width first.
const ENDED_SHARE = 0.5;
const STARTED = new Set(['smoke', 'running']);
const ENDED = new Set(['pass', 'fail', 'skipped']);
const BOX_STATE = { pass: 'pass', fail: 'fail', skipped: 'skip', smoke: 'run', running: 'run', waiting: 'wait' };
const missing = (v) => v === null || v === undefined || Number.isNaN(v);
const mins = (s) => `${int(Math.round(s / 60))}m`;

// "9:00 AM": the plan's end and the window's change, without a leading zero.
export const hmShort = (ts) => hm(ts).replace(/^0(?=\d)/, '');

export function fleetMode({ engine, fleet }) {
  const night = engine && engine.schedule && engine.schedule.in_day === false;
  const items = fleet && !fleet.error && fleet.plan && fleet.plan.items;
  return night && items && items.length ? 'plan' : 'day';
}

function box(it) {
  const state = BOX_STATE[it.state] || 'wait';
  const minutes = missing(it.expect_min) || it.expect_min <= 0 ? MIN_GROW : it.expect_min;
  const ended = state === 'pass' || state === 'fail' || state === 'skip';
  const grow = ended ? minutes * ENDED_SHARE : minutes;
  const min = state === 'run' ? RUN_MIN_PX : ended ? ENDED_MIN_PX : BOX_MIN_PX;
  const expect = missing(it.expect_min) ? '' : `${int(it.expect_min)}m`;
  let note = expect;
  let fill = null;
  if (state === 'pass') note = missing(it.held_s) ? 'OK' : `OK · ${mins(it.held_s)}`;
  else if (state === 'fail') note = 'Fail';
  else if (state === 'skip') note = 'Skipped';
  else if (state === 'run') {
    const spent = missing(it.elapsed_s) ? '' : mins(it.elapsed_s);
    note = [spent, expect && `of ${expect}`].filter(Boolean).join(' ') || (it.state === 'smoke' ? 'smoke' : 'running');
    fill = missing(it.elapsed_s) || !it.expect_min ? 0 : Math.min(100, Math.round((100 * it.elapsed_s) / (it.expect_min * 60)));
  }
  return { label: it.label, state, grow, min, note, fill };
}

const fold = (label, items) => ({ label, state: 'fold', note: '', min: BOX_MIN_PX,
  grow: Math.min(30, items.reduce((a, it) => a + (it.expect_min || MIN_GROW), 0)), fill: null });

// One box per item, as wide as its expected minutes. A plan longer than MAX_BOXES keeps two items before the
// first open one and folds the rest of each end into a counted box.
export function planStrip(items) {
  const all = items || [];
  if (all.length <= MAX_BOXES) return all.map(box);
  const first = all.findIndex((it) => !ENDED.has(it.state));
  const from = Math.min(Math.max(0, (first < 0 ? all.length : first) - 2), all.length - (MAX_BOXES - 1));
  const head = from > 0 ? [fold(`${from} earlier`, all.slice(0, from))] : [];
  const room = MAX_BOXES - head.length;
  const n = from + room >= all.length ? room : room - 1;
  const after = all.slice(from + n);
  return [...head, ...all.slice(from, from + n).map(box), ...(after.length ? [fold(`+ ${after.length} more`, after)] : [])];
}

// The minutes an open item still needs: the rest of a running one (none once over time), all of a waiting one.
function minutesLeft(it) {
  if (missing(it.expect_min)) return 0;
  if (STARTED.has(it.state)) return Math.max(0, it.expect_min - (it.elapsed_s || 0) / 60);
  return it.expect_min;
}

export function planSummary(plan, now) {
  const items = (plan && plan.items) || [];
  const open = items.filter((it) => STARTED.has(it.state) || it.state === 'waiting');
  const run = items.find((it) => STARTED.has(it.state));
  const next = items.find((it) => it.state === 'waiting');
  let current = null;
  if (run) {
    current = { label: run.label, spent: missing(run.elapsed_s) ? run.state : mins(run.elapsed_s),
      of: missing(run.expect_min) ? '' : `of ${int(run.expect_min)}m` };
  } else if (next) {
    current = { label: next.label, spent: 'next', of: missing(next.expect_min) ? '' : `${int(next.expect_min)}m` };
  }
  const left = open.reduce((a, it) => a + minutesLeft(it), 0);
  return { open: open.length, ended: items.filter((it) => ENDED.has(it.state)).length, total: items.length,
    failed: items.filter((it) => it.state === 'fail').length, current,
    endsAt: open.length ? now + Math.round(left * 60) : null };
}

const SERVES = new Set(['ready', 'starting', 'stopping']);

// The state block: the night and its plan, or the day and what the engine does.
export function fleetState(engine, summary, mode) {
  const sched = engine && engine.schedule;
  if (!sched || typeof sched.in_day !== 'boolean') return { word: 'FLEET', name: 'no schedule', sub: '', tone: 'maint' };
  if (!sched.in_day) {
    const name = mode !== 'plan' ? 'no plan' : summary && summary.open ? `plan: ${int(summary.open)} open` : 'plan done';
    return { word: 'NIGHT', name, sub: `until ${hmShort(sched.next_change)}`, tone: 'maint' };
  }
  const label = engine.label ? String(engine.label).split(/\s+[·/|]\s+/)[0] : '';
  const name = SERVES.has(engine.state) && label ? `${label} serves` : 'no engine runs';
  return { word: 'BY DAY', name, sub: `night at ${hmShort(sched.next_change)}`, tone: 'maint' };
}

// Seconds since the night window began, from the day window ("09:00-18:00") and the next change (the day's
// start at night). 12 hours when the window cannot be read.
function sinceNightS(sched, now) {
  const m = /^(\d\d):(\d\d)-(\d\d):(\d\d)$/.exec((sched && sched.day) || '');
  const dayS = m ? ((((+m[3] * 60 + +m[4]) - (+m[1] * 60 + +m[2])) + 1440) % 1440) * 60 : 12 * 3600;
  const nightS = 86400 - dayS;
  return sched && !missing(sched.next_change) ? nightS - Math.max(0, sched.next_change - now) : nightS;
}

// The right tile of the plan view: what the GPU does apart from the plan.
export function notInPlan(fleet, hw, engine, now) {
  const f = fleet && !fleet.error ? fleet : null;
  const queued = f ? (f.queue || []).length : null;
  const since = sinceNightS(engine && engine.schedule, now);
  const failed = f ? (f.recent || []).filter((r) => r.rc !== 0 && !missing(r.rc) && r.ago_s <= since).length : null;
  const holds = f ? (f.holds || []) : null;
  const busy = hw && !missing(hw.gpu_busy_pct) ? `${Math.round(hw.gpu_busy_pct)} %` : DASH;
  return [
    { label: 'Queue', value: queued === null ? DASH : queued ? `${int(queued)} wait` : 'none', tone: null },
    { label: 'Failed tonight', value: failed === null ? DASH : int(failed), tone: failed ? 'bad' : null },
    { label: 'GPU busy', value: busy, tone: null },
    { label: 'Holds', value: holds === null ? DASH : holds.length ? holds.join(', ') : 'none', tone: null }];
}

// The day queue, oldest first: who waits longest is the question at the rack (the lease wrapper's own order is by
// priority, and the dashboard shows that).
export function dayQueue(fleet, max) {
  if (!fleet || fleet.error) return { label: 'Queue', oldest: '', rows: [], more: '', empty: 'No fleet data' };
  const all = [...(fleet.queue || [])].sort((a, b) => (b.waited_s || 0) - (a.waited_s || 0));
  if (!all.length) return { label: 'Queue', oldest: '', rows: [], more: '', empty: 'Nothing waits for the GPU' };
  const rows = all.slice(0, max).map((j) => ({ name: j.agent, kind: j.kind && j.kind !== 'other' ? j.kind : '',
    usual: missing(j.typical_s) ? DASH : `≈${shortDur(j.typical_s)}`, max: missing(j.limit_s) ? '' : `max ${shortDur(j.limit_s)}` }));
  return { label: `Queue · ${int(all.length)} waiting`, oldest: missing(all[0].waited_s) ? '' : `oldest ${dur(all[0].waited_s)}`,
    rows, more: all.length > max ? `+ ${int(all.length - max)} more` : '', empty: '' };
}
