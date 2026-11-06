// The decode panel's "Today · tok/s" table (js/day_speed.js).
import { test } from 'node:test';
import assert from 'node:assert/strict';
import { daySpeedView } from '../../src/kiln/web/static/js/day_speed.js';

const today = { speeds: {
  daily: { decode: { avg: 60.7, p5: 34.6, p95: 87.2, n: 212 }, prefill: { avg: 1470.4, p5: 760.2, p95: 1590.8, n: 18 },
    ttft: { avg: 1.84, p5: 0.31, p95: 12.4, n: 230 } },
  night: { decode: { avg: 95.6, p5: 75.2, p95: 118.3, n: 29 }, prefill: null },
} };

test('the serving engine: avg, p5 and p95 for decode, prefill and TTFT', () => {
  const v = daySpeedView(today, { profile: 'daily', label: 'Crucible / Daily' });
  assert.deepEqual(v.rows.map((r) => [r.label, r.unit, r.avg, r.p5, r.p95]),
    [['Decode', 'tok/s', '60.7', '34.6', '87.2'], ['Prefill', 'tok/s', '1,470', '760', '1,591'],
      ['TTFT', '', '1.84 s', '310 ms', '12.4 s']]);
  assert.match(v.title, /TTFT: 230 requests/);
  assert.match(v.title, /^Crucible \/ Daily since midnight/);
  assert.match(v.title, /212 requests/);
  assert.match(v.title, /Other engines today: night: decode 95.6 tok\/s/);
});

test('an engine without requests today, or no figures at all, shows dashes', () => {
  const v = daySpeedView(today, { profile: 'new', label: 'New' });
  assert.deepEqual(v.rows.map((r) => [r.avg, r.p5, r.p95]), [['—', '—', '—'], ['—', '—', '—'], ['—', '—', '—']]);
  assert.match(v.title, /No request on New today yet/);
  const none = daySpeedView(null, null);
  assert.deepEqual(none.rows.map((r) => r.avg), ['—', '—', '—']);
  // The night engine had no prefill large enough: its prefill row is dashes, its decode row is not.
  const night = daySpeedView(today, { profile: 'night', label: 'Night' });
  assert.deepEqual(night.rows.map((r) => r.avg), ['95.6', '—', '—']);
});
