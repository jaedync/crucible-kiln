import pytest

from kiln.live.pairing import RETIRED_S
from kiln.live.rates import ENGINE_FRESH_S, WINDOW_S, LiveState


def start(live, rid="a", ts=100.0, profile="p"):
    live.on_event({"type": "req.start", "id": rid, "ts": ts, "client": {"key": "c"}, "profile": profile, "model": "m"})


def tokens(live, rid, times, total):
    live.on_event({"type": "req.tokens", "id": rid, "t": times, "total": total})


def test_rate_counts_only_the_last_window_and_uses_true_elapsed_time():
    live = LiveState(clock=lambda: 0)
    start(live)
    live.on_event({"type": "req.first_token", "id": "a", "ts": 101.0})
    tokens(live, "a", [101.01 + i * 0.02 for i in range(50)], 50)  # 50 tokens/s for one second
    # 1 s after the first token: window is only 1 s old, so divide by 1 s, not 2 s
    assert live.decode_tps(102.0) == pytest.approx(50 / 1.0)
    tokens(live, "a", [102.01 + i * 0.02 for i in range(50)], 100)
    # Now the stream is 2 s old and the window (101.0, 103.0] holds all 100 tokens.
    assert live.decode_tps(103.0) == pytest.approx(100 / WINDOW_S)


def test_rate_drops_to_zero_the_moment_the_stream_ends():
    live = LiveState(clock=lambda: 0)
    start(live)
    live.on_event({"type": "req.first_token", "id": "a", "ts": 101.0})
    tokens(live, "a", [101.0, 101.5, 101.9], 3)
    assert live.decode_tps(102.0) > 0
    live.on_event({"type": "req.end", "id": "a", "record": {}})
    assert live.decode_tps(102.0) == 0.0


def test_streams_sum_and_prefill_streams_contribute_zero():
    live = LiveState(clock=lambda: 0)
    start(live, "a")
    start(live, "b")
    live.on_event({"type": "req.first_token", "id": "a", "ts": 100.0})
    tokens(live, "a", [100.5 + i * 0.05 for i in range(30)], 30)
    v = live.view(102.0)
    assert v["in_flight"] == 2
    assert [s["phase"] for s in v["streams"]] == ["decode", "prefill"]
    assert v["decode_tps"] == pytest.approx(30 / 2.0)


def test_per_second_history_is_exact_and_emitted_once():
    live = LiveState(clock=lambda: 100.0)
    start(live)
    tokens(live, "a", [100.1, 100.9, 101.0, 101.2, 101.99], 5)
    first = live.completed_seconds(102.5)
    assert {s["t"]: s["decode"] for s in first} == {100: 2, 101: 3}
    assert live.completed_seconds(102.9) == []  # nothing new completed
    assert live.completed_seconds(103.1) == []  # second 102 waits EMIT_LAG_S for late batches
    assert [s["decode"] for s in live.completed_seconds(103.5)] == [0]


def test_exactness_needs_matching_counts_on_every_recent_request():
    live = LiveState(clock=lambda: 0)
    assert not live.decode_exact("p")  # unknown until measured
    live.record_exactness("p", 100, 100)
    live.record_exactness("p", 101, 100)  # one unstreamed token is tolerated
    assert live.decode_exact("p")
    live.record_exactness("p", 150, 100)  # an engine batching tokens per event
    assert not live.decode_exact("p")


def test_hello_resets_streams_from_the_snapshot():
    live = LiveState(clock=lambda: 0)
    start(live, "stale")
    live.on_event({"type": "hello", "requests": [{"id": "x", "ts": 5.0, "client": {}, "profile": "p",
                                                  "model": "m", "first_token": 6.0, "total": 40}]})
    assert list(live.streams) == ["x"] and live.streams["x"].total == 40


def test_a_stream_keeps_the_gateways_conversation_id_through_a_hello():
    live = LiveState(clock=lambda: 0)
    for rid, conv in (("a", "a1b2c3d4e5f6"), ("b", "../x"), ("c", None)):
        live.on_event({"type": "req.start", "id": rid, "ts": 1.0, "client": {}, "profile": "p", "model": "m",
                       "conv": conv})
    assert [s["conv"] for s in live.view(2.0)["streams"]] == ["a1b2c3d4e5f6", None, None]  # only an id Kiln stores
    live.on_event({"type": "hello", "requests": [{"id": "a", "ts": 1.0, "client": {}, "profile": "p",
                                                  "conv": "a1b2c3d4e5f6"}]})
    assert [s["conv"] for s in live.view(2.0)["streams"]] == ["a1b2c3d4e5f6"]


