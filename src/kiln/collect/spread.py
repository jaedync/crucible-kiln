"""Where a request's tokens go in the per-second history (series_1s).

Every series_1s row is derived from stored requests by the functions here, so
the history can be rebuilt whenever these rules change (ensure_current).

Decode: engines don't always stream tokens as they generate them. Halogen
holds a tool call's arguments back until the call is complete and sometimes
flushes buffered output in a burst, so arrival seconds can show hundreds of
tokens per second next to seconds of silence. When the engine reports its
decode time, such a request's tokens are spread evenly over that window
instead. A stream that carried nearly one event per token with no burst is a
true record of when tokens were made, so it keeps its arrival shape (scaled to
the exact count): spreading it would turn a live, uneven line into a flat block
the moment the engine line joins. Until the engine line joins (or if it never
does), a stream that is not faithful is spread over its own first-to-last token
window from the gateway: its arrival seconds would put a held-back tool call's
whole output in one second, hundreds of tok/s.

Prefill: the engine computes it one chunk at a time, so a request's tokens
belong in the seconds its chunks ran. Crucible reports a line per 4,096
computed rows and Halogen per 32,768, with the seconds since that request's
prefill began (ingest keeps them on the row, in prefill_progress). A request
without reports keeps the even spread over its prefill window. Requests still
overlap in time: Crucible pauses a long prefill to run a short one first, and
the long one's next chunk reports the seconds the pause lasted, so its window
covers the short one's. chunk_pairs gives such time to the chunk that ended
earlier, because an engine cannot prefill two chunks at once. A request whose
engine figures put its prefill above any real engine rate has no chart entry at
all (config.DEFAULT_MAX_PREFILL_TPS explains why).
"""
from __future__ import annotations

import json
import sqlite3
from bisect import bisect_left, bisect_right
from collections import defaultdict
from dataclasses import dataclass

from kiln.config import DEFAULT_MAX_PREFILL_TPS
from kiln.live.apportion import apportion

Pairs = list[tuple[int, int]]

# Bump when these rules change: the next kiln-dash start rebuilds series_1s from requests.
SERIES_VERSION = "5"
VERSION_KEY = "series_version"  # ingest_offsets row holding the rules series_1s was built with


def _share(total: int, weights) -> list[int]:
    """Integers in proportion to weights that sum exactly to total (largest remainder).

    Rounding a running total, not each share alone: per-share errors add up, and the last share used
    to absorb all of them (hundreds of tokens over a long request).
    """
    span = sum(weights)
    if total <= 0 or not weights or span <= 0:
        return [0] * len(weights)
    raw = [w * total / span for w in weights]
    out = [int(v) for v in raw]
    order = sorted(range(len(weights)), key=lambda i: (raw[i] - out[i], -i), reverse=True)
    for i in order[:total - sum(out)]:
        out[i] += 1
    return out


def scale_counts(pairs: Pairs, total: int) -> Pairs:
    """Integer counts proportional to pairs' counts that sum exactly to total (largest remainder)."""
    counts = _share(total, [n for _, n in pairs])
    out: dict[int, int] = defaultdict(int)
    for (sec, _), n in zip(pairs, counts):
        out[sec] += n
    return [(sec, n) for sec, n in sorted(out.items()) if n]


def _tokens_by_s(r) -> list | None:
    try:
        hist = json.loads(r["raw"] or "{}").get("tokens_by_s")
    except (ValueError, AttributeError):
        return None
    return hist if isinstance(hist, list) and len(hist) == 2 and isinstance(hist[1], list) else None


def streamed(r) -> bool:
    return _tokens_by_s(r) is not None


def arrival_pairs(r) -> Pairs:
    """Token-bearing stream events by arrival second, as the gateway saw them."""
    hist = _tokens_by_s(r)
    if hist is None:
        return []
    return [(hist[0] + i, n) for i, n in enumerate(hist[1]) if isinstance(n, int) and n > 0]


# A stream is faithful when it has at least this many events per token...
FAITHFUL_EVENTS = 0.9
# ...and no arrival second above this multiple of the request's mean decode rate (a flushed burst).
BURST_FACTOR = 3.0


def faithful(events: Pairs, tokens: int, decode_s: float | None) -> bool:
    """Whether arrival seconds show when the tokens were generated, not just when they were sent."""
    n = sum(c for _, c in events)
    if not events or not tokens or n < FAITHFUL_EVENTS * tokens:
        return False
    if not decode_s:
        return True
    mean = tokens / max(decode_s, 1.0)
    return max(c for _, c in events) * tokens / n <= BURST_FACTOR * mean


