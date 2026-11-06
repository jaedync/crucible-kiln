// Request columns on the live tokens/second chart. When a request completes, its average decode rate
// (completion tokens over its decode window: first to last token, or the engine's decode time) is drawn
// as a column across that window, behind the per-second line. The column's area is the tokens the
// request added to the chart, so with one request at a time the line's average runs along its top.
// A column shows only once its request ends, so it appears over seconds already drawn; a later row of the
// same request (the engine's line joined) keeps the first column. Columns stack (stackBands), widest at the
// bottom, so the stack's top is the requests' summed rate; a wider column that ends later slides under the
// ones drawn and lifts them.
export const MIN_WINDOW_S = 1;  // a shorter decode is one or two of the chart's seconds already

// columnOf(request row) -> { id, from, to, avg } or null when it has no decode to show
export function columnOf(r) {
  if (!r || r.decode_from == null || r.decode_to == null || !(r.completion > 0)) return null;
  const span = r.decode_to - r.decode_from;
  if (!(span >= MIN_WINDOW_S)) return null;
  return { id: r.id, from: r.decode_from, to: r.decode_to, avg: r.completion / span };
}

// keepColumns(columns, rows, since) -> a new Map by request id: the rows' new columns added, a drawn
// column kept as it is, and every column that ended before `since` gone.
export function keepColumns(cols, rows, since) {
  const next = new Map([...cols].filter(([, c]) => c.to >= since));
  for (const r of rows) {
    const c = columnOf(r);
    if (c && c.to >= since && !next.has(c.id)) next.set(c.id, c);
  }
  return next;
}

// columnAt(columns, t) -> the column over time t (the one that started last), or null
export function columnAt(cols, t) {
  let best = null;
  for (const c of cols.values()) if (c.from <= t && t < c.to && (!best || c.from > best.from)) best = c;
  return best;
}

// stackBands(columns) -> Map id -> [{ from, to, base }]: the rate each column stands on, piece by piece.
// The widest goes down first (ties: the earlier start). Between two column edges the set of running requests
// does not change, and each one stands on the sum of the wider ones running then. So the top of the stack is
// the summed rate of the running requests at every moment, with no overhang; a band steps where a wider one
// under it starts or ends. Kept per columns map: keepColumns makes a new one on each change.
const bands = new WeakMap();

export function stackBands(cols) {
  let out = bands.get(cols);
  if (out) return out;
  const order = [...cols.values()].sort((a, b) => (b.to - b.from) - (a.to - a.from) || a.from - b.from
    || (a.id < b.id ? -1 : 1));
  const edges = [...new Set(order.flatMap((c) => [c.from, c.to]))].sort((a, b) => a - b);
  out = new Map(order.map((c) => [c.id, []]));
  for (let k = 0; k + 1 < edges.length; k++) {
    const from = edges[k], to = edges[k + 1];
    let base = 0;
    for (const c of order) {
      if (!(c.from <= from && to <= c.to)) continue;
      const pieces = out.get(c.id);
      const last = pieces[pieces.length - 1];
      if (last && last.to === from && last.base === base) pieces[pieces.length - 1] = { ...last, to };
      else pieces.push({ from, to, base });
      base += c.avg;
    }
  }
  bands.set(cols, out);
  return out;
}

// stackTop(columns) -> the highest stacked top, for the y axis
export function stackTop(cols) {
  const all = stackBands(cols);
  let top = 0;
  for (const c of cols.values()) for (const b of all.get(c.id)) top = Math.max(top, b.base + c.avg);
  return top;
}

// latestColumn(columns) -> the column that ended last, or null
export function latestColumn(cols) {
  let best = null;
  for (const c of cols.values()) if (!best || c.to > best.to) best = c;
  return best;
}
