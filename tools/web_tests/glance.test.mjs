// node --test tools/web_tests/*.test.mjs
import { test } from 'node:test';
import assert from 'node:assert/strict';
import * as g from '../../src/kiln/web/static/js/panel/glance_model.js';

const NOW = 1790000000;
const ready = { profile: 'crucible-t14', label: 'Crucible t14 · Qwen3.8 Flash-Next (3.05 bpw)', state: 'ready',
  since: NOW - 8100, target: null, schedule: { in_day: true, next_change: NOW + 3600 } };
const night = { profile: null, label: null, state: 'stopped', since: NOW - 600, target: null,
  schedule: { in_day: false, next_change: NOW + 3600 } };
const client = (name) => ({ key: name.toLowerCase(), name });
const dec = (id, started, tokens, tps) => ({ id, client: client(id), started, first_token: started + 1,
  phase: 'decode', tokens, tps, prefill_progress: null });
const pre = (id, started, progress = null) => ({ id, client: client(id), started, first_token: null,
  phase: 'prefill', tokens: 0, tps: 0, prefill_progress: progress });
const live = (streams, extra = {}) => ({ decode_tps: 51.2, decode_exact: false, in_flight: streams.length, slots: 4,
  streams, ...extra });

// -- mode -----------------------------------------------------------------------------------------
test('mode: serve while an engine runs or switches, fleet when none runs', () => {
  assert.equal(g.glanceMode({ engine: ready, hw: {}, connDownS: 0 }), 'serve');
  assert.equal(g.glanceMode({ engine: { ...ready, state: 'starting' }, hw: {}, connDownS: 0 }), 'serve');
  assert.equal(g.glanceMode({ engine: night, hw: {}, connDownS: 0 }), 'fleet');
  assert.equal(g.glanceMode({ engine: { ...night, schedule: { in_day: true } }, hw: {}, connDownS: 0 }), 'fleet');
});

test('alarm: no data after a grace period, a failed engine, a hot GPU', () => {
  assert.equal(g.glanceMode({ engine: ready, hw: {}, connDownS: g.CONN_GRACE_S - 1 }), 'serve');  // a reconnect
  assert.equal(g.glanceMode({ engine: ready, hw: {}, connDownS: 16 }), 'serve');  // the longest stall seen on the inference host
  assert.deepEqual(g.alarmInfo({ engine: ready, hw: {}, connDownS: g.CONN_GRACE_S, now: NOW }),
    { kind: 'nodata', title: 'NO DATA', sub: 'The panel cannot reach the Kiln dashboard' });
  const failed = { ...ready, state: 'failed', since: NOW - 180 };
  assert.equal(g.glanceMode({ engine: failed, hw: {}, connDownS: 0 }), 'alarm');
  assert.deepEqual(g.alarmInfo({ engine: failed, hw: {}, connDownS: 0, now: NOW }),
    { kind: 'failed', title: 'ENGINE FAILED', sub: 'Crucible t14 · 3 min ago' });
  const hot = { edge_c: g.HOT_EDGE_C + 2.4, fan_rpm: 3100 };
  assert.deepEqual(g.alarmInfo({ engine: ready, hw: hot, connDownS: 0, now: NOW }),
    { kind: 'hot', title: 'GPU TOO HOT', sub: 'Edge 92°C · fan 3,100 rpm' });
  assert.equal(g.alarmInfo({ engine: ready, hw: { edge_c: 70 }, connDownS: 0, now: NOW }), null);
});

// -- state tile -----------------------------------------------------------------------------------
test('state tile: ready, starting, night and stopped', () => {
  assert.deepEqual(g.stateTile(ready, NOW), { word: 'READY', name: 'Crucible t14', sub: 'up 2h 15m', tone: 'ready' });
  assert.deepEqual(g.stateTile({ ...ready, state: 'starting', target: 'halogen-flashnext' }, NOW),
    { word: 'STARTING', name: 'halogen-flashnext', sub: 'switching', tone: 'busy' });
  const n = g.stateTile(night, NOW);
  assert.equal(n.word, 'FLEET');
  assert.equal(n.tone, 'maint');
  assert.match(n.name, /^until /);
  assert.equal(n.sub, 'night · engine stopped');  // the tile has no kicker line, so "night" is said here
  assert.equal(g.stateTile({ ...night, schedule: { in_day: true } }, NOW).word, 'STOPPED');
});

