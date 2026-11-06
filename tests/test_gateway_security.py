"""Attacks from the security review against the gateway (H1, H2, M2, M3, M4, L7, Info)."""
import asyncio
import json

import aiohttp
import pytest
from aiohttp import web

from kiln import config
from kiln.engines.lock import EvalLock
from kiln.engines.state import READY, STOPPED, StateFile
from kiln.gateway.app import Gateway
from kiln.gateway.attribution import Whois, parse_whois
from tests.test_gateway import FakeEngine, fast_poll, records, setup  # noqa: F401 (fixtures)

CHAT = "/v1/chat/completions"
GOOD = {"model": "qwen3.8-flash-next", "messages": [{"role": "user", "content": "hi"}]}


# -- H1: DNS rebinding ---------------------------------------------------------------------------------------
async def test_a_rebound_host_can_not_use_the_model(setup):
    gw, client, engine, cfg = await setup()
    r = await client.post(CHAT, json=GOOD, headers={"Host": "rebind.attacker.example:8000"})
    assert r.status == 403 and engine.bodies == []
    assert (await client.get("/health", headers={"Host": "rebind.attacker.example:8000"})).status == 403


async def test_own_and_container_names_reach_the_model(setup):
    gw, client, engine, cfg = await setup()
    for host in ("localhost:8000", "test.local:8000", "host.containers.internal:8000", "192.0.2.7:8000"):
        assert (await client.post(CHAT, json=GOOD, headers={"Host": host})).status == 200, host


async def test_gateway_allowed_hosts_adds_names(make_cfg, aiohttp_client):
    cfg = make_cfg(**{"gateway.allowed_hosts": ["llm.example.org"]})
    gw = Gateway(cfg, whois=Whois(lookup=_none), tailscale=None)
    client = await aiohttp_client(gw.app())
    assert (await client.get("/health", headers={"Host": "llm.example.org"})).status == 200
    assert (await client.get("/health", headers={"Host": "other.example.org"})).status == 403


async def test_the_magicdns_name_is_accepted_after_startup(make_cfg, aiohttp_client):
    async def ts():
        return {"dns_name": "box.tailnet.ts.net", "tailnet": "tailnet.ts.net"}
    gw = Gateway(make_cfg(), whois=Whois(lookup=_none), tailscale=ts)
    client = await aiohttp_client(gw.app())
    assert (await client.get("/health", headers={"Host": "box.tailnet.ts.net:8000"})).status == 200


async def _none(_ip):
    return None


# -- H2: what a record keeps of the client's values ------------------------------------------------------------
@pytest.mark.parametrize("model", [{"x": 1}, ["m"], 7, "m" * 300, "bad\nname", "a\"b"])
async def test_a_rejected_request_records_an_odd_model_as_null(setup, model):
    gw, client, engine, cfg = await setup(state=STOPPED, hold_max_s=0)
    r = await client.post(CHAT, json={**GOOD, "model": model})
    assert r.status == 503
    (rec,) = records(cfg)
    assert rec["model"] is None


async def test_a_plain_model_id_is_recorded(setup):
    gw, client, engine, cfg = await setup(state=STOPPED, hold_max_s=0)
    await client.post(CHAT, json={**GOOD, "model": "Qwen/Qwen3.8-27B:q8 (test)"})
    assert records(cfg)[0]["model"] == "Qwen/Qwen3.8-27B:q8 (test)"


async def test_an_engine_finish_that_is_not_a_short_name_is_recorded_as_null(setup):
    class OddEngine(FakeEngine):
        async def chat(self, request):
            await request.read()
            return web.json_response({"choices": [{"finish_reason": {"x": 1}, "message": {"content": "x"}}],
                                      "usage": {"prompt_tokens": 4, "completion_tokens": 1}})
    gw, client, engine, cfg = await setup(engine=OddEngine())
    assert (await client.post(CHAT, json=GOOD)).status == 200
    assert records(cfg)[0]["finish"] is None


# -- M2: the relayed path -------------------------------------------------------------------------------------
class CatchAll:
    def __init__(self):
        self.paths = []

    async def any(self, request):
        self.paths.append(request.raw_path)
        return web.json_response({"ok": True})


