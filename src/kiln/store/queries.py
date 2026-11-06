"""Read queries behind the dashboard API (docs/API.md). All read-only."""
from __future__ import annotations

import hashlib
import json
import os
import sqlite3
import statistics
import time
from collections import defaultdict

from kiln.collect import spread
from kiln.store import buckets, speeds

PERIODS = {"today": None, "7d": 7 * 86400, "30d": 30 * 86400, "all": None}
RANGES = {"15m": (900, 1), "1h": (3600, 10), "6h": (6 * 3600, 60), "24h": (86400, 300),
          "7d": (7 * 86400, 3600), "30d": (30 * 86400, 3600)}
GROUPS = {"device": "r.client_key", "model": "r.model", "profile": "COALESCE(r.profile, 'container:' || r.container)",
          "host": "r.host"}
SERIES_GROUPS = {"none": "'all'", "device": "client_key", "model": "model", "host": "host"}
MIN_RATE_TOKENS = 32  # rates of tiny replies are noise
DRAFT_POSITIONS = 7  # K = 7: the draft depths of Crucible's per-position counters
DRAFT_LENGTHS = 8  # a draft length is 0..K
# Work served by an engine: on this host (gateway, direct) or on a remote host, reported by its agent.
COUNTED = "r.source IN ('gateway', 'direct', 'agent')"
LOCAL_COUNTED = "r.source IN ('gateway', 'direct')"  # what this host's gateway and engine log saw
ALL_HOSTS = "all"


def host_filter(host: str | None, col: str = "r.host") -> tuple[str, tuple]:
    """SQL condition (with a leading AND) and args restricting rows to one host; none for all hosts."""
    if host is None or host == ALL_HOSTS:
        return "", ()
    return f" AND {col} = ?", (host,)


# The UI's Bauhaus palette (static/js/store.js) first, then quieter tones. Clients get
# colours in the order they first appear, so the first twelve are always distinct.
# Most distinct first: the busiest clients get these (rebalance_colors).
PALETTE = ("#1d4f91", "#d2342a", "#f2c230", "#161616", "#7a7466", "#2f7a5c", "#6b4f96", "#5b86c5", "#e8846f",
           "#b39020", "#8a2f24", "#9fb8dc", "#3d3a33", "#f6dc7a")
COLOR_RANK_S = 31 * 86400  # the activity heatmap's window


def local_tz() -> str:
    """IANA name of the server's zone (dates and hours in /activity use it). TZ wins, as it does for
    time.localtime."""
    from kiln.config import zone_from_env
    env = zone_from_env()
    if env:
        return env
    try:
        target = os.readlink("/etc/localtime")
        if "zoneinfo/" in target:
            return target.split("zoneinfo/", 1)[1]
    except OSError:
        pass
    return time.strftime("%Z")


def local_midnight(now: float) -> float:
    t = time.localtime(now)
    return time.mktime((t.tm_year, t.tm_mon, t.tm_mday, 0, 0, 0, 0, 0, -1))


def period_start(period: str, now: float) -> float:
    if period == "today":
        return local_midnight(now)
    if period == "all":
        return 0.0
    return now - PERIODS[period]


def client_color(key: str, chosen: str | None = None) -> str:
    if chosen:
        return chosen
    return PALETTE[int(hashlib.sha1(key.encode()).hexdigest(), 16) % len(PALETTE)]


def assign_colors(conn: sqlite3.Connection) -> int:
    """Give every client without a colour the least used palette colour, oldest client first."""
    used = defaultdict(int)
    for r in conn.execute("SELECT color FROM clients WHERE color IS NOT NULL"):
        used[r[0]] += 1
    n = 0
    for r in conn.execute("SELECT key FROM clients WHERE color IS NULL ORDER BY updated, key").fetchall():
        color = min(PALETTE, key=lambda c: (used[c], PALETTE.index(c)))
        used[color] += 1
        conn.execute("UPDATE clients SET color = ? WHERE key = ?", (color, r[0]))
        n += 1
    return n


