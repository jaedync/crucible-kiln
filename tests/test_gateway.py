import asyncio
import json
import time

import aiohttp
import pytest
from aiohttp import web

from kiln.engines.admission import AdmissionHold
from kiln.engines.state import READY, STARTING, STOPPED, STOPPING, StateFile
from kiln.gateway.app import Gateway
from kiln.gateway.attribution import Whois


def sse(obj) -> bytes:
    return b"data: " + json.dumps(obj).encode() + b"\n\n"


STREAM = (sse({"choices": [{"index": 0, "delta": {"role": "assistant"}}]})
          + b"".join(sse({"choices": [{"index": 0, "delta": {"content": c}}]}) for c in ("a", "b", "c"))
          + sse({"choices": [{"index": 0, "delta": {}, "finish_reason": "stop"}]})
          + sse({"choices": [], "usage": {"prompt_tokens": 12, "completion_tokens": 3,
                                          "prompt_tokens_details": {"cached_tokens": 8}}})
          + b"data: [DONE]\n\n")


class FakeEngine:
    """Records what it received and streams STREAM in small, slightly delayed pieces."""

    def __init__(self, delay=0.01, hang=False, hold=False):
        # hold: send everything but [DONE], then keep the stream open (the client leaves first)
        self.bodies, self.headers, self.delay, self.hang, self.hold = [], [], delay, hang, hold
        self.raws = []
        self.disconnected = asyncio.Event()

    async def chat(self, request):
        self.raws.append(await request.read())
        self.bodies.append(await request.json())
        self.headers.append(dict(request.headers))
        if not self.bodies[-1].get("stream"):
            return web.json_response({"choices": [{"finish_reason": "stop", "message": {"content": "x"}}],
                                      "usage": {"prompt_tokens": 4, "completion_tokens": 1}})
        resp = web.StreamResponse(headers={"Content-Type": "text/event-stream", "X-Engine": "fake"})
        await resp.prepare(request)
        body = STREAM[:-len(b"data: [DONE]\n\n")] if self.hold else STREAM
        try:
            for i in range(0, len(body), 37):
                await resp.write(body[i:i + 37])
                await asyncio.sleep(self.delay)
            if self.hold:
                await asyncio.sleep(30)
            while self.hang:
                await resp.write(sse({"choices": [{"index": 0, "delta": {"content": "z"}}]}))
                await asyncio.sleep(0.02)
        except (ConnectionResetError, asyncio.CancelledError):
            self.disconnected.set()
            raise
        return resp


@pytest.fixture
async def setup(make_cfg, aiohttp_server, aiohttp_client):
    async def build(state=READY, profile="halogen-flashnext", engine=None, whois=None, hold_max_s=None):
        engine = engine or FakeEngine()
        eapp = web.Application(client_max_size=64 * 1024 ** 2)
        eapp.router.add_post("/v1/chat/completions", engine.chat)
        eserver = await aiohttp_server(eapp)
        over = {f"profiles.{profile}.port": eserver.port}
        if hold_max_s is not None:
            over["gateway.hold_max_s"] = hold_max_s
        cfg = make_cfg(**over)
        StateFile(cfg.state_file).set(profile, state)
        gw = Gateway(cfg, whois=whois or Whois(lookup=_no_lookup))
        client = await aiohttp_client(gw.app(), server_kwargs={"handler_cancellation": True})
        return gw, client, engine, cfg
    return build


async def _no_lookup(ip):
    return None


class FakeSubscriber:
    def __init__(self):
        self.events = []

    async def send_str(self, data):
        self.events.append(json.loads(data))

    async def close(self):
        pass


class BrokenSubscriber(FakeSubscriber):
    async def send_str(self, data):
        raise ValueError("boom")


def records(cfg):
    return [json.loads(l) for f in sorted(cfg.gateway_data.glob("*.jsonl")) for l in f.read_text().splitlines()]


