"""How busy the gateway was in the last 5 minutes and the last hour: /health last_5m and last_1h.

Memory only, and cheap to read: every series is in time order with a running sum, so a window's count and
sum are a bisect and a subtraction, and a /health call reads no file. Each series keeps one hour (HORIZON_S)
and at most `cap` entries. When the cap drops entries that are still inside the hour, known_since moves up
and the windows report the shorter covered_s rather than a wrong count.

Requests are generation requests only, as `in_flight`. Counts by end time (finished, outcomes, tokens,
TTFT, held_s, rates) take the request's final record. `in_flight` is a step function fed by the gateway
each time hub.active changes. Each step carries the slots of the profile in force at that moment (the
profile of the last forwarded request), so a switch inside a window counts each part against its own slots.
"""
from __future__ import annotations

from bisect import bisect_left, bisect_right

from kiln.gateway.stream import is_generation

HORIZON_S = 3600.0  # the longest window
PRUNE_SLACK_S = 60.0  # drop old entries in batches, at most once a minute of traffic
MAX_ENTRIES = 10_000  # per series and hour; a busy day has about 3,000 requests in its busiest hour
MIN_RATE_TOKENS = 32  # rates of tiny replies are noise (as store/queries.py)
OUTCOMES = ("ok", "client_error", "cancelled", "server_error")
CLIENT_CLOSED = 499


def outcome(status) -> str:
    """2xx ok, 499 cancelled, other 4xx client_error; 5xx and a missing or odd status server_error."""
    if isinstance(status, int) and not isinstance(status, bool):
        if 200 <= status < 300:
            return "ok"
        if status == CLIENT_CLOSED:
            return "cancelled"
        if 400 <= status < 500:
            return "client_error"
    return "server_error"


def _num(v) -> float | None:
    return v if isinstance(v, (int, float)) and not isinstance(v, bool) else None


def _quantile(xs: list[float], p: float) -> float:
    """Linear between the closest ranks of sorted values (numpy's default; the same as store/speeds.py)."""
    i = p * (len(xs) - 1)
    lo = int(i)
    hi = min(lo + 1, len(xs) - 1)
    return xs[lo] + (xs[hi] - xs[lo]) * (i - lo)


def request_rate(rec: dict) -> float | None:
    """Completion tok/s of one request: the tokens after the first stream event over the first to last event
    (the dashboard's estimate when the engine gives no timing, store/queries.py rates)."""
    usage = rec.get("usage") if isinstance(rec.get("usage"), dict) else {}
    comp, first, last = _num(usage.get("completion")), _num(rec.get("ts_first_token")), _num(rec.get("ts_last_token"))
    if not comp or comp < MIN_RATE_TOKENS or first is None or last is None or last <= first:
        return None
    events = _num(rec.get("token_events")) or comp
    return (comp - comp / events) / (last - first)


class _Series:
    """Values in time order with a running sum."""
    __slots__ = ("t", "v", "cum", "base")

    def __init__(self):
        self.t: list[float] = []
        self.v: list = []
        self.cum: list = []
        self.base = 0  # the running sum before the first kept value

    def add(self, t: float, v=1) -> None:
        if self.t and t < self.t[-1]:
            t = self.t[-1]  # a clock step back must not break the order that bisect needs
        self.t.append(t)
        self.v.append(v)
        self.cum.append((self.cum[-1] if self.cum else self.base) + v)

    def drop(self, n: int) -> None:
        if n > 0:
            self.base = self.cum[n - 1]
            del self.t[:n], self.v[:n], self.cum[:n]

    def total(self, i: int):
        if i >= len(self.t):
            return 0
        return self.cum[-1] - (self.cum[i - 1] if i else self.base)


