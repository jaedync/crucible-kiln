"""The dashboard's three inputs: the user journal, the gateway's JSONL files and its live feed.

Each source hands batches to a callback and advances a watermark: the time up
to which it has delivered everything it ever will. Ingest only settles a
request once both durable sources are past it (see collect.ingest).
"""
from __future__ import annotations

import asyncio
import json
import time
from pathlib import Path
from typing import Awaitable, Callable

import aiohttp

# No journal output for this long after a line means we are at its end: deliver the batch. Engine
# prefill reports already reach journalctl up to 0.5 s late (journald batches its change notices), so
# the batch must not wait long on top, nor more than BATCH_MAX_AGE_S after its first line. A backfill
# fills batches of BATCH_MAX lines, or delivers every BATCH_MAX_AGE_S when journalctl reads slowly.
READ_IDLE_S = 0.05
IDLE_WAIT_S = 1.0  # with nothing to deliver, wake this often to move the watermark on
BATCH_MAX_AGE_S = 0.1  # lines closer than READ_IDLE_S apart must not hold a report back for long
BATCH_MAX = 500
RESTART_DELAY_S = 5.0
POLL_S = 0.5
READ_CHUNK_BYTES = 8 << 20  # the most of a requests file read and parsed at once
OWN_UNITS = ("kiln-gateway.service", "kiln-dash.service")


def journal_entry(raw: bytes) -> tuple[str, str, float, str | None] | None:
    """(container, message, epoch seconds, cursor) for a container log entry, else None."""
    try:
        e = json.loads(raw)
    except ValueError:
        return None
    msg, container = e.get("MESSAGE"), e.get("CONTAINER_NAME")
    if not container:
        # Engines run as native user units (e.g. a locally built binary under evaluation) log
        # without CONTAINER_NAME; their unit name plays the same role.
        unit = e.get("_SYSTEMD_USER_UNIT") or ""
        if unit.startswith(("eval-", "kiln-")) and unit.endswith(".service") and unit not in OWN_UNITS:
            container = unit.removesuffix(".service")
    if isinstance(msg, list):  # journald sends non-UTF-8 messages as byte arrays
        msg = bytes(msg).decode("utf-8", "replace")
    if not container or not isinstance(msg, str):
        return None
    try:
        ts = int(e.get("__REALTIME_TIMESTAMP", 0)) / 1e6
    except (TypeError, ValueError):
        return None
    return container, msg, ts, e.get("__CURSOR")


class JournalFollower:
    """Follows container log lines in the user journal (podman's journald log driver)."""

    def __init__(self, uid: int, on_batch: Callable[[list, str | None], Awaitable[None]],
                 cursor: str | None, backfill: str = "-30d", clock=time.time):
        self.uid, self.on_batch, self.cursor, self.backfill, self.clock = uid, on_batch, cursor, backfill, clock
        self.watermark = 0.0
        self.running = False

    def _cmd(self) -> list[str]:
        start = [f"--after-cursor={self.cursor}"] if self.cursor else [f"--since={self.backfill}"]
        return ["journalctl", "--follow", "--output=json", "--no-pager", f"_UID={self.uid}", *start]

    async def run(self) -> None:
        while True:
            delivered = await self._run_once()
            if self.cursor and delivered == 0:
                # The saved cursor no longer exists (journal vacuumed): start from the backfill window.
                self.cursor = None
            await asyncio.sleep(RESTART_DELAY_S)

    async def _run_once(self) -> int:
        proc = await asyncio.create_subprocess_exec(*self._cmd(), stdout=asyncio.subprocess.PIPE,
                                                    stderr=asyncio.subprocess.DEVNULL, limit=1 << 22)
        self.running, delivered, batch, last_cursor, since = True, 0, [], None, 0.0
        try:
            while True:
                # A cursor is news only once a line brought one: after a restart last_cursor is None.
                fresh = bool(batch) or last_cursor not in (None, self.cursor)
                wait = READ_IDLE_S if fresh else IDLE_WAIT_S
                try:
                    raw = await asyncio.wait_for(proc.stdout.readline(), wait)
                except asyncio.TimeoutError:
                    raw = None
                if raw == b"":
                    break  # journalctl exited
                if raw:
                    delivered += 1
                    entry = journal_entry(raw)
                    try:
                        last_cursor = json.loads(raw).get("__CURSOR") or last_cursor
                    except ValueError:
                        pass
                    if entry:
                        since = since if batch else time.monotonic()
                        batch.append(entry)
                full = len(batch) >= BATCH_MAX or (batch and time.monotonic() - since >= BATCH_MAX_AGE_S)
                if (raw is None or full) and (batch or last_cursor not in (None, self.cursor)):
                    await self.on_batch(batch, last_cursor)
                    self.cursor, batch = last_cursor or self.cursor, []
                if raw is None:
                    self.watermark = self.clock() - wait
            if batch or last_cursor not in (None, self.cursor):
                await self.on_batch(batch, last_cursor)
                self.cursor = last_cursor or self.cursor
        finally:
            self.running = False
            if proc.returncode is None:
                proc.kill()
            await proc.wait()
        return delivered


