// node --test tools/web_tests/*.test.mjs
// The decode panel's prefill readout (js/prefill_now.js), on prefill views in the server's shape
// (live/prefill.py progress_view).
import { test } from 'node:test';
import assert from 'node:assert/strict';
import { prefillNow, PREFILL_SHOW_S } from '../../src/kiln/web/static/js/prefill_now.js';

const NOW = 1000;
// A stream that prefills, with the engine's last report at `at` (NOW unless given).
function reading(id, started, { done, total, start = 0, rate = 1500, from = 'engine', at = NOW, next } = {}) {
  return { id, phase: 'prefill', queued: false, started, prefill_progress: {
    done, total, elapsed_s: at - started, at, start, start_estimated: false, rate, rate_from: from,
    next: next ?? Math.min(done + 4096, total), reports: 2 } };
}
const decoding = (id) => ({ id, phase: 'decode', queued: false, started: NOW - 20, prefill_progress: null });

test('a long prompt: the engine rate, the share read and the time to the first token', () => {
  const { view } = prefillNow([reading('a', NOW - 10, { done: 15000, total: 60000 })], NOW);
  assert.equal(view.rate, '1,500');
  assert.equal(view.estimated, false);
  assert.equal(view.share, 0.25);
  assert.equal(view.sub, '≈30 s left');
  assert.equal(view.onlyPrefill, true);
  assert.match(view.title, /reads 1,500 prompt tokens per second/);
  assert.match(view.title, /15,000 of 60,000 tokens read, ≈30 s left/);
});

test('nothing reads: no readout', () => {
  assert.equal(prefillNow([], NOW).view, null);
  assert.equal(prefillNow(null, NOW).view, null);
  assert.equal(prefillNow([decoding('d')], NOW).view, null);
  // A stream in the engine's queue is not read yet; one without a view has no rate to show.
  const queued = { ...reading('q', NOW - 10, { done: 15000, total: 60000 }), queued: true };
  assert.equal(prefillNow([queued], NOW).view, null);
  assert.equal(prefillNow([{ ...reading('n', NOW - 10, { done: 1, total: 2 }), prefill_progress: null }], NOW).view, null);
});

test(`a prompt that needs less than ${PREFILL_SHOW_S} s in all gets no readout`, () => {
  // Mostly cached: 0.4 s in and 1,000 tokens left at 1,500 tok/s, so 1.07 s in all.
  const cached = (started) => reading('c', started, { done: 49000, total: 50000, start: 48000 });
  assert.equal(prefillNow([cached(NOW - 0.4)], NOW).view, null);
  // The same prompt, slower than its report said: it has run past the limit, so it shows.
  assert.equal(prefillNow([cached(NOW - 1.6)], NOW).view.sub, '≈1 s left');
  // A report 1 s in that says 30 s more: it shows at once, not only from the limit.
  assert.equal(prefillNow([reading('l', NOW - 1, { done: 1500, total: 46500 })], NOW).view.sub, '≈30 s left');
});

test('two prompts: their rates add up; the meter and the time left are the one that ends last', () => {
  const { view } = prefillNow([
    reading('short', NOW - 5, { done: 5000, total: 10000, rate: 800 }),
    reading('long', NOW - 20, { done: 15000, total: 60000, rate: 700 }),
    decoding('d'),
  ], NOW);
  assert.equal(view.rate, '1,500');
  assert.equal(view.share, 0.25);
  assert.equal(view.sub, '≈1m 05s left');  // 45,000 tokens at 700 tok/s
  assert.equal(view.onlyPrefill, false);  // a decode runs too: the decode figure stays black
  assert.match(view.title, /2 prompts/);
});

test('before the first report: the recent rate with ≈, and the tokens read so far', () => {
  const p = { done: 0, total: null, elapsed_s: null, at: NOW - 1.75, start: null, start_estimated: true,
    rate: 1500, rate_from: 'recent', next: 4096, reports: 0 };
  const { view } = prefillNow([{ id: 'a', phase: 'prefill', queued: false, started: NOW - 2, prefill_progress: p }], NOW);
  // The figure has no ≈ (no room beside the decode figure): the title names the estimate.
  assert.equal(view.rate, '1,500');
  assert.equal(view.estimated, true);
  assert.equal(view.share, 0);
  assert.equal(view.sub, '≈2,625 read');
  assert.match(view.title, /Estimated/);
});

test('the time left never moves back when a report lands short of the count', () => {
  const s = reading('a', NOW - 10, { done: 15000, total: 60000, next: 19096 });
  const first = prefillNow([s], NOW + 2);  // 2 s on at 1,500 tok/s: 18,000 read
  assert.equal(first.view.sub, '≈28 s left');
  const late = reading('a', NOW - 10, { done: 17500, total: 60000, at: NOW + 2 });
  assert.equal(prefillNow([late], NOW + 2).view.sub, '≈29 s left');  // without the memo
  assert.equal(prefillNow([late], NOW + 2, first.memos).view.sub, '≈28 s left');
  // A stream that ended leaves the memos.
  assert.equal(prefillNow([], NOW + 3, first.memos).memos.size, 0);
});
