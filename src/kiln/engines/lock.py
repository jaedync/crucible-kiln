"""The evaluation lock: while an evaluation runs through Kiln, only it may switch engines
and only its own traffic (X-Kiln-Client: eval-*) is served.

A small JSON file next to the active-engine state. It expires on its own at
`until`, so a crashed evaluation can't hold the machine forever.
"""
from __future__ import annotations

import fcntl
import json
import os
import secrets
import time
from contextlib import contextmanager
from pathlib import Path

EVAL_LABEL_PREFIX = "eval-"


class EvalLock:
    def __init__(self, path: Path, clock=time.time):
        self.path, self.clock = Path(path), clock

    def read(self) -> dict | None:
        """The lock if it is held and unexpired, else None."""
        try:
            d = json.loads(self.path.read_text())
        except (OSError, ValueError):
            return None
        if not isinstance(d, dict) or float(d.get("until", 0)) <= self.clock():
            return None
        return d

    @contextmanager
    def _exclusive(self):
        """Serialize acquire across processes, so two evaluations can't both take the lock."""
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with open(self.path.with_name(self.path.name + ".guard"), "a") as guard:
            fcntl.flock(guard, fcntl.LOCK_EX)
            yield

    def acquire(self, label: str, until: float) -> str:
        with self._exclusive():
            held = self.read()
            if held:
                raise RuntimeError(f"evaluation lock already held ({held.get('label')})")
            return self._write(label, until)

    def _write(self, label: str, until: float) -> str:
        token = secrets.token_hex(16)
        tmp = self.path.with_name(self.path.name + ".tmp")
        fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
        with os.fdopen(fd, "w") as f:
            json.dump({"label": label, "token": token, "since": self.clock(), "until": until}, f)
        os.replace(tmp, self.path)
        return token

    def release(self, token: str) -> None:
        d = self.read()
        if d is None or d.get("token") == token:
            self.path.unlink(missing_ok=True)

    def allows(self, token: str | None) -> bool:
        d = self.read()
        # Bytes: compare_digest refuses a str with non-ASCII characters (a TypeError, so a 500).
        return d is None or (token is not None and secrets.compare_digest(
            str(d.get("token")).encode("utf-8", "surrogatepass"), token.encode("utf-8", "surrogatepass")))

    def describe(self) -> str | None:
        d = self.read()
        if d is None:
            return None
        until = time.strftime("%H:%M", time.localtime(float(d["until"])))
        return f"evaluation {d.get('label')} running (lock expires {until})"
