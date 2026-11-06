"""Durable per-request records: one JSON line per request, one file per UTC day.

The dashboard ingests these by byte offset, so nothing is lost while it is
down. Message content never appears here.
"""
from __future__ import annotations

import json
import time
from pathlib import Path

from kiln.private_files import private_dir, private_file

TAIL_BLOCK = 1 << 18  # bytes read per step when reading a file backward


def day_file(directory: Path, ts: float) -> Path:
    return directory / f"requests-{time.strftime('%Y-%m-%d', time.gmtime(ts))}.jsonl"


class RecordLog:
    def __init__(self, directory: Path):
        self.dir = private_dir(Path(directory))
        self._made: Path | None = None  # the day file last made owner-only: one check per day, not per write

    def write(self, rec: dict) -> Path:
        path = day_file(self.dir, rec["ts_start"])
        if path != self._made:
            private_file(path)
            self._made = path
        line = json.dumps(rec, separators=(",", ":"), ensure_ascii=False) + "\n"
        with open(path, "a", encoding="utf-8") as f:
            f.write(line)
        return path


def read_tail(directory: Path, since: float, now: float, limit: int) -> tuple[list[dict], bool]:
    """(records that ended at or after since, oldest first; whether that is all of them).

    Reads only the tails of yesterday's and today's files: a record goes to the file of its start day, so a
    request that crossed midnight is in yesterday's file. Not complete when it stopped at the limit."""
    found, complete = [], True
    for path in (day_file(Path(directory), now - 86400), day_file(Path(directory), now)):
        recs, whole = _file_tail(path, since, limit)
        found += recs
        complete = complete and whole
    found.sort(key=lambda r: r["ts_end"])
    if len(found) > limit:
        found, complete = found[-limit:], False
    return found, complete


def _file_tail(path: Path, since: float, limit: int) -> tuple[list[dict], bool]:
    """A file is appended at request end, so its lines are in ts_end order: read backward from the end and
    stop at the first record that ended before since. Lines that do not parse (a write cut short) are skipped."""
    out: list[dict] = []
    if not path.exists():
        return out, True  # no request started that day
    with open(path, "rb") as f:
        pos, rest = f.seek(0, 2), b""
        while pos > 0:
            step = min(TAIL_BLOCK, pos)
            pos -= step
            f.seek(pos)
            lines = (f.read(step) + rest).split(b"\n")
            rest = lines.pop(0) if pos > 0 else b""  # the first piece can be the end of a longer line
            for line in reversed(lines):
                rec = _parse(line)
                if rec is None:
                    continue
                if rec["ts_end"] < since:
                    out.reverse()
                    return out, True
                if len(out) >= limit:
                    out.reverse()
                    return out, False
                out.append(rec)
    out.reverse()
    return out, True


def _parse(line: bytes) -> dict | None:
    if not line.strip():
        return None
    try:
        rec = json.loads(line)
    except ValueError:
        return None
    if not isinstance(rec, dict) or not isinstance(rec.get("ts_end"), (int, float)):
        return None
    return rec