def decode_window(r) -> tuple[float | None, float | None]:
    """(start, end) of a request's decode: the engine's decode time ending at its last token, or without
    the engine's line the gateway's first to last token. (None, None) without completion tokens."""
    if not r["completion"]:
        return None, None
    end = r["ts_last_token"] or r["ts_end"]
    if r["engine_decode_s"] and end is not None:
        return max(r["ts_start"], end - r["engine_decode_s"]), end
    first, last = r["ts_first_token"], r["ts_last_token"]
    return (first, last) if first is not None and last is not None and last > first else (None, None)


def decode_pairs(r) -> Pairs:
    """A request's decode tokens by second (a requests row: gateway or direct)."""
    events = arrival_pairs(r)
    tokens = r["completion"] if r["completion"] is not None else sum(n for _, n in events)
    if not tokens:
        return []
    end = r["ts_last_token"] or r["ts_end"]
    if r["engine_decode_s"] and end is not None and not faithful(events, tokens, r["engine_decode_s"]):
        return apportion(max(r["ts_start"], end - r["engine_decode_s"]), end, tokens)
    if events:
        first = r["ts_first_token"]
        window = end - first if first is not None and end is not None else None
        if window and not faithful(events, tokens, window):
            return apportion(first, end, tokens)  # held back or flushed: arrival time is not generation time
        return events if sum(n for _, n in events) == tokens else scale_counts(events, tokens)
    first = r["ts_first_token"] if r["ts_first_token"] is not None else r["ts_end"]
    return apportion(first, r["ts_end"], tokens)


def prefill_span(r) -> tuple[int, float | None, float, float]:
    """A request's prefill as (tokens the engine computed, its prefill seconds or None, start, end).

    `end` is the first token's time (or, for a request that never streamed, the engine's decode end).
    The window start is the request's own start when the engine gave no prefill time, or when the wait
    for the first token was longer than the engine's prefill time (queueing, a cache restore).
    """
    first = r["ts_first_token"]
    if first is None and r["engine_decode_s"] is not None:
        first = r["ts_end"] - r["engine_decode_s"]  # not streamed: the engine says when decoding began
    first = first if first is not None else r["ts_end"]
    if first is None:
        return 0, None, 0.0, 0.0
    if r["engine_prefill_tokens"] is not None:
        tokens, secs = r["engine_prefill_tokens"], r["engine_prefill_s"]
    elif r["prompt"] is not None:
        tokens, secs = r["prompt"] - (r["cached"] or 0), None
    else:
        tokens, secs = 0, None
    if r["source"] == "direct":
        start = first - (secs or 0.0)
    else:
        start = max(r["ts_start"], first - secs) if secs is not None else r["ts_start"]
    return max(0, tokens or 0), secs, start, first


def prefill_rate(r) -> float | None:
    """The request's prefill tok/s from the figures the history spreads, or None when it has neither."""
    tokens, secs, start, end = prefill_span(r)
    span = secs if secs else end - start
    return tokens / span if tokens and span and span > 0 else None


def impossible_prefill(r, max_tps: float = DEFAULT_MAX_PREFILL_TPS) -> bool:
    """Whether the engine's figures put this request's prefill above any real engine rate.

    The request stays in the table; config.DEFAULT_MAX_PREFILL_TPS says why it is left out of the chart.
    """
    rate = prefill_rate(r)
    return rate is not None and rate > max_tps


@dataclass(frozen=True)
class Chunk:
    """One stretch of prefill work: the tokens the engine computed between `start` and `end`.

    A chunk passed in as another request's block (chunk_pairs) uses only its interval: an engine
    prefills one chunk at a time, so nothing else can run in those seconds.
    """
    start: float
    end: float
    tokens: int


def _progress_col(r) -> object:
    try:
        return r["prefill_progress"]
    except (IndexError, KeyError):  # a row read before the column existed, or a dict without it
        return None


def stored_chunks(r) -> list[Chunk]:
    """The engine's chunk windows for one request, from its own prefill_progress column.

    Each entry is [when the chunk ended, how many seconds it took, the tokens computed in it], which is
    all a rebuild needs. A column that is not a list of such triples is left out (never the request).
    """
    raw = _progress_col(r)
    if isinstance(raw, str):
        try:
            raw = json.loads(raw)
        except ValueError:
            return []
    if not isinstance(raw, list):
        return []
    out = []
    for e in raw:
        if not isinstance(e, list) or len(e) != 3 or not all(isinstance(v, (int, float)) for v in e):
            continue
        end, secs, tokens = (float(e[0]), float(e[1]), int(e[2]))
        if 0 < tokens and secs >= 0:
            out.append(Chunk(end - secs, end, tokens))
    return out


