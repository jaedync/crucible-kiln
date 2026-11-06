import asyncio
import json

import pytest

from kiln.dash.app import Dash
from kiln.engines.manager import SwitchRefused
from kiln.engines.state import READY, StateFile
from kiln.gateway.attribution import Whois
from kiln.store import queries
from kiln.store.db import connect
from tests.test_ingest import HALOGEN, H_LINE, gw_record

NOW = 1_790_000_000.0


class FakeSensors:
    def sample(self):
        return {"tctl_c": 61.5, "gtt_used_bytes": 29e9, "profile": "balanced"}


class FakeManager:
    def __init__(self, refuse=None, confirm=False, fleet=None):
        self.refuse, self.confirm, self.switched = refuse, confirm, []
        self.probes = []
        self.fleet = fleet

    async def reconcile(self):
        return None

    async def blocked(self):
        return self.refuse

    async def request_switch(self, target, token=None, *, confirm=False, remember=False):
        if self.refuse and not (self.confirm and confirm):
            raise SwitchRefused(self.refuse, confirm=self.confirm)
        self.switched.append((target, confirm, remember) if confirm or remember else target)

    def is_probe(self, container, ts):
        return False

    async def stop(self, reason, token=None, force=False):
        self.stopped = reason

    async def watch(self):
        self.watched = getattr(self, "watched", 0) + 1


async def _no_lookup(ip):
    return None


@pytest.fixture
async def dash_client(make_cfg, aiohttp_client):
    async def build(manager=None, db=None, overrides=None):
        cfg = make_cfg(**({"dash.db": str(db)} if db else {}), **(overrides or {}))
        StateFile(cfg.state_file).set("halogen-flashnext", READY, now=NOW - 3600)
        d = Dash(cfg, clock=lambda: NOW, sensors=FakeSensors(), whois=Whois(lookup=_no_lookup),
                 manager=manager or FakeManager(), sources=False)
        d.ingest.watermarks = {"journal": 1e12, "gateway": 1e12}
        return d, await aiohttp_client(d.app())
    return build


async def seed(d):
    await d._on_records([gw_record("g1", t0=NOW - 100), gw_record("g2", t0=NOW - 50, completion=40, events=40)],
                        {"requests-x.jsonl": 10})
    await d._on_journal([(HALOGEN, H_LINE, NOW - 96.5, "c1")], "c1")


async def test_snapshot_has_every_section(dash_client):
    d, client = await dash_client()
    await seed(d)
    snap = await (await client.get("/api/v1/state")).json()
    assert snap["type"] == "snapshot" and snap["v"] == 1 and snap["build"]
    assert snap["engine"]["profile"] == "halogen-flashnext" and snap["engine"]["state"] == "ready"
    assert snap["engine"]["kv"]["pool_used"] == 9000 and snap["engine"]["kv"]["capacity_positions"] == 524288
    assert snap["live"] == {"decode_tps": 0, "decode_exact": True, "decode_note": None, "engine_exact": True,
                            "in_flight": 0, "streams": [], "slots": 4}
    assert snap["last"]["completion"] == 40 and snap["today"]["requests"] == 2
    assert snap["me"] == {"can_act": True, "identity": "localhost"}
    assert len(snap["series_1s"]["t"]) == 900
    assert snap["health"]["parse_errors_24h"] == 0 and snap["health"]["parse_error_last_ts"] is None
    assert snap["pricing"] == {"reference": "Claude Sonnet 5.5", "input_per_m": 2.0, "cached_per_m": 0.2,
                               "output_per_m": 10.0}
    assert [p["name"] for p in snap["profiles"] if p["active"]] == ["halogen-flashnext"]


