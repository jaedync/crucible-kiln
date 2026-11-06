"""kiln init: write a first kiln.toml and the user units for kiln-gateway and kiln-dash.

No prompts: everything comes from flags, so a script or a README can run it. The paths come from the XDG
base directories of the given home and environment, and systemctl is injected, so tests never touch the
real ~/.config or the user manager.
"""
from __future__ import annotations

import re
import shutil
import subprocess
import sys
import tomllib
from dataclasses import dataclass
from pathlib import Path
from typing import Callable, Mapping

from kiln import config
from kiln.gateway.app import memory_max_mib
from kiln.systemd_quote import UnitValueError, exec_line, literal

ENGINE_PORT = 8790  # the public Crucible server's default port
GATEWAY_PORT = 8000
DASH_PORT = 7000
EVENTS_PORT = 8001
DEFAULT_CTX = 32768  # the Crucible server's own default
PROFILE = "crucible"
SERVICES = ("kiln-gateway", "kiln-dash")

Runner = Callable[[list[str]], tuple[int, str]]


def run_systemctl(cmd: list[str]) -> tuple[int, str]:
    try:
        p = subprocess.run(cmd, capture_output=True, text=True, timeout=60)
    except (OSError, subprocess.TimeoutExpired) as e:
        return 127, str(e)
    return p.returncode, (p.stdout + p.stderr).strip()


@dataclass(frozen=True)
class Dirs:
    """The XDG base directories for one user (the spec's defaults when a variable is unset or relative)."""
    config: Path
    data: Path
    state: Path

    @classmethod
    def of(cls, home: Path, env: Mapping[str, str]) -> "Dirs":
        def base(var: str, default: str) -> Path:
            v = env.get(var, "")
            return Path(v) if v and Path(v).is_absolute() else home / default
        return cls(base("XDG_CONFIG_HOME", ".config"), base("XDG_DATA_HOME", ".local/share"),
                   base("XDG_STATE_HOME", ".local/state"))

    @property
    def config_file(self) -> Path:
        return self.config / "kiln" / "kiln.toml"

    @property
    def unit_dir(self) -> Path:
        return self.config / "systemd" / "user"


class InitError(Exception):
    pass


@dataclass(frozen=True)
class Crucible:
    root: Path
    engine: Path
    python: str


def find_crucible(root: Path, fallback_python: str) -> Crucible:
    """A checkout of the public Crucible repo with its engine built. Its own .venv runs the server if it has one."""
    root = root.expanduser().resolve()
    if not (root / "server" / "__main__.py").is_file():
        raise InitError(f"{root}: no server/__main__.py; give --crucible a checkout of crucible-strix-halo")
    engine = root / "build" / "engine" / "crucible"
    if not engine.is_file():
        raise InitError(f"{engine}: not found; build the engine first (see docs/BUILD.md in the checkout)")
    venv = root / ".venv" / "bin" / "python"
    return Crucible(root, engine, str(venv) if venv.exists() else fallback_python)


def model_id(pack: Path) -> str:
    """A canonical model id from the pack's directory name; clients can also send the server's id `crucible`."""
    slug = re.sub(r"[^a-z0-9.]+", "-", pack.name.lower()).strip("-.")
    return slug or "crucible-model"


def _q(s: str) -> str:
    """A TOML basic string."""
    return '"' + s.replace("\\", "\\\\").replace('"', '\\"') + '"'


