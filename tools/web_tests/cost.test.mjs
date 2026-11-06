// node --test tools/web_tests/*.test.mjs
import { test } from 'node:test';
import assert from 'node:assert/strict';
import { liveCost, liveParts, owedStep, daySplit, BAR_KINDS, tintStep, TINT_HOLD_S,
  moving, chase, settled, wheels,
  splitUsd, UNIT }
  from '../../src/kiln/web/static/js/cost.js';
import { CAP_S } from '../../src/kiln/web/static/js/count.js';

const T = 1790000000;
const PRICING = { reference: 'Claude Sonnet 5.5', input_per_m: 2, cached_per_m: 0.1, output_per_m: 10 };
const tick = (cost, streams = []) => ({ ts: T, today: { cost: { reference: cost } }, live: { streams } });
const near = (a, b) => assert.ok(Math.abs(a - b) < 1e-9, `${a} != ${b}`);

test('live cost: finished requests plus the output of each stream at the reference output price', () => {
  const t = tick(2.5, [{ phase: 'decode', tokens: 1000, tps: 50 }, { phase: 'prefill', tokens: 0, tps: 0 }]);
  near(liveCost(t, PRICING, T), 2.5 + 1000 * 10 / 1e6);
  near(liveCost(t, PRICING, T + 0.4), 2.5 + 1020 * 10 / 1e6);  // climbs at the stream's rate between ticks
  near(liveCost(t, PRICING, T + 10), 2.5 + (1000 + 50 * CAP_S) * 10 / 1e6);  // a stalled stream stops
});

test('live cost: no price, no streams or no figure', () => {
  near(liveCost(tick(2.5, [{ phase: 'decode', tokens: 1000, tps: 50 }]), { reference: 'x' }, T), 2.5);
  near(liveCost(tick(2.5), PRICING, T), 2.5);
  assert.equal(liveCost(tick(null), PRICING, T), null);
  assert.equal(liveCost(null, PRICING, T), null);
  assert.equal(liveCost({ ts: T, today: null }, PRICING, T), null);
});

// A prefilling stream's progress as the server sends it (live/prefill.py): its last report, at T.
const report = (done, total, start, extra = {}) => ({ done, total, elapsed_s: 5, at: T, start, start_estimated: false,
  start_known: true, rate: 1000, rate_from: 'engine', next: total, reports: 2, ...extra });
const prefilling = (progress, extra = {}) => ({ phase: 'prefill', tokens: 0, tps: 0, queued: false,
  prefill_progress: progress, prompt: null, ...extra });

test('moving: not while nothing it prices can change (a prefill at its end, a start not known)', () => {
  // Review finding 4: a prompt fully read with a rate kept frames running with a still target.
  assert.ok(!moving(tick(1, [prefilling(report(12000, 12000, 0, { rate: 1500 }))]), T + 0.2));
  assert.ok(!moving(tick(1, [prefilling(report(30000, 50000, 20000, { start_known: false }))]), T + 0.2));
  // A prompt with no report yet starts to count after FIRST_REPORT_WAIT_S: frames run just before it.
  const early = { done: 0, total: null, at: T, rate: 1000, next: 4096, reports: 0, start_known: false };
  assert.ok(moving(tick(1, [prefilling(early)]), T + 0.5));
});

test('live cost: a stream that started before the day (today.since) adds nothing to today', () => {
  // Review finding 3: a prompt from before midnight belongs to yesterday's stored figure.
  const t = { ...tick(0, [{ phase: 'decode', tokens: 100, tps: 0, started: T - 10,
    prompt: { total: 50000, cached: 0 } }]), today: { cost: { reference: 0 }, since: T - 5 } };
  near(liveCost(t, PRICING, T), 0);
  near(liveCost({ ...t, today: { cost: { reference: 0 }, since: T - 20 } }, PRICING, T), 0.1 + 0.001);
  near(liveCost({ ...t, today: { cost: { reference: 0 } } }, PRICING, T), 0.1 + 0.001);  // an older server
});

