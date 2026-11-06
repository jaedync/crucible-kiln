// node --test tools/web_tests/*.test.mjs
import { test } from 'node:test';
import assert from 'node:assert/strict';
import { tabTitle } from '../../src/kiln/web/static/js/title.js';

const BASE = 'Kiln Host';
const s = (phase, queued = false) => ({ phase, queued });

test('idle or unknown: the page title', () => {
  assert.equal(tabTitle(null, BASE), BASE);
  assert.equal(tabTitle({ streams: [], decode_tps: 0, slots: 4 }, BASE), BASE);
});

test('active: decode speed first, then streams in slots, then the page title', () => {
  const live = { decode_tps: 142.4, decode_exact: true, slots: 4, streams: [s('decode'), s('decode'), s('prefill')] };
  assert.equal(tabTitle(live, BASE), '142 tok/s 3/4 Kiln Host');
  assert.equal(tabTitle({ ...live, decode_tps: 54.25 }, BASE), '54.3 tok/s 3/4 Kiln Host');
  assert.equal(tabTitle({ ...live, decode_exact: false }, BASE), '142 tok/s 3/4 Kiln Host');  // no ≈: tab width is short
});

test('queued streams wait past the slots; a prompt that only prefills says so', () => {
  const full = { decode_tps: 90, decode_exact: true, slots: 2,
    streams: [s('decode'), s('decode'), s('prefill', true), s('prefill', true)] };
  assert.equal(tabTitle(full, BASE), '90.0 tok/s 2/2 +2 queued Kiln Host');
  assert.equal(tabTitle({ decode_tps: 0, decode_exact: true, slots: 4, streams: [s('prefill')] }, BASE),
    'prefill 1/4 Kiln Host');
  assert.equal(tabTitle({ decode_tps: 0, decode_exact: true, slots: 0, streams: [s('decode')] }, BASE),
    '0 tok/s 1 Kiln Host');  // slots unknown (a remote host's engine): just the count
});
