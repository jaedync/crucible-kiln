"""kiln-dash: ingest, live state, REST and WebSocket API, static UI, engine manager.

All SQLite work happens on the event loop between awaits, so each batch is one
transaction that never interleaves with another.
"""
from __future__ import annotations

import asyncio
import hashlib
import ipaddress
import itertools
import json
import re
import sqlite3
import time
from functools import partial
from urllib.parse import urlsplit
from importlib import resources
from pathlib import Path

from aiohttp import WSMsgType, web

from kiln import SCHEMA_VERSION, netguard
from kiln.collect.hw import Sensors
from kiln.collect.memory import MemorySampler, start_facts as memory_start_facts
from kiln.collect import engines, spread
from kiln.collect.ingest import Ingestor
from kiln.collect.sources import GatewayFeed, GatewayFiles, JournalFollower
from kiln.config import Config, bind_host, describe_addresses, local_host, url_host
from kiln.dash.panel_sync import MSG_MAX_BYTES, PanelSync, tick_inputs
from kiln.engines.manager import Manager, SwitchRefused
from kiln.engines.state import StateFile
from kiln.gateway.attribution import Whois, normalize_ip, tailscale_whois
from kiln.gateway.prefix import FORGET_AFTER_S
from kiln.live.drafts import ConvDrafts
from kiln.live.rates import LiveState
from kiln.pricing import openrouter
from kiln.pricing.cards import PriceBook
from kiln.store import cache_queries, devices, queries, signals
from kiln.store.db import connect
from kiln.store.reader import Reader

TICK_S = 0.5
# A prefill report ticks at once (no later than this after the last tick), so the browser gets a cold
# prompt's size and each chunk without waiting up to TICK_S on top of the journal's own delay.
EARLY_TICK_MIN_S = 0.1
BROWSER_QUEUE = 400  # about three minutes of ticks; a browser further behind is disconnected
HW_EVERY_S = 1.0
HW_STORE_EVERY_S = 10.0
# RAM by part and the engine's disk use (collect.memory): it walks /proc and a directory, so off the loop.
MEM_EVERY_S = 10.0
# An engine that printed no sizes since the dashboard started: look its start lines up this often.
MEM_SEED_EVERY_S = 600.0
SETTLE_EVERY_S = 1.0
TODAY_MIN_S = 2.0  # the today refresh: at most this often, though requests end faster
TODAY_MAX_S = 30.0  # and at least this often: changes nothing marks
MIDNIGHT_LAG_S = 0.1  # and this long after each local midnight, for the new day's start
BLOCKED_EVERY_S = 5.0
ENGINE_CACHE_EVERY_S = 10.0
COLOUR_REBALANCE_S = 6 * 3600  # client colours follow who has been busy (queries.rebalance_colors)
# The engine card's log lists switches and their steps, not ingest diagnostics.
SWITCH_KINDS_SQL = "'switch_requested', 'stopping', 'stopped', 'starting', 'ready', 'failed', 'reconcile'"
PANEL_CACHE_S = 1.0  # many panel clients poll /api/v1/panel; the files are read once a second at most
# Day-level history (the cache panel's daily table, server signals) changes slowly; every open dashboard
# polls it, so one query serves them all for this long.
HISTORY_MEMO_S = 20.0
MAX_OPEN_CONVS = 30
CONV_ID = re.compile(r"[A-Za-z0-9_-]{1,64}")
# Halogen's GET /cache counters (cumulative since the engine started) shown on the engine card.
HALOGEN_CACHE_KEYS = ("entries", "max_entries", "bytes", "hits", "misses", "stores", "evicted", "refused",
                      "prompt_tokens_saved")


def _signals_view(conn, days: int, host: str | None, now: float) -> dict:
    return signals.view(conn, days, now, host)


def halogen_cache_counters(d) -> dict | None:
    if not isinstance(d, dict):
        return None
    out = {k: d[k] for k in HALOGEN_CACHE_KEYS if isinstance(d.get(k), int) and not isinstance(d.get(k), bool)}
    return out or None
ACTION_HEADER = "X-Kiln-Action"
TAILNET_RETRY_S = 30.0
LOCAL = {"127.0.0.1", "::1"}


def static_dir() -> Path:
    return Path(str(resources.files("kiln.web") / "static"))


async def revalidate_files(_request: web.Request, response: web.StreamResponse) -> None:
    """Every file (scripts, styles, fonts) is checked again on each load; an unchanged one costs a 304.
    Without Cache-Control a browser may reuse a cached file for hours (10% of its age since Last-Modified),
    so a page that reloads on a new build kept running the old scripts."""
    if isinstance(response, web.FileResponse):
        response.headers.setdefault("Cache-Control", "no-cache")


def csp(host: str | None) -> str:
    """Everything the UI loads is its own (scripts, styles, fonts, the live WebSocket). It must not run in a
    frame: a framed switch button invites clickjacking. 'self' covers ws: only in newer browsers, so the
    socket's own origin is named; the guard has checked the name, and an odd one keeps 'self' alone."""
    name = netguard.host_name(host)
    ws = f" ws://{host} wss://{host}" if name and host and all(c.isalnum() or c in ".-:[]" for c in host) else ""
    # Style attributes: the UI sets colours and sizes per element (dom.js h() and uPlot). A browser that knows
    # style-src-elem still refuses inline <style> elements; no script runs inline.
    return ("default-src 'self'; img-src 'self' data:; object-src 'none'; base-uri 'none'; form-action 'self';"
            " style-src 'self' 'unsafe-inline'; style-src-elem 'self';"
            f" connect-src 'self'{ws}; frame-ancestors 'none'")


async def security_headers(request: web.Request, response: web.StreamResponse) -> None:
    response.headers.setdefault("X-Content-Type-Options", "nosniff")
    response.headers.setdefault("Content-Security-Policy", csp(request.host))
    response.headers.setdefault("X-Frame-Options", "DENY")


