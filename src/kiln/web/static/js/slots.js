// The hero's stream rows: one per engine slot. A stream keeps its row while it runs, so when another ends
// nothing below it moves up; a new stream takes the first free row.

// placeStreams(placed, streams, rows) -> { placed, waiting }: placed is a new Map, stream id -> row index
// (rows 0..rows-1); waiting lists the streams that found no free row (they wait in the engine's queue),
// in the order given.
export function placeStreams(placed, streams, rows) {
  const ids = new Set(streams.map((s) => s.id));
  const next = new Map([...placed].filter(([id, i]) => ids.has(id) && i < rows));
  const used = new Set(next.values());
  const free = [];
  for (let i = 0; i < rows; i++) if (!used.has(i)) free.push(i);
  const waiting = [];
  for (const s of streams) {
    if (next.has(s.id)) continue;
    if (free.length) next.set(s.id, free.shift());
    else waiting.push(s);
  }
  return { placed: next, waiting };
}
