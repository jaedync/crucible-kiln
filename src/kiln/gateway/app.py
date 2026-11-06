"""kiln-gateway: OpenAI-compatible pass-through to the active engine.

Request path: read body -> wait out a planned switch -> resolve the active profile
-> rewrite the model id (and ask for a final usage chunk) -> forward -> relay every
response byte unchanged while an observer timestamps token events -> write one record.
"""
from __future__ import annotations

import asyncio
import re
import sys
import time
import uuid

import aiohttp
from aiohttp import web
from yarl import URL

from kiln import SCHEMA_VERSION, netguard
from kiln.config import Config, Profile, bind_host, describe_addresses
from kiln.engines.admission import AdmissionHold
from kiln.engines.lock import EvalLock
from kiln.engines.state import READY, STARTING, STOPPING, StateFile
from kiln.gateway.body import Layout, encode, parse
from kiln.gateway.compat import adapt
from kiln.gateway.attribution import CLIENT_HEADER, Whois, is_loopback, normalize_ip, valid_label
from kiln.gateway.live import LiveHub
from kiln.gateway.prefix import MAX_MESSAGES, PrefixTracker
from kiln.gateway.records import RecordLog, read_tail
from kiln.gateway.stream import SSEObserver, ended_whole, is_generation, observe_json_body
from kiln.gateway.usage_window import HORIZON_S, MAX_ENTRIES, UsageWindow

HOP_HEADERS = {"connection", "keep-alive", "proxy-authenticate", "proxy-authorization", "te",
               "trailers", "transfer-encoding", "upgrade", "host", "content-length"}
# Upstream must not compress: the observer reads the bytes it relays.
EVAL_TOKEN_HEADER = "X-Kiln-Eval-Token"
ADAPTED_HEADER = "X-Kiln-Adapted"  # request changes made for the engine's dialect (gateway/compat.py)
# content-encoding: the body sent on is never compressed (only identity passes content_encoding()).
DROP_REQUEST_HEADERS = HOP_HEADERS | {"accept-encoding", "content-encoding", CLIENT_HEADER.lower(),
                                      EVAL_TOKEN_HEADER.lower()}
UPSTREAM_TIMEOUT = aiohttp.ClientTimeout(total=None, connect=10, sock_read=None)
# Larger bodies are parsed, hashed and serialized in a worker thread. Below it the work (about 2.5 ms per MiB)
# stays on the event loop, which is faster: a worker waits up to 5 ms for the GIL while streams run.
PREFIX_THREAD_BYTES = 1 << 20
# A smaller body with more values than these also goes to the worker thread: 1 MiB of tiny values or of
# 4,096 short messages took 25 to 53 ms on the loop. A real agent body of 1 MiB has about 1,800 "{" and "["
# and 2,800 commas. Below both bounds the worst body that the release review built holds the loop for about
# 17 ms (Python 3.13): 243 ints of 4,300 digits each.
LOOP_MAX_OBJECTS = 3000
LOOP_MAX_COMMAS = 50_000
CLIENT_CLOSED = 499  # nginx's convention for "client went away before the response finished"
HOLD_POLL_S = 0.25  # a held request notices the end of a switch this late at most
SWITCHING = (STOPPING, STARTING)  # the manager is between engines: one will serve soon (or the switch fails)
HEALTH_WINDOWS = (("last_5m", 300), ("last_1h", HORIZON_S))
DIGEST_FAILED = object()
DEEP = (400, "the JSON body is nested too deeply", "invalid_body")  # a RecursionError was a 500 with no record
# Bodies larger than PREFIX_THREAD_BYTES are read, parsed and prepared at most this many at a time: their
# memory is about 4 times the body while parsed, 16 times for a body with one character above U+FFFF
# (tools/measure_body_memory.py). install.py sizes MemoryMax from it (parse_slot_mib).
PARSE_SLOTS = 2
BODY_READ_TIMEOUT_S = 120.0  # 32 MiB at 2 Mbit/s takes about as long
# Parsed JSON costs about 1.1 KB per small object and 60 bytes per small value, 37 times the body for a body
# of tiny objects. These bound a body's parse to about 230 MB whatever its shape. A real request is far
# below them: a 5,000 message agent session has about 25,000 objects, and base64 images have no commas.
MAX_OBJECTS = 200_000  # "{" and "[" bytes, inside strings too
MAX_COMMAS = 2_000_000
# A model id or a finish reason as the record keeps it; other values are recorded as null, because records
# feed the owner's tables and one odd value stopped ingest (security review H2).
_SAFE_TEXT = re.compile(r"[A-Za-z0-9 ._:/@+()\[\]-]{1,200}")
_SURROGATE_ESCAPE = re.compile(rb"\\u[dD][89abAB]")  # the first half of a character above U+FFFF
MAX_TOKENS_RECORDED = 2 ** 31  # a larger or negative max_tokens is recorded as null
WHOIS_RECORD_KEYS = ("node", "name")  # never the login (an email) or the OS


