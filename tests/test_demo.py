"""kiln demo: the fakes at the edges must speak exactly what the real readers parse (collect/*)."""
import asyncio
import json
import socket

import aiohttp
import pytest

from kiln.collect import engines, memory
from kiln.collect.hw import Sensors
from kiln.collect.memory import MemorySampler
from kiln.demo import edges, scenario
from kiln.demo.engine import PACKS, FakeEngine
from kiln.engines.manager import Manager
from kiln.engines.state import READY, StateFile


def free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def test_start_lines_give_the_memory_map_of_a_4x256k_engine():
    facts = {}
    for line in FakeEngine(PACKS["4.05"], port=1, emit=lambda *a: None).start_lines():
        facts.update(memory.engine_facts(line) or {})
    assert facts["weights_bytes"] == 69_428_838_400
    assert facts["slot_state_bytes"] == 6_996_972_544
    assert facts["cache_budget_bytes"] == 10_171_187_200
    assert facts["ple_bytes"] == 102_400_491_520
    for key in ("stage_bytes", "checkpoint_bytes", "cut_bytes", "vision_bytes", "draft_bytes"):
        assert facts[key] > 0, key


async def _chat(port: int, body: dict) -> tuple[list[dict], float, float]:
    """The SSE events of one streamed request, the time to the first token and the total time."""
    loop = asyncio.get_running_loop()
    t0, first, events = loop.time(), None, []
    async with aiohttp.ClientSession() as s:
        async with s.post(f"http://127.0.0.1:{port}/v1/chat/completions", json=body) as r:
            assert r.status == 200
            async for raw in r.content:
                line = raw.decode().strip()
                if not line.startswith("data: ") or line == "data: [DONE]":
                    continue
                ev = json.loads(line[6:])
                delta = (ev.get("choices") or [{}])[0].get("delta") or {}
                if first is None and (delta.get("content") or delta.get("reasoning_content") or delta.get("tool_calls")):
                    first = loop.time() - t0
                events.append(ev)
    return events, first, loop.time() - t0


@pytest.fixture
async def engine():
    lines = []
    eng = FakeEngine(PACKS["4.05"], port=free_port(), emit=lambda msg, ts: lines.append((msg, ts)), seed=3)
    await eng.start()
    eng.lines = lines
    yield eng
    await eng.stop()


async def test_a_streamed_turn_logs_lines_that_parse_as_crucible(engine):
    prompt = scenario.filler(9000, seed=1)  # about 9,000 tokens: three prefill reports
    body = {"model": "crucible", "stream": True, "stream_options": {"include_usage": True},
            "messages": [{"role": "user", "content": prompt + " [demo reply=120]"}]}
    events, ttft, _ = await _chat(engine.port, body)
    usage = events[-1]["usage"]
    assert usage["completion_tokens"] == 120
    assert 8900 < usage["prompt_tokens"] < 9200 and usage["prompt_tokens_details"]["cached_tokens"] == 0
    assert 3 < ttft < 7  # about 1,800 tok/s
    parsed = [engines.parse(m) for m, _ in engine.lines]
    kinds = [p[1] for p in parsed if p]
    assert kinds.count("progress") >= 3 and "decode" in kinds and kinds[-1] == "completion"
    progress = [p[2] for p in parsed if p and p[1] == "progress"]
    assert progress[-1]["tokens"] == progress[-1]["total"] == usage["prompt_tokens"]
    turn = parsed[-1][2]
    assert turn["completion"] == 120 and turn["prompt"] == usage["prompt_tokens"]
    assert 1700 < turn["prefill_tokens"] / turn["prefill_s"] < 1900
    assert 50 < turn["completion"] / turn["decode_s"] < 80
    assert turn["draft_accepted"] > 0 and len(turn["draft_pos_accepted"]) == 7  # every promise held


async def test_the_same_prompt_sent_again_restores_the_cache(engine):
    body = {"model": "crucible", "stream": True,
            "messages": [{"role": "user", "content": scenario.filler(5000, seed=2) + " [demo reply=40]"}]}
    await _chat(engine.port, body)
    events, ttft, _ = await _chat(engine.port, body)
    usage = events[-1]["usage"]
    assert usage["prompt_tokens_details"]["cached_tokens"] >= usage["prompt_tokens"] - 8
    assert ttft < 0.2
    turn = engines.parse(engine.lines[-1][0])[2]
    assert turn["session_label"] == "hit_session" and turn["cache_bytes"] > 0