async def test_usage_requests_series_activity_prices(dash_client):
    d, client = await dash_client()
    await seed(d)
    u = await (await client.get("/api/v1/usage?period=today&group=device")).json()
    (row,) = u["rows"]
    assert row["name"] == "Owner's laptop" and row["requests"] == 2 and row["completion"] == 140
    assert row["computed"] == 2000 and row["cost"]["reference"] > 0 and row["color"].startswith("#")
    assert u["totals"]["busy_s"] == pytest.approx(6.2)
    rows = (await (await client.get("/api/v1/requests?limit=10")).json())["rows"]
    assert [r["id"] for r in rows] == ["g2", "g1"]
    assert rows[1]["decode_tps"] == 50.0 and rows[1]["prefill_tps"] == 2000.0 and rows[1]["think_close"] == "answer_room"
    s = await (await client.get("/api/v1/series?range=1h&group=device")).json()
    assert s["step"] == 10 and sum(s["series"]["198.51.100.10"]["decode"]) == 140
    a = await (await client.get("/api/v1/activity?days=2")).json()
    assert len(a["days"]) == 2 and sum(h["busy_s"] for day in a["days"] for h in day["hours"]) > 0
    p = await (await client.get("/api/v1/prices")).json()
    assert p["current"][0]["source"] == "reference" and p["current"][0]["input_per_m"] == 2.0
    assert (await client.get("/api/v1/usage?period=forever")).status == 400


async def test_usage_rows_outside_the_device_group_are_named_by_their_own_key(dash_client):
    d, client = await dash_client()
    await seed(d)
    d.conn.execute("INSERT INTO requests (id, source, host, ts_start, ts_end, client_key, client_name, container, model)"
                   " VALUES ('x', 'direct', 'test', ?, ?, '127.0.0.1', 'test', 'bench-1', 'm2')", (NOW - 30, NOW - 20))
    names = {}
    for group in ("model", "profile", "host"):
        u = await (await client.get(f"/api/v1/usage?period=today&group={group}")).json()
        names[group] = {x["key"]: x["name"] for x in u["rows"]}
    assert names == {"model": {"qwen3.8-flash-next": "qwen3.8-flash-next", "m2": "m2"},
                     "profile": {"halogen-flashnext": "halogen-flashnext", "container:bench-1": "bench-1 (container)"},
                     "host": {"test": "test"}}


async def test_switch_requires_the_action_header_and_same_origin(dash_client):
    mgr = FakeManager()
    d, client = await dash_client(mgr)
    body = json.dumps({"profile": "gufo-qwen27"})
    assert (await client.post("/api/v1/engines/switch", data=body)).status == 403
    r = await client.post("/api/v1/engines/switch", data=body,
                          headers={"X-Kiln-Action": "1", "Origin": "http://evil.example"})
    assert r.status == 403
    r = await client.post("/api/v1/engines/switch", data=body, headers={"X-Kiln-Action": "1"})
    assert r.status == 202 and mgr.switched == ["gufo-qwen27"]


async def test_switch_refusal_is_409(dash_client):
    d, client = await dash_client(FakeManager(refuse="evaluation running (bench-hard)"))
    r = await client.post("/api/v1/engines/switch", data=json.dumps({"profile": "gufo-qwen27"}),
                          headers={"X-Kiln-Action": "1"})
    assert r.status == 409 and "evaluation" in (await r.json())["error"]
    d.blocked = await d.manager.blocked()  # what the 5 s blocked loop keeps
    e = await (await client.get("/api/v1/engines")).json()
    assert e["blocked"] == "evaluation running (bench-hard)"


async def test_websocket_sends_snapshot_then_requests_and_never_patches_drawn_seconds(dash_client):
    d, client = await dash_client()
    ws = await client.ws_connect("/api/v1/live")
    snap = json.loads((await ws.receive()).data)
    assert snap["type"] == "snapshot" and "starts" in snap["series_1s"]
    before = dict(zip(snap["series_1s"]["t"], snap["series_1s"]["decode"]))
    await d._on_records([gw_record("g9", t0=NOW - 30)], {"requests-x.jsonl": 5})
    msg = json.loads((await ws.receive(timeout=2)).data)
    assert msg["type"] == "request"
    # A request the live chart never saw goes to the history (series_1s); the drawn seconds stay as drawn.
    with pytest.raises(asyncio.TimeoutError):
        await ws.receive(timeout=0.3)
    after = d.live.history()
    assert dict(zip(after["t"], after["decode"])) == before
    await ws.close()


