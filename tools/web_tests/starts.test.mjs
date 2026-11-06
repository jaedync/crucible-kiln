// node --test tools/web_tests/*.test.mjs
import { test } from 'node:test';
import assert from 'node:assert/strict';
import { keepStarts, startLineAt, NEW_START_S } from '../../src/kiln/web/static/js/starts.js';

test('request starts: the snapshot or a running stream adds one, a request keeps its first line, old ones go', () => {
  let starts = keepStarts(new Map(), [{ id: 'a', t: 100 }, { id: 'b', t: 20 }, { id: 'c', started: 120 }], 50);
  assert.deepEqual([...starts], [['a', 100], ['c', 120]]);  // the snapshot's t, a stream's started
  starts = keepStarts(starts, [{ id: 'c', started: 120.2 }], 50);  // a drawn line never moves
  assert.deepEqual([...starts], [['a', 100], ['c', 120]]);
  starts = keepStarts(starts, [{ id: 'd' }, null, { t: 130 }], 50);  // no id or no time: nothing to draw
  assert.equal(starts.size, 2);
  const before = starts;
  assert.deepEqual([...keepStarts(starts, [], 110)], [['c', 120]]);
  assert.equal(before.size, 2);  // a new map each time
});

test('a start newer than the chart\'s last second waits at its right edge; older and far-future ones are skipped', () => {
  assert.equal(startLineAt(100.4, 90, 200), 100.4);
  assert.equal(startLineAt(200.6, 90, 200), 200);  // its second isn't drawn yet: the line shows at once, at "now"
  assert.equal(startLineAt(200 + NEW_START_S + 1, 90, 200), null);  // not a start the chart can be behind on
  assert.equal(startLineAt(80, 90, 200), null);
});
