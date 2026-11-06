#!/usr/bin/env python3
"""Measure what the Kiln gateway adds to a streamed chat completion.

A mock engine streams SSE chunks at a fixed cadence. A client sends the same
requests to the mock directly and through a real gateway (a subprocess started
with `python -m kiln.cli gateway`, a temp config, a temp state file and temp
record directory). Direct and through requests alternate, so drift on the
machine hits both paths alike. All times come from the client clock.

    .venv/bin/python tools/bench_gateway.py            # full run, 3 repeats, writes docs/bench/
    .venv/bin/python tools/bench_gateway.py --fast     # N=10 smoke run

Phases per repeat:
  latency      one request at a time, per body size: added time to first byte,
               first token, between tokens, total time, gateway CPU per request
  concurrency  rounds of 4 parallel streams: aggregate token rate, through vs direct
  stall        one long stream through the gateway while large or small requests
               arrive: shows whether a large request blocks the event loop

The mock engine mode (`--mock-engine`) is internal: the bench starts it itself.
"""
from __future__ import annotations

import argparse
import asyncio
import json
import math
import os
import platform
import random
import socket
import subprocess
import sys
import tempfile
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SRC = ROOT / "src"
PROFILE = "halogen-flashnext"
MODEL = "qwen3.8-flash-next"
TOKEN_HEADER = "X-Bench-Tokens"  # the gateway forwards unknown headers; the mock reads the stream length here

CONFIG = """\
host = "bench"
[gateway]
listen = "127.0.0.1:{gw}"
events_listen = "127.0.0.1:{ev}"
data_dir = "{tmp}/gateway"
state_file = "{tmp}/state/active.json"
[dash]
listen = "127.0.0.1:{dash}"
db = "{tmp}/kiln.db"
[actions]
allow_nodes = []
[pricing.reference]
name = "Reference"
provider_model = "vendor/model"
input_per_m = 2.0
cached_per_m = 0.2
output_per_m = 10.0
[pricing.openrouter]
enabled = false
[profiles.{profile}]
label = "Bench"
engine = "halogen"
model = "{model}"
served_id = "halogen-{model}"
hf_id = "bench/model"
port = {engine}
context = 262144
slots = 4
memory_gib = 1
[profiles.{profile}.kv]
positions = 1024
bytes_per_position = 1024
[profiles.{profile}.container]
Image = "localhost/bench:test"
"""


# ---------------------------------------------------------------- mock engine

def _sse(obj) -> bytes:
    return b"data: " + json.dumps(obj, separators=(",", ":")).encode() + b"\n\n"


def _chunk(delta, finish=None) -> bytes:
    return _sse({"id": "chatcmpl-bench", "object": "chat.completion.chunk", "created": 0, "model": "bench",
                 "choices": [{"index": 0, "delta": delta, "finish_reason": finish}]})


async def run_mock_engine(port: int, first_ms: float, interval_ms: float, default_tokens: int) -> None:
    from aiohttp import web

    role, token = _chunk({"role": "assistant"}), _chunk({"content": " word"})
    first, interval = first_ms / 1000, interval_ms / 1000

    async def chat(request: web.Request) -> web.StreamResponse:
        await request.read()
        n = int(request.headers.get(TOKEN_HEADER, default_tokens))
        resp = web.StreamResponse(headers={"Content-Type": "text/event-stream", "Cache-Control": "no-cache"})
        await resp.prepare(request)
        loop = asyncio.get_running_loop()
        t0 = loop.time()
        await resp.write(role)
        for i in range(n):  # absolute schedule: a late wake-up does not push the later tokens
            delay = t0 + first + i * interval - loop.time()
            if delay > 0:
                await asyncio.sleep(delay)
            await resp.write(token)
        usage = _sse({"id": "chatcmpl-bench", "object": "chat.completion.chunk", "created": 0, "model": "bench",
                      "choices": [], "usage": {"prompt_tokens": 1000, "completion_tokens": n, "total_tokens": 1000 + n}})
        await resp.write(_chunk({}, "stop") + usage + b"data: [DONE]\n\n")
        await resp.write_eof()
        return resp

    app = web.Application(client_max_size=64 * 1024 ** 2)
    app.router.add_post("/v1/chat/completions", chat)
    runner = web.AppRunner(app, access_log=None)
    await runner.setup()
    await web.TCPSite(runner, "127.0.0.1", port).start()
    print("ready", flush=True)
    await asyncio.Event().wait()