async def test_stream_is_relayed_byte_for_byte_and_recorded(setup):
    gw, client, engine, cfg = await setup()
    sub, broken = FakeSubscriber(), BrokenSubscriber()
    gw.hub.clients.update({broken, sub})
    resp = await client.post("/v1/chat/completions", json={
        "model": "qwen3.8-flash-next", "stream": True, "max_tokens": 50,
        "messages": [{"role": "user", "content": "secret prompt"}]}, headers={"X-Kiln-Client": "pi-main"})
    assert resp.status == 200 and resp.headers["X-Engine"] == "fake"
    assert await resp.read() == STREAM
    sent = engine.bodies[0]
    assert sent["model"] == "halogen-qwen3.8-flash-next"
    assert sent["stream_options"] == {"include_usage": True}
    assert "x-kiln-client" not in {k.lower() for k in engine.headers[0]}
    (rec,) = records(cfg)
    assert rec["usage"] == {"prompt": 12, "cached": 8, "completion": 3, "reasoning": None}
    assert (rec["status"], rec["finish"], rec["token_events"], rec["model"]) == (200, "stop", 3, "qwen3.8-flash-next")
    # The device is the identity; the self-declared label is only metadata.
    assert (rec["client"]["key"], rec["client"]["label"]) == ("127.0.0.1", "pi-main") and rec["max_tokens"] == 50
    assert rec["ts_start"] <= rec["ts_first_token"] <= rec["ts_last_token"] <= rec["ts_end"]
    assert "secret prompt" not in json.dumps(rec)
    for _ in range(50):
        if sub.events and sub.events[-1]["type"] == "req.end":
            break
        await asyncio.sleep(0.02)
    events = sub.events
    assert broken not in gw.hub.clients and sub in gw.hub.clients
    kinds = [e["type"] for e in events]
    assert kinds[0] == "req.start" and kinds[1] == "req.first_token" and kinds[-1] == "req.end"
    assert sum(len(e["t"]) for e in events if e["type"] == "req.tokens") == 3
    # The live view finds the conversation's earlier turns by the same random id the record stores.
    assert events[0]["conv"] == rec["prefix"]["conv"] and len(events[0]["conv"]) == 12


async def test_alias_ids_are_accepted(setup):
    _, client, engine, _ = await setup()
    resp = await client.post("/v1/chat/completions", json={"model": "Qwen3.8 Flash Next", "stream": True})
    await resp.read()
    assert resp.status == 200 and engine.bodies[0]["model"] == "halogen-qwen3.8-flash-next"


async def test_wrong_model_is_409_and_recorded(setup):
    _, client, engine, cfg = await setup()
    resp = await client.post("/v1/chat/completions", json={"model": "qwen3.8-27b"})
    body = await resp.json()
    assert resp.status == 409 and body["error"]["loaded"] == "qwen3.8-flash-next"
    assert engine.bodies == [] and records(cfg)[0]["status"] == 409


async def test_not_serving_is_503_at_once(setup):
    # Stopped with no hold (the night window): nothing is about to serve, so a wait helps nobody.
    gw, client, _, cfg = await setup(state=STOPPED)
    t = time.monotonic()
    resp = await client.post("/v1/chat/completions", json={"model": "qwen3.8-flash-next"})
    assert resp.status == 503 and resp.headers["Retry-After"] == "30"
    assert time.monotonic() - t < 1
    (rec,) = records(cfg)
    assert rec["error"] == "engine_unavailable" and "held_s" not in rec


async def test_non_stream_json_is_relayed(setup):
    _, client, _, cfg = await setup()
    resp = await client.post("/v1/chat/completions", json={"model": "qwen3.8-flash-next"})
    assert (await resp.json())["usage"]["completion_tokens"] == 1
    assert records(cfg)[0]["usage"]["completion"] == 1


async def test_models_lists_only_the_active_model(setup):
    _, client, _, _ = await setup()
    data = (await (await client.get("/v1/models")).json())["data"]
    assert [m["id"] for m in data] == ["qwen3.8-flash-next"]


