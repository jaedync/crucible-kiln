// Pure helpers for the live 1-second series. The buffer is contiguous: index i
// holds second t0 + i, and seconds the browser never received are null (a gap),
// never 0, because "no data" is not "no tokens".

export const LIVE_WINDOW_S = 900;

export function emptyBuffer() {
  return Object.freeze({ t: [], decode: [], prefill: [] });
}

// Builds a contiguous buffer from {t, decode, prefill} arrays (snapshot.series_1s).
export function fromSeries(src) {
  let buf = emptyBuffer();
  if (!src || !src.t) return buf;
  for (let i = 0; i < src.t.length; i++) {
    buf = putSecond(buf, { t: src.t[i], decode: src.decode[i], prefill: src.prefill[i] });
  }
  return buf;
}

// Appends one second, filling any gap with nulls and trimming to the window. A second the buffer already
// has is drawn and final (the server sends each once), so a repeat changes nothing.
export function putSecond(buf, sec) {
  const t = Math.floor(sec.t);
  const n = buf.t.length;
  if (!n) return trim({ t: [t], decode: [sec.decode], prefill: [sec.prefill] });
  const last = buf.t[n - 1];
  if (t <= last) return buf;
  const gap = Math.min(t - last - 1, LIVE_WINDOW_S);
  const ts = buf.t.slice(), dec = buf.decode.slice(), pre = buf.prefill.slice();
  for (let k = gap; k >= 1; k--) {
    ts.push(t - k); dec.push(null); pre.push(null);
  }
  ts.push(t); dec.push(sec.decode); pre.push(sec.prefill);
  return trim({ t: ts, decode: dec, prefill: pre });
}

function trim(buf) {
  const n = buf.t.length;
  const drop = Math.max(0, n - LIVE_WINDOW_S);
  if (!drop) return Object.freeze(buf);
  return Object.freeze({ t: buf.t.slice(drop), decode: buf.decode.slice(drop), prefill: buf.prefill.slice(drop) });
}

// Sums every group in a /api/v1/series response and converts tokens per bucket to tokens/s.
export function fromRangeResponse(res) {
  const step = res.step || 1;
  const n = res.t.length;
  const dec = new Array(n).fill(0), pre = new Array(n).fill(0);
  for (const s of Object.values(res.series || {})) {
    for (let i = 0; i < n; i++) {
      dec[i] += s.decode[i] || 0;
      pre[i] += s.prefill[i] || 0;
    }
  }
  return { t: res.t, decode: dec.map((v) => v / step), prefill: pre.map((v) => v / step), step };
}

// Top of a y axis: the next step of 1, 1.2, 1.5, 2, 2.5, 3, 4, 5, 6, 8 or 10 times a power of ten
// above max plus a little headroom, so a 105 tok/s peak gets 120, not 200.
const NICE = [1, 1.2, 1.5, 2, 2.5, 3, 4, 5, 6, 8, 10];

export function niceMax(max, floor) {
  const m = Math.max(floor, (max || 0) * 1.06);
  const mag = 10 ** Math.floor(Math.log10(m));
  return NICE.find((s) => s * mag >= m - 1e-9) * mag;
}

// holdMax(held, max, floor) -> the live chart's y axis top: it grows at once for a new peak and never
// shrinks while the view stays open, so the seconds already drawn keep their height when a peak scrolls out.
export function holdMax(held, max, floor) {
  return Math.max(held || 0, niceMax(max, floor));
}

// smoothRate(t, values, sigma) -> for each second, the Gaussian-weighted mean of the seconds within 3 sigma
// of it: a trend line with no corners. Gaps (null) are left out: "no data" is not "no tokens"; a second with
// no data in reach is null. The weights renormalise at the edges, so the newest seconds use only the past
// and move a little as the next seconds arrive. The buffer is contiguous, so index steps are seconds.
export function smoothRate(t, values, sigma) {
  const reach = Math.ceil(3 * sigma);
  const w = Array.from({ length: reach + 1 }, (_, d) => Math.exp(-(d * d) / (2 * sigma * sigma)));
  return t.map((_, i) => {
    let sum = 0, weight = 0;
    for (let j = Math.max(0, i - reach); j <= Math.min(t.length - 1, i + reach); j++) {
      if (values[j] == null) continue;
      sum += w[Math.abs(i - j)] * values[j];
      weight += w[Math.abs(i - j)];
    }
    return weight ? sum / weight : null;
  });
}

// Index of the newest non-null value, or -1: what a chart legend shows while nothing is hovered.
export function lastIndex(values) {
  for (let i = values.length - 1; i >= 0; i--) if (values[i] != null) return i;
  return -1;
}
