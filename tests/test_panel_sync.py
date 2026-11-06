"""The rack panel's shared screen over /api/v1/live (dash/panel_sync.py, live/panel_screen.py)."""
import asyncio
import json

import pytest

from kiln.dash.app import Dash
from kiln.engines.state import FAILED, READY, StateFile
from kiln.gateway.attribution import Whois
from tests.test_dash import NOW, FakeManager, FakeSensors, _no_lookup

T0 = 5000.0  # monotonic


@pytest.fixture
async def sync_client(make_cfg, aiohttp_client):
    async def build(*, mono=None, cfg=None):
        cfg = cfg or make_cfg()
        StateFile(cfg.state_file).set("halogen-flashnext", READY, now=NOW - 3600)
        d = new_dash(cfg, mono or (lambda: T0))
        return d, await aiohttp_client(d.app())
    return build


def new_dash(cfg, mono, clock=lambda: NOW):
    return Dash(cfg, clock=clock, mono=mono, sensors=FakeSensors(), whois=Whois(lookup=_no_lookup),
                manager=FakeManager(), sources=False)


async def connect(client, **kw):
    ws = await client.ws_connect("/api/v1/live", **kw)
    snap = json.loads((await ws.receive(timeout=2)).data)
    assert snap["type"] == "snapshot"
    return ws, snap


async def receive(ws):
    return json.loads((await ws.receive(timeout=2)).data)


async def silent(ws, timeout=0.3):
    with pytest.raises(asyncio.TimeoutError):
        await ws.receive(timeout=timeout)


async def test_a_change_from_one_session_reaches_every_session_once(sync_client):
    d, client = await sync_client()
    a, snap_a = await connect(client)
    b, snap_b = await connect(client)
    assert snap_a["panel"]["screen"] == "glance" and snap_a["panel"]["settings"]["idle_home_s"] == 300
    assert snap_a["me"]["sid"] != snap_b["me"]["sid"]
    await a.send_json({"type": "panel.set", "screen": "engine", "req": 12})
    for ws in (a, b):
        msg = await receive(ws)
        assert msg == {"v": 1, "type": "panel", "screen": "engine", "seq": snap_a["panel"]["seq"] + 1, "at": NOW,
                       "by": {"source": "touch", "who": "localhost", "sid": snap_a["me"]["sid"], "req": 12},
                       "what": "screen", "mute": None, "flags": []}
    await silent(a)
    await silent(b, 0.05)
    # A new session (or a reconnect) starts from the shared screen.
    _, snap_c = await connect(client)
    assert snap_c["panel"]["screen"] == "engine" and snap_c["panel"]["by"]["req"] == 12
    assert (await (await client.get("/api/v1/state")).json())["panel"]["screen"] == "engine"


async def test_a_device_that_may_not_act_is_refused_and_nothing_changes(sync_client):
    d, client = await sync_client()
    b, _ = await connect(client)

    async def viewer(_request):
        return {"can_act": False, "identity": "guest-phone"}

    d.me = viewer
    a, snap = await connect(client)
    assert snap["me"]["can_act"] is False
    await a.send_json({"type": "panel.set", "screen": "engine", "req": 3})
    assert await receive(a) == {"v": 1, "type": "panel.refused", "req": 3,
                                "error": "this device may not change the rack panel"}
    await a.send_json({"type": "panel.touch"})  # a touch is no change: no answer
    await silent(a)
    await silent(b, 0.05)
    assert d.panel_sync.state.screen == "glance" and d.panel_sync.state.active_at is None


async def test_a_page_from_another_origin_may_not_connect(sync_client):
    # It could read every snapshot otherwise (security review M4); before, it could read but not write.
    import aiohttp
    d, client = await sync_client()
    with pytest.raises(aiohttp.WSServerHandshakeError):
        await connect(client, headers={"Origin": "http://evil.example"})
    own, _ = await connect(client, headers={"Origin": f"http://{client.host}:{client.port}"})
    await own.send_json({"type": "panel.set", "screen": "engine", "req": 1})
    assert (await receive(own))["screen"] == "engine"


@pytest.mark.parametrize("raw, req", [
    ("not json", None), ("[]", None), ('{"type": "panel.reboot", "req": 4}', 4), ('{"type": [], "req": 3}', 3),
    ('{"type": "panel.set", "screen": "radar", "req": 5}', 5), ('{"type": "panel.set", "req": 6}', 6),
    ('{"type": "panel.set", "screen": "engine", "req": "7"}', None),
    ('{"type": "panel.set", "screen": "engine", "req": true}', None),
    ('{"type": "panel.settings", "req": 8, "settings": {"idle_home_s": 1}}', 8),
    ('{"type": "panel.settings", "req": 9, "settings": "all"}', 9)])
