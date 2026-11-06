"""Read-only queries on worker threads, each with its own connection.

kiln-dash serves ticks and the live feed from one event loop. A query that waits on the disk would stop
that loop: on a Strix Halo test machine the fleet's GPU jobs push the database's pages out of memory, and one rebuild of
today's usage waited 4-11 s on page reads. Here a query waits on a worker thread instead
(sqlite3 releases the GIL while SQLite works), and the loop goes on. In WAL mode readers see every
committed write and never block the writer.
"""
from __future__ import annotations

import asyncio
import sqlite3
import threading
from concurrent.futures import ThreadPoolExecutor
from functools import partial
from pathlib import Path

WORKERS = 2  # a slow history query must not hold back the today refresh for long


class Reader:
    def __init__(self, path: Path | str, workers: int = WORKERS):
        self.path = str(path)
        self._pool = ThreadPoolExecutor(max_workers=workers, thread_name_prefix="kiln-read")
        self._local = threading.local()
        self._conns: list[sqlite3.Connection] = []
        self._lock = threading.Lock()

    def _conn(self) -> sqlite3.Connection:
        conn = getattr(self._local, "conn", None)
        if conn is None:
            # Used only by this worker thread; close() runs after the pool has stopped, on another thread.
            conn = sqlite3.connect(self.path, isolation_level=None, check_same_thread=False)
            conn.row_factory = sqlite3.Row
            conn.execute("PRAGMA query_only=ON")
            self._local.conn = conn
            with self._lock:
                self._conns.append(conn)
        return conn

    def _call(self, fn, args, kwargs):
        return fn(self._conn(), *args, **kwargs)

    async def run(self, fn, *args, **kwargs):
        """fn(conn, *args, **kwargs) on a worker thread; its result or its exception."""
        return await asyncio.get_running_loop().run_in_executor(self._pool, partial(self._call, fn, args, kwargs))

    def close(self) -> None:
        self._pool.shutdown(wait=True)
        with self._lock:
            for conn in self._conns:
                conn.close()
            self._conns.clear()
