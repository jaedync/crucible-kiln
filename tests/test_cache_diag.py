"""Cache diagnostics through the real Ingestor: prefix fields, re-read tokens, engine cache reports."""
import json

import pytest

from kiln.collect import engines
from tests.test_crucible_ingest import UNIT, env, turn_line  # noqa: F401 (fixture)
from tests.test_dash import dash_client  # noqa: F401 (fixture)
from tests.test_ingest import gw_record, rows

HALOGEN_LINE = ("serve_api: mtp 100 tok in 2.00s = 50.00 t/s | prompt 5000 (4000 cached, 80.0%), prefill 0.50s"
                " (1000 new) | detok 18us/tok | 40 tok with the head off | pool 9000/524288 2% | think on |"
                " closest earlier: 4000 tokens shared with a request 12s ago, then its prompt differs")


def crucible_record(rid="g1", t0=100.0, prefix=None, **kw):
    rec = {**gw_record(rid, t0, **kw), "profile": "crucible-flashnext", "engine": "crucible"}
    if prefix is not None:
        rec["prefix"] = prefix
    return rec


KEPT = {"conv": "a1b2c3d4e5f6", "n_messages": 6, "prefix_msgs_kept": 4, "prev_n_messages": 4,
        "system_changed": False, "tools_changed": False, "new_conversation": False,
        "prev_prompt": 4500, "prev_completion": 100}


def test_kept_prefix_gives_appended_and_reread(env):
    ing, conn = env
    # prompt 5000, cached 4000: 1000 computed; the client appended 5000 - 4500 = 500, so 500 were re-read.
    ing.gateway_record(crucible_record(prefix=KEPT))
    (r,) = rows(conn)
    assert (r["conv"], r["prefix_msgs_kept"], r["system_changed"], r["appended_tokens"], r["reread_tokens"]) == \
        ("a1b2c3d4e5f6", 4, 0, 500, 500)


def test_changed_prefix_has_no_reread(env):
    ing, conn = env
    ing.gateway_record(crucible_record(prefix={**KEPT, "prefix_msgs_kept": 2}))
    (r,) = rows(conn)
    assert r["appended_tokens"] is None and r["reread_tokens"] is None and r["prefix_msgs_kept"] == 2


def test_engine_cached_count_fills_in_the_math(env):
    ing, conn = env
    ing.gateway_record(crucible_record(prefix=KEPT, cached=None))
    assert rows(conn)[0]["reread_tokens"] is None
    ing.engine_line(UNIT, turn_line(), 103.5)  # cached_tokens 4000
    assert rows(conn)[0]["reread_tokens"] == 500


def test_crucible_cache_fields_are_stored(env):
    ing, conn = env
    ing.gateway_record(crucible_record(prefix=KEPT))
    line = turn_line(cache_saved_tokens=0, cache_tail_tokens=0, cache_save_refused="host", cache_entries=9,
                     cache_bytes=10 << 30, cache_evictions=3, host_free_mib=9830, slot_context=262144,
                     replay_hit=False, session_label="hit_session")
    ing.engine_line(UNIT, line, 103.5)
    r = rows(conn)[0]
    assert (r["cache_save_refused"], r["cache_entries"], r["cache_bytes"], r["host_free_mib"], r["slot_context"],
            r["replay_hit"], r["cache_session_label"]) == ("host", 9, 10 << 30, 9830, 262144, 0, "hit_session")


@pytest.mark.parametrize("reason", ["budget", "host", "protected", "resident", "policy", "memory"])
def test_every_refusal_reason_of_the_train14_server_is_stored(reason):
    # Crucible cache_status.h (train 14) added "protected" and "resident"; on Oct 8 each "resident" turn
    # became a parse error and its metrics were lost.
    _, _, rec = engines.parse(turn_line(cache_save_refused=reason))
    assert rec["cache_save_refused"] == reason


def test_an_unknown_refusal_identifier_is_stored_as_other_not_a_parse_error():
    # The server prints only fixed lowercase identifiers. A future one must not drop the turn's metrics.
    _, _, rec = engines.parse(turn_line(cache_save_refused="newreason"))
    assert rec["cache_save_refused"] == "other" and rec["completion"] == 100


@pytest.mark.parametrize("bad", [{"cache_save_refused": "Not An Id"}, {"cache_save_refused": 5},
                                 {"cache_save_refused": "x" * 40}, {"cache_entries": "9"}, {"host_free_mib": -1}])
def test_bad_cache_fields_are_parse_errors(bad):
    with pytest.raises(engines.LogFormatError):
        engines.parse(turn_line(**bad))


def test_unloggable_turn_is_ignored():
    assert engines.parse('crucible_turn {"request_id": 4, "error": "unloggable metrics"}') is None


@pytest.mark.parametrize("head", ["serve_api: mtp 0 tok in 0.00s = n/a", "serve_api: mtp 1 tok in 0.00s = n/a"])
def test_halogen_line_without_a_decode_rate_is_parsed(head):
    # Halogen prints "= n/a" when the decode time is 0 (no or one output token); seen 7 times on Oct 6-7.
    line = head + " | prompt 4139, prefill 1.20s = 3449 t/s | detok 18us/tok | pool 9000/524288 2% | think off"
    _, kind, rec = engines.parse(line)
    assert kind == "completion" and rec["prompt"] == 4139 and rec["decode_s"] == 0.0
    assert rec["completion"] == int(head.split()[2])


def test_halogen_closest_earlier_is_parsed():
    _, kind, rec = engines.parse(HALOGEN_LINE)
    assert kind == "completion"
    assert (rec["closest_shared_tokens"], rec["closest_age_s"], rec["closest_outcome"]) == (4000, 12.0, "prompt_differs")
    assert rec["prefill_tokens"] == 1000


