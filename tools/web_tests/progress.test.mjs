// node --test tools/web_tests/*.test.mjs
// Prefill progress in the browser, replayed on prefills that the inference host's Crucible t15 logged.
// The views are the server's own (tests/test_prefill.py checks the fixture against live/prefill.py).
import { test } from 'node:test';
import assert from 'node:assert/strict';
import { readFileSync } from 'node:fs';
import { estimatePrefill, holdPrefill, prefillShare, prefillLine, FIRST_REPORT_WAIT_S } from '../../src/kiln/web/static/js/progress.js';

const CASES = JSON.parse(readFileSync(new URL('../../tests/fixtures/prefill_replay.json', import.meta.url)));
const T0 = 1000;  // the engine's prefill starts here; report k is logged at T0 + its elapsed
// journald, the journal batch and the tick: a report reaches the browser this much later (strix: 0.3-0.6 s)
const LAG = 0.7;

// The engine's real progress (tokens computed) at time t: its reports, joined by straight lines.
function truth(c, t) {
  let [pe, pd] = [0, 0];
  for (const [done, elapsed] of c.reports) {
    const d = done - c.cached;
    if (t - T0 <= elapsed) return pd + ((d - pd) * (t - T0 - pe)) / (elapsed - pe);
    [pe, pd] = [elapsed, d];
  }
  return pd;
}

// What a browser shows over the whole prefill, held so it never moves back (as hero.js and the panel do).
function replay(c, lag = LAG) {
  const out = [];
  let memo = null;
  let shown = 0;
  const end = T0 + c.reports.at(-1)[1];
  for (let t = T0 - 0.25; t <= end + lag + 0.5; t += 0.1) {
    const k = c.reports.filter(([, e]) => T0 + e + lag <= t).length;  // reports delivered by t
    let est = estimatePrefill(c.views[k], t);
    if (est) ({ est, memo } = holdPrefill(est, memo));
    if (est) shown = est.read;
    out.push({ t, k, est, shown });
  }
  return out;
}

for (const [name, c] of Object.entries(CASES)) {
  for (const lag of [0.3, LAG, 1.0]) test(`replay ${name}, reports ${lag} s late: within 6% of the engine, ends exactly`, () => {
    const work = c.total - c.cached;
    let before = 0;
    for (const { t, k, est, shown } of replay(c, lag)) {
      assert.ok(shown >= before, `${name}: the count dropped from ${before} to ${shown}`);
      before = shown;
      if (!k || !est) continue;
      assert.ok(Math.abs(shown - truth(c, t)) <= 0.06 * work, `${name} at ${(t - T0).toFixed(1)} s: ` +
        `shown ${shown}, engine ${Math.round(truth(c, t))}`);
      if (k < c.reports.length) assert.ok(prefillShare(shown, est.of) < 1, `${name}: 100% before the end`);
    }
    const last = replay(c, lag).at(-1);
    assert.equal(last.est.done, c.total);  // the engine's last report, exact
    assert.equal(last.shown, work);
    assert.equal(last.est.estimated, false);  // a full first chunk or the tail rows after a restore: exact
  });
}

test('replay resumed: a prompt resumed from the cache counts its new tokens, not 100% at once', () => {
  // The old estimate took done / elapsed of the first report (21,751 / 0.06 s) as the rate.
  const c = CASES.resumed;
  const at1 = replay(c).find((r) => r.k === 1);
  assert.ok(prefillShare(at1.shown, at1.est.of) < 0.25);
  assert.ok(Math.abs(at1.est.cached - 21743) < 100);
});

test('replay cold: a count from 1.5 s after the request, then the prompt size at the first report', () => {
  const rows = replay(CASES.cold);
  const first = rows.find((r) => r.est);
  assert.ok(first.t - (T0 - 0.25) <= 1.55 && first.est.of === null);
  const before = rows.filter((r) => r.k === 0 && r.est).at(-1);
  assert.ok(before.shown < 4096 + 1500 * 1.0);  // never past the first report's chunk and its delivery
  const after = rows.find((r) => r.k === 1);
  assert.equal(after.est.of, 37298);
  assert.equal(after.est.cached, 0);
});

test('a first report long overdue ends the count instead of freezing it at its cap', () => {
  const pre = CASES.cold.views[0];  // 1,500 tok/s, first report at 4,096: due 2.7 s in
  assert.ok(estimatePrefill(pre, pre.at + 4).read > 4096);
  assert.equal(estimatePrefill(pre, pre.at + 7), null);  // over twice that, plus the delivery
});

test('a view with nothing to go on shows nothing; a final report is exact', () => {
  assert.equal(estimatePrefill(null, T0), null);
  const pre = CASES.cold.views[0];
  assert.equal(estimatePrefill(pre, pre.at + FIRST_REPORT_WAIT_S - 0.01), null);
  assert.equal(estimatePrefill({ ...pre, rate: null }, pre.at + 5), null);
  const done = CASES.cold.views.at(-1);
  assert.deepEqual(estimatePrefill(done, done.at + 30),
    { done: 37298, total: 37298, cached: 0, read: 37298, of: 37298, estimated: false, rate: done.rate });
});

test('without a rate the last report stands; the share rounds down', () => {
  const v = { ...CASES.block.views[2], rate: null };
  const est = estimatePrefill(v, v.at + 10);
  assert.equal(est.read, v.done - v.start);
  assert.equal(prefillShare(995, 1000), 0.99);
  assert.equal(prefillShare(5, 0), 1);
});

test('hold: a short report never moves the count back; a new prompt starts over; a first size does not', () => {
  const v = CASES.cold.views[2];
  const a = holdPrefill(estimatePrefill(v, v.at + 2), null);
  const late = holdPrefill(estimatePrefill(v, v.at), a.memo);  // the next draw has an older figure
  assert.equal(late.est.done, a.est.done);
  assert.equal(late.est.estimated, true);
  const other = holdPrefill(estimatePrefill(CASES.block.views[1], CASES.block.views[1].at), a.memo);
  assert.equal(other.est.done, CASES.block.views[1].done);
  const pre = holdPrefill(estimatePrefill(CASES.cold.views[0], T0 + 3), null);
  const first = holdPrefill(estimatePrefill(CASES.cold.views[1], CASES.cold.views[1].at), pre.memo);
  assert.equal(first.est.done, pre.est.done);  // the first report landed below the count: it holds
  assert.ok(first.est.done < 37298 - 1);
});

test('prefill line: the count in a box of zeros with its digit count, the share and the time left in their own cells', () => {
  const cold = { read: 26250, of: 218296, cached: 0, estimated: true, rate: 1667 };
  assert.deepEqual(prefillLine(cold), { read: '≈26,250', size: '≈00,000', of: '218,296', unit: 'tokens',
    pct: '12%', cached: '', left: '≈1m 56s left' });
  const warm = { read: 45725, of: 128016, cached: 12000, estimated: false, rate: 2400 };
  assert.deepEqual(prefillLine(warm), { read: '45,725', size: '≈00,000', of: '128,016', unit: 'new tokens',
    pct: '35%', cached: '12K cached', left: '≈35 s left' });
  assert.equal(prefillLine({ ...warm, rate: null }).left, '');  // no rate, no guess
  assert.equal(prefillLine({ ...warm, read: 128016 }).left, '');
  assert.equal(prefillLine({ ...cold, read: 123456 }).size, '≈000,000');  // a new digit: the box grows once
});
