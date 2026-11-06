"""Synthetic history and aggregation for the Kiln mock dashboard (tools/mock_dash.py).

Generates ~35 days of believable requests from a seeded RNG, prices them with the
card in force at each request's start (sticky costs), and answers the REST
aggregations: usage, series, activity, requests, prices.
"""
from __future__ import annotations

import math
import random
import time
from collections import defaultdict
from datetime import datetime, timedelta
from pathlib import Path

from kiln.store import buckets, speeds  # mock_dash puts src/ on the path

DAY = 86400
GIB = 1024 ** 3

CLIENTS = [
    {"key": "198.51.100.10", "name": "laptop", "label": None, "color": "#1d4f91", "w": 0.52, "kind": "agent"},
    {"key": "198.51.100.4", "name": "server", "label": None, "color": "#f2c230", "w": 0.16, "kind": "agent"},
    {"key": "198.51.100.22", "name": "phone", "label": None, "color": "#d2342a", "w": 0.12, "kind": "chat"},
    {"key": "eval-set-1", "name": "eval-set-1", "label": "Eval: test set", "color": "#161616", "w": 0.0, "kind": "eval"},
    {"key": "192.0.2.57", "name": "192.0.2.57", "label": None, "color": "#7a7466", "w": 0.06, "kind": "chat"},
]
DIRECT = {"key": "direct", "name": "Direct to engine", "label": None, "color": "#b39020"}
CLIENT_BY_KEY = {c["key"]: c for c in CLIENTS + [DIRECT]}

PROFILES = [
    {"name": "crucible-flashnext", "label": "Crucible · Qwen3.8 Flash-Next (3.05 bpw)", "engine": "crucible",
     "model": "qwen3.8-flash-next", "context": 65536, "slots": 4, "memory_gib": 100,
     "kv_positions": 262144, "kv_bytes": None, "decode": 60.0, "prefill": 2000.0,
     "exact": True, "progress": True},
    {"name": "crucible-flashnext-draft", "label": "Crucible · Qwen3.8 Flash-Next (Draft)",
     "engine": "crucible", "model": "qwen3.8-flash-next", "context": 65536, "slots": 4, "memory_gib": 100,
     "kv_positions": 262144, "kv_bytes": None, "decode": 60.0, "prefill": 2000.0,
     "exact": True, "progress": True, "disabled": "the draft checkpoint is not on this machine yet"},
    {"name": "halogen-flashnext", "label": "Halogen / Qwen3.8 Flash-Next", "engine": "halogen",
     "model": "qwen3.8-flash-next", "context": 262144, "slots": 4, "memory_gib": 94,
     "kv_positions": 524288, "kv_bytes": None, "decode": 56.0, "prefill": 2400.0,
     "exact": True, "progress": True},
    {"name": "gufo-flashnext", "label": "Gufo / Qwen3.8 Flash-Next", "engine": "gufo",
     "model": "qwen3.8-flash-next", "context": 131072, "slots": 2, "memory_gib": 88,
     "kv_positions": 262144, "kv_bytes": 6979321856, "decode": 61.0, "prefill": 2050.0,
     "exact": False, "progress": False},
    {"name": "halogen-27b", "label": "Halogen / Qwen3.8 27B", "engine": "halogen",
     "model": "qwen3.8-27b", "context": 131072, "slots": 2, "memory_gib": 72,
     "kv_positions": 131072, "kv_bytes": 17179869184, "decode": 14.5, "prefill": 640.0,
     "exact": True, "progress": True},
    {"name": "gufo-27b", "label": "Gufo / Qwen3.8 27B", "engine": "gufo",
     "model": "qwen3.8-27b", "context": 65536, "slots": 1, "memory_gib": 64,
     "kv_positions": 65536, "kv_bytes": 8589934592, "decode": 16.0, "prefill": 560.0,
     "exact": False, "progress": False},
]
PROFILE_BY_NAME = {p["name"]: p for p in PROFILES}


