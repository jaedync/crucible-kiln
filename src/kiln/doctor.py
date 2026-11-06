"""kiln doctor: one line per check of a Kiln setup, PASS, WARN or FAIL, each with a one-line fix.

Every check is a pure function over the facts it needs. `gather` collects those facts (it runs
systemctl, journalctl and socket probes through injected callables), so the checks are tested without
this machine's real state. The exit code is 1 when any check fails.
"""
from __future__ import annotations

import json
import re
import socket
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Callable, Iterable

PASS, WARN, FAIL = "PASS", "WARN", "FAIL"
MIN_PYTHON = (3, 11)
CACHESTAT_KERNEL = (6, 5)  # cachestat(2): the dashboard's page-cache figures for the engine's weights
ENGINE_LINE = re.compile(r"\bcrucible_(prefill|decode|turn)\b")
JOURNAL_LINES = 2000  # how far back the engine-line check reads the engine unit's journal


@dataclass(frozen=True)
class Result:
    name: str
    status: str
    detail: str
    fix: str = ""

    def line(self) -> str:
        tail = f"  fix: {self.fix}" if self.fix and self.status != PASS else ""
        return f"{self.status}  {self.name}: {self.detail}{tail}"


# -- checks (pure) -------------------------------------------------------------------------------

def check_python(version: tuple[int, ...]) -> Result:
    v = ".".join(map(str, version[:3]))
    if tuple(version[:2]) >= MIN_PYTHON:
        return Result("python", PASS, v)
    return Result("python", FAIL, f"{v} is older than 3.11", "install Python 3.11 or newer and reinstall kiln with it")


def check_aiohttp(version: str | None) -> Result:
    if version:
        return Result("aiohttp", PASS, version)
    return Result("aiohttp", FAIL, "not importable", "reinstall kiln (uv tool install --reinstall git+https://github.com/jaedync/crucible-kiln)")


def check_config(path: str, error: str | None) -> Result:
    if error is None:
        return Result("config", PASS, path)
    return Result("config", FAIL, f"{path}: {error}",
                  "run `kiln init --crucible DIR --pack DIR`, or fix the file and run `kiln check`")


def check_systemd(code: int, out: str) -> Result:
    """`systemctl --user is-system-running`: any state but an error to connect means the user manager answers."""
    state = out.strip().splitlines()[-1] if out.strip() else ""
    if state in ("running", "degraded", "starting", "initializing", "maintenance"):
        return Result("systemd --user", PASS if state != "degraded" else WARN, state,
                      "see `systemctl --user --failed`")
    return Result("systemd --user", FAIL, out.strip()[-160:] or f"exit {code}",
                  "log in on a session with a user manager, or run `loginctl enable-linger $USER`")


def check_journal(code: int, out: str) -> Result:
    """`journalctl -n 1 --output=json _UID=<uid>`: Kiln reads the engine's lines this way."""
    if code != 0:
        return Result("journal", FAIL, out.strip()[-160:] or f"exit {code}",
                      "add your user to the systemd-journal group, or set Storage=persistent in journald.conf")
    if not out.strip():
        return Result("journal", WARN, "readable, but has no entries for this user",
                      "set Storage=persistent in /etc/systemd/journald.conf so user units log to the journal")
    return Result("journal", PASS, "the user journal is readable")


def check_amdgpu(gpu: Path | None) -> Result:
    if gpu is not None:
        return Result("amdgpu", PASS, str(gpu))
    return Result("amdgpu", WARN, "no amdgpu device with mem_info_gtt_total in /sys/class/drm",
                  "load the amdgpu driver; without it the dashboard shows no GPU memory or busy figures")


def kernel_version(release: str) -> tuple[int, int] | None:
    m = re.match(r"(\d+)\.(\d+)", release)
    return (int(m[1]), int(m[2])) if m else None


def check_kernel(release: str) -> Result:
    v = kernel_version(release)
    if v is not None and v >= CACHESTAT_KERNEL:
        return Result("kernel", PASS, release)
    return Result("kernel", WARN, f"{release} is older than 6.5 (no cachestat)",
                  "use Linux 6.5 or newer for the page-cache figures of the engine's weights")


