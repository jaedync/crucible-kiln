// node --test tools/web_tests/*.test.mjs
import { test } from 'node:test';
import assert from 'node:assert/strict';
import * as f from '../../src/kiln/web/static/js/format.js';

test('missing values print a dash, not zero', () => {
  for (const fn of [f.int, f.compact, f.rate, f.usd, f.secs, f.dur, f.gib]) assert.equal(fn(null), f.DASH);
});

test('rates: exact zero is "0", one decimal below 100, integers above', () => {
  assert.equal(f.rate(0), '0');
  assert.equal(f.rate(57.34), '57.3');
  assert.equal(f.rate(2463.2), (2463).toLocaleString());
});

test('money keeps fractions of a cent readable', () => {
  assert.equal(f.usd(0), '$0.00');
  assert.equal(f.usd(1.234), '$1.23');
  assert.equal(f.usd(0.00042), '$0.00042');
  assert.equal(f.usd(0.0004), '$0.0004');
});

test('durations', () => {
  assert.equal(f.secs(0.41), '410 ms');
  assert.equal(f.secs(1.3), '1.30 s');
  assert.equal(f.dur(45), '45 s');
  assert.equal(f.dur(725), '12m 05s');
  assert.equal(f.dur(8040), '2h 14m');
  assert.equal(f.dur(3 * 86400 + 4 * 3600), '3d 4h');
});

test('ago', () => {
  assert.equal(f.ago(100, 102), 'just now');
  assert.equal(f.ago(100, 130), '30 s ago');
  assert.equal(f.ago(0, 180), '3 min ago');
});

test('plural and client names', () => {
  assert.equal(f.plural(1, 'request'), '1 request');
  assert.equal(f.plural(3, 'request'), '3 requests');
  assert.equal(f.clientName({ key: 'k', name: 'n', label: 'L' }), 'n');
  assert.equal(f.clientName({ key: 'k', name: null, label: 'L' }), 'k');
  assert.equal(f.clientName({ key: 'k', name: null, label: null }), 'k');
});

test('finish: think_close is null or a reason string; length means a truncated answer', () => {
  assert.deepEqual(f.finishInfo({ finish: 'stop', think_close: null }), { text: 'stop', bad: false, title: null });
  const cut = f.finishInfo({ finish: 'stop', think_close: 'answer_room' });
  assert.equal(cut.text, 'stop · thinking cut off');
  assert.ok(cut.bad && cut.title.includes('answer_room'));
  const len = f.finishInfo({ finish: 'length', think_close: null });
  assert.equal(len.text, 'answer truncated');
  assert.ok(len.bad);
});

test('estimated rates carry ≈; fractional per-second values print briefly', () => {
  assert.equal(f.estRate(51.0234, true), '≈51.0');
  assert.equal(f.estRate(51.0234, false), '51.0');
  assert.equal(f.estRate(null, true), f.DASH);
});
