"""Records and the database are created owner-only (security review L6)."""
import os
import stat

import pytest

from kiln.gateway.records import RecordLog
from kiln.store.db import connect


@pytest.fixture(autouse=True)
def umask():
    old = os.umask(0o022)
    yield
    os.umask(old)


def mode(p):
    return stat.S_IMODE(os.stat(p).st_mode)


def test_gateway_records_are_owner_only(tmp_path):
    log = RecordLog(tmp_path / "share" / "kiln" / "gateway")
    path = log.write({"id": "a", "ts_start": 1_790_000_000.0})
    assert mode(tmp_path / "share") == 0o700 and mode(tmp_path / "share" / "kiln" / "gateway") == 0o700
    assert mode(path) == 0o600
    log.write({"id": "b", "ts_start": 1_790_000_000.0})
    assert path.read_text().count("\n") == 2


def test_the_database_is_owner_only(tmp_path):
    db = tmp_path / "data" / "kiln.db"
    connect(db).execute("CREATE TABLE IF NOT EXISTS x (a)")
    assert mode(tmp_path / "data") == 0o700
    for p in (db, db.with_name("kiln.db-wal"), db.with_name("kiln.db-shm")):
        if p.exists():
            assert mode(p) == 0o600, p


def test_an_existing_directory_keeps_its_mode(tmp_path):
    d = tmp_path / "gw"
    d.mkdir(mode=0o750)
    os.chmod(d, 0o750)
    RecordLog(d)
    assert mode(d) == 0o750
