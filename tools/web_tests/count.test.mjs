// node --test tools/web_tests/*.test.mjs
import { test } from 'node:test';
import assert from 'node:assert/strict';
import { streamCount, CAP_S } from '../../src/kiln/web/static/js/count.js';

const T = 1790000000;

// The counts shown at these times, each call fed the last one's result, as a page draws them.
function run(s, tickTs, times, held = null) {
  return times.map((now) => (held = streamCount(s, tickTs, now, held)).count);
}

test('between ticks the count climbs at the stream rate instead of jumping a tick at a time', () => {
  assert.deepEqual(run({ tokens: 690, tps: 54 }, T, [T, T + 0.1, T + 0.2, T + 0.3, T + 0.4]), [690, 695, 700, 706, 711]);
});

test('a tick that lands ahead is caught up at twice the rate, not in one jump', () => {
  const held = streamCount({ tokens: 690, tps: 50 }, T, T + 0.5, streamCount({ tokens: 690, tps: 50 }, T, T + 0.45));
  assert.equal(held.count, 715);
  const ahead = { tokens: 727, tps: 50 };  // the next tick says 727: 12 more than shown
  const shown = run(ahead, T + 0.5, [T + 0.55, T + 0.6, T + 0.65, T + 0.7, T + 0.75], held);
  assert.deepEqual(shown, [720, 725, 730, 735, 739]);  // + 5 per 50 ms, then on the estimate again
});

test('a stalled stream stops counting; a short tick holds the count; no rate, no movement', () => {
  const s = { tokens: 690, tps: 54 };
  assert.equal(streamCount(s, T, T + 10).count, Math.floor(690 + 54 * CAP_S));
  const held = streamCount(s, T, T + 0.5);  // 717
  assert.equal(streamCount({ tokens: 712, tps: 40 }, T + 0.5, T + 0.5, held).count, held.count);
  assert.equal(streamCount({ tokens: 12, tps: 0 }, T, T + 0.4).count, 12);
  assert.equal(streamCount({ tokens: 12, tps: 30 }, T, T - 1).count, 12);  // a clock behind the tick
});
