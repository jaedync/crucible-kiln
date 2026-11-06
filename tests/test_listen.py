"""Listen addresses: "*:PORT" binds every interface in both families.

On a client, systemd-resolved answers strix.local with only its IPv6 addresses for up to a second each
time the mDNS cache entry expires (every 120 s). A gateway bound to 0.0.0.0 refused those
connections, and the client showed a connection error.
"""
import json
import socket

import aiohttp
import pytest

from kiln import cli, config
from kiln.gateway import app as gateway_app


def _ipv6_loopback() -> bool:
    if not socket.has_ipv6:
        return False
    try:
        with socket.socket(socket.AF_INET6, socket.SOCK_STREAM) as s:
            s.bind(("::1", 0))
        return True
    except OSError:
        return False


needs_ipv6 = pytest.mark.skipif(not _ipv6_loopback(), reason="no IPv6 loopback on this machine")


def _free_port_both_families() -> int:
    # A dual-stack bind on port 0 picks a port that is free in IPv4 and IPv6 at the same time.
    with socket.socket(socket.AF_INET6, socket.SOCK_STREAM) as s:
        s.setsockopt(socket.IPPROTO_IPV6, socket.IPV6_V6ONLY, 0)
        s.bind(("::", 0))
        return s.getsockname()[1]


def _free_port_v4() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


# -- parsing ------------------------------------------------------------------

@pytest.mark.parametrize("text, expected", [
    ("0.0.0.0:8000", ("0.0.0.0", 8000)),
    ("127.0.0.1:8001", ("127.0.0.1", 8001)),
    ("*:8000", ("*", 8000)),
    ("[::]:8000", ("::", 8000)),
    ("[::1]:7000", ("::1", 7000)),
    ("[2001:db8::1]:8000", ("2001:db8::1", 8000)),
    ("127.0.0.1:0", ("127.0.0.1", 0)),
])
def test_listen_addresses_parse(text, expected):
    assert config._addr(text) == expected


@pytest.mark.parametrize("text", ["8000", ":8000", "*:", "*:http", "[]:8000", "[::1:8000", "*:65536",
                                  "::1:8000", "[::1]8000", "*:0"])
def test_bad_listen_addresses_are_refused(text):
    with pytest.raises(config.ConfigError, match="listen address"):
        config._addr(text)


def test_any_host_reaches_the_config(make_cfg):
    cfg = make_cfg(**{"gateway.listen": "*:8000", "dash.listen": "[::]:7000"})
    assert cfg.gateway_listen == ("*", 8000)
    assert cfg.dash_listen == ("::", 7000)


def test_bind_host_maps_any_to_every_interface():
    assert config.bind_host("*") is None  # asyncio: one socket per address family
    assert config.bind_host("0.0.0.0") == "0.0.0.0"
    assert config.bind_host("::1") == "::1"


@pytest.mark.parametrize("host, local", [
    ("*", "127.0.0.1"), ("0.0.0.0", "127.0.0.1"), ("::", "::1"), ("203.0.113.23", "203.0.113.23"), ("::1", "::1"),
])
def test_local_host_is_reachable_from_this_machine(host, local):
    assert config.local_host(host) == local


def test_url_host_brackets_ipv6():
    assert config.url_host("::1") == "[::1]"
    assert config.url_host("127.0.0.1") == "127.0.0.1"
    assert config.url_host("strix.local") == "strix.local"


# -- the gateway's sockets ----------------------------------------------------

async def _get_health(host: str, port: int) -> int:
    async with aiohttp.ClientSession() as s:
        async with s.get(f"http://{config.url_host(host)}:{port}/health") as r:
            return r.status


async def _started(make_cfg, listen: str):
    cfg = make_cfg(**{"gateway.listen": listen, "gateway.events_listen": f"127.0.0.1:{_free_port_v4()}"})
    return await gateway_app.start(cfg)


