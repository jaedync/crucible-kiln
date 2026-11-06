"""SQLite connection and numbered migrations.

kiln-dash is the only writer. Migrations are the SQL files in migrations/,
applied in order inside a transaction and recorded in schema_migrations.
"""
from __future__ import annotations

import sqlite3
import time
from importlib import resources
from pathlib import Path

from kiln.private_files import private_dir, private_file


def migrations() -> list[tuple[int, str, str]]:
    files = sorted(p for p in resources.files("kiln.store.migrations").iterdir() if p.name.endswith(".sql"))
    return [(int(p.name.split("_", 1)[0]), p.name, p.read_text()) for p in files]


def connect(path: Path | str) -> sqlite3.Connection:
    if str(path) != ":memory:":
        # Owner only: rows hold device names, addresses and user agents. SQLite gives -wal and -shm the
        # database file's mode.
        private_dir(Path(path).parent)
        private_file(Path(path))
    conn = sqlite3.connect(str(path), isolation_level=None, check_same_thread=False)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA foreign_keys=ON")
    conn.execute("PRAGMA synchronous=NORMAL")
    migrate(conn)
    return conn


def migrate(conn: sqlite3.Connection) -> list[str]:
    conn.execute("CREATE TABLE IF NOT EXISTS schema_migrations (version INTEGER PRIMARY KEY, name TEXT, applied REAL)")
    done = {r[0] for r in conn.execute("SELECT version FROM schema_migrations")}
    applied = []
    for version, name, sql in migrations():
        if version in done:
            continue
        conn.execute("BEGIN")
        try:
            # executescript() would commit midway; run statements one by one inside the transaction.
            for stmt in _statements(sql):
                conn.execute(stmt)
            conn.execute("INSERT INTO schema_migrations VALUES (?, ?, ?)", (version, name, time.time()))
            conn.execute("COMMIT")
        except Exception:
            conn.execute("ROLLBACK")
            raise
        applied.append(name)
    return applied


def _statements(sql: str) -> list[str]:
    # Migrations are plain DDL: no string literals contain '--' or ';'.
    lines = [l.split("--", 1)[0] for l in sql.splitlines()]
    return [s.strip() for s in "\n".join(lines).split(";") if s.strip()]