def chunks_from_reports(reports, after: int | None, total: int | None = None) -> list[Chunk]:
    """Chunk windows from an engine's progress reports: (log time, elapsed seconds, tokens done).

    A chunk runs from the previous report to its own, and the first one from its elapsed seconds before
    it (both are wall clock: the test machine's reports land within 20-40 ms of elapsed time after the prefill
    began). `after` is what the prompt's cache held when the prefill began, because a report's `done`
    counts that prefix; without it the first chunk cannot be told from the cache, so only the intervals
    are usable (a trim block). The chunk tokens are scaled to `total`, the tokens the engine says it
    computed, when both are known.
    """
    reports = [r for r in reports if isinstance(r[0], (int, float)) and isinstance(r[2], (int, float))]
    if not reports:
        return []
    if after is None:
        after = max(0, int(reports[0][2]) - total) if total else 0
    out, end_prev, done_prev = [], None, int(after)
    for ts, elapsed, done in reports:
        begin = ts - float(elapsed or 0.0) if end_prev is None else end_prev
        if done > done_prev:
            out.append(Chunk(min(begin, ts), ts, int(done) - done_prev))
        done_prev, end_prev = max(done_prev, int(done)), ts
    if total and total > 0 and out:
        out = [Chunk(c.start, c.end, n) for c, n in zip(out, _share(total, [c.tokens for c in out])) if n]
    return out


def prefill_chunks(r, max_tps: float = DEFAULT_MAX_PREFILL_TPS) -> list[Chunk]:
    """A finished request's prefill work: one window per reported chunk, else its whole prefill window.

    No windows at all for a request the engine never prefilled (a fully cached prompt) or one whose
    figures are impossible (impossible_prefill).
    """
    tokens, _secs, start, end = prefill_span(r)
    if not tokens or impossible_prefill(r, max_tps):
        return []
    chunks = stored_chunks(r)
    if chunks:
        return [Chunk(max(c.start, r["ts_start"]), c.end, c.tokens) for c in chunks]
    return [Chunk(start, end, tokens)]  # no reports: the even spread over the prefill window


@dataclass(frozen=True)
class Blocks:
    """One engine's prefill intervals, sorted by their end, with those ends (block_index).

    chunk_pairs asks them for the chunks that ended inside its own window: only those can have taken
    seconds from it, so the search must not walk a whole day of that engine's chunks.
    """
    spans: tuple[tuple[float, float], ...] = ()
    ends: tuple[float, ...] = ()


def block_index(chunks) -> Blocks:
    """The intervals of `chunks` (Chunk, or anything with start and end), ready for chunk_pairs."""
    spans = sorted(((c.start, c.end) for c in chunks if c.end > c.start), key=lambda s: (s[1], s[0]))
    return Blocks(tuple(spans), tuple(s[1] for s in spans))


def _merge(blocks) -> list[tuple[float, float]]:
    out: list[tuple[float, float]] = []
    for start, end in sorted(blocks):
        if out and start <= out[-1][1]:
            out[-1] = (out[-1][0], max(out[-1][1], end))
        else:
            out.append((start, end))
    return out


def _subtract(start: float, end: float, blocks) -> list[tuple[float, float]]:
    """The parts of [start, end] that `blocks` (merged, non-overlapping) do not cover."""
    parts = [(start, end)]
    for b0, b1 in blocks:
        nxt = []
        for p0, p1 in parts:
            if b1 <= p0 or b0 >= p1:
                nxt.append((p0, p1))
                continue
            if b0 > p0:
                nxt.append((p0, b0))
            if b1 < p1:
                nxt.append((b1, p1))
        parts = nxt
        if not parts:
            break
    return parts


def _spread_chunk(tokens: int, parts) -> Pairs:
    """Tokens over the seconds of parts, in proportion to each part's length."""
    lengths = [b - a for a, b in parts]
    if not parts:
        return []
    if not any(n > 0 for n in lengths):  # a zero-length window: its tokens land in its one second
        parts, lengths = parts[:1], [1.0]
    out: list[tuple[int, int]] = []
    for (a, b), n in zip(parts, _share(tokens, lengths)):
        out += apportion(a, b, n)
    return out


