"""Prefill progress from engine reports (live/prefill.py), on report sequences logged by the inference host's Crucible t15."""
import json
from pathlib import Path

import pytest

from kiln.live.prefill import Report, Track, add_report, chunk_of, progress_view, prompt_of

T0 = 1000.0


def track(rows, total, rate=1500.0, chunk=4096, step=None, t0=T0):
    """A stream's track after these (done, elapsed) reports; `rate` is the recent rate when the first attaches."""
    out = Track()
    for done, elapsed in rows:
        out = add_report(out, Report(done, total, elapsed, t0 + elapsed), rate=rate, chunk=chunk, step=step)
    return out


# req 80: cold, 37,298 tokens in 24.8 s, a report every 4,096 tokens
COLD = [(4096, 2.56), (8192, 5.21), (12288, 7.88)]
# req 81: 12,288 tokens from a block cache, then 36,584 computed; the first report is cached + 4,096
BLOCK = [(16384, 2.70), (20480, 5.38), (24576, 8.08)]
# req 117: resumed from 21,743 cached tokens; the engine's first chunk is an 8-row head
RESUMED = [(21751, 0.06), (25847, 2.70), (29943, 5.41)]


def test_rate_comes_from_the_streams_own_reports_not_done_over_elapsed():
    v = progress_view(track(RESUMED[:2], 29989), hint=1500.0, chunk=4096)
    # done / elapsed of the first report would be 362,000 tok/s: the cached prefix is not work
    assert v["rate"] == pytest.approx(4096 / 2.64, rel=1e-3)
    assert v["rate_from"] == "engine"


def test_one_report_uses_the_recent_rate():
    v = progress_view(track(RESUMED[:1], 29989), hint=1500.0, chunk=4096)
    assert v["rate"] == 1500.0 and v["rate_from"] == "recent"
    assert progress_view(track(RESUMED[:1], 29989), hint=None, chunk=4096)["rate"] is None


def test_a_hint_is_shared_by_the_prompts_that_prefill_at_once():
    v = progress_view(track(COLD[:1], 37298), hint=1500.0, chunk=4096, sharing=2)
    assert v["rate"] == 750.0


@pytest.mark.parametrize("rows,total,start", [(COLD, 37298, 0), (BLOCK, 48872, 12288)])
def test_a_first_report_after_a_full_chunk_starts_one_chunk_back(rows, total, start):
    for rate in (1000.0, 1500.0, 2200.0):  # the recent rate can be well off; the start must not move
        t = track(rows, total, rate=rate)
        assert (t.start, t.start_estimated) == (start, False)


def test_the_start_is_decided_once_at_the_rate_when_the_first_report_attached():
    # B, 12,288 cached, starts beside another prefill (its rate halved); that prefill ends before B's next
    # report and B's own rate doubles. Decided again at the new rate, the start became 8,192.
    shared = track([(16384, 5.4)], 48872, rate=750.0)  # its first chunk shared the engine: twice as long
    assert (shared.start, shared.start_estimated) == (12288, False)
    later = add_report(shared, Report(20480, 48872, 8.1, T0 + 8.1), rate=1500.0, chunk=4096)
    assert (later.start, later.start_estimated) == (12288, False)
    # A cold prompt whose first chunk ran alone stays cold when another prompt starts and its rate halves.
    alone = track(COLD[:1], 37298, rate=1500.0)
    slower = add_report(alone, Report(8192, 37298, 8.0, T0 + 8.0), rate=750.0, chunk=4096)
    assert (slower.start, slower.start_estimated) == (0, False)
    assert progress_view(slower, hint=1500.0, chunk=4096, sharing=2)["start"] == 0


def test_a_first_report_from_later_in_the_prefill_gives_an_estimated_start():
    # The stream paired late (another prompt started 0.1 s apart): its first report is the third.
    t = track(COLD[2:3], 37298)
    assert (t.start, t.start_estimated) == (0, True)  # 12,288 - 1,500 x 7.83 = 543: within the rate's error
    t = track([(40960, 52.0)], 60000)
    assert (t.start, t.start_estimated) == (0, True)
    t = track([(70000, 10.0)], 90000)  # a real cache stays: 70,000 - 1,500 x (10 - 0.05 - 0.527 restore)
    assert t.start == 55866 and t.start_estimated


def test_a_prompt_that_fits_in_one_chunk_has_an_estimated_start():
    t = track([(12288 + 3000, 2.0)], 15288)
    assert t.start_estimated is True


