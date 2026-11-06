"""Decode statistics in Crucible's crucible_turn line (Crucible wip/turn-stats 2ca773ad, server/metrics.py).

The fixture lines below are the exact output of that commit's turn_line() and request_line() for made-up
numbers (no request text exists in these lines).
"""
import json

import pytest

from kiln.collect import engines
from kiln.store import queries
from tests.test_crucible_ingest import UNIT, env  # noqa: F401 (env is a fixture)
from tests.test_ingest import gw_record, rows

# A batch turn with speculative decoding: 400 tokens in 40 rounds.
SPEC_LINE = ('crucible_turn {"request_id": "chatcmpl-a1", "prompt_tokens": 5000, "cached_tokens": 4096,'
             ' "prefill_tokens": null, "completion_tokens": 400, "queue_s": null, "prefill_s": null, "decode_s": 8.0,'
             ' "ttft_s": 1.5, "finish": "stop", "session_label": "hit_session", "cache_saved_tokens": 5400,'
             ' "cache_tail_tokens": 0, "cache_entries": 3, "cache_bytes": 1073741824, "cache_evictions": 0,'
             ' "replay_hit": false, "spec_rounds": 40, "draft_accepted": 360}')
# A think turn that the server closed: 90 + 36 tokens from the engine, 25 tokens of close text written at once.
CLOSED_LINE = ('crucible_turn {"request_id": "chatcmpl-b1", "prompt_tokens": 2000, "cached_tokens": null,'
               ' "prefill_tokens": null, "completion_tokens": 151, "queue_s": null, "prefill_s": null,'
               ' "decode_s": 30.0, "ttft_s": 1.0, "finish": "stop", "session_label": null, "spec_rounds": 42,'
               ' "draft_accepted": 84, "think_closed_at": 90, "think_closed_by": "answer_room",'
               ' "cont_request_id": "chatcmpl-b2", "cont_prompt_tokens": 2115, "cont_cached_tokens": 2000,'
               ' "cont_prefill_tokens": null, "cont_completion_tokens": 36, "cont_ms": 400.0,'
               ' "generated_tokens": 126, "forced_tokens": 25}')
# A serial-serve turn: no counts, as before the change.
OLD_LINE = ('crucible_turn {"request_id": "chatcmpl-c1", "prompt_tokens": 300, "cached_tokens": 0,'
            ' "prefill_tokens": null, "completion_tokens": 100, "queue_s": null, "prefill_s": null, "decode_s": 2.0,'
            ' "ttft_s": 0.2, "finish": "length", "session_label": "miss", "replay_hit": false}')
NEW_KEYS = ("spec_rounds", "draft_accepted", "draft_proposed", "generated_tokens", "forced_tokens")


def crucible_gw(rid, t0, prompt, completion, cached=0):
    return {**gw_record(rid, t0, completion=completion, prompt=prompt, cached=cached, events=completion),
            "profile": "crucible-flashnext", "engine": "crucible"}


def test_spec_counts_are_parsed():
    _, kind, rec = engines.parse(SPEC_LINE)
    assert kind == "completion"
    assert (rec["spec_rounds"], rec["draft_accepted"]) == (40, 360)
    assert "draft_proposed" not in rec and "generated_tokens" not in rec


def test_closed_think_turn_carries_generated_and_forced_tokens():
    _, _, rec = engines.parse(CLOSED_LINE)
    assert (rec["completion"], rec["generated_tokens"], rec["forced_tokens"]) == (151, 126, 25)
    assert (rec["spec_rounds"], rec["draft_accepted"]) == (42, 84)


def test_draft_proposed_is_read_when_a_later_server_sends_it():
    line = SPEC_LINE.replace('"draft_accepted": 360}', '"draft_accepted": 360, "draft_proposed": 480}')
    assert engines.parse(line)[2]["draft_proposed"] == 480


def test_an_old_line_parses_as_before():
    _, kind, rec = engines.parse(OLD_LINE)
    assert kind == "completion" and (rec["completion"], rec["decode_s"]) == (100, 2.0)
    assert not set(NEW_KEYS) & set(rec)


@pytest.mark.parametrize("bad", [{"spec_rounds": "40"}, {"draft_accepted": -1}, {"generated_tokens": 1.5},
                                 {"forced_tokens": True}, {"draft_proposed": [1]}])
def test_bad_counts_are_parse_errors(bad):
    d = json.loads(SPEC_LINE.split(" ", 1)[1])
    with pytest.raises(engines.LogFormatError):
        engines.parse("crucible_turn " + json.dumps({**d, **bad}))


def test_generated_tokens_above_the_completion_are_dropped_not_the_turn():
    # The server never writes this (it checks 0 <= generated <= completion); if a line has it, keep the turn.
    d = json.loads(CLOSED_LINE.split(" ", 1)[1])
    _, _, rec = engines.parse("crucible_turn " + json.dumps({**d, "generated_tokens": 152, "forced_tokens": 0}))
    assert rec["completion"] == 151 and "generated_tokens" not in rec and "forced_tokens" not in rec


def test_ingest_stores_the_counts_and_the_rate_uses_generated_tokens(env):
    ing, conn = env
    ing.gateway_record(crucible_gw("g1", 100.0, prompt=2000, completion=151))
    ing.engine_line(UNIT, CLOSED_LINE, 103.5)
    (r,) = rows(conn)
    assert (r["spec_rounds"], r["draft_accepted"], r["draft_proposed"]) == (42, 84, None)
    assert (r["engine_generated_tokens"], r["engine_forced_tokens"], r["engine_decode_s"]) == (126, 25, 30.0)
    (row,) = queries.recent_requests(conn)
    # 126 engine tokens in 30 s, not 151 / 30: the close text arrived at once.
    assert row["decode_tps"] == pytest.approx(4.2) and row["decode_estimated"] is False
    assert (row["spec_rounds"], row["draft_accepted"]) == (42, 84)


def test_old_lines_keep_the_old_rate(env):
    ing, conn = env
    ing.gateway_record(crucible_gw("g1", 100.0, prompt=300, completion=100))
    ing.engine_line(UNIT, OLD_LINE, 103.5)
    (r,) = rows(conn)
    assert r["engine_generated_tokens"] is None and r["spec_rounds"] is None
    assert queries.recent_requests(conn)[0]["decode_tps"] == pytest.approx(50.0)


def test_too_few_generated_tokens_give_no_rate(env):
    # The rate guard (MIN_RATE_TOKENS) applies to the tokens in the rate, not to the close text.
    ing, conn = env
    line = CLOSED_LINE.replace('"generated_tokens": 126, "forced_tokens": 25',
                               '"generated_tokens": 20, "forced_tokens": 131')
    ing.gateway_record(crucible_gw("g1", 100.0, prompt=2000, completion=151))
    ing.engine_line(UNIT, line, 103.5)
    assert queries.recent_requests(conn)[0]["decode_tps"] is None
