"""A fake Crucible server for `kiln demo`: OpenAI-compatible HTTP plus the stderr lines Kiln reads.

It serves /v1/chat/completions (streamed SSE with a final usage chunk, or one JSON body), /v1/models and
/health on 127.0.0.1, and hands each log line to `emit` instead of a journal. The lines use the formats
that collect/engines.py and collect/memory.py parse (crucible_prefill, crucible_decode, crucible_turn and
the start lines), so the real ingest and live code read them unchanged.

Timing follows the public Crucible README (v0.33.0 table): cold prefill about 1,790 to 1,850 tok/s, one
session decodes at 56 to 72 tok/s, four sessions about 114 tok/s in total, and a prompt sent again gets its
first token in about 20 ms from the conversation cache. Token counts are characters / 4, because the demo
has no tokenizer; prompts and replies are synthetic filler, never real content.
"""
from __future__ import annotations

import asyncio
import hashlib
import json
import math
import random
import re
import time
from collections import OrderedDict
from dataclasses import dataclass
from typing import Callable

from aiohttp import web

SERVED_ID = "crucible"
SLOTS = 4
CONTEXT = 262_144
CHUNK = 4096  # the prefill report step (collect/engines.CRUCIBLE_PREFILL_CHUNK)
TAIL_ROWS = 8  # a restored prefix still computes the rows after it
RESTORE_S = 0.006  # with the 8 tail rows and the gateway hop, a re-send's first token comes after about 20 ms
DRAFT_K = 7
# Conditional accept rates at draft depth 1..7: about 3.1 tokens per verify round.
ACCEPT = (0.80, 0.70, 0.62, 0.55, 0.47, 0.40, 0.33)
# Total decode rate of n sessions over one session's rate. The README gives 1 and 4 sessions; 3 sessions
# decode at about 115 tok/s in total.
SHARE = {1: 1.0, 2: 1.47, 3: 1.80, 4: 1.78}
DECODE_WHILE_PREFILL = 0.55  # the engine runs prefill chunks between decode steps
PREFILL_WHILE_DECODE = 0.88
FIXED_STATE = 151_770_112  # one slot's recurrent state: a cache entry holds it beside the KV rows
KV_BYTES_PER_TOKEN = 24_576
OTHER_GPU = 4_930_000_000  # prefill workspace, scratch, plans and the HIP runtime: no start line names them
DIRECTIVE = re.compile(r"\[demo(?P<args>[^\]]*)\]")
WORDS = ("the", "kiln", "fires", "at", "a", "steady", "heat", "while", "each", "layer", "settles", "into",
         "glaze", "and", "clay", "holds", "its", "shape", "over", "long", "slow", "hours", "of", "work")


@dataclass(frozen=True)
class Pack:
    """One EXL3 pack of Qwen3.8-Flash-Next, with the figures its start lines and the README give."""
    name: str
    payload: int  # weight bytes read from disk
    arena: int  # the weight arena on the GPU
    prefill_32k: float  # cold prefill tok/s at 32K and at 130K (README v0.33.0)
    prefill_130k: float
    decode_prose: float  # one session at 32K (README v0.33.0): the range one stream decodes in
    decode_code: float
    four_total: float
    slot_state: int = 6_996_972_544  # sequence storage per slot, 256K context
    cache_budget: int = 10_171_187_200
    ple: int = 102_400_491_520  # the BF16 n-gram table, mapped from disk
    stage_mib: float = 2174.2
    checkpoint: int = 1_837_191_168
    cut: int = 152_065_024
    vision_gb: float = 0.35
    draft_mb: float = 125.96

    def gpu_bytes(self, cache_pinned: int) -> int:
        return (self.arena + SLOTS * self.slot_state + cache_pinned + self.checkpoint + self.cut
                + round(self.stage_mib * 2**20) + round(self.vision_gb * 1e9) + round(self.draft_mb * 1e6)
                + OTHER_GPU)


PACKS = {
    "4.05": Pack("4.05", payload=68_997_727_232, arena=69_428_838_400, prefill_32k=1851, prefill_130k=1789,
                 decode_prose=56.3, decode_code=71.7, four_total=113.8),
    "3.05": Pack("3.05", payload=51_960_000_000, arena=52_340_000_000, prefill_32k=1870, prefill_130k=1809,
                 decode_prose=60.3, decode_code=73.9, four_total=118.3),
}