@pytest.fixture
async def catch_all(make_cfg, aiohttp_server, aiohttp_client):
    engine = CatchAll()
    eapp = web.Application()
    eapp.router.add_route("*", "/{tail:.*}", engine.any)
    eserver = await aiohttp_server(eapp)
    cfg = make_cfg(**{"profiles.halogen-flashnext.port": eserver.port})
    StateFile(cfg.state_file).set("halogen-flashnext", READY)
    gw = Gateway(cfg, whois=Whois(lookup=_none), tailscale=None)
    return engine, await aiohttp_client(gw.app()), cfg


@pytest.mark.parametrize("path", ["/v1/%2e%2e/admin", "/v1/..%2Fadmin", "/v1/%2E%2E%2F%2E%2E%2Fslots%3Faction=erase",
                                  "/v1/chat/../../slots", "/v1/a%5Cb", "/v1/a%23b", "/v1/a%3Fb",
                                  "/v1/a%00b", "/v1/a%0d%0aX:1", "/v1/a%2fb"])
async def test_a_path_that_leaves_v1_is_refused(catch_all, path):
    engine, client, cfg = catch_all
    r = await client.get(aiohttp.client.URL(path, encoded=True))
    # A plain ../ is resolved before routing, and the route takes no line break: those are 404s.
    assert r.status == (404 if path in ("/v1/chat/../../slots", "/v1/a%0d%0aX:1") else 400), path
    assert engine.paths == []


async def test_plain_paths_and_the_query_still_pass(catch_all):
    engine, client, cfg = catch_all
    r = await client.get(aiohttp.client.URL("/v1/embeddings/x-y_z.1?api-version=2", encoded=True))
    assert r.status == 200 and engine.paths == ["/v1/embeddings/x-y_z.1?api-version=2"]


# -- M3: body size and memory -----------------------------------------------------------------------------------
def test_the_body_cap_and_held_cap_are_configurable(make_cfg):
    cfg = make_cfg()
    assert cfg.max_body_mib == 32 and cfg.max_held == 32
    cfg = make_cfg(**{"gateway.max_body_mib": 4, "gateway.max_held": 2})
    assert cfg.max_body_mib == 4 and cfg.max_held == 2
    for bad in (0, -1, "x", 5000):
        with pytest.raises(config.ConfigError):
            make_cfg(**{"gateway.max_body_mib": bad})


async def test_a_body_over_the_cap_is_413(setup, make_cfg):
    gw, client, engine, cfg = await setup()
    gw.cfg = cfg = make_cfg(**{"gateway.max_body_mib": 1, f"profiles.halogen-flashnext.port":
                               cfg.profiles["halogen-flashnext"].port})
    client2 = await _client_for(gw)
    r = await client2.post(CHAT, data=b"x" * ((1 << 20) + 1))
    assert r.status == 413


async def _client_for(gw):
    from aiohttp.test_utils import TestClient, TestServer
    c = TestClient(TestServer(gw.app()))
    await c.start_server()
    _OPEN.append(c)
    return c


_OPEN = []


@pytest.fixture(autouse=True)
async def _close_clients():
    yield
    while _OPEN:
        await _OPEN.pop().close()


async def test_a_vision_request_with_several_photos_fits_the_default_cap(setup):
    import base64
    import os
    gw, client, engine, cfg = await setup()
    content = [{"type": "text", "text": "compare"}] + [
        {"type": "image_url", "image_url": {"url": "data:image/jpeg;base64," + base64.b64encode(
            os.urandom(4_000_000)).decode()}} for _ in range(4)]
    r = await client.post(CHAT, json={**GOOD, "messages": [{"role": "user", "content": content}]})
    assert r.status == 200


async def test_a_body_of_countless_tiny_objects_is_413(setup):
    gw, client, engine, cfg = await setup()
    one = b'{"role":"user","content":"x"}'
    raw = b'{"model":"qwen3.8-flash-next","messages":[' + b",".join([one] * 300_000) + b"]}"
    r = await client.post(CHAT, data=raw)
    assert r.status == 413 and engine.bodies == []
    assert records(cfg)[-1]["status"] == 413


