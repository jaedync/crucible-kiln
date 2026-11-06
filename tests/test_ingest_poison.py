"""A record with odd types must never stop ingest (security review H2, ingest part)."""
import json

from kiln.collect.sources import GatewayFiles
from tests.test_dash import NOW, dash_client  # noqa: F401 (fixture)
from tests.test_ingest import env, gw_record, rows  # noqa: F401 (fixture)


def poison(rid="bad", **over):
    rec = gw_record(rid, t0=NOW - 20)
    rec.update({"model": {"x": 1}, "finish": ["stop"], "profile": {"p": 1}, "engine": 3, "path": ["chat"],
                "error": {"e": 1}, "client": {"key": ["k"], "name": {"n": 1}, "label": 5, "node": [],
                                              "peer_ip": {"ip": 1}}}, **over)
    return rec


def test_text_columns_take_only_text(env):
    ing, conn = env
    ing.gateway_record(poison())
    (r,) = rows(conn)
    for col in ("model", "finish", "profile", "engine", "path", "error", "client_name", "client_label",
                "client_node", "peer_ip"):
        assert r[col] is None, col
    assert r["client_key"] == "unknown"


def test_a_record_with_an_id_that_is_not_text_is_skipped(env):
    ing, conn = env
    assert ing.gateway_record({**gw_record(), "id": {"x": 1}}) == []
    assert rows(conn) == []


async def test_a_bad_record_is_skipped_and_the_good_ones_and_the_offset_are_kept(dash_client, capsys):
    d, _ = await dash_client()
    real = d.ingest.gateway_record

    def flaky(rec):
        if rec["id"] == "boom":
            real(rec)  # half written, then it fails: the savepoint must undo the half
            raise RuntimeError("unexpected")
        return real(rec)
    d.ingest.gateway_record = flaky
    await d._on_records([gw_record("g1", t0=NOW - 100), gw_record("boom", t0=NOW - 90),
                         gw_record("g2", t0=NOW - 80)], {"requests-x.jsonl": 300})
    ids = [r["id"] for r in d.conn.execute("SELECT id FROM requests ORDER BY ts_start")]
    assert ids == ["g1", "g2"]
    pos = d.conn.execute("SELECT position FROM ingest_offsets WHERE source = 'gateway:requests-x.jsonl'").fetchone()
    assert pos[0] == "300"
    assert "skipped" in capsys.readouterr().out


async def test_a_poison_line_in_an_older_file_does_not_block_a_newer_one(dash_client, tmp_path):
    d, _ = await dash_client()
    data = tmp_path / "gwdata"
    data.mkdir()
    (data / "requests-2000-01-01.jsonl").write_text(json.dumps(poison()) + "\n")
    (data / "requests-2000-01-02.jsonl").write_text(json.dumps(gw_record("good", t0=NOW - 10)) + "\n")
    files = GatewayFiles(data, {}, d._on_records)
    assert await files.scan() == 2
    ids = {r["id"] for r in d.conn.execute("SELECT id FROM requests")}
    assert ids == {"bad", "good"}
    assert set(files.offsets) == {"requests-2000-01-01.jsonl", "requests-2000-01-02.jsonl"}


async def test_scan_goes_on_after_a_file_whose_batch_fails(tmp_path, capsys):
    (tmp_path / "requests-a.jsonl").write_text('{"id": "a"}\n')
    (tmp_path / "requests-b.jsonl").write_text('{"id": "b"}\n')
    got = []

    async def on_records(records, offsets):
        if "requests-a.jsonl" in offsets:
            raise RuntimeError("database is locked")
        got.append(records)
    files = GatewayFiles(tmp_path, {}, on_records)
    await files.scan()
    assert got == [[{"id": "b"}]] and "requests-a.jsonl" not in files.offsets
    assert "requests-a.jsonl" in capsys.readouterr().out


