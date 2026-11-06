// node --test tools/web_tests/*.test.mjs
import { test } from 'node:test';
import assert from 'node:assert/strict';
import { fromSeries, putSecond, fromRangeResponse, emptyBuffer, holdMax, smoothRate, LIVE_WINDOW_S }
  from '../../src/kiln/web/static/js/series.js';

const sec = (t, decode, prefill = 0) => ({ t, decode, prefill });

test('appends consecutive seconds', () => {
  let b = emptyBuffer();
  b = putSecond(b, sec(100, 5));
  b = putSecond(b, sec(101, 7));
  assert.deepEqual(b.t, [100, 101]);
  assert.deepEqual(b.decode, [5, 7]);
});

test('a drawn second never changes: a repeat or an older second is ignored', () => {
  const b = fromSeries({ t: [100, 101], decode: [5, 7], prefill: [0, 0] });
  assert.equal(putSecond(b, sec(101, 9)), b);
  assert.equal(putSecond(b, sec(99, 9)), b);
});

test('the live y axis grows for a new peak and never shrinks', () => {
  assert.equal(holdMax(0, 105, 10), 120);
  assert.equal(holdMax(120, 40, 10), 120);
  assert.equal(holdMax(120, 230, 10), 250);
});

test('missed seconds become null gaps, not zeros', () => {
  let b = fromSeries({ t: [100], decode: [5], prefill: [0] });
  b = putSecond(b, sec(103, 8));
  assert.deepEqual(b.t, [100, 101, 102, 103]);
  assert.deepEqual(b.decode, [5, null, null, 8]);
});

test('window is trimmed to 15 minutes', () => {
  let b = emptyBuffer();
  for (let t = 0; t < LIVE_WINDOW_S + 50; t++) b = putSecond(b, sec(t, 1));
  assert.equal(b.t.length, LIVE_WINDOW_S);
  assert.equal(b.t[0], 50);
});

test('range responses sum groups and convert to tokens per second', () => {
  const r = fromRangeResponse({ step: 10, t: [0, 10], series: {
    a: { decode: [100, 0], prefill: [0, 50] }, b: { decode: [20, 30], prefill: [0, 0] } } });
  assert.deepEqual(r.decode, [12, 3]);
  assert.deepEqual(r.prefill, [0, 5]);
});

test('smoothed rate: a Gaussian mean over the seconds near each one; gaps are left out', () => {
  const t = [...Array(60).keys()];
  const flat = smoothRate(t, t.map(() => 40), 4);
  assert.ok(flat.every((v) => Math.abs(v - 40) < 1e-9));  // also at both edges: the weights renormalise
  // A step from 0 to 100 becomes a smooth rise: no corner, half way at the step, monotonic.
  const step = smoothRate(t, t.map((s) => (s < 30 ? 0 : 100)), 4);
  for (let i = 1; i < 60; i++) assert.ok(step[i] >= step[i - 1] - 1e-9);
  assert.ok(step[29] > 30 && step[29] < 50 && step[30] > 50 && step[30] < 70);
  assert.ok(step[20] < 1 && step[40] > 99);
  // A gap is no data, not zero tokens; a second with no data within 3 sigma has no value.
  const gaps = smoothRate(t, t.map((s) => (s >= 10 && s < 40 ? null : 40)), 4);
  assert.ok(Math.abs(gaps[12] - 40) < 1e-9);
  assert.equal(gaps[25], null);
  assert.deepEqual(smoothRate([], [], 4), []);
});
