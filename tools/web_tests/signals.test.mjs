import { test } from 'node:test';
import assert from 'node:assert/strict';
import { fallbackBadge, daySummary, errorTitle } from '../../src/kiln/web/static/js/signal_rows.js';

test('a request that fell back to text gets a badge; others none', () => {
  const badge = fallbackBadge({ tool_call_fallback: true });
  assert.equal(badge.text, 'tool call as text');
  assert.match(badge.title, /not a valid tool call/);
  assert.equal(fallbackBadge({ tool_call_fallback: false }), null);
  assert.equal(fallbackBadge({ tool_call_fallback: null }), null);
  assert.equal(fallbackBadge({}), null);
});

test('day rows keep the counts and mark only the counts above zero', () => {
  const row = { date: '2026-01-15', engine: 'crucible', requests: 40, errors: 2, stream_errors: 1, tool_call_fallbacks: 3 };
  assert.deepEqual(daySummary(row), { errors: { text: '2', tone: 'bad' }, streamErrors: { text: '1', tone: 'bad' },
    fallbacks: { text: '3', tone: 'warn' }, clientErrors: { text: '—', tone: '' } });  // a server without the field
  // An error day without stream errors or fallbacks: those zeros stay plain.
  const some = daySummary({ ...row, stream_errors: 0, tool_call_fallbacks: 0 });
  assert.deepEqual([some.errors.tone, some.streamErrors.tone, some.fallbacks.tone], ['bad', '', '']);
  const quiet = daySummary({ ...row, errors: 0, stream_errors: 0, tool_call_fallbacks: 0 });
  assert.deepEqual([quiet.errors.tone, quiet.streamErrors.tone, quiet.fallbacks.tone], ['', '', '']);
});

test('the error title says what was counted, never any text', () => {
  assert.match(errorTitle(), /server errors/);
  assert.match(errorTitle(), /No message text is stored/);
});

test('client errors are a plain count: the caller made the mistake, not the server', () => {
  const s = daySummary({ date: 'd', engine: 'crucible', requests: 1, errors: 0, stream_errors: 0, client_errors: 179,
    tool_call_fallbacks: 0 });
  assert.deepEqual(s.clientErrors, { text: (179).toLocaleString(), tone: '' });
  assert.equal(daySummary({ errors: 0, stream_errors: 0, tool_call_fallbacks: 0 }).clientErrors.text, '—');
});