test('live cost: a prefill prices its cached prefix at once and its new tokens as the engine reads them', () => {
  const t = tick(2.5, [prefilling(report(30000, 50000, 20000))]);
  near(liveCost(t, PRICING, T), 2.5 + (20000 * 0.1 + 10000 * 2) / 1e6);
  near(liveCost(t, PRICING, T + 2), 2.5 + (20000 * 0.1 + 12000 * 2) / 1e6);  // on at the report's rate
});

test('live cost: before the first report the tokens counted at the recent rate are new ones', () => {
  const p = { done: 0, total: null, elapsed_s: null, at: T - 2, start: null, start_estimated: true, start_known: false,
    rate: 1000, rate_from: 'recent', next: 4096, reports: 0 };
  near(liveCost(tick(1, [prefilling(p)]), PRICING, T), 1 + 2000 * 2 / 1e6);
});

test('live cost: no prompt cost while queued, without a known start, or without prompt prices', () => {
  near(liveCost(tick(1, [prefilling(report(30000, 50000, 20000), { queued: true })]), PRICING, T), 1);
  near(liveCost(tick(1, [prefilling(report(30000, 50000, 0, { start_known: false }))]), PRICING, T), 1);
  near(liveCost(tick(1, [prefilling(null)]), PRICING, T), 1);
  near(liveCost(tick(1, [prefilling(report(30000, 50000, 20000))]), { output_per_m: 10 }, T), 1);
});

test('live cost: a decoding stream keeps its prompt cost beside its output', () => {
  const s = { phase: 'decode', tokens: 1000, tps: 0, prefill_progress: null, prompt: { total: 50000, cached: 20000 } };
  near(liveCost(tick(2.5, [s]), PRICING, T), 2.5 + (20000 * 0.1 + 30000 * 2 + 1000 * 10) / 1e6);
  // the prefill's last estimate and the decode's prompt meet: the figure does not drop at the first token
  const before = liveCost(tick(2.5, [prefilling(report(49999, 50000, 20000))]), PRICING, T);
  const after = liveCost(tick(2.5, [{ ...s, tokens: 0 }]), PRICING, T);
  assert.ok(after >= before, `${after} < ${before}`);
  near(liveCost(tick(2.5, [{ ...s, prompt: null }]), PRICING, T), 2.5 + 1000 * 10 / 1e6);  // short prompt: at the end
});

test('moving: while a stream decodes or a prompt prefills at a known rate, up to CAP_S past the tick', () => {
  assert.ok(moving(tick(1, [{ phase: 'decode', tokens: 5, tps: 50 }]), T + 0.2));
  assert.ok(!moving(tick(1, [{ phase: 'decode', tokens: 5, tps: 50 }]), T + CAP_S + 0.1));
  assert.ok(moving(tick(1, [prefilling(report(30000, 50000, 20000))]), T + 0.2));
  assert.ok(!moving(tick(1, [prefilling(report(30000, 50000, 20000), { queued: true })]), T + 0.2));
  assert.ok(!moving(tick(1, [prefilling(report(30000, 50000, 20000, { rate: null }))]), T + 0.2));
  assert.ok(!moving(tick(1), T) && !moving(null, T));
});

test('live parts: the running requests by kind, beside the stored figure', () => {
  const dec = { phase: 'decode', tokens: 1000, tps: 0, prompt: { total: 50000, cached: 20000 } };
  const t = tick(2.5, [dec, prefilling(report(30000, 50000, 20000))]);
  const p = liveParts(t, PRICING, T);
  near(p.stored, 2.5);
  near(p.output, 1000 * 10 / 1e6);
  near(p.input, (30000 + 10000) * 2 / 1e6);
  near(p.cached, (20000 + 20000) * 0.1 / 1e6);
  near(liveCost(t, PRICING, T), p.stored + p.output + p.input + p.cached);
  assert.equal(liveParts(tick(null), PRICING, T), null);
});