@web.middleware
async def plain_not_found(request: web.Request, handler):
    """A path with no route or file: a short text 404. aiohttp's own came with no body and the type
    application/octet-stream, which Chrome shows as ERR_INVALID_RESPONSE instead of "not found"."""
    missing = web.Response(status=404, text=f"404 Not found: {request.path} is not on the Kiln dashboard"
                           " (the gateway's /health is on port 8000)\n")
    # The static route answers a missing file with a FileResponse that turns into the empty 404 only as it
    # is sent, so look for the file first. A path outside the folder is left to aiohttp, which refuses it.
    if isinstance(request.match_info.route.resource, web.StaticResource):
        root = static_dir().resolve()
        target = (root / request.match_info.get("filename", "")).resolve()
        if target.is_relative_to(root) and not target.is_file():
            return missing
    try:
        return await handler(request)
    except web.HTTPNotFound:
        return missing


def switch_events(conn) -> list[dict]:
    return [dict(r) for r in conn.execute(
        f"SELECT ts, kind, profile, detail FROM engine_events WHERE kind IN ({SWITCH_KINDS_SQL})"
        " ORDER BY ts DESC LIMIT 50")]


def build_hash() -> str:
    """Changes whenever the server code or the static files change; the page reloads on a new build."""
    h = hashlib.sha256()
    pkg = Path(__file__).resolve().parents[1]
    for p in sorted(pkg.rglob("*")):
        if p.is_file() and p.suffix in (".py", ".js", ".css", ".html", ".sql", ".svg"):
            h.update(p.relative_to(pkg).as_posix().encode())
            h.update(p.read_bytes())
    return h.hexdigest()[:12]



def is_loopback(remote: str | None) -> bool:
    """Whether a request comes from this machine: the rack panel's own kiosk."""
    try:
        return remote is not None and ipaddress.ip_address(remote).is_loopback
    except ValueError:
        return False


def same_origin(request: web.Request) -> bool:
    """Browsers send Origin on POST and on the WebSocket handshake, and they apply no CORS to a WebSocket:
    without this, any page open in an allowed laptop's browser could act. Command-line clients send none."""
    origin = request.headers.get("Origin")
    if origin is None:
        return True
    o = urlsplit(origin)
    return o.scheme in ("http", "https") and o.netloc == request.host


def today_wait(now: float) -> float:
    """Seconds until the today refresh must run: TODAY_MAX_S, or just past the next local midnight, so
    today.since (and the live cost's day) turns over on time. A midnight plus 24 h misses on a 23 or 25 hour
    day, so the next midnight is the local midnight of a time 36 h past this one."""
    following = queries.local_midnight(queries.local_midnight(now) + 36 * 3600)
    return min(TODAY_MAX_S, max(0.0, following - now) + MIDNIGHT_LAG_S)