# ---------------------------------------------------------------- processes

def free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def cpu_seconds(pid: int) -> float:
    """User plus system CPU time of a process and all its threads (Linux /proc)."""
    with open(f"/proc/{pid}/stat") as f:
        rest = f.read().rsplit(")", 1)[1].split()
    return (int(rest[11]) + int(rest[12])) / os.sysconf("SC_CLK_TCK")


class Stack:
    """The mock engine and a real gateway, each a subprocess, with temp config and data."""

    def __init__(self, args):
        self.args = args
        self.tmp = tempfile.TemporaryDirectory(prefix="kiln-bench-")
        self.procs: list[subprocess.Popen] = []
        self.engine_port, self.gw_port = free_port(), free_port()

    @property
    def data_dir(self) -> Path:
        return Path(self.tmp.name) / "gateway"

    def start(self) -> None:
        a, tmp = self.args, Path(self.tmp.name)
        env = {**os.environ, "PYTHONPATH": str(SRC), "HOME": str(tmp), "PYTHONUNBUFFERED": "1"}
        mock = subprocess.Popen(
            [sys.executable, str(Path(__file__).resolve()), "--mock-engine", "--port", str(self.engine_port),
             "--first-ms", str(a.first_ms), "--interval-ms", str(a.interval_ms), "--tokens", str(a.mock_tokens)],
            env=env, stdout=subprocess.PIPE, text=True)
        self.procs.append(mock)
        if mock.stdout.readline().strip() != "ready":
            raise RuntimeError("mock engine did not start")
        (tmp / "kiln.toml").write_text(CONFIG.format(
            gw=self.gw_port, ev=free_port(), dash=free_port(), tmp=tmp, profile=PROFILE, model=MODEL,
            engine=self.engine_port))
        from kiln.engines.state import READY, StateFile
        StateFile(tmp / "state" / "active.json").set(PROFILE, READY)
        gw = subprocess.Popen([sys.executable, "-m", "kiln.cli", "--config", str(tmp / "kiln.toml"), "gateway"],
                              env=env, stdout=open(tmp / "gateway.log", "w"), stderr=subprocess.STDOUT)
        self.procs.append(gw)
        self.gateway = gw
        deadline = time.time() + 20
        while time.time() < deadline:
            if gw.poll() is not None:
                raise RuntimeError("gateway exited: " + (tmp / "gateway.log").read_text()[-500:])
            try:
                socket.create_connection(("127.0.0.1", self.gw_port), timeout=0.2).close()
                return
            except OSError:
                time.sleep(0.1)
        raise RuntimeError("gateway did not listen")

    def stop(self) -> None:
        for p in reversed(self.procs):
            p.terminate()
        for p in self.procs:
            try:
                p.wait(5)
            except subprocess.TimeoutExpired:
                p.kill()
        self.tmp.cleanup()

    def records_written(self) -> int:
        return sum(len(f.read_text().splitlines()) for f in self.data_dir.glob("requests-*.jsonl"))


# ---------------------------------------------------------------- bodies

WORDS = ("alpha beta gamma delta kernel buffer socket stream thread queue token prefix digest request response "
         "config module parser vector matrix signal handle cursor window offset length record update commit branch "
         "merge rebase deploy service engine memory cache index table column schema query result error retry "
         "timeout latency budget metric sample window").split()


def text(rng: random.Random, n_bytes: int) -> str:
    out, size = [], 0
    while size < n_bytes:
        w = rng.choice(WORDS)
        out.append(w)
        size += len(w) + 1
    return " ".join(out)


def tool_defs(rng: random.Random, n: int = 12) -> list[dict]:
    return [{"type": "function", "function": {
        "name": f"tool_{i}", "description": text(rng, 400),
        "parameters": {"type": "object", "required": ["path"], "properties": {
            "path": {"type": "string", "description": text(rng, 120)},
            "mode": {"type": "string", "enum": ["read", "write", "append"], "description": text(rng, 120)}}}}}
        for i in range(n)]