async def test_a_bad_message_is_refused_to_its_sender_only(sync_client, raw, req):
    d, client = await sync_client()
    a, _ = await connect(client)
    b, _ = await connect(client)
    await a.send_str(raw)
    msg = await receive(a)
    assert msg["type"] == "panel.refused" and msg["req"] == req and msg["error"]
    await silent(b)
    assert d.panel_sync.state.seq == 0


async def test_an_older_tab_that_sends_a_retired_screen_gets_a_refusal_and_its_session_goes_on(sync_client):
    d, client = await sync_client()
    a, _ = await connect(client)
    await a.send_json({"type": "panel.set", "screen": "live", "req": 1})
    msg = await receive(a)
    assert msg["type"] == "panel.refused" and msg["req"] == 1 and "reload" in msg["error"]
    await a.send_json({"type": "panel.set", "screen": "fleet", "req": 2})
    assert (await receive(a))["screen"] == "fleet"


async def test_a_binary_or_oversized_message_changes_nothing(sync_client):
    d, client = await sync_client()
    a, _ = await connect(client)
    await a.send_bytes(b'{"type": "panel.set", "screen": "engine", "req": 1}')
    assert (await receive(a))["type"] == "panel.refused"
    big = json.dumps({"type": "panel.set", "screen": "engine", "req": 2, "pad": "x" * 5000})
    await a.send_str(big)
    msg = await a.receive(timeout=2)
    assert msg.type.name in ("CLOSE", "CLOSED", "CLOSING")  # aiohttp closes with 1009: message too big
    assert d.panel_sync.state.seq == 0


async def test_too_many_messages_are_refused_once_then_dropped(sync_client):
    d, client = await sync_client()  # a fixed monotonic clock: the bucket never refills
    a, _ = await connect(client)
    for _ in range(25):
        await a.send_json({"type": "panel.touch"})
    await a.send_json({"type": "panel.set", "screen": "engine", "req": 1})
    msg = await receive(a)
    assert msg["type"] == "panel.refused" and "too many" in msg["error"]
    await silent(a)
    assert d.panel_sync.state.seq == 0


async def test_a_settings_change_reaches_everyone_with_the_settings_and_is_kept(sync_client, make_cfg):
    cfg = make_cfg()
    d, client = await sync_client(cfg=cfg)
    a, _ = await connect(client)
    await a.send_json({"type": "panel.settings", "req": 2, "settings": {"auto": {"enabled": True}}})
    msg = await receive(a)
    assert msg["type"] == "panel" and msg["settings"]["auto"]["enabled"] is True and msg["screen"] == "glance"
    await asyncio.sleep(0.05)  # the file is written after the broadcast
    assert new_dash(cfg, lambda: T0).panel_sync.state.settings.auto_enabled is True


async def test_the_screen_survives_a_restart_unless_it_is_older_than_the_idle_time(sync_client, make_cfg):
    cfg = make_cfg()
    d, client = await sync_client(cfg=cfg)
    a, _ = await connect(client)
    await a.send_json({"type": "panel.set", "screen": "fleet", "req": 1})
    seq = (await receive(a))["seq"]
    await asyncio.sleep(0.05)
    doc = json.loads(cfg.panel_file.read_text())
    assert doc["screen"] == "fleet" and doc["seq"] == seq
    after_deploy = new_dash(cfg, lambda: 10.0, clock=lambda: NOW + 30).panel_sync.state
    assert (after_deploy.screen, after_deploy.seq) == ("fleet", seq)
    later = new_dash(cfg, lambda: 10.0, clock=lambda: NOW + 301).panel_sync.state
    assert (later.screen, later.seq, later.by.source) == ("glance", seq + 1, "start")


async def test_the_idle_return_is_one_broadcast_for_every_session(sync_client):
    mono = [T0]
    d, client = await sync_client(mono=lambda: mono[0])
    await d.panel_tick()  # the first step learns the inputs
    a, _ = await connect(client)
    b, _ = await connect(client)
    await a.send_json({"type": "panel.set", "screen": "engine", "req": 1})
    assert (await receive(a))["screen"] == "engine" and (await receive(b))["screen"] == "engine"
    mono[0] = T0 + 200
    await a.send_json({"type": "panel.touch"})  # a person is still there
    await silent(a, 0.1)
    mono[0] = T0 + 301
    await d.panel_tick()
    await silent(b, 0.1)
    mono[0] = T0 + 501
    for _ in range(3):
        await d.panel_tick()
    for ws in (a, b):
        msg = await receive(ws)
        assert (msg["screen"], msg["by"]) == ("glance", {"source": "idle", "who": None, "sid": None, "req": None})
        await silent(ws, 0.1)


