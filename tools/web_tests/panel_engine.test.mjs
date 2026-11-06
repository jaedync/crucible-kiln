// node --test tools/web_tests/*.test.mjs
import { test } from 'node:test';
import assert from 'node:assert/strict';
import * as e from '../../src/kiln/web/static/js/panel/engine_model.js';

const NOW = 1790000000;
const GIB = 1024 ** 3;
const PROFILES = [
  { name: 'crucible-t25', label: 'Crucible t25 · Qwen3.8 Flash-Next', slots: 4, context: 262144, memory_gib: 110 },
  { name: 'halogen-flashnext', label: 'Halogen · Qwen3.8 Flash-Next', slots: 2, context: 131072, memory_gib: 96 }];
const SCHED = { in_day: true, next_change: NOW + 3600, day_profile: 'crucible-t25' };
const ready = { profile: 'crucible-t25', label: 'Crucible t25 · Qwen3.8 Flash-Next', model: 'qwen3.8-flash-next',
  state: 'ready', since: NOW - 8100, target: null, detail: '', phase: null, phase_since: null,
  kv: { pool_used: 770, pool_total: 1000 }, schedule: SCHED };
const HW = { gtt_used_bytes: 88.6 * GIB, gtt_total_bytes: 124 * GIB, gpu_busy_pct: 97.6, power_w: 109.6,
  sclk_mhz: 2900, edge_c: 63, fan_rpm: 1784 };
const base = { engine: ready, hw: HW, live: { in_flight: 3, slots: 4 }, last: { ts: NOW - 50, ttft_s: 0.62 },
  profiles: PROFILES, fleet: null, now: NOW };

test('the mode follows the engine state; a target means a switch', () => {
  assert.equal(e.engineMode(null), 'none');
  assert.equal(e.engineMode(ready), 'serve');
  assert.equal(e.engineMode({ ...ready, state: 'starting' }), 'switch');
  assert.equal(e.engineMode({ ...ready, state: 'stopping' }), 'switch');
  assert.equal(e.engineMode({ ...ready, state: 'stopped', profile: null, target: 'crucible-t25' }), 'switch');
  assert.equal(e.engineMode({ ...ready, state: 'failed' }), 'failed');
  assert.equal(e.engineMode({ ...ready, state: 'stopped', profile: null }), 'stopped');
});

test('serving: the state block, the model, the KV pool, the GPU memory and the last TTFT', () => {
  const v = e.engineView(base);
  assert.equal(v.mode, 'serve');
  assert.deepEqual(v.state, { word: 'READY', name: 'Crucible t25', sub: 'up 2h 15m · morning profile', tone: 'ready' });
  assert.equal(v.mid.kicker, 'Profile');
  assert.equal(v.mid.meta, '4 slots · 256K ctx');
  assert.equal(v.mid.title, 'Qwen3.8 Flash-Next');
  assert.deepEqual(v.mid.facts, [
    { label: 'KV pool · 3 of 4 slots', value: '77 %', tone: null },
    { label: 'GPU memory', value: '88.6 of 124 GiB', tone: null },
    { label: 'Last TTFT', value: '620 ms', tone: null }]);
  assert.deepEqual(v.mid.steps, []);
});

test('serving: an unknown pool is a dash, waiting requests are counted, an old TTFT dims', () => {
  const v = e.engineView({ ...base, engine: { ...ready, kv: null }, live: { in_flight: 6, slots: 4 },
    last: { ts: NOW - 900, ttft_s: 2.5 } });
  assert.deepEqual(v.mid.facts[0], { label: 'KV pool · 4 of 4 slots · 2 wait', value: '—', tone: null });
  assert.deepEqual(v.mid.facts[2], { label: 'Last TTFT', value: '2.50 s', tone: 'old' });
  const other = e.engineView({ ...base, engine: { ...ready, schedule: null }, last: null });
  assert.equal(other.state.sub, 'up 2h 15m');
  assert.equal(other.mid.facts[2].value, '—');
});