class Bodies:
    """Request bodies of about one size. The conversation is fixed; each request ends with one new user
    message, like the next turn of an agent session, so the prefix tracker matches the earlier turns."""

    def __init__(self, target_bytes: int, seed: int):
        self.rng = random.Random(seed)
        rng = self.rng
        if target_bytes <= 4096:
            tools, msgs = None, [{"role": "system", "content": text(rng, 300)}]
        else:
            tools, msgs = tool_defs(rng), [{"role": "system", "content": text(rng, 3000)},
                                           {"role": "user", "content": text(rng, 600)}]
        head = {"model": MODEL, "stream": True, "max_tokens": 4096, **({"tools": tools} if tools else {})}
        self.prefix = json.dumps(head)[:-1] + ', "messages": ['
        self.parts = [json.dumps(m) for m in msgs]
        size, i, turn_bytes = len(self.prefix) + sum(len(p) for p in self.parts), 0, 0
        while target_bytes > 4096 and size < target_bytes - 4000:
            i += 1
            turn_bytes = 2000 + rng.randint(0, 3000)  # an agent turn: reply with a call, then a tool result
            call = {"role": "assistant", "content": text(rng, 400), "tool_calls": [{
                "id": f"call_{i}", "type": "function",
                "function": {"name": f"tool_{i % 12}", "arguments": json.dumps({"path": f"src/mod_{i}.py"})}}]}
            result = {"role": "tool", "tool_call_id": f"call_{i}", "content": text(rng, turn_bytes)}
            ask = {"role": "user", "content": text(rng, 300)}
            for m in (call, result, ask):
                self.parts.append(json.dumps(m))
                size += len(self.parts[-1])
        if target_bytes <= 4096:
            self.parts.append(json.dumps({"role": "user", "content": text(rng, max(100, target_bytes - size - 120))}))
        self.n_messages = len(self.parts)
        self.base = self.prefix + ", ".join(self.parts) + ", "

    def make(self, i: int) -> bytes:
        tail = json.dumps({"role": "user", "content": f"turn {i}: " + text(self.rng, 200)})
        return (self.base + tail + "]}").encode()


# ---------------------------------------------------------------- client

def pct(xs: list[float], q: float) -> float:
    s = sorted(xs)
    k = (len(s) - 1) * q / 100
    f = int(math.floor(k))
    c = min(f + 1, len(s) - 1)
    return s[f] + (s[c] - s[f]) * (k - f)


def summary(xs: list[float]) -> dict:
    return {"n": len(xs), "p50": pct(xs, 50), "p95": pct(xs, 95), "p99": pct(xs, 99), "max": max(xs),
            "mean": sum(xs) / len(xs)}


async def one_request(session, url: str, body: bytes, tokens: int) -> dict:
    """One streamed request. Times are seconds from the moment the request was handed to the client."""
    t_send = time.perf_counter()
    async with session.post(url, data=body, headers={"Content-Type": "application/json", "Accept-Encoding": "identity",
                                                     TOKEN_HEADER: str(tokens)}) as resp:
        t_headers = time.perf_counter()
        if resp.status != 200:
            raise RuntimeError(f"status {resp.status}: {(await resp.read())[:200]!r}")
        t_byte, tok, done, buf = None, [], False, b""
        async for chunk in resp.content.iter_any():
            now = time.perf_counter()
            if t_byte is None:
                t_byte = now
            buf += chunk
            *events, buf = buf.split(b"\n\n")
            for ev in events:
                if b'"content"' in ev:
                    tok.append(now)
                elif b"[DONE]" in ev:
                    done = True
    t_end = time.perf_counter()
    if not done or len(tok) != tokens:
        raise RuntimeError(f"stream cut: {len(tok)} of {tokens} tokens, done={done}")
    return {"headers": t_headers - t_send, "ttfb": (t_byte or t_end) - t_send, "ttft": tok[0] - t_send,
            "total": t_end - t_send, "gaps": [b - a for a, b in zip(tok, tok[1:])], "t0": t_send,
            "first_tok": tok[0], "last_tok": tok[-1], "tokens": len(tok)}


def ms(x: float) -> float:
    return round(x * 1000, 4)


def path_stats(results: list[dict]) -> dict:
    out = {k: summary([ms(r[k]) for r in results]) for k in ("headers", "ttfb", "ttft", "total")}
    out["gap"] = summary([ms(g) for r in results for g in r["gaps"]])
    return out


def added(direct: dict, through: dict) -> dict:
    return {m: {k: through[m][k] - direct[m][k] for k in ("p50", "p95", "p99", "max", "mean")} for m in direct}


