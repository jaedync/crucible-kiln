// node --test tools/web_tests/*.test.mjs
import { test } from 'node:test';
import assert from 'node:assert/strict';
import * as m from '../../src/kiln/web/static/js/panel/model.js';
import { DASH } from '../../src/kiln/web/static/js/format.js';


test('profile labels split into engine and model at the first separator', () => {
  assert.deepEqual(m.splitLabel('Halogen / Qwen3.8 Flash-Next'), ['Halogen', 'Qwen3.8 Flash-Next']);
  assert.deepEqual(m.splitLabel('Halogen · Qwen3.8 Flash-Next · draft'), ['Halogen', 'Qwen3.8 Flash-Next · draft']);
  assert.deepEqual(m.splitLabel('crucible-flashnext'), ['crucible-flashnext', '']);
  assert.deepEqual(m.splitLabel(null), ['—', '']);
});

test('spark path scales to the box and breaks at unknown samples', () => {
  assert.equal(m.sparkPath([0, 10, 5], 100, 20), 'M0,20L50,0L100,10');
  assert.equal(m.sparkPath([4, null, 4], 100, 20, 8), 'M0,10M100,10');
  assert.equal(m.sparkPath([], 100, 20), '');
  assert.equal(m.sparkPath([0, 0], 100, 20), 'M0,20L100,20');  // all zero: a flat line on the floor
});

test('trend summary: peak and mean of the known samples', () => {
  assert.deepEqual(m.trendStats([1, null, 5, 3]), { peak: 5, mean: 3 });
  assert.deepEqual(m.trendStats([null]), { peak: null, mean: null });
});

test('history keeps the newest N samples without changing the input', () => {
  const a = [1, 2, 3];
  const b = m.pushSample(a, 4, 3);
  assert.deepEqual(b, [2, 3, 4]);
  assert.deepEqual(a, [1, 2, 3]);
});

test('decode history: the last N seconds of series_1s, then completed seconds from ticks', () => {
  const series = { t: [10, 11, 12, 13], decode: [1, 2, 3, 4] };
  let h = m.seedSeconds(series, 3);
  assert.deepEqual(h, { last: 13, values: [2, 3, 4], prefill: [0, 0, 0] });
  h = m.addSeconds(h, [{ t: 13, decode: 9 }, { t: 14, decode: 5 }, { t: 16, decode: 7 }], 3);
  // second 13 was already counted; 15 had no completed tick: it counts as 0, not as missing.
  assert.deepEqual(h, { last: 16, values: [5, 0, 7], prefill: [0, 0, 0] });
});

test('decode history keeps each second\'s prefill beside its decode', () => {
  let h = m.seedSeconds({ t: [10, 11], decode: [1, 0], prefill: [0, 900] }, 3);
  assert.deepEqual(h, { last: 11, values: [1, 0], prefill: [0, 900] });
  h = m.addSeconds(h, [{ t: 13, decode: 4, prefill: 300 }], 3);
  assert.deepEqual(h, { last: 13, values: [0, 0, 4], prefill: [900, 0, 300] });
});

test('per-second history as timed samples', () => {
  assert.deepEqual(m.perSecond(13, [1, 2, 3]), [[11, 1], [12, 2], [13, 3]]);
  assert.deepEqual(m.perSecond(null, []), []);
});

test('timed samples keep the newest keepS seconds without changing the input', () => {
  const a = [[100, 1], [105, 2]];
  assert.deepEqual(m.pushTimed(a, 111, 3, 10), [[105, 2], [111, 3]]);
  assert.deepEqual(a, [[100, 1], [105, 2]]);
  assert.deepEqual(m.pushTimed([], 5, null, 10), [[5, null]]);  // an unknown reading still moves the clock
});

test('columns: the mean of each clock window, newest last; a window with no known sample is null', () => {
  const s = [[100, 4], [101, 6], [109, 2], [110, 10], [111, null], [125, 3]];
  assert.deepEqual(m.columns(s, 10, 4), [null, 4, 10, 3]);
  assert.deepEqual(m.columns(s, 10, 2), [10, 3]);
  assert.deepEqual(m.columns([], 10, 3), [null, null, null]);
});

