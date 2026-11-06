// node --test tools/web_tests/*.test.mjs
import { test } from 'node:test';
import assert from 'node:assert/strict';
import { pollWhileSeen } from '../../src/kiln/web/static/js/poll.js';

// A fake browser: a clock, timers that run when the test advances time, visibility and an observer.
function fakeEnv() {
  let t = 0;
  let timers = [];
  let observer = null;
  const listeners = [];
  const env = {
    doc: { hidden: false, addEventListener: (_e, fn) => listeners.push(fn) },
    now: () => t,
    setTimeout: (fn, ms) => { const id = { fn, at: t + ms }; timers.push(id); return id; },
    clearTimeout: (id) => { timers = timers.filter((x) => x !== id); },
    setInterval: (fn, ms) => { const tick = () => { fn(); env.setTimeout(tick, ms); }; env.setTimeout(tick, ms); },
    Observer: class { constructor(cb) { observer = cb; } observe() {} },
  };
  const advance = async (ms) => {
    const end = t + ms;
    for (;;) {
      const next = timers.filter((x) => x.at <= end).sort((a, b) => a.at - b.at)[0];
      if (!next) break;
      t = next.at;
      timers = timers.filter((x) => x !== next);
      next.fn();
      await null;
    }
    t = end;
  };
  return { env, advance, see: (on) => observer([{ isIntersecting: on }]),
    hide: (on) => { env.doc.hidden = on; listeners.forEach((fn) => fn()); } };
}

test('loads at once, then every period while seen', async () => {
  const f = fakeEnv();
  let loads = 0;
  pollWhileSeen({}, async () => { loads += 1; }, { periodMs: 60000 }, f.env);
  await null;
  assert.equal(loads, 1);
  await f.advance(61000);
  assert.equal(loads, 2);
});

test('an off-screen or hidden panel asks for nothing, and loads when seen again if stale', async () => {
  const f = fakeEnv();
  let loads = 0;
  pollWhileSeen({}, async () => { loads += 1; }, { periodMs: 60000 }, f.env);
  await null;
  f.see(false);
  await f.advance(300000);
  assert.equal(loads, 1);
  f.see(true);
  await f.advance(0);
  assert.equal(loads, 2);
  f.hide(true);
  await f.advance(300000);
  assert.equal(loads, 2);
  f.hide(false);
  await f.advance(0);
  assert.equal(loads, 3);
});

test('poke loads soon, at most once per minimum gap, and waits while hidden', async () => {
  const f = fakeEnv();
  let loads = 0;
  const p = pollWhileSeen({}, async () => { loads += 1; }, { periodMs: 60000, minGapMs: 5000 }, f.env);
  await null;
  p.poke(1500);
  p.poke(1500);
  await f.advance(4000);
  assert.equal(loads, 1);  // the first load was 4 s ago: the gap holds it to 5 s
  await f.advance(1000);
  assert.equal(loads, 2);
  f.see(false);
  p.poke(0);
  await f.advance(10000);
  assert.equal(loads, 2);
  f.see(true);
  await f.advance(0);
  assert.equal(loads, 3);
});

test('a load asked for while one runs follows it once; a failed load keeps the polling going', async () => {
  const f = fakeEnv();
  let loads = 0;
  let release = null;
  const p = pollWhileSeen({}, () => { loads += 1; return loads === 1 ? new Promise((r) => { release = r; }) : Promise.reject(new Error('x')); },
    { periodMs: 60000 }, { ...f.env });
  p.now();
  p.now();
  assert.equal(loads, 1);
  release();
  await f.advance(0);
  await f.advance(0);
  assert.equal(loads, 2);  // once, not twice
  const errors = console.error;
  console.error = () => {};
  try {
    await f.advance(61000);
  } finally {
    console.error = errors;
  }
  assert.equal(loads, 3);
});