def rebalance_colors(conn: sqlite3.Connection, now: float) -> int:
    """Give the busiest clients of the last 31 days distinct colours, busiest first.

    Only as many clients as the palette has colours take part. A client keeps its colour unless a
    busier one already has it, so colours rarely move. Quieter and idle clients keep theirs."""
    ranked = [r[0] for r in conn.execute(
        "SELECT client_key FROM requests WHERE ts_start >= ? AND client_key IS NOT NULL GROUP BY client_key"
        " ORDER BY SUM(MAX(COALESCE(ts_end, ts_start) - ts_start, 0)) DESC, COUNT(*) DESC, client_key",
        (now - COLOR_RANK_S,))]
    current = {r[0]: r[1] for r in conn.execute("SELECT key, color FROM clients")}
    ranked = [k for k in ranked if k in current][:len(PALETTE)]
    taken: list[str] = []
    changed = 0
    for i, key in enumerate(ranked):
        color = current[key]
        if color not in PALETTE or color in taken:
            # Prefer a colour no quieter client holds either, so one fix doesn't start another.
            later = {current[k] for k in ranked[i + 1:]}
            free = [c for c in PALETTE if c not in taken and c not in later] or \
                [c for c in PALETTE if c not in taken]
            color = free[0] if free else PALETTE[len(taken) % len(PALETTE)]
        taken.append(color)
        if color != current[key]:
            conn.execute("UPDATE clients SET color = ? WHERE key = ?", (color, key))
            changed += 1
    return changed


def union_seconds(intervals: list[tuple[float, float]]) -> float:
    total, cur_s, cur_e = 0.0, None, None
    for s, e in sorted(intervals):
        if e is None or e < s:
            continue
        if cur_e is None or s > cur_e:
            if cur_e is not None:
                total += cur_e - cur_s
            cur_s, cur_e = s, e
        else:
            cur_e = max(cur_e, e)
    if cur_e is not None:
        total += cur_e - cur_s
    return total


def rates(r) -> tuple[float | None, float | None, tuple[bool, bool]]:
    """(decode tok/s, prefill tok/s, decode_estimated) for a request row.

    The engine's own timing is exact. Without it, decode is the tokens after the first stream
    event over the time from first to last event, which assumes tokens were spread evenly over
    the events: an estimate. When the engine says how many of the completion tokens it generated
    (Crucible writes the close text of a think turn it closed at once), the engine rate uses those.
    """
    decode = prefill = None
    estimated = prefill_estimated = False
    comp = r["completion"]
    generated = r["engine_generated_tokens"] if r["engine_decode_s"] else None
    if generated is not None:
        if generated >= MIN_RATE_TOKENS:
            decode = generated / r["engine_decode_s"]
    elif comp and comp >= MIN_RATE_TOKENS:
        if r["engine_decode_s"]:
            decode = comp / r["engine_decode_s"]
        elif r["ts_first_token"] and r["ts_last_token"] and r["ts_last_token"] > r["ts_first_token"]:
            events = r["token_events"] or comp
            decode = (comp - comp / events) / (r["ts_last_token"] - r["ts_first_token"])
            estimated = True
    if r["engine_prefill_tokens"] and r["engine_prefill_s"]:
        prefill = r["engine_prefill_tokens"] / r["engine_prefill_s"]
    elif r["prompt"] and r["ttft_s"] and r["engine_joined_at"] is None:
        computed = r["prompt"] - (r["cached"] or 0)
        prefill = computed / r["ttft_s"] if computed > 0 else None  # TTFT includes queueing: an estimate
        prefill_estimated = prefill is not None
    return (round(decode, 2) if decode else None), (round(prefill, 1) if prefill else None), \
        (estimated, prefill_estimated)


def names(conn) -> dict[str, dict]:
    return _names(conn)


