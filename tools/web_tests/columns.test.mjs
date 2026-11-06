// node --test tools/web_tests/*.test.mjs
import { test } from 'node:test';
import assert from 'node:assert/strict';
import { columnOf, keepColumns, columnAt, latestColumn, stackBands, stackTop }
  from '../../src/kiln/web/static/js/columns.js';

const row = (id, from, to, completion) => ({ id, decode_from: from, decode_to: to, completion });

test('a request column spans its decode window at its average rate', () => {
  assert.deepEqual(columnOf(row('a', 100, 110, 540)), { id: 'a', from: 100, to: 110, avg: 54 });
  assert.equal(columnOf(row('b', 100, 100.5, 40)), null);  // under a second: already one bar of the chart
  assert.equal(columnOf(row('c', null, null, 40)), null);
  assert.equal(columnOf(row('d', 100, 110, 0)), null);
  assert.equal(columnOf(null), null);
});

test('columns: a drawn column never moves; old ones go; each call returns a new map', () => {
  let cols = keepColumns(new Map(), [row('a', 100, 110, 540), row('b', 20, 30, 300)], 50);
  assert.deepEqual([...cols.keys()], ['a']);
  cols = keepColumns(cols, [row('a', 101, 110, 540)], 50);  // the engine's window joined: the column stays
  assert.deepEqual(cols.get('a'), { id: 'a', from: 100, to: 110, avg: 54 });
  cols = keepColumns(cols, [row('a', null, null, 0)], 50);
  assert.equal(cols.size, 1);
  assert.equal(keepColumns(cols, [], 111).size, 0);  // ended before the live view's left edge
  const before = keepColumns(new Map(), [row('a', 100, 110, 540)], 50);
  keepColumns(before, [row('z', 100, 120, 100)], 50);
  assert.equal(before.size, 1);
});

test('the column under the cursor is the one that started last; the latest is the one that ended last', () => {
  const cols = keepColumns(new Map(), [row('a', 100, 120, 1000), row('b', 110, 115, 200)], 0);
  assert.equal(columnAt(cols, 112).id, 'b');
  assert.equal(columnAt(cols, 118).id, 'a');
  assert.equal(columnAt(cols, 130), null);
  assert.equal(latestColumn(cols).id, 'a');
  assert.equal(latestColumn(new Map()), null);
});

test('stacked bands: widest at the bottom; each band stands on the wider ones running at that moment', () => {
  // a: 100-200 at 30 (widest), b: 110-160 at 20, c: 150-170 at 5 (over a and b, then over a only),
  // d: 300-305 alone, e: 200-210 only touches a's end, so it stands on the floor.
  const cols = keepColumns(new Map(), [row('c', 150, 170, 100), row('a', 100, 200, 3000), row('b', 110, 160, 1000),
    row('d', 300, 305, 50), row('e', 200, 210, 100)], 0);
  const bands = stackBands(cols);
  assert.deepEqual(bands.get('a'), [{ from: 100, to: 200, base: 0 }]);
  assert.deepEqual(bands.get('b'), [{ from: 110, to: 160, base: 30 }]);
  assert.deepEqual(bands.get('c'), [{ from: 150, to: 160, base: 50 }, { from: 160, to: 170, base: 30 }]);
  assert.deepEqual(bands.get('d'), [{ from: 300, to: 305, base: 0 }]);
  assert.deepEqual(bands.get('e'), [{ from: 200, to: 210, base: 0 }]);
  assert.equal(stackTop(cols), 55);
  assert.equal(stackBands(cols), bands);  // the same map for the same columns: drawn twice a second
  assert.equal(stackTop(new Map()), 0);
});

test('stacked bands: no overhang, the top is the summed rate of the requests running at every moment', () => {
  const rows = [];
  for (let k = 0; k < 40; k++) rows.push(row(`r${k}`, (k * 37) % 300, (k * 37) % 300 + 5 + (k * 13) % 90, 50 + k * 7));
  const cols = keepColumns(new Map(), rows, 0);
  const bands = stackBands(cols);
  for (let t = 0.5; t < 400; t += 1) {
    const on = [...cols.values()].filter((c) => c.from <= t && t < c.to);
    const sum = on.reduce((a, c) => a + c.avg, 0);
    const tops = on.map((c) => bands.get(c.id).find((b) => b.from <= t && t < b.to).base + c.avg);
    assert.ok(Math.abs((on.length ? Math.max(...tops) : 0) - sum) < 1e-9, `t=${t}`);
  }
});

test('stacked bands: equal widths stack by start time, so the order is stable', () => {
  const cols = keepColumns(new Map(), [row('y', 105, 115, 100), row('x', 100, 110, 200)], 0);
  const bands = stackBands(cols);
  assert.deepEqual(bands.get('x'), [{ from: 100, to: 110, base: 0 }]);
  assert.deepEqual(bands.get('y'), [{ from: 105, to: 110, base: 20 }, { from: 110, to: 115, base: 0 }]);
});
