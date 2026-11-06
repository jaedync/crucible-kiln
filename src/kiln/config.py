"""kiln.toml: hosts, listen addresses, pricing, actions and engine profiles.

Parsed into frozen dataclasses so every part reads the same validated view.
"""
from __future__ import annotations

import importlib.util
import ipaddress
import os
import re
import socket
import tomllib
from dataclasses import dataclass, field
from pathlib import Path

DEFAULT_PATH = "~/.config/kiln/kiln.toml"

# No engine on this box prefills a prompt faster than this. A request whose computed prefill rate is above it
# has figures the engine got wrong: once the t20 profile logged 10 conversation-cache restores as
# prefill work (118K to 122K tokens in 0.21 to 2.7 s, 45,000 to 570,000 tok/s), which would draw a spike of
# 118,000 tok/s in the chart and set the live prefill estimate to half a million tok/s. The row stays; only
# its prefill is left out of series_1s and of the rates Kiln learns from finished prefills.
DEFAULT_MAX_PREFILL_TPS = 20_000.0
# How long the gateway keeps a request waiting for a planned switch (gateway/app.py). A typical client waits
# 300 s for response headers; 150 s leaves it time for its own retries if the switch fails.
DEFAULT_HOLD_MAX_S = 150.0
# The largest request body the gateway reads. Four 12 MP phone photos as base64 make about 20 MiB
# (tools/measure_body_memory.py); a long text conversation is a few MiB. kiln init sizes the gateway's
# MemoryMax from this value (install.gateway_memory_max_mib).
DEFAULT_MAX_BODY_MIB = 32
DEFAULT_MAX_HELD = 32  # requests that may wait for a switch at once; more get 503 at once


class ConfigError(ValueError):
    pass


def _path(p: str) -> Path:
    return Path(os.path.expanduser(p))


# Every interface in both families, as separate IPv4 and IPv6 sockets. "0.0.0.0" is IPv4 only, and
# "[::]" is IPv6 only (asyncio sets IPV6_V6ONLY). A name such as strix.local can resolve to IPv6
# alone for a moment (a client's mDNS cache refresh), so a LAN listener needs both.
ANY_HOST = "*"
_LISTEN = re.compile(r"\[([^\[\]]+)\]:([0-9]+)|([^:\[\]]+):([0-9]+)")


def _addr(s: str) -> tuple[str, int]:
    """host:port, [ipv6]:port or *:port. An IPv6 host needs its brackets."""
    m = _LISTEN.fullmatch(s)
    port = int(m[2] or m[4]) if m else -1
    # "*:0" would give each address family a different random port.
    if not m or port > 65535 or (m[3] == ANY_HOST and port == 0):
        raise ConfigError(f"listen address must be host:port, [ipv6]:port or *:port, got {s!r}")
    return m[1] or m[3], port


def bind_host(host: str) -> str | None:
    """The host for aiohttp's TCPSite: None makes asyncio bind every interface in both families."""
    return None if host == ANY_HOST else host


def local_host(host: str) -> str:
    """An address on this machine that reaches a listener bound to `host`."""
    if host in (ANY_HOST, "0.0.0.0"):
        return "127.0.0.1"
    return "::1" if host == "::" else host


def url_host(host: str) -> str:
    """`host` as it goes in a URL: IPv6 literals need brackets."""
    return f"[{host}]" if ":" in host else host


def describe_addresses(addresses) -> str:
    """Bound socket names as host:port, so the journal shows which families a listener really has."""
    # asyncio collects a listener's sockets in no fixed order; IPv4 first keeps the lines comparable.
    ordered = sorted(addresses, key=lambda a: (":" in a[0], a[0], a[1]))
    return ", ".join(f"{url_host(a[0])}:{a[1]}" for a in ordered)


@dataclass(frozen=True)
class KV:
    positions: int  # total KV positions the engine reserves
    bytes_per_position: int  # from the model architecture and cache dtype; 0 if unknown

    @property
    def bytes(self) -> int | None:
        return self.positions * self.bytes_per_position if self.bytes_per_position else None


