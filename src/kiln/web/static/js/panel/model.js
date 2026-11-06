// Pure helpers for the rack panel (/panel): what each tile says, the trend lines and the touch gestures.
// No DOM here, so tools/web_tests can check every rule.
import { DASH, dur, int } from '../format.js';

const missing = (v) => v === null || v === undefined || Number.isNaN(v);

// -- engine -----------------------------------------------------------------------------------
// "Halogen / Qwen3.8 Flash-Next" -> ["Halogen", "Qwen3.8 Flash-Next"]: the engine fits the tile in big
// type and the model goes on the line below.
export function splitLabel(label) {
  if (!label) return [DASH, ''];
  const m = /\s+[·/|]\s+/.exec(label);
  return m ? [label.slice(0, m.index), label.slice(m.index + m[0].length)] : [label, ''];
}

// Requests the engine has accepted beyond its slots wait in its queue.
export function waiting(live) {
  if (!live || missing(live.in_flight) || missing(live.slots)) return null;
  return Math.max(0, live.in_flight - live.slots);
}

// -- trend lines ----------------------------------------------------------------------------------
const pt = (v) => String(Math.round(v * 10) / 10);

// An SVG path over w x h. Unknown samples break the line; the scale starts at 0.
export function sparkPath(values, w, h, max) {
  if (!values.length) return '';
  const known = values.filter((v) => !missing(v));
  const top = max || Math.max(0, ...known) || 1;
  const step = values.length > 1 ? w / (values.length - 1) : 0;
  let path = '';
  let pen = false;
  values.forEach((v, i) => {
    if (missing(v)) { pen = false; return; }
    const y = h - (Math.min(v, top) / top) * h;
    path += `${pen ? 'L' : 'M'}${pt(i * step)},${pt(y)}`;
    pen = true;
  });
  return path;
}

// -- column charts (the glance page's hero) ----------------------------------------------------------
// Windows are aligned to the clock, so a column keeps its value as the chart moves on: within a window
// only the newest column changes (all of them when it sets a new top), and the strip shifts once a window.

// [[t, value]] from a per-second history that ends at second `last`.
export function perSecond(last, values) {
  if (last === null || last === undefined) return [];
  return values.map((v, i) => [last - (values.length - 1 - i), v]);
}

// Timed samples, the newest keepS seconds. An unknown reading is kept: it still says when it was read.
export function pushTimed(samples, t, v, keepS) {
  return [...samples, [t, v]].filter(([ts]) => ts > t - keepS);
}

// The mean of the known samples in each of the last n windows of windowS seconds, the newest window
// (the one holding the newest sample) last. A window with no known sample is null.
export function columns(samples, windowS, n) {
  const out = Array(n).fill(null);
  if (!samples.length) return out;
  const newest = Math.floor(samples[samples.length - 1][0] / windowS);
  const sums = Array(n).fill(0);
  const counts = Array(n).fill(0);
  for (const [t, v] of samples) {
    const i = n - 1 - (newest - Math.floor(t / windowS));
    if (i < 0 || i >= n || missing(v)) continue;
    sums[i] += v;
    counts[i] += 1;
  }
  return out.map((_, i) => (counts[i] ? sums[i] / counts[i] : null));
}

// Each column's height in percent of `top` (default: the largest), from 0. Unknown is 0.
export function columnHeights(values, top) {
  const t = top || Math.max(0, ...values.filter((v) => !missing(v))) || 1;
  return values.map((v) => (missing(v) ? 0 : Math.round((1000 * Math.min(v, t)) / t) / 10));
}

export function trendStats(values) {
  const known = values.filter((v) => !missing(v));
  if (!known.length) return { peak: null, mean: null };
  return { peak: Math.max(...known), mean: known.reduce((a, b) => a + b, 0) / known.length };
}

export const pushSample = (arr, v, max) => [...arr, v].slice(-max);

// Decode tokens per second (`values`) and prefill tokens per second (`prefill`): seeded from the
// snapshot's series_1s, then fed the completed seconds of each tick. A second with no entry had no
// tokens, so it counts as 0.
export function seedSeconds(series, n) {
  const t = (series && series.t) || [];
  const d = (series && series.decode) || [];
  const p = (series && series.prefill) || [];
  return { last: t.length ? t[t.length - 1] : null, values: d.slice(-n), prefill: d.map((_, i) => p[i] || 0).slice(-n) };
}

export function addSeconds(hist, seconds, n) {
  let { last } = hist;
  const values = [...hist.values];
  const prefill = [...hist.prefill];
  for (const s of seconds || []) {
    if (last !== null && s.t <= last) continue;
    if (last !== null) {
      for (let t = last + 1; t < s.t && t - last <= n; t += 1) {
        values.push(0);
        prefill.push(0);
      }
    }
    values.push(s.decode || 0);
    prefill.push(s.prefill || 0);
    last = s.t;
  }
  return { last, values: values.slice(-n), prefill: prefill.slice(-n) };
}

// -- paging ---------------------------------------------------------------------------------------
const SWIPE_PX = 60;
export const TAP_PX = 20;

