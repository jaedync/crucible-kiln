"""The demo's setup and its looped scenario: the profiles, the kiln.toml it runs with, and the traffic.

The scenario is written at speed 1 (about 3.6 minutes a loop). `--speed N` divides its times and its token
counts by N and keeps every rate, so a fast run (CI, screenshots) still shows real tok/s figures.

Clients are three neutral devices. Each sends a header that only the demo's gateway reads (run.py
`demo_peer`), so each counts as its own device at a documentation address (RFC 5737), never a real one.
"""
from __future__ import annotations

import asyncio
import json
import random
import time
from dataclasses import dataclass, field
from pathlib import Path

import aiohttp

from kiln.demo.engine import CONTEXT, SERVED_ID, SLOTS, WORDS

MODEL = "qwen3.8-flash-next"
HOST = "strix-halo"
DEVICE_HEADER = "X-Kiln-Demo-Device"
DEVICES = {"laptop": "192.0.2.11", "desktop": "192.0.2.12", "agent": "192.0.2.13"}
DEFAULT_PROFILE = "crucible-405"
PROFILE_PACKS = {"crucible-405": "4.05", "crucible-305": "3.05"}
LOOP_S = 215.0
SYSTEM = ("You are a careful assistant on a local Strix Halo machine. Answer from the files the user "
          "shares. Keep each answer short.")
TOOLS = [{"type": "function", "function": {
    "name": "read_file", "description": "Read lines of a file in the workspace.",
    "parameters": {"type": "object", "properties": {"path": {"type": "string"},
                                                    "lines": {"type": "array", "items": {"type": "integer"}}},
                   "required": ["path"]}}}]


def filler(tokens: int, seed) -> str:
    """About `tokens` tokens (4 characters each) of synthetic words: never real content."""
    rng = random.Random(seed)
    out, size = [], 0
    while size < tokens * 4:
        w = rng.choice(WORDS)
        out.append(w)
        size += len(w) + 1
    return " ".join(out)[: tokens * 4]


def profiles(port_405: int, port_305: int) -> dict:
    """Two Crucible profiles of the same model, one per pack. Their units are never run: the demo's unit
    runner (edges.FakeUnits) plays systemd."""
    def one(pack: str, port: int, label: str, gib: float) -> dict:
        return {"label": f"Crucible · Qwen3.8 Flash-Next ({label})", "engine": "crucible", "model": MODEL,
                "served_id": SERVED_ID, "hf_id": "Qwen/Qwen3.8-Flash-Next", "port": port, "context": CONTEXT,
                "slots": SLOTS, "kv": {"positions": SLOTS * CONTEXT}, "memory_gib": gib,
                "request_compat": "crucible", "thinking": "pass", "default_max_tokens": 32768,
                "native": {"ExecStart": f"crucible-server --pack {pack} --slots {SLOTS} --port {port}"}}
    return {"crucible-405": one("4.05", port_405, "4.05 bpw", 112),
            "crucible-305": one("3.05", port_305, "3.05 bpw", 95)}


def config_dict(data: Path, dash_port: int, gateway_port: int, events_port: int, engine_ports: tuple[int, int],
                speed: float) -> dict:
    """A kiln.toml for the demo, parsed by config.parse like any other: loopback only, files in `data`."""
    return {
        "host": HOST,
        "gateway": {"listen": f"127.0.0.1:{gateway_port}", "events_listen": f"127.0.0.1:{events_port}",
                    "data_dir": str(data / "gateway"), "state_file": str(data / "state" / "active.json")},
        # A fast run computes the same tokens in less time: the impossible-prefill limit moves with it.
        "dash": {"listen": f"127.0.0.1:{dash_port}", "db": str(data / "kiln.db"),
                 "max_prefill_tps": 20_000.0 * max(1.0, speed)},
        "engines": {"quadlet_dir": str(data / "units"), "user_unit_dir": str(data / "units")},
        "pricing": {"reference": {"name": "Claude Sonnet 5.5", "provider_model": "anthropic/claude-sonnet-5.5",
                                  "input_per_m": 2.0, "cached_per_m": 0.1, "output_per_m": 10.0},
                    "openrouter": {"enabled": False}},
        "devices": {name: {"addresses": [addr]} for name, addr in DEVICES.items()},
        "profiles": profiles(*engine_ports),
    }


@dataclass
class Turn:
    """One request of the scenario, at speed 1. `conv` names a conversation; `append` adds a user turn
    (`tokens` long) to it, `tool_result` answers its last tool call, `resend` sends its last prompt again."""
    at: float
    client: str
    conv: str
    tokens: int = 0
    reply: int = 300
    tool: str | None = None
    kind: str = "append"  # append | resend | tool_result
    after: bool = False  # start when the previous turn of this conversation ends (and `at` seconds later)