def count_tokens(text: str) -> int:
    return math.ceil(len(text) / 4)


def _text(content) -> str:
    if isinstance(content, str):
        return content
    if isinstance(content, list):  # OpenAI content parts
        return "".join(p.get("text", "") for p in content if isinstance(p, dict))
    return ""


def message_key(m: dict) -> str:
    """What makes two messages the same for the cache: role, text and tool calls (reasoning is dropped,
    as clients often drop it)."""
    d = {"role": m.get("role"), "content": _text(m.get("content")), "tool_calls": m.get("tool_calls") or None,
         "tool_call_id": m.get("tool_call_id")}
    return hashlib.sha256(json.dumps(d, sort_keys=True).encode()).hexdigest()[:16]


def message_tokens(m: dict) -> int:
    return count_tokens(_text(m.get("content"))) + count_tokens(json.dumps(m.get("tool_calls") or "")) + 4


def directive(messages: list) -> dict:
    """`[demo reply=N tool=NAME]` in the last user message: how the scenario asks for a reply length or a
    tool call. Without it the reply length is the engine's own pick."""
    for m in reversed(messages):
        if m.get("role") in ("user", "tool"):
            found = DIRECTIVE.search(_text(m.get("content")))
            if found:
                return dict(kv.split("=", 1) for kv in found["args"].split() if "=" in kv)
            return {}
    return {}


@dataclass
class Entry:
    keys: tuple[str, ...]
    cum: tuple[int, ...]  # tokens through each message
    bytes: int