HELD_BODIES = 2  # held requests keep their bodies: together at most this many times the body cap
# What a held request keeps: the raw bytes, the decoded text (Layout.text) and the parsed values. Strings cost
# 1, 2 or 4 bytes per character, after the widest character: one emoji in 30 MiB of ASCII kept 270 MiB (delta
# review H1). Small values cost more: measured 27 to 80 bytes per comma and up to 200 bytes per "{" or "["
# (release review M2). The estimate is about 1.7 times the measured cost of a dense body, and HELD_BODIES * 5
# body caps bound the sum of them. The worst body at the cap (9 times its size: one emoji) fits that sum alone.
HELD_PER_OBJECT = 400
HELD_PER_COMMA = 80
PARSED_X = 6  # a parsed body with its raw bytes and the engine's copy, in body sizes (measured 4 + 2)
PARSE_BOUND_MIB = 230  # MAX_OBJECTS and MAX_COMMAS at their worst
# A body with one character above U+FFFF: its text and values are 4 bytes per character, and the engine's
# copy is encoded again from the text. Measured 17.0 times the body, raw bytes included (delta review H1).
PARSED_WIDE_X = 18  # with a margin over the 17.0 measured with the raw bytes
HELD_X = 5  # held_cost of all held requests together: at most HELD_BODIES * HELD_X body caps


def parse_slot_mib(max_body_mib: int) -> int:
    """The most memory of one body that is parsed and prepared now: a dense body or a wide text body."""
    return max(PARSED_X * max_body_mib + PARSE_BOUND_MIB, PARSED_WIDE_X * max_body_mib)


