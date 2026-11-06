// node --test tools/web_tests/*.test.mjs
import { test } from 'node:test';
import assert from 'node:assert/strict';
import { placeStreams } from '../../src/kiln/web/static/js/slots.js';

const s = (id) => ({ id });

test('a stream keeps its row while it runs; a new one takes the first free row', () => {
  let r = placeStreams(new Map(), [s('a'), s('b'), s('c')], 4);
  assert.deepEqual([...r.placed], [['a', 0], ['b', 1], ['c', 2]]);
  r = placeStreams(r.placed, [s('b'), s('c')], 4);  // a ended: b and c stay where they are
  assert.deepEqual([...r.placed], [['b', 1], ['c', 2]]);
  r = placeStreams(r.placed, [s('b'), s('c'), s('d')], 4);
  assert.deepEqual(r.placed.get('d'), 0);
});

test('streams past the rows wait, in order, and get a row when one frees', () => {
  let r = placeStreams(new Map(), [s('a'), s('b'), s('c')], 2);
  assert.deepEqual(r.waiting.map((x) => x.id), ['c']);
  r = placeStreams(r.placed, [s('b'), s('c')], 2);
  assert.deepEqual([...r.placed], [['b', 1], ['c', 0]]);
  assert.deepEqual(r.waiting, []);
});

test('fewer rows (another engine) drop the rows that are gone; the input is not changed', () => {
  const before = new Map([['a', 0], ['b', 3]]);
  const r = placeStreams(before, [s('a'), s('b')], 2);
  assert.deepEqual([...r.placed], [['a', 0], ['b', 1]]);
  assert.equal(before.get('b'), 3);
});