def _names(conn) -> dict[str, dict]:
    return {r["key"]: {"name": r["alias"] or r["name"], "color": client_color(r["key"], r["color"])}
            for r in conn.execute("SELECT key, name, alias, color FROM clients")}


def _int_list(text: str | None) -> list[int] | None:
    """The integers of a JSON list column (migration 013), or None. A row another writer broke reads as
    unknown: a display value must not raise on the way to the API."""
    if text is None:
        return None
    try:
        v = json.loads(text)
    except ValueError:
        return None
    return v if isinstance(v, list) and all(isinstance(x, int) and not isinstance(x, bool) for x in v) else None


def request_row(conn: sqlite3.Connection, r: sqlite3.Row, names: dict | None = None,
                costs: dict | None = None) -> dict:
    names = names if names is not None else _names(conn)
    if costs is None:
        costs = {c["source"]: c["usd"] for c in conn.execute(
            "SELECT source, usd FROM request_costs WHERE request_id = ?", (r["id"],))}
    decode, prefill, (estimated, prefill_estimated) = rates(r)
    key = r["client_key"]
    decode_from, decode_to = spread.decode_window(r)
    return {"id": r["id"], "ts": r["ts_start"], "ts_end": r["ts_end"], "host": r["host"],
            "decode_from": decode_from, "decode_to": decode_to,
            "client": {"key": key, "name": (names.get(key) or {}).get("name") or r["client_name"] or key,
                       "label": r["client_label"], "color": (names.get(key) or {}).get("color") or client_color(key)},
            "model": r["model"], "profile": r["profile"], "engine": r["engine"], "status": r["status"],
            "finish": r["finish"], "prompt": r["prompt"], "cached": r["cached"], "completion": r["completion"],
            "reasoning": r["reasoning"], "ttft_s": r["ttft_s"], "decode_tps": decode, "prefill_tps": prefill,
            "decode_estimated": estimated, "prefill_estimated": prefill_estimated,
            "latency_s": r["latency_s"], "think_close": r["think_close"], "source": r["source"],
            "tool_call_fallback": None if r["tool_call_fallback"] is None else bool(r["tool_call_fallback"]),
            "max_tokens": r["max_tokens"], "kv_pool_used": r["kv_pool_used"], "kv_pool_total": r["kv_pool_total"],
            "draft_accepted": r["draft_accepted"], "draft_proposed": r["draft_proposed"], "conv": r["conv"],
            "spec_rounds": r["spec_rounds"], "generated_tokens": r["engine_generated_tokens"],
            "forced_tokens": r["engine_forced_tokens"], "draft_passes": r["draft_passes"],
            "draft_ms": r["draft_ms"], "draft_pos_proposed": _int_list(r["draft_pos_proposed"]),
            "draft_pos_tested": _int_list(r["draft_pos_tested"]),
            "draft_pos_accepted": _int_list(r["draft_pos_accepted"]),
            "draft_len_hist": _int_list(r["draft_len_hist"]),
            "cost": {"reference": costs.get("reference"), "openrouter": costs.get("openrouter")},
            "error": r["error"]}


def get_request(conn, rid: str) -> dict | None:
    r = conn.execute("SELECT * FROM requests WHERE id = ?", (rid,)).fetchone()
    return request_row(conn, r) if r else None


def recent_requests(conn, limit: int = 50, before: float | None = None, host: str | None = None) -> list[dict]:
    limit = max(1, min(int(limit), 500))
    cond, args = host_filter(host)
    rows = conn.execute(f"SELECT * FROM requests r WHERE {COUNTED}{cond} AND ts_start < ? ORDER BY ts_start DESC"
                        " LIMIT ?", (*args, before if before is not None else 1e12, limit)).fetchall()
    names = _names(conn)
    costs = defaultdict(dict)
    if rows:
        ids = [r["id"] for r in rows]
        for c in conn.execute(f"SELECT request_id, source, usd FROM request_costs WHERE request_id IN"
                              f" ({','.join('?' * len(ids))})", ids):
            costs[c["request_id"]][c["source"]] = c["usd"]
    return [request_row(conn, r, names, costs[r["id"]]) for r in rows]


