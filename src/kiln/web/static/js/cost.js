// Today's "Via API" figure while requests run. tick.today.cost.reference counts finished requests only (a
// request is priced when its record arrives, after it ends), so on its own it sits still while a request
// runs and then jumps. liveCost adds what each running request has cost so far at the reference prices:
// its cached prefix at the cache price as soon as the engine's first report gives it, its new prompt
// tokens at the input price as the engine reads them (progress.js moves them on between reports), the
// prompt kept when the stream decodes, and its output so far, moving on between ticks at the stream's
// rate as count.js does. A prompt with no report (one that fits in one chunk, or whose cached prefix is
// unknown) is priced when it ends. The figure is shown to 1/10,000 dollar (a "unit").
import { CAP_S } from './count.js';
import { DASH } from './format.js';
import { estimatePrefill } from './progress.js';

export const DECIMALS = 4;
export const UNIT = 10 ** DECIMALS;
export const TAU_S = 0.25;  // the shown figure closes 1 - 1/e of its gap to the target in this time
const MIN_RATE = 8;  // units a second at least, so the last unit of a step does not crawl in for a second

// storedCost(tick) -> today's finished requests at the reference price, or null
export function storedCost(tick) {
  const c = tick && tick.today && tick.today.cost ? tick.today.cost.reference : null;
  return c === undefined ? null : c;
}

// liveCost(tick, pricing, now) -> dollars or null; pricing: {input_per_m, cached_per_m, output_per_m}, and
// a price that is missing adds nothing
export function liveCost(tick, pricing, now) {
  const p = liveParts(tick, pricing, now);
  return p === null ? null : p.stored + p.cached + p.input + p.output;
}

// liveParts(tick, pricing, now) -> {stored, cached, input, output} dollars or null: the finished requests,
// and the running ones by what they cost so far: cache reads, new prompt tokens, output
export function liveParts(tick, pricing, now) {
  const stored = storedCost(tick);
  if (stored === null) return null;
  const p = pricing || {};
  const run = Math.min(CAP_S, Math.max(0, now - tick.ts));
  let cached = 0;
  let input = 0;
  let output = 0;
  for (const s of todays(tick)) {
    output += per((s.tokens || 0) + (s.tps > 0 ? s.tps * run : 0), p.output_per_m);
    const prompt = promptTokens(s, now);
    if (prompt) {
      cached += per(prompt.cached, p.cached_per_m);
      input += per(prompt.fresh, p.input_per_m);
    }
  }
  return { stored, cached, input, output };
}

// moving(tick, now) -> whether liveCost can change before the next tick: a stream decodes, or a prompt's
// priced tokens grow within LOOK_S (a prompt at its estimate's cap, or one not priced live, keeps still), and
// the tick is at most CAP_S old (count.js). It keeps the frames running, so it must not say yes for nothing.
const LOOK_S = 1;

export function moving(tick, now) {
  if (!tick || !tick.live || now - tick.ts > CAP_S) return false;
  const priced = (s, t) => {
    const p = promptTokens(s, t);
    return p ? p.cached + p.fresh : 0;
  };
  return todays(tick).some((s) => s.tps > 0 || (reading(s) && priced(s, now + LOOK_S) > priced(s, now)));
}

// todays(tick) -> the running streams that count today: one that started before the day's start (today.since,
// from the server) belongs to yesterday's figure, which its record joins.
function todays(tick) {
  const since = tick.today && tick.today.since;
  const streams = (tick.live && tick.live.streams) || [];
  return since > 0 ? streams.filter((s) => !(s.started < since)) : streams;
}

const per = (tokens, price) => (price > 0 && tokens > 0 ? (tokens * price) / 1e6 : 0);
const reading = (s) => s.phase === 'prefill' && !s.queued && Boolean(s.prefill_progress);

// promptTokens(stream, now) -> {cached, fresh} so far, or null while it is not known
function promptTokens(s, now) {
  if (s.phase !== 'prefill') {
    return s.prompt ? { cached: s.prompt.cached, fresh: s.prompt.total - s.prompt.cached } : null;
  }
  // A start the server could not tell from work would price a cached prefix at the input price.
  if (!reading(s) || (s.prefill_progress.reports > 0 && s.prefill_progress.start_known === false)) return null;
  const est = estimatePrefill(s.prefill_progress, now);
  return est ? { cached: est.cached || 0, fresh: est.read } : null;
}