async def test_client_disconnect_closes_upstream_and_records_499(setup):
    engine = FakeEngine(hang=True)
    gw, client, engine, cfg = await setup(engine=engine)
    resp = await client.post("/v1/chat/completions", json={"model": "qwen3.8-flash-next", "stream": True})
    await resp.content.read(200)
    resp.close()
    await asyncio.wait_for(engine.disconnected.wait(), 5)
    for _ in range(50):
        if records(cfg):
            break
        await asyncio.sleep(0.05)
    (rec,) = records(cfg)
    assert rec["status"] == 499 and rec["error"] == "client_disconnected"
    assert gw.hub.active == {}


async def test_a_client_that_leaves_after_the_usage_report_completed_its_request(setup):
    # Pi closes the connection a few ms after the last token, before [DONE]: the answer was whole.
    gw, client, engine, cfg = await setup(engine=FakeEngine(hold=True))
    resp = await client.post("/v1/chat/completions", json={"model": "qwen3.8-flash-next", "stream": True})
    got = b""
    while b"usage" not in got:
        got += await resp.content.read(64)
    await asyncio.sleep(0.1)  # the gateway has relayed the usage chunk
    resp.close()
    await asyncio.wait_for(engine.disconnected.wait(), 5)
    for _ in range(50):
        if records(cfg):
            break
        await asyncio.sleep(0.05)
    (rec,) = records(cfg)
    assert (rec["status"], rec["error"], rec["finish"], rec["usage"]["completion"]) == (200, None, "stop", 3)


async def test_unreachable_engine_is_502(setup, make_cfg, aiohttp_client):
    _, _, _, cfg = await setup()
    cfg2 = make_cfg(**{"profiles.halogen-flashnext.port": 1})  # nothing listens on port 1
    StateFile(cfg2.state_file).set("halogen-flashnext", READY)
    client = await aiohttp_client(Gateway(cfg2, whois=Whois(lookup=_no_lookup)).app())
    resp = await client.post("/v1/chat/completions", json={"model": "qwen3.8-flash-next"})
    assert resp.status == 502 and records(cfg2)[0]["status"] == 502


async def test_tailscale_identity_is_merged_into_the_record(setup):
    async def lookup(ip):
        return {"node": "owner-laptop", "name": "Owner's laptop", "os": "linux", "user": "o@example.com"}

    whois = Whois(lookup=lookup)
    whois._cache["127.0.0.1"] = (float("inf"), await lookup("127.0.0.1"))
    _, client, _, cfg = await setup(whois=whois)
    await (await client.post("/v1/chat/completions", json={"model": "qwen3.8-flash-next", "stream": True})).read()
    c = records(cfg)[0]["client"]
    assert (c["key"], c["node"], c["name"]) == ("127.0.0.1", "owner-laptop", "Owner's laptop")


async def test_loopback_without_whois_is_named_after_the_host(setup):
    _, client, _, cfg = await setup()
    await (await client.post("/v1/chat/completions", json={"model": "qwen3.8-flash-next", "stream": True},
                             headers={"X-Kiln-Client": "eval-night"})).read()
    c = records(cfg)[0]["client"]
    assert (c["key"], c["name"], c["label"]) == ("127.0.0.1", cfg.host, "eval-night")


async def test_eval_lock_reserves_the_gateway_for_eval_traffic(setup):
    from kiln.engines.lock import EvalLock
    _, client, engine, cfg = await setup()
    token = EvalLock(cfg.lock_file).acquire("night", until=1e12)
    resp = await client.post("/v1/chat/completions", json={"model": "qwen3.8-flash-next", "stream": True})
    assert resp.status == 503 and "evaluation night running" in (await resp.json())["error"]["message"]
    resp = await client.post("/v1/chat/completions", json={"model": "qwen3.8-flash-next", "stream": True},
                             headers={"X-Kiln-Client": "eval-halogen-ar"})  # a label alone isn't enough
    assert resp.status == 503
    resp = await client.post("/v1/chat/completions", json={"model": "qwen3.8-flash-next", "stream": True},
                             headers={"X-Kiln-Client": "eval-halogen-ar", "X-Kiln-Eval-Token": token})
    assert resp.status == 200 and await resp.read() == STREAM
    assert "x-kiln-eval-token" not in {k.lower() for k in engine.headers[-1]}