def last_request(conn, host: str | None = None, tokens_known: bool = True) -> dict | None:
    """The latest successful request. tokens_known=False also takes one whose ledger had no token counts
    (older host agent records), so a host's card doesn't point days back."""
    cond, args = host_filter(host)
    if tokens_known:
        cond += " AND completion IS NOT NULL"
    r = conn.execute(f"SELECT * FROM requests r WHERE {COUNTED}{cond} AND status = 200"
                     " ORDER BY ts_end DESC LIMIT 1", args).fetchone()
    if r is None:
        return None
    decode, prefill, (estimated, prefill_estimated) = rates(r)
    return {"id": r["id"], "ts": r["ts_end"], "decode_estimated": estimated, "prefill_estimated": prefill_estimated, "ttft_s": r["ttft_s"], "decode_tps": decode, "prefill_tps": prefill,
            "prompt": r["prompt"], "cached": r["cached"], "completion": r["completion"], "finish": r["finish"],
            "think_close": r["think_close"], "source": r["source"],
            "draft_accepted": r["draft_accepted"], "draft_proposed": r["draft_proposed"]}


def usage(conn, period: str, group: str, now: float, host: str | None = None) -> dict:
    if period not in PERIODS or group not in GROUPS:
        raise ValueError("bad period or group")
    start = period_start(period, now)
    key = GROUPS[group]
    cond, args = host_filter(host)
    rows = conn.execute(
        f"SELECT {key} AS k, r.ts_start, r.ts_end, r.prompt, r.cached, r.completion, r.reasoning, r.status,"
        " r.error, r.engine_prefill_tokens, r.client_name, r.tool_call_fallback,"
        " (SELECT usd FROM request_costs c WHERE c.request_id = r.id AND c.source = 'reference') AS ref,"
        " (SELECT usd FROM request_costs c WHERE c.request_id = r.id AND c.source = 'openrouter') AS orc"
        f" FROM requests r WHERE {COUNTED}{cond} AND r.ts_start >= ?", (*args, start)).fetchall()
    names = _names(conn) if group == "device" else {}
    groups: dict[str, dict] = {}
    intervals: dict[str, list] = defaultdict(list)
    all_intervals = []
    for r in rows:
        k = r["k"] or "unknown"
        g = groups.setdefault(k, _empty(k, _group_name(group, k, names, r)))
        _add(g, r)
        if r["ts_end"]:
            intervals[k].append((r["ts_start"], r["ts_end"]))
            all_intervals.append((r["ts_start"], r["ts_end"]))
    for k, g in groups.items():
        g["busy_s"] = round(union_seconds(intervals[k]), 1)
        if group == "device":
            g["color"] = (names.get(k) or {}).get("color") or client_color(k)
    totals = _empty(None, None)
    for r in rows:
        _add(totals, r)
    totals["busy_s"] = round(union_seconds(all_intervals), 1)
    del totals["key"], totals["name"]
    out = sorted(groups.values(), key=lambda g: -(g["cost"]["reference"] or 0))
    return {"period": period, "from": start, "group": group, "host": host or ALL_HOSTS, "rows": out,
            "totals": totals, "buckets": _buckets(rows, period, start, now)}


def _buckets(rows: list, period: str, start: float, now: float) -> dict:
    """The usage bars (store/buckets.py). Tokens are prompt plus output, as in the cost headline."""
    items = (buckets.Item(r["k"] or "unknown", r["ts_start"], r["ts_end"], r["ref"] or 0.0,
                          (r["prompt"] or 0) + (r["completion"] or 0)) for r in rows)
    return buckets.aggregate(items, period, start, now, local_tz())


