"""Cache diagnostics views (GET /api/v1/cache): why a turn read its prompt again.

Every value comes from stored counts and booleans; no content exists to show.
A turn's `cause`:
- "new": the first request of a conversation (nothing to reuse from this client).
- "client": the client changed messages it had sent before, or its system prompt or tools,
  so any engine must read the changed part again.
- "engine": the client kept its prefix, yet the engine read more than REREAD_FLAG_TOKENS of it again.
- "ok": prefix kept and the engine reused it.
- "unknown": the previous turn's size is not known (it was still running, or the gateway restarted).
"""
from __future__ import annotations

from kiln.collect.ingest import REREAD_FLAG_TOKENS
from kiln.store.queries import LOCAL_COUNTED as COUNTED, _names

TURN_COLS = ("ts_start", "profile", "prompt", "cached", "completion", "ttft_s", "n_messages", "prefix_msgs_kept",
             "prev_n_messages", "system_changed", "tools_changed", "new_conversation", "appended_tokens",
             "reread_tokens", "cache_save_refused", "cache_session_label", "cache_entries", "cache_bytes",
             "cache_evictions", "host_free_mib", "replay_hit", "closest_shared_tokens", "closest_outcome")
MAX_SESSIONS = 30
MAX_TURNS = 300


def cause(r) -> str:
    if r["new_conversation"]:
        return "new"
    if r["prefix_msgs_kept"] is None:
        return "unknown"
    if (r["prev_n_messages"] is not None and r["prefix_msgs_kept"] < r["prev_n_messages"]) \
            or r["system_changed"] or r["tools_changed"]:
        return "client"
    if r["reread_tokens"] is None:
        return "unknown"
    return "engine" if r["reread_tokens"] > REREAD_FLAG_TOKENS else "ok"


def _turn(r) -> dict:
    t = {k: r[k] for k in TURN_COLS}
    for k in ("system_changed", "tools_changed", "new_conversation", "replay_hit"):
        t[k] = None if t[k] is None else bool(t[k])
    t["cause"] = cause(r)
    return t


# cause() in SQL, for the per-day and per-conversation sums: building a Python row for each of 14 days of
# turns (about 50,000) took 0.8 s of the dashboard's CPU on every call.
_KEPT = "(NOT COALESCE(r.new_conversation, 0) AND r.prefix_msgs_kept IS NOT NULL)"
_CHANGED = ("((r.prev_n_messages IS NOT NULL AND r.prefix_msgs_kept < r.prev_n_messages)"
            " OR COALESCE(r.system_changed, 0) OR COALESCE(r.tools_changed, 0))")
IS_CLIENT = f"({_KEPT} AND {_CHANGED})"
IS_ENGINE = f"({_KEPT} AND NOT {_CHANGED} AND r.reread_tokens IS NOT NULL AND r.reread_tokens > {REREAD_FLAG_TOKENS})"


def sessions(conn, hours: float, now: float, open_convs=()) -> list[dict]:
    """The last MAX_SESSIONS conversations, newest first, with their sums; `turns` only for open_convs (the
    ones a reader has open): the list itself needs one row per conversation, not every turn."""
    since = now - hours * 3600
    convs = conn.execute(
        "SELECT r.conv, MAX(r.ts_start) AS last, r.client_key, r.prompt AS last_prompt, COUNT(*) AS n,"
        " SUM(COALESCE(r.reread_tokens, 0)) AS reread,"
        f" SUM({IS_ENGINE}) AS flagged, SUM({IS_CLIENT}) AS client_changes,"
        " SUM(r.cache_save_refused IS NOT NULL) AS refusals"
        f" FROM requests r WHERE {COUNTED} AND r.conv IS NOT NULL AND r.ts_start >= ?"
        " GROUP BY r.conv ORDER BY last DESC LIMIT ?", (since, MAX_SESSIONS)).fetchall()
    names = _names(conn)
    wanted = set(open_convs)
    out = []
    for c in convs:
        key = c["client_key"]  # the latest turn's (MAX picks the row its bare columns come from)
        s = {"conv": c["conv"], "client": {"key": key, "name": (names.get(key) or {}).get("name") or key,
                                           "color": (names.get(key) or {}).get("color")},
             "last": c["last"], "n_turns": c["n"], "last_prompt": c["last_prompt"], "reread": c["reread"],
             "flagged": c["flagged"], "client_changes": c["client_changes"], "refusals": c["refusals"]}
        if c["conv"] in wanted:
            s["turns"] = turns(conn, c["conv"], since)
        out.append(s)
    return out


def turns(conn, conv: str, since: float) -> list[dict]:
    """One conversation's turns since `since`, oldest first (at most the last MAX_TURNS)."""
    rows = conn.execute(
        f"SELECT * FROM (SELECT * FROM requests r WHERE {COUNTED} AND conv = ? AND ts_start >= ?"
        " ORDER BY ts_start DESC LIMIT ?) ORDER BY ts_start", (conv, since, MAX_TURNS)).fetchall()
    return [_turn(r) for r in rows]


def daily(conn, days: int, now: float) -> list[dict]:
    """Per local day and profile: turns, computed prompt tokens, re-read tokens, flagged turns, refusals."""
    since = now - days * 86400
    rows = conn.execute(
        "SELECT date(r.ts_start, 'unixepoch', 'localtime') AS date, r.profile, COUNT(*) AS turns,"
        " SUM(MAX(0, COALESCE(r.prompt, 0) - COALESCE(r.cached, 0))) AS computed,"
        " SUM(COALESCE(r.reread_tokens, 0)) AS reread,"
        f" SUM({IS_ENGINE}) AS engine_misses, SUM({IS_CLIENT}) AS client_changes,"
        " SUM(r.cache_save_refused IS NOT NULL) AS refusals"
        f" FROM requests r WHERE {COUNTED} AND r.ts_start >= ? AND r.prompt IS NOT NULL"
        " GROUP BY date, r.profile", (since,)).fetchall()
    return sorted((dict(r) for r in rows), key=lambda a: (a["date"], a["profile"] or ""), reverse=True)


def cache_view(conn, hours: float, days: int, now: float, open_convs=()) -> dict:
    return {"flag_tokens": REREAD_FLAG_TOKENS, "sessions": sessions(conn, hours, now, open_convs),
            "daily": daily(conn, days, now)}