async def test_held_requests_are_capped(setup, fast_poll):
    gw, client, engine, cfg = await setup(state=STOPPED, hold_max_s=5)
    gw.cfg = cfg = config.parse({**_raw(cfg), "gateway": {**_raw(cfg)["gateway"], "max_held": 1, "hold_max_s": 5}})
    from kiln.engines.admission import AdmissionHold
    AdmissionHold(cfg.admission_file).write(60, "switch")
    first = asyncio.create_task(client.post(CHAT, json=GOOD))
    for _ in range(100):
        if gw.held:
            break
        await asyncio.sleep(0.01)
    r = await client.post(CHAT, json=GOOD)
    assert r.status == 503 and (await r.json())["error"]["type"] == "too_many_held"
    first.cancel()


def _raw(cfg):
    from tests.conftest import BASE
    import copy
    d = copy.deepcopy(BASE)
    d["gateway"]["data_dir"] = str(cfg.gateway_data)
    d["gateway"]["state_file"] = str(cfg.state_file)
    d["dash"]["db"] = str(cfg.db_path)
    d["profiles"]["halogen-flashnext"]["port"] = cfg.profiles["halogen-flashnext"].port
    return d


def test_prefix_tracking_skips_a_request_with_too_many_messages():
    from kiln.gateway import prefix
    t = prefix.PrefixTracker()
    msgs = [{"role": "user", "content": str(i)} for i in range(prefix.MAX_MESSAGES + 1)]
    assert t.digests({"messages": msgs}) is None
    assert t.digests({"messages": msgs[:prefix.MAX_MESSAGES]}) is not None


def test_memory_max_follows_the_body_cap():
    from kiln import install
    from kiln.gateway.app import memory_max_mib
    small = memory_max_mib(8)
    big = memory_max_mib(64)
    assert big > small >= 512
    assert f"MemoryMax={memory_max_mib(32)}M" in install.render_service(
        "kiln-gateway", "/bin/kiln", install.Path("/c.toml"))


# -- M4: the events feed and the record -----------------------------------------------------------------------
async def test_events_refuse_a_browser(make_cfg, aiohttp_client):
    gw = Gateway(make_cfg(), whois=Whois(lookup=_none), tailscale=None)
    client = await aiohttp_client(gw.events_app())
    gw.hub.start_tasks()  # the gateway app's startup does this
    with pytest.raises(aiohttp.WSServerHandshakeError) as e:
        await client.ws_connect("/events", headers={"Origin": "https://evil.example"})
    assert e.value.status == 403
    with pytest.raises(aiohttp.WSServerHandshakeError):
        await client.ws_connect("/events", headers={"Host": "rebind.attacker.example:8001"})
    ws = await client.ws_connect("/events")  # the dashboard sends no Origin
    assert json.loads((await ws.receive(timeout=5)).data)["type"] == "hello"
    await ws.close()
    await gw.hub.stop_tasks()


@pytest.mark.parametrize("listen", ["*:8001", "0.0.0.0:8001", "10.0.0.2:8001", "[::]:8001"])
def test_events_must_listen_on_loopback(make_cfg, listen):
    with pytest.raises(config.ConfigError):
        make_cfg(**{"gateway.events_listen": listen})
    make_cfg(**{"gateway.events_listen": "[::1]:8001"})


async def test_records_keep_the_device_but_not_the_login_or_os(setup):
    obj = {"Node": {"Name": "owner-laptop.tailnet.ts.net.", "Hostinfo": {"Hostname": "Owner laptop", "OS": "iOS"}},
           "UserProfile": {"LoginName": "friend@example.com"}}

    async def lookup(_ip):
        return parse_whois(obj)
    whois = Whois(lookup=lookup)
    gw, client, engine, cfg = await setup(whois=whois)
    whois._cache["127.0.0.1"] = (1e18, parse_whois(obj))
    await client.post(CHAT, json=GOOD)
    c = records(cfg)[0]["client"]
    assert c["node"] == "owner-laptop" and c["name"] == "Owner laptop"
    assert "user" not in c and "os" not in c and "friend@example.com" not in json.dumps(records(cfg))


# -- L7 ------------------------------------------------------------------------------------------------------
async def test_a_non_ascii_eval_token_is_refused_not_a_500(setup):
    gw, client, engine, cfg = await setup()
    import time
    EvalLock(cfg.lock_file).acquire("bench", time.time() + 600)
    r = await client.post(CHAT, json=GOOD, headers={"X-Kiln-Eval-Token": "t\u00e9st"})
    assert r.status == 503


