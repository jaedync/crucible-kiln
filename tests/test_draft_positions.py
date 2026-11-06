"""Per-position draft counters in Crucible's crucible_turn line (engine design note, final names).

The engine does not send these fields yet, so two rules matter here: a line without them must parse and
store exactly as before, and one bad field must cost that field alone, never the turn. The lines below
are made from the design note's field table, not from a server build.
"""
import json
import re

import pytest

from kiln.collect import engines
from kiln.store import queries
from kiln.store import db as store_db
from kiln.store.db import connect
from tests.test_crucible_ingest import UNIT, env, turn_line  # noqa: F401 (env is a fixture)
from tests.test_dash import NOW, dash_client  # noqa: F401 (dash_client is a fixture)
from tests.test_ingest import gw_record, rows
from tests.test_turn_stats import OLD_LINE, SPEC_LINE

K = 7
PROPOSED = [40, 30, 22, 15, 8, 4, 1]
TESTED = [40, 26, 18, 11, 5, 2, 1]
ACCEPTED = [32, 21, 15, 9, 4, 2, 1]
LEN_HIST = [2, 3, 5, 8, 9, 7, 4, 2]
FIELDS = ("draft_pos_proposed", "draft_pos_tested", "draft_pos_accepted", "draft_len_hist",
          "draft_passes", "draft_ms")
# One batch turn with speculation: the three lists sum to the counters the line already carries.
DRAFT = {"spec_rounds": 40, "draft_proposed": 120, "draft_accepted": 84,
         "draft_pos_proposed": PROPOSED, "draft_pos_tested": TESTED, "draft_pos_accepted": ACCEPTED,
         "draft_len_hist": LEN_HIST, "draft_passes": 12, "draft_ms": 3450.5}


def draft_line(**over):
    return turn_line(**{**DRAFT, **over})


def parsed(**over):
    return engines.parse(draft_line(**over))[2]


def test_all_six_fields_are_read():
    rec = parsed()
    assert {k: rec[k] for k in FIELDS} == {k: DRAFT[k] for k in FIELDS}
    # The older counters are still read beside them.
    assert (rec["spec_rounds"], rec["draft_proposed"], rec["draft_accepted"]) == (40, 120, 84)


def test_a_request_without_speculation_has_none_of_them():
    assert not set(FIELDS) & set(parsed(spec_rounds=None, draft_proposed=None, draft_accepted=None,
                                        **{k: None for k in FIELDS}))


@pytest.mark.parametrize("key, bad", [
    # wrong length, then one bad entry: a negative, a float, a bool, text
    ("draft_pos_proposed", PROPOSED[:6]), ("draft_pos_proposed", PROPOSED + [0]),
    ("draft_pos_proposed", [40, -1, 22, 15, 8, 4, 1]), ("draft_pos_proposed", [40, 30.0, 22, 15, 8, 4, 1]),
    ("draft_pos_proposed", [40, True, 22, 15, 8, 4, 1]), ("draft_pos_proposed", [40, "30", 22, 15, 8, 4, 1]),
    ("draft_pos_proposed", [40, 30, 22, 15, 8, 4, 2]),  # sum 121, not draft_proposed 120
    ("draft_pos_proposed", {}),
    ("draft_pos_tested", TESTED[:6]), ("draft_pos_tested", [40, -1, 18, 11, 5, 2, 1]),
    ("draft_pos_tested", [40, 31, 18, 11, 5, 2, 1]),  # tested 2 > proposed 2
    ("draft_pos_tested", [39, 26, 18, 11, 5, 2, 1]),  # tested 1 != proposed 1
    ("draft_pos_accepted", ACCEPTED + [0]), ("draft_pos_accepted", [32, 21, 15, 9, 4, 2, -1]),
    ("draft_pos_accepted", [33, 21, 15, 9, 4, 2, 1]),  # sum 85, not draft_accepted 84
    ("draft_pos_accepted", [32, 27, 15, 9, 4, 2, 1]),  # sum 84, but accepted 2 > tested 2
    ("draft_len_hist", LEN_HIST[:7]), ("draft_len_hist", LEN_HIST + [0]),
    ("draft_len_hist", [2, 3, 5, 8, 9, 7, 4, -2]),
    ("draft_len_hist", [2, 3, 5, 8, 9, 7, 4, 3]),  # sum 41 > spec_rounds 40
    ("draft_passes", -1), ("draft_passes", True), ("draft_passes", 1.5), ("draft_passes", "12"),
    ("draft_ms", -0.5), ("draft_ms", float("nan")), ("draft_ms", float("inf")), ("draft_ms", True),
    ("draft_ms", "120.5"),
])
def test_one_bad_field_is_dropped_and_the_turn_survives(key, bad):
    rec = parsed(**{key: bad})
    assert key not in rec
    assert (rec["completion"], rec["decode_s"], rec["draft_proposed"]) == (100, 2.0, 120)
    for other in FIELDS:
        if other != key:
            assert rec[other] == DRAFT[other]