@dataclass(frozen=True)
class Profile:
    name: str
    label: str
    engine: str  # halogen | gufo | ds4: selects the log parser
    model: str  # canonical id clients send
    served_id: str  # id the engine itself uses
    aliases: tuple[str, ...]
    hf_id: str  # Hugging Face repo id, for price lookup
    port: int  # engine port on 127.0.0.1
    context: int
    slots: int
    kv: KV
    memory_gib: float
    container: dict  # Quadlet [Container] keys -> str or list[str]; empty for native profiles
    start_timeout_s: float = 900.0  # loading weights can take minutes
    native: dict = field(default_factory=dict)  # [Service] keys for an engine run as a plain user unit
    # Why the profile can't be selected yet (for example a checkpoint still being made); None = usable.
    disabled: str | None = None
    # Request dialect the engine needs from the gateway ("crucible"); None = forward as is.
    request_compat: str | None = None
    # max_tokens the gateway adds when a client sends none (request_compat profiles only); None = never.
    default_max_tokens: int | None = None
    # request_compat profiles: "pass" maps clients' thinking controls, "drop" removes them.
    thinking: str = "drop"

    def accepts(self, model: str | None) -> bool:
        return model in (self.model, self.served_id, *self.aliases)

    @property
    def unit(self) -> str:
        return f"kiln-{self.name}.service"

    @property
    def container_name(self) -> str:
        return f"kiln-{self.name}"


@dataclass(frozen=True)
class Reference:
    name: str
    provider_model: str
    input_per_m: float
    cached_per_m: float
    output_per_m: float


@dataclass(frozen=True)
class Schedule:
    """The engine day window when other GPU jobs share the machine (ARCHITECTURE.md, Schedule). It needs
    kiln/engines/fleet.py, which the public package does not include."""
    day_start: tuple[int, int]  # local (hour, minute) the engine starts
    day_end: tuple[int, int]  # local (hour, minute) the engine stops and other GPU jobs get the GPU
    tz: str
    holds_dir: Path  # any file here pauses the other jobs' GPU queue; Kiln owns HOLD_NAME
    gpu_lock: Path  # the shared GPU flock; a holder outside Kiln's units is another GPU job
    day_profile_file: Path  # the profile the user last selected; the morning start uses it
    drain_timeout_s: float
    queue_dir: Path = Path("/tmp/crucible-gpu-queue")  # the lease wrapper's tickets (read only, for the panel)
    lease_log: Path = Path("~/.local/state/crucible/gpu-leases.jsonl").expanduser()  # the lease wrapper's log
    # The night driver's plan of its own items, read by the rack panel's queue tile. None: the
    # default file (collect/gpu_queue.py).
    plan_file: Path | None = None


@dataclass(frozen=True)
class Host:
    """Another inference machine whose collector agent pushes to this dashboard (ARCHITECTURE.md, Hosts)."""
    name: str  # stored in requests.host; also the client key prefix of its work
    node: str  # full MagicDNS name of the Tailscale node allowed to push for this host
    label: str
    engine: str
    profile: str  # requests.profile of its work; not a switchable profile
    model: str  # canonical model id: prices its work like a local profile of the same model
    hf_id: str
    context: int
    slots: int


@dataclass(frozen=True)
class Device:
    """A machine that reaches Kiln from several addresses (LAN, Wi-Fi, link-local IPv6, tailnet): one device."""
    name: str
    addresses: tuple[str, ...]  # normalized; the first is the key its requests are stored under

    @property
    def key(self) -> str:
        return self.addresses[0]


