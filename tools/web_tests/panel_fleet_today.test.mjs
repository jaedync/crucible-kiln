// node --test tools/web_tests/*.test.mjs
// The rack panel's Fleet (night plan and day) and Today screens: pure models.
import { test } from 'node:test';
import assert from 'node:assert/strict';
import * as fm from '../../src/kiln/web/static/js/panel/fleet_model.js';
import * as tm from '../../src/kiln/web/static/js/panel/today_model.js';

const NOW = 1790000000;
const item = (label, state, expect, extra = {}) => ({ id: label, label, state, expect_min: expect, elapsed_s: null,
  held_s: null, rc: null, ...extra });
const PLAN = { night: '2026-01-15', updated_ago_s: 60, items: [
  item('First check', 'pass', 35, { held_s: 1980 }),
  item('Short test', 'fail', 15, { held_s: 900, rc: 1 }),
  item('Second check', 'running', 20, { elapsed_s: 720 }),
  item('Long run', 'waiting', 45),
  item('Third check', 'waiting', 30)] };
const NIGHT = { state: 'stopped', schedule: { day: '09:00-18:00', in_day: false, next_change: NOW + 12 * 3600 } };
const DAY = { state: 'ready', label: 'Crucible', schedule: { day: '09:00-18:00', in_day: true, next_change: NOW + 3600 } };

// -- Fleet ------------------------------------------------------------------------------------------
test('the plan view shows at night with a plan; by day or without one the lease and the queue', () => {
  assert.equal(fm.fleetMode({ engine: NIGHT, fleet: { plan: PLAN } }), 'plan');
  assert.equal(fm.fleetMode({ engine: DAY, fleet: { plan: PLAN } }), 'day');
  assert.equal(fm.fleetMode({ engine: NIGHT, fleet: { plan: null } }), 'day');
  assert.equal(fm.fleetMode({ engine: NIGHT, fleet: { plan: { items: [] } } }), 'day');
  assert.equal(fm.fleetMode({ engine: NIGHT, fleet: null }), 'day');
});

test('one strip box per plan item, as wide as its expected minutes, in its state', () => {
  const boxes = fm.planStrip(PLAN.items);
  assert.deepEqual(boxes.map((b) => [b.label, b.state, b.grow, b.note]), [
    ['First check', 'pass', 17.5, 'OK · 33m'],
    ['Short test', 'fail', 7.5, 'Fail'],
    ['Second check', 'run', 20, '12m of 20m'],
    ['Long run', 'wait', 45, '45m'],
    ['Third check', 'wait', 30, '30m']]);
  assert.equal(boxes[2].fill, 60);  // 12 of 20 minutes
  assert.equal(fm.planStrip([item('A', 'running', 10, { elapsed_s: 1200 })])[0].fill, 100);  // never past the box
  assert.equal(fm.planStrip([item('B', 'waiting', null)])[0].grow, fm.MIN_GROW);  // no estimate: a narrow box
  assert.equal(fm.planStrip([item('C', 'skipped', 5), item('D', 'smoke', 5, { elapsed_s: 60 })])
    .map((b) => b.state).join(), 'skip,run');
});

test('the running box keeps room for its name and time; ended boxes give the most space', () => {
  const boxes = fm.planStrip(PLAN.items);
  assert.deepEqual(boxes.map((b) => b.min), [fm.ENDED_MIN_PX, fm.ENDED_MIN_PX, fm.RUN_MIN_PX, fm.BOX_MIN_PX,
    fm.BOX_MIN_PX]);
  assert.ok(fm.RUN_MIN_PX >= 180);  // about 12 letters of its name and "12M OF 20M"
  assert.ok(fm.ENDED_MIN_PX >= 64);  // "OK" and "FAIL" stay whole
  // Eight boxes at their floors, with the 3 px rules between them, still fit the strip (about 690 px).
  assert.ok(fm.RUN_MIN_PX + 7 * Math.max(fm.ENDED_MIN_PX, fm.BOX_MIN_PX) + 7 * 3 <= 690);
  // Ended items count half their minutes: the past gives up width first.
  assert.equal(fm.planStrip([item('A', 'pass', 20, { held_s: 1200 })])[0].grow, 10);
});

