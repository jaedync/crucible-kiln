import time

import pytest

from kiln.store import queries, speeds
from kiln.store.db import connect

NOW = time.mktime((2026, 10, 18, 15, 0, 0, 0, 0, -1))
MIDNIGHT = queries.local_midnight(NOW)


def test_the_average_is_all_tokens_over_all_seconds_and_the_range_is_p5_to_p95():
    # 100 tokens at 50 tok/s (2 s) and 300 at 100 tok/s (3 s): 400 tokens in 5 s, not the mean 75.
    s = speeds.summary([(100, 50.0), (300, 100.0)], 1)
    assert s == {"avg": 80.0, "p5": 52.5, "p95": 97.5, "n": 2}
    # 21 requests at 10..30 tok/s: the 5th and 95th percentiles fall on the 2nd and 20th.
    s = speeds.summary([(10, float(r)) for r in range(10, 31)], 1)
    assert (s["p5"], s["p95"], s["n"]) == (11.0, 29.0, 21)
    assert speeds.summary([(5, 20.0)], 0) == {"avg": 20.0, "p5": 20.0, "p95": 20.0, "n": 1}
    assert speeds.summary([], 1) is None
    assert speeds.summary([(0, 20.0), (10, 0.0)], 1) is None  # nothing to time


def test_ttft_is_the_plain_mean_and_p5_to_p95_of_the_waits():
    # 21 waits of 1..21 s: mean 11, the 5th and 95th percentiles on the 2nd and 20th.
    assert speeds.waits([float(s) for s in range(1, 22)], 2) == {"avg": 11.0, "p5": 2.0, "p95": 20.0, "n": 21}
    assert speeds.waits([0.25, None, -1.0], 2) == {"avg": 0.25, "p5": 0.25, "p95": 0.25, "n": 1}
    assert speeds.waits([], 2) is None


def _add(conn, rid, ts, profile="daily", host="test", status=200, completion=300, decode_s=5.0, prefill=2048,
         prefill_s=2.0, generated=None, ttft=2.5):
    conn.execute("INSERT INTO requests (id, source, host, ts_start, ts_end, status, profile, prompt, cached,"
                 " completion, ttft_s, engine_decode_s, engine_generated_tokens, engine_prefill_tokens,"
                 " engine_prefill_s, engine_joined_at) VALUES (?, 'gateway', ?, ?, ?, ?, ?, ?, 0, ?, ?, ?, ?, ?, ?, ?)",
                 (rid, host, ts, ts + 10, status, profile, prefill, completion, ttft, decode_s, generated, prefill,
                  prefill_s, ts + 10))


@pytest.fixture
def conn():
    c = connect(":memory:")
    _add(c, "a", MIDNIGHT + 3600)                                     # 60 tok/s, prefill 1,024 tok/s
    _add(c, "b", MIDNIGHT + 7200, completion=600, decode_s=6.0)       # 100 tok/s
    _add(c, "c", MIDNIGHT + 7300, prefill=500, prefill_s=0.5, ttft=0.5)  # 60 tok/s; too small a prefill to count
    _add(c, "d", MIDNIGHT + 7400, completion=20, ttft=None)           # too few tokens for a decode rate; no TTFT
    _add(c, "e", MIDNIGHT + 7500, completion=200, generated=150, decode_s=3.0)  # a closed think turn: 50 tok/s
    _add(c, "old", MIDNIGHT - 60, completion=900, decode_s=1.0)       # yesterday
    _add(c, "fail", MIDNIGHT + 7600, status=500, decode_s=1.0)
    _add(c, "other", MIDNIGHT + 100, profile="night", completion=400, decode_s=4.0)
    _add(c, "remote", MIDNIGHT + 100, host="remote", decode_s=1.0)
    return c


def test_todays_speeds_per_profile_count_only_this_hosts_good_requests_of_the_day(conn):
    s = queries.day_speeds(conn, NOW, "test")
    assert set(s) == {"daily", "night"}
    d = s["daily"]["decode"]
    # 300 + 600 + 300 + 150 tokens in 5 + 6 + 5 + 3 s; the generated tokens of a closed think turn, not all 200.
    assert (d["avg"], d["n"]) == (round(1350 / 19, 1), 4)
    assert s["daily"]["prefill"] == {"avg": 1024.0, "p5": 1024.0, "p95": 1024.0, "n": 4}  # c's 500 tokens left out
    assert s["night"]["decode"]["avg"] == 100.0
    # Every good request with a first token waits, small prompts too: a, b, c and e (d has none).
    assert s["daily"]["ttft"] == {"avg": 2.0, "p5": 0.8, "p95": 2.5, "n": 4}
    assert queries.day_speeds(conn, NOW, None)["daily"]["decode"]["n"] == 5  # every host


def test_the_today_bundle_carries_this_hosts_speeds(conn):
    today = queries.today_bundle(conn, NOW, "test", [])[0]
    assert today["speeds"]["daily"]["decode"]["n"] == 4
