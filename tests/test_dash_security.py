"""Attacks from the security review against the dashboard (H1, H3, M1, M4, L3, L5)."""
import json

import aiohttp
import pytest
from multidict import CIMultiDict

from kiln.dash.app import TAILNET_RETRY_S, Dash
from kiln.engines.state import READY, StateFile
from kiln.gateway.attribution import Whois, parse_whois
from tests.test_dash import NOW, FakeManager, FakeSensors, dash_client  # noqa: F401 (fixture)

REBIND = "rebind.attacker.example:7000"
STOP = json.dumps({"force": True})


async def test_a_rebound_host_can_not_stop_the_engine(dash_client):
    mgr = FakeManager()
    _, client = await dash_client(mgr)
    r = await client.post("/api/v1/engines/stop", data=STOP,
                          headers={"Host": REBIND, "Origin": f"http://{REBIND}", "X-Kiln-Action": "1"})
    assert r.status == 403 and not hasattr(mgr, "stopped")
    # Reads too: the page must not see the snapshot.
    assert (await client.get("/api/v1/state", headers={"Host": REBIND})).status == 403


async def test_a_rebound_host_gets_no_websocket(dash_client):
    _, client = await dash_client()
    with pytest.raises(aiohttp.WSServerHandshakeError) as e:
        await client.ws_connect("/api/v1/live", headers={"Host": REBIND, "Origin": f"http://{REBIND}"})
    assert e.value.status == 403


async def test_own_names_and_configured_names_are_accepted(dash_client):
    _, client = await dash_client(overrides={"dash.allowed_hosts": ["kiln.example.org"]})
    for host in ("localhost:7000", "test.local:7000", "[::1]:7000", "10.0.0.5:7000", "kiln.example.org"):
        assert (await client.get("/api/v1/me", headers={"Host": host})).status == 200, host


async def test_the_magicdns_name_is_accepted_after_startup(make_cfg, aiohttp_client):
    async def ts():
        return {"dns_name": "box.tailnet.ts.net", "tailnet": "tailnet.ts.net"}
    cfg = make_cfg()
    StateFile(cfg.state_file).set("halogen-flashnext", READY, now=NOW - 3600)
    d = Dash(cfg, clock=lambda: NOW, sensors=FakeSensors(), whois=Whois(lookup=_none), manager=FakeManager(),
             sources=False, tailscale=ts)
    client = await aiohttp_client(d.app())
    assert (await client.get("/api/v1/me", headers={"Host": "box.tailnet.ts.net:7000"})).status == 200
    assert d.tailnet == "tailnet.ts.net"


async def _none(_ip):
    return None


@pytest.mark.parametrize("header", ["X-Forwarded-For", "Forwarded", "X-Real-IP", "Tailscale-User-Login",
                                    "CF-Connecting-IP"])
async def test_a_proxied_loopback_request_may_not_act(dash_client, header):
    mgr = FakeManager()
    _, client = await dash_client(mgr)
    r = await client.post("/api/v1/engines/stop", data=STOP, headers={"X-Kiln-Action": "1", header: "203.0.113.9"})
    assert r.status == 403 and not hasattr(mgr, "stopped")
    me = await (await client.get("/api/v1/me", headers={header: "203.0.113.9"})).json()
    assert me["can_act"] is False


async def test_allow_loopback_false_takes_the_owner_right_from_loopback(dash_client):
    mgr = FakeManager()
    _, client = await dash_client(mgr, overrides={"actions.allow_loopback": False})
    r = await client.post("/api/v1/engines/stop", data=STOP, headers={"X-Kiln-Action": "1"})
    assert r.status == 403 and not hasattr(mgr, "stopped")


class _Req:
    def __init__(self, remote):
        self.remote, self.headers, self.host = remote, CIMultiDict(), "box.example.ts.net:7000"


def _whois(name, login="owner@example.com"):
    obj = {"Node": {"Name": name, "Hostinfo": {"Hostname": "mac", "OS": "macOS"}},
           "UserProfile": {"LoginName": login}}

    async def lookup(_ip):
        return parse_whois(obj, full=True)
    return Whois(lookup=lookup)