async def test_deeply_nested_json_is_never_a_500(setup):
    # Python 3.13 raises RecursionError at about 1,000 levels (then a 400); newer versions parse it.
    gw, client, engine, cfg = await setup()
    r = await client.post(CHAT, data=b'{"x":' + b"[" * 50_000 + b"]" * 50_000 + b"}")
    assert r.status != 500 and len(records(cfg)) == 1
    if r.status == 400:
        assert engine.bodies == [] and records(cfg)[0]["status"] == 400


@pytest.mark.parametrize("where", ["kiln.gateway.app.parse", "kiln.gateway.app.Gateway._prepare"])
async def test_a_recursion_error_is_a_400_with_a_record(setup, monkeypatch, where):
    def deep(*_a):
        raise RecursionError
    monkeypatch.setattr(where, deep)
    gw, client, engine, cfg = await setup()
    r = await client.post(CHAT, json=GOOD)
    assert r.status == 400 and engine.bodies == [] and records(cfg)[-1]["status"] == 400


async def test_a_tool_schema_of_ordinary_depth_passes(setup):
    gw, client, engine, cfg = await setup()
    schema = {"type": "string"}
    for _ in range(20):
        schema = {"type": "object", "properties": {"a": schema}}
    r = await client.post(CHAT, json={**GOOD, "tools": [{"type": "function", "function": {"parameters": schema}}]})
    assert r.status == 200


# -- Info: headers --------------------------------------------------------------------------------------------
async def test_no_accept_encoding_and_no_connection_named_headers_reach_the_engine(setup):
    gw, client, engine, cfg = await setup()
    await client.post(CHAT, json=GOOD, headers={"Connection": "keep-alive, X-Secret-Hop", "X-Secret-Hop": "1",
                                                "Accept-Encoding": "gzip"})
    sent = {k.lower() for k in engine.headers[0]}
    assert "accept-encoding" not in sent and "x-secret-hop" not in sent


async def test_a_slow_large_body_gives_up_its_parse_slot(setup, monkeypatch):
    monkeypatch.setattr("kiln.gateway.app.BODY_READ_TIMEOUT_S", 0.2)
    gw, client, engine, cfg = await setup()

    async def slow():
        yield b'{"model": "qwen3.8-flash-next", "x": "'
        await asyncio.sleep(2)
        yield b'"}'
    r = await client.post(CHAT, data=slow(), headers={"Content-Length": str(2 << 20)})
    assert r.status == 408 and gw._parse_slots._value == 2


async def test_the_query_reaches_the_engine_as_the_client_encoded_it(catch_all):
    engine, client, cfg = catch_all
    q = "/v1/embeddings?a=%23/../admin&b=%2B1&c=%2F&d=%3F"
    r = await client.get(aiohttp.client.URL(q, encoded=True))
    assert r.status == 200 and engine.paths == [q]


async def test_a_lone_surrogate_eval_token_is_refused_not_a_500(setup):
    import time
    from kiln.engines.lock import EvalLock as Lock
    gw, client, engine, cfg = await setup()
    lock = Lock(cfg.lock_file)
    lock.acquire("bench", time.time() + 600)
    assert lock.allows("\ud800") is False


@pytest.mark.parametrize("field,value", [("max_tokens", 10 ** 20), ("max_tokens", -1), ("max_tokens", 2 ** 31),
                                         ("max_tokens", 1.5), ("max_tokens", "100"), ("max_tokens", True),
                                         ("max_completion_tokens", 10 ** 20),
                                         ("reasoning_effort", "x" * (1 << 20)), ("reasoning_effort", "a\nb")])
async def test_client_values_in_the_record_are_bounded(setup, field, value):
    """A client value copied into the record must be small and plain (release review H1)."""
    gw, client, engine, cfg = await setup()
    r = await client.post("/v1/chat/completions", data=json.dumps(
        {"model": "qwen3.8-flash-next", "messages": [{"role": "user", "content": "hi"}], field: value}))
    assert r.status == 200
    (rec,) = records(cfg)
    key = "max_tokens" if "tokens" in field else field
    assert rec[key] is None
    assert len(json.dumps(rec)) < 20_000


