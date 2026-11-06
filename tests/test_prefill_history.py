"""The prefill chart follows the engine's own chunk reports (collect/spread.py).

Two things went wrong with the rules before this one (the fixture shows both):

- Prefill was spread evenly over [first token - engine prefill seconds, first token]. Crucible prefills
  one request at a time but pauses a long prefill to run a short one first (shortest first), so both
  requests' intervals covered the same seconds and the chart counted that engine time twice: it showed
  2,050 tok/s at 17:01:06 and 2,180 at 17:02:20 for an engine that prefills at about 1,400 tok/s.
- Ten rows about 3 hours earlier carry a conversation-cache restore the engine logged as prefill
  work (118K to 122K tokens in 0.21 to 2.7 s): 45,000 to 570,000 tok/s in the chart.

The fixture holds synthetic rows: request rows and the engine's own crucible_prefill lines (numeric
and timing columns only). Its times are UTC.
"""
import json
from argparse import Namespace
from pathlib import Path

import pytest

from kiln.cli import _backfill_prefill
from kiln.collect import engines, spread
from kiln.collect.backfill import apply as apply_backfill
from kiln.collect.backfill import plan as plan_backfill
from kiln.collect.ingest import Ingestor
from kiln.live.apportion import apportion
from kiln.pricing.cards import PriceBook
from kiln.store import queries
from kiln.store.db import connect
from tests.test_compat import CRUCIBLE
from tests.test_ingest import H_LINE, gw_record

UNIT = "kiln-crucible-t20-long256"  # the profile name is the container name (config.Profile)
CLIENT = "device-1"
# The best solo prefill benchmark on strix (about 1,600 tok/s): one engine cannot do more in a second.
CHART_MAX = 1600
SEC_05, SEC_06, SEC_07 = 1768496465, 1768496466, 1768496467  # 2026-01-15 17:01:05-07 UTC
SEC_19, SEC_20, SEC_21 = 1768496539, 1768496540, 1768496541  # 2026-01-15 17:02:19-21 UTC
SPIKES = [f"row-spike-{i}" for i in range(1, 11)]

FIXTURE = json.loads((Path(__file__).parent / "fixtures" / "prefill_overlap.json").read_text())
ROWS = {r["id"]: r for r in FIXTURE["rows"]}


@pytest.fixture
def env(make_cfg):
    cfg = make_cfg(**{"profiles.crucible-t20-long256": CRUCIBLE})
    conn = connect(":memory:")
    book = PriceBook(conn)
    book.add("reference", "*", "anthropic/claude-sonnet-5.5", 2.0, 0.2, 10.0, now=0)
    ing = Ingestor(conn, cfg, book, clock=lambda: 1000.0)
    ing.watermarks = {"journal": 1e12, "gateway": 1e12}
    return ing, conn


def record(rid: str):
    """A gateway record for one fixture row. The record itself was not kept (it holds no content)."""
    r = ROWS[rid]
    return {**gw_record(rid, t0=r["ts_start"], completion=r["completion"], prompt=r["prompt"],
                        cached=r["cached"], events=r["completion"]),
            "ts_first_token": r["ts_first_token"], "ts_last_token": r["ts_last_token"], "ts_end": r["ts_end"],
            "profile": r["profile"], "engine": r["engine"], "model": r["model"], "tokens_by_s": None,
            "ttft_s": r["ts_first_token"] - r["ts_start"], "latency_s": r["ts_end"] - r["ts_start"],
            "usage": {"prompt": r["prompt"], "cached": r["cached"], "completion": r["completion"],
                      "reasoning": None}}


def chunk_lines(rid: str):
    """The engine's crucible_prefill lines for one fixture row: (journal time, line)."""
    r = ROWS[rid]
    for ts, elapsed, done in FIXTURE["chunks"][r["engine_req"]]:
        yield ts, "crucible_prefill " + json.dumps({"request_id": int(r["engine_req"]), "done": done,
                                                    "total": r["prompt"], "elapsed_s": elapsed})


def turn_line(rid: str):
    """The engine's crucible_turn line for one fixture row, as it arrived right after the request."""
    r = ROWS[rid]
    return "crucible_turn " + json.dumps({"request_id": int(r["engine_req"]), "prompt_tokens": r["prompt"],
                                          "cached_tokens": r["cached"], "prefill_tokens": r["engine_prefill_tokens"],
                                          "completion_tokens": r["completion"], "queue_s": r["engine_queue_s"],
                                          "prefill_s": r["engine_prefill_s"], "decode_s": r["engine_decode_s"],
                                          "finish": "stop"})


