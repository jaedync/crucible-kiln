from kiln.live.pairing import CLOSE_S, EXTRA_S, RETIRED_S, ProgressPairs
from kiln.live.rates import LiveState
from tests.test_rates import start


def starts(pairs, *spec, profile="p"):
    for rid, ts in spec:
        pairs.started(profile, rid, ts)


def test_a_lone_prefill_is_paired_and_anchors_the_engine_numbering():
    pp = ProgressPairs()
    starts(pp, ("a", 100.0))
    assert pp.pair("p", 10, [("a", 100.0)]) == "a"
    assert pp.anchor["p"] == (10, "a")


def test_overlapping_prefills_are_told_apart_by_the_engine_request_number():
    # Crucible numbers every request it receives, in arrival order: 10 is a, so 12 is the second start after a.
    pp = ProgressPairs()
    starts(pp, ("a", 100.0), ("b", 101.0), ("c", 102.0))
    assert pp.pair("p", 10, [("a", 100.0)]) == "a"
    prefilling = [("b", 101.0), ("c", 102.0)]
    assert pp.pair("p", 12, prefilling) == "c"
    assert pp.pair("p", 11, prefilling) == "b"
    assert pp.pair("p", 12, prefilling) == "c"  # the same request keeps its stream


def test_starts_too_close_together_to_order_get_no_progress_and_move_nothing():
    pp = ProgressPairs()
    starts(pp, ("a", 100.0), ("b", 101.0), ("c", 101.0 + CLOSE_S / 2))
    pp.pair("p", 10, [("a", 100.0)])
    assert pp.pair("p", 11, [("b", 101.0), ("c", 101.0 + CLOSE_S / 2)]) is None
    assert pp.anchor["p"] == (10, "a") and "b" not in pp.paired  # an unsure match is no reference
    # Close to a stream that already decodes counts too: their engine numbers may be swapped.
    assert pp.pair("p", 12, [("c", 101.0 + CLOSE_S / 2)]) == "c"  # alone in prefill: the line is its own


def test_a_late_line_of_a_stream_that_now_decodes_goes_to_no_one():
    # Journal lines arrive in batches, often after the first token: the last line must not pass to b.
    pp = ProgressPairs()
    starts(pp, ("a", 100.0), ("b", 104.0))
    assert pp.pair("p", 7, [("a", 100.0)], now=103.0) == "a"
    pp.finished("a", 105.0)
    assert pp.pair("p", 7, [("b", 104.0)], now=105.3) is None
    assert pp.pair("p", 7, [("b", 104.0)], complete=True, now=105.4) is None
    assert pp.anchor["p"] == (7, "a") and "b" not in pp.paired


def test_the_last_line_of_a_prompt_without_its_timing_never_makes_a_new_pairing():
    pp = ProgressPairs()
    starts(pp, ("a", 100.0))
    assert pp.pair("p", 3, [("a", 100.0)], complete=True) is None
    assert pp.pair("p", 3, [("a", 100.0)]) == "a"


def test_no_anchor_or_a_number_outside_the_known_starts_gives_nothing():
    pp = ProgressPairs()
    starts(pp, ("a", 100.0), ("b", 101.0))
    assert pp.pair("p", 5, [("a", 100.0), ("b", 101.0)]) is None  # several prefill and nothing anchors the count
    pp.pair("p", 10, [("a", 100.0)])
    assert pp.pair("p", 1, [("a", 100.0), ("b", 101.0)]) is None  # the engine restarted and counts from 1 again


def test_a_stream_that_is_no_longer_prefilling_gets_no_progress():
    pp = ProgressPairs()
    starts(pp, ("a", 100.0), ("b", 101.0), ("c", 102.0), ("d", 103.0))
    pp.pair("p", 10, [("a", 100.0)])
    assert pp.pair("p", 11, [("c", 102.0), ("d", 103.0)]) is None


def test_an_engine_restart_does_not_land_on_old_pairings():
    pp = ProgressPairs()
    starts(pp, ("a", 100.0))
    assert pp.pair("p", 1, [("a", 100.0)], now=101.0) == "a"
    pp.finished("a", 102.0)  # first token
    starts(pp, ("b", 500.0))  # the engine restarted and numbers from 1 again
    assert pp.pair("p", 1, [("b", 500.0)], now=102.0 + RETIRED_S + 1) == "b"


def test_profiles_keep_separate_numbering():
    pp = ProgressPairs()
    starts(pp, ("a", 100.0), profile="p")
    starts(pp, ("x", 100.5), profile="q")
    assert pp.pair("p", 3, [("a", 100.0)]) == "a"
    assert pp.pair("q", 3, [("x", 100.5)]) == "x"


