"""Hardware readings from sysfs and /proc (a Strix Halo desktop: k10temp, amdgpu, cros_ec).

hwmon numbers change across boots, so sensors are found by name and label on
every scan. A sensor that can't be read is left out rather than reported as 0.
"""
from __future__ import annotations

import re
from pathlib import Path

SYS = Path("/sys")
PROC = Path("/proc")


def _read(path: Path) -> str | None:
    try:
        return path.read_text().strip()
    except OSError:
        return None


def _num(path: Path, scale: float = 1.0) -> float | None:
    v = _read(path)
    try:
        return float(v) / scale if v is not None else None
    except ValueError:
        return None


class Sensors:
    def __init__(self, root: Path = SYS, proc: Path = PROC):
        self.root, self.proc = root, proc

    def _hwmon(self) -> dict[str, Path]:
        out = {}
        for d in sorted((self.root / "class/hwmon").glob("hwmon*")):
            name = _read(d / "name")
            if name and name not in out:
                out[name] = d
        return out

    @staticmethod
    def _temp(d: Path | None, label: str) -> float | None:
        if d is None:
            return None
        for lab in d.glob("temp*_label"):
            if _read(lab) == label:
                v = _num(lab.with_name(lab.name.replace("_label", "_input")), 1000)
                return round(v, 1) if v is not None else None
        return None

    def _gpu(self) -> Path | None:
        for card in sorted((self.root / "class/drm").glob("card[0-9]*")):
            dev = card / "device"
            if (dev / "mem_info_gtt_total").exists():
                return dev
        return None

    @staticmethod
    def _sclk_mhz(gpu: Path) -> float | None:
        """The shader clock level marked current ("2: 2900Mhz *"); None when no level is marked."""
        for line in (_read(gpu / "pp_dpm_sclk") or "").splitlines():
            if line.rstrip().endswith("*"):
                m = re.search(r"(\d+)\s*Mhz", line, re.I)
                return float(m[1]) if m else None
        return None

    def _mem_available(self) -> float | None:
        text = _read(self.proc / "meminfo") or ""
        for line in text.splitlines():
            if line.startswith("MemAvailable:"):
                return float(line.split()[1]) * 1024
        return None

    def sample(self) -> dict:
        h = self._hwmon()
        k10, amdgpu, ec = h.get("k10temp"), h.get("amdgpu"), h.get("cros_ec")
        gpu = self._gpu()
        power = _num(amdgpu / "power1_average", 1e6) if amdgpu else None
        if power is None and amdgpu:
            power = _num(amdgpu / "power1_input", 1e6)
        data = {
            "tctl_c": self._temp(k10, "Tctl"),
            "edge_c": self._temp(amdgpu, "edge"),
            "ec_cpu_c": self._temp(ec, "cpu@4c"),
            "mem_board_c": self._temp(ec, "mainboard_memory@4d"),
            "ambient_c": self._temp(ec, "mainboard_ambient@4d"),
            "fan_rpm": _num(ec / "fan1_input") if ec else None,
            "power_w": round(power, 1) if power is not None else None,
            "gpu_busy_pct": _num(gpu / "gpu_busy_percent") if gpu else None,
            "gtt_used_bytes": _num(gpu / "mem_info_gtt_used") if gpu else None,
            "gtt_total_bytes": _num(gpu / "mem_info_gtt_total") if gpu else None,
            "vram_used_bytes": _num(gpu / "mem_info_vram_used") if gpu else None,
            "vram_total_bytes": _num(gpu / "mem_info_vram_total") if gpu else None,
            "sclk_mhz": self._sclk_mhz(gpu) if gpu else None,
            "ram_avail_bytes": self._mem_available(),
            "profile": _read(self.root / "firmware/acpi/platform_profile"),
        }
        return {k: v for k, v in data.items() if v is not None}