async def test_utility_calls_are_relayed_but_never_live(setup):
    gw, client, engine, cfg = await setup()  # the fake engine has no count_tokens; its 404 is still relayed
    sub = FakeSubscriber()
    gw.hub.clients.add(sub)
    resp = await client.post("/v1/messages/count_tokens", json={"model": "qwen3.8-flash-next", "messages": []})
    await resp.read()
    for _ in range(20):
        if sub.events:
            break
        await asyncio.sleep(0.02)
    assert [e["type"] for e in sub.events] == ["req.end"]
    assert records(cfg)[0]["path"] == "messages/count_tokens"


# -- admission hold ------------------------------------------------------------
@pytest.fixture
def fast_poll(monkeypatch):
    monkeypatch.setattr("kiln.gateway.app.HOLD_POLL_S", 0.02)


async def until(cond, timeout=5.0):
    deadline = time.monotonic() + timeout
    while not cond():
        assert time.monotonic() < deadline, "condition not met in time"
        await asyncio.sleep(0.02)


async def health(client):
    return await (await client.get("/health")).json()


async def test_a_held_request_waits_for_the_switch_then_is_forwarded(setup, fast_poll):
    gw, client, engine, cfg = await setup(state=STOPPED)
    AdmissionHold(cfg.admission_file).write(600, "switch")
    task = asyncio.create_task(client.post("/v1/chat/completions",
                                           json={"model": "qwen3.8-flash-next", "stream": True}))
    await until(lambda: gw.held == 1)
    h = await health(client)
    assert (h["held"], h["admission"], h["in_flight"]) == (1, "held", 0)  # a drain can still reach 0
    # /v1/models never waits: it answers at once with the (empty) list
    assert (await (await client.get("/v1/models")).json())["data"] == []
    StateFile(cfg.state_file).set("halogen-flashnext", READY)
    await asyncio.sleep(0.1)
    assert engine.bodies == [] and gw.held == 1  # serving, but the hold still keeps it back
    AdmissionHold(cfg.admission_file).release()
    resp = await asyncio.wait_for(task, 5)
    assert resp.status == 200 and await resp.read() == STREAM
    (rec,) = records(cfg)
    assert rec["status"] == 200 and rec["held_s"] >= 0.1
    assert rec["profile"] == "halogen-flashnext" and rec["engine"] == "halogen"
    h = await health(client)
    assert (h["held"], h["admission"], h["in_flight"]) == (0, "open", 0)


async def test_health_without_a_hold_reports_open(setup):
    _, client, _, _ = await setup()
    h = await health(client)
    assert (h["held"], h["admission"]) == (0, "open") and h["state"] == READY and h["in_flight"] == 0


OLD_HEALTH_KEYS = {"ok": bool, "v": int, "profile": str, "state": str, "model": str, "in_flight": int,
                   "held": int, "admission": str}


async def test_health_keeps_every_old_key_with_its_type(setup):
    _, client, _, _ = await setup()
    h = await health(client)
    assert {k: type(h[k]) for k in OLD_HEALTH_KEYS} == OLD_HEALTH_KEYS
    assert (h["ok"], h["v"], h["profile"], h["model"]) == (True, 1, "halogen-flashnext", "qwen3.8-flash-next")
    assert set(h) == set(OLD_HEALTH_KEYS) | {"capacity", "last_5m", "last_1h"}


