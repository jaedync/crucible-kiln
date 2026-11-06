import asyncio
import time

import pytest

from kiln.engines.manager import Manager, SwitchRefused, render_unit
from kiln.engines.state import FAILED, READY, StateFile



class FakeSystem:
    """Records commands and answers like systemctl/podman would."""

    def __init__(self, ps="", evals="", failed=False, unit=None, log=""):
        self.calls, self.ps, self.evals, self.failed, self.log = [], ps, evals, failed, log
        # `systemctl show` of an engine unit: running, unless the test says otherwise.
        self.unit = unit or ({"ActiveState": "failed", "SubState": "failed", "Result": "exit-code"} if failed
                             else {"ActiveState": "active", "SubState": "running", "Result": "success"})

    async def __call__(self, *cmd, timeout=60):
        self.calls.append(cmd)
        if cmd[:3] == ("systemctl", "--user", "list-units"):
            return 0, self.evals
        if cmd[0] == "podman":
            return 0, self.ps
        if cmd[:3] == ("systemctl", "--user", "show") and "-P" not in cmd:
            fields = {"NRestarts": "0", "Job": "", **self.unit}
            return 0, "\n".join(f"{k}={v}" for k, v in fields.items())
        if cmd[0] == "journalctl":
            return 0, self.log
        if "is-failed" in cmd:
            return (0, "") if self.failed else (1, "")
        return 0, ""


@pytest.fixture
def mk(make_cfg, tmp_path):
    def build(system=None, probe_ok=True, gtt=None, which=lambda name: f"/usr/bin/{name}", **overrides):
        cfg = make_cfg(**{"engines.quadlet_dir": str(tmp_path / "quadlet"), "engines.busy_units": ["bench-*"],
                          **overrides})
        events = []

        async def on_event(kind, profile, detail):
            events.append((kind, profile, detail))

        async def no_sleep(_):
            pass

        t = [0.0]

        def clock():
            t[0] += 1
            return t[0]
        m = Manager(cfg, StateFile(cfg.state_file), on_event=on_event, gtt_used=gtt or (lambda: 0),
                    runner=system or FakeSystem(), clock=clock, sleep=no_sleep, which=which,
                    port_in_use=lambda port: False)

        async def probe(p):
            return probe_ok, "HTTP 200" if probe_ok else "ConnectionRefused"
        m.probe = probe
        return m, cfg, events
    return build


async def test_switch_stops_waits_starts_and_probes(mk):
    system = FakeSystem()
    m, cfg, events = mk(system)
    StateFile(cfg.state_file).set("halogen-flashnext", READY)
    assert await m.switch("gufo-qwen27")
    st = StateFile(cfg.state_file).read()
    assert (st.profile, st.state) == ("gufo-qwen27", READY)
    cmds = [" ".join(c) for c in system.calls]
    stop = cmds.index("systemctl --user stop kiln-halogen-flashnext.service")
    reload_ = cmds.index("systemctl --user daemon-reload")
    start = cmds.index("systemctl --user start --no-block kiln-gufo-qwen27.service")
    assert stop < reload_ < start
    assert [e[0] for e in events] == ["stopping", "starting", "ready"]
    unit = (cfg.quadlet_dir / "kiln-gufo-qwen27.container").read_text()
    assert "WantedBy=default.target" in unit
    assert "[Install]" not in (cfg.quadlet_dir / "kiln-halogen-flashnext.container").read_text()


async def test_switch_fails_honestly_when_the_unit_fails(mk):
    m, cfg, events = mk(FakeSystem(failed=True), probe_ok=False)
    assert not await m.switch("gufo-qwen27")
    st = StateFile(cfg.state_file).read()
    assert st.state == FAILED and "failed" in st.detail


async def test_switch_waits_for_gpu_memory_and_gives_up(mk):
    m, cfg, _ = mk(gtt=lambda: 50 * 2**30)
    assert not await m.switch("gufo-qwen27")
    assert "GPU memory still in use" in StateFile(cfg.state_file).read().detail


@pytest.mark.parametrize("system,reason", [
    (FakeSystem(evals="bench-hard.service loaded active running x"), "evaluation running (bench-hard)"),
    (FakeSystem(ps="eval-gufo-qwen27-hard\n"), "another container is running (eval-gufo-qwen27-hard)"),
    (FakeSystem(ps="halogen\nkiln-halogen-flashnext\n"), "another container is running (halogen)"),
])
async def test_switch_is_refused_while_something_else_holds_the_gpu(mk, system, reason):
    m, _, _ = mk(system)
    with pytest.raises(SwitchRefused, match=reason.replace("(", r"\(").replace(")", r"\)")):
        await m.request_switch("gufo-qwen27")


async def test_one_switch_at_a_time(mk):
    m, cfg, _ = mk()
    await m.request_switch("gufo-qwen27")
    with pytest.raises(SwitchRefused, match="already in progress"):
        await m.request_switch("halogen-flashnext")
    await asyncio.wait_for(m.task, 5)
    with pytest.raises(SwitchRefused, match="unknown profile"):
        await m.request_switch("nope")


