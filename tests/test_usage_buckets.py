"""The usage bars: /usage `buckets` (store/buckets.py), in local hours and days."""
import os
import time

import pytest

from kiln.store import buckets, queries
from tests.test_crucible_ingest import env  # noqa: F401 (a fixture)
from tests.test_ingest import gw_record


@pytest.fixture(autouse=True)
def new_york():
    old = os.environ.get("TZ")
    os.environ["TZ"] = "America/New_York"
    time.tzset()
    yield
    if old is None:
        os.environ.pop("TZ")
    else:
        os.environ["TZ"] = old
    time.tzset()


def local(*ymdhm):
    y, mo, d, h, mi = ymdhm
    return time.mktime((y, mo, d, h, mi, 0, 0, 0, -1))


def test_today_has_every_hour_of_the_day_and_costs_land_in_their_start_hour(env):
    ing, conn = env
    now = local(2026, 10, 27, 14, 30)
    ing.gateway_record(gw_record("a", t0=local(2026, 10, 27, 9, 10)))
    ing.gateway_record(gw_record("b", t0=local(2026, 10, 27, 9, 50)))
    ing.gateway_record(gw_record("c", t0=local(2026, 10, 27, 13, 59)))
    u = queries.usage(conn, "today", "device", now)
    b = u["buckets"]
    assert b["step_s"] == 3600 and len(b["t"]) == 24 and b["tz"]
    assert b["local"][0] == "2026-10-27 00:00" and b["local"][13] == "2026-10-27 13:00"
    assert b["requests"][9] == 2 and b["requests"][13] == 1 and sum(b["requests"]) == 3
    (series,) = b["cost"].values()
    assert sum(series) == pytest.approx(u["totals"]["cost"]["reference"])
    assert series[9] == pytest.approx(2 * series[13]) and series[14] == 0


def test_a_daylight_saving_day_has_25_hours():
    start = local(2026, 11, 1, 0, 0)
    starts = buckets.edges("today", start, start + 3600)
    assert len(starts) == 25
    assert [time.localtime(t).tm_hour for t in starts[:4]] == [0, 1, 1, 2]


def test_seven_days_are_hours_from_the_start_hour_to_now():
    now = local(2026, 10, 27, 14, 30)
    starts = buckets.edges("7d", now - 7 * 86400, now)
    assert len(starts) == 169  # the start's hour (partly in the period) to now's hour
    assert starts[-1] == local(2026, 10, 27, 14, 0)


def test_thirty_days_and_all_are_calendar_days(env):
    ing, conn = env
    now = local(2026, 10, 27, 14, 30)
    assert len(buckets.edges("30d", now - 30 * 86400, now)) == 31
    ing.gateway_record(gw_record("old", t0=local(2026, 10, 15, 23, 0)))
    ing.gateway_record(gw_record("new", t0=local(2026, 10, 27, 1, 0)))
    b = queries.usage(conn, "all", "model", now)["buckets"]
    assert b["step_s"] == 86400 and b["local"][0] == "2026-10-15" and b["local"][-1] == "2026-10-27"
    assert len(b["t"]) == 13 and b["requests"][0] == 1 and b["requests"][-1] == 1


def test_no_requests_still_gives_the_empty_buckets(env):
    _, conn = env
    now = local(2026, 10, 27, 14, 30)
    b = queries.usage(conn, "today", "device", now)["buckets"]
    assert len(b["t"]) == 24 and b["cost"] == {} and sum(b["requests"]) == 0
    assert len(queries.usage(conn, "all", "device", now)["buckets"]["t"]) == 1


def test_tokens_count_where_a_request_started_and_busy_time_splits_at_the_hour(env):
    ing, conn = env
    now = local(2026, 10, 27, 14, 30)
    # gw_record runs 3.1 s: this one starts 2 s before 10:00, so 2 s are busy in 09:00 and 1.1 s in 10:00.
    ing.gateway_record(gw_record("a", t0=local(2026, 10, 27, 10, 0) - 2.0))
    b = queries.usage(conn, "today", "device", now)["buckets"]
    (key,) = b["tokens"]
    assert b["tokens"][key][9] == 5000 + 100 and b["tokens"][key][10] == 0
    assert b["busy"][key][9] == pytest.approx(2.0) and b["busy"][key][10] == pytest.approx(1.1)
    assert b["busy_all"][9] == pytest.approx(2.0)


def test_busy_of_all_keys_counts_overlap_once():
    t0 = local(2026, 10, 27, 9, 0)
    items = [buckets.Item("a", t0 + 60, t0 + 660, 0.1, 10), buckets.Item("b", t0 + 360, t0 + 960, 0.2, 20)]
    b = buckets.aggregate(items, "today", local(2026, 10, 27, 0, 0), t0 + 1800, "America/New_York")
    assert b["busy"]["a"][9] == 600 and b["busy"]["b"][9] == 600
    assert b["busy_all"][9] == 900 and b["requests"][9] == 2