async def test_a_plain_max_tokens_and_effort_are_kept(setup):
    gw, client, engine, cfg = await setup()
    await client.post("/v1/chat/completions", data=json.dumps(
        {"model": "qwen3.8-flash-next", "messages": [], "max_tokens": 4096, "reasoning": {"effort": "high"}}))
    (rec,) = records(cfg)
    assert (rec["max_tokens"], rec["reasoning_effort"]) == (4096, "high")


# -- release review H1: cross-site requests (a page sends text/plain, which needs no CORS preflight) ---------
async def test_a_cross_site_post_is_refused_and_recorded_nowhere(setup):
    gw, client, engine, cfg = await setup()
    r = await client.post(CHAT, data=json.dumps(GOOD), headers={
        "Content-Type": "text/plain", "Origin": "https://evil.example", "Host": "127.0.0.1:8000"})
    assert r.status == 403 and engine.bodies == []
    assert "[gateway] allowed_origins" in await r.text()
    for path in ("/health", "/v1/models"):
        assert (await client.get(path, headers={"Origin": "https://evil.example"})).status == 403
    assert (await client.get("/health", headers={"Origin": "null"})).status == 403


async def test_a_request_without_origin_passes(setup):
    gw, client, engine, cfg = await setup()
    assert (await client.post(CHAT, json=GOOD)).status == 200


@pytest.mark.parametrize("allowed,origin,status", [
    (["https://chat.example.org"], "https://chat.example.org", 200),
    (["https://chat.example.org"], "HTTPS://Chat.Example.org", 200),
    (["https://chat.example.org/"], "https://chat.example.org", 200),
    (["https://chat.example.org"], "https://chat.example.org:8443", 403),
    (["https://chat.example.org"], "http://chat.example.org", 403),
    (["https://chat.example.org"], "https://evil.example", 403),
    (["*"], "https://evil.example", 200)])
async def test_allowed_origins(make_cfg, aiohttp_client, allowed, origin, status):
    cfg = make_cfg(**{"gateway.allowed_origins": allowed})
    gw = Gateway(cfg, whois=Whois(lookup=_none), tailscale=None)
    client = await aiohttp_client(gw.app())
    assert (await client.get("/health", headers={"Origin": origin})).status == status


@pytest.mark.parametrize("bad", ["https://x.example", [5], ["https://x.example/path"], ["x.example"]])
def test_allowed_origins_must_be_a_list_of_origins(make_cfg, bad):
    with pytest.raises(config.ConfigError, match="allowed_origins"):
        make_cfg(**{"gateway.allowed_origins": bad})


# -- release review M1: compressed request bodies ------------------------------------------------------------
@pytest.mark.parametrize("encoding", ["gzip", "deflate", "GZIP", "gzip, identity", "x-custom"])
async def test_a_compressed_body_gets_415_with_a_record_and_is_not_decompressed(setup, encoding):
    import gzip
    gw, client, engine, cfg = await setup()
    bomb = gzip.compress(b'{"model": "m", "messages": [' + b" " * (64 << 20) + b"]}", 9)  # 64 MiB, over the cap
    r = await client.post(CHAT, data=bomb, headers={"Content-Encoding": encoding,
                                                    "Content-Type": "application/json"})
    assert r.status == 415 and engine.bodies == []
    body = await r.json()
    assert body["error"]["type"] == "unsupported_content_encoding"
    assert "send it uncompressed" in body["error"]["message"]
    (rec,) = records(cfg)
    assert (rec["status"], rec["error"], rec["body_bytes"]) == (415, "unsupported_content_encoding", 0)


async def test_identity_content_encoding_passes_and_is_not_relayed(setup):
    gw, client, engine, cfg = await setup()
    r = await client.post(CHAT, data=json.dumps(GOOD), headers={"Content-Encoding": "identity"})
    assert r.status == 200
    assert "Content-Encoding" not in engine.headers[-1]


