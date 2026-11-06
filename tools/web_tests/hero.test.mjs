// node --test tools/web_tests/*.test.mjs
import { test } from 'node:test';
import assert from 'node:assert/strict';
import { stateLine, decodeMeta } from '../../src/kiln/web/static/js/panels/hero.js';

const s = (phase, queued = false) => ({ phase, queued });

test('state line: a stream in the engine queue is queued, not prefilling', () => {
  const live = { streams: [s('decode'), s('decode'), s('decode'), s('prefill'), s('prefill'), s('prefill'), s('prefill', true)] };
  assert.equal(stateLine(live, null), '3 decoding · 3 prefilling · 1 queued');
  assert.equal(stateLine({ streams: [s('decode'), s('prefill')] }, null), '1 decoding · 1 prefilling');
});

test('stream meta while decoding: TTFT and decode time first, so a narrow row cuts the model name, not them', () => {
  const st = { model: 'qwen3.8-flash-next', started: 100, first_token: 164, client: null };
  assert.equal(decodeMeta(st, 194), 'TTFT 1m 04s · 30.0 s · qwen3.8-flash-next');
  // The client's label stays while it decodes (it used to be replaced).
  assert.equal(decodeMeta({ ...st, client: { label: 'pi' } }, 194), 'TTFT 1m 04s · 30.0 s · pi · qwen3.8-flash-next');
});
