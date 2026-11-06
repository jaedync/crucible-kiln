"""Host allowlist (DNS rebinding), proxied requests and node names (security review H1, H3, M1)."""
import pytest

from kiln import config, netguard


def test_host_name_parses_ports_brackets_case_and_the_trailing_dot():
    assert netguard.host_name("STRIX.local.:7000") == "strix.local"
    assert netguard.host_name("[::1]:7000") == "::1"
    assert netguard.host_name("127.0.0.1") == "127.0.0.1"
    assert netguard.host_name("") is None
    assert netguard.host_name("a b") is None


def test_defaults_accept_local_names_the_host_and_any_address_literal():
    g = netguard.HostGuard(netguard.default_names("strix", hostname="strix.lan"))
    for h in ("localhost:7000", "127.0.0.1:7000", "[::1]:7000", "strix:7000", "strix.local:7000", "strix.lan",
              "10.0.0.5:7000", "[fd00::1]:7000", "100.64.0.7:8000", "host.containers.internal:8000"):
        assert g.allows(h), h
    # A rebinding page keeps its own name in Host: refused.
    for h in ("rebind.attacker.example:7000", "strix.attacker.example", "localhost.attacker.example", "", "x y"):
        assert not g.allows(h), h


def test_the_magicdns_name_and_configured_names_are_added():
    g = netguard.HostGuard(netguard.default_names("strix", extra=("Kiln.Example.org",), hostname="strix"))
    g.add("strix.example.ts.net")
    assert g.allows("strix.example.ts.net:7000") and g.allows("kiln.example.org")
    assert not g.allows("other.example.ts.net")


def test_a_star_turns_the_check_off():
    assert netguard.HostGuard(netguard.default_names("h", extra=("*",))).allows("anything.example")


def test_config_reads_allowed_hosts_and_allow_loopback(make_cfg):
    cfg = make_cfg()
    assert cfg.dash_allowed_hosts == () and cfg.gateway_allowed_hosts == () and cfg.allow_loopback is True
    cfg = make_cfg(**{"dash.allowed_hosts": ["kiln.lan"], "gateway.allowed_hosts": ["gw.lan"],
                      "actions.allow_loopback": False})
    assert cfg.dash_allowed_hosts == ("kiln.lan",) and cfg.gateway_allowed_hosts == ("gw.lan",)
    assert cfg.allow_loopback is False
    for bad in ("kiln.lan", ["a b"], [""], [3]):
        with pytest.raises(config.ConfigError):
            make_cfg(**{"dash.allowed_hosts": bad})
    with pytest.raises(config.ConfigError):
        make_cfg(**{"actions.allow_loopback": "yes"})


class _Req:
    def __init__(self, headers):
        self.headers = headers


@pytest.mark.parametrize("header", ["Forwarded", "X-Forwarded-For", "X-Real-IP", "Tailscale-User-Login",
                                    "CF-Connecting-IP", "x-forwarded-for"])
def test_proxy_headers_mark_a_request_as_proxied(header):
    from multidict import CIMultiDict
    assert netguard.proxied(_Req(CIMultiDict({header: "1.2.3.4"})))
    assert not netguard.proxied(_Req(CIMultiDict({"User-Agent": "curl"})))


def test_a_node_name_without_a_dot_matches_only_the_own_tailnet():
    allow = ("owner-laptop", "friend-pc.your-tailnet.ts.net")
    own = "example.ts.net"
    assert netguard.node_allowed("owner-laptop.example.ts.net", allow, own)
    assert netguard.node_allowed("OWNER-LAPTOP.example.ts.net.", allow, own)
    # Node sharing: a device in another tailnet with the same first label.
    assert not netguard.node_allowed("owner-laptop.tailnet-name.ts.net", allow, own)
    assert not netguard.node_allowed("owner-laptop.example.ts.net", allow, None)  # own tailnet unknown
    assert netguard.node_allowed("friend-pc.your-tailnet.ts.net", allow, None)
    assert not netguard.node_allowed("friend-pc.tailxxxx.ts.net", allow, own)
    assert not netguard.node_allowed(None, allow, own)


def test_parse_tailscale_status():
    st = {"Self": {"DNSName": "strix.example.ts.net.", "TailscaleIPs": ["100.64.0.7"]},
          "MagicDNSSuffix": "example.ts.net"}
    assert netguard.parse_status(st) == {"dns_name": "strix.example.ts.net", "tailnet": "example.ts.net"}
    assert netguard.parse_status({"Self": {"DNSName": ""}}) is None
    assert netguard.parse_status([]) is None


def test_a_zone_is_only_taken_inside_brackets():
    assert netguard.host_name("1.2.3.4%evil.example") is None
    assert netguard.host_name("[fe80::1%eth0]:7000") == "fe80::1%eth0"


def test_mdns_names():
    assert netguard.is_mdns_name("strix.local:7000") and netguard.is_mdns_name("STRIX.LOCAL.")
    assert not netguard.is_mdns_name("strix:7000") and not netguard.is_mdns_name("strix.tailnet.ts.net")
