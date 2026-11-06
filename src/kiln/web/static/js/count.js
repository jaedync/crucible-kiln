// Token counts that stream between ticks. A tick (twice a second) carries each decoding stream's count
// and rate; between ticks the count moves on at that rate, so it climbs a few tokens at a time instead
// of jumping by a tick's worth (25-30 at 55 tok/s). It runs at most CAP_S past the tick, so a stream
// that stalls stops counting. It never moves back (a tick that lands short holds it), and when a tick
// lands ahead it catches up at up to CATCH_UP times the stream's rate instead of jumping.
export const CAP_S = 0.75;
export const CATCH_UP = 2;

// streamCount(stream, tickTs, now, held) -> { count, value, at }: the whole count to show, the exact one
// behind it, and when; `held` is the last call's result for this stream (null at first)
export function streamCount(s, tickTs, now, held = null) {
  const run = Math.min(CAP_S, Math.max(0, now - tickTs));
  const est = (s.tokens || 0) + (s.tps > 0 ? s.tps * run : 0);
  let value = est;
  if (held && est <= held.value) value = held.value;
  else if (held && s.tps > 0) value = Math.min(est, held.value + CATCH_UP * s.tps * Math.max(0, now - held.at));
  return { count: Math.floor(value + 1e-3), value, at: now };  // epoch-second sums lose a hair
}