def test_halogen_starts_are_estimates_even_with_a_learned_chunk():
    # 40,000 cached: the first report lands on the next 32,768 boundary, 25,536 tokens of work in.
    t = track([(65536, 13.85)], 90000, rate=1850.0, chunk=32768, step=32768)
    assert t.start_estimated is True and abs(t.start - 40000) < 100
    cold = track([(32768, 17.6)], 90000, rate=1850.0, chunk=32768, step=32768)
    assert cold.start == 0  # 208 left over is the rate's error, not a cache


def test_a_first_report_a_few_rows_in_is_the_restored_tail_and_its_start_is_exact():
    # 21,743 cached (the engine's turn line): the first chunk is the 8 rows after the restored prefix.
    for rate in (1000.0, 1500.0, 2200.0):
        t = track(RESUMED[:1], 29989, rate=rate)
        assert (t.start, t.start_estimated) == (21743, False)


@pytest.mark.parametrize("done,total,elapsed,rate,cached", [
    (184398, 193913, 1.7858, 1243.6, 184390),  # once called a full chunk, 4,088 cached tokens priced as new
    (233115, 233786, 1.6872, 1412.2, 233107),
    (236041, 240000, 1.68, 1381.0, 236033),
])
def test_a_long_restore_before_the_tail_rows_is_not_a_full_chunk(done, total, elapsed, rate, cached):
    # Restoring a long prefix takes time (about 6 us a token): 8 rows after 230,000 cached took 1.7 s, half a
    # chunk's time at the recent rate. A measured run had 524 such first reports and 10 full chunks.
    t = add_report(Track(), Report(done, total, elapsed, T0), rate=rate, chunk=4096)
    assert (t.start, t.start_estimated) == (cached, False)


@pytest.mark.parametrize("done,total,elapsed,rate,chunk", [
    (4096, 50000, 2.73, 500.0, 4096),  # a cold first chunk while the recent rate reads 3x low (review)
    (16384, 50000, 2.73, 500.0, 4096),  # the same after a block cache: it ends on a chunk boundary too
    (1024, 10000, 0.733, 1500.0, 4096),  # the engine's chunk is 1,024, not the 4,096 learned before
])
def test_a_report_that_may_end_a_full_chunk_is_never_an_exact_tail(done, total, elapsed, rate, chunk):
    t = add_report(Track(), Report(done, total, elapsed, T0), rate=rate, chunk=chunk)
    assert t.start_estimated is True


def test_a_slow_restore_still_reads_as_the_tail_rows():
    # 8 rows after 233,107 cached, the restore 1.5x slower than the slowest in a measured run (review)
    t = add_report(Track(), Report(233115, 233786, 3.0, T0), rate=1412.2, chunk=4096)
    assert t.start != 233115 - 4096


def test_next_report_is_one_chunk_on_and_never_past_the_total():
    v = progress_view(track(BLOCK[:2], 48872), hint=1500.0, chunk=4096)
    assert v["next"] == 24576
    v = progress_view(track([(45056, 22.89)], 48872), hint=1500.0, chunk=4096)
    assert v["next"] == 48872
    assert progress_view(track(BLOCK[:1], 48872), hint=1500.0, chunk=None)["next"] == 48872


def test_halogen_reports_land_on_fixed_boundaries():
    v = progress_view(track([(32768, 17.6)], 56994, rate=1850.0, chunk=None, step=32768), hint=1850.0,
                      chunk=None, step=32768)
    assert v["next"] == 56994
    v = progress_view(track([(32768, 17.6)], 90000, rate=1850.0, chunk=32768, step=32768), hint=1850.0,
                      chunk=32768, step=32768)
    assert v["next"] == 65536


def test_view_carries_the_last_report_and_the_start():
    v = progress_view(track(COLD, 37298), hint=1500.0, chunk=4096)
    assert (v["done"], v["total"], v["elapsed_s"], v["at"], v["reports"]) == (12288, 37298, 7.88, T0 + 7.88, 3)
    assert (v["start"], v["start_estimated"]) == (0, False)


def test_a_first_report_without_a_rate_has_no_known_start():
    # No recent rate: a resumed prompt's 21,751 after 0.06 s cannot be told from a cold one's work.
    t = track(RESUMED, 29989, rate=None)
    assert (t.start, t.start_known) == (0, False)
    assert progress_view(t, hint=None, chunk=4096)["start_known"] is False
    assert prompt_of(t) is None
    assert progress_view(track(COLD, 37298), hint=1500.0, chunk=4096)["start_known"] is True


def test_prompt_of_is_the_prompt_size_and_its_cached_start_once_reported():
    assert prompt_of(Track()) is None
    assert prompt_of(track(COLD, 37298)) == {"total": 37298, "cached": 0}
    assert prompt_of(track(BLOCK, 48872)) == {"total": 48872, "cached": 12288}