async def test_probe_windows_label_health_checks(mk):
    m, _, _ = mk()
    m.probes = [("kiln-x", 10.0, 20.0)]
    assert m.is_probe("kiln-x", 15.0) and not m.is_probe("kiln-x", 25.0) and not m.is_probe("kiln-y", 15.0)


async def test_stop_is_allowed_during_evaluations_and_leaves_nothing_serving(mk):
    system = FakeSystem(evals="bench-ar.service loaded active running x")
    m, cfg, events = mk(system)
    StateFile(cfg.state_file).set("halogen-flashnext", READY)
    await m.stop("evaluation needs the GPU")
    st = StateFile(cfg.state_file).read()
    assert (st.profile, st.state, st.detail) == (None, "stopped", "evaluation needs the GPU")
    assert ("systemctl", "--user", "stop", "kiln-halogen-flashnext.service") in system.calls


async def test_eval_lock_blocks_everyone_but_its_holder(mk):
    from kiln.engines.lock import EvalLock
    m, cfg, _ = mk()
    token = EvalLock(cfg.lock_file).acquire("night", until=1e12)
    with pytest.raises(SwitchRefused, match="evaluation night running"):
        await m.request_switch("gufo-qwen27")
    await m.request_switch("gufo-qwen27", token)
    await asyncio.wait_for(m.task, 5)
    EvalLock(cfg.lock_file).release(token)
    assert await m.blocked() is None


def test_expired_lock_is_ignored(tmp_path):
    from kiln.engines.lock import EvalLock
    lock = EvalLock(tmp_path / "eval.lock", clock=lambda: 100.0)
    lock.acquire("x", until=50.0)
    assert lock.read() is None and lock.allows(None)
    assert oct((tmp_path / "eval.lock").stat().st_mode)[-3:] == "600"


def test_native_profile_renders_a_plain_service(make_cfg, tmp_path):
    from kiln.config import ConfigError
    from kiln.engines.manager import unit_file
    cfg = make_cfg(**{"profiles.gufo-ar": {
        "engine": "gufo", "model": "qwen3.8-flash-next", "served_id": "Qwen3.8 Flash Next", "port": 18082,
        "context": 65536, "kv": {"positions": 65536},
        "native": {"ExecStart": "/opt/gufo serve --port 18082", "Environment": "PATH=/usr/bin",
                   "LimitMEMLOCK": "infinity"}}})
    p = cfg.profiles["gufo-ar"]
    unit = render_unit(cfg, p, active=False)
    assert "ExecStart=/opt/gufo serve --port 18082" in unit and "LimitMEMLOCK=infinity" in unit
    assert "[Container]" not in unit and "Conflicts=kiln-halogen-flashnext.service" in unit
    assert unit_file(cfg, p).name == "kiln-gufo-ar.service"
    assert "kiln-gufo-ar.service" in render_unit(cfg, cfg.profiles["halogen-flashnext"], True)
    with pytest.raises(ConfigError, match="exactly one"):
        make_cfg(**{"profiles.bad": {"engine": "gufo", "model": "m", "served_id": "m", "port": 1,
                                     "context": 1, "kv": {"positions": 1}}})


async def test_stop_during_an_evaluation_needs_its_token_or_force(mk):
    from kiln.engines.lock import EvalLock
    m, cfg, _ = mk()
    StateFile(cfg.state_file).set("halogen-flashnext", READY)
    token = EvalLock(cfg.lock_file).acquire("night", until=1e12)
    with pytest.raises(SwitchRefused, match="stop with force"):
        await m.stop("nope")
    await m.stop("eval needs the GPU", token=token)
    assert StateFile(cfg.state_file).read().state == "stopped"
    await m.stop("owner emergency stop", force=True)


async def test_concurrent_switch_requests_admit_one(mk):
    m, cfg, _ = mk()
    results = await asyncio.gather(m.request_switch("gufo-qwen27"), m.request_switch("halogen-flashnext"),
                                   return_exceptions=True)
    assert sum(isinstance(r, SwitchRefused) for r in results) == 1
    await asyncio.wait_for(m.task, 5)


def test_lock_cannot_be_taken_twice(tmp_path):
    from kiln.engines.lock import EvalLock
    a, b = EvalLock(tmp_path / "eval.lock"), EvalLock(tmp_path / "eval.lock")
    a.acquire("one", until=1e12)
    with pytest.raises(RuntimeError, match="already held"):
        b.acquire("two", until=1e12)


# -- the rootless podman user namespace after a reboot ---------------------------------------
USERNS_FAIL = ('time="2026-01-15T14:40:01" level=error msg="running `/usr/bin/newuidmap 4242 0 1000 1 1 100000'
               ' 65536`: newuidmap: Could not set caps\\n"\nError: cannot set up namespace using'
               ' "/usr/bin/newuidmap": exit status 1')


class NoUserns(FakeSystem):
    """podman fails like the first call after a boot under NoNewPrivileges, until a podman outside the
    dashboard's restrictions (systemd-run) has made the user namespace."""

    def __init__(self, heals=True, **kw):
        super().__init__(**kw)
        self.heals, self.userns = heals, False

    async def __call__(self, *cmd, timeout=60):
        if cmd[0] == "systemd-run":
            self.calls.append(cmd)
            self.userns = self.heals
            return (0, "") if self.heals else (1, "Failed to start transient service unit")
        if cmd[0] == "podman" and not self.userns:
            self.calls.append(cmd)
            return 125, USERNS_FAIL
        return await super().__call__(*cmd, timeout=timeout)