// -- power and temperature ------------------------------------------------------------------------
test('vitals: power and the GPU and CPU temperatures, each a level on its scale with a zone', () => {
  assert.deepEqual(g.vitals({ power_w: 109.6, edge_c: 63.4, tctl_c: 71.5 }), {
    power: { text: '110', pct: 78, zone: 'ok' },  // 0-140 W
    gpu: { text: '63', pct: 56, zone: 'ok' },     // 30-90 °C, the hot alarm's line
    cpu: { text: '72', pct: 59, zone: 'ok' },     // 30-100 °C, Tjmax
  });
  const z = (hw) => Object.fromEntries(Object.entries(g.vitals(hw)).map(([k, v]) => [k, v.zone]));
  assert.deepEqual(z({ power_w: 119.4, edge_c: 79.4, tctl_c: 84.4 }), { power: 'ok', gpu: 'ok', cpu: 'ok' });
  // The zone follows the number shown: 79.6 reads "80", so it is warm.
  assert.deepEqual(z({ power_w: 119.6, edge_c: 79.6, tctl_c: 84.6 }), { power: 'warm', gpu: 'warm', cpu: 'warm' });
  assert.deepEqual(z({ power_w: 151, edge_c: 85, tctl_c: 95 }), { power: 'warm', gpu: 'hot', cpu: 'hot' });  // power is never hot
  assert.equal(g.vitals({ power_w: 151 }).power.pct, 100);
  assert.equal(g.vitals({ edge_c: 20 }).gpu.pct, 0);
  assert.deepEqual(g.vitals({}).cpu, { text: '—', pct: 0, zone: null });
});

// -- lanes ----------------------------------------------------------------------------------------
test('lanes: one per slot in start order, decode with its token count and rate, the rest free', () => {
  const v = live([dec('Phone', NOW - 5, 2431, 31.4), dec('Laptop', NOW - 60, 12437, 47.2)]);
  const { rows, label, more } = g.lanes(v, NOW, new Map()).view;
  assert.equal(label, 'Streams 2 of 4');
  assert.equal(more, '');
  assert.deepEqual(rows.map((r) => [r.kind, r.name, r.tok, r.approx, r.val]), [
    ['dec', 'Laptop', '12,437', true, '47'], ['dec', 'Phone', '2,431', true, '31'],
    ['free', '', '', false, ''], ['free', '', '', false, '']]);
});

test('lanes: a decode lane carries its conversation draft band; other lanes carry none', () => {
  const spec = { ...dec('Laptop', NOW - 60, 12437, 47.2), draft: { accepted: 3593, proposed: 4999, turns: 5 } };
  const v = live([spec, dec('Phone', NOW - 5, 2431, 31.4), pre('Tablet', NOW - 6)]);
  const { rows } = g.lanes(v, NOW, new Map()).view;
  assert.deepEqual([rows[0].draft.text, rows[0].draft.band, rows[0].draft.pct], ['72%', 'mid', 72]);
  assert.equal(rows[1].draft, null);  // no finished turn yet
  assert.equal(rows[2].draft, null);  // prefill: no drafts run
  assert.equal(rows[3].draft, null);  // free
  const running = { ...spec, draft_turn: { accepted: 90, proposed: 100 } };  // the turn's own counts, once enough
  assert.equal(g.lanes(live([running]), NOW, new Map()).view.rows[0].draft.text, '90%');
  // One lane with a value gives every decode lane the column, so the token counts stay in line.
  assert.equal(g.lanes(v, NOW, new Map()).view.drafts, true);
  assert.equal(g.lanes(live([dec('Phone', NOW - 5, 2431, 31.4)]), NOW, new Map()).view.drafts, false);
});

// The server's prefill view (live/prefill.py): the last report, the cached start, the rate, the next report.
const report = (done, total, extra = {}) => ({ done, total, elapsed_s: 30, at: NOW, start: 0, start_estimated: false,
  rate: null, rate_from: null, next: Math.min(total, done + 4096), reports: 3, ...extra });
const counting = (started, rate = 1500) => ({ done: 0, total: null, elapsed_s: null, at: started + 0.25, start: null,
  start_estimated: true, rate, rate_from: 'recent', next: 4096, reports: 0 });