def test_before_any_report_a_lone_prompt_counts_from_its_slot_at_the_recent_rate():
    v = progress_view(Track(), hint=1500.0, chunk=4096, since=T0)
    assert v["total"] is None and v["done"] == 0 and v["next"] == 4096
    assert v["start_known"] is False
    assert v["rate"] == 1500.0 and v["at"] > T0
    assert progress_view(Track(), hint=None, chunk=4096, since=T0) is None
    assert progress_view(Track(), hint=1500.0, chunk=4096, since=T0, queued=True) is None
    assert progress_view(Track(), hint=1500.0, chunk=None, since=T0) is None  # no cap for the count
    # Admitted beside another prefill, its first report can wait behind that prompt's chunks: no count.
    assert progress_view(Track(), hint=1500.0, chunk=4096, since=T0, sharing=2, alone=False) is None
    # Admitted alone, it keeps counting when another prompt starts, at the shared rate.
    assert progress_view(Track(), hint=1500.0, chunk=4096, since=T0, sharing=2)["rate"] == 750.0


def test_a_queued_stream_gets_no_estimate_but_keeps_its_reports():
    v = progress_view(track(COLD[:2], 37298), hint=1500.0, chunk=4096, queued=True)
    assert v["done"] == 8192 and v["rate"] == pytest.approx(4096 / 2.65, rel=1e-3)


def test_a_stray_report_for_another_prompt_is_ignored_after_two_that_agree():
    t = track(COLD[:2], 37298)
    assert add_report(t, Report(12288, 50000, 8.0, T0 + 8), rate=1500.0, chunk=4096) == t
    one = add_report(track(COLD[:1], 37298), Report(4096, 50000, 2.6, T0 + 50), rate=1500.0, chunk=4096)
    assert len(one.reports) == 1 and one.reports[0].total == 50000  # one report proves nothing


def test_old_reports_are_bounded():
    many = track([(4096 * i, 2.7 * i) for i in range(1, 40)], 262144).reports
    assert len(many) <= 4 and many[0].done == 4096 and many[-1].done == 4096 * 39


def test_a_report_older_than_the_last_is_ignored():
    t = track(COLD[:2], 37298)
    assert add_report(t, Report(4096, 37298, 2.56, T0 + 2.56), rate=1500.0, chunk=4096) == t


def test_chunk_is_learned_from_two_steps_that_agree_before_the_last_chunk():
    assert chunk_of(track(RESUMED[:3], 29989).reports) == 4096
    assert chunk_of(track(RESUMED[:2], 29989).reports) is None  # one step could be a lost line's 8,192
    assert chunk_of(track([(4096, 2.6), (12288, 7.9), (16384, 10.6)], 37298).reports) is None
    assert chunk_of(track([(36864, 24.28), (37298, 24.84)], 37298).reports) is None  # a final chunk is short


# -- the browser's replay fixture (tools/web_tests/progress.test.mjs) ---------------------------------
FIXTURE = Path(__file__).parent / "fixtures" / "prefill_replay.json"
CASES = {  # whole logged sequences: (done, elapsed) reports, prompt total, cached tokens from the turn line
    "cold": ([(4096 * i, e) for i, e in enumerate((2.56, 5.21, 7.88, 10.57, 13.29, 16.02, 18.77, 21.52, 24.28), 1)]
             + [(37298, 24.84)], 37298, 0),
    "block": ([(12288 + 4096 * i, e) for i, e in enumerate((2.70, 5.38, 8.08, 10.80, 13.53, 16.86, 19.89, 22.89), 1)]
              + [(48872, 25.87)], 48872, 12288),
    "resumed": (RESUMED + [(29989, 5.59)], 29989, 21743),
}


def replay_fixture() -> dict:
    out = {}
    for name, (rows, total, cached) in CASES.items():
        views, t = [progress_view(Track(), hint=1500.0, chunk=4096, since=T0 - 0.25)], Track()
        for done, elapsed in rows:
            t = add_report(t, Report(done, total, elapsed, T0 + elapsed), rate=1500.0, chunk=4096)
            views.append(progress_view(t, hint=1500.0, chunk=4096, since=T0 - 0.25))
        out[name] = {"total": total, "cached": cached, "reports": rows, "views": views}
    return out


def test_the_browser_replay_fixture_matches_the_server():
    # Regenerate: python -c "import json,tests.test_prefill as t; t.FIXTURE.write_text(json.dumps(t.replay_fixture()))"
    assert json.loads(FIXTURE.read_text()) == json.loads(json.dumps(replay_fixture()))
