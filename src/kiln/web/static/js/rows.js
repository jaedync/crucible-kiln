// Request rows arrive twice: first from the gateway record, then again (same id)
// once the engine's log line joins with engine timing, think_close and a
// repriced cost. The second copy replaces the first in place.

// Returns { rows, added }: a new array with `row` replacing the same id, or
// prepended when the id is new.
export function upsertRow(rows, row, max = Infinity) {
  const i = rows.findIndex((r) => r.id === row.id);
  if (i >= 0) return { rows: rows.map((r, k) => (k === i ? row : r)), added: false };
  return { rows: [row, ...rows].slice(0, max), added: true };
}

const endOf = (r) => (r.ts_end ?? r.ts + (r.latency_s || 0));

// `last` carries the request id; older servers sent only its end time.
export function sameRequest(last, row) {
  if (!last || !row) return false;
  if (last.id) return last.id === row.id;
  return Math.abs(endOf(row) - last.ts) < 0.01;
}

export function lastFromRow(row) {
  const keys = ['decode_estimated', 'prefill_estimated', 'ttft_s', 'decode_tps', 'prefill_tps', 'prompt',
    'cached', 'completion', 'finish', 'think_close', 'source', 'draft_accepted', 'draft_proposed'];
  return Object.freeze({ id: row.id, ts: endOf(row), ...Object.fromEntries(keys.map((k) => [k, row[k] ?? null])) });
}