async def test_a_browser_that_joins_during_a_slow_identity_lookup_misses_no_second(dash_client):
    d, client = await dash_client()

    async def slow_me(_request):
        # Meanwhile the tick loop sends more seconds to the browsers already connected.
        await d.broadcast(d.tick(NOW, d.live.completed_seconds(NOW + 5)))
        return {"can_act": False, "identity": None}

    d.me = slow_me
    ws = await client.ws_connect("/api/v1/live")
    snap = json.loads((await ws.receive()).data)
    assert snap["series_1s"]["t"][-1] == d.live._emitted_through  # what the others drew, up to now
    await ws.close()


async def test_static_index_is_served(dash_client):
    _, client = await dash_client()
    r = await client.get("/")
    assert r.status == 200 and "<html" in (await r.text()).lower()


async def test_unknown_path_is_a_plain_text_404(dash_client):
    # An empty octet-stream 404 made Chrome show ERR_INVALID_RESPONSE (a /health typed on the dashboard port).
    _, client = await dash_client()
    for path in ("/health", "/js/no-such-file.js"):
        r = await client.get(path)
        assert r.status == 404 and r.content_type == "text/plain"
        assert "not found" in (await r.text()).lower()


async def test_stop_endpoint(dash_client):
    mgr = FakeManager()
    _, client = await dash_client(mgr)
    r = await client.post("/api/v1/engines/stop", data=json.dumps({"reason": "eval"}), headers={"X-Kiln-Action": "1"})
    assert r.status == 200 and mgr.stopped == "eval"
    assert (await client.post("/api/v1/engines/stop", data="{}")).status == 403


def test_activity_steps_through_the_dst_change(make_cfg):
    import os
    import time as _time
    from kiln.store import queries
    from kiln.store.db import connect
    old = os.environ.get("TZ")
    os.environ["TZ"] = "America/New_York"
    _time.tzset()
    try:
        conn = connect(":memory:")
        # 2026-11-01 04:30 to 08:30 UTC (00:30 EDT to 03:30 EST) spans the repeated 01:00 local hour.
        start = _time.mktime((2026, 11, 1, 0, 30, 0, 0, 0, 1))
        conn.execute("INSERT INTO requests (id, source, host, ts_start, ts_end, client_key) VALUES"
                     " ('x', 'gateway', 'h', ?, ?, 'c')", (start, start + 4 * 3600))
        a = queries.activity(conn, 2, start + 5 * 3600)
        day = next(d for d in a["days"] if d["date"] == "2026-11-01")
        assert sum(h["busy_s"] for h in day["hours"]) == pytest.approx(4 * 3600)
        assert [d["date"] for d in a["days"]] == ["2026-10-31", "2026-11-01"]
    finally:
        if old is None:
            os.environ.pop("TZ")
        else:
            os.environ["TZ"] = old
        _time.tzset()


def test_config_rejects_unit_injection(make_cfg):
    from kiln.config import ConfigError
    with pytest.raises(ConfigError, match="single-line"):
        make_cfg(**{"profiles.gufo-qwen27.container": {"Image": "x\nExecStartPre=/bin/evil"}})
    with pytest.raises(ConfigError, match="profile name"):
        make_cfg(**{"profiles.Bad_Name": {"engine": "gufo", "model": "m", "served_id": "m", "port": 1,
                                         "context": 1, "kv": {"positions": 1}, "container": {"Image": "i"}}})