@pytest.mark.parametrize("line,event", [
    ("kv pool: no room for 105728 positions; forgot the region at 171520 (3 cache entries, 0 chunks; the least"
     " recently used of 1 in the way)", "kv_evict"),
    ("kv pool: no room for 219392 positions; moved the 182156 rows this request resumes from, region 331008 ->"
     " 295168 (overlapping, copied in chunks) in 46.3 ms (no loss)", "kv_move"),
    ("kv pool: moved the 64 rows this request resumes from, region 114688 -> 289792 (1 entries, 0 chunks); the old"
     " region is free", "kv_move"),
    ("kv pool: the region at 6656 this request resumes from cannot grow and nothing holds a move; the turn runs in"
     " the 1000 positions it has left (max_tokens 32768 -> 1000)", "kv_no_grow"),
    ("prompt cache: at its 24-entry cap, forgot an older leaf of 900 tokens (region 3, the least recently used of"
     " its rank) to store a new entry", "cache_cap_evict"),
])
def test_halogen_cache_events_become_engine_events(make_cfg, line, event):
    from kiln.collect.ingest import Ingestor
    from kiln.pricing.cards import PriceBook
    from kiln.store.db import connect
    conn = connect(":memory:")
    ing = Ingestor(conn, make_cfg(), PriceBook(conn))
    assert engines.parse(line)[:2] == ("halogen", "cache_event") and engines.parse(line)[2]["event"] == event
    assert ing.engine_line("kiln-halogen-flashnext", line, 50.0) == []
    (e,) = conn.execute("SELECT profile, kind FROM engine_events").fetchall()
    assert tuple(e) == ("halogen-flashnext", f"cache:{event}")


def test_startup_pool_lines_are_not_events():
    assert engines.parse("kv pool: 524288 positions, ~26.8 GiB (12.4 GiB of it for everything but the pool)") is None


def test_raw_record_never_holds_content(env):
    ing, conn = env
    ing.gateway_record(crucible_record(prefix={**KEPT, "conv": "not a valid id!"}))
    r = rows(conn)[0]
    assert r["conv"] is None  # only the gateway's random hex ids are kept
    assert set(json.loads(r["raw"])["prefix"]) == set(KEPT)


async def test_cache_api_sessions_and_daily(dash_client):
    from tests.test_dash import NOW
    d, client = await dash_client()
    base = {**gw_record("a1", t0=NOW - 300), "prefix": {**KEPT, "new_conversation": True, "prefix_msgs_kept": 0,
                                                       "prev_n_messages": None, "prev_prompt": None,
                                                       "prev_completion": None}}
    miss = {**gw_record("a2", t0=NOW - 200, cached=0), "prefix": KEPT}  # kept prefix, nothing cached
    edit = {**gw_record("a3", t0=NOW - 100), "prefix": {**KEPT, "prefix_msgs_kept": 2}}
    await d._on_records([base, miss, edit], {"requests-x.jsonl": 1})
    res = await (await client.get("/api/v1/cache?hours=1")).json()
    (s,) = res["sessions"]
    # The list carries each conversation's sums; turns only for the conversations a reader has open.
    assert "turns" not in s and (s["n_turns"], s["flagged"], s["client_changes"], s["refusals"]) == (3, 1, 1, 0)
    assert s["reread"] == 4500 and s["last_prompt"] == 5000 and s["last"] == pytest.approx(NOW - 100)
    (s,) = (await (await client.get(f"/api/v1/cache?hours=1&open={s['conv']}")).json())["sessions"]
    assert [t["cause"] for t in s["turns"]] == ["new", "engine", "client"]
    assert s["turns"][1]["reread_tokens"] == 5000 - 500
    (day,) = res["daily"]
    assert (day["profile"], day["turns"], day["reread"], day["engine_misses"]) == ("halogen-flashnext", 3, 4500, 1)
    assert res["flag_tokens"] == 4096
    assert (await client.get("/api/v1/cache?hours=x")).status == 400
    assert (await client.get("/api/v1/cache?open=a;b")).status == 400


def test_halogen_cache_counters_keep_only_numbers():
    from kiln.dash.app import halogen_cache_counters
    raw = {"entries": 24, "max_entries": 24, "hits": 155, "misses": 7, "evicted": 35, "refused": 0,
           "disk": {"on": False}, "store_ms_total": 3198.2, "bytes": 2793351648, "x": True}
    assert halogen_cache_counters(raw) == {"entries": 24, "max_entries": 24, "hits": 155, "misses": 7, "evicted": 35,
                                           "refused": 0, "bytes": 2793351648}
    assert halogen_cache_counters([]) is None and halogen_cache_counters({}) is None


def test_engine_events_store_no_engine_text(make_cfg):
    from kiln.collect.ingest import Ingestor
    from kiln.pricing.cards import PriceBook
    from kiln.store.db import connect
    conn = connect(":memory:")
    ing = Ingestor(conn, make_cfg(), PriceBook(conn))
    ing.engine_line("kiln-halogen-flashnext", "kv pool: no room for 105728 positions; forgot the region at 171520"
                    " (3 cache entries, 0 chunks; the least recently used of 1 in the way) SECRET", 50.0)
    ing.engine_line("kiln-halogen-flashnext", "serve_api: mtp 5 tok in garbage SECRET", 51.0)
    details = [r[0] for r in conn.execute("SELECT detail FROM engine_events ORDER BY ts")]
    assert all("SECRET" not in d for d in details)
    assert json.loads(details[0]) == {"positions": 105728}
