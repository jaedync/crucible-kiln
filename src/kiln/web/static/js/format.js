// Number, time and money formatting. Every function returns DASH for a missing
// value instead of inventing a zero, so "unknown" never reads as "none".

export const DASH = '—';
const missing = (v) => v === null || v === undefined || Number.isNaN(v);

const intFmt = new Intl.NumberFormat(undefined, { maximumFractionDigits: 0 });
const oneFmt = new Intl.NumberFormat(undefined, { minimumFractionDigits: 1, maximumFractionDigits: 1 });
const compactFmt = new Intl.NumberFormat(undefined, { notation: 'compact', maximumFractionDigits: 1 });
const pctFmt = new Intl.NumberFormat(undefined, { style: 'percent', maximumFractionDigits: 0 });

export const int = (n) => (missing(n) ? DASH : intFmt.format(n));
export const one = (n) => (missing(n) ? DASH : oneFmt.format(n));
export const compact = (n) => (missing(n) ? DASH : Math.abs(n) < 10000 ? intFmt.format(n) : compactFmt.format(n));
export const pct = (ratio) => (missing(ratio) ? DASH : pctFmt.format(ratio));

// Rates: one decimal below 100 tok/s (decode territory), whole numbers above
// (prefill territory). An exact zero prints as "0", not "0.0".
export function rate(tps) {
  if (missing(tps)) return DASH;
  if (tps === 0) return '0';
  return tps < 100 ? oneFmt.format(tps) : intFmt.format(tps);
}

export function usd(v) {
  if (missing(v)) return DASH;
  const a = Math.abs(v);
  if (a === 0) return '$0.00';
  if (a >= 1000) return '$' + intFmt.format(v);
  if (a >= 0.01) return '$' + v.toFixed(2);
  // Single requests on a local model cost fractions of a cent; keep 2 significant digits.
  return '$' + v.toPrecision(2).replace(/0+$/, '');
}

export const ESTIMATED_RATE = 'Estimated from gateway timing, not the engine\'s own figures.';
// A rate the server flags as estimated carries a leading ≈.
export const estRate = (tps, estimated) => (missing(tps) ? DASH : `${estimated ? '≈' : ''}${rate(tps)}`);

export const usdPerM = (v) => (missing(v) ? DASH : '$' + (v >= 1 ? v.toFixed(2) : v.toFixed(3).replace(/0$/, '')));

// Short latencies (TTFT, prefill elapsed): 410 ms, 1.32 s, 12.4 s, then durations.
export function secs(s) {
  if (missing(s)) return DASH;
  if (s < 1) return `${Math.round(s * 1000)} ms`;
  if (s < 10) return `${s.toFixed(2)} s`;
  if (s < 60) return `${s.toFixed(1)} s`;
  return dur(s);
}

// Longer spans (uptime, busy time): 45 s, 12m 05s, 2h 14m, 3d 4h.
export function dur(s) {
  if (missing(s)) return DASH;
  s = Math.max(0, Math.floor(s));
  if (s < 60) return `${s} s`;
  const d = Math.floor(s / 86400), hr = Math.floor((s % 86400) / 3600);
  const m = Math.floor((s % 3600) / 60), sec = s % 60;
  if (d) return `${d}d ${hr}h`;
  if (hr) return `${hr}h ${String(m).padStart(2, '0')}m`;
  return `${m}m ${String(sec).padStart(2, '0')}s`;
}

export function ago(ts, now) {
  if (missing(ts)) return DASH;
  const s = Math.max(0, now - ts);
  if (s < 5) return 'just now';
  if (s < 60) return `${Math.floor(s)} s ago`;
  if (s < 3600) return `${Math.floor(s / 60)} min ago`;
  if (s < 86400) return `${Math.floor(s / 3600)} h ago`;
  return dateTime(ts);
}

const clockFmt = new Intl.DateTimeFormat(undefined, { hour: '2-digit', minute: '2-digit', second: '2-digit' });
const hmFmt = new Intl.DateTimeFormat(undefined, { hour: '2-digit', minute: '2-digit' });
const dtFmt = new Intl.DateTimeFormat(undefined, { month: 'short', day: 'numeric', hour: '2-digit', minute: '2-digit' });
const dayFmt = new Intl.DateTimeFormat(undefined, { month: 'short', day: 'numeric' });
const dateFmt = new Intl.DateTimeFormat(undefined, { month: 'short', day: 'numeric', year: 'numeric' });

export const clock = (ts) => (missing(ts) ? DASH : clockFmt.format(ts * 1000));
export const hm = (ts) => (missing(ts) ? DASH : hmFmt.format(ts * 1000));
export const dateTime = (ts) => (missing(ts) ? DASH : dtFmt.format(ts * 1000));
export const day = (ts) => (missing(ts) ? DASH : dayFmt.format(ts * 1000));
export const date = (ts) => (missing(ts) ? DASH : dateFmt.format(ts * 1000));

// Time of a recent event: clock time today, date + time otherwise.
export function when(ts, now) {
  if (missing(ts)) return DASH;
  const sameDay = new Date(ts * 1000).toDateString() === new Date(now * 1000).toDateString();
  return sameDay ? clock(ts) : dateTime(ts);
}

const GIB = 1024 ** 3;
export const gib = (bytes, digits = 1) => (missing(bytes) ? DASH : `${(bytes / GIB).toFixed(digits)} GiB`);
export const gibNum = (bytes, digits = 1) => (missing(bytes) ? DASH : (bytes / GIB).toFixed(digits));

export const celsius = (c) => (missing(c) ? DASH : `${c.toFixed(1)}°C`);

// How a request ended. think_close is null when thinking ended on its own,
// otherwise the engine's reason for cutting it off (e.g. "answer_room").
export function finishInfo(r) {
  const parts = [];
  const why = [];
  if (r.finish === 'length') {
    parts.push('answer truncated');
    why.push('The answer hit max_tokens.');
  } else {
    parts.push(r.finish || DASH);
  }
  if (r.think_close) {
    parts.push('thinking cut off');
    why.push(`The engine cut thinking off and forced the answer (${r.think_close}).`);
  }
  return { text: parts.join(' · '), bad: why.length > 0, title: why.join(' ') || null };
}

// "1 request", "3 requests"; the count is formatted with separators.
export const plural = (n, word) => `${int(n)} ${word}${n === 1 ? '' : 's'}`;

// Device display name: the Tailscale (or chosen) name, then the raw key. The X-Kiln-Client
// label is secondary text shown next to it, never the identity.
export const clientName = (c) => (c ? c.name || c.key : DASH);
