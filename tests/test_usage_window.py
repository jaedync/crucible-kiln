"""The gateway's in-memory usage window behind /health last_5m and last_1h (gateway/usage_window.py)."""
import json
import time

import pytest

from kiln.gateway.records import day_file, read_tail
from kiln.gateway.usage_window import HORIZON_S, UsageWindow

DAY = 1793059200.0  # 2026-10-27 00:00 UTC


def rec(ts_start, ts_end, status=200, path="chat/completions", forwarded=True, **extra):
    r = {"v": 1, "ts_start": ts_start, "ts_end": ts_end, "status": status, "path": path,
         "profile": "p4", "client": {"key": "203.0.113.9", "label": "secret-label"}}
    if forwarded:
        r["served_model"] = "served"
    r.update(extra)
    return r


def slots_for(name):
    return {"p4": 4, "p1": 1}.get(name)


def steps(w, points, slots=4):
    for t, level in points:
        w.in_flight(t, level, slots)


def test_a_window_with_known_times_has_the_exact_mean_peak_and_saturated_seconds():
    w = UsageWindow(0.0)
    steps(w, [(700, 1), (750, 4), (760, 5), (780, 2), (900, 0)])
    s = w.summary(1000.0, 300)
    # 1 x 50 + 4 x 10 + 5 x 20 + 2 x 120 = 430 request-seconds over 300 s
    assert s["covered_s"] == 300.0
    assert s["in_flight_mean"] == round(430 / 300, 3) and s["in_flight_peak"] == 5
    assert s["saturated_s"] == 30.0 and s["utilization_mean"] == round(430 / 4 / 300, 3)
    # A window that starts inside a step: from 750 the area is 40 + 100 + 240.
    s = w.summary(1050.0, 300)
    assert s["in_flight_mean"] == round(380 / 300, 3) and s["saturated_s"] == 30.0
    # The level in force at the window start counts for the peak.
    assert w.summary(1070.0, 300)["in_flight_peak"] == 5
    s = w.summary(1200.0, 300)
    assert (s["in_flight_mean"], s["in_flight_peak"], s["saturated_s"]) == (0.0, 0, 0.0)


def test_the_slots_in_force_at_each_moment_count():
    w = UsageWindow(0.0)
    w.in_flight(100, 1, 4)
    w.in_flight(200, 0)  # an end keeps the slots in force
    w.in_flight(300, 1, 1)  # a switch to a one-slot profile: one request saturates it
    w.in_flight(400, 0)
    s = w.summary(400.0, 300)
    assert s["saturated_s"] == 100.0
    assert s["utilization_mean"] == round((100 / 4 + 100 / 1) / 300, 3)


def test_a_partial_window_after_a_restart():
    w = UsageWindow(1000.0)
    steps(w, [(1010, 2), (1060, 0)])
    s5, s1 = w.summary(1100.0, 300), w.summary(1100.0, 3600)
    assert s5["covered_s"] == s1["covered_s"] == 100.0
    assert s5["in_flight_mean"] == 1.0 and s1["in_flight_mean"] == 1.0
    assert s5["completion_tok_s"] is None or s5["completion_tok_s"] == 0.0


def test_an_empty_window_has_zero_counts_and_null_rates():
    s = UsageWindow(0.0).summary(4000.0, 300)
    assert (s["started"], s["finished"], s["ok"], s["in_flight_peak"]) == (0, 0, 0, 0)
    assert s["ttft_s_p50"] is None and s["request_tok_s_p50"] is None and s["held_s_max"] is None
    assert s["completion_tok_s"] == 0.0 and s["prompt_tokens"] == 0


def test_outcome_counts():
    w = UsageWindow(0.0)
    for i, status in enumerate([200, 200, 201, 400, 404, 409, 499, 500, 502, 503, None]):
        w.finished(rec(900 + i, 950 + i, status))
    w.finished(rec(900, 960, 200, path="messages/count_tokens"))  # a utility call is not a request
    s = w.summary(1000.0, 300)
    assert (s["finished"], s["ok"], s["client_error"], s["cancelled"], s["server_error"]) == (11, 3, 3, 1, 4)


def test_started_counts_arrivals_in_the_window():
    w = UsageWindow(0.0)
    for t in (600, 710, 720, 990):
        w.started(t)
    assert w.summary(1000.0, 300)["started"] == 3 and w.summary(1000.0, 3600)["started"] == 4


