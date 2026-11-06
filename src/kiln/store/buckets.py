"""Time buckets for the usage bars (/usage `buckets`): local clock hours or local calendar days, in the
server's zone like the activity heatmap. A request's cost and tokens count in the bucket where it started,
as the usage period itself selects requests by start, so those bars add up to the table's totals. Busy
time is split at bucket edges: the part of a request inside a bucket is busy there."""
from __future__ import annotations

import bisect
import time
from collections import defaultdict
from typing import Iterable, NamedTuple

HOUR = 3600
DAY = 86400
STEPS = {"today": HOUR, "7d": HOUR, "30d": DAY, "all": DAY}


def hour_floor(t: float) -> float:
    lt = time.localtime(t)
    return t - (lt.tm_min * 60 + lt.tm_sec + t % 1)


def day_floor(t: float) -> float:
    lt = time.localtime(t)
    return time.mktime((lt.tm_year, lt.tm_mon, lt.tm_mday, 0, 0, 0, 0, 0, -1))


def _next_day(t: float) -> float:
    # Noon of the day, plus a day, back to midnight: a 23 or 25 hour day (daylight saving) still steps once.
    lt = time.localtime(t + 12 * HOUR)
    return time.mktime((lt.tm_year, lt.tm_mon, lt.tm_mday + 1, 0, 0, 0, 0, 0, -1))


def edges(period: str, start: float, now: float, first: float | None = None) -> list[float]:
    """Bucket start times, oldest first. Today shows the whole day (its later hours stay empty); the other
    periods run from the bucket holding their start (`first`, the oldest request, for "all") to now's."""
    if period == "today":
        end = _next_day(start)
        return [float(t) for t in range(int(start), int(end), HOUR)]
    if STEPS[period] == HOUR:
        out, t, last = [], hour_floor(start), hour_floor(now)
        while t <= last:
            out.append(t)
            t += HOUR
        return out
    if period == "all":  # from the oldest request's day; none yet is today alone
        start = now if first is None else first
    out, t, last = [], day_floor(start), day_floor(now)
    while t <= last:
        out.append(t)
        t = _next_day(t)
    return out


def index(starts: list[float], ts: float) -> int | None:
    i = bisect.bisect_right(starts, ts) - 1
    return i if i >= 0 else None


class Item(NamedTuple):
    key: str
    ts_start: float
    ts_end: float | None
    usd: float
    tokens: int


def _union(intervals: list[tuple[float, float]]) -> float:
    total, cur_s, cur_e = 0.0, None, None
    for s, e in sorted(intervals):
        if cur_e is None or s > cur_e:
            if cur_e is not None:
                total += cur_e - cur_s
            cur_s, cur_e = s, e
        else:
            cur_e = max(cur_e, e)
    return total + (cur_e - cur_s if cur_e is not None else 0.0)


def aggregate(items: Iterable[Item], period: str, start: float, now: float, tz: str) -> dict:
    """The /usage `buckets` view: per row key and bucket, cost, tokens and busy seconds; per bucket,
    requests and busy seconds of all keys at once (overlapping requests count once)."""
    items = list(items)
    starts = edges(period, start, now, min((it.ts_start for it in items), default=None))
    n, step = len(starts), STEPS[period]
    ends = starts[1:] + ([starts[-1] + HOUR if step == HOUR else _next_day(starts[-1])] if starts else [])
    cost: dict[str, list[float]] = defaultdict(lambda: [0.0] * n)
    tokens: dict[str, list[int]] = defaultdict(lambda: [0] * n)
    spans: dict[tuple[str, int], list] = defaultdict(list)
    requests = [0] * n
    for it in items:
        i = index(starts, it.ts_start)
        if i is None:
            continue
        requests[i] += 1
        cost[it.key][i] += it.usd
        tokens[it.key][i] += it.tokens
        j = i
        while it.ts_end is not None and j < n and starts[j] < it.ts_end:
            spans[(it.key, j)].append((max(it.ts_start, starts[j]), min(it.ts_end, ends[j])))
            j += 1
    busy: dict[str, list[float]] = defaultdict(lambda: [0.0] * n)
    by_bucket: dict[int, list] = defaultdict(list)
    for (key, j), ivs in spans.items():
        busy[key][j] = round(_union(ivs), 1)
        by_bucket[j].extend(ivs)
    fmt = "%Y-%m-%d %H:00" if step == HOUR else "%Y-%m-%d"
    return {"step_s": step, "tz": tz, "t": starts, "local": [time.strftime(fmt, time.localtime(t)) for t in starts],
            "cost": {k: [round(v, 6) for v in vs] for k, vs in cost.items()}, "tokens": dict(tokens),
            "busy": dict(busy), "busy_all": [round(_union(by_bucket[j]), 1) for j in range(n)],
            "requests": requests}