// Today's cost by kind, for the bar under the figure: cache reads, new prompt tokens, output. The finished
// requests' stored figure is split by today's tokens at the card's prices (it is that sum: a replay of the
// live feed matched it to $0.0001), then the running requests' parts are added. A stored figure the tokens
// do not explain (a card changed in the day) keeps its own total in their proportions.
export const BAR_KINDS = ['cached', 'input', 'output'];

// daySplit(tick, pricing, parts) -> {cached, input, output} dollars | null; parts: liveParts now
export function daySplit(tick, pricing, parts) {
  if (!parts) return null;
  const d = (tick && tick.today) || {};
  const p = pricing || {};
  const cached = d.cached || 0;
  const byTokens = {
    cached: per(cached, p.cached_per_m),
    input: per(Math.max(0, (d.prompt || 0) - cached), p.input_per_m),
    output: per(d.completion || 0, p.output_per_m),
  };
  const sum = BAR_KINDS.reduce((n, k) => n + byTokens[k], 0);
  const scale = sum > 0 ? parts.stored / sum : 0;
  return Object.fromEntries(BAR_KINDS.map((k) => [k, byTokens[k] * scale + parts[k]]));
}

// What the turning digits are paying off: each rise of the target joins a queue by kind (cache reads, new
// prompt tokens, output, and a finished request's record above what was shown: settled), and the shown
// figure pays the queue oldest first as it rolls. A big cache read keeps its colour until it has rolled in,
// and the decode added behind it shows after. A target that falls (a stream ended before its record came)
// drops what the figure no longer owes, newest first.
const OWED_KINDS = ['cached', 'input', 'output'];
const EPS = 1e-6;  // units: below this an entry is paid

const trimTo = (owed, gap) => {
  const out = [];
  let left = gap;
  for (const [k, n] of owed) {
    if (left <= EPS) break;
    out.push([k, Math.min(n, left)]);
    left -= n;
  }
  return out;
};

const pay = (owed, amount) => {
  const paid = {};
  const out = [];
  let left = amount;
  for (const [k, n] of owed) {
    const take = Math.min(n, Math.max(0, left));
    if (take > 0) paid[k] = (paid[k] || 0) + take;
    left -= take;
    if (n - take > EPS) out.push([k, n - take]);
  }
  return { owed: out, paid };
};

// owedStep(prev, parts, from, to) -> {parts, owed: [[kind, units]], kind | null}; parts: liveParts now;
// from, to: the shown figure in units before and after this frame's chase. kind: the kind this frame paid
// the most of, null when the figure did not move. A stored figure below the last one is a new day.
export function owedStep(prev, parts, from, to) {
  if (!parts || from === null || to === null) return { parts, owed: [], kind: null };
  if (!prev || parts.stored < prev.parts.stored - 1e-9) return { parts, owed: [], kind: null };
  const owed = prev.owed.map((e) => [...e]);
  const push = (k, n) => {
    const last = owed[owed.length - 1];
    if (last && last[0] === k) last[1] += n;
    else owed.push([k, n]);
  };
  for (const k of OWED_KINDS) {
    const n = (parts[k] - prev.parts[k]) * UNIT;
    if (n > EPS) push(k, n);
  }
  const target = (parts.stored + parts.cached + parts.input + parts.output) * UNIT;
  const gap = Math.max(0, target - from);
  const total = owed.reduce((n, e) => n + e[1], 0);
  if (gap - total > EPS) push('settled', gap - total);
  const paid = pay(total > gap ? trimTo(owed, gap) : owed, Math.max(0, to - from));
  const kinds = Object.keys(paid.paid);
  const kind = kinds.length ? kinds.reduce((a, b) => (paid.paid[b] > paid.paid[a] ? b : a)) : null;
  return { parts, owed: paid.owed, kind };
}