def check_port(what: str, addr: tuple[str, int], free: bool, unit_active: bool | None, unit: str) -> Result:
    """A listen port must be free, or held by our own unit. None: systemd --user does not answer, so
    nobody can tell whether the unit holds the port."""
    name = f"{what} port {addr[1]}"
    if free:
        return Result(name, PASS, "free")
    if unit_active is None:
        return Result(name, WARN, f"in use; cannot tell if {unit} holds it, because systemd --user does not answer",
                      "see the systemd --user line")
    if unit_active:
        return Result(name, PASS, f"in use by {unit}")
    return Result(name, FAIL, f"in use, and {unit} is not running",
                  f"stop the other program on port {addr[1]}, or change [{what}] listen in kiln.toml")


def check_engine(profile: str | None, state: str, port: int | None, answers: bool | None,
                 unit_active: bool | None = None, unit: str = "") -> Result:
    """The active engine's port answers HTTP, from the engine's own unit. No active engine is a warning: Kiln
    works, but serves nothing. unit_active None: systemd did not say."""
    if profile is None or port is None:
        return Result("engine", WARN, f"no active engine (state {state})", "run `kiln switch <profile>`")
    if answers and unit_active is False:
        return Result("engine", FAIL, f"127.0.0.1:{port} answers, but {unit} is not active: another process holds"
                                      " the port",
                      f"stop the other process on port {port} (for example a `python -m server` started by hand"
                      f" from the Crucible guide), then run `kiln switch {profile}`")
    if answers:
        return Result("engine", PASS, f"{profile} answers on 127.0.0.1:{port}")
    return Result("engine", FAIL, f"{profile} ({state}) does not answer on 127.0.0.1:{port}",
                  f"see `journalctl --user -u kiln-{profile} -n 50`, then `kiln switch {profile}`")


def pack_dir(native: dict) -> str | None:
    """The checkpoint directory of a Crucible profile: the argument after `-m server` in its ExecStart, or
    None when the command is not that form (another engine, or an argument with a systemd specifier)."""
    import shlex
    start = native.get("ExecStart")
    if not isinstance(start, str):
        return None
    try:
        words = shlex.split(start)
    except ValueError:
        return None
    for i in range(len(words) - 2):
        if words[i] == "-m" and words[i + 1] == "server":
            raw = words[i + 2]
            if re.search(r"%[^%]|\$[^$]", raw.replace("%%", "").replace("$$", "")):
                return None  # a specifier or variable: only systemd knows its value
            return raw.replace("%%", "%").replace("$$", "$")
    return None


def check_pack(profile: str, pack: str, is_dir: bool, has_config: bool) -> Result:
    """The checkpoint directory that a Crucible profile loads holds config.json."""
    name = f"pack {profile}"
    if is_dir and has_config:
        return Result(name, PASS, pack)
    problem = "no config.json in it" if is_dir else "not a directory"
    return Result(name, FAIL, f"{pack}: {problem}",
                  "fix the pack path in the profile's ExecStart in kiln.toml, or run `kiln init --crucible DIR"
                  " --pack DIR --force` (it replaces kiln.toml) with the EXL3 checkpoint directory (the one that holds"
                  " config.json) as --pack")


def check_packs(cfg) -> list[Result]:
    out = []
    for p in cfg.profiles.values():
        pack = pack_dir(p.native) if p.engine == "crucible" else None
        if pack is not None:
            path = Path(pack)
            out.append(check_pack(p.name, pack, path.is_dir(), (path / "config.json").is_file()))
    return out


def check_engine_lines(unit: str | None, count: int | None, engine: str = "crucible") -> Result:
    if unit is None:
        return Result("engine log", WARN, "no active engine unit", "run `kiln switch <profile>`")
    if engine != "crucible":
        return Result("engine log", PASS, f"skipped: {unit} runs {engine}, which logs no crucible_* lines")
    if count:
        return Result("engine log", PASS, f"{count} crucible_* lines since {unit} last started")
    return Result("engine log", WARN, f"no crucible_* lines in the journal of {unit}",
                  "send one request; if none appear, check that the engine is the public Crucible server")


def count_engine_lines(journal_json: Iterable[str]) -> int:
    n = 0
    for raw in journal_json:
        try:
            msg = json.loads(raw).get("MESSAGE")
        except ValueError:
            continue
        if isinstance(msg, list):
            msg = bytes(msg).decode("utf-8", "replace")
        if isinstance(msg, str) and ENGINE_LINE.search(msg):
            n += 1
    return n


# -- facts (side effects, injected) --------------------------------------------------------------

Run = Callable[[list[str]], tuple[int, str]]


