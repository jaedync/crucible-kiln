"""Mock kiln-dash for frontend work: serves src/kiln/web/static and implements every
endpoint in docs/API.md with synthetic data and a live scenario.

Scenario (repeats): idle, a long-prompt request prefills with Crucible-style
progress (a report every 4,096 tokens from its cached start, as the server
builds it: kiln.live.prefill), decodes around 55 tok/s, a second device joins,
both finish, idle.
An engine switch (stopping -> stopped -> starting -> ready) runs when the UI
asks for one.

    .venv/bin/python tools/mock_dash.py [--port 7070] [--viewer] [--blocked "reason"]
"""
from __future__ import annotations

import argparse
import asyncio
import hashlib
import json
import math
import random
import sys
import tempfile
import time
import uuid
from collections import deque
from itertools import count
from pathlib import Path

from aiohttp import WSMsgType, web

sys.path.insert(0, str(Path(__file__).resolve().parent))
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))
import mock_history as mh  # noqa: E402
from kiln.dash.panel_sync import MSG_MAX_BYTES, PanelSync  # noqa: E402
from kiln.live.panel_screen import Inputs  # noqa: E402
from kiln.live.prefill import Report, Track, add_report, progress_view  # noqa: E402

STATIC = Path(__file__).resolve().parents[1] / "src" / "kiln" / "web" / "static"
STEP_S = 0.1
TICK_S = 0.5
RATE_WINDOW_S = 2.0
PROGRESS_EVERY = 4096  # Crucible's prefill chunk
HEAD_ROWS = 8
REPORT_LAG_S = 0.5  # an engine report reaches the dashboard through the journal about this late
GIB = 1024 ** 3
# Each new stream's conversation draft counts (live/drafts.py), in turn: one for each band of js/draft.js,
# and a new conversation that has none yet.
DRAFTS = ({"accepted": 4120, "proposed": 5000, "turns": 5}, {"accepted": 2730, "proposed": 4000, "turns": 5},
          {"accepted": 540, "proposed": 1000, "turns": 2}, None)
_drafts = count()
# The running turn's drafts (crucible_decode): about this many drafts per output token, and the share of streams
# whose engine reports them during decode (the rest show their conversation's last turns).
TURN_DRAFTS_PER_TOKEN = 0.93
TURN_REPORTING = 0.75


def static_build() -> str:
    h = hashlib.sha256()
    for p in sorted(STATIC.rglob("*")):
        if p.is_file():
            h.update(p.relative_to(STATIC).as_posix().encode())
            h.update(p.read_bytes())
    return h.hexdigest()[:12]


class Stream:
    def __init__(self, client: dict, profile: dict, prompt: int, cached: int, completion: int, now: float):
        self.id = uuid.uuid4().hex[:16]
        self.client = {k: client[k] for k in ("key", "name", "label", "color")}
        self.profile = profile
        self.prompt, self.cached, self.target = prompt, cached, completion
        self.started = now
        self.first_token: float | None = None
        self.tokens = 0
        self.draft = DRAFTS[next(_drafts) % len(DRAFTS)]
        # Cumulative like the engine's counts: they only grow, at a share that drifts around the stream's own.
        self.turn_rate = random.uniform(0.55, 0.86) if random.random() < TURN_REPORTING else None
        self.turn_drafts = (0, 0)  # (accepted, proposed)
        self.frac = 0.0
        self.noise = 1.0
        self.arrivals: deque = deque()
        self.reports = Track()
        self.decode_buckets: dict[int, int] = {}
        self.prefill_buckets: dict[int, int] = {}
        self.prefill_rate = profile["prefill"] * random.uniform(0.95, 1.03)

    @property
    def computed(self) -> int:
        return self.prompt - self.cached

    def tps(self, now: float) -> float:
        if self.first_token is None:
            return 0.0
        while self.arrivals and self.arrivals[0][0] <= now - RATE_WINDOW_S:
            self.arrivals.popleft()
        span = min(RATE_WINDOW_S, now - self.first_token)
        return sum(n for _, n in self.arrivals) / span if span > 0 else 0.0

    def _turn_drafts(self, now: float) -> dict | None:
        if self.turn_rate is None or self.first_token is None:
            return None
        acc, prop = self.turn_drafts
        new = int(self.tokens * TURN_DRAFTS_PER_TOKEN) - prop
        if new > 0:
            share = min(1.0, max(0.0, self.turn_rate + 0.08 * math.sin(now / 7 + self.started)))
            self.turn_drafts = acc, prop = acc + round(new * share), prop + new
        return {"accepted": acc, "proposed": prop}

    def public(self, now: float) -> dict:
        return {"id": self.id, "client": self.client, "model": self.profile["model"], "profile": self.profile["name"],
                "conv": None, "draft": self.draft, "draft_turn": self._turn_drafts(now),
                "started": self.started, "first_token": self.first_token,
                "phase": "decode" if self.first_token else "prefill", "tokens": self.tokens,
                "tps": round(self.tps(now), 1),
                "prompt": {"total": self.prompt, "cached": self.cached} if self.first_token else None,
                "prefill_progress": None if self.first_token else progress_view(
                    self.reports, hint=self.profile["prefill"], chunk=PROGRESS_EVERY, since=self.started)}