def test_ttft_percentiles_held_seconds_tokens_and_rates():
    w = UsageWindow(0.0)
    for i in range(10):
        w.finished(rec(800 + i, 900 + i, ttft_s=float(i + 1), held_s=0.5 * i,
                       usage={"prompt": 1000, "cached": 0, "completion": 101},
                       ts_first_token=850.0, ts_last_token=860.0, token_events=101))
    w.finished(rec(800, 950, 499, usage={"prompt": 50, "completion": None}))  # no tokens, no TTFT
    s = w.summary(1000.0, 300)
    assert (s["ttft_s_p50"], s["ttft_s_p95"]) == (5.5, 9.55)  # linear between ranks, as store/speeds.py
    assert (s["held_s_total"], s["held_s_max"]) == (22.5, 4.5)
    assert (s["prompt_tokens"], s["completion_tokens"]) == (10050, 1010)
    assert s["completion_tok_s"] == round(1010 / 300, 1)
    assert s["request_tok_s_p50"] == 10.0  # the 100 tokens after the first event over 10 s


def test_tiny_replies_have_no_request_rate():
    w = UsageWindow(0.0)
    w.finished(rec(800, 900, usage={"completion": 5}, ts_first_token=850.0, ts_last_token=851.0, token_events=5))
    assert w.summary(1000.0, 300)["request_tok_s_p50"] is None


def test_old_entries_leave_the_window_and_memory():
    w = UsageWindow(0.0)
    for i in range(500):
        t = float(i)
        w.started(t)
        w.in_flight(t, 1, 4)
        w.in_flight(t + 0.5, 0)
        w.finished(rec(t, t + 0.5, ttft_s=0.1, held_s=0.1, usage={"prompt": 1, "completion": 1}))
    late = 500 + HORIZON_S + 600
    w.started(late)
    w.in_flight(late, 1, 4)
    w.finished(rec(late, late, ttft_s=0.1, usage={"prompt": 1, "completion": 1}))
    assert w.size() <= 20
    s = w.summary(late + 1, 3600)
    assert (s["started"], s["finished"]) == (1, 1) and s["in_flight_peak"] == 1


def test_the_count_cap_bounds_memory_and_shortens_the_covered_window():
    w = UsageWindow(0.0, cap=100)
    for i in range(1000):
        t = 1000.0 + i
        w.started(t)
        w.in_flight(t, 1, 4)
        w.in_flight(t + 0.5, 0)
        w.finished(rec(t, t + 0.5))
    assert w.size() <= 20 * 110
    s = w.summary(2000.0, 3600)
    assert s["covered_s"] < 1000 and s["finished"] <= 110
    # What it reports is still exact for the part it covers: one request per second.
    assert abs(s["finished"] - s["covered_s"]) <= 1


def test_seed_rebuilds_the_last_hour_from_records():
    now = 10_000.0
    w = UsageWindow(now)
    records = [rec(now - 4000, now - 3700),  # ended before the window: ignored
               rec(now - 3700, now - 3000, held_s=100.0),  # in flight from now - 3600 to now - 3000
               rec(now - 2000, now - 1000, ttft_s=2.0, usage={"prompt": 10, "completion": 20}),
               rec(now - 1500, now - 500, profile="p1"),
               rec(now - 100, now - 100, 503, forwarded=False),  # rejected: finished, never in flight
               rec(now - 90, now - 80, path="tokenize"),
               rec(now - 10, now + 50)]  # ends after now: only after a clock step back; ignored
    w.seed(records, complete=True, now=now, slots_for=slots_for)
    s = w.summary(now, 3600)
    assert s["covered_s"] == 3600.0
    assert (s["started"], s["finished"], s["ok"], s["server_error"]) == (3, 4, 3, 1)
    assert s["in_flight_peak"] == 2
    assert s["in_flight_mean"] == round((600 + 1000 + 1000) / 3600, 3)
    # From now - 1500 the slots in force are the one-slot profile's: saturated until the last request ends.
    assert s["saturated_s"] == 1000.0
    assert (s["ttft_s_p50"], s["completion_tokens"], s["held_s_total"]) == (2.0, 20, 100.0)


def test_a_seed_that_stopped_at_the_cap_reports_a_partial_window():
    now = 10_000.0
    w = UsageWindow(now)
    w.seed([rec(now - 600, now - 500), rec(now - 400, now - 300)], complete=False, now=now, slots_for=slots_for)
    assert w.summary(now, 3600)["covered_s"] == 500.0