# -- release review M2: memory of a request while it streams or waits ---------------------------------------
async def test_a_dense_held_body_is_counted_by_its_parse_cost(setup, fast_poll):
    """Held requests keep their parsed bodies. 4 dense bodies under the byte cap kept 731 MiB; count the
    parse cost (objects and commas), not the raw size."""
    from kiln.engines.admission import AdmissionHold
    from kiln.gateway.app import held_cost
    gw, client, engine, cfg = await setup(state=STOPPED, hold_max_s=5)
    gw.cfg = cfg = config.parse({**_raw(cfg), "gateway": {**_raw(cfg)["gateway"], "max_body_mib": 1,
                                                          "hold_max_s": 5}})
    AdmissionHold(cfg.admission_file).write(60, "switch")
    dense = b'{"model":"qwen3.8-flash-next","messages":[' + b",".join([b"0"] * 200_000) + b"]}"
    assert len(dense) < 1 << 20 and held_cost(dense) > 2 * 5 << 20
    r = await client.post(CHAT, data=dense)
    assert r.status == 503 and (await r.json())["error"]["type"] == "too_many_held"
    plain = json.dumps({**GOOD, "messages": [{"role": "user", "content": "x" * 900_000}]}).encode()
    assert held_cost(plain) < 4 * len(plain)
    task = asyncio.create_task(client.post(CHAT, data=plain))
    for _ in range(100):
        if gw.held:
            break
        await asyncio.sleep(0.01)
    assert gw.held == 1
    task.cancel()


async def test_the_parsed_body_is_freed_while_the_response_streams(setup):
    import gc

    class Holding(FakeEngine):
        async def chat(self, request):
            await request.read()  # keeps nothing of the body
            resp = web.StreamResponse(headers={"Content-Type": "text/event-stream"})
            await resp.prepare(request)
            await resp.write(b'data: {"choices":[{"index":0,"delta":{"content":"x"}}]}\n\n')
            self.disconnected.set()
            await asyncio.sleep(0.5)
            await resp.write(b"data: [DONE]\n\n")
            return resp
    engine = Holding()
    gw, client, _, cfg = await setup(engine=engine)
    marker = "kiln-parsed-body-marker"
    task = asyncio.create_task(client.post(CHAT, data=json.dumps({**GOOD, "stream": True, marker: [1, 2, 3]})))
    await asyncio.wait_for(engine.disconnected.wait(), 5)
    gc.collect()
    alive = [o for o in gc.get_objects() if type(o) is dict and marker in o]
    assert alive == []
    assert (await task).status == 200


# -- release review M4: stream_options that is not an object -------------------------------------------------
@pytest.mark.parametrize("opts", ["x", 5, 1.5, ["include_usage"], True, None])
async def test_stream_options_that_is_not_an_object_becomes_include_usage(setup, opts):
    gw, client, engine, cfg = await setup()
    r = await client.post(CHAT, data=json.dumps({**GOOD, "stream": True, "stream_options": opts}))
    assert r.status == 200
    await r.read()
    assert engine.bodies[-1]["stream_options"] == {"include_usage": True}
    (rec,) = records(cfg)
    assert rec["status"] == 200


# -- release review L: event-loop time of a body under PREFIX_THREAD_BYTES ----------------------------------
async def test_a_small_body_of_many_values_is_parsed_in_a_worker_thread(setup, monkeypatch):
    """A 1 MiB body of tiny values or messages kept the event loop for up to 52.7 ms (loop_block.py)."""
    from kiln.gateway import app as gateway_app
    threaded = []
    real = asyncio.to_thread

    async def spy(fn, *args):
        threaded.append(fn.__name__)
        return await real(fn, *args)
    monkeypatch.setattr(gateway_app.asyncio, "to_thread", spy)
    gw, client, engine, cfg = await setup()
    assert (await client.post(CHAT, json=GOOD)).status == 200
    assert threaded == []
    many = {**GOOD, "messages": [{"role": "user", "content": str(i)} for i in range(5000)]}
    raw = json.dumps(many).encode()
    assert len(raw) < gateway_app.PREFIX_THREAD_BYTES
    assert (await client.post(CHAT, data=raw)).status == 200
    assert threaded == ["_parse", "_prepare"]


# -- delta review L2: CORS for the origins that allowed_origins lets in -----------------------------------------
APP = "https://chat.example.org"


