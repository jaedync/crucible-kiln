"""Crucible server signals per local day and engine (GET /api/v1/signals).

Both signals are counts. `errors` are crucible_error lines of the server's own failures (HTTP 500 and
above, and failures after the stream began); `stream_errors` is the part of them that ended a stream after
HTTP 200. `client_errors` counts the lines of refused requests (HTTP 400-499 before the stream, for example
a path the server does not have); they are the caller's problem and are not in `errors`. `tool_call_fallbacks` counts
requests whose model output was not a valid tool call and returned as content. The request flag
(usage.tool_call_fallback) and the matching log line describe the same event, so a day's figure is the
larger of the two: a request that bypassed the gateway has only the line.
"""
from __future__ import annotations

import time

from kiln.store.queries import COUNTED, host_filter

MAX_DAYS = 90


_ENGINE = "COALESCE(NULLIF(engine, ''), 'unknown')"
_DAY = "date({}, 'unixepoch', 'localtime')"


def view(conn, days: int, now: float, host: str | None = None) -> dict:
    """Counted in SQL: a Python row for each of 14 days of requests cost the dashboard 0.3 s a call."""
    days = max(1, min(int(days), MAX_DAYS))
    t = time.localtime(now)
    since = time.mktime((t.tm_year, t.tm_mon, t.tm_mday - (days - 1), 0, 0, 0, 0, 0, -1))
    acc: dict[tuple[str, str], dict] = {}

    def bucket(day: str, engine: str) -> dict:
        return acc.setdefault((day, engine), {"date": day, "engine": engine, "requests": 0, "errors": 0,
                                              "stream_errors": 0, "client_errors": 0, "tool_call_fallbacks": 0,
                                              "_fallback_lines": 0})

    cond, args = host_filter(host)
    for r in conn.execute(f"SELECT {_DAY.format('ts_start')} AS d, {_ENGINE} AS e, COUNT(*) AS n FROM requests r"
                          f" WHERE {COUNTED}{cond} AND ts_start >= ? GROUP BY d, e", (*args, since)):
        bucket(r["d"], r["e"])["requests"] += r["n"]
    # The server logs the fallback as the response ends: the same day as its line, even near midnight.
    for r in conn.execute(f"SELECT {_DAY.format('COALESCE(ts_end, ts_start)')} AS d, {_ENGINE} AS e, COUNT(*) AS n"
                          f" FROM requests r WHERE {COUNTED}{cond} AND ts_start >= ? AND tool_call_fallback"
                          " GROUP BY d, e", (*args, since)):
        bucket(r["d"], r["e"])["tool_call_fallbacks"] += r["n"]
    cond, args = host_filter(host, "host")
    for r in conn.execute(f"SELECT {_DAY.format('ts')} AS d, {_ENGINE} AS e, kind, streamed, COUNT(*) AS n"
                          f" FROM crucible_errors WHERE ts >= ?{cond} GROUP BY d, e, kind, streamed",
                          (since, *args)):
        b = bucket(r["d"], r["e"])
        if r["kind"] == "tool_call_fallback":
            b["_fallback_lines"] += r["n"]
        elif r["kind"] == "client_error":
            b["client_errors"] += r["n"]
        else:
            b["errors"] += r["n"]
            b["stream_errors"] += r["n"] if r["streamed"] else 0
    for b in acc.values():
        b["tool_call_fallbacks"] = max(b["tool_call_fallbacks"], b.pop("_fallback_lines"))
    rows = sorted(acc.values(), key=lambda b: (b["date"], b["engine"]), reverse=True)
    return {"days": days, "host": host or "all", "rows": rows}