test('a switch shows its steps: done steps OK, the current one with its time, the rest waiting', () => {
  const engine = { ...ready, profile: 'halogen-flashnext', state: 'stopping', target: 'crucible-t25',
    since: NOW - 30, phase: 'drain', phase_since: NOW - 72, detail: 'fleet paused; waiting for 2 jobs' };
  const v = e.engineView({ ...base, engine });
  assert.equal(v.mode, 'switch');
  assert.deepEqual(v.state, { word: 'SWITCHING', name: 'Crucible t25', sub: 'waiting for the fleet', tone: 'busy' });
  assert.equal(v.mid.kicker, 'Switch to Crucible t25');
  assert.equal(v.mid.meta, 'step 2 of 4');
  assert.equal(v.mid.note, 'fleet paused; waiting for 2 jobs');
  assert.deepEqual(v.mid.steps.map((s) => [s.id, s.state, s.value]),
    [['stop', 'done', 'OK'], ['drain', 'now', '1:12'], ['load', 'wait', '—'], ['test', 'wait', '—']]);
});

test('the load step shows the GPU memory against the profile size', () => {
  const engine = { ...ready, state: 'starting', since: NOW - 20, phase: 'load', phase_since: NOW - 20 };
  const v = e.engineView({ ...base, engine, hw: { ...HW, gtt_used_bytes: 61 * GIB } });
  assert.equal(v.state.word, 'STARTING');
  const load = v.mid.steps[2];
  assert.deepEqual(load, { id: 'load', label: 'Load', state: 'now', value: '61 GiB', note: 'of 110 · 0:20', fill: 55.5 });
  assert.equal(v.mid.steps[3].state, 'wait');
  // No profile size known: no fill, and the time alone.
  const bare = e.engineView({ ...base, engine, profiles: [] }).mid.steps[2];
  assert.deepEqual([bare.note, bare.fill], ['0:20', null]);
});

test('without a phase from the server the step follows the state', () => {
  const v = (engine) => e.engineView({ ...base, engine: { ...ready, ...engine, phase: null, phase_since: null } });
  assert.equal(v({ state: 'stopping', target: 'crucible-t25' }).mid.steps.find((s) => s.state === 'now').id, 'stop');
  assert.equal(v({ state: 'starting', profile: null, target: 'crucible-t25' }).mid.steps.find((s) => s.state === 'now').id, 'drain');
  assert.equal(v({ state: 'starting' }).mid.steps.find((s) => s.state === 'now').id, 'load');
});

test('a plain stop has one step', () => {
  const v = e.engineView({ ...base, engine: { ...ready, state: 'stopping', phase: 'stop', phase_since: NOW - 5 } });
  assert.equal(v.state.word, 'STOPPING');
  assert.equal(v.mid.kicker, 'Stop Crucible t25');
  assert.deepEqual(v.mid.steps.map((s) => s.id), ['stop']);
  assert.equal(v.mid.meta, '');
});

test('stopped by day: why, from the engine and the fleet', () => {
  const engine = { ...ready, profile: null, label: null, state: 'stopped', since: NOW - 3600, kv: null,
    detail: 'stopped by the owner', blocked: 'another container is running (dev-job)' };
  const fleet = { lease: { agent: 'job-c', kind: 'train', prio: 1, elapsed_s: 1620, limit_s: 3660 }, queue: [{}, {}, {}],
    holds: [] };
  const v = e.engineView({ ...base, engine, fleet, live: null, last: null });
  assert.equal(v.mode, 'stopped');
  assert.equal(v.state.word, 'STOPPED');
  assert.equal(v.state.tone, 'maint');
  assert.match(v.state.sub, /^since /);
  assert.equal(v.mid.kicker, 'By day, no engine');
  assert.equal(v.mid.kicker, 'By day, no engine');
  assert.equal(v.mid.title, 'An engine cannot start now');
  assert.equal(v.mid.note, 'another container is running (dev-job)');
  assert.deepEqual(v.mid.facts, [{ label: 'GPU lease · train', value: 'job-c · 27m 00s', tone: null },
    { label: 'Fleet queue', value: '3 waiting', tone: null }]);
  const free = e.engineView({ ...base, engine: { ...engine, blocked: null }, fleet: null });
  assert.equal(free.mid.title, 'No engine runs');
  assert.equal(free.mid.note, 'stopped by the owner');
  assert.deepEqual(free.mid.facts.map((f) => f.value), ['—', '—']);
});