async def test_a_tool_call_turn_streams_the_name_first_and_ends_with_tool_calls(engine):
    body = {"model": "crucible", "stream": True, "tools": [{"type": "function", "function": {"name": "read_file"}}],
            "messages": [{"role": "user", "content": scenario.filler(300, seed=4) + " [demo tool=read_file reply=60]"}]}
    events, _, _ = await _chat(engine.port, body)
    calls = [c for ev in events for ch in ev.get("choices") or [] for c in (ch.get("delta") or {}).get("tool_calls") or []]
    assert calls[0]["function"]["name"] == "read_file"
    assert json.loads("".join(c["function"].get("arguments", "") for c in calls))
    finish = [ch["finish_reason"] for ev in events for ch in ev.get("choices") or [] if ch.get("finish_reason")]
    assert finish == ["tool_calls"] and events[-1]["usage"]["completion_tokens"] == 60


async def test_models_health_and_a_plain_reply(engine):
    async with aiohttp.ClientSession() as s:
        async with s.get(f"http://127.0.0.1:{engine.port}/v1/models") as r:
            assert (await r.json())["data"][0]["id"] == "crucible"
        async with s.get(f"http://127.0.0.1:{engine.port}/health") as r:
            assert r.status == 200
        body = {"model": "crucible", "messages": [{"role": "user", "content": "Reply with the word OK."}],
                "max_tokens": 8}
        async with s.post(f"http://127.0.0.1:{engine.port}/v1/chat/completions", json=body) as r:
            out = await r.json()
    assert 0 < out["usage"]["completion_tokens"] <= 8


def test_fake_sysfs_reads_through_the_real_sensors(tmp_path):
    hw = edges.FakeHardware(tmp_path, seed=1)
    hw.update(edges.Load(prefilling=1, decoding=0, gtt_bytes=100 * 2**30, ple_ram=3 * 2**30), dt=30)
    s = Sensors(root=hw.sys, proc=hw.proc).sample()
    for key in ("tctl_c", "edge_c", "power_w", "fan_rpm", "gpu_busy_pct", "gtt_used_bytes", "gtt_total_bytes",
                "sclk_mhz", "ram_avail_bytes", "vram_total_bytes", "profile"):
        assert key in s, key
    assert s["gtt_used_bytes"] == 100 * 2**30 and s["sclk_mhz"] >= 2000 and s["power_w"] > 100
    facts = {"weights_bytes": 69_428_838_400, "slot_state_bytes": 6_996_972_544, "ple_bytes": 102_400_491_520}
    m = MemorySampler(proc=hw.proc, probe=edges.FakeProbe(hw)).sample("kiln-x.service", facts, s["gtt_used_bytes"], 4)
    assert m["ram"]["weights"] == 69_428_838_400 and m["ram"]["kv_state"] == 4 * 6_996_972_544
    assert sum(v for k, v in m["ram"].items() if k != "total") == m["ram"]["total"]
    assert m["disk"]["ple_file"] == 102_400_491_520 and m["ram"]["ple_ram"] == 3 * 2**30


async def test_the_fake_unit_runner_lets_the_real_manager_switch_packs(make_cfg, tmp_path):
    lines = []
    cfg = make_cfg(**{"profiles": scenario.profiles(free_port(), free_port()),
                      "engines.quadlet_dir": str(tmp_path / "q"), "engines.user_unit_dir": str(tmp_path / "u")})
    units = edges.FakeUnits(cfg, emit=lambda unit, msg, ts: lines.append((unit, msg)), speed=50)
    state = StateFile(cfg.state_file)
    events = []

    async def on_event(kind, profile, detail):
        events.append(kind)
    first, second = list(cfg.profiles)
    await units.boot(first, state)
    m = Manager(cfg, state, on_event=on_event, gtt_used=units.gtt_used, runner=units.run,
                sleep=lambda s: asyncio.sleep(min(s, 0.05)))
    assert await m.blocked() is None
    assert await m.switch(second) is True
    assert state.read().state == READY and state.read().profile == second
    assert units.running == {second} and "stopping" in events and "ready" in events
    assert any(unit == cfg.profiles[second].container_name and msg.startswith("weight payload") for unit, msg in lines)
    await units.close()