@dataclass(frozen=True)
class Config:
    host: str
    gateway_listen: tuple[str, int]
    events_listen: tuple[str, int]
    gateway_data: Path
    state_file: Path
    dash_listen: tuple[str, int]
    db_path: Path
    allow_nodes: tuple[str, ...]
    allow_users: tuple[str, ...]
    reference: Reference | None  # None: no [pricing.reference], so no API-cost figure
    openrouter_enabled: bool
    openrouter_refresh_h: float
    container_uid: int
    profiles: dict[str, Profile] = field(default_factory=dict)
    journal_backfill: str = "-30d"  # how far back the first start reads engine logs
    max_prefill_tps: float = DEFAULT_MAX_PREFILL_TPS  # above this, an engine's prefill figures are wrong
    hold_max_s: float = DEFAULT_HOLD_MAX_S  # 0: requests never wait for a switch
    quadlet_dir: Path = field(default_factory=lambda: _path("~/.config/containers/systemd"))
    user_unit_dir: Path = field(default_factory=lambda: _path("~/.config/systemd/user"))
    # User unit globs (systemctl list-units patterns) that hold the GPU while active: a switch waits for them.
    busy_units: tuple[str, ...] = ()
    schedule: Schedule | None = None  # None: no GPU sharing (no holds, no drain, no window)
    hosts: dict[str, Host] = field(default_factory=dict)  # remote hosts that push work and live state
    devices: tuple[Device, ...] = ()
    # Extra Host names each listener accepts (kiln.netguard: DNS rebinding); "*" turns the check off.
    dash_allowed_hosts: tuple[str, ...] = ()
    gateway_allowed_hosts: tuple[str, ...] = ()
    gateway_allowed_origins: tuple[str, ...] = ()  # web pages that may call the gateway; "*" for all
    # A direct request from this machine may act (the rack kiosk, `kiln switch`). A proxied one never may.
    allow_loopback: bool = True
    max_body_mib: int = DEFAULT_MAX_BODY_MIB
    max_held: int = DEFAULT_MAX_HELD
    _device_by_addr: dict[str, Device] = field(default_factory=dict, repr=False)

    def device_for(self, addr: str | None) -> Device | None:
        """The configured device an address (as the gateway normalizes it) belongs to."""
        return self._device_by_addr.get(addr) if addr else None

    @property
    def lock_file(self) -> Path:
        return self.state_file.with_name("eval.lock")

    @property
    def admission_file(self) -> Path:
        return self.state_file.with_name("admission-hold.json")

    @property
    def panel_file(self) -> Path:
        """The rack panel's shared screen and its settings (dash/panel_sync.py)."""
        return self.state_file.with_name("panel.json")

    def profile_for_model(self, model: str | None) -> Profile | None:
        return next((p for p in self.profiles.values() if p.accepts(model)), None)


def _profile(name: str, d: dict) -> Profile:
    try:
        # A new user cannot know the engine's pool size: an engine that gives every slot the whole context
        # reserves slots x context positions, so that is the default. bytes_per_position 0 means unknown.
        kv, slots, context = d.get("kv", {}), int(d.get("slots", 1)), int(d["context"])
        for key, v, low, high in (("context", context, 1, 2**31 - 1), ("slots", slots, 1, 4096),
                                  ("port", int(d["port"]), 1, 65535)):
            if not low <= v <= high:
                raise ConfigError(f"profile {name}: {key} must be a whole number from {low} to {high}, got {v}")
        return Profile(
            name=name, label=d.get("label", name), engine=d["engine"], model=d["model"],
            served_id=d.get("served_id", d["model"]), aliases=tuple(d.get("aliases", ())), hf_id=d.get("hf_id", ""),
            port=int(d["port"]), context=context, slots=slots,
            kv=KV(int(kv.get("positions", slots * context)), int(kv.get("bytes_per_position", 0))),
            memory_gib=float(d.get("memory_gib", 0)), container=dict(d.get("container", {})),
            start_timeout_s=float(d.get("start_timeout_s", 900)), native=dict(d.get("native", {})),
            disabled=str(d["disabled"]) if d.get("disabled") else None,
            request_compat=d.get("request_compat"),
            default_max_tokens=int(d["default_max_tokens"]) if d.get("default_max_tokens") else None,
            thinking=str(d.get("thinking", "drop")))
    except KeyError as e:
        raise ConfigError(f"profile {name}: missing {e.args[0]}") from None
    except (ValueError, TypeError) as e:
        raise ConfigError(f"profile {name}: {_bad_number(d) or e}") from None