def progress(live, rid, now=None, slots=0):
    return next(s for s in live.view(now, slots=slots)["streams"] if s["id"] == rid)["prefill_progress"]


def test_prefill_progress_attaches_only_when_unambiguous():
    live = LiveState(clock=lambda: 0)
    start(live, "a")
    live.set_prefill_progress("p", 32768, 56994)
    assert (progress(live, "a")["done"], progress(live, "a")["total"]) == (32768, 56994)
    start(live, "b")
    live.set_prefill_progress("p", 40000, 56994)
    assert progress(live, "a")["done"] == 32768


def test_halogen_progress_moves_on_at_the_recent_rate_up_to_its_next_boundary():
    # Halogen reports only at its 32,768-token chunk boundaries; the browser fills the gap.
    live = LiveState(clock=lambda: 0)
    live.set_prefill_rate("p", 1850.0)
    start(live, "a")
    live.set_prefill_progress("p", 32768, 82414, elapsed_s=17.7, at=100.0, step=32768)
    v = progress(live, "a", 101.0)
    assert (v["done"], v["total"], v["at"], v["next"]) == (32768, 82414, 100.0, 65536)
    assert (v["rate"], v["rate_from"]) == (1850.0, "recent")
    assert live.progress_changed


def test_crucible_progress_rate_and_cached_start_come_from_its_reports():
    # strix req 117: resumed from 21,743 cached tokens; done / elapsed of the first report is 362,000 tok/s.
    live = LiveState(clock=lambda: 0)
    live.set_prefill_rate("p", 1500.0)
    live.prefill_chunks["p"] = 4096
    start(live, "a", ts=99.9)
    live.set_prefill_progress("p", 21751, 29989, elapsed_s=0.06, at=100.06)
    live.set_prefill_progress("p", 25847, 29989, elapsed_s=2.70, at=102.70)
    v = progress(live, "a", 103.0)
    assert v["rate"] == pytest.approx(4096 / 2.64, rel=1e-3) and v["rate_from"] == "engine"
    assert v["start"] == pytest.approx(21743, abs=100) and v["next"] == 29943


def test_a_chunk_is_learned_when_two_steps_agree():
    live = LiveState(clock=lambda: 0)
    start(live, "a", ts=99.9)
    for done, elapsed in ((21751, 0.06), (25847, 2.70)):
        live.set_prefill_progress("p", done, 60000, elapsed_s=elapsed, at=100 + elapsed)
    assert "p" not in live.prefill_chunks
    live.set_prefill_progress("p", 29943, 60000, elapsed_s=5.41, at=105.41)
    assert live.prefill_chunks["p"] == 4096


def test_a_repeated_report_does_not_tick_early():
    live = LiveState(clock=lambda: 0)
    start(live, "a")
    live.set_prefill_progress("p", 4096, 9000, elapsed_s=2.7, at=102.8)
    live.progress_changed = False
    live.set_prefill_progress("p", 4096, 9000, elapsed_s=2.7, at=102.8)
    assert live.progress_changed is False


def test_before_its_first_report_a_lone_prompt_counts_at_the_recent_rate():
    live = LiveState(clock=lambda: 0)
    live.set_prefill_rate("p", 1500.0)
    live.prefill_chunks["p"] = 4096
    start(live, "a", ts=100.0)
    v = progress(live, "a", 101.0)
    assert (v["total"], v["done"], v["next"], v["rate"], v["at"]) == (None, 0, 4096, 1500.0, 100.25)
    start(live, "b", ts=101.5)
    # a had the engine to itself when it began: it keeps counting, now sharing the rate. b began beside
    # a's prefill, so its first report can wait behind a's chunks: no count.
    assert progress(live, "a", 102.0)["rate"] == 750.0
    assert progress(live, "b", 102.0) is None


def test_the_cached_start_is_decided_with_the_rate_when_the_first_report_attached():
    live = LiveState(clock=lambda: 0)
    live.set_prefill_rate("p", 1500.0)
    live.prefill_chunks["p"] = 4096
    start(live, "a", ts=100.0)
    start(live, "b", ts=100.1)
    for i in range(10):  # the dashboard's ticks
        live.view(100.5 + i / 2)
    # b: 12,288 cached. a prefilled beside it until 105.0, then decoded: b's first chunk took 5.4 s, and
    # at the report's time b is alone, at 1,500 tok/s, which would put 8,025 tokens of work in it.
    live.on_event({"type": "req.first_token", "id": "a", "ts": 105.0})
    live.set_prefill_progress("p", 16384, 48872, elapsed_s=5.4, at=105.5)
    live.set_prefill_progress("p", 20480, 48872, elapsed_s=8.1, at=108.2)
    v = progress(live, "b", 108.5)
    assert (v["start"], v["start_estimated"]) == (12288, False)