async def test_a_missing_user_namespace_is_made_once_outside_the_dashboard_then_podman_retried(mk):
    system = NoUserns(ps="")
    m, _, _ = mk(system)
    assert await m.blocked() is None
    made = [c for c in system.calls if c[0] == "systemd-run"]
    assert made == [("systemd-run", "--user", "--wait", "--quiet", "--collect", "podman", "unshare", "true")]
    assert [c[0] for c in system.calls if c[0] in ("podman", "systemd-run")] == ["podman", "systemd-run", "podman"]
    assert await m.blocked() is None  # the namespace stays: no second systemd-run
    assert len([c for c in system.calls if c[0] == "systemd-run"]) == 1


async def test_when_the_namespace_cannot_be_made_the_switch_is_refused_and_retried_only_later(mk):
    system = NoUserns(heals=False)
    m, _, _ = mk(system)
    reason = await m.blocked()
    assert reason.startswith("podman ps failed") and "user namespace" in reason
    await m.blocked()  # within USERNS_RETRY_S: no new systemd-run every 5 s from the blocked loop
    assert len([c for c in system.calls if c[0] == "systemd-run"]) == 1


async def test_other_podman_failures_do_not_start_systemd_run(mk):
    class Broken(FakeSystem):
        async def __call__(self, *cmd, timeout=60):
            self.calls.append(cmd)
            return (125, "Error: database is locked") if cmd[0] == "podman" else (0, "")
    system = Broken()
    m, _, _ = mk(system)
    assert (await m.blocked()).startswith("podman ps failed: Error: database is locked")
    assert not [c for c in system.calls if c[0] == "systemd-run"]


class Flaky(NoUserns):
    """The namespace is lost again after each repair (a pathological host), or the repair keeps failing."""

    async def __call__(self, *cmd, timeout=60):
        out = await super().__call__(*cmd, timeout=timeout)
        if cmd[0] == "podman" and self.userns:
            self.userns = False  # gone again before the next check
        return out


def repairs(system):
    return len([c for c in system.calls if c[0] == "systemd-run"])


async def test_a_failed_repair_runs_again_once_300_s_have_passed(mk):
    system = NoUserns(heals=False)
    m, _, _ = mk(system)
    mono = [1000.0]
    m.monotonic = lambda: mono[0]
    await m.blocked()
    mono[0] += 299
    await m.blocked()
    assert repairs(system) == 1
    mono[0] += 2
    await m.blocked()
    assert repairs(system) == 2


async def test_after_a_successful_repair_the_next_one_waits_at_least_60_s(mk):
    system = Flaky()
    m, _, _ = mk(system)
    mono = [1000.0]
    m.monotonic = lambda: mono[0]
    assert await m.blocked() is None  # repaired and podman ps answered
    for _ in range(11):  # the blocked loop: every 5 s for 55 s
        mono[0] += 5
        assert "podman ps failed" in await m.blocked()
    assert repairs(system) == 1
    mono[0] += 6  # 61 s after the repair: the short interval, not the 300 s of a failed repair
    assert await m.blocked() is None
    assert repairs(system) == 2


async def test_the_gate_uses_monotonic_time_not_the_wall_clock(mk):
    system = NoUserns(heals=False)
    m, _, _ = mk(system)
    mono = [1000.0]
    m.monotonic = lambda: mono[0]
    wall = [2_000_000_000.0]
    m.clock = lambda: wall[0]
    await m.blocked()
    wall[0] -= 3600  # the wall clock steps back an hour (NTP after a boot)
    mono[0] += 301
    await m.blocked()
    assert repairs(system) == 2


class Recorder(StateFile):
    """A state file that keeps every state written, so a test can see the steps of a switch."""

    def __init__(self, path):
        super().__init__(path)
        self.written = []

    def write(self, st):
        super().write(st)
        self.written.append(st)


def steps(rec):
    return [(s.state, s.phase) for s in rec.written]


async def test_a_switch_reports_its_steps_stop_drain_load_test_beside_the_detail(mk):
    m, cfg, _ = mk()
    m.state = rec = Recorder(cfg.state_file)
    rec.set("halogen-flashnext", READY)
    answers = iter([(False, "ClientConnectorError"), (False, "HTTP 503"), (False, "HTTP 500"), (True, "HTTP 200")])

    async def probe(p):
        return next(answers)
    m.probe = probe
    assert await m.switch("gufo-qwen27")
    assert steps(rec)[1:] == [("stopping", "stop"), ("stopping", "drain"), ("starting", "load"),
                              ("starting", "test"), ("ready", None)]
    assert all(s.detail for s in rec.written[1:])  # the free text stays as it was
    for s in rec.written[1:-1]:
        assert s.phase_since is not None
    assert rec.written[3].phase_since > rec.written[2].phase_since
    final = StateFile(cfg.state_file).read()
    assert (final.phase, final.phase_since) == (None, None)