def _skip_line(f) -> int | None:
    """Read past the rest of the current line. The bytes read up to and with its newline, or None when the
    line has no newline yet (the file position is then undefined; the caller stops)."""
    n = 0
    while chunk := f.read(READ_CHUNK_BYTES):
        nl = chunk.find(b"\n")
        if nl >= 0:
            f.seek(f.tell() - len(chunk) + nl + 1)
            return n + nl + 1
        n += len(chunk)
    return None


class GatewayFiles:
    """Reads new complete lines from the gateway's daily requests-*.jsonl files."""

    def __init__(self, directory: Path, offsets: dict[str, int],
                 on_records: Callable[[list, dict], Awaitable[None]], clock=time.time):
        self.dir, self.offsets, self.on_records, self.clock = Path(directory), dict(offsets), on_records, clock
        self.watermark = 0.0

    async def scan(self) -> int:
        started = self.clock()
        n = 0
        for path in sorted(self.dir.glob("requests-*.jsonl")):
            # One file whose batch fails (a locked database, a bug) must not keep the later days out.
            try:
                n += await self._read(path)
            except Exception as e:  # noqa: BLE001
                print(f"kiln-dash: {path.name} not read, trying again: {type(e).__name__}: {str(e)[:200]}",
                      flush=True)
        self.watermark = started
        return n

    async def _read(self, path: Path) -> int:
        offset = self.offsets.get(path.name, 0)
        try:
            size = path.stat().st_size
        except FileNotFoundError:
            return 0
        if size < offset:
            offset = 0  # replaced or truncated
        if size == offset:
            return 0
        n = 0
        with path.open("rb") as f:
            f.seek(offset)
            while offset < size:
                # Bounded chunks: a backlog of hundreds of MiB must not be read and parsed at once (kiln-dash
                # has MemoryMax=1G; release review H1).
                data = f.read(READ_CHUNK_BYTES)
                end = data.rfind(b"\n") + 1  # leave a partly written last line for next time
                if end == 0:
                    if len(data) < READ_CHUNK_BYTES:
                        break  # a partly written last line
                    skipped = _skip_line(f)  # one line longer than a chunk: no gateway record is this long
                    if skipped is None:
                        break
                    offset += len(data) + skipped
                    await self.on_records([], {path.name: offset})
                    self.offsets[path.name] = offset
                    print(f"kiln-dash: {path.name}: a line of more than {READ_CHUNK_BYTES} bytes skipped",
                          flush=True)
                    continue
                f.seek(offset + end)
                records = []
                for line in data[:end].splitlines():
                    try:
                        records.append(json.loads(line))
                    except ValueError:
                        continue
                del data
                await self.on_records(records, {path.name: offset + end})
                offset += end
                self.offsets[path.name] = offset  # only once the records are stored
                n += len(records)
        return n

    async def run(self) -> None:
        while True:
            await self.scan()
            await asyncio.sleep(POLL_S)


class GatewayFeed:
    """The gateway's live event WebSocket, reconnecting forever."""

    def __init__(self, url: str, on_event: Callable[[dict], Awaitable[None]],
                 on_disconnect: Callable[[], Awaitable[None]]):
        self.url, self.on_event, self.on_disconnect = url, on_event, on_disconnect
        self.connected = False

    async def run(self) -> None:
        async with aiohttp.ClientSession() as session:
            while True:
                try:
                    async with session.ws_connect(self.url, heartbeat=10) as ws:
                        self.connected = True
                        async for msg in ws:
                            if msg.type != aiohttp.WSMsgType.TEXT:
                                continue
                            try:
                                ev = json.loads(msg.data)
                            except ValueError:
                                continue
                            if isinstance(ev, dict):
                                await self.on_event(ev)
                except (aiohttp.ClientError, OSError, asyncio.TimeoutError):
                    pass
                if self.connected:
                    self.connected = False
                    await self.on_disconnect()
                await asyncio.sleep(1.0)