def test_a_decoding_stream_keeps_its_prompt_size_and_cached_start():
    # The live cost prices a prompt from these until the request ends and its record has the exact count.
    live = LiveState(clock=lambda: 0)
    live.set_prefill_rate("p", 1500.0)
    live.prefill_chunks["p"] = 4096
    start(live, "a", ts=100.0)
    live.view(100.1)
    live.set_prefill_progress("p", 16384, 48872, elapsed_s=2.7, at=102.8)
    stream = lambda: live.view(110.0)["streams"][0]
    assert stream()["prompt"] == {"total": 48872, "cached": 12288}
    live.on_event({"type": "req.first_token", "id": "a", "ts": 125.0})
    v = stream()
    assert (v["phase"], v["prefill_progress"], v["prompt"]) == ("decode", None, {"total": 48872, "cached": 12288})
    live.on_event({"type": "hello", "requests": [{"id": "a", "ts": 100.0, "profile": "p", "first_token": 125.0}]})
    assert stream()["prompt"] == {"total": 48872, "cached": 12288}  # a gateway reconnect keeps it


def test_a_late_line_still_gives_a_decoding_stream_its_prompt():
    # A prompt that is almost all cached prefills in tenths of a second: its lines are logged before the first
    # token but reach the dashboard after it, in a journal batch (in a measured run, 289 of 394 requests).
    live = LiveState(clock=lambda: 0)
    live.set_prefill_rate("p", 1500.0)
    live.prefill_chunks["p"] = 4096
    start(live, "a", ts=100.0)
    start(live, "b", ts=200.0)  # starts later: the lines are not its own
    live.view(100.1)
    live.on_event({"type": "req.first_token", "id": "a", "ts": 100.4})
    live.set_prefill_progress("p", 17362, 17554, elapsed_s=0.06, at=100.06, req=7)  # an 8-row head chunk
    live.set_prefill_progress("p", 17554, 17554, elapsed_s=0.21, at=100.21, req=7)
    a, b = live.view(201.0)["streams"]
    assert (a["phase"], a["prefill_progress"]) == ("decode", None)
    assert a["prompt"]["total"] == 17554 and abs(a["prompt"]["cached"] - 17354) < 30
    assert b["prompt"] is None and b["prefill_progress"] is None


def test_a_stream_has_no_prompt_before_a_report_or_without_a_known_start():
    live = LiveState(clock=lambda: 0)
    start(live, "a", ts=100.0)
    assert live.view(101.0)["streams"][0]["prompt"] is None
    live.set_prefill_progress("p", 21751, 29989, elapsed_s=0.06, at=100.3)  # no recent rate: start unknown
    live.on_event({"type": "req.first_token", "id": "a", "ts": 106.0})
    assert live.view(107.0)["streams"][0]["prompt"] is None


def test_a_stream_past_the_engines_slots_waits_and_counts_from_when_it_gets_one():
    live = LiveState(clock=lambda: 0)
    live.set_prefill_rate("p", 1500.0)
    live.prefill_chunks["p"] = 4096
    for i, rid in enumerate("abc"):
        start(live, rid, ts=100.0 + i)
    live.on_event({"type": "req.first_token", "id": "a", "ts": 103.0})
    view = {s["id"]: s for s in live.view(104.0, slots=2)["streams"]}
    assert view["c"]["queued"] is True and view["c"]["prefill_progress"] is None
    assert view["b"]["queued"] is False and view["b"]["prefill_progress"]["rate"] == 1500.0  # c is not reading
    live.on_event({"type": "req.end", "id": "a"})
    live.on_event({"type": "req.first_token", "id": "b", "ts": 129.0})
    assert progress(live, "c", 130.0, slots=2)["at"] == 130.25  # not its start 28 s ago