const parts = (o = {}) => ({ stored: 1, cached: 0, input: 0, output: 0, ...o });

const PRICE = { input_per_m: 2, cached_per_m: 0.1, output_per_m: 10 };
const dayTick = (o = {}) => ({ today: { prompt: 3e6, cached: 2e6, completion: 1e5, cost: { reference: 4.2 }, ...o } });

test('day split: finished requests by their tokens at the card prices, the running ones added', () => {
  // 2M cached at 0.10 = 0.20, 1M new at 2.00 = 2.00, 100k out at 10.00 = 1.00: the stored 3.20
  const t = dayTick({ cost: { reference: 3.2 } });
  const d = daySplit(t, PRICE, { stored: 3.2, cached: 0.01, input: 0.04, output: 0.05 });
  assert.deepEqual(Object.fromEntries(BAR_KINDS.map((k) => [k, Math.round(d[k] * 1e4) / 1e4])),
    { cached: 0.21, input: 2.04, output: 1.05 });
  assert.deepEqual(BAR_KINDS, ['cached', 'input', 'output']);
});

test('day split: a stored figure the tokens do not explain keeps their proportions and its own total', () => {
  const d = daySplit(dayTick(), PRICE, { stored: 4.2, cached: 0, input: 0, output: 0 });
  assert.ok(Math.abs(d.cached + d.input + d.output - 4.2) < 1e-9);
  assert.ok(Math.abs(d.input / d.output - 2) < 1e-9);
});

test('day split: none without a figure, all live with no finished request', () => {
  assert.equal(daySplit(dayTick(), PRICE, null), null);
  const d = daySplit(dayTick({ prompt: 0, cached: 0, completion: 0, cost: { reference: 0 } }), PRICE,
    { stored: 0, cached: 0, input: 0.003, output: 0.001 });
  assert.deepEqual(d, { cached: 0, input: 0.003, output: 0.001 });
});

// Roll the shown figure as the masthead does, frame by frame, and return each frame's kind.
function roll(frames, dt = 1 / 30) {
  let shown = null;
  let owed = null;
  return frames.map((p) => {
    const target = p.stored + p.cached + p.input + p.output;
    const before = shown ? shown.units : null;
    shown = chase(shown, target, p.stored, dt);
    owed = owedStep(owed, p, before, shown.units);
    return owed.kind;
  });
}

test('owed: a big cache read keeps its colour until it has rolled in; the decode behind it comes after', () => {
  const frames = [parts(), parts({ cached: 0.05 })];  // $0.05 of cache read at once: 500 units to roll in
  for (let i = 1; i <= 60; i += 1) frames.push(parts({ cached: 0.05, output: 0.00005 * i }));  // decode, 2 s
  const kinds = roll(frames);
  assert.equal(kinds[0], null);  // the first frame starts on the figure
  assert.ok(kinds.slice(1, 21).every((k) => k === 'cached'), kinds.slice(1, 21).join());  // still rolling it in
  const late = kinds.slice(45).filter(Boolean);  // a frame the figure does not move has no kind
  assert.ok(late.length > 0 && late.every((k) => k === 'output'), late.join());  // paid off, decode shows
});

test('owed: a rise that no running request explains is a finished request settling', () => {
  const kinds = roll([parts({ output: 0.01 }), parts({ stored: 1 }),  // the stream ended: the figure holds
    ...Array(5).fill(parts({ stored: 1.0105 }))]);  // its record arrives 0.0005 above what was shown
  assert.equal(kinds[1], null);
  assert.equal(kinds[2], 'settled');
});