def test_a_dropped_field_is_logged_once_at_debug(caplog):
    with caplog.at_level("DEBUG", logger="kiln.collect.engines"):
        parsed(draft_pos_proposed="nonsense")
    msgs = [r.getMessage() for r in caplog.records]
    assert len(msgs) == 1 and "draft_pos_proposed" in msgs[0]


def test_a_bad_list_leaves_the_checks_that_need_it_out_of_the_way():
    # With draft_pos_proposed dropped, accepted is still checked against tested, so it stays.
    rec = parsed(draft_pos_proposed=[1] * 7)
    assert "draft_pos_proposed" not in rec and rec["draft_pos_tested"] == TESTED
    assert rec["draft_pos_accepted"] == ACCEPTED
    # With tested dropped, accepted is checked against proposed instead, and drops there too.
    rec = parsed(draft_pos_tested=[39] * 7, draft_pos_accepted=[41, 20, 10, 7, 3, 2, 1])
    assert "draft_pos_tested" not in rec and "draft_pos_accepted" not in rec
    assert rec["draft_pos_proposed"] == PROPOSED


def test_an_old_line_parses_exactly_as_before():
    # SPEC_LINE and OLD_LINE are the exact output of the engine commit that added spec_rounds.
    _, kind, spec = engines.parse(SPEC_LINE)
    assert kind == "completion" and spec == {
        "req": "chatcmpl-a1", "failed": False, "completion": 400, "prompt": 5000, "cached": 4096,
        "prefill_tokens": 5000 - 4096, "finish": "stop", "ttft_s": 1.5, "decode_s": 8.0,
        "session_label": "hit_session", "cache_saved_tokens": 5400, "cache_tail_tokens": 0,
        "cache_entries": 3, "cache_bytes": 1073741824, "cache_evictions": 0, "replay_hit": False,
        "spec_rounds": 40, "draft_accepted": 360}
    assert not set(FIELDS) & set(spec)
    # The other fixture line too: a serial-serve turn, which has no decode counters at all.
    _, kind, old = engines.parse(OLD_LINE)
    assert kind == "completion" and (old["completion"], old["decode_s"]) == (100, 2.0)
    assert not set(FIELDS) & set(old) and "spec_rounds" not in old


def test_the_per_token_detail_counters_are_never_read():
    detail = {"draft_tally": [{"id": 4711, "n": 3}], "draft_miss": [{"id": 4711, "n": 1}],
              "draft_detail_cut": 7}
    assert set(detail) == set(engines.C_DRAFT_DETAIL)
    assert not set(detail) & set(parsed(**detail))


# -- storage ---------------------------------------------------------------------------------------

def crucible_gw(rid, t0, completion=100):
    return {**gw_record(rid, t0, completion=completion, prompt=5000, cached=4000, events=completion),
            "profile": "crucible-flashnext", "engine": "crucible"}


def test_migration_adds_the_columns_and_leaves_old_rows_null():
    conn = connect(":memory:")
    cols = {r["name"] for r in conn.execute("PRAGMA table_info(requests)")}
    assert set(FIELDS) <= cols
    applied = dict(conn.execute("SELECT version, name FROM schema_migrations"))
    assert applied[13] == "013_draft_positions.sql"
    conn.execute("INSERT INTO requests (id, source, host, ts_start) VALUES ('old', 'gateway', 'test', 0)")
    r = conn.execute("SELECT * FROM requests WHERE id = 'old'").fetchone()
    assert all(r[c] is None for c in FIELDS)


def test_upgrading_an_older_database_keeps_its_rows(tmp_path):
    # The real path: a populated database at 012 gets the six columns added at its next start.
    path = tmp_path / "kiln.db"
    conn = connect(path)
    conn.execute("DELETE FROM schema_migrations WHERE version = 13")
    for col in FIELDS:
        conn.execute(f"ALTER TABLE requests DROP COLUMN {col}")
    conn.execute("INSERT INTO requests (id, source, host, ts_start, draft_proposed) VALUES ('old', 'gateway',"
                 " 'test', 0, 60)")
    assert store_db.migrate(conn) == ["013_draft_positions.sql"]
    r = conn.execute("SELECT * FROM requests WHERE id = 'old'").fetchone()
    assert r["draft_proposed"] == 60 and all(r[c] is None for c in FIELDS)