async def test_a_failed_switch_ends_its_steps(mk):
    m, cfg, _ = mk(FakeSystem(failed=True), probe_ok=False)
    m.state = rec = Recorder(cfg.state_file)
    assert not await m.switch("gufo-qwen27")
    assert steps(rec)[-2:] == [("starting", "load"), ("failed", None)]


async def test_a_plain_stop_is_one_step(mk):
    m, cfg, _ = mk()
    m.state = rec = Recorder(cfg.state_file)
    rec.set("halogen-flashnext", READY)
    await m.stop("owner")
    assert steps(rec)[1:3] == [("stopping", "stop"), ("stopped", None)]


async def test_a_step_never_marks_a_state_that_is_not_changing(mk):
    m, cfg, _ = mk()
    m.state = rec = Recorder(cfg.state_file)
    rec.set(None, "stopped")
    m._phase("drain")
    assert steps(rec) == [("stopped", None)]


def test_the_state_file_keeps_the_step_and_ignores_an_unknown_one(tmp_path):
    f = StateFile(tmp_path / "active.json")
    f.set("gufo-qwen27", "starting", None, "starting kiln-gufo-qwen27", now=50.0, phase="load")
    st = StateFile(f.path).read()
    assert (st.phase, st.phase_since, st.detail) == ("load", 50.0, "starting kiln-gufo-qwen27")
    f.path.write_text('{"v": 1, "profile": "x", "state": "starting", "since": 1, "phase": "reboot", "phase_since": 2}')
    st = StateFile(f.path).read()
    assert (st.phase, st.phase_since) == (None, None)
    f.path.write_text('{"v": 1, "profile": "x", "state": "ready", "since": 1}')  # a file from before steps
    assert StateFile(f.path).read().phase is None


async def test_a_new_drain_report_keeps_the_drain_clock(mk):
    # The fleet tells again each time its leases change; the panel's drain clock counts from the first.
    m, cfg, _ = mk()
    m.state = rec = Recorder(cfg.state_file)
    rec.set(None, "stopped")
    t = [1000.0]
    m.clock = lambda: t[0]
    await m._set(None, "starting", "gufo-qwen27", "waiting for a, b", phase="drain")
    t[0] = 1030.0
    await m._set(None, "starting", "gufo-qwen27", "waiting for b", phase="drain")
    assert rec.read().phase_since == 1000.0
    assert rec.read().detail == "waiting for b"
    await m._set("gufo-qwen27", "starting", None, "starting kiln-gufo-qwen27", phase="load")
    assert rec.read().phase_since == 1030.0


async def test_without_busy_units_no_unit_list_is_asked_for(mk):
    system = FakeSystem(evals="bench-hard.service loaded active running x")
    m, _, _ = mk(system, **{"engines.busy_units": []})
    assert await m.blocked() is None
    assert not any(c[:3] == ("systemctl", "--user", "list-units") for c in system.calls)


async def test_busy_units_are_passed_as_globs(mk):
    system = FakeSystem()
    m, _, _ = mk(system, **{"engines.busy_units": ["bench-*", "train-*.service"]})
    await m.blocked()
    (ask,) = [c for c in system.calls if c[:3] == ("systemctl", "--user", "list-units")]
    assert ask[-2:] == ("bench-*", "train-*.service")


def native_only(make_cfg, tmp_path):
    return {"profiles": {"crucible": {"engine": "crucible", "model": "m", "port": 18790, "context": 4096,
                                      "native": {"ExecStart": "/usr/bin/python3 -m server /p"}}},
            "engines.user_unit_dir": str(tmp_path / "units")}


async def test_a_native_only_setup_without_podman_is_not_blocked(mk, make_cfg, tmp_path):
    system = FakeSystem(ps="some-web-app\n")
    m, cfg, _ = mk(system, which=lambda name: None, **native_only(make_cfg, tmp_path))
    assert await m.blocked() is None
    assert not any(c[0] == "podman" for c in system.calls)  # nor is a container of another kind an engine
    await m.request_switch("crucible")
    assert await asyncio.wait_for(m.task, 5)
    assert (tmp_path / "units" / "kiln-crucible.service").exists()
    assert not (tmp_path / "quadlet").exists()


async def test_a_container_profile_without_podman_names_the_cause(mk):
    system = FakeSystem()
    m, _, _ = mk(system, which=lambda name: None)
    assert await m.blocked() == "podman is not installed, and a container profile needs it"
    assert not any(c[0] == "podman" for c in system.calls)


def two_native(make_cfg, tmp_path):
    native = {"engine": "crucible", "context": 4096, "served_id": "crucible"}
    return {"profiles": {"crucible": {**native, "model": "m1", "port": 18790,
                                      "native": {"ExecStart": "/usr/bin/python3 -m server /p1"}},
                         "crucible-b": {**native, "model": "m2", "port": 18791,
                                        "native": {"ExecStart": "/usr/bin/python3 -m server /p2"}}},
            "engines.user_unit_dir": str(tmp_path / "units")}