test('a long plan folds its far ends into counted boxes', () => {
  const items = Array.from({ length: 14 }, (_, i) => item(`item ${i}`, i < 6 ? 'pass' : i === 6 ? 'running' : 'waiting', 10));
  const boxes = fm.planStrip(items);
  assert.equal(boxes.length, fm.MAX_BOXES);
  assert.deepEqual([boxes[0].label, boxes[0].state], ['4 earlier', 'fold']);
  assert.ok(boxes.some((b) => b.label === 'item 6' && b.state === 'run'));
  assert.equal(boxes[boxes.length - 1].label, '+ 4 more');
});

test('the plan summary: the running item, the ended count, and the end from the open minutes', () => {
  const p = fm.planSummary(PLAN, NOW);
  assert.deepEqual([p.open, p.ended, p.total, p.failed], [3, 2, 5, 1]);
  assert.deepEqual(p.current, { label: 'Second check', spent: '12m', of: 'of 20m' });
  assert.equal(p.endsAt, NOW + (8 + 45 + 30) * 60);
  const waiting = fm.planSummary({ items: [item('KV', 'waiting', 45)] }, NOW);
  assert.deepEqual(waiting.current, { label: 'KV', spent: 'next', of: '45m' });
  const done = fm.planSummary({ items: [item('A', 'pass', 5)] }, NOW);
  assert.equal(done.current, null);
  assert.equal(done.endsAt, null);
  const late = fm.planSummary({ items: [item('A', 'running', 5, { elapsed_s: 900 })] }, NOW);
  assert.equal(late.endsAt, NOW);  // over its time: no minutes left in the sum
});

test('the state block of the Fleet screen', () => {
  assert.deepEqual(fm.fleetState(NIGHT, fm.planSummary(PLAN, NOW), 'plan'),
    { word: 'NIGHT', name: 'plan: 3 open', sub: `until ${fm.hmShort(NOW + 12 * 3600)}`, tone: 'maint' });
  assert.equal(fm.fleetState(NIGHT, fm.planSummary({ items: [item('A', 'pass', 5)] }, NOW), 'plan').name, 'plan done');
  assert.deepEqual(fm.fleetState(DAY, null, 'day'),
    { word: 'BY DAY', name: 'Crucible serves', sub: `night at ${fm.hmShort(NOW + 3600)}`, tone: 'maint' });
  assert.equal(fm.fleetState({ state: 'stopped', schedule: DAY.schedule }, null, 'day').name, 'no engine runs');
  assert.equal(fm.fleetState({ state: 'stopped' }, null, 'day').word, 'FLEET');
});

test('Not in the plan: the queue, failed leases tonight, GPU busy, holds', () => {
  const fleet = { queue: [{ agent: 'a' }, { agent: 'b' }, { agent: 'c' }], holds: [], holds_total: 0,
    recent: [{ agent: 'x', rc: 1, ago_s: 3600 }, { agent: 'y', rc: 0, ago_s: 600 }, { agent: 'z', rc: 2, ago_s: 50000 }] };
  // 9 h before the day: the night began 3 h ago, so only the failure 1 h ago is tonight's.
  assert.deepEqual(fm.notInPlan(fleet, { gpu_busy_pct: 99.4 }, NIGHT, NOW), [
    { label: 'Queue', value: '3 wait', tone: null },
    { label: 'Failed tonight', value: '1', tone: 'bad' },
    { label: 'GPU busy', value: '99 %', tone: null },
    { label: 'Holds', value: 'none', tone: null }]);
  const quiet = fm.notInPlan({ queue: [], holds: ['kiln'], holds_total: 1, recent: [] }, {}, NIGHT, NOW);
  assert.deepEqual(quiet.map((r) => r.value), ['none', '0', '—', 'kiln']);
});