def _group_name(group: str, key: str, names: dict, r) -> str:
    """A usage row's display name. Only a device has a name apart from its key; a model, profile or host is
    its key, except that direct traffic to a container without a profile shows the container."""
    if group == "device":
        return (names.get(key) or {}).get("name") or r["client_name"] or key
    if group == "profile" and key.startswith("container:"):
        return f"{key.removeprefix('container:')} (container)"
    return key


def _empty(key, name) -> dict:
    return {"key": key, "name": name, "requests": 0, "prompt": 0, "cached": 0, "computed": 0, "completion": 0,
            "reasoning": 0, "busy_s": 0.0, "cost": {"reference": 0.0, "openrouter": 0.0}, "errors": 0,
            "tool_call_fallbacks": 0}


def _add(g: dict, r) -> None:
    g["requests"] += 1
    prompt, cached = r["prompt"] or 0, r["cached"] or 0
    g["prompt"] += prompt
    g["cached"] += cached
    g["computed"] += r["engine_prefill_tokens"] if r["engine_prefill_tokens"] is not None else max(prompt - cached, 0)
    g["completion"] += r["completion"] or 0
    g["reasoning"] += r["reasoning"] or 0
    g["cost"]["reference"] += r["ref"] or 0.0
    g["cost"]["openrouter"] += r["orc"] or 0.0
    if (r["status"] is not None and r["status"] != 200) or r["error"]:
        g["errors"] += 1
    if r["tool_call_fallback"]:
        g["tool_call_fallbacks"] += 1


def _draft_pos_sums(conn: sqlite3.Connection, col: str, start: float, host: str | None,
                    size: int) -> dict[str, list[int]]:
    """One per-position column summed per depth, per profile.

    The column holds a short JSON list (migration 013). Summing it here rather than in SQL keeps one row that
    another writer broke from failing the whole view: a value that is not a list of integers is skipped.
    """
    cond, args = host_filter(host)
    out: dict[str, list[int]] = {}
    for r in conn.execute(
            f"SELECT COALESCE(r.profile, 'container:' || r.container) AS profile, r.{col} AS text"
            f" FROM requests r WHERE {COUNTED}{cond} AND r.ts_start >= ? AND r.{col} IS NOT NULL", (*args, start)):
        values = _int_list(r["text"])
        if values is None:
            continue
        row = out.setdefault(r["profile"] or "unknown", [0] * size)
        for i, n in enumerate(values[:size]):
            if n >= 0:
                row[i] += n
    return out


