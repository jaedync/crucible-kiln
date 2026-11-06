// node --test tools/web_tests/*.test.mjs
import { test } from 'node:test';
import assert from 'node:assert/strict';
import { niceMax, lastIndex } from '../../src/kiln/web/static/js/series.js';
import { trimDays } from '../../src/kiln/web/static/js/panels/activity.js';
import { profileGroups, detailText } from '../../src/kiln/web/static/js/panels/engine.js';
import { parseTone, engineItem } from '../../src/kiln/web/static/js/panels/health.js';
import { hasSignal, quietSummary } from '../../src/kiln/web/static/js/signal_rows.js';
import { referenceName, referenceShort } from '../../src/kiln/web/static/js/pricing.js';

test('axis tops use fine steps, not the next power of ten', () => {
  assert.equal(niceMax(105, 10), 120);
  assert.equal(niceMax(52, 10), 60);
  assert.equal(niceMax(0, 10), 10);
  assert.equal(niceMax(1900, 100), 2500);
  assert.equal(niceMax(null, 100), 100);
});

test('the legend falls back to the newest non-null value', () => {
  assert.equal(lastIndex([1, 2, null, null]), 1);
  assert.equal(lastIndex([null, null]), -1);
  assert.equal(lastIndex([]), -1);
});

test('the heatmap drops leading empty days but keeps a minimum', () => {
  const day = (date, busy) => ({ date, hours: [{ busy_s: busy, top: null }] });
  const days = [day('a', 0), day('b', 0), day('c', 5), day('d', 0)];
  assert.deepEqual(trimDays(days, 1).map((d) => d.date), ['c', 'd']);
  assert.deepEqual(trimDays(days, 3).map((d) => d.date), ['b', 'c', 'd']);
  assert.deepEqual(trimDays([day('a', 0), day('b', 0)], 1).map((d) => d.date), ['b']);
});

test('profiles group by engine; unavailable ones go behind the disclosure unless active', () => {
  const p = (name, engine, disabled = null) => ({ name, engine, disabled });
  const groups = profileGroups([p('h1', 'halogen'), p('c1', 'crucible', 'no soak'), p('c2', 'crucible'),
    p('c3', 'crucible', 'not delivered')], 'c3');
  assert.deepEqual(groups.map((g) => [g.title, g.ready.map((x) => x.name), g.unavailable.map((x) => x.name)]),
    [['Halogen', ['h1'], []], ['Crucible', ['c2', 'c3'], ['c1']]]);
});

test('engine details drop a repeated profile label', () => {
  assert.equal(detailText('Crucible t14 · X answered a test generation', 'Crucible t14 · X'), 'Answered a test generation');
  assert.equal(detailText('container exited', 'Crucible t14 · X'), 'container exited');
  assert.equal(detailText('', 'L'), '');
});

test('parse errors are red only while fresh', () => {
  assert.equal(parseTone(0, null, 1000), null);
  assert.equal(parseTone(3, 900, 1000), 'bad');
  assert.equal(parseTone(3, 1000 - 7200, 1000), 'warn');
  assert.equal(parseTone(3, null, 1000), 'bad'); // a server without the timestamp
});

test('the health line names an engine that is not serving: red when it failed', () => {
  assert.equal(engineItem({ state: 'ready' }), null);
  assert.equal(engineItem(null), null);
  assert.deepEqual(engineItem({ state: 'failed' }), { text: 'failed', tone: 'bad' });
  // The night window stops it every night: a normal state, named but not coloured (review).
  assert.deepEqual(engineItem({ state: 'stopped' }), { text: 'stopped', tone: null });
  assert.deepEqual(engineItem({ state: 'starting' }), { text: 'starting', tone: 'warn' });
  assert.deepEqual(engineItem({ state: 'stopping' }), { text: 'stopping', tone: 'warn' });
});

test('quiet signal rows are counted, not listed', () => {
  const rows = [{ errors: 0, stream_errors: 0, tool_call_fallbacks: 7, client_errors: 179 },
    { errors: 0, stream_errors: 0, tool_call_fallbacks: 0, client_errors: 0 }];
  assert.deepEqual(rows.map(hasSignal), [true, false]);
  assert.equal(quietSummary(rows, 14), '1 engine-day with signals; 1 quiet one hidden.');
  assert.equal(quietSummary([rows[1]], 14), 'No errors or tool calls as text in the last 14 days (1 engine-day).');
});

test('the reference name comes from the snapshot', () => {
  assert.equal(referenceName({ reference: 'Claude Sonnet 5.5' }), 'Claude Sonnet 5.5');
  assert.equal(referenceShort({ reference: 'Claude Sonnet 5.5' }), 'Sonnet 5.5');
  assert.equal(referenceShort(null), 'Reference');
});