def test_a_gateway_reconnect_keeps_a_prefills_reports():
    live = LiveState(clock=lambda: 0)
    live.prefill_chunks["p"] = 4096
    start(live, "a", ts=100.0)
    live.set_prefill_progress("p", 4096, 37298, elapsed_s=2.56, at=102.8)
    live.set_prefill_progress("p", 8192, 37298, elapsed_s=5.21, at=105.5)
    live.feed_lost()
    assert live.streams == {}
    live.on_event({"type": "hello", "requests": [{"id": "a", "profile": "p", "ts": 100.0, "client": {}}]})
    assert progress(live, "a", 106.0)["done"] == 8192 and progress(live, "a", 106.0)["start"] == 0


def test_the_first_token_ends_the_progress():
    live = LiveState(clock=lambda: 0)
    live.set_prefill_rate("p", 1500.0)
    start(live, "a", ts=100.0)
    live.set_prefill_progress("p", 4096, 9000, elapsed_s=2.7, at=102.8)
    live.on_event({"type": "req.first_token", "id": "a", "ts": 104.0})
    assert live.streams["a"].prefill.reports == () and progress(live, "a") is None


def test_live_ratio_is_the_typical_requests_not_inflated_by_unstreamed_tool_calls():
    live = LiveState(clock=lambda: 103.0)
    for _ in range(4):
        live.record_exactness("p", 1400, 1397)  # streamed nearly every token
    live.record_exactness("p", 633, 234)  # tool call arguments never streamed
    live.record_exactness("p", 102, 43)
    assert live.ratio("p") == pytest.approx(1400 / 1397)
    assert f"× {1400 / 1397:.3f}" in live.exactness_note("p")


def test_live_ratio_holds_when_tool_calls_carry_most_of_the_tokens():
    # Crucible t15: ordinary streams carry about 1.1 tokens per event, but a few tool
    # calls carried most of the tokens in very few events. Weighting by tokens gave 9.2, so a stream at
    # 60 tok/s showed as hundreds until it ended.
    live = LiveState(clock=lambda: 103.0)
    for _ in range(4):
        live.record_exactness("p", 300, 272)  # 1.10 tokens per event
    live.record_exactness("p", 1373, 63)  # tool call arguments held back
    live.record_exactness("p", 1373, 63)
    live.record_exactness("p", 2383, 40)
    assert live.ratio("p") == pytest.approx(300 / 272)


def test_calibrated_rate_when_engine_sends_fewer_events_than_tokens():
    live = LiveState(clock=lambda: 103.0)
    for _ in range(3):
        live.record_exactness("p", 1500, 1470)
    assert not live.decode_exact("p") and live.ratio("p") == pytest.approx(1500 / 1470)
    assert "1,470 events" not in live.exactness_note("p") and "4,410 events" in live.exactness_note("p")
    start(live)
    live.on_event({"type": "req.first_token", "id": "a", "ts": 101.0})
    tokens(live, "a", [101.01 + i * 0.02 for i in range(98)], 98)
    v = live.view(103.0)
    assert v["decode_tps"] == pytest.approx(98 * 1500 / 1470 / 2.0) and not v["decode_exact"]
    assert v["streams"][0]["tokens"] == 100 and v["decode_note"]


def test_exact_engines_keep_integer_history():
    live = LiveState(clock=lambda: 101.0)
    live.record_exactness("p", 100, 100)
    start(live)
    tokens(live, "a", [101.5, 101.6], 2)
    live.completed_seconds(102.5)
    assert live.ratio("p") == 1.0 and live.history(102.5)["decode"][-1] == 2


class Clock:
    def __init__(self, t):
        self.t = t

    def __call__(self):
        return self.t


def emit(live, clock, now, slots=None):
    clock.t = now
    return {x["t"]: (x["decode"], x["prefill"]) for x in live.completed_seconds(now, slots)}


def test_a_late_batch_counts_in_the_next_open_second_and_drawn_seconds_never_change():
    clock = Clock(100.0)
    live = LiveState(clock)
    start(live)
    tokens(live, "a", [100.2], 1)
    assert emit(live, clock, 101.5) == {100: (1, 0)}
    tokens(live, "a", [100.99, 101.3], 3)  # 100.99 arrived after second 100 was sent
    assert emit(live, clock, 102.5) == {101: (2, 0)}
    assert live.history(102.5)["decode"][-2:] == [1, 2]


