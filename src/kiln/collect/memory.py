"""Where the host's RAM goes, and what the engine keeps on disk while it runs.

RAM: the kernel's /proc/meminfo and amdgpu's GTT counter (on an APU, GPU memory is system RAM
that meminfo leaves out of its other fields), split further by numbers the engine prints when it
starts (its weight arena) and after each turn (its conversation cache). Disk: the engine's saved
conversation states (its --state-dir) and the per-layer embedding (PLE) table it maps from disk,
with the part of that table that is in the page cache now.

Every reading that cannot be made is left out, never reported as 0. Sampling reads /proc files
of other processes and walks a directory, so the dashboard runs it in a thread.
"""
from __future__ import annotations

import ctypes
import os
import re
import subprocess
import time
from pathlib import Path

PROC = Path("/proc")
CGROUP_ROOT = Path("/sys/fs/cgroup")
GIB = 1024 ** 3
# cachestat(2), Linux 6.5+: page cache counts for a file range without mapping it.
SYS_CACHESTAT = {"x86_64": 451, "aarch64": 451}
PAGE = os.sysconf("SC_PAGE_SIZE")
# A 39 GB file takes cachestat about 0.2 s: its residency changes slowly, so it is read less often.
PLE_EVERY_S = 30.0

# Engine log lines with sizes (Crucible, engine.hip, model.h, batched_serve.h, vision.h). Sizes only: no
# request content is in them. Each pattern -> (fact, group of the number, bytes per unit).
START_LINES = (
    (re.compile(r"^weight payload \d+ bytes, arena reserved (\d+) bytes"), "weights_bytes", 1),
    # An optional type first ("BF16, "): engines with a BF16 n-gram table name it, older ones do not.
    (re.compile(r"^PLE table on host: (?:[A-Za-z0-9_]+, )?(\d+) bytes"), "ple_bytes", 1),
    (re.compile(r"^conversation cache: (\d+) bytes budget"), "cache_budget_bytes", 1),
    (re.compile(r"^stage arena: ([\d.]+) MiB"), "stage_bytes", 1 << 20),
    # One slot's KV and recurrent state for its whole context (sequence_state.h), allocated for every slot.
    (re.compile(r"^sequence storage: .*\blogical_state_bytes=(\d+)"), "slot_state_bytes", 1),
    # Its fixed part: the recurrent (Gated DeltaNet) state, which does not grow with the context. Asked of
    # Crucible; until an engine prints it, KV and state stay one part.
    (re.compile(r"^sequence storage: .*\bfixed_state_bytes=(\d+)"), "slot_fixed_bytes", 1),
    (re.compile(r"^slot checkpoints: \d+ per slot, \d+ bytes each, (\d+) bytes"), "checkpoint_bytes", 1),
    (re.compile(r"^prefill cut buffer: (\d+) bytes"), "cut_bytes", 1),
    (re.compile(r"^vision tower ([\d.]+) GB loaded"), "vision_bytes", 1e9),
    (re.compile(r"^draft head \d+ tokens, ([\d.]+) MB"), "draft_bytes", 1e6),
    (re.compile(r"^draft head q\d.* ([\d.]+) MB coarse copy"), "draft_copy_bytes", 1e6),
)
START_GREP = "^(weight payload|PLE table on host|conversation cache: [0-9]|stage arena:|sequence storage:" \
             "|slot checkpoints: [0-9]|prefill cut buffer|vision tower|draft head)"

# Bottom to top (js/memory_model.js has the labels). The GPU parts come first, in the order GTT is split.
# kv and recurrent when the engine prints the split, else kv_state holds both.
GPU_BANDS = ("weights", "kv", "recurrent", "kv_state", "cache", "cache_free", "checkpoints", "stage", "vision_draft", "gpu_other")
RAM_BANDS = (*GPU_BANDS, "ple_ram", "file_cache", "programs", "kernel", "free")
DISK_BANDS = ("ple_file", "states")


def engine_facts(msg: str) -> dict | None:
    """Sizes from one engine start line ({"weights_bytes": ...}, see START_LINES), else None."""
    found = {key: round(float(m[1]) * unit) for pattern, key, unit in START_LINES if (m := pattern.match(msg))}
    return found or None