def chunk_pairs(chunks, blocks: Blocks = Blocks()) -> Pairs:
    """Tokens by second for these work windows, which may belong to one request or to several.

    An engine prefills one chunk at a time, but two requests' windows still cover the same seconds: a
    request paused for another prefills nothing while it waits, while the chunk it returns to reports
    the seconds the pause lasted, so that window reaches back over the other request's chunks. Such
    time belongs to the chunk that ended earlier (the engine ran it then), so each window here is
    spread over its own interval less the intervals, from `blocks` (block_index of the engine's other
    chunks), of the chunks that ended before it. A window another chunk covers completely keeps its
    interval: its tokens still have to land.
    """
    out: dict[int, int] = defaultdict(int)
    for c in chunks:
        claimed = _merge(blocks.spans[bisect_right(blocks.ends, c.start):bisect_left(blocks.ends, c.end)])
        for sec, n in _spread_chunk(c.tokens, _subtract(c.start, c.end, claimed)):
            out[sec] += n
    return [(sec, n) for sec, n in sorted(out.items()) if n]


def prefill_pairs(r, max_tps: float = DEFAULT_MAX_PREFILL_TPS, blocks: Blocks = Blocks()) -> Pairs:
    """A finished request's computed prompt tokens by the second the engine ran each chunk."""
    return chunk_pairs(prefill_chunks(r, max_tps=max_tps), blocks)


def engine_chunks(rows) -> dict[tuple[str, str], Blocks]:
    """Every request's prefill windows by engine (host, container), for the overlap rule in chunk_pairs."""
    out: dict[tuple[str, str], list[Chunk]] = defaultdict(list)
    for r in rows:
        if r["container"] and r["ts_start"] is not None:
            out[(r["host"], r["container"])].extend(prefill_chunks(r))
    return {key: block_index(chunks) for key, chunks in out.items()}


def series_profile(r) -> str:
    if r["source"] == "direct":
        return r["profile"] or f"container:{r['container']}"
    return r["profile"] or "?"


def diff_pairs(old: Pairs, new: Pairs) -> Pairs:
    """Per-second change from old to new (negative where tokens moved away)."""
    d: dict[int, int] = defaultdict(int)
    for sec, n in new:
        d[sec] += n
    for sec, n in old:
        d[sec] -= n
    return sorted((sec, n) for sec, n in d.items() if n)


def add_series(conn: sqlite3.Connection, host: str, kind: str, pairs: Pairs, profile: str, model: str | None,
               client: str) -> None:
    col = "decode" if kind == "decode" else "prefill"
    for sec, n in pairs:
        conn.execute(
            f"INSERT INTO series_1s (t, host, profile, model, client_key, {col}) VALUES (?, ?, ?, ?, ?, ?)"
            f" ON CONFLICT (t, host, profile, model, client_key) DO UPDATE SET {col} = {col} + excluded.{col}",
            (sec, host, profile, model or "?", client, n))


def rebuild_series(conn: sqlite3.Connection, max_tps: float = DEFAULT_MAX_PREFILL_TPS) -> None:
    """Recompute series_1s from requests with the current rules (same result as ingesting afresh)."""
    conn.execute("DELETE FROM series_1s")
    rows = conn.execute("SELECT * FROM requests WHERE source IN ('gateway', 'direct', 'agent')").fetchall()
    # One engine at a time: its requests' chunk windows are what the overlap rule needs, and the whole
    # history's windows are never all in memory (a rebuild runs at every kiln-dash start after a rule
    # change, on tens of thousands of requests).
    by_engine: dict[tuple[str, str | None], list] = defaultdict(list)
    for r in rows:
        by_engine[(r["host"], r["container"])].append(r)
    for (host, container), group in by_engine.items():
        blocks = block_index(c for r in group for c in prefill_chunks(r, max_tps)) if container else Blocks()
        for r in group:
            done = r["source"] in ("direct", "agent") or r["series_done"]
            args = (series_profile(r), r["model"], r["client_key"])
            if done or streamed(r):  # streams count from the moment they're recorded; the rest when finished
                add_series(conn, host, "decode", decode_pairs(r), *args)
            if done:
                add_series(conn, host, "prefill", chunk_pairs(prefill_chunks(r, max_tps), blocks), *args)


def ensure_current(conn: sqlite3.Connection, max_tps: float = DEFAULT_MAX_PREFILL_TPS) -> bool:
    """Rebuild series_1s once if it was built with older rules. True when it rebuilt."""
    row = conn.execute("SELECT position FROM ingest_offsets WHERE source = ?", (VERSION_KEY,)).fetchone()
    if row and row[0] == SERIES_VERSION:
        return False
    conn.execute("BEGIN")
    try:
        rebuild_series(conn, max_tps)
        conn.execute("INSERT INTO ingest_offsets (source, position) VALUES (?, ?) ON CONFLICT (source)"
                     " DO UPDATE SET position = excluded.position", (VERSION_KEY, SERIES_VERSION))
        conn.execute("COMMIT")
    except Exception:
        conn.execute("ROLLBACK")
        raise
    return True