test('owed: what the figure no longer owes leaves the queue, newest first', () => {
  let o = owedStep(null, parts(), 10000, 10000);
  o = owedStep(o, parts({ input: 0.01, output: 0.01 }), 10000, 10000);  // 100 units of each, none shown yet
  assert.deepEqual(o.owed, [['input', 100], ['output', 100]]);
  o = owedStep(o, parts({ stored: 1.005 }), 10000, 10050);  // the streams ended at 1.005: 50 units owed
  assert.deepEqual(o.owed, []);  // all of it paid by the 50 shown: the input first
  assert.equal(o.kind, 'input');
});

test('owed: a frame that pays two kinds takes the larger; a new day starts again', () => {
  let o = owedStep(null, parts(), 10000, 10000);
  o = owedStep(o, parts({ cached: 0.001, output: 0.003 }), 10000, 10040);  // 10 cached then 30 output
  assert.equal(o.kind, 'output');
  o = owedStep(o, parts({ stored: 0.5 }), 10040, 5000);
  assert.deepEqual([o.owed, o.kind], [[], null]);
});

test('tint: a digit that turns takes the kind, and keeps it TINT_HOLD_S after it stops', () => {
  let t = tintStep(null, [1, 2, 3], 'output', 10);
  assert.deepEqual(t.kinds, [null, null, null]);  // the first draw has nothing to compare
  t = tintStep(t, [1, 2, 3.4], 'output', 10.1);
  assert.deepEqual(t.kinds, [null, null, 'output']);
  t = tintStep(t, [1, 3, 0], 'input', 10.2);  // a carry: two digits turn, now for a prefill
  assert.deepEqual(t.kinds, [null, 'input', 'input']);
  t = tintStep(t, [1, 3, 0], 'input', 10.2 + TINT_HOLD_S / 2);
  assert.deepEqual(t.kinds, [null, 'input', 'input']);
  t = tintStep(t, [1, 3, 0], null, 10.2 + TINT_HOLD_S + 0.01);
  assert.deepEqual(t.kinds, [null, null, null]);
});

test('tint: digits line up from the right when the figure gains one', () => {
  let t = tintStep(null, ['9', '9', '9'], 'output', 0);  // digits only: '$' and '.' never take a colour
  t = tintStep(t, ['1', '0', '0', '0'], 'cached', 0.1);
  assert.deepEqual(t.kinds, ['cached', 'cached', 'cached', 'cached']);
  t = tintStep(t, ['1', '0', '0', '1'], null, 0.2);  // turned with nothing lit: no colour
  assert.deepEqual(t.kinds, ['cached', 'cached', 'cached', null]);
});

test('chase: starts on the figure in whole 1/10,000 dollars', () => {
  assert.deepEqual(chase(null, 2.71234, 2.71234, 0), { units: 27123, stored: 2.71234, since: null });
  assert.equal(chase(null, null, null, 0), null);
});

test('chase: a new day starts again even when its stored cost did not fall', () => {
  // Yesterday stored $0 and a stream before midnight showed $0.1010; at midnight that stream leaves the
  // day and today has stored $0 too (review).
  const yesterday = chase(null, 0.101, 0, 0, undefined, 1000);
  assert.equal(yesterday.units, 1010);
  assert.equal(chase(yesterday, 0, 0, 0.05, undefined, 1000).units, 1010);  // the same day: it holds
  assert.equal(chase(yesterday, 0, 0, 0.05, undefined, 87400).units, 0);
});

test('chase: a step rolls in over a few frames, then rests on a whole unit', () => {
  let s = chase(null, 1, 1, 0);
  const shown = [];
  for (let i = 0; i < 4; i += 1) shown.push((s = chase(s, 1.001, 1.001, 0.05)).units);
  assert.ok(shown.every((u, i) => u > (i ? shown[i - 1] : 10000) && u < 10010), String(shown));
  for (let i = 0; i < 12; i += 1) s = chase(s, 1.001, 1.001, 0.05);
  assert.equal(s.units, 10010);  // in about 0.8 s: the last units come at MIN_RATE, not ever slower
  assert.ok(settled(s, 1.001));
  assert.ok(!settled(chase(s, 1.002, 1.002, 0.05), 1.002));
});