class FakeFleet:
    def view(self, now):
        return {"day": "09:00-18:00", "tz": "UTC", "in_day": False, "next_change": now + 3600,
                "day_profile": "gufo-qwen27", "holds": ["schedule"],
                "dev_jobs": [{"pid": 202, "unit": None, "cmd": "gpu-job bench"}], "drain_timeout_s": 900}


async def test_switch_confirmation_and_morning_profile(dash_client):
    mgr = FakeManager(refuse="another GPU job is running (pid 202 bench)", confirm=True,
                      fleet=FakeFleet())
    d, client = await dash_client(mgr)
    hdr = {"X-Kiln-Action": "1"}
    r = await client.post("/api/v1/engines/switch", data=json.dumps({"profile": "gufo-qwen27"}), headers=hdr)
    body = await r.json()
    assert r.status == 409 and body["confirm"] is True and "GPU job" in body["error"]
    r = await client.post("/api/v1/engines/switch", headers=hdr,
                          data=json.dumps({"profile": "gufo-qwen27", "confirm": True, "remember": True}))
    assert r.status == 202 and mgr.switched == [("gufo-qwen27", True, True)]
    # An evaluation's switch (it carries the lock token) never becomes the user's morning profile.
    r = await client.post("/api/v1/engines/switch", headers=hdr,
                          data=json.dumps({"profile": "gufo-qwen27", "confirm": True, "remember": True,
                                           "eval_token": "t"}))
    assert mgr.switched[-1] == ("gufo-qwen27", True, False)
    snap = await (await client.get("/api/v1/state")).json()
    sched = snap["engine"]["schedule"]
    assert sched["in_day"] is False and sched["day_profile"] == "gufo-qwen27" and sched["dev_jobs"][0]["pid"] == 202


async def test_disabled_profiles_are_listed_with_their_reason(dash_client, make_cfg):
    d, client = await dash_client()
    import dataclasses
    p = d.cfg.profiles["gufo-qwen27"]
    d.cfg.profiles["gufo-qwen27"] = dataclasses.replace(p, disabled="checkpoint not delivered yet")
    snap = await (await client.get("/api/v1/state")).json()
    by = {x["name"]: x for x in snap["profiles"]}
    assert by["gufo-qwen27"]["disabled"] == "checkpoint not delivered yet" and by["halogen-flashnext"]["disabled"] is None
    assert snap["engine"]["schedule"] is None  # no [schedule] in this config


async def test_engine_history_lists_switch_steps_only(dash_client):
    d, client = await dash_client()
    for i, kind in enumerate(("switch_requested", "join_miss", "parse_error", "cache:kv_evict", "ready")):
        d.conn.execute("INSERT INTO engine_events (ts, host, profile, kind, detail) VALUES (?, 'test', 'p', ?, '')",
                       (NOW - 10 + i, kind))
    e = await (await client.get("/api/v1/engines")).json()
    assert [x["kind"] for x in e["events"]] == ["ready", "switch_requested"]


async def test_health_reports_when_the_newest_parse_error_happened(dash_client):
    d, client = await dash_client()
    for ts in (NOW - 90000, NOW - 600, NOW - 300):
        d.conn.execute("INSERT INTO engine_events (ts, host, profile, kind, detail) VALUES (?, 'test', 'p',"
                       " 'parse_error', '')", (ts,))
    snap = await (await client.get("/api/v1/state")).json()
    assert snap["health"]["parse_errors_24h"] == 2 and snap["health"]["parse_error_last_ts"] == NOW - 300


async def test_crucible_profiles_start_with_the_servers_prefill_chunk(make_cfg):
    # Until a long prefill shows the chunk, a cold prompt's count before its first report needs it.
    from tests.test_compat import CRUCIBLE
    cfg = make_cfg(**{"profiles.crucible-flashnext": CRUCIBLE})
    d = Dash(cfg, clock=lambda: NOW, sensors=FakeSensors(), whois=Whois(lookup=_no_lookup), manager=FakeManager(),
             sources=False)
    assert d.live.prefill_chunks["crucible-flashnext"] == 4096
    assert all(d.live.prefill_chunks[n] == 32768 for n, p in cfg.profiles.items() if p.engine == "halogen")