def public_profile(p: dict, active: str) -> dict:
    keys = ("name", "label", "engine", "model", "context", "slots", "memory_gib")
    return {**{k: p[k] for k in keys}, "active": p["name"] == active, "disabled": p.get("disabled")}


def make_price_cards(now: float) -> list[dict]:
    t0 = now - 60 * DAY
    cards = [
        ("reference", "qwen3.8-flash-next", "claude-sonnet-5-5", 2.0, 0.2, 10.0, t0),
        ("reference", "qwen3.8-27b", "claude-sonnet-5-5", 2.0, 0.2, 10.0, t0),
        ("openrouter", "qwen3.8-flash-next", "qwen/qwen3.8-flash-next", 0.10, 0.01, 0.45, t0),
        ("openrouter", "qwen3.8-27b", "qwen/qwen3.8-27b", 0.30, 0.03, 1.20, t0),
        ("openrouter", "qwen3.8-flash-next", "qwen/qwen3.8-flash-next", 0.10, 0.01, 0.40, now - 12 * DAY),
        ("reference", "qwen3.8-flash-next", "claude-sonnet-5-5", 2.0, 0.1, 10.0, now - DAY),
        ("reference", "qwen3.8-27b", "claude-sonnet-5-5", 2.0, 0.1, 10.0, now - DAY),
    ]
    out = []
    for i, (src, model, prov, inp, cached, outp, eff) in enumerate(cards, 1):
        out.append({"id": i, "source": src, "model": model, "provider_model": prov, "input_per_m": inp,
                    "cached_per_m": cached, "output_per_m": outp, "effective_from": eff,
                    "fetched_at": max(eff, now - 3 * 3600 - i * 60)})
    return out


def card_for(cards: list[dict], source: str, model: str, ts: float) -> dict:
    mine = sorted((c for c in cards if c["source"] == source and c["model"] == model), key=lambda c: c["effective_from"])
    in_force = [c for c in mine if c["effective_from"] <= ts]
    return in_force[-1] if in_force else mine[0]


def price(cards: list[dict], model: str, ts: float, prompt: int, cached: int, completion: int) -> dict:
    cost = {}
    for src in ("reference", "openrouter"):
        c = card_for(cards, src, model, ts)
        cost[src] = round(((prompt - cached) * c["input_per_m"] + cached * c["cached_per_m"]
                           + completion * c["output_per_m"]) / 1e6, 6)
    return cost


def build_request(rng: random.Random, cards: list[dict], ts: float, client: dict, profile: dict,
                  source: str = "gateway") -> dict:
    kind = client.get("kind", "chat")
    if kind == "agent":
        prompt = int(rng.uniform(18_000, 120_000))
        cached = int(prompt * rng.uniform(0.86, 0.995)) if rng.random() > 0.08 else 0
        completion = int(rng.lognormvariate(6.2, 0.8))
    elif kind == "eval":
        prompt, cached = int(rng.uniform(1_500, 14_000)), 0
        completion = int(rng.lognormvariate(7.2, 0.6))
    else:
        prompt = int(rng.uniform(900, 7_000))
        cached = int(prompt * rng.uniform(0, 0.6))
        completion = int(rng.lognormvariate(5.8, 0.7))
    completion = max(8, min(completion, 12_000))
    reasoning = int(completion * rng.uniform(0.2, 0.65)) if rng.random() > 0.25 else 0
    prefill_tps = profile["prefill"] * rng.uniform(0.85, 1.08)
    computed = prompt - cached
    ttft = computed / prefill_tps + rng.uniform(0.04, 0.3)
    decode_tps = profile["decode"] * rng.uniform(0.88, 1.06)
    latency = ttft + completion / decode_tps
    finish = "tool_calls" if kind == "agent" and rng.random() < 0.35 else "stop"
    # null: thinking ended on its own; a string: the engine cut it off and forced the answer.
    think_close = "answer_room" if reasoning and rng.random() < 0.03 else None
    if rng.random() < 0.02:
        finish = "length"
    status, error = 200, None
    if rng.random() < 0.008:
        status, error, finish = 502, "upstream closed the connection", None
    return {
        "id": f"{int(ts * 1000):x}{rng.randrange(16 ** 4):04x}", "ts": ts,
        "client": {k: client[k] for k in ("key", "name", "label", "color")},
        "model": profile["model"], "profile": profile["name"], "status": status, "finish": finish,
        "prompt": prompt, "cached": cached, "completion": completion, "reasoning": reasoning,
        "ttft_s": round(ttft, 3), "decode_tps": round(decode_tps, 1), "prefill_tps": round(prefill_tps, 1),
        "latency_s": round(latency, 2), "think_close": think_close, "source": source,
        "decode_from": round(ts + ttft, 3), "decode_to": round(ts + latency, 3),
        "cost": price(cards, profile["model"], ts, prompt, cached, completion), "error": error,
        # Deterministic, so the seeded history keeps its random sequence: some agent turns fell back to text.
        "tool_call_fallback": kind == "agent" and status == 200 and int(ts) % 41 == 0,
    }