@pytest.mark.parametrize("cause", ["failed", "hot"])
async def test_an_alarm_brings_home_once(sync_client, make_cfg, cause):
    cfg = make_cfg()
    d, client = await sync_client(cfg=cfg)
    await d.panel_tick()
    a, _ = await connect(client)
    await a.send_json({"type": "panel.set", "screen": "today", "req": 1})
    await receive(a)
    if cause == "failed":
        StateFile(cfg.state_file).set("halogen-flashnext", FAILED, now=NOW)
    else:
        d.hw = {"edge_c": 90.4}
    for _ in range(3):
        await d.panel_tick()
    msg = await receive(a)
    assert (msg["screen"], msg["by"]["source"]) == ("glance", "alarm")
    await silent(a)


async def hot_dash(sync_client, make_cfg):
    cfg = make_cfg()
    d, client = await sync_client(cfg=cfg)
    d.hw = {"edge_c": 91.0}
    await d.panel_tick()  # the first step learns the hot GPU
    return cfg, d, client


async def test_a_mute_reaches_everyone_and_is_kept_in_the_file(sync_client, make_cfg):
    cfg, d, client = await hot_dash(sync_client, make_cfg)
    a, snap = await connect(client)
    b, _ = await connect(client)
    assert snap["panel"]["mute"] is None and snap["panel"]["flags"] == []
    await a.send_json({"type": "panel.mute", "kind": "hot", "req": 4})
    for ws in (a, b):
        msg = await receive(ws)
        assert msg["what"] == "mute" and msg["mute"] == {"kind": "hot", "until": NOW + 1800}
        assert msg["by"]["req"] == 4 and "settings" not in msg
    await asyncio.sleep(0.05)
    assert json.loads(cfg.panel_file.read_text())["mute"] == {"kind": "hot", "until": NOW + 1800}
    await a.send_json({"type": "panel.mute", "kind": None, "req": 5})
    assert (await receive(b))["mute"] is None


@pytest.mark.parametrize("kind, error", [("nodata", "NO DATA"), ("failed", "no such alarm"), (3, "unknown alarm")])
async def test_a_mute_of_no_data_or_of_an_alarm_that_does_not_hold_is_refused(sync_client, make_cfg, kind, error):
    _, d, client = await hot_dash(sync_client, make_cfg)
    a, _ = await connect(client)
    await a.send_json({"type": "panel.mute", "kind": kind, "req": 6})
    msg = await receive(a)
    assert msg["type"] == "panel.refused" and msg["req"] == 6 and error in msg["error"]
    assert d.panel_sync.state.mute is None


async def test_a_device_that_may_not_act_may_not_mute(sync_client, make_cfg):
    _, d, client = await hot_dash(sync_client, make_cfg)

    async def viewer(_request):
        return {"can_act": False, "identity": "guest-phone"}

    d.me = viewer
    a, _ = await connect(client)
    await a.send_json({"type": "panel.mute", "kind": "hot", "req": 7})
    assert (await receive(a))["error"] == "this device may not change the rack panel"
    assert d.panel_sync.state.mute is None


def test_the_tick_inputs_count_the_failed_items_of_the_plan():
    from kiln.dash.panel_sync import tick_inputs
    from kiln.engines.state import ActiveState
    st = ActiveState(None, "stopped", 0.0)
    plan = {"items": [{"label": "First check", "state": "pass"}, {"label": "Short test", "state": "fail"},
                      {"label": "Last check", "state": "fail"}]}
    i = tick_inputs(st, None, {}, {"plan": plan})
    assert (i.plan_failed, i.plan_failed_label) == (2, "Last check")
    assert tick_inputs(st, None, {}, {"plan": None}).plan_failed == 0
    assert tick_inputs(st, None, {}, None).plan_failed is None
    assert tick_inputs(st, None, {}, {"error": "could not read"}).plan_failed is None
    # An odd shape must not stop the tick: the alarm jump runs in the same step.
    for odd in ({"plan": [1]}, {"plan": {"items": "x"}}, {"plan": {"items": [{"state": "fail", "label": 7}]}}):
        i = tick_inputs(st, None, {}, odd)
        assert isinstance(i.plan_failed, int) and i.plan_failed_label is None