async def latency_phase(session, stack: Stack, bodies: Bodies, n: int, warmup: int, tokens: int, rng) -> dict:
    urls = {"direct": f"http://127.0.0.1:{stack.engine_port}/v1/chat/completions",
            "through": f"http://127.0.0.1:{stack.gw_port}/v1/chat/completions"}
    res: dict[str, list[dict]] = {"direct": [], "through": []}
    cpu0 = wall0 = None
    for i in range(warmup + n):
        body = bodies.make(i)
        order = ["direct", "through"]
        rng.shuffle(order)
        if i == warmup:
            cpu0, wall0 = cpu_seconds(stack.gateway.pid), time.perf_counter()
        for path in order:
            r = await one_request(session, urls[path], body, tokens)
            if i >= warmup:
                res[path].append(r)
    cpu = cpu_seconds(stack.gateway.pid) - cpu0
    wall = time.perf_counter() - wall0
    d, t = path_stats(res["direct"]), path_stats(res["through"])
    return {"body_bytes": len(body), "messages": bodies.n_messages + 1, "n": n, "tokens": tokens,
            "direct": d, "through": t, "added": added(d, t),
            "gateway_cpu_ms_gross": ms(cpu / n), "wall_s": wall,
            "raw_ms": {p: {k: [ms(r[k]) for r in res[p]] for k in ("ttfb", "ttft", "total")} for p in res}}


