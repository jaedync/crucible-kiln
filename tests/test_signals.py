"""Crucible server signals in the views: tool_call_fallback per request, error counts per engine and day."""
import json
import time

import pytest

from kiln.store import queries, signals
from tests.test_crucible_ingest import UNIT, env, error_line, turn_line  # noqa: F401 (env is a fixture)
from tests.test_dash import NOW, dash_client  # noqa: F401 (a fixture)
from tests.test_ingest import gw_record, rows

DAY = 86400.0
CRUCIBLE_REC = {"profile": "crucible-flashnext", "engine": "crucible"}


def fallback_record(rid, t0, flag):
    usage = {**gw_record()["usage"], **({"tool_call_fallback": True} if flag else {})}
    return {**gw_record(rid, t0=t0), **CRUCIBLE_REC, "usage": usage}


def local_date(ts):
    return time.strftime("%Y-%m-%d", time.localtime(ts))


def test_request_row_carries_the_fallback_flag(env):
    ing, conn = env
    ing.gateway_record(fallback_record("a", 100.0, True))
    ing.gateway_record(fallback_record("b", 200.0, False))
    got = {r["id"]: r["tool_call_fallback"] for r in queries.recent_requests(conn)}
    assert got == {"a": True, "b": False}
    conn.execute("UPDATE requests SET tool_call_fallback = NULL WHERE id = 'b'")  # a row from before the column
    assert queries.get_request(conn, "b")["tool_call_fallback"] is None


def test_usage_counts_fallbacks_per_group_and_in_totals(env):
    ing, conn = env
    ing.gateway_record(fallback_record("a", NOW - 100, True))
    ing.gateway_record(fallback_record("b", NOW - 50, True))
    ing.gateway_record(fallback_record("c", NOW - 20, False))
    u = queries.usage(conn, "today", "model", NOW)
    assert u["rows"][0]["tool_call_fallbacks"] == 2 and u["totals"]["tool_call_fallbacks"] == 2


def test_signals_count_per_day_and_engine(env):
    ing, conn = env
    today, yesterday = NOW - 100, NOW - DAY - 100
    ing.gateway_record(fallback_record("a", today, True))
    ing.gateway_record(fallback_record("b", today + 1, False))
    ing.gateway_record({**gw_record("h", t0=today + 2)})  # a Halogen request on the same day
    ing.engine_line(UNIT, error_line(), today)
    ing.engine_line(UNIT, error_line(streamed=True, status=200), today + 1)
    ing.engine_line(UNIT, error_line(), yesterday)
    ing.engine_line(UNIT, error_line(status=200, error="tool_call_fallback", completion_tokens=9), today + 2)
    out = signals.view(conn, 7, NOW)
    got = {(r["date"], r["engine"]): r for r in out["rows"]}
    t = got[(local_date(today), "crucible")]
    assert (t["requests"], t["errors"], t["stream_errors"], t["tool_call_fallbacks"]) == (2, 2, 1, 1)
    assert got[(local_date(today), "halogen")]["requests"] == 1
    assert got[(local_date(today), "halogen")]["errors"] == 0
    y = got[(local_date(yesterday), "crucible")]
    assert (y["requests"], y["errors"]) == (0, 1)
    assert [r["date"] for r in out["rows"]] == sorted((r["date"] for r in out["rows"]), reverse=True)


def test_client_errors_are_counted_apart_from_server_errors(env):
    # Oct 8: count_tokens reached a server without the route, and 179 lines of 404 and 415 showed as errors.
    ing, conn = env
    ing.engine_line(UNIT, error_line(status=404), NOW - 30)
    ing.engine_line(UNIT, error_line(status=415), NOW - 20)
    ing.engine_line(UNIT, error_line(status=500), NOW - 10)
    ing.engine_line(UNIT, error_line(status=200, streamed=True), NOW - 5)
    (row,) = signals.view(conn, 1, NOW)["rows"]
    assert (row["errors"], row["stream_errors"], row["client_errors"]) == (2, 1, 2)


def test_a_fallback_line_without_a_flagged_request_still_counts_once(env):
    ing, conn = env  # a direct request has no usage flag, only the log line
    ing.engine_line(UNIT, error_line(status=200, error="tool_call_fallback"), NOW - 10)
    ing.engine_line(UNIT, error_line(status=200, error="tool_call_fallback"), NOW - 5)
    ing.gateway_record(fallback_record("a", NOW - 4, True))
    (row,) = signals.view(conn, 1, NOW)["rows"]
    assert row["tool_call_fallbacks"] == 2 and row["errors"] == 0


def test_a_fallback_near_midnight_counts_on_the_day_it_ended(env):
    ing, conn = env
    t = time.localtime(NOW)
    midnight = time.mktime((t.tm_year, t.tm_mon, t.tm_mday, 0, 0, 0, 0, 0, -1))
    rec = fallback_record("m", midnight - 5, True)
    ing.gateway_record({**rec, "ts_end": midnight + 5})  # started yesterday, ended (and logged) today
    ing.engine_line(UNIT, error_line(status=200, error="tool_call_fallback"), midnight + 5)
    got = {r["date"]: r for r in signals.view(conn, 2, NOW)["rows"]}
    assert got[local_date(midnight - 5)]["requests"] == 1
    assert got[local_date(midnight - 5)]["tool_call_fallbacks"] == 0
    assert got[local_date(midnight + 5)]["tool_call_fallbacks"] == 1


def test_a_request_without_an_end_buckets_its_flag_by_its_start(env):
    ing, conn = env
    ing.gateway_record({**fallback_record("c", NOW - 50, True), "ts_end": None})
    (row,) = signals.view(conn, 1, NOW)["rows"]
    assert row["tool_call_fallbacks"] == 1


def test_signals_stay_inside_the_window_and_host(env):
    ing, conn = env
    ing.engine_line(UNIT, error_line(), NOW - 20 * DAY)
    assert signals.view(conn, 7, NOW)["rows"] == []
    assert len(signals.view(conn, 30, NOW)["rows"]) == 1
    assert signals.view(conn, 30, NOW, host="remote")["rows"] == []


async def test_signals_endpoint_and_request_rows(dash_client):
    d, client = await dash_client()
    await d._on_records([fallback_record("a", NOW - 60, True)], {"requests-x.jsonl": 5})
    await d._on_journal([(UNIT, error_line(error="SECRET text"), NOW - 30, "c1")], "c1")
    res = await client.get("/api/v1/signals?days=3")
    body = await res.json()
    assert res.status == 200 and body["days"] == 3
    (row,) = body["rows"]
    assert (row["engine"], row["errors"], row["tool_call_fallbacks"], row["requests"]) == ("crucible", 1, 1, 1)
    assert "SECRET" not in json.dumps(body)
    reqs = (await (await client.get("/api/v1/requests")).json())["rows"]
    assert reqs[0]["tool_call_fallback"] is True
    assert (await client.get("/api/v1/signals?days=x")).status == 400
    assert (await client.get("/api/v1/signals?host=nope")).status == 400