class Dash:
    def __init__(self, cfg: Config, *, clock=time.time, mono=time.monotonic, sensors=None, whois=None,
                 host_whois=None, manager=None, sources: bool = True, mem_sampler=None, log_source=None,
                 start_facts=None, unit_runner=None, tailscale=netguard.tailscale_self):
        # log_source(on_batch), start_facts(unit) and unit_runner(*cmd) replace the journal and systemd, for
        # `kiln demo` (kiln/demo/edges.py): the same code then runs on a machine with neither.
        self.cfg, self.clock, self.sources = cfg, clock, sources
        self.conn = connect(cfg.db_path)
        try:
            devices.merge(self.conn, cfg.devices)  # before anything reads keys or colours
        except sqlite3.Error as e:  # e.g. another writer holds the lock: the dashboard must still start
            print(f"kiln-dash: devices not merged ({e}); the next start tries again", flush=True)
        self.reader = Reader(cfg.db_path)  # read-only queries off the event loop (store/reader.py)
        self.book = PriceBook(self.conn)
        r = cfg.reference
        if r is not None:  # without [pricing.reference] no request gets a reference cost
            self.book.add("reference", "*", r.provider_model, r.input_per_m, r.cached_per_m, r.output_per_m,
                          now=clock(), raw={"name": r.name})
        if spread.ensure_current(self.conn, cfg.max_prefill_tps):  # history built with older spreading rules
            print(f"kiln-dash: rebuilt series_1s (rules v{spread.SERIES_VERSION})", flush=True)
        self.live = LiveState(clock)
        # The live chart is this host's engine; remote hosts have their own live section.
        since = int(clock()) - 15 * 60
        self.live.seed(queries.seed_seconds(self.conn, since, cfg.host), queries.recent_starts(self.conn, since, cfg.host))
        # A conversation the gateway has forgotten can't be live again: its next turn gets a new id.
        self.drafts = ConvDrafts()
        for turn in queries.recent_draft_turns(self.conn, clock() - FORGET_AFTER_S, cfg.host):
            self.drafts.note(*turn)
        for profile, completion, events in queries.recent_exactness(self.conn):
            self.live.record_exactness(profile, completion, events)
        for profile, rate in queries.recent_prefill_rates(self.conn, max_tps=cfg.max_prefill_tps).items():
            self.live.set_prefill_rate(profile, rate)
        seed_chunks = {"crucible": engines.CRUCIBLE_PREFILL_CHUNK, "halogen": engines.HALOGEN_PREFILL_STEP}
        for name, prof in cfg.profiles.items():
            if prof.engine in seed_chunks:
                self.live.prefill_chunks[name] = seed_chunks[prof.engine]
        self.ingest = Ingestor(self.conn, cfg, self.book, clock)
        queries.assign_colors(self.conn)  # clients stored before colours were assigned
        queries.rebalance_colors(self.conn, clock())
        self.state = StateFile(cfg.state_file)
        self.sensors = sensors or Sensors()
        # Full names (fqdn): action rights compare the whole MagicDNS name (netguard.node_allowed).
        self.whois = whois or Whois(lookup=partial(tailscale_whois, full=True))
        # Host names this dashboard answers to (DNS rebinding); the MagicDNS name joins at startup.
        self.host_guard = netguard.HostGuard(netguard.default_names(cfg.host, cfg.dash_allowed_hosts))
        self._tailscale = tailscale
        self.tailnet: str | None = None  # this node's tailnet suffix, for allow_nodes entries without a dot
        self._tailnet_tried = -TAILNET_RETRY_S
        # Remote hosts are optional: kiln.hosts is imported only when kiln.toml lists [hosts], so a tree
        # without that package works. Host pushes need the full node name; a separate cache keeps device
        # lookups as they were.
        self.hosts = None
        if cfg.hosts:
            from kiln.hosts.service import HostsService
            self.hosts = HostsService(cfg, self.conn, self.book,
                                      host_whois or Whois(lookup=partial(tailscale_whois, full=True)), clock)
        self.hw: dict = {}
        self.mem_sampler = mem_sampler or MemorySampler()
        self.mem: dict | None = None  # the last hw.mem reading, {"ts", "ram", "disk", ...}
        self._mem_task: asyncio.Task | None = None
        self.mem_start_facts = start_facts or memory_start_facts
        self._mem_seeded: dict[str, float] = {}  # profile -> when its start lines were last looked for
        self.manager = manager or Manager(cfg, self.state, on_event=self._engine_event,
                                          gtt_used=lambda: self.sensors.sample().get("gtt_used_bytes"),
                                          **({"runner": unit_runner} if unit_runner else {}))
        self.ingest.is_probe = self.manager.is_probe
        self.kv_pool: dict[str, dict] = {}
        self.last = queries.last_request(self.conn, cfg.host)
        self.browsers: dict[web.WebSocketResponse, asyncio.Queue] = {}  # browser -> its send queue
        self._sids = itertools.count(1)  # one per socket: a panel session knows its own changes by it
        self.panel_sync = PanelSync(cfg.panel_file, self.broadcast, clock=clock, mono=mono)
        self.started = clock()
        self._tick_wake = asyncio.Event()
        self.build = build_hash()
        self._today: tuple[dict, tuple, dict, dict] | None = None  # queries.today_bundle, refreshed off the loop
        self._today_dirty = asyncio.Event()
        # Refreshed in the background (the check runs systemctl and podman); unknown counts as blocked.
        self.blocked: str | None = "checking whether the GPU is free"
        self.schedule: dict | None = self._schedule_view()
        self.engine_cache: dict | None = None  # {"profile", "ts", counters} from the engine's own cache report
        # The fleet's GPU queue exists only with a [schedule]; its module is not in every tree.
        s, self.gpu_queue = cfg.schedule, None
        if s:
            from kiln.collect.gpu_queue import DEFAULT_PLAN_FILE, GpuQueue
            self.gpu_queue = GpuQueue(lease_log=s.lease_log, queue_dir=s.queue_dir, holds_dir=s.holds_dir,
                                      plan_file=s.plan_file or DEFAULT_PLAN_FILE, clock=clock)
        self._panel: dict | None = None
        self._memo: dict[tuple, tuple[float, object]] = {}  # (query, args) -> (when, result): _memoized
        self.kiosk_seen: float | None = None  # last /api/v1/panel poll from this machine (the rack kiosk)
        offsets = {r["source"].split(":", 1)[1]: int(r["position"]) for r in self.conn.execute(
            "SELECT source, position FROM ingest_offsets WHERE source LIKE 'gateway:%'")}
        cur = self.conn.execute("SELECT position FROM ingest_offsets WHERE source = 'journal'").fetchone()
        self.journal = log_source(self._on_journal) if log_source else JournalFollower(
            cfg.container_uid, self._on_journal, cur[0] if cur else None, cfg.journal_backfill, clock)
        self.files = GatewayFiles(cfg.gateway_data, offsets, self._on_records, clock)
        host, port = cfg.events_listen
        self.feed = GatewayFeed(f"ws://{url_host(local_host(host))}:{port}/events", self._on_feed,
                                self._on_feed_lost)
        self._tasks: list[asyncio.Task] = []

    # -- lifecycle -------------------------------------------------------------
    def app(self) -> web.Application:
        app = web.Application(middlewares=[netguard.middleware(self.host_guard, "[dash] allowed_hosts"),
                                           plain_not_found])
        r = app.router
        r.add_get("/api/v1/live", self.ws_live)
        r.add_get("/api/v1/state", self.get_state)
        r.add_get("/api/v1/usage", self.get_usage)
        r.add_get("/api/v1/series", self.get_series)
        r.add_get("/api/v1/requests", self.get_requests)
        r.add_get("/api/v1/activity", self.get_activity)
        r.add_get("/api/v1/prices", self.get_prices)
        r.add_get("/api/v1/cache", self.get_cache)
        r.add_get("/api/v1/signals", self.get_signals)
        r.add_get("/api/v1/speculation", self.get_speculation)
        r.add_get("/api/v1/engines", self.get_engines)
        r.add_post("/api/v1/engines/switch", self.post_switch)
        r.add_post("/api/v1/engines/stop", self.post_stop)
        r.add_get("/api/v1/me", self.get_me)
        r.add_post("/api/v1/hosts/{host}/push", self.post_host_push)
        r.add_get("/api/v1/panel", self.get_panel)
        r.add_get("/api/v1/panel/seen", self.get_panel_seen)
        r.add_get("/", self.index)
        r.add_get("/panel", self.panel_page)
        r.add_static("/", static_dir(), append_version=False)
        app.on_response_prepare.append(revalidate_files)
        app.on_response_prepare.append(security_headers)
        app.on_startup.append(self.startup)
        app.on_cleanup.append(self.cleanup)
        return app

    async def startup(self, _app=None) -> None:
        await self._learn_tailnet()
        await self.manager.reconcile()
        if not self.sources:
            return
        await self.refresh_today()
        loops = {"journal": self.journal.run, "gateway files": self.files.run, "gateway feed": self.feed.run,
                 "tick": self._tick_loop, "today": self._today_loop, "settle": self._settle_loop,
                 "blocked check": self._blocked_loop,
                 "engine cache": self._engine_cache_loop, "colours": self._colour_loop}
        if self.cfg.openrouter_enabled:
            loops["prices"] = self._price_loop
        self._tasks = [asyncio.create_task(self._supervise(n, f)) for n, f in loops.items()]

    async def _supervise(self, name: str, loop) -> None:
        """Keep a background loop alive: an unexpected error is logged and the loop restarted."""
        while True:
            try:
                await loop()
            except asyncio.CancelledError:
                raise
            except Exception as e:
                print(f"kiln-dash: {name} loop failed, restarting in 5 s: {type(e).__name__}: {e}", flush=True)
            await asyncio.sleep(5)

    async def cleanup(self, _app=None) -> None:
        for t in self._tasks:
            t.cancel()
        await asyncio.gather(*self._tasks, return_exceptions=True)
        for ws in list(self.browsers):
            await ws.close()
        self.panel_sync.flush()
        self.reader.close()
        self.conn.close()

    # -- ingest ------------------------------------------------------------------
    def _tx(self, fn):
        self.conn.execute("BEGIN")
        try:
            out = fn()
            self.conn.execute("COMMIT")
            return out
        except Exception:
            self.conn.execute("ROLLBACK")
            raise

    def _save_offset(self, source: str, position) -> None:
        self.conn.execute("INSERT INTO ingest_offsets (source, position) VALUES (?, ?) ON CONFLICT (source)"
                          " DO UPDATE SET position = excluded.position", (source, str(position)))

    async def _on_journal(self, entries: list, cursor: str | None) -> None:
        def work():
            notes = []
            for container, msg, ts, _ in entries:
                notes += self.ingest.engine_line(container, msg, ts)
            if cursor:
                self._save_offset("journal", cursor)
            return notes
        await self._apply(self._tx(work))

    def _record(self, rec) -> list:
        """One gateway record under its own savepoint: a record that fails is undone, logged and skipped, so
        it can not hold back the records after it, and the file offset still moves past it."""
        self.conn.execute("SAVEPOINT record")
        try:
            notes = self.ingest.gateway_record(rec)
        except Exception as e:  # noqa: BLE001 (any bug or odd value; sqlite errors included)
            self.conn.execute("ROLLBACK TO record")
            self.conn.execute("RELEASE record")
            rid = rec.get("id") if isinstance(rec, dict) and isinstance(rec.get("id"), str) else "?"
            print(f"kiln-dash: gateway record {rid[:64]!r} skipped: {type(e).__name__}: {str(e)[:200]}", flush=True)
            return []
        self.conn.execute("RELEASE record")
        return notes

    async def _on_records(self, records: list, offsets: dict) -> None:
        def work():
            notes = []
            for rec in records:
                notes += self._record(rec)
            for name, pos in offsets.items():
                self._save_offset(f"gateway:{name}", pos)
            return notes
        await self._apply(self._tx(work))

    async def _on_feed(self, ev: dict) -> None:
        self.live.on_event(ev)
        if ev.get("type") == "req.end" and isinstance(ev.get("record"), dict):
            await self._apply(self._tx(lambda: self._record(ev["record"])))

    async def _on_feed_lost(self) -> None:
        self.live.feed_lost()  # the gateway restarted; its hello will say what is still running

    async def _apply(self, notes: list) -> None:
        for note in notes:
            kind = note[0]
            if kind == "request":
                row = queries.get_request(self.conn, note[1])
                if row:
                    self.drafts.note(row["conv"], row["id"], row["ts"], row["draft_proposed"], row["draft_accepted"])
                    self.last = queries.last_request(self.conn, self.cfg.host)
                    self._today_dirty.set()
                    await self.broadcast({"type": "request", "request": row})
            elif kind == "progress":
                self.live.set_prefill_progress(note[1], note[2], note[3], elapsed_s=note[4], at=note[5],
                                               step=note[6], req=note[7])
                if self.live.progress_changed:
                    self._tick_wake.set()
            elif kind == "continuation":
                self.live.set_continuation(note[1], note[2])
            elif kind == "decode_drafts":
                self.live.set_decode_drafts(note[1], note[2], note[3], note[4], tokens=note[6], elapsed_s=note[7],
                                            at=note[5])
            elif kind == "prefill_rate":
                self.live.set_prefill_rate(note[1], note[2])
            elif kind == "kv":
                self.kv_pool[note[1]] = {"pool_used": note[2], "pool_total": note[3], "pool_ts": note[4]}
            elif kind == "exactness":
                self.live.record_exactness(note[1], note[2], note[3])

    async def _settle_loop(self) -> None:
        while True:
            await asyncio.sleep(SETTLE_EVERY_S)
            self.ingest.watermarks = {"journal": self.journal.watermark, "gateway": self.files.watermark}
            await self._apply(self._tx(self.ingest.settle))

    async def _blocked_loop(self) -> None:
        while True:
            await self._blocked_round()
            await asyncio.sleep(BLOCKED_EVERY_S)

    async def _blocked_round(self) -> None:
        try:
            # A ready engine whose unit stopped or crashed outside Kiln is no longer ready (502s otherwise).
            await self.manager.watch()
        except Exception as e:  # report and retry next round
            print(f"kiln-dash: engine unit check failed: {type(e).__name__}: {e}", flush=True)
        try:
            self.blocked = await self.manager.blocked()
        except Exception as e:  # never let a failed check look like "free to switch"
            self.blocked = f"could not check ({type(e).__name__})"
        try:
            if self.manager.fleet and self.manager.tidy_hold():
                await self._engine_event("hold_released", None, "GPU memory is back; the fleet may use the GPU")
        except Exception as e:  # report and retry next round
            print(f"kiln-dash: hold check failed: {type(e).__name__}: {e}", flush=True)
        self.schedule = self._schedule_view()

    async def _engine_cache_loop(self) -> None:
        import aiohttp
        timeout = aiohttp.ClientTimeout(total=3)
        async with aiohttp.ClientSession(timeout=timeout) as session:
            while True:
                st = self.state.read()
                p = self.cfg.profiles.get(st.profile or "")
                counters = None
                if p and p.engine == "halogen" and st.serving:
                    try:
                        async with session.get(f"http://127.0.0.1:{p.port}/cache") as r:
                            counters = halogen_cache_counters(await r.json()) if r.status == 200 else None
                    except (aiohttp.ClientError, asyncio.TimeoutError, ValueError):
                        counters = None
                self.engine_cache = {"profile": p.name, "ts": self.clock(), **counters} if counters else None
                await asyncio.sleep(ENGINE_CACHE_EVERY_S)

    def _schedule_view(self) -> dict | None:
        fleet = self.manager.fleet
        if fleet is None:
            return None
        try:
            return fleet.view(self.clock())
        except Exception as e:  # a display problem must not stop the blocked check
            return {"error": f"{type(e).__name__}: {e}"[:200]}

    async def _colour_loop(self) -> None:
        while True:
            await asyncio.sleep(COLOUR_REBALANCE_S)
            self._tx(lambda: queries.rebalance_colors(self.conn, self.clock()))
            self._today_dirty.set()  # tick names carry the colours

    async def _price_loop(self) -> None:
        while True:
            try:
                models = await openrouter.fetch_models()
                priced = [*self.cfg.profiles.values(), *self.cfg.hosts.values()]  # both carry model and hf_id
                log = self._tx(lambda: openrouter.refresh(self.book, models, priced, self.clock()))
                for line in log:
                    print(f"kiln-dash: {line}", flush=True)
                await asyncio.sleep(self.cfg.openrouter_refresh_h * 3600)
            except Exception as e:  # prices are optional; retry later and say why
                print(f"kiln-dash: openrouter refresh failed: {type(e).__name__}: {e}", flush=True)
                await asyncio.sleep(1800)

    # -- engine events -----------------------------------------------------------
    async def _engine_event(self, kind: str, profile: str | None, detail: str) -> None:
        now = self.clock()
        self._tx(lambda: self.conn.execute(
            "INSERT INTO engine_events (ts, host, profile, kind, detail) VALUES (?, ?, ?, ?, ?)",
            (now, self.cfg.host, profile, kind, detail)))
        await self.broadcast({"type": "engine", "engine": self.engine_view(), "log": detail})

    def engine_view(self) -> dict:
        st = self.state.read()
        p = self.cfg.profiles.get(st.profile or "")
        kv = None
        if p:
            kv = {"capacity_positions": p.kv.positions, "capacity_bytes": p.kv.bytes,
                  "pool_used": None, "pool_total": None, "pool_ts": None}
            pool = self.kv_pool.get(p.name)
            if pool and pool["pool_ts"] >= st.since:  # a reading from before this engine started is stale
                kv.update(pool)
        return {"profile": st.profile, "label": p.label if p else None, "engine": p.engine if p else None,
                "model": p.model if p else None, "state": st.state, "since": st.since, "target": st.target,
                "detail": st.detail, "phase": st.phase, "phase_since": st.phase_since, "kv": kv,
                "blocked": self.blocked, "schedule": self.schedule,
                "cache": self.engine_cache if self.engine_cache and self.engine_cache["profile"] == st.profile
                else None}

    # -- live messages -----------------------------------------------------------
    def _cached_today(self, now: float) -> tuple[dict, tuple, dict, dict]:
        # tick.today stays this host's own work; each remote host gets its own today and last. The tick only
        # reads memory: _today_loop refreshes this off the loop. Computed here only before the first refresh
        # (a dashboard without sources, as in tests).
        if self._today is None:
            self._today = queries.today_bundle(self.conn, now, self.cfg.host, list(self.cfg.hosts))
        return self._today

    async def refresh_today(self) -> None:
        self._today_dirty.clear()  # before the query: a change during it asks for another refresh
        self._today = await self.reader.run(queries.today_bundle, self.clock(), self.cfg.host, list(self.cfg.hosts))

    async def _today_loop(self) -> None:
        """Refresh the tick's slower parts when a request or a name changes (at most every TODAY_MIN_S) and at
        least every TODAY_MAX_S. On a Strix Halo test machine this query waited up to 11 s on page reads; ticks
        never wait for it."""
        while True:
            try:
                await asyncio.wait_for(self._today_dirty.wait(), today_wait(self.clock()))
            except asyncio.TimeoutError:
                pass
            await self.refresh_today()
            await asyncio.sleep(TODAY_MIN_S)

    def hosts_view(self, now: float, per_host: dict) -> dict:
        if self.hosts is None:
            return {}
        return {n: {**self.hosts.live.view(h, now), **per_host.get(n, {})} for n, h in self.cfg.hosts.items()}

    def tick(self, now: float | None = None, seconds: list | None = None) -> dict:
        now = self.clock() if now is None else now
        st = self.state.read()
        p = self.cfg.profiles.get(st.profile or "")
        live = {**self.live.view(now, slots=p.slots if p else 0, profile=p.name if p else None),
                "slots": p.slots if p else 0}
        today, parse_errors, names, per_host = self._cached_today(now)
        for s in live["streams"]:
            key = s["client"].get("key") or ""
            device = self.cfg.device_for(key)  # the gateway keys streams by address, ingest by device
            key = device.key if device else key
            known = names.get(key) or {}
            name = device.name if device else known.get("name") or s["client"].get("name") or key
            s["client"] = {**s["client"], "key": key, "name": name,
                           "color": known.get("color") or queries.client_color(key)}
            s["draft"] = self.drafts.view(s.get("conv"))
        seconds = seconds or []
        return {"v": SCHEMA_VERSION, "type": "tick", "ts": now, "live": live,
                "second": seconds[-1] if seconds else None, "seconds": seconds,
                "last": self.last, "engine": self.engine_view(), "hw": self.hw, "today": today,
                "health": {"gateway": "up" if self.feed.connected else "down", "dash_started": self.started,
                           "parse_errors_24h": parse_errors[0], "parse_error_last_ts": parse_errors[1],
                           "journal": "following" if self.journal.running
                           else "restarting", "last_request_ts": (self.last or {}).get("ts")},
                "host": self.cfg.host, "hosts": self.hosts_view(now, per_host)}

    async def snapshot(self, request: web.Request | None = None) -> dict:
        # Who is asking first: the lookup can wait up to 3 s, and the history must be the seconds sent up to
        # the moment the caller joins the broadcast (ws_live), or its chart misses the ones sent meanwhile.
        me = await self.me(request) if request is not None else {"can_act": False, "identity": None}
        return self.snapshot_now(me)

    def snapshot_now(self, me: dict) -> dict:
        now = self.clock()
        return {**self.tick(now), "type": "snapshot", "series_1s": self.live.history(now),
                "profiles": self.profiles_view(), "build": self.build,
                "pricing": self.pricing_view(),
                "me": me, "panel": self.panel_sync.view()}

    def pricing_view(self) -> dict | None:
        """The reference card's name for labels and its prices for the live cost (web/static/js/cost.js).
        None without [pricing.reference]: the UI then hides the API-cost figure."""
        r = self.cfg.reference
        if r is None:
            return None
        return {"reference": r.name, "input_per_m": r.input_per_m, "cached_per_m": r.cached_per_m,
                "output_per_m": r.output_per_m}

    def profiles_view(self) -> list[dict]:
        active = self.state.read().profile
        return [{"name": p.name, "label": p.label, "engine": p.engine, "model": p.model, "context": p.context,
                 "slots": p.slots, "memory_gib": p.memory_gib, "active": p.name == active,
                 "disabled": p.disabled}
                for p in self.cfg.profiles.values()]

    async def broadcast(self, msg: dict) -> None:
        """Queue a message for every browser. Never waits on a browser: ingestion calls this."""
        if not self.browsers:
            return
        data = json.dumps({"v": SCHEMA_VERSION, **msg})
        for ws, q in list(self.browsers.items()):
            self._offer(ws, q, data)

    def _offer(self, ws: web.WebSocketResponse, q: asyncio.Queue, data: str) -> None:
        try:
            q.put_nowait(data)
        except asyncio.QueueFull:  # too slow to keep up: drop it; it reconnects to a fresh snapshot
            self.browsers.pop(ws, None)
            asyncio.get_running_loop().create_task(ws.close())

    async def sample_mem(self, now: float) -> None:
        """hw.mem: RAM by part and the active engine's disk use (collect.memory), read in a thread."""
        p = self.cfg.profiles.get(self.state.read().profile or "")
        facts = self.ingest.engine_facts(p.container_name) if p else {}
        # Once per dashboard run even with complete-looking sizes: a newer parser can read more from the same
        # start lines (the BF16 PLE line). seed_sizes only fills gaps.
        first = p is not None and p.name not in self._mem_seeded
        missing = "weights_bytes" not in facts or "stage_bytes" not in facts
        if p and (first or missing and now - self._mem_seeded.get(p.name, -1e9) >= MEM_SEED_EVERY_S):
            self._mem_seeded[p.name] = now
            try:
                seed = await asyncio.to_thread(self.mem_start_facts, p.unit)
            except Exception as e:
                print(f"kiln-dash: engine start sizes not read: {type(e).__name__}: {e}", flush=True)
                seed = {}
            if seed:
                self.ingest.seed_sizes(p.container_name, seed)
                facts = self.ingest.engine_facts(p.container_name)
        try:
            m = await asyncio.to_thread(self.mem_sampler.sample, p.unit if p else None, facts,
                                        self.hw.get("gtt_used_bytes"), p.slots if p else None)
        except Exception as e:  # a reading must never stop the live feed
            print(f"kiln-dash: memory sample failed: {type(e).__name__}: {e}", flush=True)
            m = None
        self.mem = {"ts": now, **m} if m else None

    async def panel_tick(self) -> None:
        """The idle return, the alarm jump and the auto rules: once here for every session, not in each."""
        try:
            fleet = self.panel_view()["fleet"]  # cached for PANEL_CACHE_S: the rack's poll reads the same files
            await self.panel_sync.tick(tick_inputs(self.state.read(), self.schedule, self.hw, fleet))
        except Exception as e:  # the panel's screen must never stop the live feed
            print(f"kiln-dash: panel step failed: {type(e).__name__}: {e}", flush=True)

    async def _tick_loop(self) -> None:
        next_hw = next_store = next_mem = 0.0
        while True:
            now = self.clock()
            if now >= next_hw:
                self.hw = {"ts": now, **self.sensors.sample()}
                if self.mem and now - self.mem["ts"] < 3 * MEM_EVERY_S:
                    self.hw["mem"] = self.mem
                if now >= next_mem:  # a reading still running skips this turn instead of starting late
                    if self._mem_task is None or self._mem_task.done():
                        self._mem_task = asyncio.get_running_loop().create_task(self.sample_mem(now))
                    next_mem = now + MEM_EVERY_S
                next_hw = now + HW_EVERY_S
                if now >= next_store:
                    data = json.dumps({k: v for k, v in self.hw.items() if k != "ts"})
                    self._tx(lambda: self.conn.execute(
                        "INSERT OR REPLACE INTO hw_samples (ts, host, data) VALUES (?, ?, ?)",
                        (round(now, 3), self.cfg.host, data)))
                    next_store = now + HW_STORE_EVERY_S
            p = self.cfg.profiles.get(self.state.read().profile or "")
            seconds = self.live.completed_seconds(now, p.slots if p else 0)
            await self.panel_tick()  # before the tick, so a session draws the new screen with the new data
            await self.broadcast(self.tick(now, seconds))
            try:
                await asyncio.wait_for(self._tick_wake.wait(), TICK_S - (self.clock() - now) % TICK_S)
                await asyncio.sleep(max(0.0, EARLY_TICK_MIN_S - (self.clock() - now)))
            except asyncio.TimeoutError:
                pass
            self._tick_wake.clear()
            self.live.progress_changed = False

    # -- authorization -------------------------------------------------------------
    async def _learn_tailnet(self) -> None:
        """This node's MagicDNS name (a Host to accept) and tailnet (for allow_nodes). At boot tailscaled
        can start after Kiln, so a tailnet device's request asks again, at most every TAILNET_RETRY_S."""
        if self._tailscale is None or self.tailnet is not None:
            return
        now = time.monotonic()
        if now - self._tailnet_tried < TAILNET_RETRY_S:
            return
        self._tailnet_tried = now
        ts = await self._tailscale()
        if ts:
            self.host_guard.add(ts["dns_name"])
            self.tailnet = ts["tailnet"]

    def direct_local(self, request: web.Request) -> bool:
        """From this machine and not relayed by a proxy (which makes every client look local)."""
        return is_loopback(request.remote) and not netguard.proxied(request)

    async def me(self, request: web.Request) -> dict:
        if netguard.proxied(request):  # the TCP peer is a proxy: the client is unknown
            return {"can_act": False, "identity": None}
        if netguard.is_mdns_name(request.host):  # a LAN peer can rebind it (netguard.is_mdns_name)
            return {"can_act": False, "identity": None}
        ip = self.peer_ip(request)
        if ip in LOCAL:
            return {"can_act": self.cfg.allow_loopback, "identity": "localhost"}
        self.whois.prefetch(ip)
        info = await self.whois.get(ip, wait=3.0)
        if not info:
            return {"can_act": False, "identity": None}
        await self._learn_tailnet()
        can = (netguard.node_allowed(info.get("fqdn"), self.cfg.allow_nodes, self.tailnet)
               or info.get("user") in self.cfg.allow_users)
        return {"can_act": bool(can), "identity": info.get("node")}

    # -- HTTP handlers ------------------------------------------------------------
    async def index(self, _request: web.Request) -> web.StreamResponse:
        return web.FileResponse(static_dir() / "index.html", headers={"Cache-Control": "no-cache"})

    async def panel_page(self, _request: web.Request) -> web.StreamResponse:
        return web.FileResponse(static_dir() / "panel.html", headers={"Cache-Control": "no-cache"})

    async def ws_live(self, request: web.Request) -> web.StreamResponse:
        if not same_origin(request):  # browsers apply no CORS to a WebSocket: a foreign page could read all
            return web.json_response({"error": "cross-origin WebSocket refused"}, status=403)
        ws = web.WebSocketResponse(heartbeat=20, max_msg_size=MSG_MAX_BYTES)
        await ws.prepare(request)
        me = {**(await self.me(request)), "sid": next(self._sids)}
        # Who may change the rack is fixed at the handshake: an allow-list change counts at the next reconnect.
        client = self.panel_sync.client(me, local=self.direct_local(request), same_origin=same_origin(request))
        snap = self.snapshot_now(me)
        q: asyncio.Queue = asyncio.Queue(maxsize=BROWSER_QUEUE)
        # Snapshot first, then everything broadcast after it, in order: nothing awaits in between.
        q.put_nowait(json.dumps({"v": SCHEMA_VERSION, **snap}))
        self.browsers[ws] = q
        sender = asyncio.create_task(self._send_loop(ws, q))
        try:
            async for msg in ws:  # the rack panel's screen messages; the feed itself is one way
                if msg.type not in (WSMsgType.TEXT, WSMsgType.BINARY):
                    continue
                try:
                    reply = await self.panel_sync.receive(client, msg.data)
                except Exception as e:  # a bug here must not end the session's live feed
                    print(f"kiln-dash: panel message failed: {type(e).__name__}: {e}", flush=True)
                    continue
                if reply is not None and ws in self.browsers:
                    self._offer(ws, q, json.dumps({"v": SCHEMA_VERSION, **reply}))
        finally:
            self.browsers.pop(ws, None)
            sender.cancel()
        return ws

    @staticmethod
    async def _send_loop(ws: web.WebSocketResponse, q: asyncio.Queue) -> None:
        try:
            while True:
                await ws.send_str(await q.get())
        except (ConnectionError, RuntimeError):
            await ws.close()

    async def get_state(self, request: web.Request) -> web.Response:
        return web.json_response(await self.snapshot(request))

    @staticmethod
    def _bad(msg: str) -> web.Response:
        return web.json_response({"error": msg}, status=400)

    def _host_param(self, request: web.Request) -> str:
        """?host=<name>|all (default all): history views cover every host unless asked for one."""
        host = request.query.get("host", queries.ALL_HOSTS)
        if host != queries.ALL_HOSTS and host != self.cfg.host and host not in self.cfg.hosts:
            raise ValueError("unknown host")
        return host

    async def get_usage(self, request: web.Request) -> web.Response:
        q = request.query
        try:
            return web.json_response(await self.reader.run(queries.usage, q.get("period", "today"),
                                                           q.get("group", "device"), self.clock(),
                                                           self._host_param(request)))
        except ValueError as e:
            return self._bad(str(e))

    async def get_series(self, request: web.Request) -> web.Response:
        q = request.query
        rng, group = q.get("range", "1h"), q.get("group", "none")
        try:
            host = self._host_param(request)
            # Live memory (this host only) includes requests still streaming.
            if rng == "15m" and group == "none" and host == self.cfg.host:
                h = self.live.history(self.clock())
                return web.json_response({"range": rng, "step": 1, "t": h["t"], "series": {
                    "all": {"name": "all", "decode": h["decode"], "prefill": h["prefill"]}}})
            return web.json_response(await self.reader.run(queries.series, rng, group, self.clock(), host))
        except ValueError as e:
            return self._bad(str(e))

    async def get_requests(self, request: web.Request) -> web.Response:
        try:
            host = self._host_param(request)
        except ValueError as e:
            return self._bad(str(e))
        try:
            limit = int(request.query.get("limit", 50))
            before = float(request.query["before"]) if "before" in request.query else None
        except ValueError:
            return self._bad("limit and before must be numbers")
        return web.json_response({"rows": await self.reader.run(queries.recent_requests, limit, before, host)})

    async def get_activity(self, request: web.Request) -> web.Response:
        try:
            host = self._host_param(request)
            days = int(request.query.get("days", 31))
        except ValueError:
            return self._bad("days must be a number and host a known host")
        return web.json_response(await self.reader.run(queries.activity, days, self.clock(), host))

    # -- remote hosts ----------------------------------------------------------------
    @staticmethod
    def peer_ip(request: web.Request) -> str | None:
        return normalize_ip(request.remote)

    async def post_host_push(self, request: web.Request) -> web.Response:
        if self.hosts is None:  # no [hosts] in kiln.toml: nothing may push
            return web.json_response({"error": "this dashboard takes no host pushes"}, status=404)
        from kiln.hosts.service import Refused
        from kiln.hosts.validate import MAX_BODY_BYTES
        ip, now = self.peer_ip(request), self.clock()
        try:
            host, device = await self.hosts.authorize(request.match_info["host"], ip)
            size = request.content_length
            if size is None:
                raise Refused(411, "Content-Length is required")
            if size > MAX_BODY_BYTES:
                raise Refused(413, f"body larger than {MAX_BODY_BYTES} bytes")
            state, items = self.hosts.parse(await request.read(), now)
        except Refused as e:
            if e.status == 403:
                self.hosts.note_refusal(ip, e.status, now)
            return web.json_response({"error": str(e)}, status=e.status)
        reply, live_ids = self._tx(lambda: self.hosts.store(host, device, state, items, now))
        if reply["accepted"]:
            self._today_dirty.set()
        for rid in live_ids:
            row = queries.get_request(self.conn, rid)
            if row:
                await self.broadcast({"type": "request", "request": row})
        return web.json_response(reply)

    async def _memoized(self, key: tuple, fn, *args):
        """fn(conn, *args, now) on the reader, or its result from the last HISTORY_MEMO_S."""
        now = self.clock()
        hit = self._memo.get(key)
        if hit is not None and 0 <= now - hit[0] < HISTORY_MEMO_S:
            return hit[1]
        out = await self.reader.run(fn, *args, now)
        self._memo = {**{k: v for k, v in self._memo.items() if 0 <= now - v[0] < HISTORY_MEMO_S}, key: (now, out)}
        return out

    async def get_cache(self, request: web.Request) -> web.Response:
        """The conversations' sums and the daily table; `open` (comma-separated conversation ids) adds those
        conversations' turns, for the ones a reader has open."""
        try:
            hours = min(max(float(request.query.get("hours", 24)), 0.1), 24 * 31)
            days = min(max(int(request.query.get("days", 14)), 1), 90)
        except ValueError:
            return self._bad("hours and days must be numbers")
        convs = [c for c in request.query.get("open", "").split(",") if c]
        if not all(CONV_ID.fullmatch(c) for c in convs):
            return self._bad("open lists conversation ids, separated by commas")
        convs = convs[:MAX_OPEN_CONVS]  # an old tab with many open ones still gets its list
        daily = await self._memoized(("cache_daily", days), cache_queries.daily, days)
        sessions = await self.reader.run(cache_queries.sessions, hours, self.clock(), tuple(convs))
        return web.json_response({"flag_tokens": cache_queries.REREAD_FLAG_TOKENS, "sessions": sessions,
                                  "daily": daily})

    async def get_signals(self, request: web.Request) -> web.Response:
        try:
            host = self._host_param(request)
            days = max(1, min(int(request.query.get("days", 14)), signals.MAX_DAYS))  # one memo key per real view
        except ValueError:
            return self._bad("days must be a number and host a known host")
        return web.json_response(await self._memoized(("signals", days, host), _signals_view, days, host))

    async def get_speculation(self, request: web.Request) -> web.Response:
        """Crucible's per-position draft counters summed per profile (ARCHITECTURE.md, Speculative decoding)."""
        try:
            host = self._host_param(request)
            period = request.query.get("period", "today")
            return web.json_response(await self.reader.run(queries.draft_acceptance, period, self.clock(), host))
        except ValueError as e:
            return self._bad(str(e))

    def panel_view(self) -> dict:
        """Data for the rack panel that the live feed does not carry: the fleet's GPU queue."""
        now = self.clock()
        if self._panel is None or not 0 <= now - self._panel["ts"] < PANEL_CACHE_S:
            fleet = None
            if self.gpu_queue is not None:
                try:
                    fleet = self.gpu_queue.view()
                except Exception as e:  # a missing or odd file must not break the page
                    fleet = {"error": f"could not read the GPU queue ({type(e).__name__})"}
            self._panel = {"v": SCHEMA_VERSION, "ts": now, "fleet": fleet}
        return self._panel

    async def get_panel(self, request: web.Request) -> web.Response:
        if self.direct_local(request):
            self.kiosk_seen = self.clock()
        return web.json_response(self.panel_view())

    async def get_panel_seen(self, _request: web.Request) -> web.Response:
        """For the kiosk's watchdog (deploy/panel/kiln-panel-browser): the page polls /api/v1/panel every
        second, so a long silence while the dashboard runs means a crashed or hung page."""
        now = self.clock()
        age = None if self.kiosk_seen is None else round(now - self.kiosk_seen, 1)
        return web.json_response({"v": SCHEMA_VERSION, "age_s": age, "uptime_s": round(now - self.started, 1)})

    async def get_prices(self, _request: web.Request) -> web.Response:
        return web.json_response({"current": [c.view() for c in self.book.current()],
                                  "history": [c.view() for c in self.book.history()]})

    async def get_engines(self, _request: web.Request) -> web.Response:
        # The blocked check runs systemctl and podman: the 5 s loop's value, so a flood spawns no processes.
        events = await self.reader.run(switch_events)
        return web.json_response({"profiles": self.profiles_view(), "engine": self.engine_view(),
                                  "events": events, "blocked": self.blocked})

    async def get_me(self, request: web.Request) -> web.Response:
        return web.json_response(await self.me(request))

    async def _authorize(self, request: web.Request) -> tuple[dict | None, web.Response | None]:
        if request.headers.get(ACTION_HEADER) != "1":
            return None, web.json_response({"error": f"missing {ACTION_HEADER} header"}, status=403)
        if not same_origin(request):
            return None, web.json_response({"error": "cross-origin request refused"}, status=403)
        me = await self.me(request)
        if not me["can_act"]:
            return None, web.json_response({"error": "this device may not control engines"}, status=403)
        try:
            body = await request.json()
        except ValueError:
            return None, self._bad("body must be JSON")
        return {"me": me, "body": body if isinstance(body, dict) else {}}, None

    async def post_stop(self, request: web.Request) -> web.Response:
        ctx, err = await self._authorize(request)
        if err:
            return err
        body = ctx["body"]
        reason = str(body.get("reason") or f"stopped by {ctx['me']['identity']}")[:200]
        token = body.get("eval_token") if isinstance(body.get("eval_token"), str) else None
        try:
            await self.manager.stop(reason, token=token, force=body.get("force") is True)
        except SwitchRefused as e:
            return web.json_response({"error": str(e)}, status=409)
        except RuntimeError as e:
            return web.json_response({"error": str(e)}, status=500)
        return web.json_response({"stopped": True})

    async def post_switch(self, request: web.Request) -> web.Response:
        ctx, err = await self._authorize(request)
        if err:
            return err
        me, body = ctx["me"], ctx["body"]
        target = body.get("profile")
        if not isinstance(target, str):
            return self._bad("profile is required")
        token = body.get("eval_token") if isinstance(body.get("eval_token"), str) else None
        confirm, remember = body.get("confirm") is True, body.get("remember") is True
        try:
            await self.manager.request_switch(target, token, confirm=confirm, remember=remember and token is None)
        except SwitchRefused as e:
            return web.json_response({"error": str(e), "confirm": e.confirm}, status=409)
        self.schedule = self._schedule_view()
        extra = " (fleet paused by confirmation)" if confirm else ""
        await self._engine_event("switch_requested", target,
                                 f"switch to {target} requested by {me['identity']}{extra}")
        return web.json_response({"accepted": True}, status=202)


async def serve(cfg: Config) -> None:
    dash = Dash(cfg)
    runner = web.AppRunner(dash.app(), access_log=None)
    await runner.setup()
    host, port = cfg.dash_listen
    await web.TCPSite(runner, bind_host(host), port).start()
    print(f"kiln-dash listening on {describe_addresses(runner.addresses)} (build {dash.build})", flush=True)
    try:
        await asyncio.Event().wait()
    finally:
        await runner.cleanup()