test('lanes: prefill with progress fills by percent; before any report a count, or its time', () => {
  const v = live([pre('Agent', NOW - 31, report(45056, 105807)), pre('Phone', NOW - 3, counting(NOW - 3)),
    pre('Tablet', NOW - 6)]);
  const { rows } = g.lanes(v, NOW, new Map()).view;
  assert.deepEqual([rows[0].kind, rows[0].tok, rows[0].val, rows[0].pct], ['pre', '45K / 106K', '42%', 42]);
  assert.deepEqual([rows[1].kind, rows[1].tok, rows[1].val, rows[1].pct], ['wait', '', '6 s', null]);  // in start order
  assert.deepEqual([rows[2].kind, rows[2].tok, rows[2].approx, rows[2].val], ['wait', '4.1K', true, '3 s']);
});

test('lanes: a prompt resumed from the cache counts only the tokens the engine computes', () => {
  // strix req 117: 21,743 cached, 8,246 new; the first report (21,751 after 0.06 s) is not 73% done.
  const p = report(21751, 29989, { elapsed_s: 0.06, start: 21661, start_estimated: true, rate: 1500, next: 25847 });
  const { rows } = g.lanes(live([pre('Agent', NOW - 1, p)]), NOW + 1, new Map()).view;
  assert.deepEqual([rows[0].tok, rows[0].val], ['1.6K / 8.3K', '19%']);
});

test('lanes: progress never moves back when a report lands short of the estimate', () => {
  const p = report(40000, 100000, { at: NOW - 2, rate: 1000 });
  const a = g.lanes(live([pre('A', NOW - 22, p)]), NOW, new Map());
  const shown = a.view.rows[0].pct;
  const late = { ...p, done: 41000, elapsed_s: 32.5, at: NOW + 0.5, rate: 800 };
  const b = g.lanes(live([pre('A', NOW - 22, late)]), NOW + 0.6, a.memo);
  assert.ok(b.view.rows[0].pct >= shown);
});

test('lanes: a decode count streams between ticks and never moves back', () => {
  const v = live([dec('Laptop', NOW - 60, 12437, 47.2)]);
  const at = (now, memo) => g.lanes(v, now, memo, NOW);
  const a = at(NOW, new Map());
  const b = at(NOW + 0.3, a.memo);
  assert.deepEqual([a.view.rows[0].tok, b.view.rows[0].tok], ['12,437', '12,451']);  // + 47.2 x 0.3
  const next = g.lanes(live([dec('Laptop', NOW - 60, 12449, 40)]), NOW + 0.5, b.memo, NOW + 0.5);
  assert.equal(next.view.rows[0].tok, '12,451');  // the tick landed 2 short: the count holds
  assert.equal(g.laneTokens(123456), '123.5K');
});

test('lanes: more streams than slots are counted as waiting; more streams than lanes are counted as more', () => {
  const four = [0, 1, 2, 3].map((i) => dec(`S${i}`, NOW - 10 + i, 100, 20));
  const full = g.lanes(live([...four, pre('Late', NOW)], { in_flight: 5 }), NOW, new Map()).view;
  assert.equal(full.rows.length, 4);
  assert.equal(full.more, '+1 waiting');
  assert.equal(full.label, 'Streams 4 of 4');  // the fifth waits in the engine's queue
  assert.ok(full.rows.every((r) => r.name !== 'Late'));
  const seven = [...four, ...[4, 5, 6].map((i) => dec(`S${i}`, NOW - 6 + i, 9, 9))];
  const wide = g.lanes(live(seven, { slots: 8, in_flight: 7 }), NOW, new Map()).view;
  assert.equal(wide.label, 'Streams 7 of 8');
  assert.equal(wide.more, `+${7 - g.MAX_LANES} more`);
  const none = g.lanes({ streams: [], slots: 0, in_flight: 0 }, NOW, new Map()).view;
  assert.deepEqual(none.rows.map((r) => r.kind), ['free']);
  assert.equal(none.label, 'Streams 0');
});

test('lanes: a 6-slot engine gets 6 compact lanes, one for each slot', () => {
  const six = Array.from({ length: 6 }, (_, i) => dec(`S${i}`, NOW - 10 + i, 100, 20));
  const v = g.lanes(live([...six, pre('Late', NOW)], { slots: 6, in_flight: 7 }), NOW, new Map()).view;
  assert.equal(v.rows.length, 6);
  assert.equal(v.compact, true);
  assert.equal(v.more, '+1 waiting');
  assert.equal(g.lanes(live([dec('A', NOW, 1, 1)]), NOW, new Map()).view.compact, false);  // 4 slots: full size
});