def test_prefill_uses_the_streams_own_reported_rate_and_stops_at_the_prompt_size():
    clock = Clock(100.0)
    live = LiveState(clock)  # no recent rate: nothing counts until the stream's own reports give one
    start(live, "a", ts=100.0)
    live.set_prefill_progress("p", 1000, 6000, elapsed_s=1.0, at=101.0)
    assert emit(live, clock, 101.9) == {100: (0, 0)}  # sent with no rate known: it stays 0
    live.set_prefill_progress("p", 2000, 6000, elapsed_s=2.0, at=102.0)
    live.set_prefill_progress("p", 9000, 12000, elapsed_s=2.5, at=102.5)  # another size: a stray, ignored
    assert emit(live, clock, 102.6) == {101: (0, pytest.approx(1000))}  # 1000 tok/s from its reports
    sent = emit(live, clock, 108.5)
    assert [sent[t][1] for t in range(102, 108)] == [pytest.approx(v) for v in (1000,) * 5 + (0,)]
    assert live.streams["a"].pre_sum == pytest.approx(6000)  # never more than the prompt


def test_history_ends_at_the_last_second_sent_and_lists_request_starts():
    clock = Clock(100.0)
    live = LiveState(clock)
    start(live, "a", ts=100.2)
    tokens(live, "a", [100.5, 101.6], 2)
    emit(live, clock, 101.5)
    h = live.history(101.9)
    assert h["t"][-1] == 100 and h["decode"][-1] == 1  # 101 is not sent yet, so a new browser waits for it
    assert h["starts"] == [{"id": "a", "t": 100.2}]


def test_a_gateway_reconnect_keeps_the_tokens_already_counted():
    clock = Clock(103.0)
    live = LiveState(clock)
    start(live)
    tokens(live, "a", [103.5] * 10, 10)
    live.feed_lost()
    live.on_event({"type": "hello", "requests": [{"id": "a", "ts": 100.0, "first_token": 101.0, "total": 10}]})
    tokens(live, "a", [103.7] * 5, 15)
    assert live.seconds[103]["decode"] == 15


def test_prefill_counts_as_it_happens_at_the_recent_rate_shared_by_the_prompts():
    clock = Clock(100.0)
    live = LiveState(clock)
    live.set_prefill_rate("p", 1000.0)
    start(live, "a", ts=100.0)
    start(live, "b", ts=100.0)
    # Nothing for the first 1.5 s (no report yet), then 500 tok/s each.
    assert emit(live, clock, 103.5) == {100: (0, 0), 101: (0, pytest.approx(500)), 102: (0, pytest.approx(1000))}
    live.on_event({"type": "req.first_token", "id": "a", "ts": 103.5})
    assert emit(live, clock, 104.5)[103] == (0, pytest.approx(1000))  # b alone from 103.5: 1000 tok/s
    assert (live.streams["a"].pre_sum, live.streams["b"].pre_sum) == (pytest.approx(1000), pytest.approx(2000))


def test_a_short_cached_turn_draws_no_prefill():
    clock = Clock(100.0)
    live = LiveState(clock)
    live.set_prefill_rate("p", 1500.0)
    start(live, "a", ts=100.0)
    clock.t = 100.6
    live.on_event({"type": "req.first_token", "id": "a", "ts": 100.6})
    tokens(live, "a", [100.7], 1)
    assert emit(live, clock, 102.5) == {100: (1, 0), 101: (0, 0)}


def test_a_queued_stream_prefills_nothing_until_it_gets_a_slot():
    clock = Clock(100.0)
    live = LiveState(clock)
    live.set_prefill_rate("p", 1000.0)
    start(live, "a", ts=100.0)
    start(live, "b", ts=100.5)
    sent = emit(live, clock, 103.5, slots=1)
    assert sent[101] == (0, pytest.approx(500)) and sent[102] == (0, pytest.approx(1000))  # a alone: b waits


def test_a_stream_cancelled_while_queued_counts_no_prefill():
    clock = Clock(100.0)
    live = LiveState(clock)
    live.set_prefill_rate("p", 1000.0)
    start(live, "a", ts=100.0)
    start(live, "b", ts=100.2)
    emit(live, clock, 101.0, slots=1)
    clock.t = 105.0
    live.on_event({"type": "req.end", "id": "b", "record": {}})
    emit(live, clock, 106.0, slots=1)
    drawn = sum(v["prefill"] for v in live.seconds.values())
    assert drawn == pytest.approx(live.streams["a"].pre_sum) == pytest.approx(1000 * (106.0 - 101.5))


def test_a_stream_that_ends_while_prefilling_stops_counting():
    clock = Clock(100.0)
    live = LiveState(clock)
    live.set_prefill_rate("p", 1000.0)
    start(live, "a", ts=100.0)
    clock.t = 103.0
    live.on_event({"type": "req.end", "id": "a", "record": {}})
    emit(live, clock, 106.0)
    assert sum(v["prefill"] for v in live.seconds.values()) == pytest.approx(1500)


