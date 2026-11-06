// Prefill progress between engine reports. The server (live/prefill.py) sends the stream's last report,
// its rate (from its own reports, or the profile's recent cold prefills before it has two), the cached
// start and where the next report lands. Between reports the count moves on at that rate. It may pass
// the next report's position by DELIVERY_SLACK_S of work: a report reaches the browser up to about 1 s
// after the engine logs it (journald batches its notices), and a count that stopped at every chunk
// boundary to wait for it would be both choppy and low. Only the engine's last report claims the end.
import { compact, dur, int, pct } from './format.js';

export const DELIVERY_SLACK_S = 1.0;
// Before the first report, the count starts this long after `at` (when the engine could begin, plus its
// lead), 1.5 s in all: a prompt resumed from the cache has its first report by then (0.1 s of engine
// work and at most 0.6 s on the way, measured), so a mostly cached prompt never shows a count that then
// drops to the report's few rows. The server sends no count while another prompt prefills.
export const FIRST_REPORT_WAIT_S = 1.25;
// A first report this many times later than the recent rate says (a stream that never paired, or an
// engine that stalled) ends the count: a count frozen at its cap would look like a hang.
const OVERDUE = 2;

// estimatePrefill(progress, now) -> { done, total, cached, read, of, estimated, rate } or null
//   done, total: prompt tokens in the engine's cache now, of the whole prompt (total is null before the
//     first report, when the prompt size is not known)
//   cached: tokens it took from its cache (0 for a cold prompt, null before the first report)
//   read, of: the same without the cached start: what the engine computed, of what it has to compute
//   rate: tokens per second for the time left (null when unknown)
export function estimatePrefill(p, now) {
  if (!p) return null;
  const rate = p.rate > 0 ? p.rate : null;
  const run = Math.max(0, now - p.at);
  if (p.total == null) {
    if (!rate || !p.next || run < FIRST_REPORT_WAIT_S || run > (OVERDUE * p.next) / rate + DELIVERY_SLACK_S) return null;
    const read = Math.floor(Math.min(p.next - 1 + rate * DELIVERY_SLACK_S, rate * run));
    return { done: read, total: null, cached: null, read, of: null, estimated: true, rate };
  }
  const start = Math.max(0, Math.min(p.start || 0, p.done));
  let done = p.done;
  if (rate && p.done < p.total) {
    const cap = Math.min(p.total - 1, (p.next || p.total) - 1 + rate * DELIVERY_SLACK_S);
    done = Math.max(p.done, Math.floor(Math.min(cap, p.done + rate * run)));
  }
  return { done, total: p.total, cached: start, read: done - start, of: p.total - start,
    estimated: done > p.done || Boolean(p.start_estimated && start > 0), rate };
}

// holdPrefill(estimate, memo) -> { est, memo }: the count shown never moves back, even when a report
// lands short of the estimate. It is held in whole-prompt tokens, the engine's own unit, and the cached
// start at its lowest for the prompt (each report re-estimates it a little), so the tokens computed
// only rise and their total never shrinks under them. `memo` is the last call's (null at first); a
// different prompt size starts over, a first or lost size does not.
export function holdPrefill(est, memo) {
  const same = memo && (memo.total === est.total || memo.total == null || est.total == null);
  let done = Math.max(same ? memo.done : 0, est.done);
  if (est.total != null && est.done < est.total) done = Math.min(done, est.total - 1);  // the last report's
  const keep = same && memo.total === est.total && memo.cached != null && est.cached != null;
  const cached = keep ? Math.min(memo.cached, est.cached) : est.cached;
  return {
    est: { ...est, done, cached, read: done - (cached || 0), of: est.total == null ? null : est.total - (cached || 0),
      estimated: est.estimated || done > est.done },
    memo: { total: est.total, done, cached },
  };
}

// The share of the work done, rounded down: 99.6% must not read as done.
export function prefillShare(read, of) {
  if (!(of > 0)) return 1;
  return Math.min(1, Math.floor((100 * read) / of) / 100);
}

// prefillLine(est) -> the hero row's progress cells, for an estimate with a known size (est.of). DM Sans
// digits differ in width, so `size` is the count with every digit 0 (the widest) and room for the ≈: a box
// that wide holds every count with as many digits, and the text after it moves only when a digit is added.
export function prefillLine(est) {
  const left = est.rate && est.read < est.of ? `≈${dur(Math.ceil((est.of - est.read) / est.rate))} left` : '';
  return { read: `${est.estimated ? '≈' : ''}${int(est.read)}`, size: `≈${int(est.read).replace(/[0-9]/g, '0')}`, of: int(est.of),
    unit: est.cached ? 'new tokens' : 'tokens', pct: pct(prefillShare(est.read, est.of)),
    cached: est.cached ? `${compact(est.cached)} cached` : '', left };
}
