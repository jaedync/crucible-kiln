// What the rack panel's Today screen says (pure; tools/web_tests/panel_fleet_today.test.mjs): requests per
// hour from the `buckets` of /api/v1/usage?period=today (one per local hour, 23-25 on a day of a clock
// change), the token totals and the GPU busy time. The page polls the usage every 15 s, so it changes then.
import { DASH, compact, dur, int } from '../format.js';

const HOURS = 24;
const missing = (v) => v === null || v === undefined || Number.isNaN(v);

// The local hour of a bucket from its label ("2026-10-15 13:00"): the server's time zone, not this browser's.
const hourOf = (label) => {
  const m = /\s(\d\d):/.exec(label || '');
  return m ? Number(m[1]) : null;
};
const h12 = (h) => (h % 12 === 0 ? 12 : h % 12);
const ampm = (h) => `${h12(h)} ${h < 12 ? 'AM' : 'PM'}`;
// The axis names every third hour; noon and midnight say which half of the day they start.
const axis = (h) => (h === null || h % 3 ? '' : h % 12 === 0 ? ampm(h) : String(h12(h)));

const empty = () => ({ cols: Array.from({ length: HOURS }, () => ({ pct: 0, zero: true, now: false })), peak: '',
  labels: Array.from({ length: HOURS }, (_, h) => axis(h)) });

// Each hour's height in percent of the busiest hour, the hour that holds `now` (a light, outlined column),
// the peak and the axis labels.
export function hourColumns(buckets, now) {
  const t = buckets && Array.isArray(buckets.t) ? buckets.t : null;
  const counts = buckets && Array.isArray(buckets.requests) ? buckets.requests : null;
  if (!t || !counts || !t.length || t.length !== counts.length) return empty();
  const step = buckets.step_s || 3600;
  const top = Math.max(0, ...counts.filter((v) => !missing(v)));
  const cols = counts.map((v, i) => ({ pct: top && v ? Math.round((1000 * v) / top) / 10 : 0, zero: !v,
    now: t[i] <= now && now < (i + 1 < t.length ? t[i + 1] : t[i] + step) }));
  const at = counts.indexOf(top);
  const hour = hourOf((buckets.local || [])[at]);
  const peak = !top ? 'No requests yet' : `Peak ${int(top)}${hour === null ? '' : ` · ${ampm(hour)}`}`;
  return { cols, peak, labels: t.map((_, i) => axis(hourOf((buckets.local || [])[i]))) };
}

const plural = (n, word) => `${int(n)} ${word}${n === 1 ? '' : 's'}`;

export function requestsSub(usage) {
  if (!usage || !usage.totals) return '';
  return `${plural(usage.totals.errors || 0, 'error')} · ${plural((usage.rows || []).length, 'client')}`;
}

// The live totals (tick.today) for the tokens; the usage poll for the GPU busy time (the union of the
// requests' times, so overlapping requests count once).
export function tokenFacts(today, usage) {
  const share = today && today.prompt ? today.cached / today.prompt : null;
  const busy = usage && usage.totals ? usage.totals.busy_s : null;
  return [
    { label: 'Prompt', value: compact(today ? today.prompt : null) },
    { label: 'Cached', value: share === null ? DASH : `${Math.round(share * 100)} %` },
    { label: 'Output', value: compact(today ? today.completion : null) },
    { label: 'GPU busy', value: missing(busy) ? DASH : dur(busy) }];
}