class ConvCache:
    """The conversation cache: saved states by message prefix, least recently used out first."""

    def __init__(self, budget: int):
        self.budget, self.entries, self.evictions, self.pinned = budget, OrderedDict(), 0, 0

    @property
    def bytes(self) -> int:
        return sum(e.bytes for e in self.entries.values())

    def match(self, keys: tuple[str, ...]) -> tuple[int, str]:
        """(cached tokens, session label) for a prompt of these messages."""
        best, label = 0, "miss"
        for k, e in list(self.entries.items()):
            n = 0
            while n < min(len(keys), len(e.keys)) and keys[n] == e.keys[n]:
                n += 1
            if n and e.cum[n - 1] > best:
                best = e.cum[n - 1]
                # A turn that continues after the saved reply resumes the previous turn's decode-time tail.
                label = "hit_replay" if n == len(e.keys) and len(keys) > n else "hit_session"
                self.entries.move_to_end(k)
        return best, label

    def save(self, keys: tuple[str, ...], cum: tuple[int, ...]) -> int:
        size = FIXED_STATE + cum[-1] * KV_BYTES_PER_TOKEN
        self.entries[keys] = Entry(keys, cum, size)
        self.entries.move_to_end(keys)
        while self.bytes > self.budget and len(self.entries) > 1:
            self.entries.popitem(last=False)
            self.evictions += 1
        # The pinned pool grows in 512 MiB steps and never shrinks.
        self.pinned = min(self.budget, max(self.pinned, -(-self.bytes // 2**29) * 2**29))
        return size


class FakeEngine:
    """One running engine of a pack. `emit(line, ts)` takes every stderr line it prints."""

    def __init__(self, pack: Pack, port: int, emit: Callable[[str, float], None], seed: int = 0,
                 clock=time.time):
        self.pack, self.port, self.emit, self.seed, self.clock = pack, port, emit, seed, clock
        self.loaded = False
        self.prefilling = 0
        self.decoding = 0
        self.cache = ConvCache(pack.cache_budget)
        self.states_bytes = 38_200_000_000  # saved conversation states on disk from earlier sessions
        self._next_id = 0
        self._slots = asyncio.Semaphore(SLOTS)
        self._runner: web.AppRunner | None = None

    # -- life cycle ----------------------------------------------------------------
    def start_lines(self) -> list[str]:
        p = self.pack
        return [
            f"PLE table on host: BF16, {p.ple} bytes not resident, 1 segment(s), ngram_embedding_bf16.safetensors",
            f"weight payload {p.payload} bytes, arena reserved {p.arena} bytes, staging peak 1",
            f"vision tower {p.vision_gb} GB loaded in 1.5 s",
            f"draft head 65536 tokens, {p.draft_mb} MB, original EXL3 blocks",
            f"stage arena: {p.stage_mib} MiB (shared; attention {p.stage_mib} MiB, MoE 1995.5 MiB)",
            f"sequence storage: CRUCIBLE_COMPACT_STATE=1 raw_slots={SLOTS * 4} mtp=lazy "
            f"logical_state_bytes={p.slot_state}",
            f"conversation cache: {p.cache_budget} bytes budget, host reserve 11264 MiB",
            f"slot checkpoints: 3 per slot, {p.checkpoint // 12} bytes each, {p.checkpoint} bytes from the cache budget",
            f"prefill cut buffer: {p.cut} bytes from the cache budget",
        ]

    async def start(self, load_s: float = 0.0, on_line_s: Callable[[int], None] | None = None) -> None:
        """Listen at once (503 while loading, as the real server's port answers late), print the start
        lines over `load_s`, then serve."""
        app = web.Application(client_max_size=64 * 2**20)
        app.router.add_get("/health", self.health)
        app.router.add_get("/v1/models", self.models)
        app.router.add_post("/v1/chat/completions", self.chat)
        # A stop is a kill for the streams still open (systemctl stop): they must not hold it for a minute.
        self._runner = web.AppRunner(app, access_log=None, handler_cancellation=True, shutdown_timeout=0.2)
        await self._runner.setup()
        await web.TCPSite(self._runner, "127.0.0.1", self.port).start()
        lines = self.start_lines()
        for i, line in enumerate(lines):
            if load_s:
                await asyncio.sleep(load_s / len(lines))
            self.emit(line, self.clock())
            if on_line_s:
                on_line_s(i + 1)
        self.loaded = True

    async def stop(self) -> None:
        self.loaded = False
        if self._runner:
            await self._runner.cleanup()
            self._runner = None

    def gpu_bytes(self) -> int:
        return self.pack.gpu_bytes(self.cache.pinned)

    # -- small endpoints --------------------------------------------------------------
    async def health(self, _r: web.Request) -> web.Response:
        if not self.loaded:
            return web.json_response({"status": "loading"}, status=503)
        return web.json_response({"status": "ok", "slots": SLOTS, "busy": self.prefilling + self.decoding})

    async def models(self, _r: web.Request) -> web.Response:
        return web.json_response({"object": "list", "data": [{"id": SERVED_ID, "object": "model",
                                                              "owned_by": "crucible", "context_length": CONTEXT}]})

    # -- rates ----------------------------------------------------------------------------
    def prefill_rate(self, prompt: int, rng: random.Random) -> float:
        p = self.pack
        f = min(1.0, max(0.0, (prompt - 32_768) / (131_072 - 32_768)))
        return (p.prefill_32k - (p.prefill_32k - p.prefill_130k) * f) * rng.uniform(0.995, 1.005)

    def stream_rate(self, single: float) -> float:
        """One stream's decode tok/s now: its single-session rate shared with the other decoding streams."""
        n = max(1, min(SLOTS, self.decoding))
        rate = single * SHARE[n] / n
        return rate * (DECODE_WHILE_PREFILL if self.prefilling else 1.0)

    # -- generation ------------------------------------------------------------------------
    async def chat(self, request: web.Request) -> web.StreamResponse:
        try:
            body = await request.json()
        except ValueError:
            return web.json_response({"error": {"message": "body must be JSON"}}, status=400)
        if not self.loaded:
            return web.json_response({"error": {"message": "loading"}}, status=503)
        if body.get("model") != SERVED_ID:
            return web.json_response({"error": {"message": f"unknown model {body.get('model')!r}"}}, status=404)
        messages = body.get("messages") if isinstance(body.get("messages"), list) else []
        self._next_id += 1
        turn = Turn(self, self._next_id, body, messages)
        if body.get("stream"):
            resp = web.StreamResponse(headers={"Content-Type": "text/event-stream", "Cache-Control": "no-cache"})
            await resp.prepare(request)
            await turn.run(resp)
            await resp.write(b"data: [DONE]\n\n")
            await resp.write_eof()
            return resp
        await turn.run(None)
        message = {"role": "assistant", "content": turn.content or None}
        if turn.tool_calls:
            message["tool_calls"] = turn.tool_calls
        return web.json_response({"id": f"chatcmpl-{turn.id}", "object": "chat.completion", "model": SERVED_ID,
                                  "choices": [{"index": 0, "message": message, "finish_reason": turn.finish}],
                                  "usage": turn.usage()})


class Turn:
    """One request through the fake engine: queue, prefill with progress lines, speculative decode."""

    def __init__(self, engine: FakeEngine, rid: int, body: dict, messages: list):
        self.e, self.id, self.body = engine, rid, body
        self.rng = random.Random(f"{engine.seed}:{engine.pack.name}:{rid}")
        self.keys = tuple(message_key(m) for m in messages)
        cum, total = [], 3
        for m in messages:
            total += message_tokens(m)
            cum.append(total)
        self.tools_tokens = count_tokens(json.dumps(body.get("tools"))) if body.get("tools") else 0
        self.cum = tuple(c + self.tools_tokens for c in cum)
        self.prompt = (cum[-1] if cum else 3) + self.tools_tokens
        d = directive(messages)
        limit = body.get("max_completion_tokens") or body.get("max_tokens")
        want = int(d["reply"]) if d.get("reply", "").isdigit() else self.rng.randint(180, 640)
        self.n = max(1, min(want, limit if isinstance(limit, int) and limit > 0 else want))
        self.tool = d.get("tool")
        self.content, self.tool_calls, self.finish = "", [], "stop"
        self.cached, self.label = 0, "miss"
        self.reasoning = 0
        self.rounds, self.accepted = 0, 0
        self.pos_tested, self.pos_accepted = [0] * DRAFT_K, [0] * DRAFT_K
        self.t = {}

    def usage(self) -> dict:
        return {"prompt_tokens": self.prompt, "completion_tokens": self.done, "total_tokens": self.prompt + self.done,
                "prompt_tokens_details": {"cached_tokens": self.cached},
                "completion_tokens_details": {"reasoning_tokens": self.reasoning}}

    def _chunk(self, delta: dict | None = None, finish: str | None = None, usage: dict | None = None) -> bytes:
        ev = {"id": f"chatcmpl-{self.id}", "object": "chat.completion.chunk", "model": SERVED_ID,
              "choices": [] if usage else [{"index": 0, "delta": delta or {}, "finish_reason": finish}]}
        if usage:
            ev["usage"] = usage
        return b"data: " + json.dumps(ev).encode() + b"\n\n"

    async def run(self, resp: web.StreamResponse | None) -> None:
        e, clock = self.e, self.e.clock
        self.t["submit"] = clock()
        self.done = 0
        async with e._slots:
            self.t["admit"] = clock()
            await self._prefill()
            await self._decode(resp)
        self.t["done"] = clock()
        if resp is not None:
            await resp.write(self._chunk({}, self.finish))
            await resp.write(self._chunk(usage=self.usage()))
        self._log_turn()

    async def _prefill(self) -> None:
        e, clock = self.e, self.e.clock
        cached, self.label = e.cache.match(self.keys)
        self.cached = min(cached, max(0, self.prompt - TAIL_ROWS))
        if self.label == "miss":
            self.cached = 0
        todo, done = self.prompt - self.cached, 0
        rate = e.prefill_rate(self.prompt, self.rng)
        e.prefilling += 1
        t0 = clock()
        try:
            if self.cached:
                await asyncio.sleep(RESTORE_S)
            next_report = CHUNK
            while done < todo:
                share = rate / e.prefilling * (PREFILL_WHILE_DECODE if e.decoding else 1.0)
                step = min(todo - done, max(1, round(share * 0.05)))
                await asyncio.sleep(step / share)
                done += step
                while done >= next_report and next_report < todo:
                    self._log_prefill(self.cached + next_report, t0)
                    next_report += CHUNK
            self._log_prefill(self.prompt, t0)
        finally:
            e.prefilling -= 1
        self.t["prefill_end"] = clock()
        self.prefill_s = self.t["prefill_end"] - t0

    def _log_prefill(self, done: int, t0: float) -> None:
        now = self.e.clock()
        self.e.emit("crucible_prefill " + json.dumps({"request_id": self.id, "done": done, "total": self.prompt,
                                                      "elapsed_s": round(now - t0, 4)}), now)

    def _accept(self, room: int) -> int:
        """Drafts the target accepts this round, at most `room`: the reply's end cuts the last round short."""
        a = 0
        while a < DRAFT_K:
            self.pos_tested[a] += 1
            if a >= room or self.rng.random() >= ACCEPT[a]:
                break
            self.pos_accepted[a] += 1
            a += 1
        return a

    async def _decode(self, resp) -> None:
        e, clock = self.e, self.e.clock
        single = self.rng.uniform(self.e.pack.decode_prose, self.e.pack.decode_code)
        think = round(self.n * 0.3) if not self.tool else max(0, self.n - 24)
        self.reasoning = think
        e.decoding += 1
        last_line = None
        args = json.dumps({"path": "notes/plan.md", "lines": [1, 120]})
        try:
            if resp is not None:
                await resp.write(self._chunk({"role": "assistant"}))
            while self.done < self.n:
                k = 1 + self._accept(self.n - self.done - 1)
                self.rounds += 1
                self.accepted += k - 1
                await asyncio.sleep(k / e.stream_rate(single))
                if self.done == 0:
                    self.t["first_token"] = clock()
                    last_line = clock()
                out = []
                for _ in range(k):
                    i = self.done
                    self.done += 1
                    word = " " + WORDS[(self.id * 7 + i) % len(WORDS)]
                    if i < think:
                        out.append({"reasoning_content": word})
                    elif self.tool and not self.tool_calls:
                        self.tool_calls = [{"index": 0, "id": f"call_{self.id}", "type": "function",
                                            "function": {"name": self.tool, "arguments": ""}}]
                        out.append({"tool_calls": [dict(self.tool_calls[0])]})
                    elif self.tool:
                        pass  # Crucible holds a call's non-string parameters back until they close
                    else:
                        self.content += word
                        out.append({"content": word})
                if resp is not None:
                    for delta in out:
                        await resp.write(self._chunk(delta))
                if clock() - last_line >= 1.0:
                    self._log_decode()
                    last_line = clock()
            if self.tool:
                self.tool_calls[0]["function"]["arguments"] = args
                self.finish = "tool_calls"
                if resp is not None:
                    await resp.write(self._chunk({"tool_calls": [{"index": 0, "function": {"arguments": args}}]}))
            self._log_decode()
        finally:
            e.decoding -= 1

    def _log_decode(self) -> None:
        now = self.e.clock()
        self.e.emit("crucible_decode " + json.dumps({
            "request_id": self.id, "completion_tokens": self.done, "spec_rounds": self.rounds,
            "draft_proposed": self.rounds * DRAFT_K, "draft_accepted": self.accepted,
            "elapsed_s": round(now - self.t.get("first_token", now), 4)}), now)

    def _log_turn(self) -> None:
        e, t = self.e, self.t
        reply = {"role": "assistant", "content": self.content}
        if self.tool_calls:
            reply["tool_calls"] = [{k: v for k, v in c.items() if k != "index"} for c in self.tool_calls]
        saved = e.cache.save((*self.keys, message_key(reply)), (*self.cum, self.prompt + self.done))
        e.states_bytes += saved
        proposed = [self.rounds] * DRAFT_K
        line = {
            "request_id": self.id, "prompt_tokens": self.prompt, "cached_tokens": self.cached,
            "prefill_tokens": self.prompt - self.cached, "completion_tokens": self.done,
            "queue_s": round(t["admit"] - t["submit"], 4), "prefill_s": round(self.prefill_s, 4),
            "decode_s": round(t["done"] - t.get("first_token", t["done"]), 4),
            "ttft_s": round(t.get("first_token", t["done"]) - t["submit"], 4), "finish": self.finish,
            "session_label": self.label,
            "cache_saved_tokens": self.prompt + self.done, "cache_tail_tokens": TAIL_ROWS,
            "cache_entries": len(e.cache.entries), "cache_bytes": e.cache.bytes, "cache_evictions": e.cache.evictions,
            "cache_pinned_bytes": e.cache.pinned, "host_free_mib": 9_830, "slot_context": CONTEXT,
            "replay_hit": self.label == "hit_replay",
            "spec_rounds": self.rounds, "draft_proposed": self.rounds * DRAFT_K, "draft_accepted": self.accepted,
            "draft_pos_proposed": proposed, "draft_pos_tested": self.pos_tested, "draft_pos_accepted": self.pos_accepted,
            "draft_len_hist": [0] * DRAFT_K + [self.rounds], "draft_passes": self.rounds,
            "draft_ms": round(self.rounds * 2.1, 3),
        }
        e.emit("crucible_turn " + json.dumps(line), e.clock())