async def concurrency_phase(session, stack: Stack, bodies: Bodies, n: int, streams: int, warmup_rounds: int,
                            tokens: int) -> dict:
    urls = {"direct": f"http://127.0.0.1:{stack.engine_port}/v1/chat/completions",
            "through": f"http://127.0.0.1:{stack.gw_port}/v1/chat/completions"}
    rounds = max(1, n // streams)
    tot = {p: {"tokens": 0, "wall": 0.0, "steady": 0.0, "results": []} for p in urls}
    k = 0
    for r in range(warmup_rounds + rounds):
        for path in (("direct", "through") if r % 2 == 0 else ("through", "direct")):
            batch = [bodies.make(k + j) for j in range(streams)]
            k += streams
            t0 = time.perf_counter()
            got = await asyncio.gather(*(one_request(session, urls[path], b, tokens) for b in batch))
            wall = time.perf_counter() - t0
            if r >= warmup_rounds:
                a = tot[path]
                a["tokens"] += sum(x["tokens"] for x in got)
                a["wall"] += wall
                a["steady"] += sum((x["tokens"] - 1) / (x["last_tok"] - x["first_tok"]) for x in got)
                a["results"] += got
    out = {"streams": streams, "rounds": rounds, "requests": rounds * streams, "tokens": tokens}
    for p, a in tot.items():
        out[p] = {"aggregate_tok_s": a["tokens"] / a["wall"],
                  "steady_tok_s": a["steady"] / rounds,
                  "ttft_ms": summary([ms(x["ttft"]) for x in a["results"]]),
                  "total_ms": summary([ms(x["total"]) for x in a["results"]])}
    out["rate_ratio"] = out["through"]["aggregate_tok_s"] / out["direct"]["aggregate_tok_s"]
    out["steady_ratio"] = out["through"]["steady_tok_s"] / out["direct"]["steady_tok_s"]
    out["added_ttft_ms"] = {k2: out["through"]["ttft_ms"][k2] - out["direct"]["ttft_ms"][k2]
                            for k2 in ("p50", "p95", "p99", "max")}
    return out


async def stall_phase(session, stack: Stack, small: Bodies, large: Bodies, iterations: int, probe_tokens: int,
                      load_tokens: int, loads: int) -> dict:
    """A long stream through the gateway while `loads` more requests arrive, 0.4 s apart. Large and small load
    alternate. A blocked event loop shows as a long gap between the probe's tokens."""
    url = f"http://127.0.0.1:{stack.gw_port}/v1/chat/completions"
    gaps: dict[str, list[float]] = {"small": [], "large": []}
    worst: dict[str, list[float]] = {"small": [], "large": []}
    k = 0
    for it in range(iterations + 1):  # the first iteration is a warm-up
        for kind, src in (("large", large), ("small", small)) if it % 2 == 0 else (("small", small), ("large", large)):
            probe = asyncio.create_task(one_request(session, url, small.make(10_000 + k), probe_tokens))
            await asyncio.sleep(0.8)
            tasks = []
            for _ in range(loads):
                k += 1
                tasks.append(asyncio.create_task(one_request(session, url, src.make(k), load_tokens)))
                await asyncio.sleep(0.4)
            r = await probe
            await asyncio.gather(*tasks)
            if it > 0:
                gaps[kind] += [ms(g) for g in r["gaps"]]
                worst[kind].append(ms(max(r["gaps"])))
    return {"iterations": iterations, "probe_tokens": probe_tokens, "loads": loads,
            **{kind: {"gap": summary(g), "worst_gap_per_probe": summary(worst[kind]),
                      "gaps_over_30ms": sum(1 for x in g if x > 30)} for kind, g in gaps.items()}}


async def run_once(args, run_no: int) -> dict:
    import aiohttp
    stack = Stack(args)
    stack.start()
    through = 0
    try:
        rng = random.Random(1000 + run_no)
        conn = aiohttp.TCPConnector(limit=0, force_close=False)
        timeout = aiohttp.ClientTimeout(total=120)
        async with aiohttp.ClientSession(connector=conn, timeout=timeout, auto_decompress=False) as session:
            await asyncio.sleep(1.0)
            c0, w0 = cpu_seconds(stack.gateway.pid), time.perf_counter()
            await asyncio.sleep(args.idle_s)
            idle = (cpu_seconds(stack.gateway.pid) - c0) / (time.perf_counter() - w0)  # CPU seconds per second
            run: dict = {"idle_cpu_ms_per_s": idle * 1000, "latency": {}, "concurrency": {},
                         "loadavg_start": os.getloadavg()[0]}
            sizes = {"small": args.small_bytes, "large": args.large_bytes, "xlarge": args.xlarge_bytes}
            bodies = {c: Bodies(sizes[c], seed=run_no * 10 + i) for i, c in enumerate(sizes)}
            for case in args.cases:
                n = args.xlarge_n if case == "xlarge" else args.n
                log(f"run {run_no}: latency {case} (N={n}, {sizes[case] // 1024} KB)")
                r = await latency_phase(session, stack, bodies[case], n, args.warmup, args.lat_tokens, rng)
                net = max(0.0, r["gateway_cpu_ms_gross"] - idle * r["wall_s"] * 1000 / n)  # minus idle drift
                r["gateway_cpu_ms_per_request"] = net
                run["latency"][case] = r
                through += n + args.warmup
            for case in args.conc_cases:
                log(f"run {run_no}: concurrency {case} ({args.streams} streams, N={args.n})")
                c0 = cpu_seconds(stack.gateway.pid)
                run["concurrency"][case] = await concurrency_phase(session, stack, bodies[case], args.n, args.streams,
                                                                   args.warmup_rounds, args.conc_tokens)
                run["concurrency"][case]["gateway_cpu_ms_per_request_gross"] = ms(
                    (cpu_seconds(stack.gateway.pid) - c0) / ((args.n // args.streams + args.warmup_rounds) * args.streams))
                through += (args.n // args.streams + args.warmup_rounds) * args.streams
            if args.stall_iterations:
                log(f"run {run_no}: stall probe ({args.stall_iterations} iterations per kind)")
                run["stall"] = await stall_phase(session, stack, bodies["small"], bodies["large"],
                                                 args.stall_iterations, args.stall_tokens, 10, 3)
                through += (args.stall_iterations + 1) * 2 * (1 + 3)
        await asyncio.sleep(0.5)
        run["records_written"], run["through_requests"] = stack.records_written(), through
        run["loadavg_end"] = os.getloadavg()[0]
        return run
    finally:
        stack.stop()


# ---------------------------------------------------------------- report

MACHINE_CLASS = {"x86_64": "x86-64", "amd64": "x86-64", "aarch64": "arm64", "arm64": "arm64"}


def machine(detail: bool = False) -> dict:
    """The machine of a run. By default only its class (x86-64, arm64 or other) and its OS name, so a
    published result does not name the CPU model, the CPU count, the kernel build or the governor.
    `detail` (--machine-detail) adds them."""
    import aiohttp
    commit = subprocess.run(["git", "-C", str(ROOT), "rev-parse", "--short", "HEAD"], capture_output=True,
                            text=True).stdout.strip()
    if not detail:
        return {"cpu": MACHINE_CLASS.get(platform.machine().lower(), "other"),
                "python": platform.python_version(), "aiohttp": aiohttp.__version__, "os": platform.system(),
                "kiln_commit": commit or None, "detail": False}
    model = "unknown"
    try:
        for line in Path("/proc/cpuinfo").read_text().splitlines():
            if line.startswith("model name"):
                model = line.split(":", 1)[1].strip()
                break
    except OSError:
        pass
    try:
        gov = Path("/sys/devices/system/cpu/cpu0/cpufreq/scaling_governor").read_text().strip()
    except OSError:
        gov = None
    return {"cpu": model, "logical_cpus": os.cpu_count(), "cpu_governor": gov,
            "python": platform.python_version(), "aiohttp": aiohttp.__version__,
            "os": f"{platform.system()} {platform.release()}", "kiln_commit": commit or None, "detail": True}


def spread(values: list[float]) -> tuple[float, float, float]:
    s = sorted(values)
    mid = s[len(s) // 2] if len(s) % 2 else (s[len(s) // 2 - 1] + s[len(s) // 2]) / 2
    return mid, s[0], s[-1]


def cell(values: list[float], digits: int = 1) -> str:
    mid, lo, hi = spread(values)
    if len(values) == 1:
        return f"{mid:.{digits}f}"
    return f"{mid:.{digits}f} ({lo:.{digits}f} to {hi:.{digits}f})"


def measured_line(m: dict) -> str:
    """The report's line about the machine: the CPU count only with --machine-detail."""
    where = (f"{m['cpu']} ({m['logical_cpus']} logical CPUs), Python {m['python']}, aiohttp {m['aiohttp']}, "
             f"{m['os']}" if m.get("detail", True) else
             f"a test machine ({m['cpu']}, {m['os']}), Python {m['python']}, "
             f"aiohttp {m['aiohttp']}")
    return f"Measured with `tools/bench_gateway.py` on {where}, Kiln commit `{m['kiln_commit']}`."


def markdown(result: dict) -> str:
    runs, cfg, m = result["runs"], result["config"], result["machine"]
    L: list[str] = []
    w = L.append

    def col(path: tuple, digits=1):
        vals = []
        for r in runs:
            v = r
            for p in path:
                v = v[p]
            vals.append(v)
        return cell(vals, digits)

    w("# Kiln gateway overhead")
    w("")
    w(measured_line(m))
    w(f"The full run was repeated {len(runs)} times. Each cell shows the median of the repeats, "
      f"then the lowest and highest repeat in brackets. All times are in milliseconds, measured by the client.")
    w("")
    w("## What the bench does")
    w("")
    w(f"- A mock engine on 127.0.0.1 streams SSE chat chunks. The first token comes {cfg['first_ms']:g} ms after the "
      f"request body arrives. After that, one token comes every {cfg['interval_ms']:g} ms. The last chunk carries usage.")
    w("- The gateway is a real `kiln gateway` process with a temp config, a temp state file and a temp record directory. "
      "The mock engine and the gateway run in their own processes. Neither shares a process with the client.")
    w("- Each pair of requests sends the same body direct to the mock and through the gateway, in random order. "
      "The gateway adds its own work: it reads and parses the body, computes the prefix digests, rewrites the body, "
      "forces `include_usage`, observes the stream and writes one record per request.")
    w("- Added time is the through value minus the direct value, taken at the same percentile.")
    w(f"- N={cfg['n']} measured requests per case ({cfg['xlarge_n']} for the xlarge case), after {cfg['warmup']} warm-up "
      f"requests that the tables leave out. Latency streams carry {cfg['lat_tokens']} tokens. "
      f"Concurrency streams carry {cfg['conc_tokens']} tokens.")
    w("")
    sizes = {c: runs[0]["latency"][c] for c in cfg["cases"]}
    w("| Case | Body size | Messages |")
    w("|---|---|---|")
    for c, r in sizes.items():
        w(f"| {c} | {r['body_bytes'] / 1024:.0f} KB | {r['messages']} |")
    w("")
    w("The xlarge case is above `PREFIX_THREAD_BYTES` (1 MiB), so the gateway hashes it in a worker thread. "
      "The other cases hash on the event loop.")
    w("")
    w("## Added time, one request at a time")
    w("")
    for c in cfg["cases"]:
        w(f"### {c} body ({sizes[c]['body_bytes'] / 1024:.0f} KB)")
        w("")
        w("| Metric | p50 | p95 | p99 | max |")
        w("|---|---|---|---|---|")
        for label, key in (("Added time to first byte", "ttfb"), ("Added time to first token", "ttft")):
            w(f"| {label} | " + " | ".join(col(("latency", c, "added", key, q), 2) for q in ("p50", "p95", "p99", "max")) + " |")
        w("| Added delay between tokens | " + " | ".join(col(("latency", c, "added", "gap", q), 2) for q in ("p50", "p95", "p99", "max")) + " |")
        w("| Added total request time | " + " | ".join(col(("latency", c, "added", "total", q), 2) for q in ("p50", "p95", "p99", "max")) + " |")
        w("")
        w("| Absolute (direct, then through) | p50 | p99 | max |")
        w("|---|---|---|---|")
        for label, key in (("Time to first token", "ttft"), ("Total request time", "total"), ("Delay between tokens", "gap")):
            for path in ("direct", "through"):
                w(f"| {label}, {path} | " + " | ".join(col(("latency", c, path, key, q), 2) for q in ("p50", "p99", "max")) + " |")
        w("")
        w(f"Gateway CPU time per request (user plus system, idle load removed): "
          f"{col(('latency', c, 'gateway_cpu_ms_per_request'), 2)} ms.")
        w("")
    if runs[0]["concurrency"]:
        w(f"## {cfg['streams']} concurrent streams")
        w("")
        w(f"Each round starts {cfg['streams']} streams at the same moment and waits for all of them. Direct rounds "
          f"and through rounds alternate. A stream carries {cfg['conc_tokens']} tokens. "
          f"The ideal rate is {cfg['streams'] * 1000 / cfg['interval_ms']:.0f} tokens per second.")
        w("")
        w("| Case | Direct, tokens/s | Through, tokens/s | Through / direct | Steady rate through / direct | Added time to first token p50 | p99 | max | Gateway CPU per request |")
        w("|---|---|---|---|---|---|---|---|---|")
        for c in runs[0]["concurrency"]:
            w(f"| {c} | {col(('concurrency', c, 'direct', 'aggregate_tok_s'), 1)} | "
              f"{col(('concurrency', c, 'through', 'aggregate_tok_s'), 1)} | {col(('concurrency', c, 'rate_ratio'), 4)} | "
              f"{col(('concurrency', c, 'steady_ratio'), 4)} | "
              + " | ".join(col(("concurrency", c, "added_ttft_ms", q), 2) for q in ("p50", "p99", "max"))
              + f" | {col(('concurrency', c, 'gateway_cpu_ms_per_request_gross'), 2)} ms |")
        w("")
        w("Aggregate rate counts all tokens of a round over the wall time of the round, so it includes the first-token wait. "
          "Steady rate counts only the time from the first to the last token of each stream.")
        w("")
    if "stall" in runs[0]:
        s0 = runs[0]["stall"]
        w("## Does a large request block the event loop?")
        w("")
        w(f"One probe stream of {s0['probe_tokens']} tokens runs through the gateway. While it runs, {s0['loads']} more "
          f"requests arrive, 0.4 s apart. The extra requests are either large (the large body) or small. "
          f"The probe is a small request. A blocked event loop shows as a long gap between probe tokens. "
          f"The expected gap is {cfg['interval_ms']:g} ms. {s0['iterations']} probes per kind.")
        w("")
        w("| Extra requests | Probe gap p50 | p99 | max | Worst gap per probe, median | Gaps over 30 ms |")
        w("|---|---|---|---|---|---|")
        for kind in ("small", "large"):
            w(f"| {kind} | " + " | ".join(col(("stall", kind, "gap", q), 2) for q in ("p50", "p99", "max"))
              + f" | {col(('stall', kind, 'worst_gap_per_probe', 'p50'), 2)} | {col(('stall', kind, 'gaps_over_30ms'), 0)} |")
        w("")
    w("## Checks")
    w("")
    for i, r in enumerate(runs, 1):
        w(f"- Repeat {i}: the gateway wrote {r['records_written']} records for {r['through_requests']} requests "
          f"that went through it. Idle gateway CPU was {r['idle_cpu_ms_per_s']:.2f} ms per second. "
          f"One-minute load average was {r['loadavg_start']:.1f} at the start and {r['loadavg_end']:.1f} at the end.")
    w("")
    w("Every stream arrived whole: the client checks the token count and the final `[DONE]` of each request, "
      "and stops the run on a mismatch.")
    w("")
    return "\n".join(L)


# ---------------------------------------------------------------- main

def log(msg: str) -> None:
    print(f"[{time.strftime('%H:%M:%S')}] {msg}", file=sys.stderr, flush=True)


def parse_args(argv):
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--mock-engine", action="store_true", help=argparse.SUPPRESS)
    ap.add_argument("--port", type=int, help=argparse.SUPPRESS)
    ap.add_argument("--tokens", dest="mock_tokens", type=int, default=300, help="default tokens per mock stream")
    ap.add_argument("--first-ms", type=float, default=50, help="first token this long after the request body arrives")
    ap.add_argument("--interval-ms", type=float, default=15, help="time between tokens")
    ap.add_argument("--fast", action="store_true", help="smoke run: N=10, short streams, one repeat")
    ap.add_argument("--n", type=int, default=200, help="measured requests per case")
    ap.add_argument("--xlarge-n", type=int, default=100)
    ap.add_argument("--warmup", type=int, default=20, help="requests per case that are not measured")
    ap.add_argument("--repeats", type=int, default=3)
    ap.add_argument("--cases", default="small,large,xlarge")
    ap.add_argument("--conc-cases", default="small,large")
    ap.add_argument("--streams", type=int, default=4)
    ap.add_argument("--warmup-rounds", type=int, default=3)
    ap.add_argument("--lat-tokens", type=int, default=30, help="tokens per stream in the latency phase")
    ap.add_argument("--conc-tokens", type=int, default=100, help="tokens per stream in the concurrency phase")
    ap.add_argument("--stall-iterations", type=int, default=20, help="probes per kind; 0 turns the phase off")
    ap.add_argument("--stall-tokens", type=int, default=300)
    ap.add_argument("--small-bytes", type=int, default=1024)
    ap.add_argument("--large-bytes", type=int, default=600 * 1024)
    ap.add_argument("--xlarge-bytes", type=int, default=2 * 1024 * 1024)
    ap.add_argument("--idle-s", type=float, default=5.0)
    ap.add_argument("--out-dir", default=str(ROOT / "docs" / "bench"))
    ap.add_argument("--stem", default="gateway-overhead", help="output file name without extension")
    ap.add_argument("--machine-detail", action="store_true",
                    help="name the CPU model, the kernel release and the CPU governor in the results")
    ap.add_argument("--no-raw", action="store_true", help="leave the per-request samples out of the JSON file")
    args = ap.parse_args(argv)
    if args.fast:
        args.n = args.xlarge_n = 10
        args.warmup, args.repeats, args.warmup_rounds = 2, 1, 1
        args.lat_tokens = args.conc_tokens = 8
        args.stall_iterations, args.stall_tokens = 2, 60
        args.idle_s = 1.0
        args.large_bytes, args.xlarge_bytes = 200 * 1024, 1100 * 1024
    args.cases = [c for c in args.cases.split(",") if c]
    args.conc_cases = [c for c in args.conc_cases.split(",") if c and c in args.cases]
    return args


def main(argv=None) -> int:
    args = parse_args(argv)
    if args.mock_engine:
        asyncio.run(run_mock_engine(args.port, args.first_ms, args.interval_ms, args.mock_tokens))
        return 0
    sys.path.insert(0, str(SRC))
    runs = []
    for i in range(1, args.repeats + 1):
        runs.append(asyncio.run(run_once(args, i)))
    cfg = {k: getattr(args, k) for k in ("first_ms", "interval_ms", "n", "xlarge_n", "warmup", "repeats", "cases",
                                         "streams", "lat_tokens", "conc_tokens", "small_bytes", "large_bytes",
                                         "xlarge_bytes", "stall_iterations", "stall_tokens")}
    result = {"schema": 1, "date_utc": time.strftime("%Y-%m-%d", time.gmtime()), "machine": machine(args.machine_detail),
              "config": cfg, "runs": runs}
    out = Path(args.out_dir)
    out.mkdir(parents=True, exist_ok=True)
    md = markdown(result)
    if args.no_raw:
        for r in runs:
            for c in r["latency"].values():
                c.pop("raw_ms", None)
    (out / f"{args.stem}.json").write_text(json.dumps(result, indent=1) + "\n")
    (out / f"{args.stem}.md").write_text(md + "\n")
    log(f"wrote {out / (args.stem + '.json')} and {out / (args.stem + '.md')}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