def _bad_number(d: dict) -> str | None:
    """The first number field of a profile that is not a number, said plainly."""
    for key, kind in (("port", int), ("context", int), ("slots", int), ("memory_gib", float),
                      ("start_timeout_s", float), ("default_max_tokens", int)):
        if key in d:
            try:
                kind(d[key])
            except (ValueError, TypeError):
                return f"{key} must be a {'whole ' if kind is int else ''}number, got {d[key]!r}"
    return None


REQUEST_COMPATS = (None, "crucible")


def _clock_time(s: str, what: str) -> tuple[int, int]:
    m = re.fullmatch(r"(\d\d):(\d\d)", s.strip())
    if not m or int(m.group(1)) > 23 or int(m.group(2)) > 59:
        raise ConfigError(f"schedule.{what} must be HH:MM, got {s!r}")
    return int(m.group(1)), int(m.group(2))


def zone_from_env() -> str | None:
    """The zone that TZ names, if TZ is set. Python's local time follows TZ, so the zone name must too."""
    tz = os.environ.get("TZ", "").strip().lstrip(":")
    if not tz:
        return None
    return tz.split("zoneinfo/", 1)[1] if "zoneinfo/" in tz else tz


def system_tz() -> str:
    """The IANA name of this machine's zone, the one the dashboard's dates and hours use; else UTC."""
    env = zone_from_env()
    if env:
        return env
    try:
        target = os.readlink("/etc/localtime")
    except OSError:
        return "UTC"
    return target.split("zoneinfo/", 1)[1] if "zoneinfo/" in target else "UTC"


def _has_module(name: str) -> bool:
    try:
        return importlib.util.find_spec(name) is not None
    except (ImportError, ValueError):
        return False


def _schedule(d: dict | None) -> Schedule | None:
    if not d:
        return None
    if not _has_module("kiln.engines.fleet"):
        raise ConfigError("[schedule] needs kiln/engines/fleet.py, which is not part of this install; remove"
                          " [schedule] from kiln.toml")
    start, sep, end = str(d.get("day", "")).partition("-")
    if not sep:
        raise ConfigError("schedule.day must be HH:MM-HH:MM")
    return Schedule(
        day_start=_clock_time(start, "day"), day_end=_clock_time(end, "day"), tz=d.get("tz") or system_tz(),
        holds_dir=_path(d.get("holds_dir", "~/.local/state/crucible/gpu-holds")),
        gpu_lock=_path(d.get("gpu_lock", "/tmp/crucible-gpu.lock")),
        day_profile_file=_path(d.get("day_profile_file", "~/.local/state/kiln/day_profile")),
        drain_timeout_s=float(d.get("drain_timeout_s", 900)),
        queue_dir=_path(d.get("queue_dir", "/tmp/crucible-gpu-queue")),
        lease_log=_path(d.get("lease_log", "~/.local/state/crucible/gpu-leases.jsonl")),
        plan_file=_path(d["plan_file"]) if d.get("plan_file") else None)


_NAME = re.compile(r"^[a-z0-9][a-z0-9-]{0,40}$")  # becomes a unit and container name
# A host's full MagicDNS name: the first label alone is not unique (shared-in nodes, renames).
_FQDN = re.compile(r"^[a-z0-9][a-z0-9-]{0,62}(\.[a-z0-9][a-z0-9-]{0,62})+$")
_UNIT_KEY = re.compile(r"^[A-Za-z][A-Za-z0-9]*$")


def _check_unit_values(p: Profile, section: dict, what: str) -> None:
    for key, value in section.items():
        if not _UNIT_KEY.match(key):
            raise ConfigError(f"profile {p.name}: bad {what} key {key!r}")
        for v in (value if isinstance(value, list) else [value]):
            if not isinstance(v, (str, int, float)) or any(c in str(v) for c in "\r\n\x00"):
                raise ConfigError(f"profile {p.name}: {what}.{key} must be a single-line value")
            if str(v).endswith("\\"):  # systemd would join the unit's next line to this one
                raise ConfigError(f"profile {p.name}: {what}.{key} must not end in a backslash")