@pytest.fixture
async def cors(make_cfg, aiohttp_server, aiohttp_client):
    from tests.test_gateway import FakeEngine

    async def build(allowed=(APP,)):
        engine = FakeEngine()
        eapp = web.Application()
        eapp.router.add_post("/v1/chat/completions", engine.chat)
        eapp.router.add_route("OPTIONS", "/v1/chat/completions", engine.chat)
        eserver = await aiohttp_server(eapp)
        cfg = make_cfg(**{"profiles.halogen-flashnext.port": eserver.port, "gateway.allowed_origins": list(allowed)})
        StateFile(cfg.state_file).set("halogen-flashnext", READY)
        gw = Gateway(cfg, whois=Whois(lookup=_none), tailscale=None)
        return engine, await aiohttp_client(gw.app()), cfg
    return build


def _cors_of(r) -> dict:
    return {k.lower(): v for k, v in r.headers.items() if k.lower().startswith("access-control-") or k.lower() == "vary"}


PREFLIGHT = {"Origin": APP, "Access-Control-Request-Method": "POST",
             "Access-Control-Request-Headers": "content-type, authorization, x-kiln-client, bad header, x(y)"}


async def test_a_preflight_from_an_allowed_origin_is_answered_by_the_gateway(cors):
    engine, client, cfg = await cors()
    r = await client.options(CHAT, headers=PREFLIGHT)
    assert r.status == 204 and engine.bodies == []
    assert _cors_of(r) == {"access-control-allow-origin": APP,
                           "access-control-allow-methods": "GET, POST, OPTIONS",
                           "access-control-allow-headers": "content-type, authorization, x-kiln-client",
                           "access-control-max-age": "600", "vary": "Origin"}
    assert records(cfg) == []


async def test_a_streamed_post_from_an_allowed_origin_has_the_cors_headers(cors):
    engine, client, cfg = await cors()
    r = await client.post(CHAT, json={**GOOD, "stream": True}, headers={"Origin": APP})
    assert r.status == 200 and r.headers["Content-Type"].startswith("text/event-stream")
    await r.read()
    assert r.headers["Access-Control-Allow-Origin"] == APP and "Origin" in r.headers["Vary"]
    assert "Access-Control-Allow-Credentials" not in r.headers


@pytest.mark.parametrize("path", ["/v1/models", "/health"])
async def test_the_local_routes_have_the_cors_headers(cors, path):
    engine, client, cfg = await cors()
    r = await client.get(path, headers={"Origin": APP})
    assert r.status == 200 and r.headers["Access-Control-Allow-Origin"] == APP and r.headers["Vary"] == "Origin"


async def test_an_error_for_an_allowed_origin_has_the_cors_headers(cors):
    engine, client, cfg = await cors()
    StateFile(cfg.state_file).set(None, STOPPED)
    r = await client.post(CHAT, json=GOOD, headers={"Origin": APP})
    assert r.status == 503 and r.headers["Access-Control-Allow-Origin"] == APP


async def test_any_origin_with_a_star(cors):
    engine, client, cfg = await cors(allowed=["*"])
    r = await client.options(CHAT, headers={**PREFLIGHT, "Origin": "tauri://localhost"})
    assert r.status == 204 and r.headers["Access-Control-Allow-Origin"] == "tauri://localhost"


async def test_a_disallowed_preflight_is_403_with_no_cors_headers(cors):
    engine, client, cfg = await cors()
    r = await client.options(CHAT, headers={**PREFLIGHT, "Origin": "https://evil.example"})
    assert r.status == 403 and _cors_of(r) == {} and engine.bodies == []


async def test_a_request_without_origin_gets_no_cors_headers(cors):
    engine, client, cfg = await cors()
    r = await client.get("/v1/models")
    assert _cors_of(r) == {}


@pytest.mark.parametrize("origin", ["tauri://localhost", "chrome-extension://abcdefghijklmnop",
                                    "app+x.y-z://host:1234", "https://chat.example.org"])
def test_allowed_origins_take_any_scheme(make_cfg, origin):
    assert make_cfg(**{"gateway.allowed_origins": [origin]}).gateway_allowed_origins == (origin,)


@pytest.mark.parametrize("bad", [["1http://x.example"], ["tauri://"], ["tauri://localhost/path"], ["tauri:localhost"]])
def test_allowed_origins_still_refuse_what_is_not_an_origin(make_cfg, bad):
    with pytest.raises(config.ConfigError, match="allowed_origins"):
        make_cfg(**{"gateway.allowed_origins": bad})
