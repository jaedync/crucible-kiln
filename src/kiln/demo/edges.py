"""The edges `kiln demo` replaces: the journal, sysfs and /proc, the engine's processes, and systemd.

Each fake feeds the real reader through a seam that reader already has, or one small parameter:
- LogBus.source: a log source for Dash (in place of collect.sources.JournalFollower), with the same
  batch callback, watermark and running flag.
- FakeHardware: a sysfs and /proc tree in the demo's data folder, read by the real collect.hw.Sensors and
  collect.memory.MemorySampler (both take their root paths).
- FakeProbe: collect.memory.EngineProbe's disk() (the engine's processes, its state folder, its PLE file).
- FakeUnits: the command runner of engines.manager.Manager, which plays `systemctl --user` and `podman ps`
  for the fake engines.
"""
from __future__ import annotations

import asyncio
import itertools
import math
import os
import random
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Callable

from kiln.collect import memory
from kiln.demo.engine import PACKS, FakeEngine
from kiln.demo.scenario import PROFILE_PACKS

GIB = 2**30
MEM_TOTAL = 134_304_514_048  # a 128 GB Strix Halo after the 512 MiB VRAM carve-out
GTT_TOTAL = 133_143_986_176
IDLE_GTT = 380 * 2**20  # the desktop and the driver's own buffers with no engine
STATES_CAP = 150 * GIB  # --state-disk-gib 150


# -- the engine log ---------------------------------------------------------------------------
class LogBus:
    """Engine lines, in the order the engines print them, for one Dash log source."""

    def __init__(self, clock=time.time):
        self.clock = clock
        self.queue: asyncio.Queue = asyncio.Queue()
        self.started: dict[str, dict] = {}  # container -> sizes from its last start lines
        self._cursor = itertools.count(1)

    def push(self, container: str, msg: str, ts: float) -> None:
        facts = memory.engine_facts(msg)
        if facts:
            if "weights_bytes" in facts:  # a new start
                self.started[container] = {}
            self.started.setdefault(container, {}).update(facts)
        self.queue.put_nowait((container, msg, ts, f"demo:{next(self._cursor)}"))

    def start_facts(self, unit: str) -> dict:
        """collect.memory.start_facts for the demo: the sizes a unit printed at its start."""
        return dict(self.started.get(unit.removesuffix(".service"), {}))

    def source(self, on_batch) -> "FakeLog":
        return FakeLog(self, on_batch)


class FakeLog:
    """A Dash log source. A batch closes after BATCH_S, like the journal follower's short batches."""
    BATCH_S = 0.05
    IDLE_S = 0.5

    def __init__(self, bus: LogBus, on_batch):
        self.bus, self.on_batch = bus, on_batch
        self.watermark = 0.0
        self.running = False

    async def run(self) -> None:
        self.running = True
        try:
            while True:
                try:
                    first = await asyncio.wait_for(self.bus.queue.get(), self.IDLE_S)
                except asyncio.TimeoutError:
                    self.watermark = self.bus.clock() - self.IDLE_S
                    continue
                batch = [first]
                await asyncio.sleep(self.BATCH_S)
                while not self.bus.queue.empty():
                    batch.append(self.bus.queue.get_nowait())
                await self.on_batch(batch, batch[-1][3])
                self.watermark = self.bus.clock() - self.BATCH_S
        finally:
            self.running = False


# -- sysfs and /proc --------------------------------------------------------------------------------
@dataclass
class Load:
    """What the engine does now, for the sensors."""
    prefilling: int = 0
    decoding: int = 0
    gtt_bytes: int = IDLE_GTT
    ple_ram: int = 0
    unit: str | None = None  # the unit whose processes run; None: any
    states_bytes: int = 0
    read_bps: int = 0
    write_bps: int = 0


def _write(path: Path, text: str) -> None:
    """Atomic: the memory sampler reads in a worker thread."""
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(path.name + ".tmp")
    tmp.write_text(text + "\n")
    os.replace(tmp, path)