test('lanes: more slots than lanes: running streams without a lane are counted beside the queue', () => {
  const all = Array.from({ length: 8 }, (_, i) => dec(`S${i}`, NOW - 10 + i, 100, 20));
  const v = g.lanes(live([...all, pre('Late', NOW)], { slots: 8, in_flight: 9 }), NOW, new Map()).view;
  assert.equal(v.label, 'Streams 8 of 8');
  assert.equal(v.more, `+${8 - g.MAX_LANES} more · +1 waiting`);
});

test('lanes: exact engines carry no ≈ mark', () => {
  const v = live([dec('A', NOW - 5, 50, 10)], { decode_exact: true });
  assert.equal(g.lanes(v, NOW, new Map()).view.rows[0].approx, false);
});

// -- hero -----------------------------------------------------------------------------------------
test('hero: decode rate while anything decodes or nothing runs', () => {
  const p = report(1, 10);
  assert.deepEqual(g.heroServe(live([dec('A', NOW - 5, 50, 10), pre('B', NOW - 1, p)]), NOW, new Map()),
    { kind: 'decode', value: '51.2', approx: true });
  // No stream means exactly 0 (ARCHITECTURE.md rule 2): no ≈ mark.
  assert.deepEqual(g.heroServe(live([], { decode_tps: 0 }), NOW, new Map()), { kind: 'decode', value: '0', approx: false });
});

test('hero: prefill progress and time left when only prefill runs', () => {
  const p = report(36864, 105807, { rate: 1638.4 });  // 16,384 tokens in 10 s by its own reports
  const h = g.heroServe(live([pre('A', NOW - 25, p)]), NOW, new Map());
  assert.deepEqual([h.kind, h.pct, h.done, h.total, h.cached, h.left], ['prefill', 34, '37K', '106K', null, '42 s']);
  const resumed = report(21751, 29989, { start: 21661, start_estimated: true, rate: 1500, next: 25847 });
  const r = g.heroServe(live([pre('A', NOW - 1, resumed)]), NOW, new Map());
  assert.deepEqual([r.pct, r.done, r.total, r.cached, r.left], [1, '90', '8.3K', '22K', '5 s']);
});

test('hero: no rate gives no time left; before any report a count, or the wait', () => {
  const p = report(4096, 50000);
  assert.equal(g.heroServe(live([pre('A', NOW - 3, p)]), NOW, new Map()).left, null);
  assert.equal(g.heroServe(live([pre('A', NOW - 3, { ...p, rate: 1000 })]), NOW, new Map()).left, '45 s');
  assert.deepEqual(g.heroServe(live([pre('A', NOW - 12), pre('B', NOW - 2)]), NOW, new Map()),
    { kind: 'wait', value: '12', unit: 's', count: 2, read: null });
  assert.deepEqual(g.heroServe(live([pre('A', NOW - 2, counting(NOW - 2))]), NOW, new Map()),
    { kind: 'wait', value: '2', unit: 's', count: 1, read: '2.6K' });
  assert.deepEqual(g.heroServe(live([pre('A', NOW - 150)]), NOW, new Map()),
    { kind: 'wait', value: '2', unit: 'min', count: 1, read: null });
});

// -- short counts ---------------------------------------------------------------------------------
test('short counts stay within 4 characters for prompt progress', () => {
  assert.deepEqual([g.shortK(812), g.shortK(4096), g.shortK(45056), g.shortK(105807), g.shortK(null)],
    ['812', '4.1K', '45K', '106K', '—']);
});

// -- the fleet's lease and queue (night) ----------------------------------------------------------
const job = (agent, kind, typical, limit) => ({ agent, prio: 2, waited_s: 600, kind, typical_s: typical, limit_s: limit });

test('hold times: seconds under 90 s, then minutes, then hours; a lease clock as m:ss or h:mm:ss', () => {
  assert.deepEqual([45, 89.6, 146, 960, 2460, 5400, 9000].map(g.shortDur), ['45s', '90s', '2m', '16m', '41m', '1.5h', '2.5h']);
  assert.equal(g.shortDur(null), '—');
  assert.deepEqual([0, 61.9, 1142, 3600, 3761].map(g.clockText), ['0:00', '1:01', '19:02', '1:00:00', '1:02:41']);
});