def test_memory_stays_bounded():
    pp = ProgressPairs()
    for i in range(5000):
        pp.started("p", f"r{i}", float(i))
        pp.pair("p", i, [(f"r{i}", float(i))])
    assert len(pp.starts["p"]) <= ProgressPairs.KEEP and len(pp.pairs) <= ProgressPairs.KEEP


def test_live_state_attaches_progress_by_engine_request_number():
    live = LiveState(clock=lambda: 104.0)  # lines without a time are judged at the clock
    start(live, "a", ts=100.0)
    live.set_prefill_progress("p", 4096, 50000, req=7)
    start(live, "b", ts=101.0)
    start(live, "c", ts=102.0)
    live.on_event({"type": "req.first_token", "id": "a", "ts": 103.0})
    live.set_prefill_progress("p", 8192, 90000, req=9)
    live.set_prefill_progress("p", 4096, 20000, req=8)
    assert live.streams["c"].prefill.reports[-1].total == 90000
    assert live.streams["b"].prefill.reports[-1].total == 20000


def test_who_was_prefilling_is_judged_at_the_lines_own_time():
    clock = [110.0]
    live = LiveState(clock=lambda: clock[0])
    start(live, "a", ts=100.0)
    start(live, "b", ts=105.0)
    live.on_event({"type": "req.first_token", "id": "a", "ts": 108.0})
    # Written at 104 (only a prefilled then), read at 110: it is a's line, and a decodes now.
    live.set_prefill_progress("p", 4096, 9000, elapsed_s=3.0, at=104.0, req=7)
    assert live.streams["b"].prefill.reports == ()
    live.on_event({"type": "req.end", "id": "a"})
    live.set_prefill_progress("p", 4096, 50000, elapsed_s=2.0, at=109.0, req=8)  # b's own first line
    assert live.streams["b"].prefill.reports[-1].total == 50000


def test_a_decode_line_finds_the_stream_its_prefill_paired_for_as_long_as_it_decodes():
    pp = ProgressPairs()
    starts(pp, ("a", 100.0), ("b", 101.0))
    assert pp.pair("p", 10, [("a", 100.0)]) == "a"
    pp.finished("a", 102.0)  # first token: the number is retired, and still names a
    assert pp.decoding("p", 10, {"a", "b"}) == "a"
    assert pp.decoding("p", 10, {"b"}) is None  # a ended: a late line goes to no one
    assert pp.decoding("q", 10, {"a"}) is None  # another engine's numbers


def test_a_decode_line_of_a_number_never_paired_uses_the_start_order_when_sure():
    pp = ProgressPairs()
    starts(pp, ("a", 100.0), ("b", 101.0), ("c", 101.0 + CLOSE_S / 2), ("d", 103.0))
    pp.pair("p", 10, [("a", 100.0)])
    assert pp.decoding("p", 13, {"a", "d"}) == "d"  # three starts after a
    assert pp.decoding("p", 12, {"a", "c"}) is None  # c started too close to b to tell 11 from 12
    assert pp.decoding("p", 13, {"a"}) is None  # d does not decode


def test_a_retired_number_stays_its_streams_while_the_stream_is_open():
    pp = ProgressPairs()
    starts(pp, ("a", 100.0), ("b", 150.0))
    assert pp.pair("p", 10, [("a", 100.0)]) == "a"
    pp.finished("a", 102.0)
    later = 102.0 + RETIRED_S + 20
    assert pp.pair("p", 10, [("b", 150.0)], now=later, live={"a", "b"}) is None  # a still decodes
    assert pp.decoding("p", 10, {"a"}) == "a"
    # Once a has ended, the number is free after RETIRED_S, as before (an engine that restarted counts from 1).
    assert pp.pair("p", 10, [("b", 150.0)], now=later, live={"b"}) == "b"


def test_a_number_the_engine_gave_its_own_job_is_skipped_in_the_start_order():
    # Crucible's continuation of a closed think turn takes the next number but is no gateway start.
    pp = ProgressPairs()
    starts(pp, ("a", 100.0), ("b", 101.0), ("c", 103.0))
    assert pp.pair("p", 10, [("a", 100.0)]) == "a"
    pp.continuation("p", 11, now=102.0)
    prefilling = [("b", 101.0), ("c", 103.0)]
    assert pp.pair("p", 12, prefilling, now=104.0) == "b"  # not c
    assert pp.pair("p", 13, prefilling, now=104.0) == "c"
    assert pp.decoding("p", 11, {"a", "b", "c"}, now=104.0) is None  # the job itself is no stream