def generate_history(now: float, cards: list[dict], days: int = 35, seed: int = 7) -> list[dict]:
    rng = random.Random(seed)
    rows = []
    start = datetime.fromtimestamp(now).replace(hour=0, minute=0, second=0, microsecond=0) - timedelta(days=days)
    weights = [c["w"] for c in CLIENTS]
    for d in range(days + 1):
        day = start + timedelta(days=d)
        weekend = day.weekday() >= 5
        # The 27B ran for a stretch in the middle of the month.
        profile = PROFILE_BY_NAME["halogen-27b" if 14 <= days - d <= 18 else "halogen-flashnext"]
        for hour in range(24):
            busy = 0.75 if 9 <= hour <= 18 else 0.35 if 19 <= hour <= 23 else 0.06
            busy *= 0.55 if weekend else 1.0
            if rng.random() > busy:
                continue
            for _ in range(rng.randint(1, 7 if not weekend else 3)):
                ts = day.timestamp() + hour * 3600 + rng.uniform(0, 3500)
                if ts > now - 120:
                    continue
                client = rng.choices(CLIENTS, weights)[0]
                source = "direct" if rng.random() < 0.035 else "gateway"
                rows.append(build_request(rng, cards, ts, DIRECT if source == "direct" else client, profile, source))
        # A nightly eval burst on some nights.
        if d % 4 == 1:
            t = day.timestamp() + 2 * 3600 + rng.uniform(0, 1800)
            for _ in range(rng.randint(30, 60)):
                if t > now - 120:
                    break
                r = build_request(rng, cards, t, CLIENT_BY_KEY["eval-set-1"], profile)
                rows.append(r)
                t += r["latency_s"] + rng.uniform(0.5, 4)
    rows.sort(key=lambda r: r["ts"])
    return rows


# ---------------------------------------------------------------- aggregation

def union_seconds(intervals: list[tuple[float, float]]) -> float:
    total, end = 0.0, -math.inf
    for a, b in sorted(intervals):
        if b <= end:
            continue
        total += b - max(a, end)
        end = b
    return total


def group_key(r: dict, group: str) -> tuple[str, str]:
    if group == "model":
        return r["model"], r["model"]
    if group == "profile":
        return r["profile"], PROFILE_BY_NAME.get(r["profile"], {}).get("label", r["profile"])
    c = r["client"]
    return c["key"], c["label"] or c["name"] or c["key"]


def period_start(period: str, now: float) -> float:
    if period == "today":
        return datetime.fromtimestamp(now).replace(hour=0, minute=0, second=0, microsecond=0).timestamp()
    return {"7d": now - 7 * DAY, "30d": now - 30 * DAY}.get(period, 0.0)