async def test_a_decode_line_puts_the_running_turns_drafts_on_its_stream_in_the_tick(make_cfg):
    cfg = make_cfg()
    StateFile(cfg.state_file).set("halogen-flashnext", READY, now=NOW - 3600)
    d = Dash(cfg, clock=lambda: NOW, sensors=FakeSensors(), whois=Whois(lookup=_no_lookup), manager=FakeManager(),
             sources=False)
    d.live.on_event({"type": "req.start", "id": "a", "profile": "halogen-flashnext", "ts": NOW - 9, "client": {}})
    await d._apply([("progress", "halogen-flashnext", 4096, 9000, 1.0, NOW - 8, None, 10)])  # engine 10 is a
    d.live.on_event({"type": "req.first_token", "id": "a", "ts": NOW - 6})
    await d._apply([("decode_drafts", "halogen-flashnext", 10, 480, 360, NOW - 1, 500, 5.0)])
    (s,) = d.tick(now=NOW)["live"]["streams"]
    assert s["draft_turn"] == {"accepted": 360, "proposed": 480}


async def test_a_continuation_note_reaches_the_pairing(make_cfg):
    cfg = make_cfg()
    d = Dash(cfg, clock=lambda: NOW, sensors=FakeSensors(), whois=Whois(lookup=_no_lookup), manager=FakeManager(),
             sources=False)
    await d._apply([("continuation", "halogen-flashnext", 52)])
    assert [n for n, _ in d.live.progress_pairs.extra["halogen-flashnext"]] == [52]


async def test_a_prefill_report_ticks_at_once_and_a_repeated_one_does_not(make_cfg):
    import asyncio
    cfg = make_cfg()
    StateFile(cfg.state_file).set("halogen-flashnext", READY, now=NOW - 3600)
    d = Dash(cfg, clock=lambda: NOW, sensors=FakeSensors(), whois=Whois(lookup=_no_lookup), manager=FakeManager(),
             sources=False)
    d.live.on_event({"type": "req.start", "id": "a", "profile": "halogen-flashnext", "ts": NOW - 3, "client": {}})
    note = ("progress", "halogen-flashnext", 32768, 56994, 17.6, NOW - 0.2, 32768, None)
    sent = []

    async def broadcast(msg):
        sent.append((asyncio.get_running_loop().time(), msg["type"]))
    d.broadcast = broadcast
    loop = asyncio.create_task(d._tick_loop())
    await asyncio.sleep(0.05)
    t0 = asyncio.get_running_loop().time()
    await d._apply([note])
    await asyncio.sleep(0.3)
    ticks = [t for t, kind in sent if kind == "tick" and t > t0]
    assert ticks and ticks[0] - t0 < 0.2  # not the regular 0.5 s
    await d._apply([note])  # the same report again: nothing new to send
    assert not d._tick_wake.is_set()
    loop.cancel()


# -- no database work on the event loop (inference host stalls) --------------------------------
class NoConn:
    """Stands in for the dashboard's own connection: any use on the event loop fails the test."""
    def __getattr__(self, name):
        raise AssertionError(f"database used on the event loop: conn.{name}")


@pytest.mark.parametrize("tokens_known", [True, False])
def test_last_request_reads_the_newest_rows_not_the_whole_host(make_cfg, tokens_known):
    conn = connect(make_cfg().db_path)
    sqls = []
    conn.set_trace_callback(sqls.append)
    queries.last_request(conn, "remote", tokens_known=tokens_known)
    conn.set_trace_callback(None)
    plan = " | ".join(r[3] for r in conn.execute("EXPLAIN QUERY PLAN " + sqls[-1]))
    # Sorting all of a host's rows read ~7,000 pages for one row; on a busy disk that stalled the loop.
    assert "requests_host_end" in plan and "TEMP B-TREE" not in plan, plan


