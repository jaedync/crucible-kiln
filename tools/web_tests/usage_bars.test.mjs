// node --test tools/web_tests/*.test.mjs
import { test } from 'node:test';
import assert from 'node:assert/strict';
import { niceScale, usdTick, durTick, busyScale, axisLabels, bucketName, stacks, nowIndex, detailText } from '../../src/kiln/web/static/js/usage_bars.js';

const hours = (date, from, to) => Array.from({ length: to - from }, (_, i) => `${date} ${String(from + i).padStart(2, '0')}:00`);

test('nice scale: the top covers the peak in 1, 2, 2.5 or 5 steps', () => {
  assert.deepEqual(niceScale(0.92), { top: 1, step: 0.5 });
  assert.deepEqual(niceScale(2.91), { top: 3, step: 1 });
  const s = niceScale(0.0123);
  assert.ok(Math.abs(s.step - 0.005) < 1e-12 && Math.abs(s.top - 0.015) < 1e-12);
  assert.deepEqual(niceScale(0), { top: 0, step: 0 });
});

test('gridline prices', () => {
  assert.equal(usdTick(1), '$1');
  assert.equal(usdTick(1.5), '$1.50');
  assert.equal(usdTick(0.25), '$0.25');
  assert.equal(usdTick(0.5), '$0.50');
  assert.equal(usdTick(0.005), '$0.005');
});

test('axis: one day labels every third hour; midnight and noon are major', () => {
  const l = axisLabels(hours('2026-10-15', 0, 24), 3600);
  assert.deepEqual(l.map((x) => x.text), ['00', '03', '06', '09', '12', '15', '18', '21']);
  assert.deepEqual(l.filter((x) => x.major).map((x) => x.i), [0, 12]);
});

test('axis: a daylight saving day labels its repeated hour once', () => {
  const local = ['2026-11-01 00:00', '2026-11-01 01:00', '2026-11-01 01:00', '2026-11-01 02:00', '2026-11-01 03:00'];
  assert.deepEqual(axisLabels(local, 3600).map((x) => x.i), [0, 4]);
});

test('axis: several days of hours label each day at its midnight', () => {
  const local = [...hours('2026-10-27', 20, 24), ...hours('2026-10-28', 0, 24), ...hours('2026-10-29', 0, 3)];
  const l = axisLabels(local, 3600);
  assert.deepEqual(l.map((x) => x.i), [4, 28]);
  assert.ok(l.every((x) => x.major) && /28/.test(l[0].text));
});

test('axis: days label the first day, month starts and Mondays', () => {
  const local = ['2027-03-29', '2027-03-30', '2027-03-31', '2027-04-01', '2027-04-02', '2027-04-03', '2027-04-04',
    '2027-04-05', '2027-04-06', '2027-04-07', '2027-04-08'];
  const l = axisLabels(local, 86400);
  assert.deepEqual(l.map((x) => x.i), [0, 3, 7]);  // Mar 29 (a Monday, first), Apr 1, Mon Apr 5
  assert.deepEqual(l.map((x) => x.major), [true, true, false]);
  assert.equal(l[2].text, '5');
});

test('bucket names', () => {
  assert.equal(bucketName('2026-10-15 14:00', 3600), '14:00–15:00');
  assert.equal(bucketName('2026-10-15 23:00', 3600), '23:00–00:00');
  assert.match(bucketName('2026-10-15 14:00', 3600, true), /15 · 14:00–15:00$/);
  assert.match(bucketName('2026-10-15', 86400), /15$/);
});

test('stacks follow the table order and drop empty parts', () => {
  const b = { t: [0, 1], cost: { a: [0.5, 0], b: [0.25, 0.1] } };
  const s = stacks(b, ['b', 'a', 'gone']);
  assert.deepEqual(s[0], { total: 0.75, parts: [{ key: 'b', own: 0.25, value: 0.25 }, { key: 'a', own: 0.5, value: 0.5 }] });
  assert.deepEqual(s[1], { total: 0.1, parts: [{ key: 'b', own: 0.1, value: 0.1 }] });
  assert.equal(stacks({ t: [0], tokens: { a: [1200] } }, ['a'], 'tokens')[0].total, 1200);
});

test('busy stacks: overlap counts once, each key gets its share of the bucket', () => {
  const b = { t: [0], busy: { a: [600], b: [600] }, busy_all: [900] };
  const [s] = stacks(b, ['a', 'b'], 'busy');
  assert.equal(s.total, 900);
  assert.deepEqual(s.parts.map((p) => [p.own, p.value]), [[600, 450], [600, 450]]);
});

test('busy axis: an hour bar spans the whole hour; days step in minutes or hours', () => {
  assert.deepEqual(busyScale(120, 3600), { top: 3600, step: 900 });
  assert.deepEqual(busyScale(5 * 3600, 86400), { top: 6 * 3600, step: 2 * 3600 });
  assert.deepEqual(busyScale(40 * 60, 86400), { top: 40 * 60, step: 20 * 60 });
  assert.deepEqual(busyScale(0, 86400), { top: 0, step: 0 });
  assert.deepEqual([900, 3600, 9000].map(durTick), ['15m', '1h', '2.5h']);
});

test('detail line: the three totals, then the top rows by the shown measure', () => {
  const b = { t: [0], step_s: 3600, local: ['2026-10-15 14:00'], requests: [5], busy_all: [900],
    cost: { a: [0.5], b: [0.25] }, tokens: { a: [1000], b: [3000] }, busy: { a: [600], b: [600] } };
  const meta = new Map([['a', { name: 'Mac' }], ['b', { name: 'Eval' }]]);
  const text = detailText(b, 0, 'tokens', meta);
  assert.match(text, /14:00–15:00 · \$0\.75 · 4,000 tokens · 15m 00s busy · 5 requests · Eval 3,000 tokens · Mac 1,000 tokens$/);
});

test('now is the last bucket that started', () => {
  assert.equal(nowIndex([0, 10, 20], 15), 1);
  assert.equal(nowIndex([0, 10, 20], 99), 2);
  assert.equal(nowIndex([10], 5), -1);
});