test('column heights: percent of the top, from 0; unknown is 0', () => {
  assert.deepEqual(m.columnHeights([0, 5, 10, null], 20), [0, 25, 50, 0]);
  assert.deepEqual(m.columnHeights([1, 3, 2]), [33.3, 100, 66.7]);  // no top: the largest
  assert.deepEqual(m.columnHeights([0, null]), [0, 0]);
  assert.deepEqual(m.columnHeights([150], 100), [100]);
});

test('gesture: a tap moves less than 20 px, a swipe at least 60 px sideways; nothing else counts', () => {
  const at = (x, y) => ({ x, y });
  assert.deepEqual(m.gesture(at(900, 100), at(700, 120)), { tap: false, dir: 1 });
  assert.deepEqual(m.gesture(at(300, 100), at(600, 90)), { tap: false, dir: -1 });
  assert.deepEqual(m.gesture(at(100, 100), at(104, 102)), { tap: true, dir: 0 });  // the side thirds are gone
  assert.deepEqual(m.gesture(at(1300, 100), at(1300, 100)), { tap: true, dir: 0 });
  assert.deepEqual(m.gesture(at(700, 100), at(719, 100)), { tap: true, dir: 0 });
  assert.deepEqual(m.gesture(at(700, 100), at(720, 100)), { tap: false, dir: 0 });  // 20 px: neither
  assert.deepEqual(m.gesture(at(700, 20), at(740, 250)), { tap: false, dir: 0 });  // mostly vertical
  // A finger that went out 80 px and came back is no tap: a swipe over a button must not press it.
  assert.deepEqual(m.gesture(at(700, 100), at(705, 100), 80), { tap: false, dir: 0 });
  assert.equal(m.turn(0, 2, -1), 1);
  assert.equal(m.turn(1, 2, 1), 0);
});

test('GPU lease line: the holder, else held, else free', () => {
  const lease = { agent: 'job-c', kind: 'gates', prio: 2, elapsed_s: 200, limit_s: 360 };
  assert.deepEqual(m.leaseInfo({ lease, queue: [{}, {}], holds: [] }),
    { text: 'job-c', sub: 'gates · p2 · 3m 20s', queued: 2, tone: 'busy' });
  assert.deepEqual(m.leaseInfo({ lease: null, queue: [], holds: ['kiln'] }),
    { text: 'Held', sub: 'by kiln', queued: 0, tone: 'maint' });
  assert.deepEqual(m.leaseInfo({ lease: null, queue: [], holds: [] }),
    { text: 'Free', sub: 'no lease', queued: 0, tone: 'idle' });
  assert.deepEqual(m.leaseInfo(null), { text: DASH, sub: 'no fleet data', queued: null, tone: null });
  assert.deepEqual(m.leaseInfo({ error: 'could not read' }), { text: DASH, sub: 'could not read', queued: null, tone: 'bad' });
});

test('GPU lease line: by day the daily engine holds the lock itself', () => {
  const daily = { agent: 'daily engine', kind: 'serve', prio: null, elapsed_s: null, limit_s: null, typical_s: null,
    daily: true };
  // The GPU page and the Fleet page read this: no "serve · p- · -" and no "no running lease".
  assert.deepEqual(m.leaseInfo({ lease: daily, queue: [{}, {}], holds: ['kiln'], holds_total: 1 }),
    { text: 'daily engine', sub: 'by day', queued: 2, tone: 'busy' });
});

test('big-number formats for the rack display', () => {
  assert.equal(m.watts(100.14), '100');
  assert.equal(m.degrees(63.6), '64');
  assert.equal(m.mhz(2900), (2900).toLocaleString());
  assert.equal(m.gibBig(29.4e9), '27.4');
  assert.equal(m.gibBig(268435456), '0.25');
  for (const fn of [m.watts, m.degrees, m.mhz, m.gibBig]) assert.equal(fn(null), DASH);
});

