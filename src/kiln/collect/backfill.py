"""Recompute the prefill part of series_1s for a time range from the stored requests.

The spreading rules change (spread.SERIES_VERSION), and only the next kiln-dash start rebuilds the
whole history: until then the seconds already drawn with the old rules keep their old prefill. This
recomputes prefill from the rows alone (each request's engine chunk reports are stored on its row) for
a range, and reports every second that would change. Decode is left untouched: it comes from the
gateway's arrival times and the engine's decode time, which these rules do not change.

A request's prefill can start before the range and end inside it, so a row counts when its prefill
window touches the range; its whole prefill is then recomputed, but only seconds inside the range are
compared and written. The engine's other work nearby is read too (their chunk windows are what the
overlap rule trims), which is why a wider band of rows is fetched than the range itself.
"""
from __future__ import annotations

import sqlite3
from collections import defaultdict
from dataclasses import dataclass

from kiln.collect import spread
from kiln.config import DEFAULT_MAX_PREFILL_TPS

# Rows this far outside the range are read for their chunk windows (the overlap rule), never written.
BAND_S = 1800.0


@dataclass(frozen=True)
class Change:
    """One series_1s second of one request group: its prefill now, against what it holds."""
    t: int
    host: str
    profile: str
    model: str
    client: str
    was: int
    now: int


Key = tuple[int, str, str, str, str]


def plan(conn: sqlite3.Connection, since: float, until: float,
         max_tps: float = DEFAULT_MAX_PREFILL_TPS) -> list[Change]:
    """Every prefill change the current rules would make in the range's whole seconds."""
    t0, t1 = int(since), int(until)
    rows = conn.execute("SELECT * FROM requests WHERE source IN ('gateway', 'direct', 'agent')"
                        " AND ts_end >= ? AND ts_start <= ?", (t0 - BAND_S, t1 + 1 + BAND_S)).fetchall()
    engines = spread.engine_chunks(rows)
    want: dict[Key, int] = defaultdict(int)
    keys: set[tuple] = set()
    for r in rows:
        key = (r["host"], spread.series_profile(r), r["model"] or "?", r["client_key"])
        keys.add(key)
        blocks = (engines.get((r["host"], r["container"]), spread.Blocks()) if r["container"]
                  else spread.Blocks())
        for sec, n in spread.prefill_pairs(r, max_tps, blocks):
            if t0 <= sec <= t1:
                want[(sec, *key)] += n
    stored: dict[Key, int] = {}
    for key in keys:
        for t, n in conn.execute("SELECT t, prefill FROM series_1s WHERE t BETWEEN ? AND ? AND host = ?"
                                 " AND profile = ? AND model = ? AND client_key = ?", (t0, t1, *key)):
            if n:
                stored[(t, *key)] = n
    out = [Change(t, *key, was=stored.get((t, *key), 0), now=n)
           for (t, *key), n in want.items() if n != stored.get((t, *key), 0)]
    out += [Change(t, *key, was=n, now=0) for (t, *key), n in stored.items() if (t, *key) not in want]
    return sorted(out, key=lambda c: (c.t, c.host, c.profile, c.model, c.client))


def apply(conn: sqlite3.Connection, changes: list[Change]) -> None:
    """Write the changes: only the prefill column, and a row left with no tokens at all goes."""
    where = "t = ? AND host = ? AND profile = ? AND model = ? AND client_key = ?"
    for c in changes:
        args = (c.t, c.host, c.profile, c.model, c.client)
        if c.now:
            conn.execute("INSERT INTO series_1s (t, host, profile, model, client_key, prefill)"
                         " VALUES (?, ?, ?, ?, ?, ?) ON CONFLICT (t, host, profile, model, client_key)"
                         " DO UPDATE SET prefill = excluded.prefill", (*args, c.now))
        else:
            conn.execute(f"UPDATE series_1s SET prefill = 0 WHERE {where}", args)
            conn.execute(f"DELETE FROM series_1s WHERE {where} AND decode = 0 AND prefill = 0", args)