async def test_the_active_native_engine_is_enabled_so_it_starts_at_boot(mk, make_cfg, tmp_path):
    """A Quadlet unit with [Install] boots by itself (the generator links it); a plain user unit needs
    `systemctl enable`. Only the active engine may boot, and none after a stop."""
    system = FakeSystem()
    m, cfg, _ = mk(system, which=lambda name: None, **two_native(make_cfg, tmp_path))
    assert await m.switch("crucible")
    cmds = [" ".join(c) for c in system.calls]
    enable = cmds.index("systemctl --user enable kiln-crucible.service")
    assert cmds.index("systemctl --user daemon-reload") < enable
    assert enable < cmds.index("systemctl --user start --no-block kiln-crucible.service")
    assert "systemctl --user disable kiln-crucible-b.service" in cmds

    system.calls.clear()
    assert await m.switch("crucible-b")
    cmds = [" ".join(c) for c in system.calls]
    assert "systemctl --user enable kiln-crucible-b.service" in cmds
    assert "systemctl --user disable kiln-crucible.service" in cmds

    system.calls.clear()
    await m.stop("test")
    cmds = [" ".join(c) for c in system.calls]
    assert "systemctl --user disable kiln-crucible.service kiln-crucible-b.service" in cmds
    assert not any(c.startswith("systemctl --user enable") for c in cmds)


async def test_a_failed_enable_fails_the_switch(mk, make_cfg, tmp_path):
    class NoEnable(FakeSystem):
        async def __call__(self, *cmd, timeout=60):
            if "enable" in cmd:
                self.calls.append(cmd)
                return 1, "Failed to enable unit: Access denied"
            return await super().__call__(*cmd, timeout=timeout)
    m, cfg, _ = mk(NoEnable(), which=lambda name: None, **two_native(make_cfg, tmp_path))
    assert not await m.switch("crucible")
    st = StateFile(cfg.state_file).read()
    assert st.state == FAILED and "enable" in st.detail


class Booted(FakeSystem):
    """The engine unit is active and entered that state at `started` (systemctl show, unix time)."""

    def __init__(self, started):
        super().__init__()
        self.started = started

    async def __call__(self, *cmd, timeout=60):
        if cmd[2:3] == ("show",):
            self.calls.append(cmd)
            return 0, f"@{self.started}"
        return await super().__call__(*cmd, timeout=timeout)


async def test_an_engine_that_restarted_since_its_ready_record_is_tested_again(mk, make_cfg, tmp_path):
    """After a reboot the enabled engine starts again and loads its weights; the old ready record must not
    send requests to it until it answers a test generation."""
    system = Booted(started=2000)
    m, cfg, events = mk(system, which=lambda name: None, **two_native(make_cfg, tmp_path))
    StateFile(cfg.state_file).set("crucible", READY, now=1000.0)
    st = await m.reconcile()
    assert st.state == "starting" and st.profile == "crucible" and "restarted" in st.detail
    assert ("systemctl", "--user", "show", "-P", "ActiveEnterTimestamp", "--timestamp=unix",
            "kiln-crucible.service") in system.calls
    await asyncio.wait_for(m.task, 5)
    assert StateFile(cfg.state_file).read().state == READY


@pytest.mark.parametrize("answer", ["@900", "", "n/a"])
async def test_an_engine_that_ran_through_a_dash_restart_stays_ready(mk, make_cfg, tmp_path, answer):
    class Show(Booted):
        async def __call__(self, *cmd, timeout=60):
            if cmd[2:3] == ("show",):
                return 0, answer
            return await super().__call__(*cmd, timeout=timeout)
    m, cfg, _ = mk(Show(0), which=lambda name: None, **two_native(make_cfg, tmp_path))
    StateFile(cfg.state_file).set("crucible", READY, now=1000.0)
    st = await m.reconcile()
    assert st.state == READY and m.task is None


# -- a unit that does not run (newcomer test H3, M3, M4) ------------------------------------------------

CRASH_LOOP = {"ActiveState": "activating", "SubState": "auto-restart", "Result": "exit-code", "NRestarts": "1"}


async def test_switch_fails_soon_when_the_unit_keeps_restarting(mk):
    system = FakeSystem(unit=CRASH_LOOP, log="starting\n/home/user/crucible/.venv/bin/python: No module named server")
    m, cfg, _ = mk(system, probe_ok=False)
    assert not await m.switch("gufo-qwen27")
    st = StateFile(cfg.state_file).read()
    assert (st.profile, st.state) == ("gufo-qwen27", FAILED)
    assert "No module named server" in st.detail and "restart" in st.detail
    assert sum(1 for c in system.calls if c[:3] == ("systemctl", "--user", "show")) <= 2  # at the first poll
    # The unit must not go on restarting every 10 s: Kiln stops it.
    start = system.calls.index(("systemctl", "--user", "start", "--no-block", "kiln-gufo-qwen27.service"))
    assert ("systemctl", "--user", "stop", "kiln-gufo-qwen27.service") in system.calls[start:]
    await m.request_switch("gufo-qwen27")  # nothing is in progress any more
    await asyncio.wait_for(m.task, 5)