def test_a_gateway_reconnect_during_prefill_counts_nothing_twice():
    clock = Clock(100.0)
    live = LiveState(clock)
    live.set_prefill_rate("p", 1000.0)
    start(live, "a", ts=100.0)
    emit(live, clock, 102.5)
    live.feed_lost()
    live.on_event({"type": "hello", "requests": [{"id": "a", "ts": 100.0, "profile": "p", "first_token": None,
                                                  "total": 0}]})
    emit(live, clock, 104.5)
    assert sum(v["prefill"] for v in live.seconds.values()) == pytest.approx(1000 * (104.5 - 101.5))



def test_the_prompts_prefilling_at_once_share_the_engines_rate_even_with_their_own_reports():
    # The engine pauses a long prefill (1,387 tok/s of its own reports) to run a
    # short one (740 tok/s). Both kept counting at their own rate, so the chart summed to 2,127 tok/s for
    # one engine that cannot prefill that fast; the profile's recent rate is the engine's share here.
    clock = Clock(100.0)
    live = LiveState(clock)
    live.set_prefill_rate("p", 1400.0)
    start(live, "a", ts=100.0)
    live.set_prefill_progress("p", 4096, 49311, elapsed_s=2.95, at=100.0, req=1)
    live.set_prefill_progress("p", 8192, 49311, elapsed_s=5.90, at=100.0, req=1)
    start(live, "b", ts=100.5)  # started far enough apart that its engine number can be paired
    live.set_prefill_progress("p", 4096, 111096, elapsed_s=5.53, at=100.5, req=2)
    live.set_prefill_progress("p", 8192, 111096, elapsed_s=11.07, at=100.5, req=2)
    sent = emit(live, clock, 103.5)
    own = live.streams["a"].pre_rate + live.streams["b"].pre_rate
    assert own == pytest.approx(4096 / 2.95 + 4096 / 5.54)  # what the two streams claim on their own
    scale = 1400.0 / own
    a_rate, b_rate = 4096 / 2.95 * scale, 4096 / 5.54 * scale
    assert sent[101][1] == pytest.approx(a_rate + b_rate) == pytest.approx(1400.0)
    assert sent[102][1] == pytest.approx(1400.0)
    assert sent[100][1] == pytest.approx(a_rate + b_rate * 0.5, abs=0.02)  # a all second, b half


def test_a_decode_line_gives_its_stream_the_running_turns_draft_counts():
    live = LiveState(clock=lambda: 110.0)
    start(live, "a", 100.0)
    live.set_prefill_progress("p", 4096, 9000, elapsed_s=1.0, at=101.0, req=10)  # pairs engine 10 with a
    live.on_event({"type": "req.first_token", "id": "a", "ts": 103.0})
    view = lambda: live.view(110.0)["streams"][0]["draft_turn"]
    assert view() is None  # no decode line yet
    live.set_decode_drafts("p", 10, 120, 90)
    assert view() == {"accepted": 90, "proposed": 120}
    live.set_decode_drafts("p", 10, 100, 70)  # counts are cumulative: an older line comes late
    assert view() == {"accepted": 90, "proposed": 120}
    live.set_decode_drafts("p", 11, 500, 400)  # another request's number: not a's
    assert view() == {"accepted": 90, "proposed": 120}
    live.on_event({"type": "hello", "requests": [{"id": "a", "ts": 100.0, "profile": "p", "first_token": 103.0}]})
    assert view() == {"accepted": 90, "proposed": 120}  # a gateway reconnect keeps them
    # A cancelled turn writes no final line: the last value stays, however old, until the stream ends.
    assert live.view(400.0)["streams"][0]["draft_turn"] == {"accepted": 90, "proposed": 120}
    live.on_event({"type": "req.end", "id": "a"})
    assert live.view(400.0)["streams"] == []


def test_a_closed_think_turns_second_job_lines_stay_with_the_stream_that_still_decodes():
    # Crucible runs a think turn it closes as two engine jobs, and both write with the first job's request_id,
    # perhaps long after the first token. Its prefill line must not pair the number with a prompt that
    # prefills alone then, or the stream's decode lines would follow it there.
    clock = [110.0]
    live = LiveState(clock=lambda: clock[0])
    start(live, "a", 100.0)
    live.set_prefill_progress("p", 4096, 9000, elapsed_s=1.0, at=101.0, req=10)
    live.on_event({"type": "req.first_token", "id": "a", "ts": 103.0})
    clock[0] = 103.0 + RETIRED_S + 20
    start(live, "b", clock[0] - 2)
    live.set_prefill_progress("p", 512, 600, elapsed_s=0.2, at=clock[0], req=10)  # the second job's prefill
    assert not live.streams["b"].prefill.reports
    live.set_decode_drafts("p", 10, 900, 700)
    assert live.streams["a"].turn_drafts == (700, 900) and live.streams["b"].turn_drafts is None