test('lease: its clock counts on from the poll, with its limit, a bar toward it and its usual hold', () => {
  const panel = { ts: NOW - 0.8, fleet: { lease: { agent: 'job-k', kind: 'other', prio: 2, elapsed_s: 1141.5,
    limit_s: 2460, typical_s: 250 }, queue: [job('long-run', 'bench', 130, 360)], holds: [] } };
  assert.deepEqual(g.leaseNow(panel, NOW),
    { name: 'job-k', meta: 'p2 · usually 4m', clock: '19:02', of: 'of 41m', pct: 46.4, tone: 'busy' });
  const bench = { ...panel, fleet: { ...panel.fleet, lease: { ...panel.fleet.lease, kind: 'bench', typical_s: null,
    elapsed_s: 3000 } } };
  const b = g.leaseNow(bench, NOW);
  assert.equal(b.meta, 'bench · p2');  // no usual hold without a finished lease of its own
  assert.equal(b.pct, 100);            // past the limit: the lease wrapper kills it at once, the bar stays full
});

test('lease: held, free or no data has no clock', () => {
  const held = g.leaseNow({ ts: NOW, fleet: { lease: null, queue: [], holds: ['kiln'], holds_total: 1 } }, NOW);
  assert.deepEqual(held, { name: 'Held', meta: 'by kiln', clock: '', of: '', pct: null, tone: 'maint' });
  assert.equal(g.leaseNow({ ts: NOW, fleet: { lease: null, queue: [], holds: [] } }, NOW).name, 'Free');
  assert.deepEqual(g.leaseNow(null, NOW), { name: '—', meta: 'no fleet data', clock: '', of: '', pct: null, tone: null });
});

test('queue: what waits in the lease wrapper order, its usual hold and its hard limit, the rest counted', () => {
  const queue = [job('check-2', 'bench', 146, 960), job('long-run', 'other', null, 360),
    job('job-k', 'gates', 52, 2460), job('job-l', 'train', 300, 360), job('job-m', 'other', 90, 360),
    job('job-n', 'other', 90, 360)];
  const q = g.queueView({ lease: null, queue, holds: [] });
  assert.equal(q.label, 'Queue · 6 waiting');
  assert.equal(q.more, '+2 more');
  assert.deepEqual(q.rows.slice(0, 3), [
    { name: 'check-2', kind: 'bench', usual: '≈2m', max: 'max 16m' },
    { name: 'long-run', kind: '', usual: '', max: 'max 6m' },  // "other" says nothing; no history, no guess
    { name: 'job-k', kind: 'gates', usual: '≈52s', max: 'max 41m' }]);
  assert.equal(q.rows.length, g.QUEUE_ROWS);
  assert.deepEqual(g.queueView({ lease: null, queue: [], holds: [] }),
    { label: 'Queue', more: '', rows: [], empty: 'Nothing waits for the GPU' });
  assert.equal(g.queueView(null).empty, 'No fleet data');
});

// -- the night driver's plan and the daily engine ---------------------------------------------------
const planItem = (id, state, expect_min, extra = {}) => ({ id, label: id.toUpperCase(), state, expect_min,
  elapsed_s: null, held_s: null, rc: null, ...extra });
const withPlan = (...items) => ({ lease: null, queue: [], holds: [], plan: { night: '2026-01-15', updated_ago_s: 60,
  items } });

test('queue: a night plan shows the running items then the next waiting ones, the rest counted', () => {
  const q = g.queueView(withPlan(
    planItem('first', 'pass', 35, { held_s: 2100, rc: 0 }),
    planItem('long-run', 'running', 35, { elapsed_s: 720 }),
    planItem('short', 'smoke', 5, { elapsed_s: 60 }),
    planItem('gates', 'waiting', 20),
    planItem('bench', 'waiting', 12),
    planItem('train', 'waiting', 60),
    planItem('last', 'waiting', 20),
    planItem('prefill', 'fail', 10, { rc: 1 }),
    planItem('extra', 'waiting', 5)));
  assert.equal(q.label, 'Night plan');
  assert.equal(q.rows.length, g.QUEUE_ROWS);
  assert.deepEqual(q.rows.map((r) => [r.name, r.kind, r.usual, r.max]), [
    ['LONG-RUN', 'running', '12m', 'of 35m'],
    ['SHORT', 'smoke', '1m', 'of 5m'],
    ['GATES', 'waiting', '20m', ''],
    ['BENCH', 'waiting', '12m', '']]);
  assert.equal(q.more, '1 done · 1 failed · 3 more');  // 5 wait, 2 fit under the 2 running items
  assert.equal(q.empty, '');
});

test('queue: a started item without a time yet shows only its expectation', () => {
  const q = g.queueView(withPlan(planItem('last', 'running', 30)));
  assert.equal(q.rows[0].usual, '30m');
  assert.equal(q.rows[0].max, '');
  assert.equal(q.more, '');
});