def render_config(*, host: str, dirs: Dirs, crucible: Crucible, pack: Path, gateway_port: int, dash_port: int,
                  engine_port: int, ctx: int, slots: int, lan: bool) -> str:
    bind = "*" if lan else "127.0.0.1"
    # Quoted and escaped per argument: a space or a % in a path must not split it or become a specifier.
    try:
        exec_start = exec_line([crucible.python, "-m", "server", str(pack), "--engine", str(crucible.engine),
                                "--ctx", str(ctx), "--streams", str(slots), "--port", str(engine_port)])
        workdir = literal(str(crucible.root))
    except UnitValueError:
        raise InitError("a path contains a line break or ends in a backslash; move it and run init again") from None
    model = model_id(pack)
    lan_note = ("# Every interface (IPv4 and IPv6): other machines on the network can use the gateway and see the\n"
                "# dashboard. Only localhost may switch engines unless [actions] names Tailscale nodes.\n"
                if lan else "# Loopback only. `kiln init --lan --force` writes `*:PORT` (every interface) instead.\n")
    return f"""# Kiln configuration, written by `kiln init`. Edit it, then restart kiln-gateway and kiln-dash:
#   systemctl --user restart kiln-gateway kiln-dash
# `kiln check` validates this file; `kiln doctor` checks the whole setup.

# The name stored with every request and hardware sample (default: this machine's short host name).
host = {_q(host)}

[gateway]
# OpenAI-compatible endpoint for clients: http://<this machine>:{gateway_port}/v1
{lan_note}listen = {_q(f"{bind}:{gateway_port}")}
# Live events for the dashboard. Keep it on loopback.
events_listen = {_q(f"127.0.0.1:{EVENTS_PORT}")}
data_dir = {_q(str(dirs.data / "kiln" / "gateway"))}
state_file = {_q(str(dirs.state / "kiln" / "active.json"))}

[dash]
listen = {_q(f"{bind}:{dash_port}")}
db = {_q(str(dirs.data / "kiln" / "kiln.db"))}

[engines]
# Native profiles become user units here (kiln-<profile>.service).
user_unit_dir = {_q(str(dirs.unit_dir))}

# [actions]
# Tailscale devices that may switch engines from the dashboard: a name without a dot is a device of
# this machine's own tailnet; give the full MagicDNS name for a device of another tailnet.
# Localhost may switch too (allow_loopback = false turns that off; set it behind a reverse proxy).
# allow_loopback = true
# allow_nodes = ["my-laptop"]

# [pricing.reference]
# Optional: price every request as if it went to a paid API. Without this table the dashboard shows
# no API cost.
# name = "Some API model"
# provider_model = "vendor/model"
# input_per_m = 2.0
# cached_per_m = 0.2
# output_per_m = 10.0

[pricing.openrouter]
# OpenRouter's list price for the profile's hf_id (needs internet; set false to never fetch).
enabled = false

# --- engine profiles ------------------------------------------------------------
# One profile per way to run an engine. `kiln switch <profile>` starts it as a user unit.
[profiles.{PROFILE}]
label = {_q(f"Crucible · {pack.name}")}
# Selects the log parser.
engine = "crucible"
# The id clients send. The server's own id ("crucible") also works.
model = {_q(model)}
# The server's --model-id (its default).
served_id = "crucible"
# The server's --port (it listens on 127.0.0.1), --ctx and --streams.
port = {engine_port}
context = {ctx}
slots = {slots}
# KV positions default to slots x context. memory_gib (expected GPU memory) is optional.
# Remove request fields that the Crucible server refuses, and map clients' thinking controls.
request_compat = "crucible"
thinking = "pass"
# The Hugging Face id, for OpenRouter prices.
# hf_id = "Org/Model"

[profiles.{PROFILE}.native]
# [Service] keys of the engine's user unit. Kiln adds Restart, timeouts and Conflicts.
ExecStart = {_q(exec_start)}
WorkingDirectory = {_q(workdir)}
Environment = ["PATH=/opt/rocm/bin:/usr/local/bin:/usr/bin:/bin", "PYTHONUNBUFFERED=1"]
LimitMEMLOCK = "infinity"
"""


def kiln_command(which: Callable[[str], str | None] = shutil.which, python: str = sys.executable) -> str:
    """The installed `kiln` entry point: on PATH, else next to this interpreter, else the module itself."""
    found = which("kiln")
    if found:  # as found on PATH, not resolved: a tool manager's shim (~/.local/bin/kiln) survives upgrades
        return str(Path(found).absolute())
    beside = Path(python).with_name("kiln")
    return str(beside) if beside.exists() else f"{python} -m kiln.cli"


def _kiln_argv(kiln: str) -> list[str]:
    """kiln_command's text as arguments: its fallback is an interpreter path and `-m kiln.cli`."""
    fallback = " -m kiln.cli"
    return [kiln.removesuffix(fallback), "-m", "kiln.cli"] if kiln.endswith(fallback) else [kiln]


def render_service(name: str, kiln: str, config_file: Path) -> str:
    # The gateway's memory follows the body cap of the config that init writes (the default); a larger
    # [gateway] max_body_mib needs a larger MemoryMax (gateway.app.memory_max_mib gives it).
    gw_mem = f"{memory_max_mib(config.DEFAULT_MAX_BODY_MIB)}M"
    what = {"kiln-gateway": ("Kiln gateway (OpenAI-compatible relay with usage records)", "gateway", "2", gw_mem),
            "kiln-dash": ("Kiln dashboard, ingest and engine manager", "dash", "5", "1G")}[name]
    after = "network-online.target" + (" kiln-gateway.service" if name == "kiln-dash" else "")
    return f"""# Written by `kiln init`.
[Unit]
Description={what[0]}
After={after}

[Service]
Environment=PYTHONUNBUFFERED=1
ExecStart={exec_line([*_kiln_argv(kiln), "--config", str(config_file), what[1]])}
Restart=always
RestartSec={what[2]}
MemoryMax={what[3]}
NoNewPrivileges=yes

[Install]
WantedBy=default.target
"""


def wait_for_ports(addrs: list[tuple[str, int]], timeout_s: float = 10.0) -> list[tuple[str, int]]:
    """The addresses that still refuse a TCP connection after timeout_s."""
    import socket
    import time
    deadline, left = time.monotonic() + timeout_s, list(addrs)
    while left:
        for a in list(left):
            try:
                socket.create_connection(a, timeout=0.5).close()
                left.remove(a)
            except OSError:
                pass
        if not left or time.monotonic() >= deadline:
            break
        time.sleep(0.2)
    return left


