"""Crucible's per-turn and per-chunk stderr lines: parsing, joining to gateway records, live progress."""
import json

import pytest

from kiln.collect import engines
from kiln.collect.ingest import Ingestor
from kiln.collect.sources import journal_entry
from kiln.pricing.cards import PriceBook
from kiln.store.db import connect
from tests.test_compat import CRUCIBLE
from tests.test_ingest import gw_record, rows, series

UNIT = "kiln-crucible-flashnext"
TURN = {"request_id": "chatcmpl-1", "prompt_tokens": 5000, "cached_tokens": 4000, "prefill_tokens": 1000,
        "completion_tokens": 100, "queue_s": 0.05, "prefill_s": 0.5, "decode_s": 2.0, "ttft_s": 0.6,
        "finish": "stop", "session_label": "hit_session"}


def turn_line(**over):
    return "crucible_turn " + json.dumps({**TURN, **over})


@pytest.fixture
def env(make_cfg):
    cfg = make_cfg(**{"profiles.crucible-flashnext": CRUCIBLE})
    conn = connect(":memory:")
    book = PriceBook(conn)
    book.add("reference", "*", "anthropic/claude-sonnet-5.5", 2.0, 0.2, 10.0, now=0)
    ing = Ingestor(conn, cfg, book, clock=lambda: 1000.0)
    ing.watermarks = {"journal": 1e12, "gateway": 1e12}
    return ing, conn


def test_turn_line_fields():
    engine, kind, rec = engines.parse(turn_line())
    assert (engine, kind) == ("crucible", "completion")
    assert rec == {"req": "chatcmpl-1", "failed": False, "completion": 100, "prompt": 5000, "cached": 4000,
                   "prefill_tokens": 1000, "finish": "stop", "queue_s": 0.05, "prefill_s": 0.5, "decode_s": 2.0,
                   "ttft_s": 0.6, "session_label": "hit_session"}


def test_unknown_values_stay_unknown():
    _, _, rec = engines.parse(turn_line(prefill_tokens=None, prefill_s=None, decode_s=None, cached_tokens=None))
    assert rec["prefill_tokens"] == 5000 and rec["cached"] == 0  # derived from counts the engine did give
    assert "prefill_s" not in rec and "decode_s" not in rec


@pytest.mark.parametrize("line", [
    "crucible_turn {not json",
    "crucible_turn [1, 2]",
    turn_line(completion_tokens=None),
    turn_line(prompt_tokens=-1),
    turn_line(prefill_s="fast"),
])
def test_malformed_lines_are_errors_not_silence(line):
    with pytest.raises(engines.LogFormatError):
        engines.parse(line)


def test_other_server_output_is_ignored():
    for line in ('127.0.0.1 - - [15/Jan/2026 14:01:02] "POST /v1/chat/completions HTTP/1.1" 200 -',
                 "Crucible is ready at http://127.0.0.1:8790/v1", "prefill plan for 4096 rows built in 3.1 ms"):
        assert engines.parse(line) is None
        assert engines.parse(line, "crucible") is None


def test_native_unit_lines_reach_ingest_under_the_profile_container():
    raw = json.dumps({"MESSAGE": turn_line(), "_SYSTEMD_USER_UNIT": f"{UNIT}.service",
                      "__REALTIME_TIMESTAMP": "103500000", "__CURSOR": "c"}).encode()
    assert journal_entry(raw)[:3] == (UNIT, turn_line(), 103.5)


def test_turn_joins_the_gateway_record(env):
    ing, conn = env
    ing.gateway_record({**gw_record(), "profile": "crucible-flashnext", "engine": "crucible"})
    notes = ing.engine_line(UNIT, turn_line(), 103.5)
    (r,) = rows(conn)
    assert (r["source"], r["engine_prefill_tokens"], r["engine_prefill_s"], r["engine_decode_s"],
            r["engine_queue_s"]) == ("gateway", 1000, 0.5, 2.0, 0.05)
    assert series(conn, "prefill") == {100: 1000}
    assert ("request", "g1") in notes


def test_prefill_chunks_feed_live_progress(env):
    ing, _ = env
    line = 'crucible_prefill {"request_id": 41, "done": 8192, "total": 30000, "elapsed_s": 3.9}'
    # Crucible's own request number goes along: the live view pairs it with a stream (live/pairing.py).
    assert ing.engine_line(UNIT, line, 200.0) == [("progress", "crucible-flashnext", 8192, 30000, 3.9, 200.0, None, 41)]
    named = line.replace('41', '"chatcmpl-1"')
    assert ing.engine_line(UNIT, named, 200.5)[0][7] is None  # not a number: only the lone-prefill rule applies
    from kiln.collect.ingest import engine_number
    assert [engine_number(v) for v in ("12", 12, "²", "1" * 5000, True, None)] == [12, 12, None, None, None, None]
    unknown_total = 'crucible_prefill {"request_id": "x", "done": 4096, "total": null, "elapsed_s": 1.0}'
    assert ing.engine_line(UNIT, unknown_total, 201.0) == []  # no total, no progress bar


