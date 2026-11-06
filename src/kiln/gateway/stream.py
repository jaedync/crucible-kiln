"""Observe an OpenAI-compatible response without changing a byte of it.

The gateway forwards every chunk unchanged and feeds a copy here. The observer
splits server-sent events at line boundaries (chunks may cut anywhere), and
records when each token-bearing event arrived, the final usage report and the
finish reason. These timestamps are the source of every live rate (see
ARCHITECTURE.md, correctness rules 1-3).
"""
from __future__ import annotations

import json
from dataclasses import dataclass, field

# Delta fields that carry generated text. Engines differ: Halogen and Gufo
# send reasoning_content, some servers send reasoning.
TEXT_DELTA_KEYS = ("content", "reasoning_content", "reasoning")


# Paths under /v1 that generate tokens. Anything else (count_tokens, tokenize, ...) is a utility
# call: relayed and logged, but not a request in usage figures or the live view.
GENERATION_PATHS = frozenset({"chat/completions", "completions", "messages", "responses"})


def is_generation(path: str | None) -> bool:
    return (path or "").strip("/") in GENERATION_PATHS


def safe_error(err) -> str:
    """An upstream error reduced to its type and code: messages can quote the prompt."""
    if isinstance(err, dict):
        parts = [str(err[k])[:60] for k in ("type", "code") if isinstance(err.get(k), (str, int))]
        return "upstream: " + (" / ".join(parts) if parts else "error")
    return "upstream: error"


def _int(v):
    return v if isinstance(v, int) and not isinstance(v, bool) else None


def ended_whole(finish, usage) -> bool:
    """Whether a stream the client closed had already ended: its finish reason and the usage report (the
    last event before [DONE]) were relayed. Pi closes the connection a few ms after the last token; that is a
    completed request, not a cancel (in one day, 262 of 266 "client_disconnected" records)."""
    return bool(finish) and isinstance(usage, dict) and usage.get("completion") is not None


def normalize_usage(u: dict) -> dict:
    """OpenAI usage (with optional details) -> {prompt, cached, completion, reasoning, ...optional flags}."""
    pd = u.get("prompt_tokens_details") if isinstance(u.get("prompt_tokens_details"), dict) else {}
    cd = u.get("completion_tokens_details") if isinstance(u.get("completion_tokens_details"), dict) else {}
    cached = _int(pd.get("cached_tokens"))
    if cached is None:
        cached = _int(u.get("cached_tokens"))  # some servers put it top level
    out = {"prompt": _int(u.get("prompt_tokens")), "cached": cached,
           "completion": _int(u.get("completion_tokens")), "reasoning": _int(cd.get("reasoning_tokens"))}
    # Halogen reports when and why thinking was cut off (its "answer room").
    if isinstance(cd.get("reasoning_closed_by"), str):
        out["think_close"] = cd["reasoning_closed_by"][:40]
        out["think_closed_at"] = _int(cd.get("reasoning_closed_at"))
    # Crucible: the model output was not a valid tool call and went back as content. A boolean, no text.
    if u.get("tool_call_fallback") is True:
        out["tool_call_fallback"] = True
    return out


@dataclass
class StreamStats:
    first_byte: float | None = None
    first_token: float | None = None
    last_token: float | None = None
    token_events: int = 0
    chars: int = 0
    usage: dict | None = None
    finish: str | None = None
    done: bool = False
    error: str | None = None
    # Arrival times of token events not yet handed to the live feed.
    pending: list = field(default_factory=list)
    # Token events per arrival second, kept so history survives a dashboard restart.
    per_second: dict = field(default_factory=dict)

    def take_pending(self) -> list:
        out, self.pending = self.pending, []
        return out

    def histogram(self) -> list | None:
        """[first epoch second, [events in that second, the next, ...]] or None."""
        if not self.per_second:
            return None
        first, last = min(self.per_second), max(self.per_second)
        return [first, [self.per_second.get(s, 0) for s in range(first, last + 1)]]


class SSEObserver:
    def __init__(self):
        self.stats = StreamStats()
        self._buf = b""

    def feed(self, chunk: bytes, now: float) -> int:
        """Consume a chunk; return the token-bearing events it completed."""
        s = self.stats
        if s.first_byte is None:
            s.first_byte = now
        self._buf += chunk
        n = 0
        while (i := self._buf.find(b"\n")) >= 0:
            line, self._buf = self._buf[:i], self._buf[i + 1:]
            n += self._line(line.rstrip(b"\r"), now)
        return n

    def _line(self, line: bytes, now: float) -> int:
        if not line.startswith(b"data:"):
            return 0
        payload = line[5:].strip()
        if payload == b"[DONE]":
            self.stats.done = True
            return 0
        try:
            obj = json.loads(payload)
        except ValueError:
            return 0
        return self.event(obj, now) if isinstance(obj, dict) else 0

    def event(self, obj: dict, now: float) -> int:
        s = self.stats
        if isinstance(obj.get("usage"), dict):
            s.usage = normalize_usage(obj["usage"])
        if obj.get("error"):
            s.error = safe_error(obj["error"])
        tokens = 0
        for ch in obj.get("choices") or []:
            if not isinstance(ch, dict):
                continue
            if ch.get("finish_reason"):
                s.finish = ch["finish_reason"]
            d = ch.get("delta") if isinstance(ch.get("delta"), dict) else {}
            text = "".join(v for k in TEXT_DELTA_KEYS if isinstance(v := d.get(k), str))
            if isinstance(ch.get("text"), str):  # /v1/completions streams choices[].text
                text += ch["text"]
            if text or d.get("tool_calls"):
                tokens += 1
                s.chars += len(text)
        if tokens:
            if s.first_token is None:
                s.first_token = now
            s.last_token = now
            s.token_events += tokens
            s.pending.extend([now] * tokens)
            sec = int(now)
            s.per_second[sec] = s.per_second.get(sec, 0) + tokens
        return tokens


def observe_json_body(body: bytes) -> StreamStats:
    """Usage and finish reason from a non-streamed response."""
    s = StreamStats()
    try:
        obj = json.loads(body)
    except ValueError:
        return s
    if not isinstance(obj, dict):
        return s
    if isinstance(obj.get("usage"), dict):
        s.usage = normalize_usage(obj["usage"])
    choices = obj.get("choices") or []
    if choices and isinstance(choices[0], dict):
        s.finish = choices[0].get("finish_reason")
    if obj.get("error"):
        s.error = safe_error(obj["error"])
    return s