def test_a_request_that_does_not_stream_decodes_from_its_first_decode_line():
    clock = [110.0]
    live = LiveState(clock=lambda: clock[0])
    start(live, "a", 100.0)
    live.set_prefill_progress("p", 4096, 9000, elapsed_s=1.0, at=101.0, req=10)
    view = lambda: live.view(clock[0])["streams"][0]
    assert view()["phase"] == "prefill"  # no first token at the gateway: it does not stream
    live.set_decode_drafts("p", 10, 120, 90, tokens=100, elapsed_s=2.0, at=110.0)
    v = view()
    assert (v["phase"], v["first_token"], v["prefill_progress"]) == ("decode", 108.0, None)
    assert (v["tokens"], v["tps"], v["draft_turn"]) == (100, 50.0, {"accepted": 90, "proposed": 120})
    clock[0] = 111.0
    live.set_decode_drafts("p", 10, 190, 150, tokens=160, elapsed_s=3.0, at=111.0)
    assert (view()["tokens"], view()["tps"]) == (160, 60.0)  # the rate between its last two lines
    live.on_event({"type": "hello", "requests": [{"id": "a", "ts": 100.0, "profile": "p"}]})
    assert (view()["phase"], view()["tokens"]) == ("decode", 160)  # a gateway reconnect keeps it decoding


def test_a_short_request_that_does_not_stream_decodes_from_its_one_prefill_line():
    # One line, done == total, then decode lines: the pattern of a batch client with ~1k-token prompts.
    clock = [110.0]
    live = LiveState(clock=lambda: clock[0])
    start(live, "a", 100.0)
    live.set_prefill_progress("p", 976, 976, elapsed_s=1.06, at=101.1, req=10)
    live.set_decode_drafts("p", 10, 120, 90, tokens=100, elapsed_s=2.0, at=103.1)
    v = live.view(clock[0])["streams"][0]
    assert (v["phase"], v["tokens"]) == ("decode", 100)


def decoding_stream(clock, first_token=103.0):
    """Stream a, paired with engine request 10 by its prefill line, decoding from first_token."""
    live = LiveState(clock=lambda: clock.t)
    start(live, "a", 100.0)
    live.set_prefill_progress("p", 4096, 9000, elapsed_s=1.0, at=101.0, req=10)
    live.on_event({"type": "req.first_token", "id": "a", "ts": first_token})
    return live


def test_a_held_back_tool_call_shows_the_engines_tokens_and_rate():
    # Crucible streams a tool call's string parameters only: an array parameter (a coding agent's edit) goes out
    # when it closes, so the gateway gets nothing for a minute while the engine decodes at 60 tok/s
    # (in a measured run, 4,280 tokens in 100 events). Its decode lines say what it does.
    clock = Clock(110.0)
    live = decoding_stream(clock)
    tokens(live, "a", [103.1, 103.2, 103.3], 3)
    live.set_decode_drafts("p", 10, 300, 200, tokens=300, elapsed_s=6.0, at=109.0)
    live.set_decode_drafts("p", 10, 360, 240, tokens=360, elapsed_s=7.0, at=110.0)
    v = live.view(110.5)["streams"][0]
    assert (v["phase"], v["first_token"], v["tokens"], v["tps"]) == ("decode", 103.0, 360, 60.0)
    assert live.decode_tps(110.5) == 60.0


def test_without_a_recent_engine_line_the_gateway_rate_counts_again():
    clock = Clock(110.0)
    live = decoding_stream(clock)
    live.set_decode_drafts("p", 10, 360, 240, tokens=360, elapsed_s=7.0, at=110.0)
    late = 110.0 + ENGINE_FRESH_S + 0.1  # the engine stalled, or its turn was cancelled
    tokens(live, "a", [late - 1.0 + i * 0.05 for i in range(10)], 10)
    v = live.view(late)["streams"][0]
    assert v["tokens"] == 360  # the engine's count stays: those tokens were made
    assert v["tps"] == pytest.approx(10 / WINDOW_S) == live.decode_tps(late)