async def _me(make_cfg, name, tailnet="example.ts.net"):
    cfg = make_cfg()
    d = Dash(cfg, clock=lambda: NOW, sensors=FakeSensors(), whois=_whois(name), manager=FakeManager(), sources=False,
             tailscale=None)
    d.tailnet = tailnet
    return await d.me(_Req("100.64.0.9"))


async def test_a_shared_in_node_with_the_owners_first_label_may_not_act(make_cfg):
    me = await _me(make_cfg, "owner-laptop.tailnet-name.ts.net.")
    assert me["can_act"] is False
    me = await _me(make_cfg, "owner-laptop.example.ts.net.")
    assert me == {"can_act": True, "identity": "owner-laptop"}
    assert (await _me(make_cfg, "owner-laptop.example.ts.net.", tailnet=None))["can_act"] is False


async def test_the_live_socket_refuses_a_foreign_origin(dash_client):
    _, client = await dash_client()
    with pytest.raises(aiohttp.WSServerHandshakeError) as e:
        await client.ws_connect("/api/v1/live", headers={"Origin": "https://evil.example"})
    assert e.value.status == 403
    ws = await client.ws_connect("/api/v1/live")  # no Origin (a script) or the own origin: allowed
    assert json.loads((await ws.receive()).data)["type"] == "snapshot"
    await ws.close()


async def test_get_engines_uses_the_kept_blocked_value(dash_client):
    class Counting(FakeManager):
        calls = 0

        async def blocked(self):
            Counting.calls += 1
            return None
    d, client = await dash_client(Counting())
    d.blocked = "kept by the loop"
    for _ in range(3):
        e = await (await client.get("/api/v1/engines")).json()
    assert Counting.calls == 0 and e["blocked"] == "kept by the loop"


async def test_responses_refuse_framing_and_sniffing(dash_client):
    _, client = await dash_client()
    for path in ("/", "/panel", "/api/v1/me", "/js/main.js", "/no-such"):
        r = await client.get(path)
        assert r.headers.get("X-Content-Type-Options") == "nosniff", path
        csp = r.headers.get("Content-Security-Policy", "")
        assert "frame-ancestors 'none'" in csp and "default-src 'self'" in csp, path


async def test_the_own_tailnet_is_looked_up_again_when_tailscaled_was_late(make_cfg):
    answers = [None, {"dns_name": "box.example.ts.net", "tailnet": "example.ts.net"}]
    calls = []

    async def ts():
        calls.append(1)
        return answers[min(len(calls) - 1, 1)]
    d = Dash(make_cfg(), clock=lambda: NOW, sensors=FakeSensors(), whois=_whois("owner-laptop.example.ts.net."),
             manager=FakeManager(), sources=False, tailscale=ts)
    await d.startup()
    assert d.tailnet is None
    await d.me(_Req("100.64.0.9"))
    assert len(calls) == 1  # asked again only after TAILNET_RETRY_S
    d._tailnet_tried -= TAILNET_RETRY_S
    me = await d.me(_Req("100.64.0.9"))
    assert me["can_act"] is True and d.tailnet == "example.ts.net"
    assert d.host_guard.allows("box.example.ts.net:7000")
    await d.me(_Req("100.64.0.9"))
    assert len(calls) == 2  # known now: no more lookups


async def test_a_local_name_may_not_act(dash_client):
    # Any LAN peer can answer mDNS for <host>.local, then rebind it to the box's tailnet or loopback address.
    mgr = FakeManager()
    _, client = await dash_client(mgr)
    port = client.port
    r = await client.post("/api/v1/engines/stop", data=STOP, headers={
        "Host": f"test.local:{port}", "Origin": f"http://test.local:{port}", "X-Kiln-Action": "1"})
    assert r.status == 403 and not hasattr(mgr, "stopped")
    assert (await client.get("/api/v1/state", headers={"Host": f"test.local:{port}"})).status == 200  # reads
    me = await (await client.get("/api/v1/me", headers={"Host": f"test.local:{port}"})).json()
    assert me["can_act"] is False