def test_ingest_stores_the_lists_and_the_scalars(env):  # noqa: F811
    ing, conn = env
    ing.gateway_record(crucible_gw("g1", 100.0))
    ing.engine_line(UNIT, draft_line(), 103.5)
    (r,) = rows(conn)
    assert json.loads(r["draft_pos_proposed"]) == PROPOSED
    assert json.loads(r["draft_pos_tested"]) == TESTED
    assert json.loads(r["draft_pos_accepted"]) == ACCEPTED
    assert json.loads(r["draft_len_hist"]) == LEN_HIST
    assert (r["draft_passes"], r["draft_ms"]) == (12, 3450.5)
    # The request detail returns them as lists, not as text.
    row = queries.recent_requests(conn)[0]
    assert row["draft_pos_proposed"] == PROPOSED and row["draft_len_hist"] == LEN_HIST
    assert (row["draft_passes"], row["draft_ms"]) == (12, 3450.5)


def test_a_direct_request_keeps_them_too(env):  # noqa: F811
    ing, conn = env
    ing.engine_line(UNIT, draft_line(), 200.0)
    (r,) = rows(conn)
    assert r["source"] == "engine_pending" and json.loads(r["draft_pos_accepted"]) == ACCEPTED
    assert (r["draft_passes"], r["draft_ms"]) == (12, 3450.5)


def test_an_old_line_leaves_the_new_columns_null(env):  # noqa: F811
    ing, conn = env
    ing.gateway_record(crucible_gw("g1", 100.0, completion=400))
    ing.engine_line(UNIT, SPEC_LINE.replace("chatcmpl-a1", "g1"), 103.5)
    (r,) = rows(conn)
    assert all(r[c] is None for c in FIELDS) and r["spec_rounds"] == 40
    assert all(queries.recent_requests(conn)[0][c] is None for c in FIELDS)


def test_the_per_token_detail_counters_reach_no_table(env):  # noqa: F811
    ing, conn = env
    line = draft_line(draft_tally=[{"id": 4711, "n": 3, "tok": "hello"}], draft_miss=[{"id": 4711, "n": 1}],
                      draft_detail_cut=7)
    ing.gateway_record(crucible_gw("g1", 100.0))
    ing.engine_line(UNIT, line, 103.5)
    tables = [r["name"] for r in conn.execute("SELECT name FROM sqlite_master WHERE type = 'table'")]
    dump = "\n".join(str(tuple(r)) for t in tables for r in conn.execute(f"SELECT * FROM {t}"))
    assert "draft_tally" not in dump and "draft_miss" not in dump and "draft_detail_cut" not in dump
    # The token id as a whole number only: a timestamp such as 1768484711.0 holds the digits.
    assert not re.search(r"(?<![\d.])4711(?![\d.])", dump) and "hello" not in dump


# -- the aggregate query ---------------------------------------------------------------------------

def spec_row(conn, rid, ts, proposed, tested, accepted, hist, passes, ms, generated=100):
    conn.execute("INSERT INTO requests (id, source, host, ts_start, ts_end, client_key, profile, engine, completion,"
                 " spec_rounds, draft_proposed, draft_accepted, draft_passes, draft_ms,"
                 " engine_generated_tokens, draft_pos_proposed, draft_pos_tested, draft_pos_accepted,"
                 " draft_len_hist) VALUES (?, 'gateway', 'test', ?, ?, '127.0.0.1', 'crucible-flashnext',"
                 " 'crucible', ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                 (rid, ts, ts + 1, generated, sum(hist), sum(proposed), sum(accepted), passes, ms, generated,
                  json.dumps(proposed), json.dumps(tested), json.dumps(accepted), json.dumps(hist)))


def test_the_aggregate_sums_positions_and_computes_the_rates():
    conn = connect(":memory:")
    spec_row(conn, "s1", 100.0, [10, 8, 4, 2, 0, 0, 0], [10, 6, 2, 1, 0, 0, 0], [8, 4, 1, 0, 0, 0, 0],
              [1, 2, 3, 4, 0, 0, 0, 0], 5, 2000.0, generated=100)
    spec_row(conn, "s2", 200.0, [6, 3, 1, 0, 0, 0, 0], [6, 2, 0, 0, 0, 0, 0], [3, 1, 0, 0, 0, 0, 0],
              [2, 1, 1, 0, 0, 0, 0, 0], 3, 500.0, generated=40)
    (row,) = queries.draft_acceptance(conn, "all", now=1000.0)["rows"]
    assert row["profile"] == "crucible-flashnext" and row["engine"] == "crucible"
    assert row["requests"] == 2 and row["passes"] == 8
    assert row["proposed"] == [16, 11, 5, 2, 0, 0, 0]
    assert row["tested"] == [16, 8, 2, 1, 0, 0, 0]
    assert row["accepted"] == [11, 5, 1, 0, 0, 0, 0]
    # The conditional rate at each depth: accepted_i / tested_i over the sums, never the mean of rates.
    assert row["accept_rate"][0] == pytest.approx(11 / 16) and row["accept_rate"][1] == pytest.approx(5 / 8)
    assert row["accept_rate"][4:] == [None] * 3
    assert row["len_hist"] == [3, 3, 4, 4, 0, 0, 0, 0]
    assert row["draft_ms"] == 2500.0 and row["generated_tokens"] == 140
    assert row["ms_per_token"] == pytest.approx(2500.0 / 140, abs=0.001)