test('fleet: recent leases show the outcome', () => {
  const fleet = { recent: [{ agent: 'x', kind: 'bench', rc: 0, held_s: 212, ago_s: 40 },
    { agent: 'y', kind: 'gates', rc: 3, held_s: 10, ago_s: 640 }, { agent: 'z', kind: 'other', rc: null, held_s: null, ago_s: 9 }] };
  assert.deepEqual(m.recentRows(fleet, 3, 1000).map((r) => [r.agent, r.what, r.outcome, r.tone]), [
    ['x', 'bench · 3m 32s', 'ok', 'ok'], ['y', 'gates · 10 s', 'rc 3', 'bad'], ['z', 'other · —', '?', null]]);
});


test('fleet: repeated leases in a row fold into one line with a count', () => {
  const run = (rc, held) => ({ agent: 'job-p', kind: 'gates', rc, held_s: held, ago_s: 10 });
  const fleet = { recent: [run(0, 21), run(0, 26), run(0, 13), run(1, 9), run(0, 24), { agent: 'job-q', kind: 'bench', rc: 0, held_s: 60 }] };
  assert.deepEqual(m.recentRows(fleet, 3).map((r) => [r.agent, r.what, r.outcome, r.tone]), [
    ['job-p', 'gates × 3 · last 21 s', 'ok', 'ok'], ['job-p', 'gates · 9 s', 'rc 1', 'bad'],
    ['job-p', 'gates · 24 s', 'ok', 'ok']]);
});

test('engine: a last TTFT older than 10 minutes is old news', () => {
  assert.equal(m.ttftOld({ ts: 1000 }, 1000 + m.TTFT_OLD_S + 1), true);
  assert.equal(m.ttftOld({ ts: 1000 }, 1000 + m.TTFT_OLD_S - 1), false);
  assert.equal(m.ttftOld(null, 5000), false);
});

test('today: clients by requests', () => {
  const usage = { rows: [{ key: 'a', name: 'A', color: '#1', requests: 3 }, { key: 'b', name: null, requests: 9 },
    { key: 'c', name: 'C', requests: 1 }] };
  const c = m.clientRows(usage, 2);
  assert.deepEqual(c.rows.map((r) => [r.name, r.requests]), [['b', '9'], ['A', '3']]);
  assert.equal(c.count, 3);
  assert.deepEqual(m.clientRows(null, 2), { rows: [], count: null });
});

test('holds: the exact total counts, the list may be cut by the server', () => {
  assert.deepEqual(m.holdsInfo({ holds: ['kiln', 'other'], holds_total: 5 }), { count: 5, names: 'kiln, other + 3 more' });
  assert.deepEqual(m.holdsInfo({ holds: ['kiln'] }), { count: 1, names: 'kiln' });  // a server without holds_total
  assert.deepEqual(m.holdsInfo({ holds: [], holds_total: 0 }), { count: 0, names: '' });
  assert.deepEqual(m.holdsInfo(null), { count: null, names: '' });
  assert.equal(m.leaseInfo({ lease: null, queue: [], holds: ['kiln'], holds_total: 3 }).sub, 'by kiln + 2 more');
});

test('fitScreen: the kiosk window gets no transform; any other window shows the whole screen, centred', () => {
  assert.equal(m.fitScreen(1424, 280), null);
  // A laptop window narrower than the panel: scaled down to its width, centred in its height.
  const half = m.fitScreen(1000, 800);
  assert.ok(Math.abs(half.s - 1000 / 1424) < 1e-9);
  assert.equal(half.x, 0);
  assert.equal(half.y, Math.round((800 - 280 * half.s) / 2));
  // A wide, short window: the height decides.
  const wide = m.fitScreen(3000, 300);
  assert.ok(Math.abs(wide.s - 300 / 280) < 1e-9);
  assert.ok(wide.x > 0 && wide.y === 0);
  assert.equal(m.fitScreen(0, 800), null);
});