SCENARIO = (
    # A long cold prompt: about 67 s of prefill at 1,790 tok/s, then decode alone.
    Turn(6, "laptop", "long", tokens=120_000, reply=2_400),
    # Two more clients join; from about 95 s three of the four slots decode at once.
    Turn(82, "desktop", "desk", tokens=6_000, reply=1_100),
    Turn(86, "agent", "agent", tokens=14_000, reply=700),
    # The same prompt again: the conversation cache gives the first token in about 20 ms.
    Turn(150, "laptop", "long", kind="resend", reply=500),
    # A tool call, then the turn with its result.
    Turn(166, "agent", "agent", tokens=900, reply=90, tool="read_file"),
    Turn(1.5, "agent", "agent", kind="tool_result", tokens=1_200, reply=350, after=True),
)


@dataclass
class Conversation:
    messages: list = field(default_factory=list)
    last: list | None = None  # the messages of the last request


class Traffic:
    """Plays SCENARIO through the gateway, as three clients would."""

    def __init__(self, gateway_url: str, speed: float = 1.0, seed: int = 0, log=print):
        self.url, self.speed, self.seed, self.log = gateway_url.rstrip("/"), speed, seed, log
        self.results: list[dict] = []  # one per finished request: client, status, usage, ttft
        self.loops = 0

    def _tokens(self, n: int) -> int:
        return max(48, round(n / self.speed))

    async def play(self, loops: int | None = None, tail: bool = True) -> None:
        """Run the scenario `loops` times (None: until cancelled). tail=False skips the idle end of the last
        loop, for a run that stops after it."""
        async with aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=None)) as session:
            while loops is None or self.loops < loops:
                last = loops is not None and self.loops + 1 >= loops
                await self._loop(session, self.loops, tail=tail or not last)
                self.loops += 1

    async def _loop(self, session: aiohttp.ClientSession, n: int, tail: bool = True) -> None:
        start = time.monotonic()
        convs: dict[str, Conversation] = {}
        ends: dict[str, asyncio.Task] = {}
        tasks = []
        for i, turn in enumerate(SCENARIO):
            if turn.after and turn.conv in ends:
                await ends[turn.conv]
                await asyncio.sleep(turn.at / self.speed)
            else:
                await asyncio.sleep(max(0.0, start + turn.at / self.speed - time.monotonic()))
            conv = convs.setdefault(turn.conv, Conversation())
            body = self._body(turn, conv, seed=(self.seed, n, i))
            task = asyncio.create_task(self._send(session, turn, conv, body))
            ends[turn.conv] = task
            tasks.append(task)
        await asyncio.gather(*tasks)
        if tail:
            await asyncio.sleep(max(0.0, start + LOOP_S / self.speed - time.monotonic()))

    def _body(self, turn: Turn, conv: Conversation, seed) -> dict:
        ask = f" [demo reply={max(16, round(turn.reply / self.speed))}" + (f" tool={turn.tool}]" if turn.tool else "]")
        if turn.kind == "resend" and conv.last:
            messages = conv.last
        else:
            if not conv.messages:
                conv.messages.append({"role": "system", "content": SYSTEM})
            text = filler(self._tokens(turn.tokens), seed=repr(seed)) + ask
            if turn.kind == "tool_result" and conv.messages and conv.messages[-1].get("tool_calls"):
                call = conv.messages[-1]["tool_calls"][0]
                conv.messages.append({"role": "tool", "tool_call_id": call["id"], "content": text})
            else:
                conv.messages.append({"role": "user", "content": text})
            messages = list(conv.messages)
        conv.last = messages
        body = {"model": MODEL, "stream": True, "messages": messages, "max_tokens": 32768}
        if turn.conv == "agent":
            body["tools"] = TOOLS
        return body

    async def _send(self, session: aiohttp.ClientSession, turn: Turn, conv: Conversation, body: dict) -> None:
        t0 = time.monotonic()
        result = {"client": turn.client, "kind": turn.kind, "status": None, "usage": None, "ttft": None}
        content, calls = "", []
        try:
            async with session.post(f"{self.url}/v1/chat/completions", json=body,
                                    headers={DEVICE_HEADER: turn.client, "User-Agent": "kiln-demo"}) as r:
                result["status"] = r.status
                async for raw in r.content:
                    line = raw.strip()
                    if not line.startswith(b"data: ") or line == b"data: [DONE]":
                        continue
                    try:
                        ev = json.loads(line[6:])
                    except ValueError:  # a stream cut by an engine switch ends mid-line
                        break
                    if ev.get("usage"):
                        result["usage"] = ev["usage"]
                    for ch in ev.get("choices") or []:
                        d = ch.get("delta") or {}
                        if result["ttft"] is None and (d.get("content") or d.get("reasoning_content")
                                                       or d.get("tool_calls")):
                            result["ttft"] = time.monotonic() - t0
                        content += d.get("content") or ""
                        for c in d.get("tool_calls") or []:
                            if c.get("id"):
                                calls.append({"id": c["id"], "type": "function",
                                              "function": {"name": c["function"]["name"], "arguments": ""}})
                            elif calls:
                                calls[-1]["function"]["arguments"] += c["function"].get("arguments", "")
        except (aiohttp.ClientError, asyncio.TimeoutError) as e:
            result["error"] = type(e).__name__
        if turn.kind != "resend":
            reply = {"role": "assistant", "content": content}
            if calls:
                reply["tool_calls"] = calls
            conv.messages.append(reply)
        self.results.append(result)