def start_facts(unit: str, run=subprocess.run) -> dict:
    """The sizes a running unit printed at its start, read back from the journal.

    Ingest learns them as the lines arrive; this covers a dashboard that started after the engine
    and so never saw its start lines (its journal cursor is past them).
    """
    try:
        since = run(["systemctl", "--user", "show", "-p", "ActiveEnterTimestamp", "--timestamp=unix", "--value",
                     unit], capture_output=True, text=True, timeout=10).stdout.strip().lstrip("@")
        if not since.isdigit() or int(since) == 0:
            return {}
        out = run(["journalctl", "--user", "-u", unit, f"--since=@{since}", "-o", "cat", "--no-pager",
                   "-g", START_GREP],
                  capture_output=True, text=True, timeout=20).stdout
    except (OSError, subprocess.SubprocessError):
        return {}
    facts: dict = {}
    for line in out.splitlines():
        facts.update(engine_facts(line.strip()) or {})
    return facts


def parse_meminfo(text: str) -> dict[str, int]:
    """/proc/meminfo as bytes by field name."""
    out = {}
    for line in text.splitlines():
        name, _, rest = line.partition(":")
        parts = rest.split()
        if parts and parts[0].isdigit():
            out[name] = int(parts[0]) * (1024 if parts[1:] == ["kB"] else 1)
    return out


def gpu_parts(gpu: int, facts: dict, slots: int | None) -> dict[str, int]:
    """GTT split by the sizes the engine printed, in GPU_BANDS order; gpu_other takes what is left.

    Each part takes at most what is left of GTT, so a stale figure can never make the parts exceed it.
    Without the weight arena (another engine, or none running) GTT stays whole.
    """
    out = dict.fromkeys(GPU_BANDS, 0)
    weights = facts.get("weights_bytes")
    if not weights or weights > gpu:
        out["gpu_other"] = gpu
        return out
    pinned, in_use = facts.get("cache_pinned_bytes"), facts.get("cache_bytes") or 0
    pool = pinned if pinned is not None else in_use  # the pinned pool holds the cache's entries
    slot, fixed = facts.get("slot_state_bytes") or 0, facts.get("slot_fixed_bytes") or 0
    split = 0 < fixed <= slot
    wanted = {
        "weights": weights,
        "kv": (slot - fixed) * (slots or 0) if split else 0,
        "recurrent": fixed * (slots or 0) if split else 0,
        "kv_state": 0 if split else slot * (slots or 0),
        "cache": min(in_use, pool),
        "cache_free": max(0, pool - in_use),
        "checkpoints": (facts.get("checkpoint_bytes") or 0) + (facts.get("cut_bytes") or 0),
        "stage": facts.get("stage_bytes") or 0,
        "vision_draft": sum(facts.get(k) or 0 for k in ("vision_bytes", "draft_bytes", "draft_copy_bytes")),
    }
    left = gpu
    for key, v in wanted.items():
        out[key] = min(max(0, int(v)), left)
        left -= out[key]
    out["gpu_other"] = left
    return out


def ram_bands(mi: dict[str, int], gtt_used: float | None, facts: dict | None = None, slots: int | None = None,
              ple_ram: int | None = None) -> dict[str, int] | None:
    """RAM by part, in bytes; the parts add up to MemTotal. None without GTT or the meminfo fields it needs.

    GTT pages are in none of meminfo's other fields, so the kernel's own share is what is left after
    free, GTT, the page cache and anonymous memory. The engine's printed sizes split GTT (gpu_parts).
    """
    need = ("MemTotal", "MemFree", "Cached", "AnonPages")
    if gtt_used is None or any(k not in mi for k in need):
        return None
    total, free = mi["MemTotal"], mi["MemFree"]
    # GTT is read apart from meminfo: when the engine frees memory between the two reads, free and GTT both
    # count it. GTT gives way, so the parts still add up to the total.
    host_side = free + mi["Cached"] + mi.get("Buffers", 0) + mi["AnonPages"]
    gpu = min(max(0, int(gtt_used)), max(0, total - host_side))
    file_all = mi["Cached"] + mi.get("Buffers", 0) - mi.get("Shmem", 0)
    ple = min(max(0, ple_ram or 0), max(0, file_all))
    return {**gpu_parts(gpu, facts or {}, slots), "ple_ram": ple, "file_cache": max(0, file_all - ple),
            "programs": mi["AnonPages"] + mi.get("Shmem", 0), "kernel": max(0, total - host_side - gpu),
            "free": free, "total": total}


