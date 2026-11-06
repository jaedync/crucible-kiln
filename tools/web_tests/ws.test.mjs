// node --test tools/web_tests/*.test.mjs
import { test, mock } from 'node:test';
import assert from 'node:assert/strict';
import { createStore } from '../../src/kiln/web/static/js/store.js';

// A socket that never closes on its own: close() starts a handshake that a wedged peer never answers.
class FakeSocket {
  static all = [];
  static OPEN = 1;
  constructor(url) {
    this.url = url;
    this.readyState = FakeSocket.OPEN;
    this.closeCalls = 0;
    FakeSocket.all.push(this);
  }
  close() { this.closeCalls += 1; this.readyState = 2; }  // CLOSING, and no onclose
  deliver(msg) { if (this.onmessage) this.onmessage({ data: JSON.stringify({ v: 1, ...msg }) }); }
}

globalThis.WebSocket = FakeSocket;
globalThis.location = { href: 'http://kiln.test/', protocol: 'http:', reload() {} };
mock.timers.enable({ apis: ['setInterval', 'setTimeout', 'Date'], now: 1_000_000 });
const { connectLive } = await import('../../src/kiln/web/static/js/ws.js');

test('a silent socket goes stale, then a new socket opens without waiting for the old one to close', () => {
  const store = createStore();
  connectLive(store);
  const first = FakeSocket.all.at(-1);
  first.deliver({ type: 'snapshot', ts: 1000, build: 'b1' });
  assert.equal(store.get().conn, 'open');
  mock.timers.tick(3000);
  assert.equal(store.get().conn, 'stale');
  mock.timers.tick(8000);  // past DEAD_MS: the old socket is dropped
  assert.equal(first.closeCalls, 1);
  assert.equal(store.get().conn, 'reconnecting');
  mock.timers.tick(1000);  // the first backoff step
  const second = FakeSocket.all.at(-1);
  assert.notEqual(second, first, 'a new socket opened while the old one never closed');
  first.deliver({ type: 'tick', ts: 1012 });  // a late frame from the dropped socket is not the feed
  assert.equal(store.get().conn, 'reconnecting');
  second.deliver({ type: 'snapshot', ts: 1013, build: 'b1' });
  assert.equal(store.get().conn, 'open');
});

test('a new socket that stays silent is dropped too, so the page keeps trying', () => {
  const store = createStore();
  connectLive(store);
  const first = FakeSocket.all.at(-1);
  first.deliver({ type: 'snapshot', ts: 2000, build: 'b1' });
  mock.timers.tick(11000);  // stale, then dropped
  mock.timers.tick(1000);  // the retry opens a second socket, which sends nothing (review)
  const second = FakeSocket.all.at(-1);
  assert.notEqual(second, first);
  mock.timers.tick(11000);
  assert.equal(second.closeCalls, 1);
  mock.timers.tick(2000);
  assert.notEqual(FakeSocket.all.at(-1), second, 'a third socket opened');
});