@pytest.mark.parametrize("n, free, queued, utilization, busy", [(0, 4, 0, 0.0, False), (3, 1, 0, 0.75, False),
                                                               (4, 0, 0, 1.0, True), (5, 0, 1, 1.25, True)])
async def test_capacity_against_four_slots(setup, n, free, queued, utilization, busy):
    gw, client, _, _ = await setup()
    gw.hub.active.update({f"r{i}": {} for i in range(n)})  # in_flight is the size of this map
    h = await health(client)
    assert h["in_flight"] == n
    assert h["capacity"] == {"slots": 4, "in_flight": n, "free": free, "queued": queued,
                              "utilization": utilization, "busy": busy}


@pytest.mark.parametrize("state", [STOPPED, STARTING, STOPPING])
async def test_capacity_without_a_serving_profile_is_busy_with_no_slots(setup, state):
    _, client, _, _ = await setup(state=state)
    assert (await health(client))["capacity"] == {"slots": None, "in_flight": 0, "free": None, "queued": None,
                                                  "utilization": None, "busy": True}


async def test_capacity_is_busy_while_admission_is_held(setup):
    _, client, _, cfg = await setup()
    AdmissionHold(cfg.admission_file).write(600, "switch")
    c = (await health(client))["capacity"]
    assert (c["slots"], c["free"], c["busy"]) == (4, 4, True)


async def test_health_windows_count_requests_and_never_name_a_client(setup):
    gw, client, _, _ = await setup()
    for _ in range(2):
        resp = await client.post("/v1/chat/completions", json={"model": "qwen3.8-flash-next", "stream": True},
                                 headers={"X-Kiln-Client": "secret-label"})
        await resp.read()
    await (await client.post("/v1/chat/completions", json={"model": "other-model"})).read()  # 409
    h = await health(client)
    for key in ("last_5m", "last_1h"):
        s = h[key]
        assert (s["started"], s["finished"], s["ok"], s["client_error"]) == (3, 3, 2, 1)
        assert (s["prompt_tokens"], s["completion_tokens"], s["in_flight_peak"]) == (24, 6, 1)
        # No record files: a seed that worked, so the whole window is known (it was idle).
        assert s["ttft_s_p50"] is not None and s["covered_s"] == {"last_5m": 300, "last_1h": 3600}[key]
    assert "secret-label" not in json.dumps(h) and "127.0.0.1" not in json.dumps(h)


async def test_startup_seeds_the_last_hour_from_the_records(setup, make_cfg, aiohttp_client):
    cfg = make_cfg()
    now = time.time()
    cfg.gateway_data.mkdir(parents=True, exist_ok=True)
    line = {"v": 1, "path": "chat/completions", "profile": "halogen-flashnext", "served_model": "x",
            "status": 200, "usage": {"prompt": 7, "completion": 5}}
    with open(cfg.gateway_data / time.strftime("requests-%Y-%m-%d.jsonl", time.gmtime(now)), "w") as f:
        f.write(json.dumps({**line, "ts_start": now - 20, "ts_end": now - 10}) + "\n{bad line\n")
    StateFile(cfg.state_file).set("halogen-flashnext", READY)
    client = await aiohttp_client(Gateway(cfg, whois=Whois(lookup=_no_lookup)).app())
    h = await health(client)
    assert (h["last_1h"]["finished"], h["last_1h"]["completion_tokens"]) == (1, 5)
    assert h["last_1h"]["covered_s"] >= 3599 and h["last_5m"]["in_flight_peak"] == 1


async def test_a_failed_seed_reports_a_partial_window_and_startup_goes_on(setup, monkeypatch):
    def broken(*_a, **_k):
        raise OSError("disk gone")
    monkeypatch.setattr("kiln.gateway.app.read_tail", broken)
    _, client, _, _ = await setup()
    h = await health(client)
    assert h["ok"] and h["last_1h"]["covered_s"] < 60 and h["last_1h"]["finished"] == 0