test('the day queue lists the oldest first, with its kind, usual hold and limit', () => {
  const fleet = { queue: [
    { agent: 'job-f', kind: 'bench', waited_s: 600, typical_s: 420, limit_s: 900 },
    { agent: 'job-g', kind: 'gates', waited_s: 7000, typical_s: null, limit_s: 660 },
    { agent: 'job-h', kind: 'other', waited_s: 60, typical_s: 840, limit_s: 1080 }] };
  const q = fm.dayQueue(fleet, 2);
  assert.deepEqual(q.rows.map((r) => [r.name, r.kind, r.usual, r.max]),
    [['job-g', 'gates', '—', 'max 11m'], ['job-f', 'bench', '≈7m', 'max 15m']]);
  assert.equal(q.label, 'Queue · 3 waiting');
  assert.equal(q.oldest, 'oldest 1h 56m');
  assert.equal(q.more, '+ 1 more');
  assert.deepEqual(fm.dayQueue({ queue: [] }, 4), { label: 'Queue', oldest: '', rows: [], more: '',
    empty: 'Nothing waits for the GPU' });
  assert.equal(fm.dayQueue(null, 4).empty, 'No fleet data');
});

// -- Today ------------------------------------------------------------------------------------------
function buckets(counts, startHour = 0) {
  const t0 = 1790000000 - 14 * 3600;  // not a real midnight: the model reads the hour from `local`
  return { step_s: 3600, t: counts.map((_, i) => t0 + i * 3600),
    local: counts.map((_, i) => `2026-10-15 ${String((startHour + i) % 24).padStart(2, '0')}:00`), requests: counts };
}

test('24 columns, one per hour, scaled to the peak; the current hour is marked', () => {
  const counts = Array(24).fill(0);
  counts[3] = 9; counts[8] = 81; counts[14] = 40;
  const b = buckets(counts);
  const h = tm.hourColumns(b, b.t[14] + 1200);
  assert.equal(h.cols.length, 24);
  assert.deepEqual(h.cols.slice(7, 9).map((c) => c.pct), [0, 100]);
  assert.equal(h.cols[3].pct, 11.1);
  assert.equal(h.cols[0].zero, true);
  assert.equal(h.cols[14].now, true);
  assert.equal(h.cols.filter((c) => c.now).length, 1);
  assert.equal(h.peak, 'Peak 81 · 8 AM');
  assert.deepEqual(h.labels.slice(0, 4), ['12 AM', '', '', '3']);
  assert.equal(h.labels[12], '12 PM');
  assert.equal(h.labels[15], '3');
});

test('a day of 25 hours has 25 columns, and no usage gives empty columns', () => {
  const b = buckets(Array(25).fill(1));
  assert.equal(tm.hourColumns(b, b.t[0]).cols.length, 25);
  const none = tm.hourColumns(null, NOW);
  assert.equal(none.cols.length, 24);
  assert.ok(none.cols.every((c) => c.zero && !c.now));
  assert.equal(none.peak, '');
  assert.equal(tm.hourColumns(buckets(Array(24).fill(0)), NOW).peak, 'No requests yet');
});

test('the Today numbers: errors and clients, tokens, cached share and GPU busy time', () => {
  const usage = { rows: [{ name: 'Studio laptop', requests: 196 }, { name: 'Phone', requests: 71 },
    { name: 'Build agent', requests: 55 }], totals: { errors: 0, busy_s: 9660 } };
  const today = { requests: 322, prompt: 3_900_000, cached: 3_393_000, completion: 61_000 };
  assert.equal(tm.requestsSub(usage), '0 errors · 3 clients');
  assert.equal(tm.requestsSub({ rows: [{}], totals: { errors: 1 } }), '1 error · 1 client');
  assert.equal(tm.requestsSub(null), '');
  assert.deepEqual(tm.tokenFacts(today, usage), [
    { label: 'Prompt', value: '3.9M' }, { label: 'Cached', value: '87 %' }, { label: 'Output', value: '61K' },
    { label: 'GPU busy', value: '2h 41m' }]);
  assert.deepEqual(tm.tokenFacts(null, null).map((f) => f.value), ['—', '—', '—', '—']);
});