def memory_max_mib(max_body_mib: int) -> int:
    """A MemoryMax for kiln-gateway that the body work can not pass: the parse slots at their worst, the held
    bodies, and 256 MiB for the process and its streams. Rounded up to 64 MiB."""
    need = 256 + PARSE_SLOTS * parse_slot_mib(max_body_mib) + HELD_BODIES * max_body_mib * HELD_X
    return -(-need // 64) * 64


def safe_text(v) -> str | None:
    return v if isinstance(v, str) and _SAFE_TEXT.fullmatch(v) else None


def path_refusal(request: web.Request) -> str | None:
    """Why the relayed path must not reach the engine, or None. aiohttp decodes %2e%2e and %2F in
    match_info, so /v1/%2e%2e/admin would reach /admin and an encoded ? would add a query."""
    raw = request.raw_path.split("?", 1)[0].lower()
    if re.search(r"%(2f|5c|23|3f|[01][0-9a-f]|7f)", raw):
        return "an encoded /, \\, #, ? or control character"
    path = request.match_info["path"]
    if any(c in path for c in "\\?#") or any(ord(c) < 32 or ord(c) == 127 for c in path):
        return "a \\, #, ? or control character"
    if any(seg in (".", "..") for seg in path.split("/")):
        return "a . or .. segment"
    return None


def held_cost(raw: bytes, layout: Layout | None = None) -> int:
    """The memory in bytes that a held request keeps, at most (an estimate; see HELD_PER_OBJECT): the raw
    bytes, the decoded text when the layout keeps it, and the parsed values at the width of the widest
    character. All of it is C work (no walk over the values), so it stays cheap for a large body."""
    objects, commas = json_counts(raw)
    if layout is not None:
        text, chars = sys.getsizeof(layout.text), len(layout.text)
        width = 1 if layout.ascii else min(4, text // max(1, chars))  # str: 1, 2 or 4 bytes per character
    else:  # json.loads parsed it: no text kept
        text, chars, width = 0, len(raw), 1 if raw.isascii() else 4
    if b"\\u" in raw:  # an escape can make a value wider than the text: \u00e9 is 2 bytes, a surrogate pair 4
        width = 4 if _SURROGATE_ESCAPE.search(raw) else max(width, 2)
    return len(raw) + text + width * chars + HELD_PER_OBJECT * objects + HELD_PER_COMMA * commas


def json_counts(raw: bytes) -> tuple[int, int]:
    """("{" and "[" bytes, "," bytes), inside strings too."""
    return raw.count(b"{") + raw.count(b"["), raw.count(b",")


def body_refusal(raw: bytes, counts: tuple[int, int] | None = None) -> str | None:
    objects, commas = counts or json_counts(raw)
    if objects > MAX_OBJECTS or commas > MAX_COMMAS:
        return f"the body has too many JSON values (more than {MAX_OBJECTS:,} objects or {MAX_COMMAS:,} commas)"
    return None


def content_encoding(request: web.Request) -> str | None:
    """The request body's Content-Encoding when it is not plain (identity), else None."""
    codings = [c.strip().lower() for v in request.headers.getall("Content-Encoding", ()) for c in v.split(",")]
    return ", ".join(codings) if any(c not in ("", "identity") for c in codings) else None


def connection_named(headers) -> set[str]:
    """Headers that the Connection header names: hop-by-hop, so they must not be relayed (RFC 9110 7.6.1)."""
    return {t.strip().lower() for v in headers.getall("Connection", ()) for t in v.split(",") if t.strip()}


def _client_left(status, stats) -> tuple[int, str | None]:
    """(status, error) for a client that closed the connection: a cancel, unless the stream had ended."""
    if status == 200 and ended_whole(stats.finish, stats.usage):
        return 200, None
    return CLIENT_CLOSED, "client_disconnected"


def _json_error(status: int, message: str, kind: str, **extra) -> web.Response:
    return web.json_response({"error": {"message": message, "type": kind, **extra}}, status=status)


def _request_meta(body: dict) -> dict:
    msgs, tools = body.get("messages"), body.get("tools")
    effort = body.get("reasoning_effort")
    if effort is None and isinstance(body.get("reasoning"), dict):
        effort = body["reasoning"].get("effort")
    # Client values: the record keeps them only when small and plain (release review H1).
    mt = body.get("max_completion_tokens") or body.get("max_tokens")
    return {"max_tokens": mt if type(mt) is int and 0 <= mt < MAX_TOKENS_RECORDED else None,
            "reasoning_effort": safe_text(effort),
            "n_messages": len(msgs) if isinstance(msgs, list) else None,
            "n_tools": len(tools) if isinstance(tools, list) else None}


def rewrite_body(body: dict, profile: Profile) -> tuple[dict, list[str]]:
    """The body the engine gets, and the names of any dialect changes (gateway/compat.py)."""
    out, changes = adapt({**body, "model": profile.served_id}, profile.request_compat, profile.default_max_tokens,
                         profile.thinking)
    if out.get("stream"):
        opts = out.get("stream_options")  # a string or a list was a 500 with no record (release review M4)
        out["stream_options"] = {**(opts if isinstance(opts, dict) else {}), "include_usage": True}
    return out, changes


class Gateway:
    def __init__(self, cfg: Config, *, clock=time.time, whois: Whois | None = None,
                 session: aiohttp.ClientSession | None = None, peer=None, tailscale=netguard.tailscale_self):
        self.cfg, self.clock = cfg, clock
        # Host names this gateway answers to (DNS rebinding); the MagicDNS name joins at startup.
        self.host_guard = netguard.HostGuard(netguard.default_names(cfg.host, cfg.gateway_allowed_hosts))
        self.origin_guard = netguard.OriginGuard(cfg.gateway_allowed_origins)  # cross-site requests
        self._tailscale = tailscale
        self._parse_slots = asyncio.Semaphore(PARSE_SLOTS)
        # peer(request) -> the client's address. Only `kiln demo` passes one, so its clients on loopback can be
        # three devices; every other gateway keys clients by the connection's own address.
        self.peer = peer or (lambda request: normalize_ip(request.remote))
        self.state = StateFile(cfg.state_file)
        self.lock = EvalLock(cfg.lock_file, clock)
        self.admission = AdmissionHold(cfg.admission_file, clock)
        self.held = 0  # requests waiting for a switch; not in hub.active, so a drain can reach 0
        self.held_bytes = 0  # held_cost of their bodies, bounded with max_held (security review M3)
        self.records = RecordLog(cfg.gateway_data)
        self.hub = LiveHub()
        self.whois = whois or Whois()
        self.prefix = PrefixTracker(clock)  # content-free; memory only (gateway/prefix.py)
        self.usage = UsageWindow(clock())  # /health last_5m and last_1h; aggregates only (gateway/usage_window.py)
        self._session = session

    # -- lifecycle ---------------------------------------------------------
    async def startup(self, _app=None) -> None:
        if self._tailscale is not None:
            ts = await self._tailscale()
            if ts:
                self.host_guard.add(ts["dns_name"])
        if self._session is None:
            self._session = aiohttp.ClientSession(timeout=UPSTREAM_TIMEOUT, auto_decompress=False)
        self.seed_usage()
        self.hub.start_tasks()

    def seed_usage(self) -> None:
        """The last hour from the record tails, so a restart (the deploy) keeps the windows. Any failure leaves
        a partial window (covered_s from now): startup must go on, because held work waits for it."""
        t = time.monotonic()
        now = self.clock()
        try:
            recs, complete = read_tail(self.cfg.gateway_data, now - HORIZON_S, now, MAX_ENTRIES)
            self.usage.seed(recs, complete=complete, now=now, slots_for=self._slots_for)
        except Exception as e:  # noqa: BLE001
            self.usage = UsageWindow(now)  # a half-done seed could count twice; start clean
            print(f"kiln-gateway: usage seed failed ({type(e).__name__}); the windows start now", flush=True)
            return
        print(f"kiln-gateway: usage seeded from {len(recs)} records in {time.monotonic() - t:.3f} s"
              + ("" if complete else " (stopped at the cap: partial window)"), flush=True)

    def _slots_for(self, name: str) -> int | None:
        profile = self.cfg.profiles.get(name)
        return profile.slots if profile else None

    def _feed(self, method: str, *args) -> None:
        """Usage figures must never fail a request."""
        try:
            getattr(self.usage, method)(*args)
        except Exception:  # noqa: BLE001
            print(f"kiln-gateway: usage window failed at {method}", flush=True)

    async def cleanup(self, _app=None) -> None:
        await self.hub.stop_tasks()
        if self._session:
            await self._session.close()

    def app(self) -> web.Application:
        app = web.Application(client_max_size=self.cfg.max_body_mib * 1024 ** 2,
                              middlewares=[netguard.middleware(self.host_guard, "[gateway] allowed_hosts"),
                                           netguard.origin_middleware(self.origin_guard,
                                                                      "[gateway] allowed_origins", cors=True)])
        app.on_response_prepare.append(netguard.cors_signal(self.origin_guard))  # streams and errors too
        app.router.add_get("/health", self.health)
        app.router.add_get("/v1/models", self.models)
        app.router.add_route("*", "/v1/{path:.*}", self.proxy)
        app.on_startup.append(self.startup)
        app.on_cleanup.append(self.cleanup)
        return app

    def events_app(self) -> web.Application:
        app = web.Application(middlewares=[netguard.middleware(self.host_guard, "[gateway] allowed_hosts")])
        app.router.add_get("/events", self.hub.handle_ws)
        return app

    # -- small endpoints ---------------------------------------------------
    def _active(self) -> tuple[Profile | None, object]:
        st = self.state.read()
        return (self.cfg.profiles.get(st.profile) if st.profile else None), st

    async def health(self, _request: web.Request) -> web.Response:
        profile, st = self._active()
        in_flight, held = len(self.hub.active), self.admission.active() is not None
        slots = profile.slots if profile and st.serving and profile.slots > 0 else None
        free = max(0, slots - in_flight) if slots else None
        body = {"ok": True, "v": SCHEMA_VERSION, "profile": st.profile,
                "state": st.state, "model": profile.model if profile else None,
                "in_flight": in_flight, "held": self.held,
                "admission": "held" if held else "open",
                # Requests above the slots wait inside the engine: queued.
                "capacity": {"slots": slots, "in_flight": in_flight, "free": free,
                             "queued": max(0, in_flight - slots) if slots else None,
                             "utilization": round(in_flight / slots, 3) if slots else None,
                             "busy": st.state != READY or held or not free}}
        now = self.clock()
        for key, window_s in HEALTH_WINDOWS:
            try:
                body[key] = self.usage.summary(now, window_s)
            except Exception:  # noqa: BLE001 (the old keys must always answer: switch scripts drain on them)
                print("kiln-gateway: usage window failed in /health", flush=True)
                body[key] = None
        return web.json_response(body)

    async def models(self, _request: web.Request) -> web.Response:
        profile, st = self._active()
        data = []
        if profile and st.serving:
            data.append({"id": profile.model, "object": "model", "owned_by": "kiln",
                         "context_length": profile.context,
                         "meta": {"profile": profile.name, "engine": profile.engine,
                                  "served_id": profile.served_id}})
        return web.json_response({"object": "list", "data": data})

    # -- the request path --------------------------------------------------
    async def proxy(self, request: web.Request) -> web.StreamResponse:
        t0 = self.clock()
        rid = uuid.uuid4().hex
        peer = self.peer(request)
        self.whois.prefetch(peer)
        label = valid_label(request.headers.get(CLIENT_HEADER))
        if why := path_refusal(request):
            return _json_error(400, f"the path has {why}", "invalid_path")
        refusal = None
        if encoded := content_encoding(request):
            # aiohttp would decompress it, past the body cap's intent and outside the parse slots (release
            # review M1). No client of an OpenAI API compresses requests; refuse before a byte is read.
            raw, body, layout, threaded = b"", None, None, False
            refusal = (415, f"the gateway does not accept a compressed request body (Content-Encoding: "
                            f"{encoded[:40]}); send it uncompressed", "unsupported_content_encoding")
        elif (request.content_length or PREFIX_THREAD_BYTES + 1) > PREFIX_THREAD_BYTES:
            async with self._parse_slots:  # large bodies: a bounded number in memory at once
                try:  # a client that sends slowly must not keep a slot
                    raw = await asyncio.wait_for(request.read(), BODY_READ_TIMEOUT_S)
                except asyncio.TimeoutError:
                    return _json_error(408, f"the body did not arrive within {BODY_READ_TIMEOUT_S:.0f} s",
                                       "body_timeout")
                refusal, body, layout = await asyncio.to_thread(self._parse, raw)
            threaded = True
        else:
            raw = await request.read()
            counts = json_counts(raw)
            threaded = counts[0] > LOOP_MAX_OBJECTS or counts[1] > LOOP_MAX_COMMAS
            if threaded:
                async with self._parse_slots:
                    refusal, body, layout = await asyncio.to_thread(self._parse, raw, counts)
            else:
                refusal, body, layout = self._parse(raw, counts)
        if is_generation(request.match_info["path"]):
            self._feed("started", t0)
        profile, st = self._active()
        rec = {"v": SCHEMA_VERSION, "id": rid, "ts_start": t0, "method": request.method,
               "path": request.match_info["path"], "body_bytes": len(raw),
               # The device is the identity; the label is a self-declared tag kept as metadata.
               "client": {"key": peer or "unknown", "label": label, "peer_ip": peer},
               "profile": st.profile, "engine": profile.engine if profile else None,
               "model": safe_text((body or {}).get("model")), "stream": bool((body or {}).get("stream")),
               "user_agent": request.headers.get("User-Agent", "")[:120], **_request_meta(body or {})}

        if refusal:
            return await self._reject(rec, *refusal)
        if refused := await self._eval_refusal(request, rec):
            return refused
        cost = None  # the request does not wait
        if self.cfg.hold_max_s > 0 and self._must_wait():
            cost = held_cost(raw, layout)
            if self.held >= self.cfg.max_held or \
                    self.held_bytes + cost > HELD_BODIES * HELD_X * self.cfg.max_body_mib * 1024 ** 2:
                return await self._reject(rec, 503, f"{self.held} requests already wait for the engine switch",
                                          "too_many_held", {"Retry-After": "30"})
        if not await self._hold(request, rec, cost):
            return web.Response(status=CLIENT_CLOSED)  # nobody reads it; _hold recorded the request
        if "held_s" in rec:  # the switch went on meanwhile: route to what serves now
            if refused := await self._eval_refusal(request, rec):
                return refused
            profile, st = self._active()
            rec.update(profile=st.profile, engine=profile.engine if profile else None)
        if not profile or not st.serving:
            msg = (f"no engine is serving (state {st.state}"
                   + (f", switching to {st.target}" if st.target else "") + ")")
            return await self._reject(rec, 503, msg, "engine_unavailable", {"Retry-After": "30"})
        if body is not None and body.get("model") is not None and not profile.accepts(body["model"]):
            other = self.cfg.profile_for_model(body["model"])
            msg = (f"model {body['model']!r} is not loaded; this host is serving {profile.model!r}"
                   + ("" if other else " and has no profile for that model"))
            return await self._reject(rec, 409, msg, "model_not_loaded", loaded=profile.model)
        rec["model"] = profile.model
        rec["served_model"] = profile.served_id
        data, turn = raw, None
        if body is not None:
            track = bool(body) and is_generation(rec["path"])
            args = (body, layout, profile, track)
            try:
                if threaded or len(raw) > PREFIX_THREAD_BYTES:
                    async with self._parse_slots:
                        digested, changes, data = await asyncio.to_thread(self._prepare, *args)
                else:
                    digested, changes, data = self._prepare(*args)
            except RecursionError:  # the rewrite serializes a deep value that a newer parser accepted
                return await self._reject(rec, *DEEP)
            if track:
                turn = self._prefix_start(self.conversation_key(peer), body, digested)
            if turn:
                rec["prefix"] = turn.fields
            if changes:
                rec["adapted"] = changes
        # The parsed body need not live as long as the stream: 8 dense bodies in flight kept 1837 MiB, against
        # 534 MiB without them (release review M2). `data` is what the engine gets.
        body = layout = args = raw = None
        return await self._forward(request, rec, profile, data, turn)

    @staticmethod
    def _parse(raw: bytes, counts: tuple[int, int] | None = None) -> tuple[tuple | None, dict | None, object]:
        """(refusal for _reject or None, body, layout). Deep nesting raised RecursionError, a 500 with no
        record; a body of countless tiny values used about 37 times its size (security review M3, L7)."""
        if why := body_refusal(raw, counts):
            return (413, why, "body_too_complex"), None, None
        try:
            body, layout = parse(raw)
        except RecursionError:  # Python 3.13 at about 1,000 levels; newer versions parse deeper bodies
            return DEEP, None, None
        return None, body, layout

    def _prepare(self, body: dict, layout: Layout | None, profile: Profile, track: bool):
        """(prefix digests, dialect changes, bytes for the engine): the CPU work of a request. It touches no
        shared state but the prefix tracker's digest cache, so it may run in a worker thread."""
        digested = None
        if track:
            msgs = body.get("messages")
            try:  # no message views for a body that gets no diagnostics: 2M of them used about 600 MB
                digested = self.prefix.digests(body, *((layout.message_bytes(), layout.value_bytes("tools"))
                                                       if layout and isinstance(msgs, list)
                                                       and len(msgs) <= MAX_MESSAGES else ()))
            except Exception:  # noqa: BLE001 (reported by _prefix_start)
                digested = DIGEST_FAILED
        out, changes = rewrite_body(body, profile)
        return digested, changes, encode(out, layout)

    async def _eval_refusal(self, request: web.Request, rec: dict) -> web.Response | None:
        if self.lock.read() and not self.lock.allows(request.headers.get(EVAL_TOKEN_HEADER)):
            # An evaluation owns the machine; other traffic would skew its timings. Only requests
            # carrying its token get through (a label alone is just a claim).
            return await self._reject(rec, 503, f"the engine host is busy: {self.lock.describe()}", "evaluation_running",
                                      {"Retry-After": "600"})
        return None

    def _must_wait(self) -> bool:
        return self.admission.active() is not None or self.state.read().state in SWITCHING

    async def _hold(self, request: web.Request, rec: dict, cost: int | None) -> bool:
        """Wait while a planned switch runs: a client can give up on a 503 after about 14 s, a switch takes
        about a minute, and a client can wait 300 s for headers. At hold_max_s after arrival the normal path decides.
        False if the client left meanwhile (then the request is recorded here as a cancel). cost: held_cost of
        the body, or None when the request found no switch to wait for."""
        if cost is None:
            return True
        start, gone = self.clock(), False
        self.held += 1
        self.held_bytes += cost
        try:
            while self._must_wait() and self.clock() - rec["ts_start"] < self.cfg.hold_max_s:
                if request.transport is None:  # gone, and the server does not cancel handlers
                    gone = True
                    break
                await asyncio.sleep(HOLD_POLL_S)
        except asyncio.CancelledError:  # gone: handler_cancellation cancels the wait
            gone = True
            raise
        finally:
            self.held -= 1
            self.held_bytes -= cost
            rec["held_s"] = round(self.clock() - start, 3)
            if gone:
                await self._finish(rec, status=CLIENT_CLOSED, error="client_disconnected")
        return not gone

    def conversation_key(self, peer: str | None) -> str:
        """Whose conversations a request continues: a configured device's key for any of its addresses."""
        device = self.cfg.device_for(peer)
        return device.key if device else peer or "unknown"

    def _prefix_start(self, client: str, body: dict, digested):
        """Cache diagnostics for one request. Any failure drops them for this request only and logs a fixed
        text: an exception message or traceback could quote message content."""
        try:
            if digested is DIGEST_FAILED:
                raise ValueError
            return self.prefix.start(client, body, digested)
        except Exception:  # noqa: BLE001
            print("kiln-gateway: prefix tracking failed; this request has no cache diagnostics", flush=True)
            return None

    async def _reject(self, rec: dict, status: int, message: str, kind: str,
                      headers: dict | None = None, **extra) -> web.Response:
        resp = _json_error(status, message, kind, **extra)
        if headers:
            resp.headers.update(headers)
        await self._finish(rec, status=status, error=kind)
        return resp

    async def _forward(self, request: web.Request, rec: dict, profile: Profile,
                       data: bytes, turn=None) -> web.StreamResponse:
        # The path as decoded and checked (path_refusal); the query as the client encoded it, so %23 or %2B
        # reach the engine unchanged.
        url = URL(f"http://127.0.0.1:{profile.port}").with_path(f"/v1/{rec['path']}")
        raw_query = request.rel_url.raw_query_string
        if raw_query:
            url = URL(f"{url}?{raw_query}", encoded=True)
        if not url.path.startswith("/v1/") or url.host != "127.0.0.1":  # path_refusal must have made sure
            await self._finish(rec, status=400, error="invalid_path")
            return _json_error(400, "the path leaves /v1/", "invalid_path")
        drop = DROP_REQUEST_HEADERS | connection_named(request.headers)
        headers = {k: v for k, v in request.headers.items() if k.lower() not in drop}
        obs = SSEObserver()
        rid = rec["id"]
        if is_generation(rec["path"]):  # utility calls never show as live streams
            self.hub.request_started(rid, {"ts": rec["ts_start"], "client": rec["client"],
                                           "profile": profile.name, "model": profile.model,
                                           "max_tokens": rec.get("max_tokens"),
                                           "conv": (rec.get("prefix") or {}).get("conv")}, obs)
            self._feed("in_flight", self.clock(), len(self.hub.active), profile.slots)
        resp: web.StreamResponse | None = None
        status, error, stats = None, None, obs.stats
        try:
            # No automatic Accept-Encoding: the observer must read plain bytes.
            async with self._session.request(request.method, url, data=data, headers=headers,
                                             skip_auto_headers=("Accept-Encoding",)) as up:
                status = up.status
                hop = HOP_HEADERS | connection_named(up.headers)
                out_headers = {k: v for k, v in up.headers.items() if k.lower() not in hop}
                if rec.get("adapted"):  # the only signal a client gets that, say, thinking was dropped
                    out_headers[ADAPTED_HEADER] = ", ".join(rec["adapted"])
                if up.headers.get("Content-Type", "").startswith("text/event-stream"):
                    resp = web.StreamResponse(status=up.status, headers=out_headers)
                    await resp.prepare(request)
                    async for chunk in up.content.iter_any():
                        had_token = stats.first_token is not None
                        obs.feed(chunk, self.clock())
                        if not had_token and stats.first_token is not None:
                            self.hub.first_token(rid, stats.first_token)
                        await resp.write(chunk)
                    await resp.write_eof()
                    return resp
                payload = await up.read()
                stats = observe_json_body(payload)
                stats.first_byte = self.clock()
                return web.Response(status=up.status, body=payload, headers=out_headers)
        except asyncio.CancelledError:
            status, error = _client_left(status, stats)  # leaving the block closes upstream
            raise
        except (ConnectionResetError, aiohttp.ClientConnectionResetError):
            status, error = _client_left(status, stats)
            return resp
        except aiohttp.ClientError as e:
            status, error = 502, f"upstream: {type(e).__name__}"
            if resp is None:
                return _json_error(502, f"engine on port {profile.port} unreachable: {type(e).__name__}",
                                   "upstream_unreachable")
            return resp  # mid-stream failure: the client sees the stream end
        finally:
            await self._finish(rec, status=status, error=error or stats.error, stats=stats)
            if status == 200:
                try:
                    self.prefix.finish(turn, stats.usage)
                except Exception:  # noqa: BLE001 (diagnostics must never fail a request)
                    print("kiln-gateway: prefix tracking failed at request end", flush=True)

    async def _finish(self, rec: dict, *, status, error=None, stats=None) -> None:
        now = self.clock()
        peer = rec["client"]["peer_ip"]
        info = self.whois.known(peer) or ({"name": self.cfg.host} if is_loopback(peer) else None)
        info = {k: info[k] for k in WHOIS_RECORD_KEYS if k in info} if info else None
        rec = {**rec, "client": {**rec["client"], **(info or {})}, "status": status, "error": error,
               "ts_end": now, "latency_s": round(now - rec["ts_start"], 4)}
        if stats is not None:
            rec.update(ts_first_byte=stats.first_byte, ts_first_token=stats.first_token,
                       ts_last_token=stats.last_token, token_events=stats.token_events,
                       chars=stats.chars, usage=stats.usage, finish=safe_text(stats.finish),
                       tokens_by_s=stats.histogram())
            if stats.first_token is not None:
                rec["ttft_s"] = round(stats.first_token - rec["ts_start"], 4)
        try:
            self.records.write(rec)
        except OSError as e:  # never fail a request over its record; the journal keeps the reason
            print(f"kiln-gateway: record write failed: {e}", flush=True)
        self.hub.request_ended(rec["id"], rec)
        if is_generation(rec["path"]):
            self._feed("finished", rec)
            self._feed("in_flight", now, len(self.hub.active))


async def start(cfg: Config, gateway: Gateway | None = None) -> list[web.AppRunner]:
    """Bind the gateway and its events feed. Returns [gateway runner, events runner]."""
    gw = gateway or Gateway(cfg)
    runners = []
    try:
        for app, (host, port) in ((gw.app(), cfg.gateway_listen), (gw.events_app(), cfg.events_listen)):
            # handler_cancellation: a client that hangs up cancels the handler, which closes the
            # upstream request so the engine stops generating for nobody.
            runner = web.AppRunner(app, handler_cancellation=True, access_log=None)
            await runner.setup()
            runners.append(runner)
            await web.TCPSite(runner, bind_host(host), port).start()
    except BaseException:
        for r in runners:
            await r.cleanup()
        raise
    return runners


async def serve(cfg: Config) -> None:
    runners = await start(cfg)
    print(f"kiln-gateway listening on {describe_addresses(runners[0].addresses)} "
          f"(events {describe_addresses(runners[1].addresses)})", flush=True)
    try:
        await asyncio.Event().wait()
    finally:
        for r in runners:
            await r.cleanup()
