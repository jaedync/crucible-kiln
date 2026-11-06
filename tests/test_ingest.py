from pathlib import Path

import pytest

from kiln.collect import engines
from kiln.collect.ingest import SETTLE_S, Ingestor
from kiln.pricing.cards import PriceBook
from kiln.store.db import connect

FIX = Path(__file__).parent / "fixtures"
HALOGEN = "kiln-halogen-flashnext"
H_LINE = ("serve_api: mtp 100 tok in 2.00s = 50.00 t/s | 40 rounds, commit 2.50/round | prompt 5000 (4000 cached,"
          " 80.0%), prefill 0.50s (1000 new) | detok 18us/tok | pool 9000/524288 2% | think on, closed at 90 by"
          " answer room")


@pytest.fixture
def env(make_cfg):
    cfg = make_cfg()
    conn = connect(":memory:")
    book = PriceBook(conn)
    book.add("reference", "*", "anthropic/claude-sonnet-5.5", 2.0, 0.2, 10.0, now=0)
    ing = Ingestor(conn, cfg, book, clock=lambda: 1000.0)
    ing.watermarks = {"journal": 1e12, "gateway": 1e12}
    return ing, conn


def gw_record(rid="g1", t0=100.0, completion=100, prompt=5000, cached=4000, events=100):
    return {"id": rid, "ts_start": t0, "ts_first_token": t0 + 1.0, "ts_last_token": t0 + 3.0, "ts_end": t0 + 3.1,
            "client": {"key": "198.51.100.10", "name": "Owner's laptop", "node": "owner-laptop", "peer_ip": "198.51.100.10"},
            "profile": "halogen-flashnext", "engine": "halogen", "model": "qwen3.8-flash-next", "path": "chat/completions",
            "status": 200, "finish": "stop", "token_events": events, "ttft_s": 1.0, "latency_s": 3.1,
            "usage": {"prompt": prompt, "cached": cached, "completion": completion, "reasoning": None},
            "tokens_by_s": [int(t0 + 1), [completion // 2, completion - completion // 2]]}


def rows(conn):
    return conn.execute("SELECT * FROM requests ORDER BY ts_start").fetchall()


def series(conn, col):
    return {r["t"]: r[col] for r in conn.execute(f"SELECT t, SUM({col}) AS {col} FROM series_1s GROUP BY t")
            if r[col]}


def test_gateway_then_engine_line_joins_and_uses_engine_prefill(env):
    ing, conn = env
    notes = ing.gateway_record(gw_record())
    assert ("request", "g1") in notes and ("exactness", "halogen-flashnext", 100, 100) in notes
    assert series(conn, "decode") == {101: 50, 102: 50}
    notes = ing.engine_line(HALOGEN, H_LINE, 103.5)
    (r,) = rows(conn)
    assert (r["source"], r["engine_prefill_tokens"], r["engine_prefill_s"], r["think_close"]) == \
        ("gateway", 1000, 0.5, "answer_room")
    assert (r["kv_pool_used"], r["spec_rounds"], r["series_done"]) == (9000, 40, 1)
    # Prefill spread over the half second before the first token (100.5 to 101.0).
    assert series(conn, "prefill") == {100: 1000}
    assert ("kv", "halogen-flashnext", 9000, 524288, 103.5) in notes
    cost = conn.execute("SELECT usd FROM request_costs WHERE request_id='g1' AND source='reference'").fetchone()[0]
    assert cost == pytest.approx((1000 * 2 + 4000 * 0.2 + 100 * 10) / 1e6)


def test_engine_line_first_waits_then_is_absorbed(env):
    ing, conn = env
    ing.engine_line(HALOGEN, H_LINE, 103.0)
    assert [r["source"] for r in rows(conn)] == ["engine_pending"]
    ing.gateway_record(gw_record())
    (r,) = rows(conn)
    assert r["source"] == "gateway" and r["engine_prefill_tokens"] == 1000 and r["engine_line_id"]
    ing.engine_line(HALOGEN, H_LINE, 103.0)  # replayed after a restart
    assert len(rows(conn)) == 1
    assert ing.settle(10_000) == []


def test_unmatched_engine_line_becomes_a_direct_request(env):
    ing, conn = env
    ing.engine_line(HALOGEN, H_LINE, 200.0)
    assert ing.settle(200.0 + SETTLE_S - 1) == []
    notes = ing.settle(200.0 + SETTLE_S + 1)
    (r,) = rows(conn)
    # The engines listen on loopback: a request that bypassed the gateway came from this host. The device is
    # the host (keyed like the gateway's own local requests), the container its label.
    assert (r["source"], r["client_key"], r["client_name"], r["client_label"], r["model"]) == \
        ("direct", "127.0.0.1", "test", HALOGEN, "qwen3.8-flash-next")
    assert [tuple(c) for c in conn.execute("SELECT key, name FROM clients")] == [("127.0.0.1", "test")]
    assert sum(series(conn, "decode").values()) == 100 and sum(series(conn, "prefill").values()) == 1000
    assert notes == [("request", r["id"])]
    assert conn.execute("SELECT usd FROM request_costs WHERE request_id=? AND source='reference'", (r["id"],)).fetchone()[0] > 0


def test_settle_waits_for_both_sources(env):
    ing, conn = env
    ing.engine_line(HALOGEN, H_LINE, 200.0)
    ing.watermarks["gateway"] = 150.0  # the gateway files haven't been read up to this line yet
    assert ing.settle(10_000) == [] and rows(conn)[0]["source"] == "engine_pending"


def test_gateway_without_engine_line_uses_usage_for_prefill(env):
    ing, conn = env
    ing.gateway_record(gw_record())
    ing.settle(10_000)
    # (prompt - cached) over request start to first token: 100.0 to 101.0
    assert series(conn, "prefill") == {100: 1000}
    assert rows(conn)[0]["series_done"] == 1


def test_records_are_idempotent(env):
    ing, conn = env
    ing.gateway_record(gw_record())
    assert ing.gateway_record(gw_record()) == []
    assert series(conn, "decode") == {101: 50, 102: 50}


def test_cancelled_gateway_request_joins_by_streamed_count(env):
    ing, conn = env
    rec = {**gw_record(events=99), "status": 499, "usage": None}
    ing.gateway_record(rec)
    ing.engine_line(HALOGEN, H_LINE, 103.5)
    assert len(rows(conn)) == 1 and rows(conn)[0]["engine_prefill_tokens"] == 1000


def test_a_client_that_left_after_the_usage_report_is_a_completed_request(env):
    ing, conn = env
    whole = {**gw_record("w1"), "status": 499, "error": "client_disconnected", "finish": "tool_calls"}
    cut = {**gw_record("c1", t0=200.0), "status": 499, "error": "client_disconnected", "finish": None, "usage": None}
    ing.gateway_record(whole)
    ing.gateway_record(cut)
    got = {r["id"]: (r["status"], r["error"]) for r in rows(conn)}
    assert got == {"w1": (200, None), "c1": (499, "client_disconnected")}


def test_migration_counts_old_whole_499_rows_as_completed():
    from kiln.store.db import connect, migrate
    conn = connect(":memory:")
    conn.execute("DELETE FROM schema_migrations WHERE version = 11")
    ins = ("INSERT INTO requests (id, source, host, ts_start, status, error, finish, completion)"
           " VALUES (?, 'gateway', 'h', 1.0, ?, ?, ?, ?)")
    conn.execute(ins, ("whole", 499, "client_disconnected", "stop", 40))
    conn.execute(ins, ("cut", 499, "client_disconnected", None, None))
    conn.execute(ins, ("bad", 502, "upstream: ClientPayloadError", "stop", 40))
    migrate(conn)
    got = {r["id"]: (r["status"], r["error"]) for r in conn.execute("SELECT id, status, error FROM requests")}
    assert got == {"whole": (200, None), "cut": (499, "client_disconnected"), "bad": (502, "upstream: ClientPayloadError")}


def test_eval_containers_learn_their_model_and_parse_errors_are_kept(env):
    ing, conn = env
    ing.engine_line("eval-gufo-x", "2026-09-01 10:05:44 [INFO] [loader] event=load_completed kind=text"
                    " model=Qwen3.8 Flash Next sessions=1 context_tokens=1", 50.0)
    ing.engine_line("eval-gufo-x", "2026-09-01 10:06:00 [INFO] [http] request=r2 event=completed method=POST"
                    " path=/v1/chat/completions status=200 outcome=completed prompt_tokens=55 prefill_tokens=55"
                    " generated_tokens=34 finish=stop cached_tokens=0 queue_ms=0.3 ttft_ms=420.4"
                    " prefill_tps=133.6 decode_tps=34.3", 60.0)
    ing.settle(1000)
    (r,) = rows(conn)
    assert (r["client_key"], r["client_name"], r["client_label"], r["model"]) == \
        ("127.0.0.1", "test", "eval-gufo-x", "qwen3.8-flash-next")
    assert r["engine_decode_s"] == pytest.approx(34 / 34.3)
    assert ing.engine_line(HALOGEN, "serve_api: mtp 5 tok in garbage", 70.0) == [("parse_error", HALOGEN)]
    assert conn.execute("SELECT COUNT(*) FROM engine_events WHERE kind='parse_error'").fetchone()[0] == 1


def test_fixture_logs_parse_without_errors():
    counts = {}
    for f in FIX.glob("*.log"):
        for line in f.read_text().splitlines():
            out = engines.parse(line)
            if out:
                counts[(out[0], out[1])] = counts.get((out[0], out[1]), 0) + 1
    assert counts[("halogen", "completion")] == 37
    assert counts[("gufo", "completion")] == 37 + 15
    assert counts[("gufo", "model")] >= 2


def test_halogen_prefill_progress_note_carries_timing(env):
    ing, _ = env
    notes = ing.engine_line(HALOGEN, "flash_serve: req 4 prefill 32768/74452 tokens, 17.7 s", 200.0)
    assert notes == [("progress", "halogen-flashnext", 32768, 74452, 17.7, 200.0, engines.HALOGEN_PREFILL_STEP, 4)]
    assert engines.HALOGEN_PREFILL_STEP == 32768


def test_cold_prefills_report_a_rate_hint_and_small_ones_do_not(env):
    ing, conn = env
    cold = H_LINE.replace("prompt 5000 (4000 cached, 80.0%), prefill 0.50s (1000 new)",
                          "prompt 52414, prefill 28.00s = 1871.93 t/s")
    notes = ing.engine_line(HALOGEN, cold, 300.0)
    assert ("prefill_rate", "halogen-flashnext", pytest.approx(52414 / 28.0)) in notes
    assert not any(n[0] == "prefill_rate" for n in ing.engine_line(HALOGEN, H_LINE, 400.0))
    from kiln.store import queries
    ing.settle(10_000)
    assert queries.recent_prefill_rates(conn) == {"halogen-flashnext": pytest.approx(52414 / 28.0)}


def test_halogen_line_fields():
    _, kind, rec = engines.parse(H_LINE)
    assert kind == "completion"
    assert (rec["prompt"], rec["cached"], rec["prefill_tokens"], rec["completion"]) == (5000, 4000, 1000, 100)
    assert (rec["think_closed_at"], rec["think_close"]) == (90, "answer_room")


def test_journal_entries_from_containers_and_native_units():
    import json as _json
    from kiln.collect.sources import journal_entry
    base = {"MESSAGE": "hello", "__REALTIME_TIMESTAMP": "1790000000123456", "__CURSOR": "c"}
    assert journal_entry(_json.dumps({**base, "CONTAINER_NAME": "kiln-x"}).encode())[:3] == \
        ("kiln-x", "hello", 1790000000.123456)
    assert journal_entry(_json.dumps({**base, "_SYSTEMD_USER_UNIT": "eval-gufo-ar.service"}).encode())[0] == \
        "eval-gufo-ar"
    assert journal_entry(_json.dumps({**base, "_SYSTEMD_USER_UNIT": "kiln-dash.service"}).encode()) is None
    assert journal_entry(_json.dumps({**base, "MESSAGE": list(b"caf\xe9"), "CONTAINER_NAME": "k"}).encode())[1] == \
        "caf\ufffd"
    assert journal_entry(b"not json") is None


def test_clients_get_distinct_colours_in_order_of_appearance(env):
    from kiln.store.queries import PALETTE
    ing, conn = env
    for i in range(len(PALETTE) + 2):
        ing._client(f"c{i}", None, None, float(i))
    colors = [r[0] for r in conn.execute("SELECT color FROM clients ORDER BY updated")]
    assert colors[:len(PALETTE)] == list(PALETTE) and len(set(colors[:len(PALETTE)])) == len(PALETTE)
    ing._client("c0", "renamed", None, 99.0)  # an update keeps the colour
    assert conn.execute("SELECT color FROM clients WHERE key='c0'").fetchone()[0] == PALETTE[0]


def test_scale_counts_sums_exactly():
    from kiln.collect.ingest import scale_counts
    out = scale_counts([(1, 49), (2, 49), (3, 2)], 102)
    assert sum(n for _, n in out) == 102 and [s for s, _ in out] == [1, 2, 3]
    assert scale_counts([], 5) == [] and scale_counts([(1, 3)], 0) == []


def test_gateway_histogram_is_scaled_to_usage(env):
    ing, conn = env
    ing.gateway_record({**gw_record(completion=102, events=98), "tokens_by_s": [101, [49, 49]]})
    assert series(conn, "decode") == {101: 51, 102: 51}


def test_engine_line_with_a_different_prompt_size_does_not_join(env):
    ing, conn = env
    ing.gateway_record(gw_record())  # prompt 5000
    other = H_LINE.replace("prompt 5000 (4000 cached, 80.0%)", "prompt 7000 (6000 cached, 85.7%)")
    ing.engine_line(HALOGEN, other, 103.5)
    srcs = sorted(r["source"] for r in rows(conn))
    assert srcs == ["engine_pending", "gateway"]
    ing.settle(10_000)
    assert conn.execute("SELECT COUNT(*) FROM engine_events WHERE kind='join_miss'").fetchone()[0] == 1


def test_join_reprices_with_engine_cached_tokens_and_the_same_card(env):
    ing, conn = env
    ing.gateway_record({**gw_record(), "usage": {"prompt": 5000, "cached": None, "completion": 100}})
    before = conn.execute("SELECT usd, card_id FROM request_costs WHERE source='reference'").fetchone()
    ing.book.add("reference", "*", "anthropic/claude-sonnet-5.5", 4.0, 0.4, 20.0, now=500)  # price doubles later
    ing.engine_line(HALOGEN, H_LINE, 103.5)  # says 4000 of the 5000 were cached
    after = conn.execute("SELECT usd, card_id FROM request_costs WHERE source='reference'").fetchone()
    assert after["card_id"] == before["card_id"]
    assert before["usd"] == pytest.approx((5000 * 2 + 100 * 10) / 1e6)
    assert after["usd"] == pytest.approx((1000 * 2 + 4000 * 0.2 + 100 * 10) / 1e6)


def test_non_streamed_output_enters_the_history(env):
    ing, conn = env
    rec = {**gw_record(), "ts_first_token": None, "ts_last_token": None, "tokens_by_s": None}
    ing.gateway_record(rec)
    ing.engine_line(HALOGEN, H_LINE, 103.1)  # decode 2.0 s before the end, prefill 0.5 s before that
    assert sum(series(conn, "decode").values()) == 100 and sum(series(conn, "prefill").values()) == 1000
    # Decoding began 2.0 s before the end (101.1), prefill the 0.5 s before that.
    assert min(series(conn, "decode")) == 101 and series(conn, "prefill") == {100: 800, 101: 200}


def test_utility_calls_are_not_counted(env):
    from kiln.store import queries
    ing, conn = env
    ing.gateway_record({**gw_record("u1"), "path": "messages/count_tokens", "usage": {}, "token_events": 0,
                        "tokens_by_s": None, "ts_first_token": None, "ts_last_token": None})
    ing.gateway_record(gw_record("g1"))
    assert {r["id"]: r["source"] for r in rows(conn)} == {"u1": "utility", "g1": "gateway"}
    assert [r["id"] for r in queries.recent_requests(conn, 10)] == ["g1"]


def test_old_labelled_records_are_keyed_by_device(env):
    ing, conn = env
    rec = gw_record()
    ing.gateway_record({**rec, "client": {**rec["client"], "key": "verify-x", "label": "verify-x"}})
    (r,) = rows(conn)
    assert (r["client_key"], r["client_label"]) == ("198.51.100.10", "verify-x")
    assert [k for (k,) in conn.execute("SELECT key FROM clients")] == ["198.51.100.10"]


def test_migration_rekeys_labelled_rows_to_the_device():
    from kiln.store.db import connect, migrate
    conn = connect(":memory:")
    conn.execute("DELETE FROM schema_migrations WHERE version = 3")
    conn.execute("INSERT INTO requests (id, source, host, ts_start, client_key, client_label, peer_ip) VALUES"
                 " ('a', 'gateway', 'strix', 1, 'mac', NULL, 'mac'), ('b', 'gateway', 'strix', 2, 'verify', 'verify', 'mac'),"
                 " ('c', 'gateway', 'strix', 3, 'eval-x', 'eval-x', '127.0.0.1'), ('d', 'direct', 'strix', 4, 'direct:h', NULL, NULL)")
    conn.execute("INSERT INTO series_1s (t, host, profile, model, client_key, decode, prefill) VALUES"
                 " (10, 'strix', 'p', 'm', 'mac', 5, 0), (10, 'strix', 'p', 'm', 'verify', 3, 7), (11, 'strix', 'p', 'm', 'verify', 2, 0),"
                 " (12, 'strix', 'p', 'm', 'eval-x', 1, 0), (12, 'strix', 'p', 'm', 'direct:h', 4, 0)")
    conn.execute("INSERT INTO clients (key, name, node, color, updated) VALUES ('mac', 'Mac', 'mac-node', '#111', 1),"
                 " ('verify', 'Mac', 'mac-node', '#222', 2), ('eval-x', NULL, NULL, '#333', 3), ('direct:h', 'Direct', NULL, '#444', 4)")
    migrate(conn)
    assert dict(conn.execute("SELECT id, client_key FROM requests").fetchall()) == \
        {"a": "mac", "b": "mac", "c": "127.0.0.1", "d": "direct:h"}
    assert dict(conn.execute("SELECT id, client_label FROM requests WHERE client_label IS NOT NULL").fetchall()) == \
        {"b": "verify", "c": "eval-x"}
    assert {(r[0], r[1]): (r[2], r[3]) for r in conn.execute("SELECT t, client_key, decode, prefill FROM series_1s")} == \
        {(10, "mac"): (8, 7), (11, "mac"): (2, 0), (12, "127.0.0.1"): (1, 0), (12, "direct:h"): (4, 0)}
    clients = {r[0]: (r[1], r[2]) for r in conn.execute("SELECT key, name, color FROM clients")}
    assert clients == {"mac": ("Mac", "#111"), "127.0.0.1": ("strix", None), "direct:h": ("Direct", "#444")}


def test_the_engine_managers_health_check_is_labelled_and_is_no_join_miss(env):
    ing, conn = env
    ing.is_probe = lambda container, ts: True
    ing.engine_line(HALOGEN, H_LINE, 200.0)
    ing.settle(200.0 + SETTLE_S + 1)
    (r,) = rows(conn)
    assert (r["source"], r["client_key"], r["client_label"]) == ("direct", "127.0.0.1", "kiln-probe")
    assert conn.execute("SELECT COUNT(*) FROM engine_events WHERE kind = 'join_miss'").fetchone()[0] == 0


def test_migration_moves_direct_rows_to_the_kiln_host_device():
    from kiln.store.db import connect, migrate
    conn = connect(":memory:")
    conn.execute("DELETE FROM schema_migrations WHERE version = 8")
    conn.execute("INSERT INTO requests (id, source, host, ts_start, client_key, client_name, client_label) VALUES"
                 " ('a', 'gateway', 'strix', 1, '127.0.0.1', 'strix', NULL), ('b', 'direct', 'strix', 2, 'direct:halogen', 'Direct to halogen', NULL),"
                 " ('c', 'direct', 'strix', 3, 'eval:gufo-x', 'Eval gufo-x', NULL), ('d', 'direct', 'strix', 4, 'kiln:probe', 'Kiln health check', NULL),"
                 " ('e', 'agent', 'remote', 5, 'remote/lan/pi', 'pi (remote LAN)', NULL)")
    conn.execute("INSERT INTO series_1s (t, host, profile, model, client_key, decode, prefill) VALUES"
                 " (10, 'strix', 'p', 'm', '127.0.0.1', 5, 0), (10, 'strix', 'p', 'm', 'direct:halogen', 3, 7),"
                 " (11, 'strix', 'p', 'm', 'eval:gufo-x', 2, 0), (12, 'strix', 'p', 'm', 'kiln:probe', 1, 0),"
                 " (13, 'remote', 'q', 'n', 'remote/lan/pi', 4, 0)")
    conn.execute("INSERT INTO clients (key, name, color, updated) VALUES ('direct:halogen', 'Direct to halogen', '#111', 6),"
                 " ('eval:gufo-x', 'Eval gufo-x', '#222', 3), ('kiln:probe', 'Kiln health check', '#333', 4),"
                 " ('remote/lan/pi', 'pi (remote LAN)', '#444', 5)")
    migrate(conn)
    got = {r["id"]: (r["client_key"], r["client_name"], r["client_label"]) for r in conn.execute("SELECT * FROM requests")}
    assert got == {"a": ("127.0.0.1", "strix", None), "b": ("127.0.0.1", "strix", "halogen"),
                   "c": ("127.0.0.1", "strix", "eval-gufo-x"), "d": ("127.0.0.1", "strix", "kiln-probe"),
                   "e": ("remote/lan/pi", "pi (remote LAN)", None)}  # the host's first push moves these
    assert {(r[0], r[1]): (r[2], r[3]) for r in conn.execute("SELECT t, client_key, decode, prefill FROM series_1s")} == \
        {(10, "127.0.0.1"): (8, 7), (11, "127.0.0.1"): (2, 0), (12, "127.0.0.1"): (1, 0), (13, "remote/lan/pi"): (4, 0)}
    clients = {r[0]: (r[1], r[2]) for r in conn.execute("SELECT key, name, updated FROM clients")}
    assert clients == {"127.0.0.1": ("strix", 6), "remote/lan/pi": ("pi (remote LAN)", 5)}


def test_migration_reclassifies_old_utility_rows():
    from kiln.store.db import connect, migrate
    conn = connect(":memory:")
    conn.execute("DELETE FROM schema_migrations WHERE version = 2")
    conn.execute("INSERT INTO requests (id, source, host, ts_start, client_key, path) VALUES"
                 " ('a', 'gateway', 'h', 1, 'c', 'messages/count_tokens'), ('b', 'gateway', 'h', 2, 'c', 'chat/completions')")
    migrate(conn)
    assert dict(conn.execute("SELECT id, source FROM requests").fetchall()) == {"a": "utility", "b": "gateway"}


def test_tool_call_fallback_is_stored_as_a_flag(env):
    ing, conn = env
    crucible = {"profile": "crucible-flashnext", "engine": "crucible"}
    ing.gateway_record({**gw_record("f1"), **crucible})
    ing.gateway_record({**gw_record("f2", t0=200.0), **crucible,
                        "usage": {**gw_record()["usage"], "tool_call_fallback": True}})
    ing.gateway_record({**gw_record("f3", t0=300.0), **crucible,
                        "usage": {**gw_record()["usage"], "tool_call_fallback": "x"}})
    got = {r["id"]: r["tool_call_fallback"] for r in rows(conn)}
    assert got == {"f1": 0, "f2": 1, "f3": 0}


def test_tool_call_fallback_is_unknown_for_engines_that_do_not_report_it(env):
    ing, conn = env  # Halogen and Gufo have no such flag: NULL (not known), not 0
    ing.gateway_record(gw_record("h1"))
    ing.gateway_record({**gw_record("g1", t0=200.0), "profile": "gufo-x", "engine": "gufo"})
    ing.gateway_record({**gw_record("n1", t0=300.0), "engine": None})
    assert {r["id"]: r["tool_call_fallback"] for r in rows(conn)} == {"h1": None, "g1": None, "n1": None}


def test_a_request_row_carries_its_decode_window_for_the_charts_column(env):
    # Gateway only: first to last token. With the engine's line: its decode time, ending at the last token.
    from kiln.store import queries
    ing, conn = env
    ing.gateway_record(gw_record())
    r = queries.get_request(conn, "g1")
    assert (r["decode_from"], r["decode_to"]) == (101.0, 103.0)
    ing.engine_line(HALOGEN, H_LINE, 103.5)  # 100 tokens in 2.00 s
    r = queries.get_request(conn, "g1")
    assert (r["decode_from"], r["decode_to"]) == (101.0, 103.0)
    ing.gateway_record(gw_record("g2", t0=200.0, completion=0))
    r = queries.get_request(conn, "g2")
    assert (r["decode_from"], r["decode_to"]) == (None, None)  # no tokens: no column


def test_settle_reads_only_unsettled_rows_not_the_whole_table(env):
    # Every second, on the event loop: the gateway query walked all 14,000 settled-for-good utility rows
    # (series_done = 0) and the engine_pending query scanned the table, about 14,000 pages a second. When
    # the fleet's GPU jobs pushed those pages out of memory, the tick waited seconds on disk reads.
    ing, _ = env
    sqls = []
    ing.conn.set_trace_callback(sqls.append)
    ing.settle()
    ing.conn.set_trace_callback(None)
    selects = [s for s in sqls if s.lstrip().upper().startswith("SELECT")]
    assert len(selects) == 2
    for sql in selects:
        plan = " | ".join(r[3] for r in ing.conn.execute("EXPLAIN QUERY PLAN " + sql))
        assert "SCAN" not in plan and "requests_pending" not in plan, plan