async def test_switch_fails_when_the_unit_exits_during_the_start(mk):
    system = FakeSystem(unit={"ActiveState": "inactive", "SubState": "dead", "Result": "success"})
    m, cfg, _ = mk(system, probe_ok=False)
    assert not await m.switch("gufo-qwen27")
    st = StateFile(cfg.state_file).read()
    assert st.state == FAILED and "kiln-gufo-qwen27.service exited" in st.detail


async def test_a_pending_start_job_is_no_exit(mk):
    calls = []

    class Pending(FakeSystem):
        async def __call__(self, *cmd, timeout=60):
            if cmd[:3] == ("systemctl", "--user", "show"):
                calls.append(cmd)
                if len(calls) == 1:  # the start job is queued, the unit has not run yet
                    return 0, "ActiveState=inactive\nSubState=dead\nResult=success\nNRestarts=0\nJob=42"
            return await super().__call__(*cmd, timeout=timeout)
    m, cfg, _ = mk(Pending())
    assert await m.switch("gufo-qwen27")


async def test_switch_is_not_ready_while_the_unit_is_not_active(mk):
    # A foreign server on the port answers the test generation, but the engine's own unit failed.
    system = FakeSystem(unit={"ActiveState": "failed", "SubState": "failed", "Result": "exit-code"},
                        log="OSError: [Errno 98] address already in use")
    m, cfg, _ = mk(system, probe_ok=True)
    assert not await m.switch("gufo-qwen27")
    st = StateFile(cfg.state_file).read()
    assert st.state == FAILED and "address already in use" in st.detail


async def test_switch_refuses_to_start_on_a_port_another_process_holds(mk):
    system = FakeSystem()
    m, cfg, _ = mk(system)
    m.port_in_use = lambda port: True
    StateFile(cfg.state_file).set("halogen-flashnext", READY)
    assert not await m.switch("gufo-qwen27")
    st = StateFile(cfg.state_file).read()
    port = cfg.profiles["gufo-qwen27"].port
    assert st.state == FAILED and st.profile is None
    assert f"port {port} is in use by another process" in st.detail and "python -m server" in st.detail
    assert not any(c[:3] == ("systemctl", "--user", "start") for c in system.calls)


async def test_stop_during_a_switch_cancels_it_and_stops_the_unit(mk):
    system = FakeSystem(unit={"ActiveState": "activating", "SubState": "start", "Result": "success"})
    m, cfg, events = mk(system, probe_ok=False)

    async def yield_(_):
        await asyncio.sleep(0)
    m.sleep = yield_
    await m.request_switch("gufo-qwen27")
    for _ in range(20):
        await asyncio.sleep(0)
    assert StateFile(cfg.state_file).read().state == "starting"
    await m.stop("stopped from the command line")
    assert m.task.done()
    st = StateFile(cfg.state_file).read()
    assert (st.profile, st.state) == (None, "stopped")
    assert "switch to gufo-qwen27 cancelled" in st.detail
    assert ("systemctl", "--user", "stop", "kiln-gufo-qwen27.service") in system.calls
    await m.request_switch("gufo-qwen27")  # a new switch is allowed again


@pytest.mark.parametrize("unit,state", [
    ({"ActiveState": "inactive", "SubState": "dead", "Result": "success"}, FAILED),
    ({"ActiveState": "failed", "SubState": "failed", "Result": "exit-code"}, FAILED),
    ({"ActiveState": "active", "SubState": "running", "Result": "success"}, READY),
    ({"ActiveState": "deactivating", "SubState": "stop-sigterm", "Result": "success"}, READY),  # next round
])
async def test_watch_ends_ready_when_the_unit_is_not_active(mk, unit, state):
    m, cfg, _ = mk(FakeSystem(unit=unit, log="bye"))
    StateFile(cfg.state_file).set("gufo-qwen27", READY)
    await m.watch()
    st = StateFile(cfg.state_file).read()
    assert st.state == state
    if state == FAILED:
        assert st.profile == "gufo-qwen27" and "kiln-gufo-qwen27.service is not running" in st.detail


async def test_watch_trusts_no_failed_query_and_skips_a_running_switch(mk):
    class Broken(FakeSystem):
        async def __call__(self, *cmd, timeout=60):
            if cmd[:3] == ("systemctl", "--user", "show"):
                return 1, "Failed to connect to bus"
            return await super().__call__(*cmd, timeout=timeout)
    m, cfg, _ = mk(Broken())
    StateFile(cfg.state_file).set("gufo-qwen27", READY)
    await m.watch()
    assert StateFile(cfg.state_file).read().state == READY
    m2, cfg2, _ = mk(FakeSystem(unit={"ActiveState": "inactive", "SubState": "dead", "Result": "success"}))
    StateFile(cfg2.state_file).set("gufo-qwen27", READY)
    m2.task = asyncio.get_running_loop().create_future()  # a switch in progress
    await m2.watch()
    assert StateFile(cfg2.state_file).read().state == READY
    m2.task.cancel()