test('chase: a target that falls mid-roll does not stop a wheel between two digits', () => {
  // Replay of the live feed: a stream ends (its live part leaves before its record) or a tick's real count is
  // below the estimate, while the figure rolls. It must finish the digit it turns, then rest on a whole unit.
  let c = chase(null, 1.0, 1.0, 0);  // 10000 units
  c = chase(c, 1.001, 1.0, 0.05);    // rolling toward 10010: 10001.8 shown
  assert.ok(c.units > 10001 && c.units < 10002, String(c.units));
  const at = c.units;
  c = chase(c, 1.0001, 1.0, 1 / 60);  // the target falls below the shown figure
  assert.ok(c.units > at, 'it keeps rolling forward');
  assert.equal(settled(c, 1.0001), false, 'not at rest between two digits');
  for (let i = 0; i < 120 && !settled(c, 1.0001); i += 1) c = chase(c, 1.0001, 1.0, 1 / 60);
  assert.equal(c.units, Math.ceil(at));
  assert.equal(settled(c, 1.0001), true);
  assert.equal(chase(c, 1.0001, 1.0, 1).units, Math.ceil(at), 'and then holds');
});

test('chase: never moves back in a day; a smaller stored figure is a new day', () => {
  const s = chase(null, 1.5, 1.4, 0);  // 0.1 of it from a stream still decoding
  assert.equal(chase(s, 1.4, 1.4, 0.05).units, 15000);  // the stream ended before its stored cost arrived
  assert.deepEqual(chase(s, 0.02, 0.02, 0.05), { units: 200, stored: 0.02, since: null });
});

test('chase: with no time constant (reduced motion, the panel) it goes straight to the figure', () => {
  const s = chase(null, 1, 1, 0);
  assert.equal(chase(s, 1.0042, 1.0042, 0.05, 0).units, 10042);
  assert.equal(chase(s, 1.0042, 1.0042, Infinity).units, 10042);
});

test('wheels: at rest each wheel shows its digit', () => {
  assert.deepEqual(wheels(127100), [1, 2, 7, 1, 0, 0]);  // $12.7100
  assert.deepEqual(wheels(42), [0, 0, 0, 4, 2]);  // $0.0042
});

test('wheels: the last wheel turns with the figure; the others only on a carry', () => {
  assert.deepEqual(wheels(12345.25), [1, 2, 3, 4, 5.25]);
  const w = wheels(19999.5);  // $1.99995: every 9 half way to 0
  assert.deepEqual(w.map((r) => Math.round(r * 100) / 100), [1.5, 9.5, 9.5, 9.5, 9.5]);
  assert.deepEqual(wheels(19998.5).map((r) => Math.round(r * 100) / 100), [1, 9, 9, 9, 8.5]);
});

test('wheels: a dollar figure wider than the wheels gets more of them', () => {
  assert.equal(wheels(UNIT * 1234).length, 4 + 4);
});

test('splitUsd: cents strong, the two digits after them apart, thousands grouped', () => {
  assert.deepEqual(splitUsd(27123), ['$2.71', '23']);
  assert.deepEqual(splitUsd(42), ['$0.00', '42']);
  assert.deepEqual(splitUsd(12345678), ['$1,234.56', '78']);
  assert.deepEqual(splitUsd(null), ['—', '']);
});

test('hasReference: the API-cost figure shows only with reference prices in the snapshot', async () => {
  const { hasReference } = await import('../../src/kiln/web/static/js/pricing.js');
  assert.equal(hasReference({ reference: 'Claude Sonnet 5.5', input_per_m: 2 }), true);
  assert.equal(hasReference(null), false);  // kiln.toml has no [pricing.reference]
  assert.equal(hasReference(undefined), false);
});