def run_cmd(cmd: list[str]) -> tuple[int, str]:
    import subprocess
    try:
        p = subprocess.run(cmd, capture_output=True, text=True, timeout=20)
    except (OSError, subprocess.TimeoutExpired) as e:
        return 127, str(e)
    return p.returncode, (p.stdout if p.returncode == 0 else p.stdout + p.stderr)


def port_free(host: str, port: int) -> bool:
    """True if this process could bind the port now (and it is released at once)."""
    from kiln.config import ANY_HOST
    family = socket.AF_INET6 if ":" in host else socket.AF_INET
    with socket.socket(family, socket.SOCK_STREAM) as s:
        s.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        try:
            s.bind(("" if host == ANY_HOST else host, port))
        except OSError:
            return False
    return True


def http_answers(port: int, timeout: float = 3.0) -> bool:
    import urllib.error
    import urllib.request
    try:
        with urllib.request.urlopen(f"http://127.0.0.1:{port}/v1/models", timeout=timeout):
            return True
    except urllib.error.HTTPError:
        return True  # any HTTP answer: the server is up
    except (OSError, ValueError):
        return False


def gather(config_path: str | None, *, run: Run = run_cmd, sys_root: Path = Path("/sys"),
           release: str | None = None, free: Callable[[str, int], bool] = port_free,
           answers: Callable[[int], bool] = http_answers, version: tuple[int, ...] = tuple(sys.version_info),
           uid: int | None = None) -> list[Result]:
    import os
    from kiln import config
    from kiln.collect.hw import Sensors
    out = [check_python(version)]
    try:
        import aiohttp
        out.append(check_aiohttp(aiohttp.__version__))
    except ImportError:
        out.append(check_aiohttp(None))
    path = str(config_path or config.default_path())
    try:
        cfg, err = config.load(path), None
    except (OSError, config.ConfigError, ValueError) as e:
        cfg, err = None, str(e)
    out.append(check_config(path, err))
    systemd = check_systemd(*run(["systemctl", "--user", "is-system-running"]))
    out.append(systemd)
    uid = os.getuid() if uid is None else uid
    out.append(check_journal(*run(["journalctl", "--no-pager", "-n", "1", "--output=json",
                                   f"_UID={cfg.container_uid if cfg else uid}"])))
    out.append(check_amdgpu(Sensors(root=sys_root)._gpu()))
    out.append(check_kernel(release if release is not None else os.uname().release))
    if cfg is None:
        return out
    out.extend(check_packs(cfg))
    reachable = systemd.status != FAIL  # without a user manager, is-active says "not running" for every unit
    for what, addr, unit in (("gateway", cfg.gateway_listen, "kiln-gateway"), ("dash", cfg.dash_listen, "kiln-dash")):
        is_free = free(addr[0], addr[1])
        active = (None if not reachable else
                  False if is_free else run(["systemctl", "--user", "is-active", "--quiet", unit])[0] == 0)
        out.append(check_port(what, addr, is_free, active, unit))
    from kiln.engines.state import StateFile
    st = StateFile(cfg.state_file).read()
    p = cfg.profiles.get(st.profile or "")
    unit_active = None
    if p and reachable:
        unit_active = run(["systemctl", "--user", "is-active", "--quiet", p.unit])[0] == 0
    out.append(check_engine(p.name if p else None, st.state, p.port if p else None,
                            answers(p.port) if p else None, unit_active, p.unit if p else ""))
    count = None
    if p:
        # Only the lines of the unit's current run: an old run's lines say nothing about this one.
        code, inv = run(["systemctl", "--user", "show", "-p", "InvocationID", "--value", p.unit])
        inv = inv.strip() if code == 0 and re.fullmatch(r"[0-9a-f]{32}", inv.strip()) else ""
        match = [f"_SYSTEMD_INVOCATION_ID={inv}"] if inv else ["-u", p.unit]
        code, text = run(["journalctl", "--user", "--no-pager", "--output=json", "-n", str(JOURNAL_LINES), *match])
        count = count_engine_lines(text.splitlines()) if code == 0 else 0
    out.append(check_engine_lines(p.unit if p else None, count, p.engine if p else "crucible"))
    return out


def main(config_path: str | None, print_=print, **kw) -> int:
    results = gather(config_path, **kw)
    for r in results:
        print_(r.line())
    return 1 if any(r.status == FAIL for r in results) else 0