def _sum(rows: list[dict]) -> dict:
    out = {"requests": len(rows), "prompt": 0, "cached": 0, "computed": 0, "completion": 0, "reasoning": 0,
           "cost": {"reference": 0.0, "openrouter": 0.0}, "errors": 0, "tool_call_fallbacks": 0}
    for r in rows:
        out["tool_call_fallbacks"] += 1 if r.get("tool_call_fallback") else 0
        out["prompt"] += r["prompt"]
        out["cached"] += r["cached"]
        out["computed"] += r["prompt"] - r["cached"]
        out["completion"] += r["completion"]
        out["reasoning"] += r["reasoning"]
        out["errors"] += 1 if r["error"] else 0
        for k in out["cost"]:
            out["cost"][k] += r["cost"][k]
    out["cost"] = {k: round(v, 4) for k, v in out["cost"].items()}
    out["busy_s"] = round(union_seconds([(r["ts"], r["ts"] + r["latency_s"]) for r in rows]), 1)
    return out


def usage(rows: list[dict], period: str, group: str, now: float) -> dict:
    start = period_start(period, now)
    picked = [r for r in rows if r["ts"] >= start]
    groups: dict[str, list] = defaultdict(list)
    names = {}
    for r in picked:
        key, name = group_key(r, group)
        groups[key].append(r)
        names[key] = name
    out_rows = [{"key": k, "name": names[k], **_sum(v)} for k, v in groups.items()]
    if group == "device":  # device rows carry the server-assigned colour
        out_rows = [{**r, "color": CLIENT_BY_KEY.get(r["key"], {}).get("color")} for r in out_rows]
    return {"v": 1, "period": period, "from": start, "group": group, "rows": out_rows, "totals": _sum(picked),
            "buckets": _buckets(picked, period, group, start, now)}


def _buckets(picked: list[dict], period: str, group: str, start: float, now: float) -> dict:
    items = (buckets.Item(group_key(r, group)[0], r["ts"], r["ts"] + r["latency_s"], r["cost"]["reference"],
                          r["prompt"] + r["completion"]) for r in picked)
    return buckets.aggregate(items, period, start, now, time.strftime("%Z"))


RANGES = {"15m": (900, 1), "1h": (3600, 10), "6h": (21600, 60), "24h": (DAY, 300), "7d": (7 * DAY, 3600), "30d": (30 * DAY, 3600)}