def draft_acceptance(conn, period: str, now: float, host: str | None = None) -> dict:
    """Crucible's per-position draft counters per profile, and the conditional accept rate at each depth.

    The rate is accepted_i / tested_i, so it is computed from the summed counters: the mean of the requests'
    own rates would weigh a request with two tested drafts as much as one with two thousand.
    """
    if period not in PERIODS:
        raise ValueError("bad period")
    start = period_start(period, now)
    cond, args = host_filter(host)
    proposed = _draft_pos_sums(conn, "draft_pos_proposed", start, host, DRAFT_POSITIONS)
    tested = _draft_pos_sums(conn, "draft_pos_tested", start, host, DRAFT_POSITIONS)
    accepted = _draft_pos_sums(conn, "draft_pos_accepted", start, host, DRAFT_POSITIONS)
    hist = _draft_pos_sums(conn, "draft_len_hist", start, host, DRAFT_LENGTHS)
    rows = []
    for r in conn.execute(
            f"SELECT COALESCE(r.profile, 'container:' || r.container) AS profile, r.engine AS engine,"
            " COUNT(*) AS requests, SUM(r.spec_rounds) AS rounds, SUM(r.draft_passes) AS passes,"
            " SUM(r.draft_ms) AS draft_ms,"
            # The engine split exists only for a think turn that the server closed; else completion is the engine's.
            " SUM(COALESCE(r.engine_generated_tokens, r.completion)) AS generated"
            f" FROM requests r WHERE {COUNTED}{cond} AND r.ts_start >= ?"
            " AND (r.draft_pos_proposed IS NOT NULL OR r.draft_pos_tested IS NOT NULL"
            " OR r.draft_pos_accepted IS NOT NULL OR r.draft_len_hist IS NOT NULL"
            " OR r.draft_passes IS NOT NULL OR r.draft_ms IS NOT NULL)"
            " GROUP BY 1, 2 ORDER BY 1, 2", (*args, start)):
        key = r["profile"] or "unknown"
        t = tested.get(key, [0] * DRAFT_POSITIONS)
        a = accepted.get(key, [0] * DRAFT_POSITIONS)
        rows.append({"profile": key, "engine": r["engine"], "requests": r["requests"], "rounds": r["rounds"],
                     "passes": r["passes"], "proposed": proposed.get(key, [0] * DRAFT_POSITIONS),
                     "tested": t, "accepted": a,
                     # A depth nothing tested has no rate; a rate of 0 would read as "never accepted".
                     "accept_rate": [round(ai / ti, 4) if ti else None for ti, ai in zip(t, a)],
                     "len_hist": hist.get(key, [0] * DRAFT_LENGTHS), "draft_ms": r["draft_ms"],
                     "generated_tokens": r["generated"],
                     "ms_per_token": round(r["draft_ms"] / r["generated"], 3)
                     if r["generated"] and r["draft_ms"] is not None else None})
    return {"period": period, "from": start, "host": host or ALL_HOSTS, "positions": DRAFT_POSITIONS,
            "rows": rows}


def series(conn, rng: str, group: str, now: float, host: str | None = None) -> dict:
    if rng not in RANGES or group not in SERIES_GROUPS:
        raise ValueError("bad range or group")
    span, step = RANGES[rng]
    end = int(now) // step * step
    start = end - span + step
    key = SERIES_GROUPS[group]
    t = list(range(start, end + 1, step))
    index = {b: i for i, b in enumerate(t)}
    out: dict[str, dict] = {}
    names = _names(conn) if group == "device" else {}
    cond, args = host_filter(host, "host")
    for r in conn.execute(f"SELECT (t / ?) * ? AS b, {key} AS k, SUM(decode) AS d, SUM(prefill) AS p"
                          f" FROM series_1s WHERE t >= ? AND t < ?{cond} GROUP BY b, k",
                          (step, step, start, end + step, *args)):
        s = out.setdefault(r["k"], {"name": (names.get(r["k"]) or {}).get("name") or r["k"],
                                    "decode": [0] * len(t), "prefill": [0] * len(t)})
        i = index.get(r["b"])
        if i is not None:
            s["decode"][i], s["prefill"][i] = r["d"], r["p"]
    return {"range": rng, "step": step, "host": host or ALL_HOSTS, "t": t, "series": out}