def test_the_aggregate_counts_completion_when_the_engine_split_is_absent():
    # The server writes generated_tokens only for a think turn that it closed; most rows have completion only.
    conn = connect(":memory:")
    spec_row(conn, "s1", 100.0, PROPOSED, TESTED, ACCEPTED, LEN_HIST, 12, 3450.5, generated=90)
    conn.execute("UPDATE requests SET engine_generated_tokens = NULL WHERE id = 's1'")
    row = queries.draft_acceptance(conn, "7d", now=1000.0)["rows"][0]
    assert row["generated_tokens"] == 90


def test_the_aggregate_counts_only_the_range_and_the_host_and_keeps_unknown_lists_out():
    conn = connect(":memory:")
    spec_row(conn, "s1", 100.0, PROPOSED, TESTED, ACCEPTED, LEN_HIST, 12, 3450.5)
    spec_row(conn, "s2", 100.0 + 3 * 86400, PROPOSED, TESTED, ACCEPTED, LEN_HIST, 12, 3450.5)
    conn.execute("UPDATE requests SET host = 'remote' WHERE id = 's2'")
    out = queries.draft_acceptance(conn, "7d", now=1000.0 + 3 * 86400, host="remote")
    assert [r["requests"] for r in out["rows"]] == [1] and out["host"] == "remote"
    # A range that starts after the older row drops it, and a host with no rows has none.
    assert [r["requests"] for r in queries.draft_acceptance(conn, "7d", now=100.0 + 8 * 86400)["rows"]] == [1]
    assert queries.draft_acceptance(conn, "all", now=1000.0, host="strix")["rows"] == []
    # draft_passes alone makes a row, with the lists empty rather than missing and no rate without tokens.
    conn.execute("INSERT INTO requests (id, source, host, ts_start, profile, engine, draft_passes)"
                 " VALUES ('s3', 'gateway', 'other', 100.0, 'crucible-flashnext', 'crucible', 4)")
    (only,) = queries.draft_acceptance(conn, "all", now=1000.0, host="other")["rows"]
    assert only["passes"] == 4 and only["rounds"] is None and only["ms_per_token"] is None
    assert only["proposed"] == [0] * K and only["accept_rate"] == [None] * K
    assert queries.draft_acceptance(conn, "all", now=1000.0)["positions"] == K
    with pytest.raises(ValueError):
        queries.draft_acceptance(conn, "nonsense", now=1000.0)


def test_a_broken_list_column_cannot_fail_the_aggregate():
    conn = connect(":memory:")
    spec_row(conn, "s1", 100.0, PROPOSED, TESTED, ACCEPTED, LEN_HIST, 12, 3450.5)
    conn.execute("UPDATE requests SET draft_pos_proposed = '{not json', draft_len_hist = '[1, 2]' WHERE id = 's1'")
    (row,) = queries.draft_acceptance(conn, "all", now=1000.0)["rows"]
    assert row["proposed"] == [0] * K and row["tested"] == TESTED
    assert row["len_hist"][:2] == [1, 2] and row["len_hist"][2:] == [0] * 6


# -- the API ---------------------------------------------------------------------------------------

async def test_the_speculation_endpoint_serves_the_sums_and_the_request_detail_the_lists(dash_client):
    d, client = await dash_client()
    spec_row(d.conn, "s1", NOW, PROPOSED, TESTED, ACCEPTED, LEN_HIST, 12, 3450.5)
    res = await (await client.get("/api/v1/speculation?period=all")).json()
    (row,) = res["rows"]
    assert (res["period"], res["positions"], row["profile"]) == ("all", K, "crucible-flashnext")
    assert row["accept_rate"][0] == pytest.approx(32 / 40) and row["len_hist"] == LEN_HIST
    assert (await client.get("/api/v1/speculation?period=nonsense")).status == 400
    assert (await client.get("/api/v1/speculation?host=nowhere")).status == 400
    detail = await (await client.get("/api/v1/requests?limit=1")).json()
    assert detail["rows"][0]["draft_pos_proposed"] == PROPOSED
    assert detail["rows"][0]["draft_len_hist"] == LEN_HIST