test('stopped at night: the GPU is the fleet\'s until the day starts', () => {
  const engine = { ...ready, profile: null, label: null, state: 'stopped', schedule: { ...SCHED, in_day: false } };
  const v = e.engineView({ ...base, engine });
  assert.equal(v.state.word, 'FLEET');
  assert.equal(v.mid.kicker, 'At night');
  assert.equal(v.mid.title, 'The GPU is for the fleet');
  assert.match(v.mid.note, /^Crucible t25 starts at /);
});

test('failed: a red block and the error detail', () => {
  const v = e.engineView({ ...base, engine: { ...ready, state: 'failed', since: NOW - 120,
    detail: 'kiln-crucible-t25.service failed: out of memory' } });
  assert.deepEqual([v.mode, v.state.word, v.state.tone, v.state.name], ['failed', 'FAILED', 'bad', 'Crucible t25']);
  assert.equal(v.mid.kicker, 'Why it failed');
  assert.equal(v.mid.meta, '2 min ago');
  assert.equal(v.mid.note, 'kiln-crucible-t25.service failed: out of memory');
  assert.equal(e.engineView({ ...base, engine: { ...ready, state: 'failed', detail: '' } }).mid.note, 'No detail was given.');
});

test('no engine data', () => {
  const v = e.engineView({ ...base, engine: null });
  assert.deepEqual(v.state, { word: 'NO DATA', name: '', sub: 'Dashboard not reachable', tone: 'bad' });
});

test('the GPU cells: busy, power, shader clock and temperature, from the old GPU page', () => {
  const cells = e.gpuCells(HW, [90, 100], [100, 120]);
  assert.deepEqual(cells.map((c) => [c.id, c.value, c.unit]),
    [['busy', '98', '%'], ['power', '110', 'W'], ['clock', '2,900', 'MHz'], ['temp', '63', '°C']]);
  assert.deepEqual(cells.map((c) => c.meta), ['mean 95%', 'peak 120 W', '', '']);
  assert.deepEqual(cells.map((c) => c.spark), ['busy', 'power', null, null]);
  assert.equal(cells[2].sub, '');
  assert.equal(e.gpuCells({ ...HW, profile: 'performance' })[2].sub, 'profile performance');
  assert.equal(cells[3].sub, 'fan 1,784 rpm');
  assert.equal(cells[3].tone, null);
  assert.equal(e.gpuCells({ ...HW, edge_c: 91 }, [], [])[3].tone, 'hot');
  const empty = e.gpuCells({}, [], []);
  assert.deepEqual(empty.map((c) => c.value), ['—', '—', '—', '—']);
  assert.deepEqual(empty.map((c) => c.meta), ['', '', '', '']);
});

test('a time before today says its day: "since 5:10 PM" alone would read as today', () => {
  const now = new Date(2026, 9, 24, 16, 20).getTime() / 1000;
  const at = (d, h, m) => new Date(2026, 9, d, h, m).getTime() / 1000;
  assert.doesNotMatch(e.sinceText(at(24, 9, 0), now), /yesterday|Oct/);
  assert.match(e.sinceText(at(23, 17, 10), now), /^yesterday /);
  assert.match(e.sinceText(at(20, 17, 10), now), /^Oct 20 /);
});

test('stopped without a fleet ([schedule]): no lease or queue facts', () => {
  const engine = { ...ready, profile: null, label: null, state: 'stopped', since: NOW - 3600, kv: null,
    detail: 'stopped from the command line', blocked: null, schedule: null };
  const v = e.engineView({ ...base, engine, fleet: null, live: null, last: null });
  assert.equal(v.mode, 'stopped');
  assert.equal(v.mid.kicker, 'No engine');
  assert.deepEqual(v.mid.facts, []);
});