def _spread(buckets: list[float], t0: float, step: int, a: float, b: float, tokens: float) -> None:
    """Adds `tokens` spread uniformly over [a, b) into fixed-width buckets."""
    if tokens <= 0:
        return
    n = len(buckets)
    if b <= a:
        i = int((a - t0) // step)
        if 0 <= i < n:
            buckets[i] += tokens
        return
    density = tokens / (b - a)
    i = max(0, int((a - t0) // step))
    while i < n:
        lo, hi = t0 + i * step, t0 + (i + 1) * step
        if lo >= b:
            break
        overlap = min(hi, b) - max(lo, a)
        if overlap > 0:
            buckets[i] += density * overlap
        i += 1


def _add_exact(buckets: list[float], t0: float, step: int, per_second: dict) -> None:
    n = len(buckets)
    for sec, v in per_second.items():
        i = int((sec - t0) // step)
        if 0 <= i < n:
            buckets[i] += v


def series(rows: list[dict], live_streams: list[dict], rng_name: str, group: str, now: float) -> dict:
    span, step = RANGES.get(rng_name, RANGES["15m"])
    end = int(now // step) * step  # the bucket containing now is incomplete; stop before it
    t0 = end - span
    n = span // step
    t = [t0 + i * step for i in range(n)]
    out: dict[str, dict] = {}

    def slot(r):
        key, name = ("all", "All") if group == "none" else group_key(r, "device" if group == "device" else "model")
        if key not in out:
            out[key] = {"name": name, "decode": [0.0] * n, "prefill": [0.0] * n}
        return out[key]

    for r in rows + live_streams:
        if r["ts"] + r.get("latency_s", 0) < t0 and "buckets" not in r:
            continue
        s = slot(r)
        if "buckets" in r:  # live-simulated requests carry exact per-second counts
            _add_exact(s["decode"], t0, step, r["buckets"]["decode"])
            _add_exact(s["prefill"], t0, step, r["buckets"]["prefill"])
        else:
            _spread(s["prefill"], t0, step, r["ts"], r["ts"] + r["ttft_s"], r["prompt"] - r["cached"])
            _spread(s["decode"], t0, step, r["ts"] + r["ttft_s"], r["ts"] + r["latency_s"], r["completion"])
    for s in out.values():
        s["decode"] = [round(v) for v in s["decode"]]
        s["prefill"] = [round(v) for v in s["prefill"]]
    return {"v": 1, "range": rng_name, "step": step, "t": t, "series": out}


def activity(rows: list[dict], days: int, now: float) -> dict:
    today = datetime.fromtimestamp(now).replace(hour=0, minute=0, second=0, microsecond=0)
    first = today - timedelta(days=days - 1)
    per = defaultdict(lambda: defaultdict(list))  # (date, hour) -> client -> intervals
    for r in rows:
        a, b = r["ts"], r["ts"] + r["latency_s"]
        if b < first.timestamp():
            continue
        cur = a
        while cur < b:
            dt = datetime.fromtimestamp(cur)
            hour_end = dt.replace(minute=0, second=0, microsecond=0) + timedelta(hours=1)
            seg_end = min(b, hour_end.timestamp())
            per[(dt.date().isoformat(), dt.hour)][r["client"]["key"]].append((cur, seg_end))
            cur = seg_end
    out_days = []
    for i in range(days):
        d = (first + timedelta(days=i)).date().isoformat()
        hours = []
        for hr in range(24):
            clients = per.get((d, hr), {})
            all_iv = [iv for ivs in clients.values() for iv in ivs]
            busy = union_seconds(all_iv)
            top = max(clients, key=lambda k: union_seconds(clients[k])) if clients else None
            hours.append({"busy_s": round(busy, 1), "top": top})
        out_days.append({"date": d, "hours": hours})
    used = {r["client"]["key"] for r in rows if r["ts"] >= first.timestamp()}
    clients = {k: {"name": CLIENT_BY_KEY[k]["label"] or CLIENT_BY_KEY[k]["name"], "color": CLIENT_BY_KEY[k]["color"]}
               for k in used if k in CLIENT_BY_KEY}
    tz = time.tzname[time.localtime().tm_isdst]
    try:
        tz = str(Path("/etc/localtime").resolve()).split("zoneinfo/")[1]
    except (IndexError, OSError):
        pass
    return {"v": 1, "slot_s": 3600, "tz": tz, "days": out_days, "clients": clients}


def today_summary(rows: list[dict], now: float) -> dict:
    picked = [r for r in rows if r["ts"] >= period_start("today", now)]
    s = _sum(picked)
    by: dict[str, tuple[list, list, list]] = defaultdict(lambda: ([], [], []))
    for r in picked:
        dec, pre, ttft = by[r["profile"]]
        dec.append((r["completion"], r["decode_tps"]))
        if r["prompt"] - r["cached"] >= speeds.MIN_PREFILL_RATE_TOKENS:
            pre.append((r["prompt"] - r["cached"], r["prefill_tps"]))
        ttft.append(r["ttft_s"])
    return {**{k: s[k] for k in ("requests", "prompt", "cached", "completion", "cost")},
            "speeds": {k: {"decode": speeds.summary(d, 1), "prefill": speeds.summary(p, 0), "ttft": speeds.waits(t, 2)}
                       for k, (d, p, t) in by.items()}}


# A plausible K = 7 draft for a mock whose history has no per-position counters: how often a round still has
# a draft at each depth, and how often the target accepts it there. Lengths 0..7 get DRAFT_HIST's shares.
DRAFT_LENGTH_SHARE = (1.0, 0.93, 0.86, 0.78, 0.70, 0.62, 0.54)
DRAFT_ACCEPT = (0.79, 0.66, 0.55, 0.45, 0.36, 0.28, 0.20)
DRAFT_HIST = (0.04, 0.07, 0.11, 0.16, 0.22, 0.20, 0.13, 0.07)


def speculation(rows: list[dict], period: str, now: float) -> dict:
    """Per-profile draft sums and the accept rate per depth, as GET /api/v1/speculation.

    The generated history carries no draft counters and is Halogen, so any profile with enough output gets
    the shape above; the real view counts only an engine that reports them. Failed and tiny replies are left
    out, as no such request has counters.
    """
    start = period_start(period, now)
    out: dict[str, dict] = {}
    for r in rows:
        prof = PROFILE_BY_NAME.get(r.get("profile") or "")
        if r["ts"] < start or prof is None or r["error"] or (r["completion"] or 0) < 32:
            continue
        rounds = max(1, r["completion"] // 3)
        passes = max(1, rounds // 4)
        b = out.setdefault(prof["name"], {"profile": prof["name"], "engine": prof["engine"], "requests": 0,
                                          "rounds": 0, "passes": 0, "proposed": [0] * 7, "tested": [0] * 7,
                                          "accepted": [0] * 7, "len_hist": [0] * 8, "draft_ms": 0.0,
                                          "generated_tokens": 0})
        b["requests"] += 1
        b["rounds"] += rounds
        b["passes"] += passes
        b["draft_ms"] += round(passes * 2.7, 1)
        b["generated_tokens"] += r["completion"]
        for i, accept in enumerate(DRAFT_ACCEPT):
            proposed = round(rounds * DRAFT_LENGTH_SHARE[i])
            tested = proposed if i == 0 else round(proposed * DRAFT_LENGTH_SHARE[i])
            b["proposed"][i] += proposed
            b["tested"][i] += tested
            b["accepted"][i] += round(tested * accept)
        for length, share in enumerate(DRAFT_HIST):
            b["len_hist"][length] += round(rounds * share)
    out_rows = [{**b, "accept_rate": [round(a / t, 4) if t else None for t, a in zip(b["tested"], b["accepted"])],
                 "ms_per_token": round(b["draft_ms"] / b["generated_tokens"], 3) if b["generated_tokens"] else None}
                for b in sorted(out.values(), key=lambda x: x["profile"])]
    return {"v": 1, "period": period, "from": start, "host": "all", "positions": 7, "rows": out_rows}


def signals(rows: list[dict], days: int, now: float) -> dict:
    """Per local day and engine, as GET /api/v1/signals. The mock has no server error lines: errors are failed rows,
    and a few of them stream errors so the panel shows that column too."""
    out: dict[tuple[str, str], dict] = {}
    for r in rows:
        if r["ts"] < now - days * 86400:
            continue
        key = (time.strftime("%Y-%m-%d", time.localtime(r["ts"])), r.get("engine") or "mock")
        b = out.setdefault(key, {"date": key[0], "engine": key[1], "requests": 0, "errors": 0, "stream_errors": 0,
                                 "client_errors": 0, "tool_call_fallbacks": 0})
        b["requests"] += 1
        b["errors"] += 1 if r["error"] else 0
        b["stream_errors"] += 1 if r["error"] and int(r["ts"]) % 2 == 0 else 0
        b["tool_call_fallbacks"] += 1 if r.get("tool_call_fallback") else 0
        b["client_errors"] += 1 if int(r["ts"]) % 17 == 0 else 0  # refused calls, such as a missing path
    return {"days": days, "host": "all", "rows": sorted(out.values(), key=lambda b: b["date"], reverse=True)}


if __name__ == "__main__":
    n = time.time()
    hist = generate_history(n, make_price_cards(n))
    print(len(hist), "requests;", usage(hist, "30d", "device", n)["totals"])