def test_a_continuation_jobs_prefill_line_is_no_progress_and_names_its_number(env):
    # Crucible train 23: the job that continues a think turn the server closed marks its prefill lines with the
    # first job's turn_request_id. It is no gateway request, so it gets no progress bar and no pairing.
    ing, conn = env
    line = 'crucible_prefill {"request_id": 52, "turn_request_id": 41, "done": 4096, "total": 9000, "elapsed_s": 1.2}'
    assert ing.engine_line(UNIT, line, 210.0) == [("continuation", "crucible-flashnext", 52)]
    assert conn.execute("SELECT done FROM prefill_chunks WHERE req = '52'").fetchone()[0] == 4096  # stored as before
    assert ing.engine_line(UNIT, line.replace("52", '"x"'), 210.5) == []  # no engine number: nothing to skip


def test_cold_turn_sets_the_prefill_rate_hint(env):
    ing, _ = env
    notes = ing.engine_line(UNIT, turn_line(prompt_tokens=40000, cached_tokens=0, prefill_tokens=40000,
                                            prefill_s=20.0), 300.0)
    assert ("prefill_rate", "crucible-flashnext", pytest.approx(2000.0)) in notes


# -- crucible_error lines (Crucible 17d9954 and the tool-call fallback) ---------------------------

SECRET = "SECRET-PROMPT-TEXT the user typed"


def error_line(**over):
    return "crucible_error " + json.dumps({"request_id": "chatcmpl-7", "status": 500, "streamed": False,
                                           "error": "The engine failed.", **over})


def error_rows(conn):
    return [dict(r) for r in conn.execute("SELECT * FROM crucible_errors ORDER BY ts")]


def test_error_line_keeps_numbers_and_flags_only():
    engine, kind, rec = engines.parse(error_line(streamed=True, status=200, error=SECRET))
    assert (engine, kind) == ("crucible", "error")
    assert rec == {"req": "chatcmpl-7", "status": 200, "streamed": True, "kind": "stream_error",
                   "completion_tokens": None}
    assert SECRET not in repr(rec)


def test_error_kind_comes_from_a_fixed_list_never_from_the_text():
    kinds = {}
    for name, over in {"server": {}, "stream": {"streamed": True}, "client": {"status": 400},
                       "fallback": {"status": 200, "error": "tool_call_fallback", "completion_tokens": 12},
                       "spoof": {"error": "tool_call_fallback", "status": 500}}.items():
        kinds[name] = engines.parse(error_line(**over))[2]["kind"]
    assert kinds == {"server": "server_error", "stream": "stream_error", "client": "client_error",
                     "fallback": "tool_call_fallback", "spoof": "server_error"}


def test_error_without_a_request_id_and_odd_ids():
    assert engines.parse(error_line(request_id=None))[2]["req"] is None
    assert engines.parse(error_line(request_id=SECRET))[2]["req"] is None  # not an id pattern: dropped
    assert engines.parse(error_line(request_id="x" * 200))[2]["req"] is None
    assert engines.parse(error_line(request_id="chatcmpl-7\n"))[2]["req"] is None
    assert engines.parse(error_line(request_id=17))[2]["req"] == "17"  # Crucible's batch server: an integer
    assert engines.parse(error_line(request_id=True))[2]["req"] is None
    assert engines.parse(error_line(request_id=-1))[2]["req"] is None


@pytest.mark.parametrize("line", [
    "crucible_error {not json",
    "crucible_error [1]",
    error_line(status="500"),
    error_line(status=None),
    error_line(streamed="yes"),
    error_line(status=99),
    error_line(completion_tokens=-1),
])
def test_malformed_error_lines_are_errors_not_silence(line):
    with pytest.raises(engines.LogFormatError):
        engines.parse(line)


def test_error_line_is_stored_without_the_message(env):
    ing, conn = env
    ing.engine_line(UNIT, error_line(error=SECRET, streamed=True), 150.0)
    (r,) = error_rows(conn)
    assert (r["ts"], r["host"], r["profile"], r["engine"], r["request_id"], r["status"], r["streamed"],
            r["kind"]) == (150.0, "test", "crucible-flashnext", "crucible", "chatcmpl-7", 500, 1, "stream_error")
    dump = json.dumps([dict(x) for x in conn.execute("SELECT * FROM crucible_errors")]
                      + [dict(x) for x in conn.execute("SELECT * FROM engine_events")])
    assert SECRET not in dump and "engine failed" not in dump


def test_replayed_error_line_counts_once(env):
    ing, conn = env
    ing.engine_line(UNIT, error_line(), 150.0)
    ing.engine_line(UNIT, error_line(), 150.0)
    assert len(error_rows(conn)) == 1


def test_fallback_line_is_stored_as_its_own_kind(env):
    ing, conn = env
    ing.engine_line(UNIT, error_line(status=200, error="tool_call_fallback", completion_tokens=40), 151.0)
    (r,) = error_rows(conn)
    assert (r["kind"], r["status"], r["completion_tokens"]) == ("tool_call_fallback", 200, 40)