class FakeHardware:
    """A sysfs and /proc tree with a Framework Desktop's sensor names (k10temp, amdgpu, cros_ec)."""

    def __init__(self, root: Path, seed: int = 0):
        self.sys, self.proc = Path(root) / "sys", Path(root) / "proc"
        self.rng = random.Random(f"hw:{seed}")
        self.power, self.edge, self.fan = 11.0, 41.0, 900.0
        self.load = Load()
        hw = self.sys / "class" / "hwmon"
        self.k10, self.amdgpu, self.ec = hw / "hwmon2", hw / "hwmon4", hw / "hwmon6"
        self.gpu = self.sys / "class" / "drm" / "card1" / "device"
        for d, name in ((self.k10, "k10temp"), (self.amdgpu, "amdgpu"), (self.ec, "cros_ec")):
            _write(d / "name", name)
        _write(self.k10 / "temp1_label", "Tctl")
        _write(self.amdgpu / "temp1_label", "edge")
        for i, label in enumerate(("cpu@4c", "mainboard_memory@4d", "mainboard_ambient@4d"), 1):
            _write(self.ec / f"temp{i}_label", label)
        _write(self.gpu / "mem_info_gtt_total", str(GTT_TOTAL))
        _write(self.gpu / "mem_info_vram_total", str(512 * 2**20))
        _write(self.gpu / "mem_info_vram_used", str(256 * 2**20))
        _write(self.sys / "firmware" / "acpi" / "platform_profile", "balanced")
        self.update(Load(), dt=0)

    def update(self, load: Load, dt: float) -> None:
        """Move the readings toward what this load draws: power in about 2 s, heat in about 25 s."""
        self.load = load
        busy = load.prefilling > 0 or load.decoding > 0
        target = 128.0 if load.prefilling else (88.0 + 6.0 * min(load.decoding, 4) if load.decoding else 11.0)
        self.power += (target - self.power) * (1 - math.exp(-dt / 2.0)) if dt else 0
        self.edge += (40.0 + self.power * 0.27 - self.edge) * (1 - math.exp(-dt / 25.0)) if dt else 0
        self.fan += (850.0 + max(0.0, self.edge - 45.0) * 70.0 - self.fan) * (1 - math.exp(-dt / 8.0)) if dt else 0
        jitter = self.rng.uniform(-0.6, 0.6)
        tctl = self.edge + (6.0 if busy else 3.0) + jitter
        _write(self.k10 / "temp1_input", str(round(tctl * 1000)))
        _write(self.amdgpu / "temp1_input", str(round((self.edge + jitter / 3) * 1000)))
        _write(self.amdgpu / "power1_average", str(round((self.power + jitter) * 1e6)))
        _write(self.ec / "temp1_input", str(round((tctl - 1.5) * 1000)))
        _write(self.ec / "temp2_input", str(round((36.0 + self.power * 0.1) * 1000)))
        _write(self.ec / "temp3_input", str(round(27.5 * 1000)))
        _write(self.ec / "fan1_input", str(round(self.fan)))
        pct = 100 if load.prefilling else (self.rng.randint(93, 99) if load.decoding else self.rng.randint(0, 2))
        _write(self.gpu / "gpu_busy_percent", str(pct))
        _write(self.gpu / "mem_info_gtt_used", str(load.gtt_bytes))
        level = 2 if busy else 0
        clocks = ("600Mhz", "1600Mhz", "2900Mhz")
        _write(self.gpu / "pp_dpm_sclk", "\n".join(f"{i}: {c}{' *' if i == level else ''}"
                                                    for i, c in enumerate(clocks)))
        self._meminfo(load)

    def _meminfo(self, load: Load) -> None:
        kb = 1024
        anon, shmem, buffers, kernel = 1_410_000_000, 9_806_000, 310_000_000, 1_730_000_000
        cached = load.ple_ram + 1_620_000_000
        free = max(256 * 2**20, MEM_TOTAL - load.gtt_bytes - cached - buffers - anon - kernel)
        fields = {"MemTotal": MEM_TOTAL, "MemFree": free, "MemAvailable": free + cached, "Buffers": buffers,
                  "Cached": cached, "AnonPages": anon, "Shmem": shmem}
        _write(self.proc / "meminfo", "\n".join(f"{k}:{v // kb:>16} kB" for k, v in fields.items()))


class FakeProbe:
    """collect.memory.EngineProbe for the demo: what the running engine holds on disk."""

    def __init__(self, hw: FakeHardware):
        self.hw = hw

    def disk(self, unit: str, ple_bytes: int | None) -> dict:
        load = self.hw.load
        if load.unit is not None and load.unit != unit:
            return {}
        out = {"running": True, "states_cap": STATES_CAP, "read_bps": load.read_bps, "write_bps": load.write_bps}
        if load.states_bytes:
            out["states"] = load.states_bytes
        if ple_bytes:
            out["ple_file"], out["ple_ram"] = ple_bytes, load.ple_ram
        return out