def _check_kind(p: Profile) -> Profile:
    if not _NAME.match(p.name):
        raise ConfigError(f"profile name {p.name!r}: use lowercase letters, digits and dashes")
    if p.unit in ("kiln-gateway.service", "kiln-dash.service"):  # the same user unit directory
        raise ConfigError(f"profile name {p.name!r} would replace Kiln's own {p.unit}")
    if any(ord(c) < 32 or c == "\x7f" for c in p.label) or p.label.endswith("\\"):
        raise ConfigError(f"profile {p.name}: label must be a single line without a trailing backslash")
    _check_unit_values(p, p.container, "container")
    _check_unit_values(p, p.native, "native")
    if bool(p.container) == bool(p.native):
        raise ConfigError(f"profile {p.name}: give exactly one of [container] or [native]")
    if p.native and "ExecStart" not in p.native:
        raise ConfigError(f"profile {p.name}: [native] needs ExecStart")
    if p.thinking not in ("drop", "pass"):
        raise ConfigError(f"profile {p.name}: thinking must be drop or pass")
    if p.request_compat not in REQUEST_COMPATS:
        raise ConfigError(f"profile {p.name}: request_compat must be one of {REQUEST_COMPATS[1:]}")
    return p


def _host(name: str, d: dict, local: str, profiles: dict) -> Host:
    if not _NAME.match(name) or name == local:
        raise ConfigError(f"hosts.{name}: use lowercase letters, digits and dashes, not the local host name")
    try:
        h = Host(name=name, node=str(d["node"]), label=str(d.get("label", name)), engine=str(d["engine"]),
                 profile=str(d.get("profile", name)), model=str(d["model"]), hf_id=str(d.get("hf_id", "")),
                 context=int(d.get("context", 0)), slots=int(d.get("slots", 1)))
    except KeyError as e:
        raise ConfigError(f"hosts.{name}: missing {e.args[0]}") from None
    if not _FQDN.match(h.node):
        raise ConfigError(f"hosts.{name}: node must be the full MagicDNS name (host.<tailnet>.ts.net)")
    if not _NAME.match(h.profile) or h.profile in profiles:
        raise ConfigError(f"hosts.{name}: profile must be a simple name, not a local profile")
    if any(c in h.label for c in "\r\n\x00"):
        raise ConfigError(f"hosts.{name}: label must be a single line")
    return h


def _ip(s) -> str:
    """One address in the form the gateway records (no zone, IPv4-mapped IPv6 as IPv4)."""
    try:
        ip = ipaddress.ip_address(str(s).split("%")[0])
    except ValueError:
        raise ConfigError(f"devices: {s!r} is not an IP address") from None
    return str(ip.ipv4_mapped if ip.version == 6 and ip.ipv4_mapped else ip)


def _devices(d: dict) -> tuple[tuple[Device, ...], dict[str, Device]]:
    out, by_addr = [], {}
    for name, v in d.items():
        dev = Device(name=str(v.get("name", name)), addresses=tuple(_ip(a) for a in v.get("addresses", ())))
        if not dev.addresses:
            raise ConfigError(f"devices.{name}: give at least one address")
        if any(c in dev.name for c in "\r\n\x00"):
            raise ConfigError(f"devices.{name}: name must be a single line")
        if len(set(dev.addresses)) < len(dev.addresses):
            raise ConfigError(f"devices.{name}: an address is listed twice")
        for a in dev.addresses:
            if ipaddress.ip_address(a).is_loopback:
                raise ConfigError(f"devices.{name}: {a} is loopback, the Kiln host itself")
            if by_addr.setdefault(a, dev) is not dev:
                raise ConfigError(f"devices.{name}: {a} already belongs to {by_addr[a].name}")
        out.append(dev)
    return tuple(out), by_addr


