// node --test tools/web_tests/*.test.mjs
import { test } from 'node:test';
import assert from 'node:assert/strict';
import { upsertRow, sameRequest, lastFromRow } from '../../src/kiln/web/static/js/rows.js';

test('a repeated id replaces the row in place', () => {
  const rows = [{ id: 'c' }, { id: 'b', cost: 1 }, { id: 'a' }];
  const res = upsertRow(rows, { id: 'b', cost: 2 });
  assert.equal(res.added, false);
  assert.deepEqual(res.rows.map((r) => r.id), ['c', 'b', 'a']);
  assert.equal(res.rows[1].cost, 2);
  assert.equal(rows[1].cost, 1, 'input not mutated');
});

test('a new id is prepended and capped', () => {
  const res = upsertRow([{ id: 'a' }, { id: 'b' }], { id: 'n' }, 2);
  assert.equal(res.added, true);
  assert.deepEqual(res.rows.map((r) => r.id), ['n', 'a']);
});

test('last matches the row that ended at last.ts', () => {
  const row = { id: 'x', ts: 100, ts_end: 105.5, decode_tps: 40, decode_estimated: true, think_close: 'answer_room' };
  assert.ok(sameRequest({ ts: 105.5 }, row));
  assert.ok(!sameRequest({ ts: 101 }, row));
  assert.ok(sameRequest({ ts: 103 }, { ts: 100, latency_s: 3 }));
  const last = lastFromRow(row);
  assert.equal(last.ts, 105.5);
  assert.equal(last.decode_estimated, true);
  assert.equal(last.think_close, 'answer_room');
  assert.equal(last.draft_accepted, null);  // an engine without draft counts
  const spec = lastFromRow({ ...row, draft_accepted: 360, draft_proposed: 480 });
  assert.deepEqual([spec.draft_accepted, spec.draft_proposed], [360, 480]);
});