# -- reading the record tails ------------------------------------------------------------------
def write_lines(path, lines):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("".join(l + "\n" for l in lines))


def test_read_tail_reads_back_to_the_window_start_and_skips_bad_lines(tmp_path):
    now = DAY + 1800  # 00:30 UTC: the window starts yesterday
    write_lines(day_file(tmp_path, DAY - 10), [json.dumps(rec(DAY - 8000, DAY - 7000)),
                                               json.dumps(rec(DAY - 1000, DAY - 900)),
                                               json.dumps(rec(DAY - 100, DAY + 60))])  # crossed midnight
    write_lines(day_file(tmp_path, DAY), [json.dumps(rec(DAY + 10, DAY + 20)), "{broken",
                                          json.dumps([1]), json.dumps(rec(DAY + 30, DAY + 40))[:-5]
                                          + "\n" + json.dumps(rec(DAY + 50, DAY + 70))])
    got, complete = read_tail(tmp_path, now - HORIZON_S, now, limit=100)
    assert complete and [r["ts_end"] for r in got] == [DAY - 900, DAY + 20, DAY + 60, DAY + 70]


def test_read_tail_without_files(tmp_path):
    assert read_tail(tmp_path / "missing", 0.0, DAY, limit=10) == ([], True)


def test_read_tail_stops_at_the_limit(tmp_path):
    write_lines(day_file(tmp_path, DAY), [json.dumps(rec(DAY + i, DAY + i)) for i in range(50)])
    got, complete = read_tail(tmp_path, DAY, DAY + 100, limit=10)
    assert not complete and [r["ts_end"] for r in got] == [DAY + i for i in range(40, 50)]


def test_read_tail_crosses_block_boundaries(tmp_path, monkeypatch):
    monkeypatch.setattr("kiln.gateway.records.TAIL_BLOCK", 64)
    lines = [json.dumps(rec(DAY + i, DAY + i, ttft_s=0.25)) for i in range(30)]
    write_lines(day_file(tmp_path, DAY), lines)
    got, complete = read_tail(tmp_path, DAY + 10, DAY + 100, limit=100)
    assert complete and [r["ts_end"] for r in got] == [DAY + i for i in range(10, 30)]


# -- cost -----------------------------------------------------------------------------------------
def busy_hour(w, now, n=3000):
    """n requests over the hour before now, up to 6 at once (slots 4)."""
    for i in range(n):
        t0 = now - HORIZON_S + i * HORIZON_S / n
        w.started(t0)
        w.in_flight(t0 + 0.01, 1 + i % 6, 4)
        w.finished(rec(t0, t0 + 1.0, 200 if i % 50 else 499, ttft_s=0.3 + (i % 7) / 10, held_s=None,
                       usage={"prompt": 20000 + i, "cached": 18000, "completion": 400 + i % 300},
                       ts_first_token=t0 + 0.3, ts_last_token=t0 + 0.9, token_events=400))
        w.in_flight(t0 + 1.0, i % 6)


def test_a_busy_hour_summary_is_cheap():
    now = 1_000_000.0
    w = UsageWindow(now - 2 * HORIZON_S)
    busy_hour(w, now)
    times = []
    for _ in range(300):
        t = time.perf_counter()
        w.summary(now, 300)
        w.summary(now, 3600)
        times.append(time.perf_counter() - t)
    times.sort()
    # The median, not p99: a slow summary makes every call slow, while a p99 on a shared CI runner
    # measures the scheduler.
    assert times[len(times) // 2] < 0.005
    assert w.summary(now, 3600)["finished"] == 3000


def test_summary_has_only_aggregate_numbers():
    w = UsageWindow(0.0)
    w.finished(rec(900, 950, ttft_s=1.0))
    s = w.summary(1000.0, 300)
    assert all(v is None or isinstance(v, (int, float)) for v in s.values())
    assert "secret-label" not in json.dumps(s) and "203.0.113.9" not in json.dumps(s)


@pytest.mark.parametrize("bad", [{"ts_end": "x", "path": "chat/completions"}, {"path": "chat/completions"},
                                 {"ts_end": 950.0, "ts_start": 900.0, "path": "chat/completions",
                                  "status": "200", "usage": "x", "ttft_s": "1", "held_s": [1]}])
def test_odd_records_never_raise(bad):
    w = UsageWindow(0.0)
    w.finished(bad)
    w.seed([bad], complete=True, now=1000.0, slots_for=slots_for)
    w.summary(1000.0, 300)