async def test_a_bad_feed_record_does_not_end_the_feed(dash_client, capsys):
    d, _ = await dash_client()

    def boom(rec):
        raise RuntimeError("unexpected")
    d.ingest.gateway_record = boom
    await d._on_feed({"type": "req.end", "id": "f1", "record": gw_record("f1", t0=NOW - 5)})
    assert "skipped" in capsys.readouterr().out


def test_numbers_out_of_the_sqlite_range_are_null_and_the_row_is_kept(env):
    """max_tokens 10**20 gave OverflowError at the bind and the row was lost (release review M3)."""
    ing, conn = env
    rec = gw_record("big")
    rec.update(max_tokens=10 ** 20, token_events=-(2 ** 64), latency_s=10 ** 30,
               usage={"prompt": 2 ** 63, "completion": 5}, prefix={"n_messages": 10 ** 19})
    ing.gateway_record(rec)
    (r,) = rows(conn)
    assert (r["max_tokens"], r["token_events"], r["latency_s"], r["prompt"], r["n_messages"]) == \
        (None, None, None, None, None)
    assert r["completion"] == 5


def test_a_long_reasoning_effort_is_cut_and_a_huge_raw_record_keeps_only_its_numbers(env):
    ing, conn = env
    rec = {**gw_record("long"), "reasoning_effort": "x" * 5000, "junk": "y" * (1 << 20)}
    ing.gateway_record(rec)
    (r,) = rows(conn)
    assert len(r["reasoning_effort"]) == 200
    assert json.loads(r["raw"]) == {"tokens_by_s": rec["tokens_by_s"]}


async def test_the_jsonl_tail_is_read_in_bounded_chunks(tmp_path, monkeypatch):
    """A large backlog is read and stored chunk by chunk, so kiln-dash stays under its MemoryMax."""
    from kiln.collect import sources
    monkeypatch.setattr(sources, "READ_CHUNK_BYTES", 300)
    lines = [json.dumps({"id": f"r{i}", "pad": "p" * 50}) for i in range(20)]
    huge = json.dumps({"id": "huge", "pad": "h" * 1000})  # one line longer than a chunk: skipped
    path = tmp_path / "requests-a.jsonl"
    path.write_text("\n".join(lines[:10] + [huge] + lines[10:]) + "\n" + '{"id": "partial"')
    batches = []

    async def on_records(records, offsets):
        assert sum(len(json.dumps(r)) for r in records) <= 300
        batches.append([r["id"] for r in records])
    files = GatewayFiles(tmp_path, {}, on_records)
    assert await files.scan() == 20
    assert [i for b in batches for i in b] == [f"r{i}" for i in range(20)] and len(batches) > 3
    assert files.offsets["requests-a.jsonl"] == path.stat().st_size - len('{"id": "partial"')


def test_a_long_stream_over_the_raw_cap_keeps_its_per_second_token_spread(env):
    """spread.py reads tokens_by_s from the raw copy (delta review L1). A long stream must keep it."""
    from kiln.collect import spread
    ing, conn = env
    rec = gw_record("long-stream", completion=60_000)
    per_s = [2] * 30_000
    rec.update(tokens_by_s=[int(rec["ts_start"]) + 1, per_s], held_s=1.5, junk="y" * 1000)
    assert len(json.dumps(rec)) > 64 * 1024
    ing.gateway_record(rec)
    (r,) = rows(conn)
    kept = json.loads(r["raw"])
    assert kept == {"tokens_by_s": rec["tokens_by_s"], "held_s": 1.5}  # no client data, only the numbers
    assert spread.streamed(r) and sum(n for _, n in spread.arrival_pairs(r)) == 60_000


def test_an_odd_tokens_by_s_on_a_huge_record_is_not_kept(env):
    ing, conn = env
    ing.gateway_record({**gw_record("odd"), "tokens_by_s": [1, ["x" * (1 << 17)]]})
    (r,) = rows(conn)
    assert r["raw"] is None