// A tap (a tab or a button may fire) or a swipe (dir +1 next page, -1 previous page). `moved` is the
// farthest the pointer went from its start, so a swipe that comes back to its start is no tap.
export function gesture(start, end, moved = 0) {
  const dx = end.x - start.x;
  const dy = end.y - start.y;
  const far = Math.max(moved, Math.hypot(dx, dy));
  if (far < TAP_PX) return { tap: true, dir: 0 };
  if (Math.abs(dx) >= SWIPE_PX && Math.abs(dx) > Math.abs(dy)) return { tap: false, dir: dx < 0 ? 1 : -1 };
  return { tap: false, dir: 0 };
}

// The panel is laid out for the rack's 1424 x 280 screen. Since every /panel session follows the rack, a
// laptop or phone window shows it too: scaled to fit and centred. The kiosk's window is exactly that size
// and gets no transform at all, so its software rendering pays nothing for this.
export const SCREEN_W = 1424;
export const SCREEN_H = 280;
export function fitScreen(w, h) {
  if (!(w > 0 && h > 0) || (w === SCREEN_W && h === SCREEN_H)) return null;
  const s = Math.min(w / SCREEN_W, h / SCREEN_H);
  const px = (v) => Math.max(0, Math.round(v));  // whole pixels: a fractional offset blurs every edge
  return { s, x: px((w - SCREEN_W * s) / 2), y: px((h - SCREEN_H * s) / 2) };
}

export const turn = (page, count, dir) => (((page + dir) % count) + count) % count;

// -- fleet ----------------------------------------------------------------------------------------
export function leaseInfo(fleet) {
  if (!fleet) return { text: DASH, sub: 'no fleet data', queued: null, tone: null };
  if (fleet.error) return { text: DASH, sub: fleet.error, queued: null, tone: 'bad' };
  const queued = (fleet.queue || []).length;
  const l = fleet.lease;
  // By day the daily engine holds the GPU lock itself: it has no lease line of the lease wrapper, priority or limit.
  if (l && l.daily) return { text: 'daily engine', sub: 'by day', queued, tone: 'busy' };
  if (l) return { text: l.agent, sub: `${l.kind} · p${l.prio ?? DASH} · ${dur(l.elapsed_s)}`, queued, tone: 'busy' };
  const hi = holdsInfo(fleet);
  if (hi.count) return { text: 'Held', sub: `by ${hi.names}`, queued, tone: 'maint' };
  return { text: 'Free', sub: 'no lease', queued, tone: 'idle' };
}

// -- big numbers ------------------------------------------------------------------------------------
const GIB = 1024 ** 3;
export const watts = (w) => (missing(w) ? DASH : String(Math.round(w)));
export const degrees = (c) => (missing(c) ? DASH : String(Math.round(c)));
export const mhz = (n) => (missing(n) ? DASH : int(n));
export function gibBig(bytes) {
  if (missing(bytes)) return DASH;
  const g = bytes / GIB;
  return g < 1 ? g.toFixed(2) : g < 100 ? g.toFixed(1) : g.toFixed(0);
}

// The server lists at most a few hold names and gives the exact count in holds_total.
export function holdsInfo(fleet) {
  if (!fleet || fleet.error) return { count: null, names: '' };
  const names = fleet.holds || [];
  const count = Number.isInteger(fleet.holds_total) ? fleet.holds_total : names.length;
  const more = count - names.length;
  return { count, names: names.join(', ') + (more > 0 ? ` + ${more} more` : '') };
}

// -- fleet page -------------------------------------------------------------------------------------
// Newest first. Back-to-back leases with the same agent, kind and outcome fold into one line: a test
// loop's gates runs would otherwise fill the tile with copies.
export function recentRows(fleet, max) {
  const outcome = (r) => (r.rc === 0 ? 'ok' : missing(r.rc) ? '?' : `rc ${r.rc}`);
  const groups = [];
  for (const r of (fleet && fleet.recent) || []) {
    const last = groups[groups.length - 1];
    if (last && last.r.agent === r.agent && last.r.kind === r.kind && outcome(last.r) === outcome(r)) last.n += 1;
    else groups.push({ r, n: 1 });
  }
  return groups.slice(0, max).map(({ r, n }) => ({
    agent: r.agent, what: n > 1 ? `${r.kind} × ${n} · last ${dur(r.held_s)}` : `${r.kind} · ${dur(r.held_s)}`,
    outcome: outcome(r), tone: r.rc === 0 ? 'ok' : missing(r.rc) ? null : 'bad',
  }));
}

// -- today page -------------------------------------------------------------------------------------
export function clientRows(usage, max) {
  if (!usage || !usage.rows) return { rows: [], count: null };
  const all = [...usage.rows].sort((a, b) => (b.requests || 0) - (a.requests || 0));
  return {
    rows: all.slice(0, max).map((r) => ({ name: r.name || r.key, color: r.color || null, requests: int(r.requests) })),
    count: all.length,
  };
}

// -- engine page ------------------------------------------------------------------------------------
export const TTFT_OLD_S = 600;  // from across the room a big TTFT reads as current, so an old one dims

export const ttftOld = (last, now) => Boolean(last) && !missing(last.ts) && now - last.ts > TTFT_OLD_S;