def init(*, crucible_dir: Path, pack: Path, home: Path, env: Mapping[str, str], systemctl: Runner,
         kiln: str, host: str, python: str, user: str, gateway_port: int = GATEWAY_PORT, dash_port: int = DASH_PORT,
         engine_port: int = ENGINE_PORT, ctx: int = DEFAULT_CTX, slots: int = 1, lan: bool = False,
         force: bool = False, start: bool = True, out: Callable[[str], None] = print,
         which: Callable[[str], str | None] = shutil.which,
         wait_ready: Callable[[list[tuple[str, int]]], list[tuple[str, int]]] = lambda addrs: []) -> int:
    dirs = Dirs.of(home, env)
    try:
        crucible = find_crucible(crucible_dir, python)
        pack = pack.expanduser().resolve()
        if not pack.is_dir():
            raise InitError(f"{pack}: not a directory; give --pack the EXL3 checkpoint directory")
        if not (pack / "config.json").is_file():
            raise InitError(f"{pack}: no config.json; give --pack the EXL3 checkpoint directory (the one that"
                            " holds config.json, the tokenizer files and the *.safetensors weights)")
        if len({gateway_port, dash_port, engine_port, EVENTS_PORT}) < 4:
            raise InitError("the gateway, dashboard, engine and events ports must all differ")
        text = render_config(host=host, dirs=dirs, crucible=crucible, pack=pack, gateway_port=gateway_port,
                             dash_port=dash_port, engine_port=engine_port, ctx=ctx, slots=slots, lan=lan)
        config.parse(tomllib.loads(text))  # never write a file that kiln itself would refuse
    except (InitError, config.ConfigError) as e:
        out(f"kiln init: {e}")
        return 2
    files = {dirs.config_file: text}
    for name in SERVICES:
        files[dirs.unit_dir / f"{name}.service"] = render_service(name, kiln, dirs.config_file)
    there = [p for p, t in files.items() if p.exists() and p.read_text() != t]
    if there and not force:
        out("kiln init: these files exist; run again with --force to replace them:")
        for p in there:
            out(f"  {p}")
        return 1
    for path, t in files.items():
        path.parent.mkdir(parents=True, exist_ok=True)
        tmp = path.with_name(path.name + ".tmp")
        tmp.write_text(t)
        tmp.replace(path)
        out(f"wrote {path}")
    units = [f"{n}.service" for n in SERVICES]
    cmds = [["systemctl", "--user", "daemon-reload"], ["systemctl", "--user", "enable", *units]]
    if start:  # restart: an init --force must reach units that already run with the old kiln.toml
        cmds.append(["systemctl", "--user", "restart", *units])
    for cmd in cmds:
        code, msg = systemctl(cmd)
        if code:
            out(f"kiln init: {' '.join(cmd)} failed ({code}): {msg[-300:]}")
            if cmd[2] == "restart":
                out("  see `journalctl --user -u kiln-dash -u kiln-gateway -n 50`")
            return 1
    code, linger = systemctl(["loginctl", "show-user", user, "--property=Linger", "--value"])
    lingers = code == 0 and linger.strip() == "yes"
    dash_host = "127.0.0.1" if not lan else host
    steps = [] if start else [("systemctl --user start kiln-gateway kiln-dash",
                               "start the gateway and the dashboard")]
    # The kiln that the units run, by name only when that name finds it (a venv install is not on PATH).
    on_path = which("kiln")
    cli = "kiln" if on_path and str(Path(on_path).absolute()) == kiln else kiln
    steps.append((f"{cli} doctor", "check the setup"))
    if not lingers:
        steps.append(('loginctl enable-linger "$USER"', "keep Kiln and the engine running after you log out"))
    steps.append((f"{cli} switch {PROFILE}", "start the Crucible engine (loads the weights)"))
    width = max(len(c) for c, _ in steps) + 2
    started = ""
    if start:
        # Both units listen on loopback also with --lan ("*"), so loopback is the address to try.
        missing = wait_ready([("127.0.0.1", gateway_port), ("127.0.0.1", dash_port)])
        unit = {gateway_port: "kiln-gateway", dash_port: "kiln-dash"}
        started = "Started kiln-gateway and kiln-dash.\n" if not missing else "".join(
            f"Started {unit[p]}, but 127.0.0.1:{p} does not answer yet; see"
            f" `journalctl --user -u {unit[p]} -n 50`\n" for _, p in missing)
    out("\n" + started + "Next:")
    for c, why in steps:
        out(f"  {c:<{width}}# {why}")
    out(f"Dashboard: http://{dash_host}:{dash_port}/   Gateway: http://{dash_host}:{gateway_port}/v1")
    return 0