async def test_health_answers_when_the_window_fails(setup, monkeypatch):
    gw, client, _, _ = await setup()

    def broken(*_a, **_k):
        raise ValueError("bug")
    monkeypatch.setattr(gw.usage, "summary", broken)
    h = await health(client)
    assert h["ok"] and h["in_flight"] == 0 and h["capacity"]["slots"] == 4
    assert h["last_5m"] is None and h["last_1h"] is None


async def test_health_after_a_busy_hour_is_fast(setup):
    from tests.test_usage_window import busy_hour
    gw, _, _, _ = await setup()
    busy_hour(gw.usage, time.time())
    times = []
    for _ in range(300):
        t = time.perf_counter()
        await gw.health(None)
        times.append(time.perf_counter() - t)
    times.sort()
    # The median, not p99: a slow summary makes every call slow, while a p99 on a shared CI runner
    # measures the scheduler.
    assert times[len(times) // 2] < 0.005


async def test_a_switch_in_progress_holds_without_a_hold_file(setup, fast_poll):
    gw, client, engine, cfg = await setup(state=STARTING)
    task = asyncio.create_task(client.post("/v1/chat/completions", json={"model": "qwen3.8-flash-next"}))
    await until(lambda: gw.held == 1)
    assert (await health(client))["admission"] == "open"
    StateFile(cfg.state_file).set("halogen-flashnext", READY)
    resp = await asyncio.wait_for(task, 5)
    assert resp.status == 200 and len(engine.bodies) == 1


async def test_a_held_request_goes_to_the_engine_that_serves_after_the_switch(setup, fast_poll):
    gw, client, engine, cfg = await setup()
    StateFile(cfg.state_file).set("gufo-qwen27", STOPPING, "halogen-flashnext")
    task = asyncio.create_task(client.post("/v1/chat/completions", json={"model": "qwen3.8-flash-next"}))
    await until(lambda: gw.held == 1)
    StateFile(cfg.state_file).set("halogen-flashnext", STARTING)
    await asyncio.sleep(0.05)
    StateFile(cfg.state_file).set("halogen-flashnext", READY)
    resp = await asyncio.wait_for(task, 5)
    assert resp.status == 200
    (rec,) = records(cfg)
    assert (rec["profile"], rec["engine"], rec["served_model"]) == ("halogen-flashnext", "halogen",
                                                                    "halogen-qwen3.8-flash-next")


async def test_a_switch_that_ends_stopped_releases_held_requests_with_503(setup, fast_poll):
    gw, client, _, cfg = await setup(state=STOPPING)
    task = asyncio.create_task(client.post("/v1/chat/completions", json={"model": "qwen3.8-flash-next"}))
    await until(lambda: gw.held == 1)
    StateFile(cfg.state_file).set(None, STOPPED)  # a plain stop, no hold: nothing will serve
    resp = await asyncio.wait_for(task, 5)
    assert resp.status == 503 and records(cfg)[0]["error"] == "engine_unavailable"


async def test_the_cap_ends_the_wait_with_503(setup, fast_poll):
    gw, client, engine, cfg = await setup(state=STOPPED, hold_max_s=0.3)
    AdmissionHold(cfg.admission_file).write(600)
    t = time.monotonic()
    resp = await client.post("/v1/chat/completions", json={"model": "qwen3.8-flash-next"})
    waited = time.monotonic() - t
    assert resp.status == 503 and resp.headers["Retry-After"] == "30"
    assert (await resp.json())["error"]["type"] == "engine_unavailable"
    assert 0.3 <= waited < 2 and engine.bodies == [] and gw.held == 0
    (rec,) = records(cfg)
    assert rec["status"] == 503 and rec["held_s"] >= 0.25


async def test_the_cap_forwards_if_an_engine_serves_while_the_hold_stays(setup, fast_poll):
    _, client, engine, cfg = await setup(hold_max_s=0.2)
    AdmissionHold(cfg.admission_file).write(600)
    resp = await client.post("/v1/chat/completions", json={"model": "qwen3.8-flash-next"})
    assert resp.status == 200 and len(engine.bodies) == 1


@pytest.mark.parametrize("text", [None, "{not json"])
async def test_an_expired_or_broken_hold_file_does_not_hold(setup, fast_poll, text):
    _, client, _, cfg = await setup(state=STOPPED)
    if text is None:
        cfg.admission_file.write_text(json.dumps({"v": 1, "until": time.time() - 1, "reason": "old"}))
    else:
        cfg.admission_file.write_text(text)
    t = time.monotonic()
    resp = await client.post("/v1/chat/completions", json={"model": "qwen3.8-flash-next"})
    assert resp.status == 503 and time.monotonic() - t < 1
    assert (await health(client))["admission"] == "open"


async def test_hold_max_s_zero_turns_the_hold_off(setup, fast_poll):
    gw, client, _, cfg = await setup(state=STARTING, hold_max_s=0)
    AdmissionHold(cfg.admission_file).write(600)
    t = time.monotonic()
    resp = await client.post("/v1/chat/completions", json={"model": "qwen3.8-flash-next"})
    assert resp.status == 503 and time.monotonic() - t < 1
    assert "held_s" not in records(cfg)[0]


async def test_a_client_that_leaves_while_held_is_recorded_as_499(setup, fast_poll):
    gw, client, engine, cfg = await setup(state=STOPPED)
    AdmissionHold(cfg.admission_file).write(600)
    task = asyncio.create_task(client.post("/v1/chat/completions",
                                           json={"model": "qwen3.8-flash-next", "stream": True}))
    await until(lambda: gw.held == 1)
    task.cancel()  # closes the connection
    await until(lambda: records(cfg))
    (rec,) = records(cfg)
    assert (rec["status"], rec["error"]) == (499, "client_disconnected") and "held_s" in rec
    assert gw.held == 0 and gw.hub.active == {}
    StateFile(cfg.state_file).set("halogen-flashnext", READY)
    AdmissionHold(cfg.admission_file).release()
    await asyncio.sleep(0.1)
    assert engine.bodies == [] and len(records(cfg)) == 1


async def test_a_held_client_that_leaves_is_noticed_without_handler_cancellation(setup, fast_poll):
    # aiohttp's test server always cancels handlers; a plain runner shows the wait's own check.
    _, _, engine, cfg = await setup(state=STOPPED)
    AdmissionHold(cfg.admission_file).write(600)
    gw = Gateway(cfg, whois=Whois(lookup=_no_lookup))
    runner = web.AppRunner(gw.app(), handler_cancellation=False)
    await runner.setup()
    try:
        await web.TCPSite(runner, "127.0.0.1", 0).start()
        port = runner.addresses[0][1]
        async with aiohttp.ClientSession() as s:
            task = asyncio.create_task(s.post(f"http://127.0.0.1:{port}/v1/chat/completions",
                                              json={"model": "qwen3.8-flash-next"}))
            await until(lambda: gw.held == 1)
            task.cancel()
            await until(lambda: records(cfg))
        (rec,) = records(cfg)
        assert (rec["status"], rec["error"]) == (499, "client_disconnected") and gw.held == 0
        assert engine.bodies == []
    finally:
        await runner.cleanup()


async def test_a_held_request_respects_an_evaluation_lock_taken_meanwhile(setup, fast_poll):
    from kiln.engines.lock import EvalLock
    gw, client, engine, cfg = await setup(state=STARTING)
    task = asyncio.create_task(client.post("/v1/chat/completions", json={"model": "qwen3.8-flash-next"}))
    await until(lambda: gw.held == 1)
    EvalLock(cfg.lock_file).acquire("night", until=1e12)
    StateFile(cfg.state_file).set("halogen-flashnext", READY)
    resp = await asyncio.wait_for(task, 5)
    assert resp.status == 503 and (await resp.json())["error"]["type"] == "evaluation_running"
    assert engine.bodies == []


async def test_utility_calls_wait_too(setup, fast_poll):
    gw, client, _, cfg = await setup(state=STARTING)
    task = asyncio.create_task(client.post("/v1/messages/count_tokens",
                                           json={"model": "qwen3.8-flash-next", "messages": []}))
    await until(lambda: gw.held == 1)
    StateFile(cfg.state_file).set("halogen-flashnext", READY)
    resp = await asyncio.wait_for(task, 5)
    assert resp.status == 404  # the fake engine's own answer: the call was forwarded
    assert records(cfg)[0]["path"] == "messages/count_tokens"


async def test_a_body_with_nothing_to_rewrite_reaches_the_engine_byte_for_byte(setup):
    gw, client, engine, cfg = await setup()
    raw = b'{"model":"halogen-qwen3.8-flash-next",\n "messages":[{"role":"user","content":"\\u00e9 x"}]}'
    resp = await client.post("/v1/chat/completions", data=raw, headers={"Content-Type": "application/json"})
    assert resp.status == 200
    assert engine.raws == [raw]


async def test_a_rewritten_body_reaches_the_engine_with_the_same_value(setup):
    gw, client, engine, cfg = await setup()
    msgs = [{"role": "system", "content": "s"}, {"role": "user", "content": "caf\u00e9 \ud83d cut"}]
    body = {"model": "qwen3.8-flash-next", "stream": True, "stream_options": {"x": 1}, "messages": msgs}
    raw = json.dumps(body, separators=(",", ":")).encode()
    await (await client.post("/v1/chat/completions", data=raw)).read()
    sent = {**body, "model": "halogen-qwen3.8-flash-next", "stream_options": {"x": 1, "include_usage": True}}
    assert engine.bodies == [sent]


async def test_a_large_body_does_not_block_the_event_loop(setup):
    """A probe on the gateway's loop must keep running while a large conversation goes through. The test
    client and engine share the loop and copy the bytes, so the bound is relative to the same bytes sent
    as an opaque body, which the gateway forwards without parsing."""
    import random

    class ReadOnly(FakeEngine):
        async def chat(self, request):
            await request.read()  # the engine side of this test must not parse on the shared loop
            return web.json_response({"choices": [{"finish_reason": "stop", "message": {"content": "x"}}]})

    gw, client, engine, cfg = await setup(engine=ReadOnly())
    rng = random.Random(5)
    msgs = [{"role": "user", "content": " ".join(rng.choice(["alpha", "beta", "gamma"]) for _ in range(800))}
            for _ in range(1600)]
    raw = json.dumps({"model": "qwen3.8-flash-next", "messages": msgs}).encode()  # about 8 MB

    async def worst_gap(data: bytes) -> float:
        gaps, stop = [], asyncio.Event()

        async def probe():
            loop = asyncio.get_running_loop()
            last = loop.time()
            while not stop.is_set():
                await asyncio.sleep(0.001)
                now = loop.time()
                gaps.append(now - last)
                last = now

        task = asyncio.create_task(probe())
        await asyncio.sleep(0.02)
        resp = await client.post("/v1/chat/completions", data=data)
        assert resp.status == 200
        stop.set()
        await task
        return max(gaps)

    for attempt in range(3):  # a shared machine can stall any one pass; a real block stalls all of them
        opaque = max([await worst_gap(b"x" * len(raw)) for _ in range(2)])
        # The second request continues the conversation of the first.
        parsed = max([await worst_gap(raw) for _ in range(2)])
        if parsed < opaque + 0.012:
            break
    assert len(records(cfg)) == 4 * (attempt + 1) and "prefix" in records(cfg)[-1]
    assert parsed < opaque + 0.012, f"loop blocked for {parsed * 1000:.1f} ms ({opaque * 1000:.1f} ms opaque)"