def _globs(v) -> tuple[str, ...]:
    if isinstance(v, str) or not all(isinstance(g, str) and g and not g.startswith("-") for g in v):
        raise ConfigError("engines.busy_units must be a list of unit name globs, such as [\"eval-*\"]")
    return tuple(v)


_HOST_ENTRY = re.compile(r"^(\*|[A-Za-z0-9_][A-Za-z0-9_.-]{0,252})$")


def _allowed_hosts(v, what: str) -> tuple[str, ...]:
    if isinstance(v, str) or not isinstance(v, (list, tuple)) or not all(
            isinstance(h, str) and _HOST_ENTRY.match(h) for h in v):
        raise ConfigError(f"{what}.allowed_hosts must be a list of host names, such as [\"kiln.example.org\"]")
    return tuple(v)


# scheme://host[:port] for any scheme: a desktop app (tauri://localhost) or an extension (chrome-extension://id)
_ORIGIN_ENTRY = re.compile(r"^(\*|[A-Za-z][A-Za-z0-9+.-]*://([A-Za-z0-9_.-]+|\[[0-9A-Fa-f:.]+\])(:[0-9]{1,5})?/?)$")


def _allowed_origins(v) -> tuple[str, ...]:
    if isinstance(v, str) or not isinstance(v, (list, tuple)) or not all(
            isinstance(o, str) and _ORIGIN_ENTRY.match(o) for o in v):
        raise ConfigError("gateway.allowed_origins must be a list of origins, such as [\"https://chat.example.org\"],"
                          " or [\"*\"]")
    return tuple(v)


def _bool(v, what: str) -> bool:
    if not isinstance(v, bool):
        raise ConfigError(f"{what} must be true or false")
    return v


def _count(v, what: str, low: int, high: int) -> int:
    if isinstance(v, bool) or not isinstance(v, int) or not low <= v <= high:
        raise ConfigError(f"{what} must be a whole number from {low} to {high}")
    return v


def _loopback(host: str) -> bool:
    if host == "localhost":
        return True
    try:
        return ipaddress.ip_address(host).is_loopback
    except ValueError:
        return False


def _short_hostname() -> str:
    """This machine's name without its domain: the name requests and hardware samples are stored under."""
    return socket.gethostname().split(".")[0] or "localhost"