def activity(conn, days: int, now: float, host: str | None = None) -> dict:
    days = max(1, min(int(days), 366))
    t = time.localtime(now)
    first = time.mktime((t.tm_year, t.tm_mon, t.tm_mday - (days - 1), 0, 0, 0, 0, 0, -1))  # calendar days
    slots: dict[tuple[str, int], dict[str, list]] = defaultdict(lambda: defaultdict(list))
    cond, args = host_filter(host)
    for r in conn.execute(f"SELECT client_key, ts_start, ts_end FROM requests r WHERE {COUNTED}{cond} AND ts_end >= ?",
                          (*args, first)):
        s, e = max(r["ts_start"], first), r["ts_end"]
        while s < e:
            lt = time.localtime(s)
            # Step to the next whole local hour from s itself: rebuilding the hour with mktime is
            # ambiguous in the repeated hour when daylight saving ends and could stop advancing.
            seg_end = min(e, s - (lt.tm_min * 60 + lt.tm_sec + (s % 1)) + 3600)
            slots[(time.strftime("%Y-%m-%d", lt), lt.tm_hour)][r["client_key"]].append((s, seg_end))
            s = seg_end
    names = _names(conn)
    out_days, seen = [], set()
    for d in range(days):
        date = time.strftime("%Y-%m-%d", time.localtime(
            time.mktime((t.tm_year, t.tm_mon, t.tm_mday - (days - 1) + d, 12, 0, 0, 0, 0, -1))))
        hours = []
        for h in range(24):
            by_client = slots.get((date, h), {})
            busy = union_seconds([iv for ivs in by_client.values() for iv in ivs])
            top = max(by_client, key=lambda k: union_seconds(by_client[k])) if by_client else None
            if top:
                seen.add(top)
            hours.append({"busy_s": round(busy, 1), "top": top})
        out_days.append({"date": date, "hours": hours})
    clients = {k: {"name": (names.get(k) or {}).get("name") or k,
                   "color": (names.get(k) or {}).get("color") or client_color(k)} for k in seen}
    return {"slot_s": 3600, "tz": local_tz(), "days": out_days, "clients": clients}


def today(conn, now: float, host: str | None = None) -> dict:
    u = usage(conn, "today", "model", now, host)["totals"]
    # since: the day's start, so the browser leaves out a stream that began before it (its record is yesterday's)
    return {**{k: u[k] for k in ("requests", "prompt", "cached", "completion", "cost")}, "since": local_midnight(now)}


def today_bundle(conn, now: float, host: str, hosts: list[str]) -> tuple[dict, tuple, dict, dict]:
    """The tick's slower parts: this host's today (with its speeds), parse errors in 24 h, client names, and
    each remote host's today and last request. It reads every request of the day: kiln-dash runs it off the
    event loop."""
    per_host = {n: {"today": today(conn, now, n), "last": last_request(conn, n, tokens_known=False)} for n in hosts}
    return ({**today(conn, now, host), "speeds": day_speeds(conn, now, host)}, parse_errors_since(conn, now - 86400),
            names(conn), per_host)


# The columns rates() reads, and the profile.
RATE_COLUMNS = ("profile, completion, prompt, cached, ttft_s, ts_first_token, ts_last_token, token_events,"
                " engine_decode_s, engine_generated_tokens, engine_prefill_tokens, engine_prefill_s, engine_joined_at")


def day_speeds(conn, now: float, host: str | None = None) -> dict[str, dict]:
    """Each profile's decode and prefill speeds and TTFT since local midnight (store/speeds.py), from the rates a
    request row shows: decode from MIN_RATE_TOKENS output tokens, prefill from MIN_PREFILL_RATE_TOKENS new tokens,
    TTFT from every request with a first token (a small cached prompt waits too)."""
    cond, args = host_filter(host)
    pairs: dict[str, tuple[list, list, list]] = defaultdict(lambda: ([], [], []))
    for r in conn.execute(f"SELECT {RATE_COLUMNS} FROM requests r WHERE {COUNTED}{cond} AND r.status = 200"
                          " AND r.profile IS NOT NULL AND r.ts_start >= ?", (*args, local_midnight(now))):
        decode, prefill, _ = rates(r)
        dec, pre, ttft = pairs[r["profile"]]
        if decode:
            dec.append((_decode_tokens(r), decode))
        new = _prefill_tokens(r)
        if prefill and new >= speeds.MIN_PREFILL_RATE_TOKENS:
            pre.append((new, prefill))
        ttft.append(r["ttft_s"])
    return {k: {"decode": speeds.summary(d, 1), "prefill": speeds.summary(p, 0), "ttft": speeds.waits(t, 2)}
            for k, (d, p, t) in pairs.items()}


def _decode_tokens(r) -> int:
    """The tokens behind a row's decode rate (rates): those the engine generated when it says, else all."""
    if r["engine_decode_s"] and r["engine_generated_tokens"] is not None:
        return r["engine_generated_tokens"]
    return r["completion"] or 0