// Each digit that turns shows the colour of what it pays off (owedStep), and keeps it TINT_HOLD_S after it
// stops, so a digit that turns once still shows. tintStep(prev, values, kind, now) -> {values, kinds, at}:
// values are the digits' positions (wheels) or digit characters (no '$', '.' or ','), lined up from the
// right, so a figure that gains a digit keeps its colours; now in seconds. The first call has nothing to
// compare: no colour.
export const TINT_HOLD_S = 0.4;

export function tintStep(prev, values, kind, now, hold = TINT_HOLD_S) {
  const off = prev ? prev.values.length - values.length : 0;
  const kinds = [];
  const at = [];
  values.forEach((v, i) => {
    const j = i + off;
    const had = Boolean(prev) && j >= 0 && j < prev.values.length;
    if (had && prev.values[j] === v) {
      const kept = prev.at[j] !== null && now - prev.at[j] < hold;
      kinds.push(kept ? prev.kinds[j] : null);
      at.push(kept ? prev.at[j] : null);
    } else {
      const lit = Boolean(prev) && Boolean(kind);
      kinds.push(lit ? kind : null);
      at.push(lit ? now : null);
    }
  });
  return { values, kinds, at };
}

const goalOf = (target) => Math.floor(target * UNIT + 1e-6);

// chase(prev, target, stored, dt, tau, since) -> { units, stored, since } | null: the figure to show after dt seconds,
// in units, moving toward the target so a tick's step or a finished request's prompt cost rolls in. It
// never moves back while the day lasts (a stream that ended before its stored cost arrived holds it); a
// stored figure below the last one, or another day start (today.since), is a new day, so it starts again
// from there: a day with no stored cost yet would otherwise keep showing yesterday's. tau 0 goes straight there.
// A target that falls below the figure mid-roll (a stream ended before its record, or a tick counted fewer
// tokens than the estimate) still lets it finish the digit it turns: it rests only on a whole unit, never
// with a wheel stopped between two digits (a replay of the live feed found 105 such stops of 1 to 14 s).
export function chase(prev, target, stored, dt, tau = TAU_S, since = null) {
  if (target === null || target === undefined) return null;
  const day = since ?? null;
  if (!prev || stored < prev.stored - 1e-9 || day !== prev.since) return { units: goalOf(target), stored, since: day };
  const goal = Math.max(goalOf(target), Math.ceil(prev.units));
  if (goal <= prev.units) return { units: prev.units, stored, since: day };
  const gap = goal - prev.units;
  const step = tau > 0 ? Math.max(gap * (1 - Math.exp(-Math.max(0, dt) / tau)), MIN_RATE * dt) : gap;
  return { units: Math.min(goal, prev.units + step), stored, since: day };
}

export const settled = (shown, target) => !shown || target === null || target === undefined
  || (shown.units >= goalOf(target) && Number.isInteger(shown.units));

// wheels(units) -> roll positions, most significant first: wheel i shows digit floor(r) and is r - floor(r)
// of the way to the next. The last wheel turns with the figure; every other one turns only while the
// wheels right of it pass from all 9s to all 0s, as on a mechanical counter. At least one dollar wheel.
export function wheels(units) {
  const n = Math.max(1, String(Math.floor(units / UNIT)).length) + DECIMALS;
  const out = [];
  for (let p = n - 1; p >= 0; p -= 1) {
    const pow = 10 ** p;
    const base = Math.floor(units / pow) % 10;
    out.push(p === 0 ? units % 10 : base + Math.max(0, (units % pow) - (pow - 1)));
  }
  return out;
}

// splitUsd(units) -> ['$2.71', '23']: dollars and cents, then the two finer digits (drawn grey).
export function splitUsd(units) {
  if (units === null || units === undefined || Number.isNaN(units)) return [DASH, ''];
  const u = Math.floor(units);
  const dollars = String(Math.floor(u / UNIT)).replace(/\B(?=(\d{3})+(?!\d))/g, ',');
  const frac = String(u % UNIT).padStart(DECIMALS, '0');
  return [`$${dollars}.${frac.slice(0, 2)}`, frac.slice(2)];
}
