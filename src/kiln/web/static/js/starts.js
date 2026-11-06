// Request starts on the live tokens/second chart: a thin line at the second each request started.

// keepStarts(starts, items, since) -> a new Map, request id -> start time. Items are the snapshot's starts
// ({id, t}) or running streams ({id, started}): a stream shows its line as soon as it starts, and the same
// request keeps one line. Starts before `since` go.
export function keepStarts(starts, items, since) {
  const next = new Map([...starts].filter(([, t]) => t >= since));
  for (const it of items) {
    const t = it ? (it.t ?? it.started) : null;
    if (it && it.id && t != null && t >= since && !next.has(it.id)) next.set(it.id, t);
  }
  return next;
}

// The chart's right edge is the start of its newest whole second, so a request that started after it is
// past the edge for a second or two. NEW_START_S: how far past the edge a start may be and still show.
export const NEW_START_S = 3;

// startLineAt(t, min, max) -> the time to draw a start line at on an x scale from min to max, or null: a
// start in the newest seconds waits at the right edge, so its line shows the moment the request starts.
export function startLineAt(t, min, max) {
  if (t < min || t > max + NEW_START_S) return null;
  return Math.min(t, max);
}
