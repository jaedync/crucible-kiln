"""Read-only queries on a worker thread (store/reader.py): the event loop keeps running while one waits."""
import asyncio
import sqlite3
import threading
import time

import pytest

from kiln.store.db import connect
from kiln.store.reader import Reader


@pytest.fixture
def db(tmp_path):
    path = tmp_path / "kiln.db"
    conn = connect(path)
    yield path, conn
    conn.close()


async def test_a_read_runs_on_its_own_thread_and_connection_and_sees_committed_writes(db):
    path, conn = db
    conn.execute("INSERT INTO engine_events (ts, host, profile, kind, detail) VALUES (1, 'h', NULL, 'k', 'd')")
    reader = Reader(path)
    try:
        def read(c):
            return threading.current_thread() is not threading.main_thread(), c is not conn, \
                c.execute("SELECT detail FROM engine_events").fetchone()["detail"]
        assert await reader.run(read) == (True, True, "d")
    finally:
        reader.close()


async def test_the_reader_cannot_write_and_errors_reach_the_caller(db):
    path, _ = db
    reader = Reader(path)
    try:
        with pytest.raises(sqlite3.OperationalError):
            await reader.run(lambda c: c.execute("DELETE FROM engine_events"))

        def bad(_c):
            raise ValueError("bad period")
        with pytest.raises(ValueError, match="bad period"):
            await reader.run(bad)
    finally:
        reader.close()


async def test_the_event_loop_keeps_running_while_a_read_waits(db):
    path, _ = db
    reader = Reader(path)
    beats = 0

    async def heartbeat():
        nonlocal beats
        while True:
            await asyncio.sleep(0.01)
            beats += 1
    hb = asyncio.create_task(heartbeat())
    try:
        t = time.monotonic()
        await reader.run(lambda c: time.sleep(0.5))  # a query that waits on a busy disk
        assert time.monotonic() - t >= 0.5
        assert beats >= 25  # ~50 if the loop never stopped; 0 if the read had run on it
    finally:
        hb.cancel()
        reader.close()
