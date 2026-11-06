"""End to end: `kiln demo --once` runs the real gateway and dashboard on fake edges, and the dashboard shows
the scenario. CI runs it on its own with `pytest -m demo`; it takes about 30 s at --speed 8."""
import asyncio
import json
import os
import re
import socket
import sys
import time
from pathlib import Path

import aiohttp
import pytest

pytestmark = pytest.mark.demo
SPEED = 8
TIMEOUT_S = 90


def free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


async def test_the_demo_shows_prefill_decode_a_cache_hit_and_the_memory_map():
    port = free_port()
    t_start = time.time()
    proc = await asyncio.create_subprocess_exec(
        sys.executable, "-m", "kiln.cli", "demo", "--port", str(port), "--speed", str(SPEED), "--seed", "7", "--once",
        stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.STDOUT,
        env={**os.environ, "PYTHONUNBUFFERED": "1"})
    out: list[str] = []
    seen = {"history": 0, "prefill": None, "decode": None, "cache_hit": None, "mem": None, "clients": set(), "tool": None}
    try:
        url = None
        deadline = time.monotonic() + 30
        while url is None and time.monotonic() < deadline:
            line = (await asyncio.wait_for(proc.stdout.readline(), 30)).decode()
            out.append(line)
            if m := re.search(r"(http://127\.0\.0\.1:\d+/)", line):
                url = m[1]
        assert url == f"http://127.0.0.1:{port}/", "".join(out)

        async def drain():
            async for raw in proc.stdout:
                out.append(raw.decode())
        drainer = asyncio.create_task(drain())

        async with aiohttp.ClientSession() as s:
            async with s.get(url + "api/v1/state") as r:
                state = await r.json()
            assert {p["name"] for p in state["profiles"]} == {"crucible-405", "crucible-305"}
            assert state["engine"]["state"] == "ready" and state["engine"]["profile"] == "crucible-405"
            await asyncio.sleep(2)  # the backlog of two weeks is ingested by now
            async with s.get(url + "api/v1/requests?limit=500") as r:
                seen["history"] = sum(1 for row in (await r.json())["rows"] if row["ts"] < t_start)
            try:
                async with s.ws_connect(url + "api/v1/live") as ws:
                    async for msg in ws:
                        ev = json.loads(msg.data)
                        if ev["type"] == "tick":
                            for st in ev["live"]["streams"]:
                                seen["clients"].add(st["client"]["name"])
                                p = st.get("prefill_progress") or {}
                                if st["phase"] == "prefill" and p.get("total"):
                                    seen["prefill"] = seen["prefill"] or p
                                if st["phase"] == "decode" and st["tps"] > 0:
                                    seen["decode"] = seen["decode"] or st
                            mem = (ev.get("hw") or {}).get("mem")
                            if mem and mem["ram"].get("weights"):
                                seen["mem"] = mem
                        elif ev["type"] == "request":
                            row = ev["request"]
                            if row["ts"] < t_start:
                                continue  # the history, not the scenario
                            if row["cached"] and row["prompt"] and row["cached"] >= row["prompt"] - 64 \
                                    and row["ttft_s"] < 0.5:
                                seen["cache_hit"] = row
                            if row["finish"] == "tool_calls":
                                seen["tool"] = row
            except aiohttp.ClientError:
                pass  # the demo ends the socket when it exits
        assert await asyncio.wait_for(proc.wait(), TIMEOUT_S) == 0, "".join(out)
        await drainer
    finally:
        if proc.returncode is None:
            proc.kill()
            await proc.wait()
    log = "".join(out)
    assert seen["history"] > 100, log
    assert seen["prefill"] and seen["prefill"]["total"] > 10_000, log
    assert seen["decode"] and 20 < seen["decode"]["tps"] < 200, log
    assert seen["cache_hit"] and seen["cache_hit"]["decode_tps"], log
    assert seen["tool"] is not None, log
    assert {"laptop", "desktop", "agent"} <= seen["clients"], log
    ram = seen["mem"]["ram"]
    assert ram["weights"] == 69_428_838_400 and ram["kv_state"] == 4 * 6_996_972_544, ram
    assert seen["mem"]["disk"]["ple_file"] == 102_400_491_520
    data = re.search(r"data folder (\S+)", log)
    assert data and not Path(data[1]).exists(), log  # removed at exit
