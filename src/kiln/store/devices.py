"""Configured devices ([devices] in kiln.toml) in the database.

Requests are keyed by the address they came from, so a machine that used its LAN address, its Wi-Fi address
and a link-local IPv6 address showed as three unnamed devices. Ingest stores a configured device's requests
under its first address. merge() moves rows stored before (or before an address was listed) to that key, the
way migration 003 moved label keys: series_1s has a client_key column, so equal seconds add up.
A move cannot be undone for series_1s (it has no peer column); requests keep peer_ip.
"""
from __future__ import annotations

import sqlite3

from kiln.config import Device


def merge(conn: sqlite3.Connection, devices: tuple[Device, ...]) -> int:
    """Move rows of each device's other addresses to its key and give its client row its name.
    Returns how many addresses had rows. Cheap when there is nothing to move: two indexed lookups per device."""
    moved = 0
    for dev in devices:
        # Never the key itself: moving its rows onto themselves would delete them (config refuses repeats too).
        others = tuple(a for a in dev.addresses[1:] if a != dev.key)
        marks = ", ".join("?" * len(others))
        present = [r[0] for r in conn.execute(
            f"SELECT key FROM clients WHERE key IN ({marks})"
            f" UNION SELECT DISTINCT client_key FROM requests WHERE client_key IN ({marks})", others * 2)] if others else []
        if present:
            _move(conn, dev.key, present)
            moved += len(present)
        conn.execute("UPDATE clients SET name = ? WHERE key = ?", (dev.name, dev.key))
    return moved


def _move(conn: sqlite3.Connection, key: str, old: list[str]) -> None:
    marks = ", ".join("?" * len(old))
    conn.execute("BEGIN")
    try:
        # The key may have no client row yet: it takes the row (and colour) of the address used most.
        if conn.execute("SELECT 1 FROM clients WHERE key = ?", (key,)).fetchone() is None:
            busiest = conn.execute(
                f"SELECT c.key FROM clients c WHERE c.key IN ({marks}) ORDER BY"
                " (SELECT COUNT(*) FROM requests r WHERE r.client_key = c.key) DESC, c.updated DESC LIMIT 1",
                old).fetchone()
            if busiest:  # else the next request makes the row and the colour
                conn.execute("UPDATE clients SET key = ? WHERE key = ?", (key, busiest[0]))
        # The newest update time of all of them (NULL only if none is known), and a node if the key has none.
        conn.execute(f"UPDATE clients SET updated = (SELECT MAX(updated) FROM clients WHERE key IN (?, {marks})),"
                     f" node = COALESCE(node, (SELECT node FROM clients WHERE key IN ({marks}) AND node IS NOT NULL"
                     " ORDER BY updated DESC LIMIT 1)) WHERE key = ?", (key, *old, *old, key))
        conn.execute(f"DELETE FROM clients WHERE key IN ({marks})", old)
        conn.execute(f"INSERT INTO series_1s (t, host, profile, model, client_key, decode, prefill)"
                     f" SELECT t, host, profile, model, ?, decode, prefill FROM series_1s WHERE client_key IN ({marks})"
                     " ON CONFLICT (t, host, profile, model, client_key)"
                     " DO UPDATE SET decode = decode + excluded.decode, prefill = prefill + excluded.prefill",
                     (key, *old))
        conn.execute(f"DELETE FROM series_1s WHERE client_key IN ({marks})", old)
        conn.execute(f"UPDATE requests SET client_key = ? WHERE client_key IN ({marks})", (key, *old))
        conn.execute("COMMIT")
    except Exception:
        conn.execute("ROLLBACK")
        raise