async def test_the_tick_reads_no_database_once_today_is_loaded(dash_client):
    d, _ = await dash_client()
    await seed(d)
    await d.refresh_today()
    real, d.conn = d.conn, NoConn()
    try:
        t = d.tick(NOW)
    finally:
        d.conn = real
    assert t["today"]["requests"] == 2


async def test_a_live_stream_shows_its_conversations_draft_acceptance(dash_client):
    d, _ = await dash_client()
    rec = {**gw_record("g1", t0=NOW - 100), "prefix": {"conv": "c0ffee01", "n_messages": 3}}
    await d._on_records([rec], {"requests-x.jsonl": 10})
    d.conn.execute("UPDATE requests SET draft_proposed = 200, draft_accepted = 150 WHERE id = 'g1'")
    await d._apply([("request", "g1")])  # the engine line joined: the counts are known
    for rid, conv in (("a", "c0ffee01"), ("b", "beef01")):
        d.live.on_event({"type": "req.start", "id": rid, "profile": "halogen-flashnext", "ts": NOW - 3,
                         "client": {}, "conv": conv})
    streams = {s["id"]: s for s in d.tick(NOW)["live"]["streams"]}
    assert streams["a"]["draft"] == {"accepted": 150, "proposed": 200, "turns": 1}
    assert streams["b"]["draft"] is None  # a new conversation: no finished turn yet
    # A restarted dashboard reads the recent turns back at once.
    d2 = Dash(d.cfg, clock=lambda: NOW, sensors=FakeSensors(), whois=Whois(lookup=_no_lookup),
              manager=FakeManager(), sources=False)
    d2.live.on_event({"type": "req.start", "id": "a", "profile": "halogen-flashnext", "ts": NOW - 3,
                      "client": {}, "conv": "c0ffee01"})
    assert d2.tick(NOW)["live"]["streams"][0]["draft"] == {"accepted": 150, "proposed": 200, "turns": 1}


async def test_the_last_request_carries_its_draft_counts(dash_client):
    d, _ = await dash_client()
    await seed(d)
    d.conn.execute("UPDATE requests SET draft_proposed = 480, draft_accepted = 360 WHERE id = 'g2'")
    last = queries.last_request(d.conn, d.cfg.host)
    assert (last["id"], last["draft_accepted"], last["draft_proposed"]) == ("g2", 360, 480)


async def test_a_new_request_marks_today_for_a_refresh_off_the_loop(dash_client):
    d, _ = await dash_client()
    await d.refresh_today()
    assert d.tick(NOW)["today"]["requests"] == 0
    d._today_dirty.clear()
    await seed(d)
    assert d._today_dirty.is_set()
    assert d.tick(NOW)["today"]["requests"] == 0  # the tick keeps the last refresh until the next one
    await d.refresh_today()
    assert d.tick(NOW)["today"]["requests"] == 2


@pytest.mark.parametrize("path", ["/api/v1/usage?period=today", "/api/v1/usage?period=all&group=model",
                                  "/api/v1/series?range=1h", "/api/v1/requests", "/api/v1/activity",
                                  "/api/v1/cache", "/api/v1/signals", "/api/v1/engines"])
async def test_history_views_query_on_a_worker_thread_not_the_event_loop(dash_client, path):
    d, client = await dash_client()
    await seed(d)
    real, d.conn = d.conn, NoConn()
    try:
        r = await client.get(path)
        body = await r.text()
    finally:
        d.conn = real
    assert r.status == 200, body