class UsageWindow:
    def __init__(self, now: float, *, cap: int = MAX_ENTRIES):
        self.cap = cap
        self.known_since = now  # before this, the gateway saw nothing (a restart without a seed)
        self._started = _Series()
        self._outcomes = {k: _Series() for k in OUTCOMES}
        self._held, self._ttft, self._rate = _Series(), _Series(), _Series()
        self._prompt, self._completion = _Series(), _Series()
        self._series = (self._started, *self._outcomes.values(), self._held, self._ttft, self._rate,
                        self._prompt, self._completion)
        # in_flight steps: from _pt[i] to _pt[i + 1] the level is _lvl[i] against _slots[i]. The running
        # integrals at each step: request-seconds, request-seconds per slot, seconds at or above the slots.
        self._pt: list[float] = []
        self._lvl: list[int] = []
        self._slots: list[int | None] = []
        self._area: list[float] = []
        self._util: list[float] = []
        self._sat: list[float] = []

    # -- feed --------------------------------------------------------------------------------------
    def started(self, t: float) -> None:
        """A generation request arrived."""
        self._started.add(t)
        self._prune(t)

    def in_flight(self, t: float, level: int, slots: int | None = None) -> None:
        """hub.active has `level` requests from t on. slots: the profile's, when a request was forwarded;
        None keeps the slots in force."""
        if slots is not None and slots <= 0:
            slots = None
        if self._pt:
            t = max(t, self._pt[-1])
            if slots is None:
                slots = self._slots[-1]
            if level == self._lvl[-1] and slots == self._slots[-1]:
                return
            area, util, sat = self._integrals(len(self._pt) - 1, t)
        else:
            area = util = sat = 0.0
        self._pt.append(t)
        self._lvl.append(level)
        self._slots.append(slots)
        self._area.append(area)
        self._util.append(util)
        self._sat.append(sat)
        self._prune(t)

    def finished(self, rec: dict) -> None:
        """A request's final record (as the gateway writes it). Utility calls and odd records are ignored."""
        t = _num(rec.get("ts_end"))
        if t is None or not is_generation(rec.get("path") if isinstance(rec.get("path"), str) else None):
            return
        self._outcomes[outcome(rec.get("status"))].add(t)
        if (held := _num(rec.get("held_s"))) is not None:
            self._held.add(t, held)
        if (ttft := _num(rec.get("ttft_s"))) is not None:
            self._ttft.add(t, ttft)
        if (rate := request_rate(rec)) is not None:
            self._rate.add(t, rate)
        usage = rec.get("usage") if isinstance(rec.get("usage"), dict) else {}
        for key, series in (("prompt", self._prompt), ("completion", self._completion)):
            if isinstance(n := usage.get(key), int) and not isinstance(n, bool):
                series.add(t, n)
        self._prune(t)

    def seed(self, records: list[dict], *, complete: bool, now: float, slots_for) -> None:
        """Rebuild the last hour from record files (oldest end first) at startup, before any live event.
        A record that went to the engine was in flight from its start plus held_s to its end. complete=False
        (the read stopped at its cap) covers only from the oldest record read."""
        since = now - HORIZON_S
        if complete:
            self.known_since = min(self.known_since, since)
        else:
            ends = [e for r in records if (e := _num(r.get("ts_end"))) is not None]
            if ends:
                self.known_since = min(self.known_since, max(min(ends), since))
        starts, steps = [], []  # the cap can only move known_since up from here
        for rec in records:
            end, start = _num(rec.get("ts_end")), _num(rec.get("ts_start"))
            path = rec.get("path") if isinstance(rec.get("path"), str) else None
            if end is None or not since <= end <= now or not is_generation(path):
                continue  # after now only with a clock step back: it has no place in the steps
            self.finished(rec)
            if start is None:
                continue
            if start >= since:
                starts.append(start)
            if "served_model" in rec:
                fwd = min(start + (_num(rec.get("held_s")) or 0.0), end)
                profile = rec.get("profile")
                steps += [(fwd, 1, slots_for(profile) if isinstance(profile, str) else None), (end, -1, None)]
        for t in sorted(starts):
            self._started.add(t)
        level = 0
        for t, delta, slots in sorted(steps, key=lambda s: (s[0], s[1])):  # an end before a start at one time
            level = max(0, level + delta)
            self.in_flight(t, level, slots)

    # -- bounds ------------------------------------------------------------------------------------
    def _prune(self, t: float) -> None:
        old = t - HORIZON_S
        for s in self._series:
            if len(s.t) > self.cap * 1.1:
                n = len(s.t) - self.cap
                self.known_since = max(self.known_since, s.t[n])
                s.drop(n)
            elif s.t and s.t[0] < old - PRUNE_SLACK_S:
                s.drop(bisect_left(s.t, old))
        n = 0
        if len(self._pt) > 2 * self.cap * 1.1:
            n = len(self._pt) - 2 * self.cap
            self.known_since = max(self.known_since, self._pt[n])
        elif self._pt and self._pt[0] < old - PRUNE_SLACK_S:
            n = bisect_right(self._pt, old) - 1  # keep the step in force at the horizon
        if n > 0:
            for lst in (self._pt, self._lvl, self._slots, self._area, self._util, self._sat):
                del lst[:n]

    def size(self) -> int:
        return sum(len(s.t) for s in self._series) + len(self._pt)

    # -- read --------------------------------------------------------------------------------------
    def _integrals(self, i: int, t: float) -> tuple[float, float, float]:
        """The running integrals at time t, inside step i."""
        dt, level, slots = t - self._pt[i], self._lvl[i], self._slots[i]
        return (self._area[i] + level * dt, self._util[i] + (level / slots * dt if slots else 0.0),
                self._sat[i] + (dt if slots and level >= slots else 0.0))

    def _at(self, t: float) -> tuple[tuple[float, float, float], int]:
        """(running integrals, step index) at time t; index -1 before the first step (level 0)."""
        i = bisect_right(self._pt, t) - 1
        if i < 0:
            return ((self._area[0], self._util[0], self._sat[0]) if self._pt else (0.0, 0.0, 0.0)), -1
        return self._integrals(i, t), i

    def summary(self, now: float, window_s: float) -> dict:
        start = max(now - window_s, self.known_since)
        covered = max(0.0, float(now - start))
        (a0, u0, s0), i0 = self._at(start)
        (a1, u1, s1), _ = self._at(max(now, start))
        peak = max([self._lvl[i0] if i0 >= 0 else 0, *self._lvl[i0 + 1:bisect_right(self._pt, now)]])
        idx = {name: bisect_left(s.t, start) for name, s in
               (("held", self._held), ("ttft", self._ttft), ("rate", self._rate),
                ("prompt", self._prompt), ("completion", self._completion))}
        counts = {k: len(s.t) - bisect_left(s.t, start) for k, s in self._outcomes.items()}
        held = self._held.v[idx["held"]:]  # max needs the values; the total is a subtraction
        ttft = sorted(self._ttft.v[idx["ttft"]:])
        rates = sorted(self._rate.v[idx["rate"]:])
        completion = self._completion.total(idx["completion"])
        return {
            "covered_s": round(covered, 1),
            "started": len(self._started.t) - bisect_left(self._started.t, start),
            "finished": sum(counts.values()), **counts,
            "in_flight_mean": round((a1 - a0) / covered, 3) if covered else None,
            "in_flight_peak": peak,
            "utilization_mean": round((u1 - u0) / covered, 3) if covered else None,
            "saturated_s": round(s1 - s0, 1),
            "held_s_total": round(float(self._held.total(idx["held"])), 3),
            "held_s_max": round(max(held), 3) if held else None,
            "ttft_s_p50": round(_quantile(ttft, 0.5), 3) if ttft else None,
            "ttft_s_p95": round(_quantile(ttft, 0.95), 3) if ttft else None,
            "prompt_tokens": self._prompt.total(idx["prompt"]), "completion_tokens": completion,
            "completion_tok_s": round(completion / covered, 1) if covered else None,
            "request_tok_s_p50": round(_quantile(rates, 0.5), 1) if rates else None,
        }