def _prefill_tokens(r) -> int:
    """The new prompt tokens behind a row's prefill rate (rates)."""
    if r["engine_prefill_tokens"] and r["engine_prefill_s"]:
        return r["engine_prefill_tokens"]
    return max(0, (r["prompt"] or 0) - (r["cached"] or 0))


def parse_errors_since(conn, since: float) -> tuple[int, float | None]:
    """How many engine lines failed to parse since `since`, and when the latest one did."""
    n, last = conn.execute("SELECT COUNT(*), MAX(ts) FROM engine_events WHERE kind = 'parse_error' AND ts >= ?",
                           (since,)).fetchone()
    return n, last


def seed_seconds(conn, since: int, host: str | None = None) -> dict[int, dict]:
    cond, args = host_filter(host, "host")
    return {r["t"]: {"decode": r["d"], "prefill": r["p"]} for r in conn.execute(
        f"SELECT t, SUM(decode) AS d, SUM(prefill) AS p FROM series_1s WHERE t >= ?{cond} GROUP BY t", (since, *args))}


def recent_starts(conn, since: float, host: str | None = None) -> list[tuple[str, float]]:
    """(id, start) of the gateway's requests since `since`: the live chart's start lines after a restart."""
    cond, args = host_filter(host, "host")
    return [(r[0], r[1]) for r in conn.execute(
        f"SELECT id, ts_start FROM requests WHERE source = 'gateway' AND ts_start >= ?{cond}", (since, *args))]


def recent_draft_turns(conn, since: float, host: str | None = None) -> list[tuple]:
    """(conv, id, start, proposed, accepted) of the turns since `since` that drafted, oldest first: the live
    view's draft acceptance for each conversation after a restart (live/drafts.py)."""
    cond, args = host_filter(host, "host")
    return [tuple(r) for r in conn.execute(
        "SELECT conv, id, ts_start, draft_proposed, draft_accepted FROM requests"
        f" WHERE ts_start >= ? AND conv IS NOT NULL AND draft_proposed > 0{cond} ORDER BY ts_start",
        (since, *args))]


def recent_exactness(conn, per_profile: int = 20) -> list[tuple[str, int, int]]:
    out = []
    for p in conn.execute("SELECT DISTINCT profile FROM requests WHERE source = 'gateway' AND profile IS NOT NULL"):
        rows = conn.execute("SELECT completion, token_events FROM requests WHERE source = 'gateway' AND profile = ?"
                            " AND completion IS NOT NULL AND token_events IS NOT NULL ORDER BY ts_end DESC LIMIT ?",
                            (p[0], per_profile)).fetchall()
        out += [(p[0], r[0], r[1]) for r in reversed(rows)]
    return out


def recent_prefill_rates(conn, per_profile: int = 10, min_tokens: int = 4096,
                         max_tps: float = spread.DEFAULT_MAX_PREFILL_TPS) -> dict[str, float]:
    """Median cold-prefill tok/s per profile over its latest large prefills (live progress estimates).

    Prefills the engine reported above `max_tps` are left out: a conversation-cache restore logged as
    prefill work (config.DEFAULT_MAX_PREFILL_TPS) would otherwise set the estimate to half a million
    tok/s and draw it as prefill in a second.
    """
    out = {}
    for (profile,) in conn.execute("SELECT DISTINCT profile FROM requests WHERE profile IS NOT NULL"):
        rates = [r[0] / r[1] for r in conn.execute(
            "SELECT engine_prefill_tokens, engine_prefill_s FROM requests WHERE profile = ?"
            " AND engine_prefill_tokens >= ? AND engine_prefill_s > 0 AND engine_prefill_tokens <= ? * engine_prefill_s"
            " ORDER BY ts_end DESC LIMIT ?",
            (profile, min_tokens, max_tps, per_profile))]
        if rates:
            out[profile] = statistics.median(rates)
    return out