def _old_history(conn, host, n):
    # Settled rows of the kinds that piled up on the inference host: joined gateway rows on the serving engine, with and
    # without a completion count, and utility rows (their series_done stays 0). The joined ones carry the
    # engine's prefill chunks, as Crucible rows do since migration 012: the join reads the other requests
    # of the engine to place its own chunks, so that read must not widen with the container's history.
    t0 = NOW - 30 * 86400
    rows = [(f"old-{kind}{i}", source, host, container, t0 + i, t0 + i + 1, completion, joined, done,
             json.dumps([[t0 + i + 1.5, 1.0, 4096]]) if container else None)
            for i in range(n)
            for kind, source, container, completion, joined, done in (
                ("g", "gateway", HALOGEN, None, t0 + i + 2, 1), ("c", "gateway", HALOGEN, 100, t0 + i + 2, 1),
                ("u", "utility", None, None, None, 0))]
    conn.executemany("INSERT INTO requests (id, source, host, container, ts_start, ts_end, completion,"
                     " engine_joined_at, series_done, prefill_progress)"
                     " VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)", rows)
    conn.commit()


async def _loop_work(d):
    """SQLite VM steps the event loop's own connection spends on two requests, a settle and a tick."""
    await d.refresh_today()  # off the loop in production
    steps = 0

    def count():
        nonlocal steps
        steps += 1
        return 0
    d.conn.set_progress_handler(count, 100)
    await seed(d)
    d._tx(d.ingest.settle)
    d.tick(NOW)
    d.conn.set_progress_handler(None, 0)
    return steps * 100


async def test_the_event_loops_own_queries_do_not_grow_with_history(dash_client, tmp_path):
    # Ingest, settle and the tick run on the event loop. On the inference host the fleet's GPU jobs push the database's
    # pages out of memory, so a query that walks old rows there stalls every tick (NO DATA on the panel).
    # Before migration 010 settle walked every utility row and scanned requests each second, and the
    # gateway join read every row of the engine without a completion count.
    fresh, _ = await dash_client()
    aged, _ = await dash_client(db=tmp_path / "aged.db")
    _old_history(aged.conn, aged.cfg.host, 3000)
    base, grown = await _loop_work(fresh), await _loop_work(aged)
    assert grown < base * 1.2 + 2000, (base, grown)


async def test_the_engine_view_carries_the_step_of_a_switch(dash_client):
    d, client = await dash_client()
    StateFile(d.cfg.state_file).set("gufo-qwen27", "starting", None, "starting kiln-gufo-qwen27", now=NOW - 20,
                                    phase="load")
    snap = await (await client.get("/api/v1/state")).json()
    eng = snap["engine"]
    assert (eng["phase"], eng["phase_since"], eng["detail"]) == ("load", NOW - 20, "starting kiln-gufo-qwen27")


def test_the_today_refresh_wakes_just_after_midnight():
    # A refresh up to TODAY_MAX_S late kept yesterday's day start, so a stream from before midnight was
    # priced into the new day for up to 30 s (review).
    from kiln.dash.app import TODAY_MAX_S, today_wait
    midnight = queries.local_midnight(1768485600.0) + 86400  # a local midnight
    assert today_wait(midnight - 10) == pytest.approx(10.1, abs=0.01)
    assert today_wait(midnight - 100) == TODAY_MAX_S
    assert 0 < today_wait(midnight) <= TODAY_MAX_S


async def test_without_reference_prices_the_dashboard_works_and_has_no_api_cost(dash_client):
    d, client = await dash_client(overrides={"pricing.reference": {}})
    await seed(d)
    snap = await (await client.get("/api/v1/state")).json()
    assert snap["pricing"] is None  # the UI hides the API-cost figure (web/static/js/pricing.js hasReference)
    assert snap["today"]["requests"] == 2
    rows = (await (await client.get("/api/v1/requests?limit=10")).json())["rows"]
    assert [r["cost"]["reference"] for r in rows] == [None, None]
    p = await (await client.get("/api/v1/prices")).json()
    assert all(c["source"] != "reference" for c in p["current"])


async def test_the_blocked_round_also_watches_the_ready_engine(dash_client):
    m = FakeManager()
    d, _ = await dash_client(manager=m)
    await d._blocked_round()
    assert m.watched == 1
