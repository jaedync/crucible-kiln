"""The active engine profile, shared by the manager (writer) and the gateway (reader).

A small JSON file instead of IPC: the gateway keeps routing correctly even if
the dashboard (which owns the manager) is down or restarting.
"""
from __future__ import annotations

import json
import os
import time
from dataclasses import asdict, dataclass
from pathlib import Path

READY, STARTING, STOPPING, STOPPED, FAILED = "ready", "starting", "stopping", "stopped", "failed"
# The steps of a switch or a start, in order (a plain stop has only "stop"). The rack panel's Engine screen
# shows them; `detail` keeps the free text for people and the event log.
PHASES = ("stop", "drain", "load", "test")


@dataclass(frozen=True)
class ActiveState:
    profile: str | None
    state: str
    since: float
    target: str | None = None  # profile being switched to, if any
    detail: str = ""
    phase: str | None = None  # one of PHASES while the state is starting or stopping
    phase_since: float | None = None  # wall time the phase began

    @property
    def serving(self) -> bool:
        return self.state == READY and self.profile is not None


STOPPED_STATE = ActiveState(None, STOPPED, 0.0)


class StateFile:
    def __init__(self, path: Path):
        self.path = Path(path)
        self._cache: tuple[float, ActiveState] | None = None

    def read(self) -> ActiveState:
        try:
            mtime = self.path.stat().st_mtime_ns
        except FileNotFoundError:
            return STOPPED_STATE
        if self._cache and self._cache[0] == mtime:
            return self._cache[1]
        try:
            d = json.loads(self.path.read_text())
            phase = d.get("phase") if d.get("phase") in PHASES else None
            since = d.get("phase_since") if phase else None
            st = ActiveState(d.get("profile"), d.get("state", STOPPED), float(d.get("since", 0)),
                             d.get("target"), d.get("detail", ""), phase,
                             float(since) if isinstance(since, (int, float)) and not isinstance(since, bool) else None)
        except (OSError, ValueError, TypeError):
            return STOPPED_STATE
        self._cache = (mtime, st)
        return st

    def write(self, st: ActiveState) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        tmp = self.path.with_name(self.path.name + ".tmp")
        tmp.write_text(json.dumps({"v": 1, **asdict(st)}))
        os.replace(tmp, self.path)

    def set(self, profile: str | None, state: str, target: str | None = None, detail: str = "",
            now: float | None = None, phase: str | None = None, phase_since: float | None = None) -> ActiveState:
        at = time.time() if now is None else now
        st = ActiveState(profile, state, at, target, detail, phase, (phase_since or at) if phase else None)
        self.write(st)
        return st