def test_a_streamed_request_with_no_decode_lines_keeps_the_gateways_figures():
    clock = Clock(106.0)
    live = decoding_stream(clock)
    tokens(live, "a", [104.1 + i * 0.1 for i in range(20)], 20)
    v = live.view(106.0)["streams"][0]
    assert (v["tokens"], v["tps"]) == (20, 10.0)


def test_a_decode_line_for_another_engine_number_never_moves_a_streams_figures():
    # a is engine 10 and b is engine 12, each paired while it prefilled alone. A request sent straight to
    # the engine took 11, so the start order puts 11 on a. Its lines are not a's.
    clock = Clock(100.2)
    live = LiveState(clock=lambda: clock.t)
    start(live, "a", 100.0)
    live.set_prefill_progress("p", 4096, 9000, elapsed_s=0.2, at=100.2, req=10)
    start(live, "b", 100.6)
    live.on_event({"type": "req.first_token", "id": "a", "ts": 100.7})
    clock.t = 101.0
    live.set_prefill_progress("p", 4096, 9000, elapsed_s=0.4, at=101.0, req=12)
    assert live.streams["b"].prefill.reports  # b is 12
    clock.t = 105.0
    live.set_decode_drafts("p", 11, 900, 600, tokens=900, elapsed_s=4.0, at=105.0)
    a = live.view(105.0)["streams"][0]
    assert (a["id"], a["tokens"], a["draft_turn"]) == ("a", 0, None)
    live.set_decode_drafts("p", 10, 200, 150, tokens=200, elapsed_s=4.0, at=105.0)
    assert live.view(105.0)["streams"][0]["tokens"] == 200  # its own number still counts


def test_the_live_chart_counts_held_back_tokens_as_the_engine_reports_them_and_never_twice():
    clock = Clock(103.0)
    live = decoding_stream(clock)
    live.record_exactness("p", 100, 100)
    tokens(live, "a", [103.1, 103.2], 2)
    assert emit(live, clock, 104.5)[103] == (2, 0)
    clock.t = 105.3
    live.set_decode_drafts("p", 10, 70, 50, tokens=62, elapsed_s=2.0, at=105.0)  # 60 held back in 104-105
    assert emit(live, clock, 105.5) == {104: (0, 0)}  # a drawn second never changes
    assert emit(live, clock, 106.5) == {105: (60, 0)}
    clock.t = 106.4
    live.set_decode_drafts("p", 10, 140, 100, tokens=122, elapsed_s=3.0, at=106.0)
    tokens(live, "a", [106.2] * 130, 132)  # the call closes: 120 held tokens and 10 new ones arrive
    assert emit(live, clock, 107.5) == {106: (70, 0)}  # 60 from the engine, 10 new from the gateway


def test_a_dashboard_restart_does_not_chart_a_streams_earlier_tokens_at_its_first_engine_line():
    clock = Clock(199.0)
    live = LiveState(clock=lambda: clock.t)
    live.record_exactness("p", 100, 100)
    live.on_event({"type": "hello", "requests": [{"id": "a", "ts": 100.0, "profile": "p", "first_token": 103.0,
                                                 "total": 50}]})
    start(live, "b", 199.0)
    live.set_prefill_progress("p", 4096, 9000, elapsed_s=0.5, at=199.5, req=11)  # b alone: the anchor
    emit(live, clock, 200.5)
    clock.t = 200.6
    live.set_decode_drafts("p", 10, 6000, 4000, tokens=5000, elapsed_s=97.0, at=200.6)  # a, by start order
    assert live.streams["a"].engine_decode is not None
    live.set_decode_drafts("p", 10, 6070, 4050, tokens=5060, elapsed_s=98.0, at=201.6)
    assert emit(live, clock, 202.5) == {200: (0, 0), 201: (60, 0)}


def test_a_gateway_reconnect_keeps_the_engine_figures():
    clock = Clock(110.0)
    live = decoding_stream(clock)
    live.set_decode_drafts("p", 10, 360, 240, tokens=360, elapsed_s=7.0, at=110.0)
    live.feed_lost()
    live.on_event({"type": "hello", "requests": [{"id": "a", "ts": 100.0, "profile": "p", "first_token": 103.0}]})
    assert (live.view(110.5)["streams"][0]["tokens"], live.decode_tps(110.5)) == (360, pytest.approx(360 / 7))
    assert live.streams["a"].engine_req == 10
