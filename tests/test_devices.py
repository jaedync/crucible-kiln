"""[devices]: a machine that reaches Kiln from several addresses (LAN, Wi-Fi, link-local IPv6, tailnet) is one
device. Its requests are stored under its first address and show its configured name; rows already stored under
its other addresses move to that key when kiln-dash starts."""
import pytest

from kiln import config
from kiln.collect.ingest import Ingestor
from kiln.pricing.cards import PriceBook
from kiln.store import devices
from kiln.store.db import connect

from .test_dash import NOW, dash_client  # noqa: F401 (fixture)
from .test_ingest import gw_record

LAPTOP = {"name": "Dev box", "addresses": ["192.0.2.84", "FE80::A:1%enp0s31f6", "198.51.100.109"]}


def test_config_normalizes_addresses_and_names_the_device(make_cfg):
    cfg = make_cfg(**{"devices.laptop": LAPTOP})
    (dev,) = cfg.devices
    assert (dev.name, dev.key) == ("Dev box", "192.0.2.84")
    assert dev.addresses == ("192.0.2.84", "fe80::a:1", "198.51.100.109")
    assert cfg.device_for("fe80::a:1") is dev and cfg.device_for("203.0.113.9") is None
    assert make_cfg(**{"devices.laptop": {"addresses": ["203.0.113.11"]}}).devices[0].name == "laptop"


@pytest.mark.parametrize("bad", [
    {"devices.a": {"addresses": []}},
    {"devices.a": {"addresses": ["not-an-ip"]}},
    {"devices.a": {"addresses": ["10.0.0.0/8"]}},
    {"devices.a": {"addresses": ["203.0.113.11"], "name": "two\nlines"}},
    {"devices.a": {"addresses": ["203.0.113.11"]}, "devices.b": {"addresses": ["::ffff:203.0.113.11"]}},
    # The same address twice in one device: merge() would move the key's rows onto themselves and lose them.
    {"devices.a": {"addresses": ["203.0.113.11", "::ffff:203.0.113.11"]}},
    # Loopback is the Kiln host itself (direct engine traffic is stored under 127.0.0.1).
    {"devices.a": {"addresses": ["203.0.113.11", "127.0.0.1"]}},
    {"devices.a": {"addresses": ["::1"]}},
])
def test_config_refuses_bad_devices(make_cfg, bad):
    with pytest.raises(config.ConfigError):
        make_cfg(**bad)


@pytest.fixture
def env(make_cfg):
    cfg = make_cfg(**{"devices.laptop": LAPTOP})
    conn = connect(":memory:")
    book = PriceBook(conn)
    book.add("reference", "*", "anthropic/claude-sonnet-5.5", 2.0, 0.2, 10.0, now=0)
    ing = Ingestor(conn, cfg, book, clock=lambda: 1000.0)
    ing.watermarks = {"journal": 1e12, "gateway": 1e12}
    return cfg, ing, conn


def _from(rid, ip, t0, name=None):
    rec = gw_record(rid, t0=t0)
    return {**rec, "client": {"key": ip, "peer_ip": ip, "name": name}}


def test_ingest_stores_every_address_of_a_device_under_its_key(env):
    _, ing, conn = env
    ing.gateway_record(_from("a", "fe80::a:1", 100.0))
    ing.gateway_record(_from("b", "198.51.100.109", 200.0, name="laptop"))
    got = [tuple(r) for r in conn.execute("SELECT client_key, peer_ip, client_name FROM requests ORDER BY id")]
    assert got == [("192.0.2.84", "fe80::a:1", "Dev box"),
                   ("192.0.2.84", "198.51.100.109", "Dev box")]
    assert [tuple(r) for r in conn.execute("SELECT key, name FROM clients")] == [("192.0.2.84", "Dev box")]


def _stored(conn, key, rid, t, decode, color):
    conn.execute("INSERT INTO requests (id, source, host, ts_start, client_key, peer_ip) VALUES (?, 'gateway', 'h', ?, ?, ?)",
                 (rid, t, key, key))
    conn.execute("INSERT INTO series_1s (t, host, profile, model, client_key, decode, prefill) VALUES (?, 'h', 'p', 'm', ?, ?, 0)",
                 (t, key, decode))
    conn.execute("INSERT OR IGNORE INTO clients (key, name, color, updated) VALUES (?, NULL, ?, ?)", (key, color, t))