async def test_a_target_unit_that_runs_outside_kiln_is_restarted_not_called_foreign(mk):
    # `systemctl --user start kiln-gufo-qwen27` by hand while the record says stopped: the port is the unit's own.
    system = FakeSystem()
    m, cfg, _ = mk(system)
    stopped = []

    def in_use(port):
        return not stopped
    m.port_in_use = in_use
    real = system.__call__

    async def runner(*cmd, timeout=60):
        if cmd[:3] == ("systemctl", "--user", "stop") and cmd[-1] == "kiln-gufo-qwen27.service":
            stopped.append(cmd)
        return await real(*cmd, timeout=timeout)
    m.run = runner
    assert await m.switch("gufo-qwen27")
    assert stopped and StateFile(cfg.state_file).read().state == READY


# -- delta review M1, M2, M3, L6 ----------------------------------------------------------------------------
async def test_watch_does_not_write_failed_over_a_stop_that_ran_during_its_journal_read(mk):
    """watch() reads the journal after its last check. A stop that ran meanwhile must keep its record."""
    gate = asyncio.Event()

    class SlowJournal(FakeSystem):
        async def __call__(self, *cmd, timeout=60):
            if cmd[0] == "journalctl":
                await gate.wait()
            return await super().__call__(*cmd, timeout=timeout)
    m, cfg, _ = mk(SlowJournal(unit={"ActiveState": "inactive", "SubState": "dead", "Result": "success"}, log="bye"))
    StateFile(cfg.state_file).set("gufo-qwen27", READY)
    watch = asyncio.create_task(m.watch())
    for _ in range(20):
        await asyncio.sleep(0)
    await m.stop("stopped from the command line")
    gate.set()
    await watch
    st = StateFile(cfg.state_file).read()
    assert (st.profile, st.state) == (None, "stopped")


def _collect_transports():
    """No subprocess transport stays open after the cancel: an open one warns when the loop is gone."""
    import gc
    from asyncio.base_subprocess import BaseSubprocessTransport
    gc.collect()
    left = [t for t in gc.get_objects() if isinstance(t, BaseSubprocessTransport) and not t.is_closing()]
    assert left == []


async def test_a_cancelled_run_waits_for_its_child_so_a_late_start_cannot_follow_a_stop(tmp_path):
    from kiln.engines.manager import run
    mark = tmp_path / "started"
    task = asyncio.create_task(run("sh", "-c", f"sleep 0.3; touch {mark}"))
    await asyncio.sleep(0.05)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert mark.exists()  # the child finished before the cancellation went on
    _collect_transports()


async def test_a_cancelled_run_kills_a_child_that_does_not_end(tmp_path, monkeypatch):
    from kiln.engines import manager
    monkeypatch.setattr(manager, "CANCEL_WAIT_S", 0.2)
    mark = tmp_path / "started"
    task = asyncio.create_task(manager.run("sh", "-c", f"sleep 3; touch {mark}"))
    await asyncio.sleep(0.05)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await asyncio.wait_for(task, 2)
    await asyncio.sleep(0.1)
    assert not mark.exists()
    _collect_transports()


class Sequence(FakeSystem):
    """`systemctl show` answers from a list; the last answer repeats."""

    def __init__(self, units, **kw):
        super().__init__(**kw)
        self.units = list(units)

    async def __call__(self, *cmd, timeout=60):
        if cmd[:3] == ("systemctl", "--user", "show") and "-P" not in cmd:
            self.unit = self.units.pop(0) if len(self.units) > 1 else self.units[0]
        return await super().__call__(*cmd, timeout=timeout)


ACTIVE = {"ActiveState": "active", "SubState": "running", "Result": "success"}


def crashed(n, job=""):
    return {"ActiveState": "activating", "SubState": "auto-restart", "Result": "exit-code", "NRestarts": str(n),
            "Job": job}


async def test_a_crash_that_systemd_repairs_becomes_ready_again_with_no_user_action(mk):
    system = Sequence([crashed(1), crashed(1), {"ActiveState": "activating", "SubState": "start",
                                                "Result": "success", "NRestarts": "2", "Job": "7 start"}, ACTIVE],
                      log="Segmentation fault")
    m, cfg, events = mk(system)
    StateFile(cfg.state_file).set("gufo-qwen27", READY)
    await m.watch()
    st = StateFile(cfg.state_file).read()
    assert (st.profile, st.state) == ("gufo-qwen27", "starting")
    assert "systemd restarts it" in st.detail
    await asyncio.wait_for(m.task, 5)
    st = StateFile(cfg.state_file).read()
    assert (st.profile, st.state) == ("gufo-qwen27", READY)
    assert not any(c[:3] == ("systemctl", "--user", "stop") for c in system.calls)


async def test_a_crash_loop_after_a_ready_engine_ends_in_failed(mk):
    system = Sequence([crashed(1), crashed(2), crashed(3), crashed(4), crashed(5)], log="Segmentation fault")
    m, cfg, _ = mk(system, probe_ok=False)
    StateFile(cfg.state_file).set("gufo-qwen27", READY)
    await m.watch()
    await asyncio.wait_for(m.task, 5)
    st = StateFile(cfg.state_file).read()
    assert (st.profile, st.state) == ("gufo-qwen27", FAILED)
    assert "Segmentation fault" in st.detail and "kiln switch gufo-qwen27" in st.detail
    assert ("systemctl", "--user", "stop", "kiln-gufo-qwen27.service") in system.calls


