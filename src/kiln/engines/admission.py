"""The admission hold: a planned engine switch makes new gateway requests wait instead of failing.

A switch script writes the file (`kiln hold`) before it drains the engine and removes it
(`kiln release`) once the new engine serves. A file, like the state file, so the hold works
while the dashboard is down. The file only ever delays: a missing, broken or expired file is
no hold, so a script that dies mid-switch blocks nobody past `until`.
"""
from __future__ import annotations

import json
import math
import os
import time
from dataclasses import dataclass
from pathlib import Path

MAX_HOLD_S = 3600  # a longer hold is a mistake: refresh it instead
MAX_REASON = 200


@dataclass(frozen=True)
class Hold:
    until: float
    reason: str = ""


def _parse(text: str) -> Hold | None:
    try:
        d = json.loads(text)
    except ValueError:
        return None
    until = d.get("until") if isinstance(d, dict) else None
    # bool is an int to Python, but `true` is no time
    if isinstance(until, bool) or not isinstance(until, (int, float)) or not math.isfinite(until):
        return None
    reason = d.get("reason")
    return Hold(float(until), reason[:MAX_REASON] if isinstance(reason, str) else "")


class AdmissionHold:
    def __init__(self, path: Path, clock=time.time):
        self.path, self.clock = Path(path), clock
        self._cache: tuple[int, Hold | None] | None = None

    def read(self) -> Hold | None:
        """The hold in the file, expired or not. The gateway asks every 0.25 s per waiting request,
        so it parses only when the file changed."""
        try:
            mtime = self.path.stat().st_mtime_ns
        except FileNotFoundError:
            return None
        if self._cache and self._cache[0] == mtime:
            return self._cache[1]
        try:
            hold = _parse(self.path.read_text())
        except OSError:
            return None
        self._cache = (mtime, hold)
        return hold

    def active(self) -> Hold | None:
        hold = self.read()
        return hold if hold and hold.until > self.clock() else None

    def write(self, seconds: float, reason: str = "") -> dict:
        """Hold (or refresh the hold) for `seconds` from now. Returns what the file holds."""
        if not 1 <= seconds <= MAX_HOLD_S:
            raise ValueError(f"seconds must be from 1 to {MAX_HOLD_S}, got {seconds}")
        d = {"v": 1, "until": float(self.clock() + seconds), "reason": reason[:MAX_REASON]}
        self.path.parent.mkdir(parents=True, exist_ok=True)
        tmp = self.path.with_name(self.path.name + ".tmp")
        tmp.write_text(json.dumps(d))
        os.replace(tmp, self.path)
        return d

    def release(self) -> bool:
        """Remove the hold. False if there was none."""
        try:
            self.path.unlink()
            return True
        except FileNotFoundError:
            return False