test('queue: a finished plan counts its items; no plan keeps the lease wrapper\'s queue and its text', () => {
  const done = g.queueView(withPlan(planItem('a', 'pass', 10), planItem('b', 'skipped', 5),
    planItem('c', 'fail', 5)));
  // Nothing runs and nothing waits: the tile says so, else it would be a bare label (point 4).
  assert.deepEqual([done.label, done.rows, done.more, done.empty],
    ['Night plan', [], '1 done · 1 failed · 1 skipped', 'Night plan done']);
  const open = g.queueView(withPlan(planItem('a', 'pass', 10), planItem('b', 'waiting', 5)));
  assert.deepEqual([open.rows.length, open.empty, open.more], [1, '', '1 done']);
  assert.equal(g.queueView({ plan: null, lease: null, queue: [], holds: [] }).empty, 'Nothing waits for the GPU');
  assert.equal(g.queueView({ plan: { items: [] }, lease: null, queue: [], holds: [] }).label, 'Queue');
  assert.equal(g.queueView({ plan: null, lease: null, queue: [job('a', 'bench', 60, 360)], holds: [] }).more, '');
  assert.equal(g.queueView({ plan: null, lease: null, queue: [job('a', 'bench', 60, 360)], holds: [] }).label,
    'Queue · 1 waiting');
});

test('the plan helper gives both tiles the same rows, counts and open items', () => {
  const plan = { items: [planItem('a', 'pass', 10, { held_s: 540 }), planItem('b', 'running', 35, { elapsed_s: 720 }),
    planItem('c', 'smoke', 5, { elapsed_s: 60 }), planItem('d', 'waiting', 20), planItem('e', 'waiting', 12)] };
  // The Fleet page cuts the same list to its own height: the running items first, then the waiting ones.
  assert.deepEqual(g.planView(plan, 2), {
    rows: [{ id: 'b', label: 'B', state: 'running', expected: '12m of 35m', spent: '12m', expect: '35m' },
      { id: 'c', label: 'C', state: 'smoke', expected: '1m of 5m', spent: '1m', expect: '5m' }],
    more: '1 done · 2 more', open: 4 });
  assert.equal(g.planView(plan, 4).rows.length, 4);
  assert.deepEqual(g.planView({ items: [] }, 4), { rows: [], more: '', open: 0 });
  assert.deepEqual(g.planView(null, 4), { rows: [], more: '', open: 0 });
});

test('lease: the daily engine holds the GPU by day, with no clock and no bar', () => {
  const daily = { agent: 'daily engine', kind: 'serve', prio: null, elapsed_s: null, limit_s: null, typical_s: null,
    daily: true };
  assert.deepEqual(g.leaseNow({ ts: NOW, fleet: { lease: daily, queue: [], holds: ['kiln'] } }, NOW + 5),
    { name: 'daily engine', meta: 'by day', clock: '', of: '', pct: null, tone: 'busy' });
  // A fleet lease keeps its clock whether or not the hold file is there (daily is false or absent).
  const l = { agent: 'job-k', kind: 'bench', prio: 1, elapsed_s: 60, limit_s: 360, typical_s: null, daily: false };
  assert.equal(g.leaseNow({ ts: NOW, fleet: { lease: l, queue: [], holds: ['kiln'] } }, NOW).clock, '1:00');
});

// -- hero columns ---------------------------------------------------------------------------------
test('hero columns: 10 s windows of decode, marked where the window also prefilled', () => {
  const last = 10 * 1000 + 19;  // seconds 10000-10019: two whole windows
  const hist = { last, values: [...Array(10).fill(30), ...Array(10).fill(50)],
    prefill: [0, 0, 800, ...Array(17).fill(0)] };
  const c = g.decodeColumns(hist);
  assert.equal(c.decode.length, g.HERO_COLUMNS);
  assert.deepEqual(c.decode.slice(-3), [null, 30, 50]);
  assert.deepEqual(c.prefill.slice(-3), [false, true, false]);
  assert.equal(g.HERO_COLUMNS * g.HERO_WINDOW_S, g.HERO_SPAN_S);
});

test('hero columns at night: the mean GPU busy of each window', () => {
  const c = g.busyColumns([[10000, 90], [10004, 100], [10011, 40]]);
  assert.deepEqual(c.slice(-2), [95, 40]);
});
