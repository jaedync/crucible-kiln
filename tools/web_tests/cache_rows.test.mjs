import { test } from 'node:test';
import assert from 'node:assert/strict';
import { CAUSE, causeTitle, engineNote } from '../../src/kiln/web/static/js/cache_rows.js';

test('client changes name what changed', () => {
  const t = { cause: 'client', prefix_msgs_kept: 2, prev_n_messages: 40, system_changed: false, tools_changed: true };
  assert.equal(causeTitle(t), 'The client kept 2 of its 40 earlier messages, changed the tool list. Any engine must read the changed part again.');
});

test('engine misses give the re-read count', () => {
  const t = { cause: 'engine', prev_n_messages: 12, reread_tokens: 98304 };
  assert.equal(causeTitle(t), `The client kept all 12 earlier messages, but the engine read ${(98304).toLocaleString()} of their tokens again.`);
  assert.equal(CAUSE.engine.tone, 'bad');
});

test('engine notes show refusals and Halogen prefix matches', () => {
  assert.equal(engineNote({ cache_save_refused: 'host', cache_session_label: 'hit_session', cache_evictions: 3 }),
    'save refused (host) · hit session · 3 evictions');
  assert.equal(engineNote({ closest_outcome: 'prompt_differs', closest_shared_tokens: 4000 }),
    `shares ${(4000).toLocaleString()} tokens, then the prompt differs`);
  assert.equal(engineNote({ closest_outcome: 'all', closest_shared_tokens: 4000 }), '');
});
