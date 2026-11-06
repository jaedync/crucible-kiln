import { test } from 'node:test';
import assert from 'node:assert/strict';
import { RAM_BANDS, ramTree, diskTree, treemap, layout, mapSizes, treeTotal, MIN_MAP_PX } from '../../src/kiln/web/static/js/memory_model.js';

const ram = (over = {}) => ({ weights: 64, cache: 7, gpu_other: 36, ple_ram: 3, file_cache: 1, programs: 1,
  kernel: 2, free: 11, total: 125, ...over });
const area = (r) => r.w * r.h;
const close = (a, b) => assert.ok(Math.abs(a - b) < 1e-6, `${a} != ${b}`);

test('the RAM parts are the server\'s', () => {
  assert.deepEqual(RAM_BANDS.map((b) => b.key),
    ['weights', 'kv', 'recurrent', 'kv_state', 'cache', 'cache_free', 'checkpoints', 'stage', 'vision_draft', 'gpu_other', 'ple_ram',
      'file_cache', 'programs', 'kernel', 'free']);
});

test('the RAM tree groups GPU memory and the page cache, and leaves out empty parts', () => {
  const t = ramTree({ ram: ram({ cache: 0 }) });
  assert.deepEqual(t.map((n) => [n.key, n.value]),
    [['gpu', 100], ['files', 4], ['programs', 1], ['kernel', 2], ['free', 11]]);
  assert.deepEqual(t[0].children.map((n) => n.key), ['weights', 'gpu_other']);
  assert.deepEqual(ramTree(null), []);
});

test('the disk tree splits the state limit into saved states and room', () => {
  const t = diskTree({ disk: { ple_file: 36, states: 70, states_cap: 150 } });
  assert.deepEqual(t.map((n) => [n.key, n.value]), [['ple_file', 36], ['cap', 150]]);
  assert.deepEqual(t[1].children.map((n) => [n.key, n.value]), [['states', 70], ['room', 80]]);
  const full = diskTree({ disk: { states: 160, states_cap: 150 } });
  assert.deepEqual(full[0].children.map((n) => n.key), ['states']);  // over the limit: no room
  assert.deepEqual(diskTree({ disk: { states: 10 } })[0].label, 'Saved states');
  assert.deepEqual(diskTree({ ram: {} }), []);
});

test('each rectangle\'s area is its share, and the rectangles fill the space without overlap', () => {
  const nodes = [6, 6, 4, 3, 2, 2, 1].map((value, i) => ({ key: `n${i}`, value }));
  const rects = treemap(nodes, 0, 0, 600, 400);
  assert.equal(rects.length, 7);
  close(rects.reduce((s, r) => s + area(r), 0), 600 * 400);
  for (const r of rects) close(area(r), (r.node.value / 24) * 600 * 400);
  for (const a of rects) {
    for (const b of rects) {
      if (a === b) continue;
      const ox = Math.min(a.x + a.w, b.x + b.w) - Math.max(a.x, b.x);
      const oy = Math.min(a.y + a.h, b.y + b.h) - Math.max(a.y, b.y);
      assert.ok(ox <= 1e-6 || oy <= 1e-6, 'rectangles overlap');
    }
  }
  // The squarified example of the paper: no rectangle much worse than 1:3.
  for (const r of rects) assert.ok(Math.max(r.w / r.h, r.h / r.w) < 3.5);
});

test('zero values, an empty list and an empty space give no rectangles', () => {
  assert.deepEqual(treemap([{ value: 0 }], 0, 0, 10, 10), []);
  assert.deepEqual(treemap([], 0, 0, 10, 10), []);
  assert.deepEqual(treemap([{ value: 1 }], 0, 0, 0, 10), []);
});

test('a group\'s parts sit inside it, under its caption', () => {
  const rects = layout(ramTree({ ram: ram() }), 800, 300);
  const gpu = rects.find((r) => r.node.key === 'gpu');
  const inner = rects.filter((r) => r.depth === 1 && ['weights', 'cache', 'gpu_other'].includes(r.node.key));
  assert.equal(inner.length, 3);
  for (const r of inner) {
    assert.ok(r.x >= gpu.x && r.x + r.w <= gpu.x + gpu.w + 1e-6);
    assert.ok(r.y >= gpu.y + 20 && r.y + r.h <= gpu.y + gpu.h + 1e-6);
  }
});

test('side by side, the two maps share a height and split the width by their totals', () => {
  const s = mapSizes([125, 245], { width: 1000, gap: 40, height: 300, stacked: false });
  assert.deepEqual(s.map((m) => m.h), [300, 300]);
  close(s[0].w + s[1].w, 960);
  close(s[0].w * s[0].h / 125, s[1].w * s[1].h / 245);  // one GiB covers the same area in both
});

test('stacked, the two maps share a width and split the height by their totals', () => {
  const s = mapSizes([125, 245], { width: 360, gap: 20, height: 220, stacked: true });
  assert.deepEqual(s.map((m) => m.w), [360, 360]);
  close(s[0].h + s[1].h, 440);
  close(s[0].w * s[0].h / 125, s[1].w * s[1].h / 245);
});

test('a map with no total takes no room, and a tiny one stays visible', () => {
  const s = mapSizes([125, 0], { width: 1000, gap: 40, height: 300, stacked: false });
  assert.deepEqual([s[0].w, s[1].w], [1000, 0]);
  const tiny = mapSizes([125, 0.01], { width: 1000, gap: 40, height: 300, stacked: false });
  assert.equal(tiny[1].w, MIN_MAP_PX);
  assert.equal(tiny[0].w, 960 - MIN_MAP_PX);
  assert.deepEqual(mapSizes([0, 0], { width: 1000, gap: 40, height: 300 }).map((m) => m.w), [1000, 0]);
});

test('a tree\'s total is the sum of its top-level parts', () => {
  assert.equal(treeTotal(diskTree({ disk: { ple_file: 95, states: 28, states_cap: 150 } })), 245);
  assert.equal(treeTotal([]), 0);
});