def test_a_skipped_number_also_counts_back_from_a_later_anchor():
    pp = ProgressPairs()
    starts(pp, ("a", 100.0), ("b", 101.0), ("c", 103.0))
    assert pp.pair("p", 13, [("c", 103.0)]) == "c"
    pp.continuation("p", 11, now=103.0)
    assert pp.pair("p", 10, [("a", 100.0), ("b", 101.0)], now=104.0) == "a"
    assert pp.pair("p", 10, [("a", 100.0), ("b", 101.0)], now=104.0 + EXTRA_S) == "a"  # a kept pairing
    later = ProgressPairs()
    starts(later, ("a", 100.0), ("b", 101.0), ("c", 103.0))
    later.pair("p", 13, [("c", 103.0)])
    later.continuation("p", 11, now=103.0)
    # After EXTRA_S the number counts no more: an engine that restarts numbers from 1 again.
    assert later.pair("p", 10, [("a", 100.0), ("b", 101.0)], now=103.0 + EXTRA_S + 1) is None


def test_a_decode_line_does_not_move_the_anchor_back_to_an_older_number():
    # Decode lines come every second from every decoding stream; the newest pairing is the better reference.
    pp = ProgressPairs()
    starts(pp, ("a", 100.0), ("b", 101.0))
    assert pp.pair("p", 10, [("a", 100.0)]) == "a"
    pp.finished("a", 100.5)
    assert pp.pair("p", 11, [("b", 101.0)]) == "b"
    assert pp.decoding("p", 10, {"a"}) == "a"
    assert pp.anchor["p"] == (11, "b")


def test_live_state_skips_a_continuation_in_the_start_order():
    live = LiveState(clock=lambda: 104.0)
    for rid, ts in (("a", 100.0), ("b", 101.0), ("c", 103.0)):
        start(live, rid, ts)
    live.on_event({"type": "req.first_token", "id": "a", "ts": 100.6})
    live.set_prefill_progress("p", 4096, 9000, elapsed_s=0.5, at=100.5, req=10)  # a alone: anchors 10
    live.set_continuation("p", 11)
    live.set_prefill_progress("p", 4096, 50000, elapsed_s=2.0, at=104.0, req=12)
    assert live.streams["b"].prefill.reports and not live.streams["c"].prefill.reports


def test_a_decode_line_names_a_stream_still_in_prefill_only_by_its_own_pairing():
    # A request that does not stream has no first token at the gateway: the engine's decode line is the first
    # sign that it decodes. Only the pairing its own prefill lines made may say so, never the start order.
    pp = ProgressPairs()
    starts(pp, ("a", 100.0), ("b", 101.0), ("c", 103.0))
    assert pp.pair("p", 10, [("a", 100.0)]) == "a"
    assert pp.decoding("p", 10, set(), waiting={"a", "b", "c"}) == "a"
    assert pp.decoding("p", 12, set(), waiting={"a", "b", "c"}) is None  # c by the start order: not sure enough
    assert pp.decoding("p", 12, {"c"}, waiting={"a", "b"}) == "c"  # as before, for a stream that decodes


def test_the_last_line_pairs_a_stream_that_was_prefilling_when_that_prompt_began():
    # A short prompt has one line only, done == total. A stream that started before the prompt began (the line's
    # time minus its elapsed time) and still waits for its first token is the one: a late last line belongs to a
    # stream that started earlier and decodes now, so a newer stream started after that prompt began.
    pp = ProgressPairs()
    starts(pp, ("a", 100.0))
    assert pp.pair("p", 3, [("a", 100.0)], complete=True, began=100.1) == "a"
    assert pp.anchor["p"] == (3, "a")


def test_the_last_line_never_pairs_a_stream_that_started_after_its_prompt_began():
    pp = ProgressPairs()
    starts(pp, ("a", 100.0), ("b", 104.0))
    # a decodes already and was never paired; its last line arrives while only b prefills.
    assert pp.pair("p", 7, [("b", 104.0)], complete=True, began=101.0, now=105.0) is None
    assert "b" not in pp.paired and "p" not in pp.anchor


def test_the_last_line_goes_by_the_start_order_when_several_prefill():
    # Requests that do not stream wait for their first token at the gateway until a decode line comes, so two
    # of them often overlap: the engine number and the start order tell them apart.
    pp = ProgressPairs()
    starts(pp, ("a", 100.0), ("b", 101.0), ("c", 102.0))
    assert pp.pair("p", 10, [("a", 100.0)], complete=True, began=100.0) == "a"
    prefilling = [("a", 100.0), ("b", 101.0), ("c", 102.0)]
    assert pp.pair("p", 12, prefilling, complete=True, began=102.1) == "c"
    assert pp.pair("p", 11, [("b", 101.0), ("c", 102.0)], complete=True, began=101.05) == "b"
