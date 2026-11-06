import pytest

from kiln.collect.spread import ensure_current, rebuild_series
from kiln.live.apportion import apportion
from tests.test_ingest import HALOGEN, H_LINE, env, gw_record, series  # noqa: F401 (env is a fixture)

# A Halogen tool call: a little reasoning streams at first, then the call's arguments are generated
# for about 16 s without a single stream event and arrive in a burst at the end.
TOOL_LINE = H_LINE.replace("mtp 100 tok in 2.00s = 50.00 t/s", "mtp 1010 tok in 17.46s = 57.85 t/s")
TOOL_HIST = [9, 34] + [0] * 15 + [1, 11]


def tool_record(rid="t1"):
    return {**gw_record(rid, completion=1010, events=55), "ts_first_token": 101.8, "ts_last_token": 119.23,
            "ts_end": 119.28, "finish": "tool_calls", "tokens_by_s": [101, TOOL_HIST]}


def test_apportion_never_piles_rounding_into_the_last_second():
    # 24,000 tokens over 872.7 s is 27.5 tok/s; rounding each second alone left 434 for the last one.
    out = dict(apportion(1000.2, 1000.2 + 872.7272727, 24000))
    assert sum(out.values()) == 24000
    inner = [n for s, n in out.items() if 1001 <= s <= 1871]
    assert max(inner) - min(inner) <= 1 and max(out.values()) <= 28


def test_apportion_edge_cases_still_hold():
    assert apportion(5.0, 5.0, 7) == [(5, 7)]
    assert apportion(None, 5.0, 7) == [] and apportion(5.0, 4.0, 7) == [] and apportion(1.0, 2.0, 0) == []
    assert apportion(10.5, 12.5, 4) == [(10, 1), (11, 2), (12, 1)]


@pytest.mark.parametrize("engine_first", [False, True])
def test_bursty_stream_is_spread_over_the_engine_decode_time(env, engine_first):
    ing, conn = env
    steps = [lambda: ing.gateway_record(tool_record()), lambda: ing.engine_line(HALOGEN, TOOL_LINE, 119.3)]
    notes = [n for step in (steps[::-1] if engine_first else steps) for n in step()]
    dec = series(conn, "decode")
    assert sum(dec.values()) == 1010
    # 1010 tokens in 17.46 s is 57.8 tok/s: no 600-token spike, and no fake silence while the
    # tool call was being generated.
    assert max(dec.values()) <= 58 and min(dec[s] for s in range(102, 119)) >= 57
    assert not [n for n in notes if n[0] not in ("request", "exactness", "prefill_rate", "kv")]


def test_held_back_stream_without_engine_timing_is_spread_over_its_own_token_window(env):
    ing, conn = env
    ing.gateway_record(tool_record())
    # Before the engine line joins (or if it never does), the burst of held-back arguments must not show
    # as hundreds of tok/s: the gateway's first-to-last token window (101.8-119.23 s) is the evidence.
    first = series(conn, "decode")
    assert sum(first.values()) == 1010 and max(first.values()) <= 59
    ing.settle(10_000)  # the engine line never came
    dec = series(conn, "decode")
    assert sum(dec.values()) == 1010 and max(dec.values()) <= 59


def test_a_faithful_stream_without_engine_timing_keeps_its_arrival_shape(env):
    ing, conn = env
    ing.gateway_record(even_record())
    ing.settle(10_000)
    dec = series(conn, "decode")
    assert sum(dec.values()) == 436 and dec[106] < 35 and dec[104] > 55


def _snapshot(conn):
    return {tuple(r[:5]): (r[5], r[6]) for r in conn.execute(
        "SELECT t, host, profile, model, client_key, decode, prefill FROM series_1s") if r[5] or r[6]}


def test_rebuild_reproduces_the_incremental_series(env):
    ing, conn = env
    ing.gateway_record(tool_record())
    ing.engine_line(HALOGEN, TOOL_LINE, 119.3)  # joined stream
    ing.gateway_record(gw_record("g2", t0=200.0))  # stream that never got an engine line
    ing.gateway_record({**gw_record("g3", t0=300.0), "ts_first_token": None, "ts_last_token": None,
                        "tokens_by_s": None})  # not streamed
    ing.engine_line(HALOGEN, H_LINE.replace("prompt 5000", "prompt 6000"), 500.0)  # bypassed the gateway
    ing.settle(990.0)
    before = _snapshot(conn)
    assert {k[4] for k in before} == {"198.51.100.10", "127.0.0.1"}
    rebuild_series(conn)
    assert _snapshot(conn) == before


def test_history_is_rebuilt_once_per_spreading_version(env):
    _, conn = env
    stale = "INSERT INTO series_1s (t, host, profile, model, client_key, decode) VALUES (5, 'h', 'p', 'm', 'c', 999)"
    conn.execute(stale)
    assert ensure_current(conn) is True
    assert conn.execute("SELECT COUNT(*) FROM series_1s").fetchone()[0] == 0
    conn.execute(stale)
    assert ensure_current(conn) is False
    assert conn.execute("SELECT COUNT(*) FROM series_1s").fetchone()[0] == 1


# Crucible: about one stream event per token. The rate dips while another stream prefills.
EVEN_HIST = [37, 50, 61, 63, 57, 29, 29, 29, 36, 43]
EVEN_LINE = H_LINE.replace("mtp 100 tok in 2.00s = 50.00 t/s", "mtp 436 tok in 9.80s = 44.49 t/s")


def even_record(rid="e1"):
    return {**gw_record(rid, completion=436, events=434), "ts_first_token": 101.1, "ts_last_token": 110.9,
            "ts_end": 111.0, "tokens_by_s": [101, EVEN_HIST]}


@pytest.mark.parametrize("engine_first", [False, True])
def test_a_faithful_stream_keeps_its_arrival_shape_when_the_engine_line_joins(env, engine_first):
    ing, conn = env
    steps = [lambda: ing.gateway_record(even_record()), lambda: ing.engine_line(HALOGEN, EVEN_LINE, 111.0)]
    for step in (steps[::-1] if engine_first else steps):
        step()
    dec = series(conn, "decode")
    assert sum(dec.values()) == 436
    assert dec[106] < 35 and dec[104] > 55  # the dip stays: no flat block


def test_faithful_needs_events_for_nearly_every_token_and_no_burst():
    from kiln.collect.spread import faithful
    steady = [(i, 50) for i in range(10)]
    assert faithful(steady, 500, 10.0)
    assert not faithful(steady, 1000, 10.0)  # half the tokens never streamed
    assert not faithful([(0, 20), (1, 0), (2, 480)], 500, 10.0)  # a flushed burst at 10 times the mean
    assert faithful([(0, 34), (1, 24)], 58, 1.37)  # a short request is judged on whole seconds
    assert not faithful([], 10, 1.0)