class Mock:
    def __init__(self, args):
        self.args = args
        self.started = time.time()
        self.cards = mh.make_price_cards(self.started)
        self.history = mh.generate_history(self.started, self.cards)
        self.active = "halogen-flashnext"
        self.engine_state = {"state": "stopped" if args.night else "ready", "since": self.started - 2 * 3600 - 840,
                             "target": None, "detail": "night: GPU to the fleet" if args.night else ""}
        self.events = [{"ts": self.started - 2 * 3600 - 900, "kind": "stopping", "profile": "halogen-27b", "detail": ""},
                       {"ts": self.started - 2 * 3600 - 840, "kind": "ready", "profile": "halogen-flashnext", "detail": "warm-up generation ok"}]
        self.pool = {"used": 70144, "ts": self.started - 190}
        self.streams: dict[str, Stream] = {}
        self.second: dict[int, list[int]] = {}  # live seconds: [decode, prefill]
        self.clients: set[web.WebSocketResponse] = set()
        self.sids = count(1)
        # The rack panel's shared screen, with the dashboard's own rules (dash/panel_sync.py).
        self.panel = PanelSync(Path(tempfile.gettempdir()) / "kiln-mock-panel.json",
                               lambda msg: self.send_all({"v": 1, **msg}), clock=time.time, mono=time.monotonic)
        self.sent_second = 0
        self.hw = {"tctl_c": 47.0, "edge_c": 44.0, "mem_board_c": 41.0, "power_w": 18.0, "fan_rpm": 0.0}
        self.last = self._row_to_last(self.history[-1])

    # ------------------------------------------------------------------ state
    @property
    def profile(self) -> dict:
        return mh.PROFILE_BY_NAME[self.active]

    def engine(self) -> dict:
        p = self.profile
        halogen = p["engine"] == "halogen"
        return {"profile": p["name"], "label": p["label"], "engine": p["engine"], "model": p["model"],
                **self.engine_state, "blocked": self.args.blocked, "schedule": self.schedule(),
                "kv": {"capacity_positions": p["kv_positions"], "capacity_bytes": p["kv_bytes"],
                       "pool_used": self.pool["used"] if halogen else None,
                       "pool_total": p["kv_positions"] if halogen else None,
                       "pool_ts": self.pool["ts"] if halogen else None}}

    def schedule(self) -> dict | None:
        if not self.args.night:
            return None
        now = time.time()
        return {"day": "09:00-18:00", "tz": "UTC", "in_day": False, "next_change": now + 5 * 3600,
                "day_profile": "crucible-flashnext", "holds": ["schedule"], "drain_timeout_s": 900,
                "dev_jobs": [{"pid": 4242, "unit": None, "cmd": "gpu-job -- timeout 300 tools/bench"}]}

    def hw_sample(self, now: float) -> dict:
        busy = bool(self.streams)
        prefilling = any(s.first_token is None for s in self.streams.values())
        target = (74, 70, 52, 118, 2100) if prefilling else (69, 65, 50, 98, 1800) if busy else (47, 44, 41, 18, 0)
        for key, tgt in zip(("tctl_c", "edge_c", "mem_board_c", "power_w", "fan_rpm"), target):
            # Physical quantities do lag; a first-order approach is the honest model here.
            self.hw[key] += (tgt - self.hw[key]) * (0.08 if key != "power_w" else 0.5)
        ready = self.engine_state["state"] == "ready"
        gtt = (self.profile["memory_gib"] * 0.93 if ready else 0.6) * GIB
        return {"ts": now, "tctl_c": round(self.hw["tctl_c"] + random.uniform(-.4, .4), 1),
                "edge_c": round(self.hw["edge_c"] + random.uniform(-.3, .3), 1),
                "mem_board_c": round(self.hw["mem_board_c"], 1), "power_w": round(self.hw["power_w"] + random.uniform(-2, 2), 1),
                "fan_rpm": int(self.hw["fan_rpm"]), "gpu_busy_pct": random.randint(96, 100) if busy else random.randint(0, 2),
                "gtt_used_bytes": int(gtt), "gtt_total_bytes": 133143986176,
                "vram_used_bytes": 268435456, "vram_total_bytes": 536870912,
                "sclk_mhz": 2900 if busy else 600,
                "ram_avail_bytes": int((124 - gtt / GIB - 5.5) * GIB), "profile": "balanced"}

    def live(self, now: float) -> dict:
        streams = [s.public(now) for s in self.streams.values()]
        exact = self.profile["exact"]
        note = None if exact else "Gufo streams several tokens per event; live counts use the measured ratio (1.38 tokens/event)."
        return {"decode_tps": round(sum(s["tps"] for s in streams), 1), "decode_exact": exact, "decode_note": note,
                "in_flight": len(streams), "slots": self.profile["slots"], "streams": streams}

    def tick(self, now: float) -> dict:
        t = int(now) - 1
        dec, pre = self.second.get(t, [0, 0])
        # Every second completed since the previous tick (usually one, none, or several on lag).
        first = max(self.sent_second + 1, t - 10) if self.sent_second else t
        seconds = [{"t": x, "decode": self.second.get(x, [0, 0])[0], "prefill": self.second.get(x, [0, 0])[1]}
                   for x in range(first, t + 1)]
        self.sent_second = max(self.sent_second, t)
        all_rows = self.history
        return {"v": 1, "type": "tick", "ts": now, "live": self.live(now),
                "second": {"t": t, "decode": dec, "prefill": pre}, "seconds": seconds, "last": self.last,
                "engine": self.engine(), "hw": self.hw_sample(now), "today": mh.today_summary(all_rows, now),
                "health": {"gateway": "up", "dash_started": self.started, "parse_errors_24h": self.args.parse_errors,
                           "parse_error_last_ts": now - 7200 if self.args.parse_errors else None,
                           "last_request_ts": max((r["ts"] + r["latency_s"] for r in all_rows[-50:]), default=None)}}

    def snapshot(self, now: float) -> dict:
        sent = self.sent_second
        snap = self.tick(now)
        self.sent_second = sent  # a snapshot must not consume seconds owed to other clients' ticks
        snap.pop("seconds")
        s = mh.series(self.history, self.live_buckets(), "15m", "none", now)
        agg = s["series"].get("all", {"decode": [0] * len(s["t"]), "prefill": [0] * len(s["t"])})
        snap.update({"type": "snapshot", "series_1s": {"t": s["t"], "decode": agg["decode"], "prefill": agg["prefill"]},
                     "profiles": [mh.public_profile(p, self.active) for p in mh.PROFILES],
                     "me": self.me(), "build": static_build(),
                     "pricing": {"reference": "Claude Sonnet 5.5", "input_per_m": 2.0, "cached_per_m": 0.1,
                                 "output_per_m": 10.0}})
        return snap

    def me(self) -> dict:
        return {"can_act": not self.args.viewer, "identity": "owner-phone" if self.args.viewer else "owner-laptop"}

    def live_buckets(self) -> list[dict]:
        return [{"ts": s.started, "client": s.client, "model": s.profile["model"],
                 "buckets": {"decode": s.decode_buckets, "prefill": s.prefill_buckets}} for s in self.streams.values()]

    @staticmethod
    def _row_to_last(r: dict) -> dict:
        return {"ts": r["ts"] + r["latency_s"], "ttft_s": r["ttft_s"], "decode_tps": r["decode_tps"],
                "prefill_tps": r["prefill_tps"], "prompt": r["prompt"], "cached": r["cached"],
                "completion": r["completion"], "finish": r["finish"], "think_close": r["think_close"],
                # The generated history is Halogen without draft counts; this shows the block's draft row.
                "draft_accepted": round(r["completion"] * 0.73), "draft_proposed": r["completion"]}

    # -------------------------------------------------------------- broadcast
    async def send_all(self, msg: dict) -> None:
        data = json.dumps(msg, separators=(",", ":"))
        for ws in list(self.clients):
            try:
                await ws.send_str(data)
            except Exception:  # a closed browser tab must not break the loop for others
                self.clients.discard(ws)

    # ------------------------------------------------------------- simulation
    def step(self, now: float) -> list[dict]:
        out = []
        decoding = [s for s in self.streams.values() if s.first_token]
        share = 1.0 if len(decoding) <= 1 else 0.74
        for s in list(self.streams.values()):
            if s.first_token is None:
                done = (now - s.started) * s.prefill_rate
                seen = (now - REPORT_LAG_S - s.started) * s.prefill_rate  # what the journal has delivered
                # Crucible resumes a cached prompt with an 8-row head chunk, then chunks of 4,096.
                head = HEAD_ROWS if s.cached else 0
                if s.profile["progress"] and seen >= min(head or PROGRESS_EVERY, s.computed):
                    rows = min(s.computed, head + int((seen - head) // PROGRESS_EVERY) * PROGRESS_EVERY or s.computed)
                    elapsed = rows / s.prefill_rate
                    s.reports = add_report(s.reports, Report(s.cached + rows, s.prompt, round(elapsed + 0.06, 2),
                                                             s.started + elapsed), rate=s.profile["prefill"],
                                           chunk=PROGRESS_EVERY)
                # Live prefill counts as it happens (live/rates.py), never back-filled when it ends.
                n = min(s.prefill_rate * STEP_S, max(0.0, s.computed - sum(s.prefill_buckets.values())))
                s.prefill_buckets[int(now)] = s.prefill_buckets.get(int(now), 0) + n
                self.second.setdefault(int(now), [0, 0])[1] += n
                if done >= s.computed:
                    s.first_token = now
                    s.reports = Track()
                continue
            s.noise = min(1.06, max(0.94, s.noise + random.uniform(-0.01, 0.01)))
            s.frac += s.profile["decode"] * share * s.noise * STEP_S
            n = int(s.frac)
            if n:
                s.frac -= n
                n = min(n, s.target - s.tokens)
                s.tokens += n
                s.arrivals.append((now, n))
                sec = int(now)
                s.decode_buckets[sec] = s.decode_buckets.get(sec, 0) + n
                self.second.setdefault(sec, [0, 0])[0] += n
            if s.tokens >= s.target:
                out.append(self._finish(s, now))
        return out

    def _finish(self, s: Stream, now: float, finish: str = "stop", status: int = 200, error: str | None = None) -> dict:
        self.streams.pop(s.id, None)
        ttft = (s.first_token or now) - s.started
        decode_s = now - s.first_token if s.first_token else 0
        row = mh.build_request(random.Random(), self.cards, s.started, s.client, s.profile)
        row.update({"id": s.id, "prompt": s.prompt, "cached": s.cached, "completion": s.tokens,
                    "reasoning": int(s.tokens * 0.3), "ttft_s": round(ttft, 3), "latency_s": round(now - s.started, 2),
                    "decode_tps": round(s.tokens / decode_s, 1) if decode_s else None,
                    "prefill_tps": round(s.computed / ttft, 1) if s.first_token else None,
                    "finish": finish, "status": status, "error": error,
                    "decode_from": s.first_token, "decode_to": now if s.first_token else None,
                    "think_close": None, "buckets": {"decode": s.decode_buckets, "prefill": s.prefill_buckets},
                    "cost": mh.price(self.cards, s.profile["model"], s.started, s.prompt, s.cached, s.tokens)})
        self.history.append(row)
        if status == 200:
            self.last = self._row_to_last(row)
            self.pool = {"used": min(s.profile["kv_positions"], s.prompt + s.tokens + 4096), "ts": now}
        public = {k: v for k, v in row.items() if k != "buckets"}
        return {"v": 1, "type": "request", "request": public}

    def direct_request(self, now: float) -> list[dict]:
        """Traffic that bypassed the gateway: known only from the engine log at its end. Its tokens go to the
        history (the longer ranges); the live chart's drawn seconds never change."""
        row = mh.build_request(random.Random(), self.cards, now, mh.DIRECT, self.profile, "direct")
        start = now - row["latency_s"]
        row["ts"] = start
        pre: dict[int, int] = {}
        dec: dict[int, int] = {}
        for buckets, a, b, n in ((pre, start, start + row["ttft_s"], row["prompt"] - row["cached"]),
                                 (dec, start + row["ttft_s"], now, row["completion"])):
            for sec in range(int(a), int(b) + 1):
                overlap = min(sec + 1, b) - max(sec, a)
                if overlap > 0:
                    buckets[sec] = round(n * overlap / (b - a))
        row["buckets"] = {"decode": dec, "prefill": pre}
        self.history.append(row)
        public = {k: v for k, v in row.items() if k != "buckets"}
        return [{"v": 1, "type": "request", "request": public}]

    def start(self, client_key: str, prompt: int, cached: int, completion: int) -> Stream:
        s = Stream(mh.CLIENT_BY_KEY[client_key], self.profile, prompt, cached, completion, time.time())
        self.streams[s.id] = s
        return s

    # ---------------------------------------------------------------- engine
    async def switch(self, target: str) -> None:
        old = self.profile
        for s in list(self.streams.values()):
            await self.send_all(self._finish(s, time.time(), finish=None, status=502, error="engine stopped during switch"))
        await self._engine_step("stopping", target, f"stopping kiln-{old['name']}", 3.0)
        await self._engine_step("stopped", target, "waiting for GPU memory to be released", 2.0)
        self.active = target
        new = self.profile
        for line, wait in [(f"starting kiln-{target}", 1.5), (f"loading weights {new['memory_gib'] - 8} GiB", 2.0),
                           ("weights loaded, allocating KV cache", 1.5), ("warm-up generation", 2.0)]:
            await self._engine_step("starting", target, line, wait)
        self.pool = {"used": 0, "ts": time.time()}
        await self._engine_step("ready", None, "", 0, log=f"ready: {new['label']}")

    async def _engine_step(self, state: str, target: str | None, detail: str, wait: float, log: str | None = None) -> None:
        now = time.time()
        self.engine_state = {"state": state, "since": now, "target": target, "detail": detail}
        self.events.append({"ts": now, "kind": state, "profile": self.active, "detail": detail})
        await self.send_all({"v": 1, "type": "engine", "engine": self.engine(), "log": log or detail or state})
        if wait:
            await asyncio.sleep(wait)


# -------------------------------------------------------------------- loops
async def sim_loop(m: Mock) -> None:
    while True:
        now = time.time()
        for msg in m.step(now):
            await m.send_all(msg)
        await asyncio.sleep(STEP_S)


async def tick_loop(m: Mock) -> None:
    while True:
        await asyncio.sleep(TICK_S)
        await m.panel.tick(Inputs(failed=m.engine_state["state"] == "failed", edge_c=m.hw.get("edge_c")))
        await m.send_all(m.tick(time.time()))


async def wait_until(m: Mock, cond, timeout: float = 300) -> bool:
    end = time.time() + timeout
    while time.time() < end:
        if m.engine_state["state"] != "ready":
            return False
        if cond():
            return True
        await asyncio.sleep(0.2)
    return False


async def scenario_loop(m: Mock) -> None:
    await asyncio.sleep(m.args.idle)
    while True:
        if m.engine_state["state"] != "ready":
            await asyncio.sleep(1)
            continue
        a = m.start("198.51.100.10", prompt=56994, cached=0, completion=2600)
        if await wait_until(m, lambda: a.first_token is not None):
            await asyncio.sleep(10)
            if m.engine_state["state"] == "ready":
                b = m.start("198.51.100.22", prompt=98000, cached=92000, completion=900)  # an agent turn
                await wait_until(m, lambda: b.id not in m.streams)
            await wait_until(m, lambda: a.id not in m.streams)
        await asyncio.sleep(m.args.gap / 2)
        if m.engine_state["state"] == "ready":
            for msg in m.direct_request(time.time()):
                await m.send_all(msg)
        await asyncio.sleep(m.args.gap / 2)


# --------------------------------------------------------------------- HTTP
def j(data: dict, status: int = 200) -> web.Response:
    return web.json_response({"v": 1, **data}, status=status)


def routes(m: Mock) -> web.RouteTableDef:
    r = web.RouteTableDef()

    @r.get("/api/v1/live")
    async def live(request):
        ws = web.WebSocketResponse(heartbeat=20, max_msg_size=MSG_MAX_BYTES)
        await ws.prepare(request)
        me = {**m.me(), "sid": next(m.sids)}
        client = m.panel.client(me, local=False, same_origin=True)
        await ws.send_str(json.dumps({**m.snapshot(time.time()), "me": me, "panel": m.panel.view()}))
        m.clients.add(ws)
        try:
            async for msg in ws:
                if msg.type == WSMsgType.ERROR:
                    break
                reply = await m.panel.receive(client, msg.data)
                if reply is not None:
                    await ws.send_str(json.dumps({"v": 1, **reply}))
        finally:
            m.clients.discard(ws)
        return ws

    @r.get("/api/v1/state")
    async def state(request):
        return j(m.snapshot(time.time()))

    @r.get("/api/v1/usage")
    async def usage(request):
        period = request.query.get("period", "today")
        group = request.query.get("group", "device")
        if period not in ("today", "7d", "30d", "all") or group not in ("device", "model", "profile"):
            return j({"error": "period must be today|7d|30d|all and group device|model|profile"}, 400)
        return j(mh.usage(m.history, period, group, time.time()))

    @r.get("/api/v1/series")
    async def series(request):
        rng = request.query.get("range", "15m")
        group = request.query.get("group", "none")
        if rng not in mh.RANGES or group not in ("none", "device", "model"):
            return j({"error": "unknown range or group"}, 400)
        return j(mh.series(m.history, m.live_buckets(), rng, group, time.time()))

    @r.get("/api/v1/requests")
    async def requests(request):
        try:
            limit = max(1, min(200, int(request.query.get("limit", 50))))
            before = float(request.query["before"]) if "before" in request.query else None
        except ValueError:
            return j({"error": "limit and before must be numbers"}, 400)
        rows = [x for x in reversed(m.history) if before is None or x["ts"] < before][:limit]
        return j({"rows": [{k: v for k, v in x.items() if k != "buckets"} for x in rows]})

    @r.get("/api/v1/activity")
    async def activity(request):
        days = max(1, min(90, int(request.query.get("days", 31))))
        return j(mh.activity(m.history, days, time.time()))

    @r.get("/api/v1/signals")
    async def signals(request):
        days = max(1, min(90, int(request.query.get("days", 14))))
        return j(mh.signals(m.history, days, time.time()))

    @r.get("/api/v1/speculation")
    async def speculation(request):
        period = request.query.get("period", "today")
        if period not in ("today", "7d", "30d", "all"):
            return j({"error": "period must be today|7d|30d|all"}, 400)
        return j(mh.speculation(m.history, period, time.time()))

    @r.get("/api/v1/cache")
    async def cache(_request):
        now = time.time()

        def turn(i, prompt, cached, appended, reread, cause, **kw):
            base = {"ts_start": now - 900 + i * 60, "profile": "crucible-flashnext", "prompt": prompt, "cached": cached,
                    "completion": 400, "ttft_s": 1.2 + (prompt - cached) / 1500, "n_messages": 3 + 2 * i,
                    "prefix_msgs_kept": 1 + 2 * i, "prev_n_messages": 1 + 2 * i, "system_changed": False,
                    "tools_changed": False, "new_conversation": cause == "new", "appended_tokens": appended,
                    "reread_tokens": reread, "cache_save_refused": None, "cache_session_label": None,
                    "cache_entries": None, "cache_bytes": None, "cache_evictions": None, "host_free_mib": None,
                    "replay_hit": None, "closest_shared_tokens": None, "closest_outcome": None, "cause": cause}
            return {**base, **kw}
        turns = [turn(0, 12000, 0, None, None, "new"), turn(1, 30000, 12000, 18000, 0, "ok"),
                 turn(2, 53248 + 9000, 53248, 1200, 7800, "engine", cache_save_refused="host",
                      cache_session_label="hit_session"),
                 turn(3, 96000, 53248, 1500, 41252, "engine", cache_save_refused="host"),
                 turn(4, 40000, 8000, None, None, "client", prefix_msgs_kept=3, prev_n_messages=9)]
        sess = {"conv": "a1b2c3d4e5f6", "client": {"key": "198.51.100.10", "name": "laptop",
                                                   "color": "#1f4fa3"}, "turns": turns, "last": now,
                "reread": 49052, "flagged": 2, "client_changes": 1, "refusals": 2}
        return j({"flag_tokens": 4096, "sessions": [sess], "daily": [
            {"date": "2026-01-15", "profile": "crucible-flashnext", "turns": 5, "computed": 120000, "reread": 49052,
             "engine_misses": 2, "client_changes": 1, "refusals": 2}]})

    @r.get("/api/v1/prices")
    async def prices(request):
        current = {}
        for c in sorted(m.cards, key=lambda c: c["effective_from"]):
            current[(c["source"], c["model"])] = c
        return j({"current": list(current.values()), "history": m.cards})

    @r.get("/api/v1/engines")
    async def engines(request):
        return j({"profiles": [mh.public_profile(p, m.active) for p in mh.PROFILES], "engine": m.engine(),
                  "events": m.events[-20:], "blocked": m.args.blocked})

    @r.get("/api/v1/panel")
    async def panel(request):
        now = time.time()
        return j({"v": 1, "ts": now, "fleet": {
            "lease": {"agent": "job-c", "kind": "gates", "prio": 2, "elapsed_s": round(now % 300, 1),
                      "limit_s": 360.0},
            "queue": [{"agent": "job-a", "prio": 1, "waited_s": 142.0},
                      {"agent": "job-b", "prio": 2, "waited_s": 61.0}],
            "holds": [], "holds_total": 0, "recent": [{"agent": "job-e", "kind": "bench", "prio": 1, "rc": 0, "held_s": 189.0,
                                     "ago_s": 40.0}],
            # A night driver's plan, so the panel's plan tiles have something to draw in a dev session.
            "plan": {"night": "2026-01-15", "updated_ago_s": 60.0, "items": [
                {"id": "check-1", "label": "First check", "state": "pass", "expect_min": 35, "elapsed_s": None,
                 "held_s": 1980.0, "rc": 0},
                {"id": "check-2", "label": "Second check", "state": "running", "expect_min": 20,
                 "elapsed_s": round(now % 900, 1), "held_s": None, "rc": None},
                {"id": "long-run", "label": "Long run", "state": "waiting", "expect_min": 45, "elapsed_s": None,
                 "held_s": None, "rc": None},
                {"id": "short-test", "label": "Short test", "state": "fail", "expect_min": 15, "elapsed_s": None,
                 "held_s": 900.0, "rc": 1}]}}})

    @r.get("/api/v1/me")
    async def me(request):
        return j(m.me())

    @r.post("/api/v1/engines/switch")
    async def switch(request):
        origin = request.headers.get("Origin")
        if request.headers.get("X-Kiln-Action") != "1" or (origin and origin != f"{request.scheme}://{request.host}"):
            return j({"error": "missing X-Kiln-Action header or cross-origin request"}, 403)
        if m.args.viewer:
            return j({"error": "owner-phone is not in actions.allow"}, 403)
        if m.args.blocked:
            return j({"error": m.args.blocked}, 409)
        try:
            body = await request.json()
            target = body["profile"]
            if m.args.night and body.get("confirm") is not True:
                return j({"error": "another GPU job is running (pid 4242 gpu-job); confirm to pause"
                                   " the fleet and wait up to 15 min for it to finish", "confirm": True}, 409)
        except Exception:
            return j({"error": "body must be {\"profile\": \"<name>\"}"}, 400)
        if target not in mh.PROFILE_BY_NAME:
            return j({"error": f"unknown profile {target}"}, 400)
        if m.engine_state["state"] not in ("ready", "stopped") or m.engine_state["target"]:
            return j({"error": "a switch is already in progress"}, 409)
        if target == m.active:
            return j({"error": f"{target} is already active"}, 409)
        asyncio.get_running_loop().create_task(m.switch(target))
        return j({"accepted": True}, 202)

    return r


@web.middleware
async def no_cache(request, handler):
    resp = await handler(request)
    if not request.path.startswith("/api/"):
        resp.headers["Cache-Control"] = "no-cache"
    return resp


async def index(request):
    return web.FileResponse(STATIC / "index.html")


async def panel_page(request):
    return web.FileResponse(STATIC / "panel.html")


def make_app(args) -> web.Application:
    m = Mock(args)
    app = web.Application(middlewares=[no_cache])
    app.add_routes(routes(m))
    app.router.add_get("/", index)
    app.router.add_get("/panel", panel_page)
    app.router.add_static("/", STATIC)

    async def start_loops(app):
        app["tasks"] = [asyncio.create_task(f(m)) for f in (sim_loop, tick_loop, scenario_loop)]

    async def stop_loops(app):
        for t in app["tasks"]:
            t.cancel()
        for ws in list(m.clients):
            await ws.close()

    app.on_startup.append(start_loops)
    app.on_shutdown.append(stop_loops)
    return app


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--host", default="127.0.0.1")
    ap.add_argument("--port", type=int, default=7070)
    ap.add_argument("--viewer", action="store_true", help="me.can_act = false")
    ap.add_argument("--night", action="store_true", help="night window: engine stopped, one development GPU job")
    ap.add_argument("--blocked", default=None, help='engine switching blocked, e.g. "evaluation running (bench-hard)"')
    ap.add_argument("--parse-errors", type=int, default=0)
    ap.add_argument("--idle", type=float, default=8.0, help="seconds of idle before the first request")
    ap.add_argument("--gap", type=float, default=45.0, help="idle seconds between scenario runs")
    args = ap.parse_args()
    web.run_app(make_app(args), host=args.host, port=args.port, print=lambda *_: print(f"kiln mock on http://{args.host}:{args.port}/"))


if __name__ == "__main__":
    main()