def test_bad_error_line_becomes_a_fixed_parse_event(env):
    ing, conn = env
    assert ing.engine_line(UNIT, "crucible_error {broken " + SECRET, 152.0) == [("parse_error", UNIT)]
    assert error_rows(conn) == []
    (e,) = conn.execute("SELECT detail FROM engine_events WHERE kind = 'parse_error'").fetchall()
    assert SECRET not in e["detail"]


# -- crucible_decode lines (Crucible train 23) ---------------------------------------------------

def decode_line(**over):
    d = {"request_id": 41, "completion_tokens": 812, "spec_rounds": 301, "draft_proposed": 690,
         "draft_accepted": 511, "elapsed_s": 9.8, **over}
    return "crucible_decode " + json.dumps(d)


def test_decode_line_fields():
    engine, kind, rec = engines.parse(decode_line())
    assert (engine, kind) == ("crucible", "decode")
    assert rec == {"req": 41, "tokens": 812, "spec_rounds": 301, "draft_proposed": 690, "draft_accepted": 511,
                   "elapsed_s": 9.8}
    assert engines.detect_engine(decode_line()) == "crucible"


@pytest.mark.parametrize("bad", [{"draft_proposed": -1}, {"draft_accepted": 1.5}, {"spec_rounds": True}])
def test_a_decode_line_of_the_wrong_shape_is_a_parse_error(bad):
    # Like the other Crucible lines: a broken build shows as parse errors, not as a silent gap.
    with pytest.raises(engines.LogFormatError):
        engines.parse(decode_line(**bad))


@pytest.mark.parametrize("gone", ["draft_proposed", "draft_accepted", "spec_rounds"])
def test_a_decode_line_without_a_count_parses_and_shows_no_figure(env, gone):
    # As in a turn line, an absent or null count is unknown (an older engine, an early step), not an error.
    ing, _ = env
    for d in ({k: v for k, v in json.loads(decode_line()[len("crucible_decode "):]).items() if k != gone},
              {**json.loads(decode_line()[len("crucible_decode "):]), gone: None}):
        engine, kind, rec = engines.parse("crucible_decode " + json.dumps(d))
        assert (kind, rec[gone]) == ("decode", None)
        expected = [] if gone != "spec_rounds" else [("decode_drafts", "crucible-flashnext", 41, 690, 511, 300.0, 812,
                                                      9.8)]
        assert ing.engine_line(UNIT, "crucible_decode " + json.dumps(d), 300.0) == expected


def test_a_decode_line_with_more_accepted_than_proposed_is_dropped():
    assert engines.parse(decode_line(draft_accepted=691)) is None  # as live/drafts.py drops a turn like it


def test_decode_lines_feed_live_drafts_and_store_nothing(env):
    ing, conn = env
    note = ("decode_drafts", "crucible-flashnext", 41, 690, 511, 300.0, 812, 9.8)
    assert ing.engine_line(UNIT, decode_line(), 300.0) == [note]  # the first line also learns the container's engine
    before = conn.total_changes
    # Live only: a line a second for each decoding slot would be a write a second, for a figure that ends with
    # the turn (its crucible_turn line stores the final counts).
    assert ing.engine_line(UNIT, decode_line(), 300.0) == [note]
    assert conn.total_changes == before
    assert ing.engine_line(UNIT, decode_line(request_id="chatcmpl-1"), 300.5) == []  # no engine number to pair


def test_start_lines_and_turns_teach_the_engine_sizes_and_survive_a_restart(env, make_cfg):
    ing, conn = env
    ing.engine_line(UNIT, "PLE table on host: 39040187392 bytes not resident, 1 segment(s)", 900.0)
    ing.engine_line(UNIT, "weight payload 68997727232 bytes, arena reserved 69428838400 bytes, staging peak 1", 901.0)
    ing.engine_line(UNIT, turn_line(cache_bytes=152942144, cache_pinned_bytes=159383552), 910.0)
    assert ing.engine_facts(UNIT) == {"ple_bytes": 39040187392, "weights_bytes": 69428838400,
                                      "cache_bytes": 152942144, "cache_pinned_bytes": 159383552}
    again = Ingestor(conn, ing.cfg, ing.book, clock=lambda: 1000.0)  # kiln-dash restarted
    assert again.engine_facts(UNIT)["cache_bytes"] == 152942144
    # The engine restarts: its cache is empty until the first turn reports it.
    again.engine_line(UNIT, "weight payload 1 bytes, arena reserved 69428838400 bytes, staging peak 1", 950.0)
    assert "cache_bytes" not in again.engine_facts(UNIT) and "cache_pinned_bytes" not in again.engine_facts(UNIT)
    assert again.engine_facts(UNIT)["ple_bytes"] == 39040187392


def test_a_slot_size_without_a_fixed_part_drops_the_old_fixed_part(env):
    ing, _ = env
    ing.engine_line(UNIT, "sequence storage: raw_slots=16 logical_state_bytes=700 fixed_state_bytes=15", 900.0)
    assert ing.engine_facts(UNIT) == {"slot_state_bytes": 700, "slot_fixed_bytes": 15}
    ing.engine_line(UNIT, "sequence storage: raw_slots=16 logical_state_bytes=800", 950.0)  # a build without it
    assert ing.engine_facts(UNIT) == {"slot_state_bytes": 800}