def tree_bytes(path: Path) -> int | None:
    """Bytes a directory tree takes on disk (allocated blocks, so sparse files count what they use)."""
    total, stack = 0, [path]
    try:
        if not path.is_dir():
            return None
    except OSError:
        return None
    while stack:
        try:
            with os.scandir(stack.pop()) as it:
                for e in it:
                    try:
                        if e.is_dir(follow_symlinks=False):
                            stack.append(Path(e.path))
                        else:
                            total += e.stat(follow_symlinks=False).st_blocks * 512
                    except OSError:
                        continue  # a file the engine removed while we walked
        except OSError:
            continue
    return total


class _CacheStat(ctypes.Structure):
    _fields_ = [("nr_cache", ctypes.c_uint64), ("nr_dirty", ctypes.c_uint64), ("nr_writeback", ctypes.c_uint64),
                ("nr_evicted", ctypes.c_uint64), ("nr_recently_evicted", ctypes.c_uint64)]


class _CacheStatRange(ctypes.Structure):
    _fields_ = [("off", ctypes.c_uint64), ("len", ctypes.c_uint64)]


def page_cache_bytes(path: Path) -> int | None:
    """Bytes of the file in the page cache now (cachestat), or None where the call is missing."""
    nr = SYS_CACHESTAT.get(os.uname().machine)
    if nr is None:
        return None
    try:
        libc = ctypes.CDLL(None, use_errno=True)
        fd = os.open(path, os.O_RDONLY)
    except OSError:
        return None
    try:
        out = _CacheStat()
        if libc.syscall(nr, fd, ctypes.byref(_CacheStatRange(0, 0)), ctypes.byref(out), 0) != 0:
            return None
        return int(out.nr_cache) * PAGE
    finally:
        os.close(fd)


def _arg(argv: list[str], name: str) -> str | None:
    for i, a in enumerate(argv):
        if a == name and i + 1 < len(argv):
            return argv[i + 1]
        if a.startswith(name + "="):
            return a.split("=", 1)[1]
    return None