async def test_a_stop_during_a_crash_repair_wins(mk):
    system = Sequence([crashed(1)])
    m, cfg, _ = mk(system, probe_ok=False)

    async def yield_(_):
        await asyncio.sleep(0)
    m.sleep = yield_
    StateFile(cfg.state_file).set("gufo-qwen27", READY)
    await m.watch()
    for _ in range(10):
        await asyncio.sleep(0)
    await m.stop("stopped from the command line")
    assert m.task.done()
    assert StateFile(cfg.state_file).read().state == "stopped"
    await m.watch()  # stopped: nothing to watch
    assert StateFile(cfg.state_file).read().state == "stopped"


async def test_a_switch_is_refused_while_systemd_repairs_a_crash(mk):
    m, cfg, _ = mk(Sequence([crashed(1)]), probe_ok=False)

    async def yield_(_):
        await asyncio.sleep(0)
    m.sleep = yield_
    StateFile(cfg.state_file).set("gufo-qwen27", READY)
    await m.watch()
    with pytest.raises(SwitchRefused, match="restarts after a crash"):
        await m.request_switch("halogen-flashnext")
    await m.stop("test end")


def test_a_queued_start_job_is_not_down_even_when_the_unit_failed_before():
    from kiln.engines.manager import unit_down
    assert unit_down("u", {"ActiveState": "failed", "SubState": "failed", "Result": "exit-code",
                           "Job": "42 start"}) is None
    assert unit_down("u", {"ActiveState": "failed", "SubState": "failed", "Result": "exit-code", "Job": ""})


async def test_a_retry_switch_to_a_unit_left_failed_waits_for_its_start_job(mk):
    system = Sequence([{"ActiveState": "failed", "SubState": "failed", "Result": "exit-code", "Job": "42 start"},
                       ACTIVE])
    m, cfg, _ = mk(system)
    assert await m.switch("gufo-qwen27")
    assert StateFile(cfg.state_file).read().state == READY


async def test_a_failed_unit_query_before_the_start_is_not_called_a_foreign_process(mk):
    """Delta review L5: the port is in use and `systemctl show` fails: say so, do not blame another process."""
    class NoShow(FakeSystem):
        async def __call__(self, *cmd, timeout=60):
            if cmd[:3] == ("systemctl", "--user", "show"):
                return 1, "Failed to connect to bus"
            return await super().__call__(*cmd, timeout=timeout)
    system = NoShow()
    m, cfg, _ = mk(system)
    m.port_in_use = lambda port: True
    assert not await m.switch("gufo-qwen27")
    st = StateFile(cfg.state_file).read()
    assert st.state == FAILED and st.profile is None
    assert "could not read the unit state of kiln-gufo-qwen27.service" in st.detail
    assert "another process" not in st.detail
    assert not any(c[:3] == ("systemctl", "--user", "start") for c in system.calls)


async def test_a_run_that_times_out_kills_the_child_group_and_closes_its_pipe():
    from kiln.engines.manager import run
    t = time.monotonic()
    code, msg = await run("sh", "-c", "sleep 5 & sleep 5", timeout=0.2)
    assert code == 124 and "timed out" in msg
    assert time.monotonic() - t < 2  # the grandchild that holds the pipe is killed too
    _collect_transports()


async def test_a_stop_before_the_repair_task_runs_clears_the_repair_text(mk):
    # Delta review round 2, L1: a task cancelled before its first step never runs its finally.
    system = Sequence([crashed(1)])
    m, cfg, _ = mk(system, probe_ok=False)
    StateFile(cfg.state_file).set("gufo-qwen27", READY)
    await m.watch()  # the repair task exists but has not run a step
    await m.stop("stopped from the command line")
    assert StateFile(cfg.state_file).read().state == "stopped"
    assert m._repairing is None
    assert "restarts after a crash" not in (await m.blocked() or "")


class RestartingAtStart(Sequence):
    """kiln-dash starts while systemd waits RestartSec after a crash: `is-active` says no."""

    async def __call__(self, *cmd, timeout=60):
        if "is-active" in cmd:
            self.calls.append(cmd)
            return 3, ""
        return await super().__call__(*cmd, timeout=timeout)


async def test_reconcile_during_a_crash_restart_resumes_the_repair(mk):
    # Delta review round 2, L2: the record must not say stopped while systemd brings the engine back.
    system = RestartingAtStart([crashed(1), ACTIVE])
    m, cfg, _ = mk(system)
    StateFile(cfg.state_file).set("gufo-qwen27", "starting", detail="systemd restarts it")
    st = await m.reconcile()
    assert (st.profile, st.state) == ("gufo-qwen27", "starting")
    await asyncio.wait_for(m.task, 5)
    assert StateFile(cfg.state_file).read().state == READY


async def test_reconcile_of_a_unit_that_really_stopped_writes_stopped(mk):
    system = RestartingAtStart([{"ActiveState": "inactive", "SubState": "dead", "Result": "success"}])
    m, cfg, _ = mk(system)
    StateFile(cfg.state_file).set("gufo-qwen27", READY)
    st = await m.reconcile()
    assert st.state == "stopped" and m.task is None