def ingest_fixture(ing, order=("row-A-402", "row-B-403", "row-C-413", "row-D-414")):
    """Feed the historical rows in the order the dashboard saw them: records, progress, then turns."""
    for rid in order:
        ing.gateway_record(record(rid))
    for rid in order:
        for ts, line in chunk_lines(rid):
            ing.engine_line(UNIT, line, ts)
    for rid in sorted(order, key=lambda i: ROWS[i]["ts_end"]):
        ing.engine_line(UNIT, turn_line(rid), ROWS[rid]["ts_end"])


def sec_of(ts: float) -> int:
    return int(ts // 1)


def prefill_by_second(conn):
    return {r["t"]: r["n"] for r in conn.execute("SELECT t, SUM(prefill) AS n FROM series_1s GROUP BY t") if r["n"]}


def even_pairs(rid: str):
    """The old rule: the whole prefill spread over [first token - prefill seconds, first token]."""
    r = ROWS[rid]
    return apportion(max(r["ts_start"], r["ts_first_token"] - r["engine_prefill_s"]), r["ts_first_token"],
                     r["engine_prefill_tokens"])


def put(conn, rid: str, client: str = CLIENT, series_done: int = 1):
    """One fixture row as the engine's join leaves it (without its chunk reports)."""
    row = {**ROWS[rid], "client_key": client, "series_done": series_done}
    row.pop("engine_req", None)
    cols = list(row)
    conn.execute(f"INSERT INTO requests ({', '.join(cols)}) VALUES ({', '.join('?' * len(cols))})",
                 [row[c] for c in cols])


def attach(conn, rid: str):
    """Write one fixture row's engine chunk reports onto it, as ingest does at the join."""
    r = ROWS[rid]
    chunks = spread.chunks_from_reports([tuple(c) for c in FIXTURE["chunks"][r["engine_req"]]], r["cached"],
                                        r["engine_prefill_tokens"])
    conn.execute("UPDATE requests SET prefill_progress = ? WHERE id = ?",
                 (json.dumps([[round(c.end, 3), round(c.end - c.start, 3), c.tokens] for c in chunks]), rid))


# -- the two requests that shared one engine second ------------------------------------------------

def test_a_paused_prefill_does_not_count_the_engine_second_twice(env):
    ing, conn = env
    ingest_fixture(ing)
    s = prefill_by_second(conn)
    # Request A (49,311 tokens, 35.6 s) is paused at 17:01:05.6 for B (845 tokens, 1.15 s) and resumes
    # after 17:01:06.9. Both even spreads covered second 17:01:06: the engine looked 2,050 tok/s fast.
    old = {sec: n for sec, n in even_pairs("row-A-402")}
    old_b = dict(even_pairs("row-B-403"))
    assert old[SEC_06] + old_b[SEC_06] == 2050 and old[SEC_05] + old_b[SEC_05] == 1565
    assert (s[SEC_05], s[SEC_06], s[SEC_07]) == (1046, 790, 1356)
    # Request C (56,701 tokens, 40.6 s) is paused for D (1,241 tokens, 1.6 s) inside 17:02:19-21.
    old_c = dict(even_pairs("row-C-413"))
    old_d = dict(even_pairs("row-D-414"))
    assert old_c[SEC_20] + old_d[SEC_20] == 2180
    assert (s[SEC_19], s[SEC_20], s[SEC_21]) == (998, 814, 1392)
    # Nothing is lost, and no second of that engine is faster than it can prefill.
    assert sum(s[t] for t in range(SEC_05, SEC_07 + 1)) == 3192
    assert max(n for t, n in s.items() if SEC_05 <= t <= SEC_07) <= CHART_MAX * 0.95
    assert max(n for t, n in s.items() if SEC_19 <= t <= SEC_21) <= CHART_MAX * 0.95


def test_the_joined_rows_hold_the_engines_chunk_windows(env):
    ing, conn = env
    ingest_fixture(ing)
    (row,) = conn.execute("SELECT prefill_progress, engine_req FROM requests WHERE prompt = 49311")
    chunks = spread.stored_chunks(row)
    # 12 full chunks of 4,096 tokens and the short last one, ending when the engine logged each.
    assert [c.tokens for c in chunks][:3] == [4096, 4096, 4096]
    assert sum(c.tokens for c in chunks) == 49311 and len(chunks) == 13
    assert (row["engine_req"], chunks[-1].end) == ("402", FIXTURE["chunks"]["402"][-1][0])
    assert conn.execute("SELECT COUNT(*) FROM prefill_chunks").fetchone()[0] == 0  # attached, then removed


def test_building_the_history_from_the_rows_matches_what_ingest_wrote(env):
    ing, conn = env
    ingest_fixture(ing)
    before = dict(conn.execute("SELECT t, SUM(prefill) FROM series_1s GROUP BY t").fetchall())
    spread.rebuild_series(conn, 20_000.0)
    assert dict(conn.execute("SELECT t, SUM(prefill) FROM series_1s GROUP BY t").fetchall()) == before
    assert sum(before.values()) == 49311 + 845 + 56701 + 1241


def test_the_overlap_rule_does_not_depend_on_which_turn_line_joins_first(env):
    ing, conn = env
    ingest_fixture(ing, order=("row-A-402", "row-B-403"))
    natural = dict(conn.execute("SELECT t, SUM(prefill) FROM series_1s GROUP BY t").fetchall())
    conn.execute("DELETE FROM series_1s")
    conn.execute("DELETE FROM requests")
    ing.settle(1e12)  # clear the staging table too
    ing.gateway_record(record("row-A-402"))
    ing.gateway_record(record("row-B-403"))
    for rid in ("row-A-402", "row-B-403"):
        for ts, line in chunk_lines(rid):
            ing.engine_line(UNIT, line, ts)
    for rid in ("row-B-403", "row-A-402"):  # B's turn line first, then A's
        ing.engine_line(UNIT, turn_line(rid), ROWS[rid]["ts_end"])
    assert dict(conn.execute("SELECT t, SUM(prefill) FROM series_1s GROUP BY t").fetchall()) == natural


def test_progress_reports_place_the_tokens_when_the_engine_ran_each_chunk(env):
    ing, conn = env
    ing.gateway_record({**gw_record("g1", t0=100.0, completion=100, prompt=12288, cached=0, events=100),
                        "ts_first_token": 112.0, "ts_last_token": 116.0, "ts_end": 116.5, "tokens_by_s": None,
                        "profile": "crucible-t20-long256", "engine": "crucible", "model": "qwen3.8-flash-next"})
    for ts, elapsed, done in ((103.0, 2.5, 4096), (108.0, 7.5, 8192), (109.0, 8.5, 12288)):
        ing.engine_line(UNIT, "crucible_prefill " + json.dumps({"request_id": 7, "done": done, "total": 12288,
                                                               "elapsed_s": elapsed}), ts)
    ing.engine_line(UNIT, "crucible_turn " + json.dumps({"request_id": 7, "prompt_tokens": 12288,
                                                         "cached_tokens": 0, "prefill_tokens": 12288,
                                                         "completion_tokens": 100, "prefill_s": 8.5,
                                                         "decode_s": 4.5, "finish": "stop"}), 116.5)
    s = prefill_by_second(conn)
    # The chunks ran in [100.5, 103.0], [103.0, 108.0] and [108.0, 109.0]. An even spread over the wait
    # for the first token (103.5 to 112.0, 1,445 tok/s) would put tokens in seconds where the engine
    # was doing nothing: it finished this prompt at 109.0.
    assert (s[100], s[101], s[102]) == (819, 1639, 1638)
    assert (s[103], s[104], s[107]) == (819, 819, 819)
    assert s[108] == 4096 and 111 not in s
    assert sum(s.values()) == 12288


def test_a_halogen_prompt_attaches_its_reports_by_the_prompt_size_it_names(env):
    # Halogen numbers its progress lines but not its completion line, so the request's own prompt size
    # (with its prefill window) is what links them. Nothing else in the window may name the same size.
    ing, conn = env
    ing.gateway_record({**gw_record("g2", t0=200.0, completion=100, prompt=5000, cached=4000, events=100),
                        "ts_first_token": 202.2, "ts_last_token": 204.0, "ts_end": 205.0, "tokens_by_s": None,
                        "profile": "halogen-flashnext", "engine": "halogen", "model": "qwen3.8-flash-next",
                        "usage": {"prompt": 5000, "cached": 4000, "completion": 100, "reasoning": None}})
    ing.engine_line("kiln-halogen-flashnext", "flash_serve: req 5 prefill 4096/5000 tokens, 1.5 s", 200.7)
    ing.engine_line("kiln-halogen-flashnext", "flash_serve: req 5 prefill 5000/5000 tokens, 2.2 s", 201.4)
    line = H_LINE.replace("prefill 0.50s", "prefill 2.20s")
    ing.engine_line("kiln-halogen-flashnext", line, 205.0)
    s = prefill_by_second(conn)
    # 96 tokens (the head chunk after 4,000 cached) end at 200.7, 904 end at 201.4. The wait for the
    # first token (200.0 to 202.2) would have spread them evenly instead.
    assert (s[200], s[201]) == (96 + 387, 517) and 202 not in s
    assert sum(s.values()) == 1000


def test_an_engine_line_that_arrives_before_its_gateway_record_still_keeps_its_chunks(env):
    # The engine can log a whole short request before the gateway's record reaches the dashboard. The
    # row then joins a pending engine row, which has to carry the engine's request id and reports too,
    # or the prefill falls back to the even spread over the wait for the first token.
    ing, conn = env
    rid = "row-B-403"
    for ts, line in chunk_lines(rid):
        ing.engine_line(UNIT, line, ts)
    ing.engine_line(UNIT, turn_line(rid), ROWS[rid]["ts_end"])
    ing.gateway_record(record(rid))
    r = conn.execute("SELECT engine_req, prefill_progress FROM requests WHERE id = ?", (rid,)).fetchone()
    assert r["engine_req"] == "403" and spread.stored_chunks(r)
    s = prefill_by_second(conn)
    # 8 tokens (its small head chunk) end at 17:01:05.756, the other 837 at 17:01:06.903.
    assert (s[SEC_05], s[SEC_06]) == (186, 659)
    assert sec_of(ROWS[rid]["ts_start"]) not in s  # nothing before the request was recorded


# -- impossible prefill figures --------------------------------------------------------------------

def rate_turn(prompt=118408, prefill=118408, prefill_s=0.6889):
    """The engine's line for one of the cache restores it misreported as prefill work."""
    return "crucible_turn " + json.dumps({"request_id": 9, "prompt_tokens": prompt, "cached_tokens": 0,
                                          "prefill_tokens": prefill, "completion_tokens": 1, "queue_s": 0.01,
                                          "prefill_s": prefill_s, "decode_s": 0.0, "finish": "stop"})


def test_a_restore_spike_is_kept_but_leaves_the_chart_and_the_rates_alone(env, capsys):
    ing, conn = env
    line = rate_turn()
    assert ing.engine_line(UNIT, line, 100.0) == []  # no rate hint to learn from
    notes = ing.settle(200.0)  # no gateway record: it becomes a direct row, as the real ones did
    (row,) = conn.execute("SELECT id, source, engine_prefill_tokens FROM requests")
    assert (row["source"], row["engine_prefill_tokens"]) == ("direct", 118408)  # the row is kept
    assert ("prefill_impossible", row["id"]) in notes and ing.skipped_prefill == 1
    assert "over the 20,000 tok/s limit" in capsys.readouterr().out
    assert prefill_by_second(conn) == {} and queries.recent_prefill_rates(conn) == {}


def test_the_restore_rows_do_not_reach_the_rebuilt_history_or_the_medians():
    conn = connect(":memory:")
    for rid in SPIKES:
        put(conn, rid)
    put(conn, "row-A-402")  # a real prefill of 49,311 tokens in 35.6 s stays
    attach(conn, "row-A-402")
    spread.rebuild_series(conn)
    assert queries.recent_prefill_rates(conn) == {"crucible-t20-long256": pytest.approx(49311 / 35.6043)}
    assert sum(prefill_by_second(conn).values()) == 49311
    assert max(prefill_by_second(conn).values()) <= CHART_MAX
    # A limit of its own can bring them back: the figures are stored, only the chart leaves them out.
    assert spread.prefill_pairs(conn.execute("SELECT * FROM requests WHERE id = ?", (SPIKES[0],)).fetchone(),
                                max_tps=1e9)


# -- the backfill tool -----------------------------------------------------------------------------

def test_the_backfill_dry_run_reports_the_change_and_writes_nothing():
    conn = connect(":memory:")
    put(conn, "row-A-402")
    put(conn, "row-B-403")
    attach(conn, "row-A-402")
    attach(conn, "row-B-403")
    # What a chart drawn with the old rules holds: every second of both even spreads.
    for rid in ("row-A-402", "row-B-403"):
        spread.add_series(conn, "strix", "prefill", even_pairs(rid), "crucible-t20-long256",
                          "qwen3.8-flash-next", CLIENT)
    conn.execute("UPDATE series_1s SET decode = 7 WHERE t = ?", (SEC_05,))
    before = {tuple(r) for r in conn.execute("SELECT t, prefill, decode FROM series_1s")}
    changes = plan_backfill(conn, SEC_05, SEC_07, 20_000.0)
    assert {c.t: (c.was, c.now) for c in changes} == {SEC_05: (1565, 1046), SEC_06: (2050, 790),
                                                      SEC_07: (1385, 1356)}
    # A dry run writes nothing at all.
    assert {tuple(r) for r in conn.execute("SELECT t, prefill, decode FROM series_1s")} == before
    assert conn.execute("SELECT prefill FROM series_1s WHERE t = ?", (SEC_05,)).fetchone()[0] == 1565
    apply_backfill(conn, changes)
    applied = prefill_by_second(conn)
    assert {t: applied[t] for t in (SEC_05, SEC_06, SEC_07)} == {SEC_05: 1046, SEC_06: 790, SEC_07: 1356}
    assert applied[SEC_05 + 5] == dict(even_pairs("row-A-402"))[SEC_05 + 5]  # outside the range: untouched
    assert conn.execute("SELECT decode FROM series_1s WHERE t = ?", (SEC_05,)).fetchone()[0] == 7  # untouched
    spread.rebuild_series(conn)
    rebuilt = prefill_by_second(conn)
    assert {t: rebuilt[t] for t in (SEC_05, SEC_06, SEC_07)} == {SEC_05: 1046, SEC_06: 790, SEC_07: 1356}


def test_the_backfill_command_prints_a_dry_run(make_cfg, capsys):
    cfg = make_cfg()
    conn = connect(cfg.db_path)
    put(conn, "row-A-402")
    attach(conn, "row-A-402")
    conn.execute("INSERT INTO series_1s (t, host, profile, model, client_key, prefill) VALUES (?, 'strix',"
                 " 'crucible-t20-long256', 'qwen3.8-flash-next', ?, 4000000)", (SEC_06, CLIENT))
    args = Namespace(since="2026-01-15T17:01:00+00:00", until="2026-01-15T17:02:00+00:00", dry_run=True)
    assert _backfill_prefill(cfg, args) == 0
    out = capsys.readouterr().out
    assert "crucible-t20-long256" in out and "4,000,000 ->" in out and "(dry run: nothing written)" in out
    assert conn.execute("SELECT prefill FROM series_1s WHERE t = ?", (SEC_06,)).fetchone()[0] == 4000000


def test_prefill_median_ignores_a_restore_and_a_missing_engine_time():
    conn = connect(":memory:")
    for rid in SPIKES:
        put(conn, rid)
    assert queries.recent_prefill_rates(conn) == {}  # ten restores say nothing about the engine
    put(conn, "row-A-402")
    assert queries.recent_prefill_rates(conn) == {"crucible-t20-long256": pytest.approx(49311 / 35.6043)}

    def figure(tokens, secs):
        conn.execute("UPDATE requests SET engine_prefill_tokens = ?, engine_prefill_s = ? WHERE id = ?",
                     (tokens, secs, "row-A-402"))
    figure(49311, 0.0)  # no seconds: not a rate
    assert queries.recent_prefill_rates(conn) == {}
    figure(118408, 0.6889)  # a restore: 171,880 tok/s, over the limit
    assert queries.recent_prefill_rates(conn) == {}
    figure(8192, 2.0)
    assert queries.recent_prefill_rates(conn) == {"crucible-t20-long256": 4096.0}


def test_the_engine_number_matching_uses_the_engines_own_id():
    from kiln.collect.ingest import engine_key
    assert [engine_key(v) for v in (7, "402", "chatcmpl-1", None, True, "x" * 5000)] == \
        ["7", "402", "chatcmpl-1", None, None, None]
    assert engines.parse('crucible_prefill {"request_id": 402, "done": 1, "total": 2, "elapsed_s": 1.0}')[2]["req"] \
        == 402