class EngineProbe:
    """The engine unit's processes (its cgroup, beside kiln-dash's own) and what they hold on disk."""

    def __init__(self, proc: Path = PROC, cgroup_root: Path = CGROUP_ROOT, cache_bytes=page_cache_bytes,
                 clock=time.monotonic):
        self.proc, self.cgroup_root, self.cache_bytes, self.clock = proc, cgroup_root, cache_bytes, clock
        self._ple: dict[int, str] = {}  # pid -> the PLE file it maps
        self._ple_ram: tuple[float, str, int | None] | None = None  # (when, path, bytes)
        self._io: tuple[float, tuple[int, ...], int, int] | None = None  # (when, pids, read, written)

    def _unit_dir(self, unit: str) -> Path | None:
        """The unit's cgroup: beside kiln-dash's own, else found from any process in it."""
        try:
            own = (self.proc / "self/cgroup").read_text()
        except OSError:
            own = ""
        for line in own.splitlines():
            if line.startswith("0::/"):
                d = (self.cgroup_root / line[4:].strip()).parent / unit
                if d.is_dir():
                    return d
        for cg in self.proc.glob("[0-9]*/cgroup"):
            try:
                text = cg.read_text()
            except OSError:
                continue
            for line in text.splitlines():
                path = line.partition("::")[2].strip()
                if f"/{unit}" in path + "/":
                    head = path[:path.index(f"/{unit}") + len(unit) + 1]
                    return self.cgroup_root / head.lstrip("/")
        return None

    def pids(self, unit: str) -> list[int]:
        d = self._unit_dir(unit)
        if d is None or not d.is_dir():
            return []
        out = []
        for procs in sorted(d.rglob("cgroup.procs")):
            try:
                out += [int(x) for x in procs.read_text().split()]
            except (OSError, ValueError):
                continue
        return sorted(set(out))

    def _argv(self, pid: int) -> list[str]:
        try:
            return (self.proc / str(pid) / "cmdline").read_bytes().decode("utf-8", "replace").split("\0")
        except OSError:
            return []

    def _mapped_file(self, pid: int, min_bytes: int) -> str | None:
        if pid in self._ple:
            return self._ple[pid]
        mapped: dict[str, int] = {}  # path -> bytes mapped, over all its segments
        try:
            with open(self.proc / str(pid) / "maps") as f:
                for line in f:
                    parts = line.split(maxsplit=5)
                    if len(parts) < 6 or not parts[5].startswith("/"):
                        continue
                    lo, hi = (int(x, 16) for x in parts[0].split("-"))
                    path = parts[5].strip()
                    mapped[path] = mapped.get(path, 0) + hi - lo
        except (OSError, ValueError):
            return None
        # The smallest file mapped as far as the table's size: the table, not a larger weights file.
        fits = [(n, p) for p, n in mapped.items() if n >= min_bytes]
        found = min(fits)[1] if fits else None
        if found:  # not found is not kept: a starting engine maps its table a few seconds in
            self._ple[pid] = found
        return found

    def _io_rates(self, pids: list[int], now: float) -> tuple[float | None, float | None]:
        read = written = 0
        for pid in pids:
            try:
                for line in (self.proc / str(pid) / "io").read_text().splitlines():
                    k, _, v = line.partition(":")
                    if k == "read_bytes":
                        read += int(v)
                    elif k == "write_bytes":
                        written += int(v)
            except (OSError, ValueError):
                return None, None
        prev, self._io = self._io, (now, tuple(pids), read, written)
        if not prev or prev[1] != tuple(pids) or now <= prev[0]:
            return None, None
        dt = now - prev[0]
        return max(0.0, (read - prev[2]) / dt), max(0.0, (written - prev[3]) / dt)

    def disk(self, unit: str, ple_bytes: int | None) -> dict:
        """The unit's saved states, its PLE file, and its disk read and write rates. {} without processes."""
        pids = self.pids(unit)
        self._ple = {p: v for p, v in self._ple.items() if p in pids}
        if not pids:
            self._io = None
            return {}
        out: dict = {"running": True}
        for pid in pids:
            argv = self._argv(pid)
            state_dir = _arg(argv, "--state-dir")
            if state_dir:
                size = tree_bytes(Path(state_dir))
                if size is not None:
                    out["states"] = size
                cap = _arg(argv, "--state-disk-gib")
                try:
                    if cap is not None:
                        out["states_cap"] = int(float(cap) * GIB)
                except ValueError:
                    pass
                break
        if ple_bytes:
            path = next((p for p in (self._mapped_file(pid, ple_bytes) for pid in pids) if p), None)
            if path:
                try:
                    out["ple_file"] = os.stat(path).st_size
                except OSError:
                    path = None
            if path:
                ram = self._ple_resident(path)
                if ram is not None:
                    out["ple_ram"] = ram
        r, w = self._io_rates(pids, self.clock())
        if r is not None:
            out["read_bps"], out["write_bps"] = round(r), round(w)
        return out

    def _ple_resident(self, path: str) -> int | None:
        now = self.clock()
        last = self._ple_ram
        if last and last[1] == path and now - last[0] < PLE_EVERY_S:
            return last[2]
        value = self.cache_bytes(Path(path))
        self._ple_ram = (now, path, value)
        return value


class MemorySampler:
    """One reading of RAM by part and the engine's disk use, for hw.mem (docs/API.md)."""

    def __init__(self, proc: Path = PROC, probe: EngineProbe | None = None):
        self.proc = proc
        self.probe = probe or EngineProbe(proc=proc)

    def sample(self, unit: str | None, facts: dict, gtt_used: float | None, slots: int | None = None) -> dict | None:
        try:
            mi = parse_meminfo((self.proc / "meminfo").read_text())
        except OSError:
            return None
        disk = self.probe.disk(unit, facts.get("ple_bytes")) if unit else {}
        # The engine's own figures count only while its processes run: a stopped engine left them behind.
        running = disk.get("running", False)
        bands = ram_bands(mi, gtt_used, facts if running else None, slots, disk.get("ple_ram"))
        if bands is None:
            return None
        return {"ram": bands, "disk": {k: disk[k] for k in ("ple_file", "states", "states_cap") if k in disk},
                **{k: disk[k] for k in ("read_bps", "write_bps") if k in disk}}