def test_history_is_two_weeks_that_ingest_joins_and_the_seed_repeats(tmp_path):
    from kiln import config
    from kiln.collect.ingest import Ingestor
    from kiln.demo import history
    from kiln.pricing.cards import PriceBook
    from kiln.store.db import connect
    now = 1_793_900_000.0
    records, lines = history.generate(now, seed=5)
    assert (records, lines) == history.generate(now, seed=5)
    assert records != history.generate(now, seed=6)[0]
    assert now - 15 * 86400 < records[0]["ts_start"] and records[-1]["ts_end"] < now - history.END_GAP_S
    cfg = config.parse(scenario.config_dict(tmp_path, 1, 2, 3, (4, 5), 1.0))
    conn = connect(":memory:")
    book = PriceBook(conn)
    book.add("reference", "*", "x", 2.0, 0.1, 10.0, now=0)
    ing = Ingestor(conn, cfg, book, clock=lambda: now)
    for rec in records:
        ing.gateway_record(rec)
    for container, line, ts in lines:
        ing.engine_line(container, line, ts)
    joined = conn.execute("SELECT COUNT(*) FROM requests WHERE source = 'gateway' AND engine_joined_at IS NOT NULL"
                          " AND draft_pos_accepted IS NOT NULL").fetchone()[0]
    assert joined == len(records) > 100
    assert conn.execute("SELECT COUNT(*) FROM engine_events WHERE kind = 'parse_error'").fetchone()[0] == 0
    assert {r["client_name"] for r in conn.execute("SELECT DISTINCT client_name FROM requests")} == set(scenario.DEVICES)


async def test_turn_counts_repeat_with_the_seed():
    async def once(seed):
        lines = []
        eng = FakeEngine(PACKS["4.05"], port=free_port(), emit=lambda msg, ts: lines.append(msg), seed=seed)
        await eng.start()
        try:
            await _chat(eng.port, {"model": "crucible", "stream": True,
                                   "messages": [{"role": "user", "content": scenario.filler(600, seed=1) + " [demo reply=40]"}]})
        finally:
            await eng.stop()
        turn = json.loads(lines[-1].split(" ", 1)[1])
        return {k: v for k, v in turn.items() if not k.endswith("_s")}
    assert await once(9) == await once(9)
    assert await once(9) != await once(10)


async def test_a_stop_ends_open_streams_at_once(make_cfg, tmp_path):
    cfg = make_cfg(**{"profiles": scenario.profiles(free_port(), free_port())})
    units = edges.FakeUnits(cfg, emit=lambda *a: None, speed=50)
    name = scenario.DEFAULT_PROFILE
    await units.boot(name, StateFile(cfg.state_file))
    body = {"model": "crucible", "stream": True, "messages": [{"role": "user", "content": "x [demo reply=5000]"}]}
    stream = asyncio.create_task(_chat(cfg.profiles[name].port, body))
    await asyncio.sleep(0.5)
    t0 = asyncio.get_running_loop().time()
    assert await units.run("systemctl", "--user", "stop", cfg.profiles[name].unit) == (0, "")
    assert asyncio.get_running_loop().time() - t0 < 1.5
    with pytest.raises(aiohttp.ClientError):
        await asyncio.wait_for(stream, 2)
    assert units.running == set()
    await units.close()


def test_the_demo_imports_no_fleet_or_host_module():
    # The public tree drops these (gpu_queue, hosts, agent, engines.fleet); the demo must run without them.
    import ast
    from pathlib import Path
    import kiln.demo
    banned = ("kiln.collect.gpu_queue", "kiln.hosts", "kiln.agent", "kiln.engines.fleet")
    for path in Path(kiln.demo.__file__).parent.glob("*.py"):
        for node in ast.walk(ast.parse(path.read_text())):
            names = [a.name for a in node.names] if isinstance(node, ast.Import) else \
                [node.module or ""] if isinstance(node, ast.ImportFrom) else []
            assert not any(n.startswith(banned) for n in names), (path.name, names)


def test_the_banner_of_a_single_run_does_not_say_it_repeats(tmp_path):
    from kiln.demo.run import Demo
    for once, want, never in ((True, "plays once", "Ctrl-C"), (False, "Press Ctrl-C to stop", "plays once")):
        lines = []
        demo = Demo(free_port(), 1.0, 0, lines.append, False)
        try:
            demo.banner(once)
        finally:
            import shutil
            shutil.rmtree(demo.data, ignore_errors=True)
        text = "\n".join(lines)
        assert want in text and never not in text


def test_a_taken_port_gives_a_clean_message(capsys):
    from kiln.demo import run
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        s.listen()
        port = s.getsockname()[1]
        assert run.main(port, 50.0, 0, True, history=False) == 1
    err = capsys.readouterr().err
    assert f"kiln demo: port {port} is in use" in err and "--port" in err and "Errno" not in err
