"""Did the client keep its conversation prefix? Content-free, in memory only.

A slow turn has two possible causes: the client changed earlier messages (so
any engine must read them again), or the client kept them and the engine's
cache missed. To tell them apart the gateway compares each request's messages
with the same client's earlier requests.

Design rule: no transcript is ever stored, and no hash of a message is
stored either. Each message becomes an HMAC under a random key that
is made when the process starts and never written anywhere, so the values are
useless outside this process and gone at restart. Only counts and booleans
leave this module, plus a random conversation id that is not derived from
content.

A conversation sends all its earlier messages again on each turn. To avoid
serializing them again, the tracker keeps a map from a keyed hash of a
message's bytes as the client sent them to the message's digest. That hash is
Python's own SipHash, keyed by the random hash secret the interpreter makes
at start (or SHA-256 under a second random key when PYTHONHASHSEED turns the
secret off). The map is bounded, in memory only, and forgets each entry after
FORGET_AFTER_S (6 h), so it follows the same rule.

One device can run several conversations at once (Pi sessions, Hermes), so a
request is matched to the remembered conversation of the same client that
shares the longest message prefix with it.
"""
from __future__ import annotations

import hashlib
import hmac
import itertools
import json
import secrets
import sys
import time
from dataclasses import dataclass, field

MAX_CLIENTS = 64
MAX_CONVERSATIONS = 16  # per client
FORGET_AFTER_S = 6 * 3600
DIGEST_BYTES = 16
# A request with more messages gets no cache diagnostics: each remembered message costs about 50 bytes for
# up to 6 h, in MAX_CLIENTS x MAX_CONVERSATIONS entries (security review M3).
MAX_MESSAGES = 4096
MAX_CACHED = 1 << 15  # client-bytes hash -> digest entries (about 5 MB at the bound)


@dataclass
class Conversation:
    conv: str  # random id, not derived from content
    messages: list[bytes]  # keyed digests, memory only
    system: bytes | None
    tools: bytes | None
    seen: float
    prompt: int | None = None  # usage of the latest request, once it ends
    completion: int | None = None


@dataclass
class Turn:
    """What a request shares with the conversation it continues. `fields` goes into the record."""
    conv: Conversation
    fields: dict = field(default_factory=dict)