# -- systemd ------------------------------------------------------------------------------------------
class FakeUnits:
    """Plays `systemctl --user` and `podman ps` for engines.manager.Manager, with one FakeEngine per running
    profile. A stop takes about 3 s and a start loads for about 6 s (both divided by `speed`)."""
    STOP_S = 3.0
    LOAD_S = 6.0
    RELEASE_S = 1.5  # the GPU memory comes back this long after the stop

    def __init__(self, cfg, emit: Callable[[str, str, float], None], speed: float = 1.0, seed: int = 0,
                 clock=time.time):
        self.cfg, self.emit, self.speed, self.seed, self.clock = cfg, emit, max(speed, 0.01), seed, clock
        self.engines: dict[str, FakeEngine] = {}
        self.loading: dict[str, float] = {}  # profile -> share of its memory allocated so far
        self.released: dict[str, float] = {}  # profile -> when its last GPU memory goes
        self.ple_ram = 2_650_000_000
        self._tasks: set[asyncio.Task] = set()

    @property
    def running(self) -> set[str]:
        return set(self.engines)

    def _profile_by_unit(self, unit: str):
        return next((p for p in self.cfg.profiles.values() if p.unit == unit), None)

    def _engine(self, name: str) -> FakeEngine:
        p = self.cfg.profiles[name]
        pack = PACKS[PROFILE_PACKS[name]]
        return FakeEngine(pack, p.port, lambda msg, ts: self.emit(p.container_name, msg, ts), seed=self.seed,
                          clock=self.clock)

    async def boot(self, name: str, state) -> None:
        """The demo's start: the default profile is up and serving before the dashboard starts."""
        from kiln.engines.state import READY
        eng = self._engine(name)
        self.engines[name] = eng
        await eng.start()
        state.set(name, READY, None, f"{self.cfg.profiles[name].label} answered a test generation", now=self.clock())

    async def _start(self, name: str) -> None:
        eng = self.engines[name]  # set by the start command itself, as systemd marks the unit at once
        self.loading[name] = 0.0
        n = len(eng.start_lines())

        def step(i: int) -> None:
            self.loading[name] = i / n
        try:
            await eng.start(load_s=self.LOAD_S / self.speed, on_line_s=step)
        finally:
            self.loading.pop(name, None)

    async def run(self, *cmd: str, timeout: float = 60) -> tuple[int, str]:
        if cmd[:1] == ("podman",):
            return 0, ""  # no containers: the demo's engines are native units
        if cmd[:2] != ("systemctl", "--user"):
            return 127, f"{cmd[0]}: not available in the demo"
        verb, unit = cmd[2], cmd[-1]
        p = self._profile_by_unit(unit)
        if verb in ("daemon-reload", "list-units"):
            return 0, ""
        if verb == "stop":
            if p and p.name in self.engines:
                await asyncio.sleep(self.STOP_S / self.speed)
                await self.engines.pop(p.name).stop()
                self.released[p.name] = self.clock() + self.RELEASE_S / self.speed
            return 0, ""
        if verb == "start":
            if p is None:
                return 5, f"Unit {unit} not found."
            if p.name not in self.engines:
                self.engines[p.name] = self._engine(p.name)
                task = asyncio.get_running_loop().create_task(self._start(p.name))
                self._tasks.add(task)
                task.add_done_callback(self._tasks.discard)
            return 0, ""
        if verb == "is-active":
            return (0, "active") if p and p.name in self.engines else (3, "inactive")
        if verb == "is-failed":
            return 1, "active" if p and p.name in self.engines else "inactive"
        if verb == "show" and "-P" not in cmd:
            on = p is not None and p.name in self.engines
            return 0, (f"ActiveState={'active' if on else 'inactive'}\nSubState={'running' if on else 'dead'}\n"
                       "Result=success\nNRestarts=0\nJob=")
        if verb == "status":
            return 0, f"{unit} (demo)"
        return 0, ""

    # -- what the sensors see --------------------------------------------------------------------------
    def gtt_used(self) -> int:
        now, used = self.clock(), IDLE_GTT
        for name, eng in self.engines.items():
            full = eng.gpu_bytes()
            used += round(full * self.loading[name]) if name in self.loading else full
        for name, until in list(self.released.items()):
            if now < until and name not in self.engines:
                used += 60 * GIB
            elif now >= until:
                del self.released[name]
        return used

    def load(self, dt: float) -> Load:
        """The load of the serving engine, for FakeHardware."""
        eng = next((e for e in self.engines.values() if e.loaded), None)
        if eng is None:
            return Load(gtt_bytes=self.gtt_used(), unit="none", ple_ram=self.ple_ram)
        # Prefill reads n-gram rows from the PLE file, so more of it stays in the page cache.
        if eng.prefilling:
            self.ple_ram = min(9 * GIB, self.ple_ram + round(140_000_000 * dt))
        name = next(n for n, e in self.engines.items() if e is eng)
        busy = eng.prefilling or eng.decoding
        return Load(prefilling=eng.prefilling, decoding=eng.decoding, gtt_bytes=self.gtt_used(),
                    ple_ram=self.ple_ram, unit=self.cfg.profiles[name].unit, states_bytes=eng.states_bytes,
                    read_bps=(410_000_000 if eng.prefilling else 9_600_000) if busy else 0,
                    write_bps=136_000_000 if eng.decoding else 0)

    async def close(self) -> None:
        for t in list(self._tasks):
            t.cancel()
        await asyncio.gather(*self._tasks, return_exceptions=True)
        for eng in list(self.engines.values()):
            await eng.stop()
        self.engines.clear()