@needs_ipv6
async def test_any_host_serves_ipv4_and_ipv6(make_cfg):
    port = _free_port_both_families()
    runners = await _started(make_cfg, f"*:{port}")
    try:
        families = {len(a) for a in runners[0].addresses}  # IPv4 names have 2 fields, IPv6 names 4
        assert families == {2, 4}
        assert await _get_health("127.0.0.1", port) == 200
        assert await _get_health("::1", port) == 200
    finally:
        for r in runners:
            await r.cleanup()


@needs_ipv6
async def test_ipv4_host_still_binds_ipv4_only(make_cfg):
    port = _free_port_both_families()
    runners = await _started(make_cfg, f"127.0.0.1:{port}")
    try:
        assert [a[:2] for a in runners[0].addresses] == [("127.0.0.1", port)]
        assert await _get_health("127.0.0.1", port) == 200
        with pytest.raises(aiohttp.ClientConnectionError):
            await _get_health("::1", port)
    finally:
        for r in runners:
            await r.cleanup()


async def test_a_failed_bind_releases_the_sockets_already_bound(make_cfg):
    gw_port = _free_port_v4()
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as taken:
        taken.bind(("127.0.0.1", 0))
        taken.listen()
        events_port = taken.getsockname()[1]
        cfg = make_cfg(**{"gateway.listen": f"127.0.0.1:{gw_port}",
                          "gateway.events_listen": f"127.0.0.1:{events_port}"})
        with pytest.raises(OSError):
            await gateway_app.start(cfg)
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as again:
        again.bind(("127.0.0.1", gw_port))  # the gateway socket bound first was closed


def test_listening_line_names_every_bound_socket_ipv4_first():
    line = config.describe_addresses([("::", 8000, 0, 0), ("0.0.0.0", 8000)])
    assert line == "0.0.0.0:8000, [::]:8000"


# -- the CLI's own requests to the dashboard ------------------------------------

def test_cli_action_calls_the_dashboard_on_loopback_for_any_host(make_cfg, monkeypatch):
    cfg = make_cfg(**{"dash.listen": "*:17000"})
    seen = []

    class Reply:
        def __enter__(self):
            return self

        def __exit__(self, *exc):
            return False

        def read(self):
            return json.dumps({"accepted": True}).encode()

    def fake_urlopen(req, timeout):
        seen.append(req.full_url)
        return Reply()

    monkeypatch.setattr(cli.urllib.request, "urlopen", fake_urlopen)
    assert cli._action(cfg, "stop", {"reason": "test"}) == 0
    assert seen == ["http://127.0.0.1:17000/api/v1/engines/stop"]


def test_cli_action_brackets_an_ipv6_dashboard(make_cfg, monkeypatch):
    cfg = make_cfg(**{"dash.listen": "[::]:17000"})
    seen = []

    def fake_urlopen(req, timeout):
        seen.append(req.full_url)
        raise OSError("no dashboard in tests")

    monkeypatch.setattr(cli.urllib.request, "urlopen", fake_urlopen)
    assert cli._action(cfg, "stop", {"reason": "test"}) == 1  # one clean line, no traceback
    assert seen == ["http://[::1]:17000/api/v1/engines/stop"]


@pytest.mark.parametrize("listen, target", [("*:17000", ("127.0.0.1", 17000)), ("[::]:17000", ("::1", 17000))])
def test_cli_probes_the_dashboard_on_loopback(make_cfg, monkeypatch, listen, target):
    import shutil
    cfg = make_cfg(**{"dash.listen": listen})
    seen = []

    def fake_connect(addr, timeout):
        seen.append(addr)
        raise OSError("no dashboard in tests")

    monkeypatch.setattr(shutil, "which", lambda _name: None)  # skip systemctl, probe the port
    monkeypatch.setattr(socket, "create_connection", fake_connect)
    assert cli._dash_answers(cfg) is False
    assert seen == [target]