def parse(d: dict) -> Config:
    g, dash = d.get("gateway", {}), d.get("dash", {})
    ref = d.get("pricing", {}).get("reference", {})
    orr = d.get("pricing", {}).get("openrouter", {})
    act = d.get("actions", {})
    try:
        reference = Reference(ref["name"], ref["provider_model"], float(ref["input_per_m"]),
                              float(ref["cached_per_m"]), float(ref["output_per_m"])) if ref else None
    except KeyError as e:
        raise ConfigError(f"pricing.reference: missing {e.args[0]}") from None
    host = str(d.get("host") or _short_hostname())
    profiles = {n: _check_kind(_profile(n, p)) for n, p in d.get("profiles", {}).items()}
    devices, device_by_addr = _devices(d.get("devices", {}))
    ports = {p.port for p in profiles.values()}
    cfg = Config(
        host=host,
        # Loopback unless the user opts in: the gateway and the dashboard have no login of their own.
        gateway_listen=_addr(g.get("listen", "127.0.0.1:8000")),
        events_listen=_addr(g.get("events_listen", "127.0.0.1:8001")),
        gateway_data=_path(g.get("data_dir", "~/.local/share/kiln/gateway")),
        state_file=_path(g.get("state_file", "~/.local/state/kiln/active.json")),
        dash_listen=_addr(dash.get("listen", "127.0.0.1:7000")),
        db_path=_path(dash.get("db", "~/.local/share/kiln/kiln.db")),
        allow_nodes=tuple(act.get("allow_nodes", ())), allow_users=tuple(act.get("allow_users", ())),
        reference=reference, openrouter_enabled=bool(orr.get("enabled", True)),
        openrouter_refresh_h=float(orr.get("refresh_hours", 24)),
        container_uid=int(d.get("container_uid", os.getuid())), profiles=profiles,
        journal_backfill=str(dash.get("journal_backfill", "-30d")),
        max_prefill_tps=float(dash.get("max_prefill_tps", DEFAULT_MAX_PREFILL_TPS)),
        hold_max_s=float(g.get("hold_max_s", DEFAULT_HOLD_MAX_S)),
        quadlet_dir=_path(d.get("engines", {}).get("quadlet_dir", "~/.config/containers/systemd")),
        user_unit_dir=_path(d.get("engines", {}).get("user_unit_dir", "~/.config/systemd/user")),
        busy_units=_globs(d.get("engines", {}).get("busy_units", ())),
        schedule=_schedule(d.get("schedule")),
        hosts={n: _host(n, h, host, profiles) for n, h in d.get("hosts", {}).items()},
        devices=devices, _device_by_addr=device_by_addr,
        dash_allowed_hosts=_allowed_hosts(dash.get("allowed_hosts", ()), "dash"),
        gateway_allowed_hosts=_allowed_hosts(g.get("allowed_hosts", ()), "gateway"),
        gateway_allowed_origins=_allowed_origins(g.get("allowed_origins", ())),
        allow_loopback=_bool(act.get("allow_loopback", True), "actions.allow_loopback"),
        max_body_mib=_count(g.get("max_body_mib", DEFAULT_MAX_BODY_MIB), "gateway.max_body_mib", 1, 1024),
        max_held=_count(g.get("max_held", DEFAULT_MAX_HELD), "gateway.max_held", 1, 10_000))
    if not _loopback(cfg.events_listen[0]):
        # The feed carries every request's record; it has no login, so only this machine may read it.
        raise ConfigError("gateway.events_listen must be a loopback address, such as 127.0.0.1:8001")
    if cfg.gateway_listen[1] in ports or cfg.dash_listen[1] in ports:
        raise ConfigError("an engine port collides with the gateway or dashboard port")
    if not 0 <= cfg.hold_max_s < float("inf"):
        raise ConfigError("gateway.hold_max_s must be 0 (off) or a number of seconds")
    # Two profiles may serve the same canonical model (that is the point of
    # switching engines), but a model id or alias must not mean two models. A served id is the engine's own
    # name: two engines of different models may share it (each public Crucible server calls itself
    # `crucible`), because the gateway asks only the active profile. It must not be another model's id.
    by_id: dict[str, str] = {}
    for p in profiles.values():
        for i in (p.model, *p.aliases):
            if by_id.setdefault(i, p.model) != p.model:
                raise ConfigError(f"model id {i!r} maps to both {by_id[i]} and {p.model}")
    for p in profiles.values():
        if by_id.get(p.served_id, p.model) != p.model:
            raise ConfigError(f"model id {p.served_id!r} maps to both {by_id[p.served_id]} and {p.model}")
    return cfg


def default_path() -> str:
    """$KILN_CONFIG, else kiln/kiln.toml in $XDG_CONFIG_HOME (an absolute path), else in ~/.config."""
    xdg = os.environ.get("XDG_CONFIG_HOME", "")
    return os.environ.get("KILN_CONFIG") or (os.path.join(xdg, "kiln", "kiln.toml") if os.path.isabs(xdg)
                                             else DEFAULT_PATH)


def load(path: str | os.PathLike | None = None) -> Config:
    p = _path(str(path or default_path()))
    with open(p, "rb") as f:
        try:
            data = tomllib.load(f)
        except tomllib.TOMLDecodeError as e:
            raise ConfigError(f"not valid TOML: {e}") from None
    try:
        return parse(data)
    except ConfigError:
        raise
    except (ValueError, TypeError, AttributeError) as e:  # a value of the wrong type, such as context = "big"
        raise ConfigError(f"a value has the wrong type or form: {e}") from None