class PrefixTracker:
    def __init__(self, clock=time.time):
        self._key = secrets.token_bytes(32)  # never written, never logged
        self._bytes_key = hashlib.sha256(secrets.token_bytes(32))  # keyed state for client-bytes hashes
        self._siphash = bool(sys.flags.hash_randomization)  # hash() is keyed by a random secret
        self._cached: dict = {}  # keyed hash of a message's client bytes -> (its digest, time made)
        self.clock = clock
        self.clients: dict[str, list[Conversation]] = {}

    def _digest(self, value) -> bytes:
        # ensure_ascii: escapes every non-ASCII character, so a lone surrogate (a JS client that cut a string
        # inside an emoji) cannot make the encoding fail.
        data = json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=True).encode("ascii")
        return hmac.new(self._key, data, hashlib.sha256).digest()[:DIGEST_BYTES]

    def _digest_known(self, value, raw, now: float = 0.0) -> bytes:
        """_digest(value), looked up by the bytes the client sent for it (raw) when they were seen before.
        The same bytes always parse to the same value, so the lookup cannot give another value's digest."""
        if raw is None:
            return self._digest(value)
        if self._siphash:
            key = (len(raw), hash(raw))  # 3 times faster than SHA-256 on long conversations
        else:
            h = self._bytes_key.copy()
            h.update(raw)
            key = h.digest()[:DIGEST_BYTES]
        hit = self._cached.get(key)
        if hit is not None and now - hit[1] < FORGET_AFTER_S:
            return hit[0]
        digest = self._digest(value)
        self._cached.pop(key, None)  # so the oldest entry stays first
        self._cached[key] = (digest, now)
        if len(self._cached) > MAX_CACHED:  # drop the oldest quarter
            for old in list(itertools.islice(self._cached, len(self._cached) // 4)):
                self._cached.pop(old, None)
        return digest

    def _expire_cached(self, now: float) -> None:
        """Drop the entries older than FORGET_AFTER_S. Insertion order is age order, so this stops at the
        first young entry."""
        try:
            while self._cached:
                key = next(iter(self._cached))
                if now - self._cached[key][1] < FORGET_AFTER_S:
                    return
                self._cached.pop(key, None)
        except (RuntimeError, KeyError):  # another worker thread changed the map: the next call goes on
            return

    def digests(self, body: dict, raw_messages: list | None = None,
                raw_tools: bytes | None = None) -> tuple[list[bytes], bytes | None] | None:
        """Message and tool digests. raw_messages and raw_tools are the client's bytes for the same values
        (gateway/body.py); they only make it faster. Safe in a worker thread (the cache is a plain dict)."""
        msgs = body.get("messages")
        if not isinstance(msgs, list) or not msgs or len(msgs) > MAX_MESSAGES:
            return None
        if raw_messages is None or len(raw_messages) != len(msgs):
            raw_messages = [None] * len(msgs)
        now = self.clock()
        self._expire_cached(now)
        tools = self._digest_known(body["tools"], raw_tools, now) if body.get("tools") else None
        return [self._digest_known(m, r, now) for m, r in zip(msgs, raw_messages)], tools

    def _forget_old(self, now: float) -> None:
        for key in list(self.clients):
            convs = [c for c in self.clients[key] if now - c.seen < FORGET_AFTER_S]
            if convs:
                self.clients[key] = convs
            else:
                del self.clients[key]

    def _bound(self, current: str) -> None:
        while len(self.clients) > MAX_CLIENTS:  # least recently active client goes first, never the caller
            oldest = min((k for k in self.clients if k != current),
                         key=lambda k: max(c.seen for c in self.clients[k]))
            del self.clients[oldest]

    def start(self, client: str, body: dict, digested: tuple | None = None) -> Turn | None:
        """Match a chat request to its conversation and remember it. None for requests without messages.
        `digested` is digests(body), computed elsewhere (a worker thread for large bodies)."""
        digested = digested or self.digests(body)
        if digested is None:
            return None
        digests, tools = digested
        msgs = body["messages"]
        now = self.clock()
        self._forget_old(now)
        first = msgs[0] if isinstance(msgs[0], dict) else {}
        system = digests[0] if first.get("role") in ("system", "developer") else None
        convs = self.clients.setdefault(client, [])
        self._bound(client)
        best, kept = None, 0
        for c in sorted(convs, key=lambda c: -c.seen):  # ties go to the most recent
            n = _common_prefix(c.messages, digests)
            if n > kept:
                best, kept = c, n
        # Sharing only the system message means a new conversation with the same agent setup.
        if best is not None and kept == 1 and system is not None and len(best.messages) > 1:
            best = None
        fields = {"n_messages": len(msgs), "prefix_msgs_kept": kept if best else 0, "prev_n_messages": None,
                  "system_changed": None, "tools_changed": None, "prev_prompt": None, "prev_completion": None,
                  "new_conversation": best is None}
        if best is None:
            conv = self._add(convs, Conversation(secrets.token_hex(6), digests, system, tools, now))
            return Turn(conv, {**fields, "conv": conv.conv})
        fields.update(conv=best.conv, prev_n_messages=len(best.messages),
                      system_changed=system != best.system, tools_changed=tools != best.tools,
                      prev_prompt=best.prompt, prev_completion=best.completion)
        if kept < len(best.messages):
            # The client rewrote earlier messages (compaction, edits) or branched: keep both lines.
            conv = self._add(convs, Conversation(best.conv, digests, system, tools, now))
        else:
            best.messages, best.system, best.tools, best.seen = digests, system, tools, now
            best.prompt = best.completion = None  # filled when this request ends
            conv = best
        return Turn(conv, fields)

    @staticmethod
    def _add(convs: list[Conversation], conv: Conversation) -> Conversation:
        convs.append(conv)
        if len(convs) > MAX_CONVERSATIONS:
            convs.remove(min(convs, key=lambda c: c.seen))
        return conv

    def finish(self, turn: Turn | None, usage: dict | None) -> None:
        """Remember this request's token counts for the next turn of the conversation."""
        if turn is None or not isinstance(usage, dict):
            return
        turn.conv.prompt, turn.conv.completion = usage.get("prompt"), usage.get("completion")
        turn.conv.seen = self.clock()


def _common_prefix(a: list[bytes], b: list[bytes]) -> int:
    """How many leading digests a and b share. Binary search over list compares (C speed): a session keeps
    up to MAX_CONVERSATIONS branches of one long prefix, and a loop over each digest of each was a hot spot.
    The time this takes depends on the result, as it did with the loop that stopped at the first difference."""
    lo, hi = 0, min(len(a), len(b))  # a[:lo] == b[:lo] holds; the answer is at most hi
    while lo < hi:
        mid = (lo + hi + 1) // 2
        if a[lo:mid] == b[lo:mid]:
            lo = mid
        else:
            hi = mid - 1
    return lo


def cache_math(prompt, cached, kept, prev_n, prev_prompt, prev_completion) -> tuple[int | None, int | None]:
    """(appended_tokens, reread_tokens) for a turn that kept its whole prefix, else (None, None).

    appended = prompt - previous prompt: what the client added since its last request (the reply it
    replays, the tool results, the new message). The previous completion is not counted, because
    clients often drop the reply's reasoning. reread = (prompt - cached) - appended: tokens the
    engine computed beyond what the client added, so a turn is an engine miss when
    prompt - cached > appended + REREAD_FLAG_TOKENS (the soak rule).

    One case content-free counting cannot see: a chat template that renders older turns
    differently (for example it drops old reasoning) changes the token prefix while the
    messages stay the same, so it shows here as an engine miss.
    """
    ints = (prompt, cached, kept, prev_n, prev_prompt)
    if any(not isinstance(v, int) or isinstance(v, bool) for v in ints) or kept < prev_n:
        return None, None
    appended = max(0, prompt - prev_prompt)
    return appended, max(0, (prompt - cached) - appended)