def test_merge_moves_rows_from_other_addresses_and_sums_their_seconds(env):
    cfg, _, conn = env
    _stored(conn, "fe80::a:1", "r1", 100, 5, "#111111")
    _stored(conn, "198.51.100.109", "r2", 100, 7, "#222222")  # same second, same profile: the counts add
    _stored(conn, "198.51.100.109", "r3", 101, 3, "#222222")
    _stored(conn, "203.0.113.9", "r4", 100, 1, "#333333")  # another device stays as it was
    assert devices.merge(conn, cfg.devices) == 2
    keys = dict(conn.execute("SELECT id, client_key FROM requests").fetchall())
    assert keys == {"r1": "192.0.2.84", "r2": "192.0.2.84", "r3": "192.0.2.84", "r4": "203.0.113.9"}
    series = {(r[0], r[1]): r[2] for r in conn.execute("SELECT t, client_key, decode FROM series_1s")}
    assert series == {(100, "192.0.2.84"): 12, (101, "192.0.2.84"): 3, (100, "203.0.113.9"): 1}
    clients = {r[0]: (r[1], r[2]) for r in conn.execute("SELECT key, name, color FROM clients")}
    # The key had no row: it takes the colour of the address with the most requests.
    assert clients == {"192.0.2.84": ("Dev box", "#222222"), "203.0.113.9": (None, "#333333")}
    assert devices.merge(conn, cfg.devices) == 0  # nothing left to move


def test_merge_keeps_the_key_colour_and_names_it(env):
    cfg, _, conn = env
    _stored(conn, "192.0.2.84", "r1", 100, 5, "#111111")
    _stored(conn, "fe80::a:1", "r2", 100, 2, "#222222")
    devices.merge(conn, cfg.devices)
    assert [tuple(r) for r in conn.execute("SELECT key, name, color FROM clients")] == [
        ("192.0.2.84", "Dev box", "#111111")]


async def test_live_streams_from_other_addresses_show_as_the_device(dash_client, make_cfg, tmp_path):  # noqa: F811
    d, _ = await dash_client(overrides={"devices.laptop": LAPTOP})
    d.live.on_event({"type": "req.start", "id": "a", "profile": "halogen-flashnext", "ts": NOW - 3,
                     "client": {"key": "198.51.100.109", "peer_ip": "198.51.100.109", "name": "laptop"}})
    (s,) = d.tick(NOW)["live"]["streams"]
    assert (s["client"]["key"], s["client"]["name"]) == ("192.0.2.84", "Dev box")


async def _no_lookup(ip):
    return None


def test_the_gateway_follows_a_devices_conversations_across_its_addresses(make_cfg):
    # A turn sent over IPv6 after one over IPv4 continues the same conversation in the cache diagnostics.
    from kiln.gateway.app import Gateway
    from kiln.gateway.attribution import Whois
    gw = Gateway(make_cfg(**{"devices.laptop": LAPTOP}), whois=Whois(lookup=_no_lookup))
    assert gw.conversation_key("fe80::a:1") == "192.0.2.84"
    assert (gw.conversation_key("203.0.113.9"), gw.conversation_key(None)) == ("203.0.113.9", "unknown")


def test_merge_moves_rows_of_an_address_without_a_client_row(env):
    cfg, _, conn = env
    _stored(conn, "fe80::a:1", "r1", 100, 5, "#111111")
    conn.execute("DELETE FROM clients")
    assert devices.merge(conn, cfg.devices) == 1
    assert conn.execute("SELECT client_key FROM requests").fetchone()[0] == "192.0.2.84"
    assert [tuple(r) for r in conn.execute("SELECT client_key, decode FROM series_1s")] == [("192.0.2.84", 5)]


def test_merge_keeps_a_node_and_an_unknown_update_time(env):
    cfg, _, conn = env
    _stored(conn, "198.51.100.109", "r1", 100, 5, "#111111")
    conn.execute("UPDATE clients SET node = 'laptop', updated = NULL")
    devices.merge(conn, cfg.devices)
    assert tuple(conn.execute("SELECT key, node, updated FROM clients").fetchone()) == ("192.0.2.84", "laptop", None)


async def test_a_failed_merge_does_not_stop_the_dashboard(dash_client, monkeypatch, capsys):  # noqa: F811
    def locked(*a):
        raise devices.sqlite3.OperationalError("database is locked")
    monkeypatch.setattr(devices, "_move", locked)
    d, _ = await dash_client(overrides={"devices.laptop": LAPTOP})
    _stored(d.conn, "fe80::a:1", "r1", NOW - 10, 5, "#111111")  # stored before the config listed it
    from kiln.dash.app import Dash
    Dash(d.cfg, clock=lambda: NOW, sensors=d.sensors, whois=d.whois, manager=d.manager, sources=False)
    assert "devices not merged" in capsys.readouterr().out
