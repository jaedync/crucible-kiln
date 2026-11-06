"""Parsers for the engines' own per-request log lines.

Each engine prints one line when a request completes; these lines carry what
the HTTP stream can't: prefill time and computed tokens, speculative decoding
stats, KV pool use and whether thinking was cut off. Log formats are not a
stable API, so a line that looks like a completion but doesn't parse raises
LogFormatError and is counted, never silently dropped.

Ported from an earlier metrics reader on a Strix Halo test machine, whose fixtures cover the same lines.
A new engine is a new parse function registered in PARSERS.
"""
from __future__ import annotations

import json
import logging
import math
import re

# A new engine build can add fields Kiln does not know; a value that breaks the design note's promises is
# dropped at this level so a dashboard operator can find it, and never reaches the store.
log = logging.getLogger(__name__)

# The halogen-flash-server image serves only this model.
HALOGEN_MODEL = "qwen3.8-flash-next"
# The image bakes HALOGEN_MAX_TOK=32768 and admits a prompt beside active streams in pieces of
# the same size (HALOGEN_ADMIT_CHUNK 0), so its prefill progress lines land on these boundaries.
HALOGEN_PREFILL_STEP = 32768
# Crucible's server prefills in chunks of its --prefill rows (4096 on a Strix Halo test machine) from where its
# cache left off. The dashboard learns the chunk from each profile's reports (live/prefill.py); this is its start until then.
CRUCIBLE_PREFILL_CHUNK = 4096


class LogFormatError(ValueError):
    pass


# Halogen ------------------------------------------------------------------
# The rate is "n/a" when the decode time is 0 (no or one output token).
H_HEAD = re.compile(r"serve_api: (\w+) (\d+) tok in ([\d.]+)s = (?:\S+ t/s|n/a)$")
H_ROUNDS = re.compile(r"(\d+) rounds, commit [\d.]+/round$")
H_PROMPT = re.compile(r"prompt (\d+)(?: \((\d+) cached, [\d.]+%\))?, prefill ([\d.]+)s"
                      r"(?: \((\d+) new\)| = \S+ t/s)?$")
H_POOL = re.compile(r"pool (\d+)/(\d+) [\d.]+%$")
H_THINK_CLOSED = re.compile(r"think on, closed at (\d+) by ([\w ]+?)\s*$")
H_CLOSEST = re.compile(r"closest earlier: (\d+) tokens shared with a request ([\d.]+)s ago, "
                       r"(all of it|then its prompt differs|then its reply differs)$")
H_CLOSEST_OUTCOME = {"all of it": "all", "then its prompt differs": "prompt_differs",
                     "then its reply differs": "reply_differs"}
# Cache housekeeping lines (no request id): what the engine dropped or moved to make room.
H_CACHE_EVENTS = (
    ("kv_evict", re.compile(r"^kv pool: no room for (?P<positions>\d+) positions; forgot ")),
    ("kv_move", re.compile(r"^kv pool: (?:no room for (?P<positions>\d+) positions; )?moved the (?P<rows>\d+) rows ")),
    ("kv_no_grow", re.compile(r"^kv pool: the region at \d+ this request resumes from cannot grow")),
    ("cache_cap_evict", re.compile(r"^prompt cache: at its (?P<cap>\d+)-entry cap, forgot ")),
)
H_PROGRESS = re.compile(r"flash_serve: req (\d+) (generated|prefill) (\d+)(?:/(\d+))? tokens, ([\d.]+) s$")


def parse_halogen(msg: str) -> tuple[str, dict] | None:
    """('completion' | 'progress' | 'cache_event', record) for request and cache lines, None otherwise."""
    if msg.startswith(("kv pool: ", "prompt cache: ")):
        for event, pattern in H_CACHE_EVENTS:
            if m := pattern.match(msg):
                return "cache_event", {"event": event,
                                       "values": {k: int(v) for k, v in m.groupdict().items() if v is not None}}
        return None
    if msg.startswith("flash_serve: req "):
        m = H_PROGRESS.match(msg)
        if not m:
            return None
        req, phase, tokens, total, elapsed = m.groups()
        rec = {"req": req, "phase": "decode" if phase == "generated" else "prefill",
               "tokens": int(tokens), "elapsed_s": float(elapsed)}
        if total:
            rec["total"] = int(total)
        return "progress", rec
    if not msg.startswith("serve_api: ") or " tok in " not in msg:
        return None
    parts = [p.strip() for p in msg.split(" | ")]
    head = H_HEAD.match(parts[0])
    prompt = next((m for m in map(H_PROMPT.match, parts[1:]) if m), None)
    if not head or not prompt:
        raise LogFormatError(msg[:200])
    total, cached, prefill_s, new = prompt.groups()
    cached = int(cached or 0)
    rec = {"model": HALOGEN_MODEL, "mode": head.group(1), "completion": int(head.group(2)),
           "decode_s": float(head.group(3)), "prompt": int(total), "cached": cached,
           "prefill_s": float(prefill_s), "prefill_tokens": int(new) if new else int(total) - cached}
    for part in parts[1:]:
        if m := H_ROUNDS.match(part):
            rec["spec_rounds"] = int(m.group(1))
        elif m := H_POOL.match(part):
            rec["pool_used"], rec["pool_total"] = int(m.group(1)), int(m.group(2))
        elif m := H_THINK_CLOSED.match(part):
            rec["think_closed_at"] = int(m.group(1))
            rec["think_close"] = m.group(2).replace(" ", "_")
        elif m := H_CLOSEST.match(part):
            rec["closest_shared_tokens"], rec["closest_age_s"] = int(m.group(1)), float(m.group(2))
            rec["closest_outcome"] = H_CLOSEST_OUTCOME[m.group(3)]
    return "completion", rec


# Gufo ---------------------------------------------------------------------
G_LINE = re.compile(r"^\d{4}-\d\d-\d\d \d\d:\d\d:\d\d \[[A-Z]+\] \[(\w+)\] (.*)$")
G_KV = re.compile(r"(\w+)=(\S*)")
G_MODEL = re.compile(r"\bmodel=(.+?) sessions=")
CHAT_PATHS = ("/v1/chat/completions", "/v1/completions")


def slug(name: str) -> str:
    return re.sub(r"\s+", "-", name.strip().lower())


def parse_gufo(msg: str) -> tuple[str, dict] | None:
    """('completion' | 'received' | 'model', record) or None."""
    m = G_LINE.match(msg)
    if not m:
        return None
    component, rest = m.groups()
    kv = dict(G_KV.findall(rest))
    if component == "loader" and kv.get("event") == "load_completed":
        model = G_MODEL.search(rest)
        return ("model", {"model": slug(model.group(1))}) if model else None
    if component != "http" or kv.get("method") != "POST" or kv.get("path") not in CHAT_PATHS:
        return None
    req, event = kv.get("request"), kv.get("event")
    if event == "received":
        return "received", {"req": req}
    if event != "completed":
        return None
    if kv.get("status") != "200" or kv.get("outcome", "completed") != "completed":
        return "completion", {"req": req, "failed": True, "status": kv.get("status"),
                              "outcome": kv.get("outcome")}
    try:
        return "completion", _gufo_record(kv)
    except (KeyError, ValueError) as e:
        raise LogFormatError(f"{e!r}: {msg[:200]}") from None


def _gufo_record(kv: dict) -> dict:
    gen, prompt = int(kv["generated_tokens"]), int(kv["prompt_tokens"])
    prefill = int(kv.get("prefill_tokens", prompt))
    decode_tps, prefill_tps = float(kv.get("decode_tps", 0)), float(kv.get("prefill_tps", 0))
    rec = {"req": kv.get("request"), "failed": False, "completion": gen, "prompt": prompt,
           "prefill_tokens": prefill, "cached": int(kv.get("cached_tokens", 0)),
           # Gufo computes these rates from its own durations, so dividing back is exact.
           "decode_s": gen / decode_tps if decode_tps > 0 else 0.0,
           "prefill_s": prefill / prefill_tps if prefill_tps > 0 else 0.0,
           "finish": kv.get("finish")}
    if "ttft_ms" in kv:
        rec["ttft_s"] = float(kv["ttft_ms"]) / 1000
    if "queue_ms" in kv:
        rec["queue_s"] = float(kv["queue_ms"]) / 1000
    for key in ("draft_accepted", "draft_proposed"):
        if key in kv:
            rec[key] = int(kv[key])
    # Present with the answer-room patch (feat/answer-room); absent in 0.3.0.
    if kv.get("think_close"):
        rec["think_close"] = kv["think_close"]
        if kv.get("think_closed_at", "").isdigit():
            rec["think_closed_at"] = int(kv["think_closed_at"])
    return rec


# Crucible ------------------------------------------------------------------
# The server (python -m server) prints one JSON object per finished turn and per prefill chunk to
# stderr. Values it does not know are null, never estimates.
C_LINE = re.compile(r"^(crucible_turn|crucible_prefill|crucible_decode|crucible_error) (.*)$")
# The server's own id for a request. Anything else is dropped rather than stored.
C_REQUEST_ID = re.compile(r"^[A-Za-z0-9_.:-]{1,64}$")
C_FALLBACK_MARK = "tool_call_fallback"  # the one fixed `error` text that names a kind
C_TURN_NUMS = {"queue_s": "queue_s", "prefill_s": "prefill_s", "decode_s": "decode_s", "ttft_s": "ttft_s"}
# Batch-serve cache report (Crucible d866c78); older servers omit them.
C_CACHE_INTS = ("cache_saved_tokens", "cache_tail_tokens", "cache_entries", "cache_bytes", "cache_evictions",
                "host_free_mib", "slot_context", "cache_pinned_bytes")
# Crucible's fixed refusal reasons (engine/cache_status.h; train 14 added protected and resident). The
# server prints only lowercase identifiers; one Kiln does not know yet is stored as "other", so a new
# reason never costs the turn's metrics.
# Decode counts (Crucible wip/turn-stats): rounds and accepted drafts of the batch engine (proposed drafts
# later, only with spec > 0), and for a think turn the server closed, the tokens the engine generated and the
# close text the server wrote at once (both parts of completion_tokens).
C_DECODE_INTS = ("spec_rounds", "draft_accepted", "draft_proposed")
C_SPLIT_INTS = ("generated_tokens", "forced_tokens")
# Crucible's per-position draft counters (engine design note, final names): what the engine proposed, what the
# target tested and what it accepted at each depth, the rounds by draft length, and the head passes with their
# wall time. A request without speculation leaves every one of them out, so all six are optional.
C_DRAFT_POS = ("draft_pos_proposed", "draft_pos_tested", "draft_pos_accepted")
C_DRAFT_POS_LEN = 7  # K = 7 draft positions
C_DRAFT_LEN_HIST = "draft_len_hist"
C_DRAFT_LEN_COUNT = 8  # a draft length is 0..K
# The engine's done event may also carry these per-token counts with token ids. Kiln reads a turn line key by
# key, so they are never read and never stored, in any column and in the raw line; a test holds that.
C_DRAFT_DETAIL = ("draft_tally", "draft_miss", "draft_detail_cut")
C_REFUSALS = ("budget", "host", "protected", "resident", "policy", "memory")
C_REFUSAL_ID = re.compile(r"[a-z]{1,32}")


def _c_int(d: dict, key: str, required: bool = False) -> int | None:
    v = d.get(key)
    if isinstance(v, int) and not isinstance(v, bool) and v >= 0:
        return v
    if v is None and not required:
        return None
    raise LogFormatError(f"crucible: {key}={v!r}")


def _c_num(d: dict, key: str) -> float | None:
    v = d.get(key)
    if v is None:
        return None
    if isinstance(v, (int, float)) and not isinstance(v, bool) and v >= 0:
        return float(v)
    raise LogFormatError(f"crucible: {key}={v!r}")


def _c_int_list(d: dict, key: str, size: int) -> list[int] | None:
    """A fixed-length list of non-negative integers, or None when the field is absent.

    The engine promises the length and the entry type; anything else drops this one field and keeps the turn,
    because these counters come from a new server build and one odd list must not cost the request's other
    metrics. The value is not logged: a turn line can be long and is not Kiln's to keep.
    """
    v = d.get(key)
    if v is None:
        return None
    if (not isinstance(v, list) or len(v) != size
            or not all(isinstance(x, int) and not isinstance(x, bool) and x >= 0 for x in v)):
        log.debug("crucible: dropped %s: not %d non-negative integers", key, size)
        return None
    return list(v)


def _c_draft(d: dict, rec: dict) -> None:
    """Crucible's per-position draft counters onto the turn record, each check dropping only its own field.

    The engine promises sum(proposed) == draft_proposed, sum(accepted) == draft_accepted,
    accepted_i <= tested_i <= proposed_i, tested_1 == proposed_1 and sum(len_hist) <= spec_rounds. A scalar
    the line does not carry cannot be checked against, and the list then stays.
    """
    proposed = _c_int_list(d, C_DRAFT_POS[0], C_DRAFT_POS_LEN)
    tested = _c_int_list(d, C_DRAFT_POS[1], C_DRAFT_POS_LEN)
    accepted = _c_int_list(d, C_DRAFT_POS[2], C_DRAFT_POS_LEN)
    hist = _c_int_list(d, C_DRAFT_LEN_HIST, C_DRAFT_LEN_COUNT)
    if proposed is not None and rec.get("draft_proposed") is not None and sum(proposed) != rec["draft_proposed"]:
        log.debug("crucible: dropped %s: its sum is not draft_proposed", C_DRAFT_POS[0])
        proposed = None
    if tested is not None and proposed is not None:
        if any(t > p for p, t in zip(proposed, tested)):
            log.debug("crucible: dropped %s: a position was tested more often than proposed", C_DRAFT_POS[1])
            tested = None
        elif tested[0] != proposed[0]:
            log.debug("crucible: dropped %s: position 1 tested is not position 1 proposed", C_DRAFT_POS[1])
            tested = None
    if accepted is not None and rec.get("draft_accepted") is not None and sum(accepted) != rec["draft_accepted"]:
        log.debug("crucible: dropped %s: its sum is not draft_accepted", C_DRAFT_POS[2])
        accepted = None
    # A draft can only be accepted where it was tested, so a dropped tested list is checked against proposed.
    against = tested if tested is not None else proposed
    if accepted is not None and against is not None and any(a > b for b, a in zip(against, accepted)):
        log.debug("crucible: dropped %s: a position was accepted more often than it was tested", C_DRAFT_POS[2])
        accepted = None
    if hist is not None and rec.get("spec_rounds") is not None and sum(hist) > rec["spec_rounds"]:
        log.debug("crucible: dropped %s: its sum is over spec_rounds", C_DRAFT_LEN_HIST)
        hist = None
    for key, value in ((C_DRAFT_POS[0], proposed), (C_DRAFT_POS[1], tested), (C_DRAFT_POS[2], accepted),
                       (C_DRAFT_LEN_HIST, hist)):
        if value is not None:
            rec[key] = value
    passes = d.get("draft_passes")
    if passes is not None:
        if isinstance(passes, int) and not isinstance(passes, bool) and passes >= 0:
            rec["draft_passes"] = passes
        else:
            log.debug("crucible: dropped draft_passes: not a non-negative integer")
    ms = d.get("draft_ms")
    if ms is not None:
        if isinstance(ms, (int, float)) and not isinstance(ms, bool) and math.isfinite(ms) and ms >= 0:
            rec["draft_ms"] = float(ms)
        else:
            log.debug("crucible: dropped draft_ms: not a finite number >= 0")


def _c_error(d: dict) -> dict:
    """Counts, flags and a kind for a crucible_error line. Its `error` text is read for one fixed marker and
    never returned: it is the server's message and could echo client input."""
    status, streamed = d.get("status"), d.get("streamed")
    if not isinstance(status, int) or isinstance(status, bool) or not 100 <= status <= 599:
        raise LogFormatError(f"crucible: error status={status!r}")
    if not isinstance(streamed, bool):
        raise LogFormatError(f"crucible: error streamed={streamed!r}")
    req = d.get("request_id")
    if isinstance(req, int) and not isinstance(req, bool) and req >= 0:
        req = str(req)  # the batch server's ids are integers
    if d.get("error") == C_FALLBACK_MARK and status == 200:
        kind = "tool_call_fallback"
    elif streamed:
        kind = "stream_error"
    else:
        kind = "server_error" if status >= 500 else "client_error"
    return {"req": req if isinstance(req, str) and C_REQUEST_ID.fullmatch(req) else None, "status": status,
            "streamed": streamed, "kind": kind, "completion_tokens": _c_int(d, "completion_tokens")}


def _c_decode(d: dict) -> tuple[str, dict] | None:
    """A decoding request's cumulative draft counts (crucible_decode, at most once a second), or None when it
    accepted more drafts than it proposed. Live only: the turn's crucible_turn line has the final counts.
    As in a turn line, an absent or null count is unknown (an older engine, an early step), not an error."""
    counts = {key: _c_int(d, key) for key in C_DECODE_INTS}
    accepted, proposed = counts["draft_accepted"], counts["draft_proposed"]
    if accepted is not None and proposed is not None and accepted > proposed:
        return None
    return "decode", {"req": d.get("request_id"), "tokens": _c_int(d, "completion_tokens"), **counts,
                      "elapsed_s": _c_num(d, "elapsed_s")}


def parse_crucible(msg: str) -> tuple[str, dict] | None:
    """('completion' | 'progress' | 'decode' | 'error', record) for Crucible's per-turn, per-chunk, per-second
    decode and error lines, else None."""
    m = C_LINE.match(msg)
    if not m:
        return None
    try:
        d = json.loads(m.group(2))
    except ValueError:
        raise LogFormatError(msg[:200]) from None
    if not isinstance(d, dict):
        raise LogFormatError(msg[:200])
    if m.group(1) == "crucible_error":
        return "error", _c_error(d)
    if m.group(1) == "crucible_decode":
        return _c_decode(d)
    if m.group(1) == "crucible_prefill":
        done, total = _c_int(d, "done", True), _c_int(d, "total")
        # turn_request_id marks the job that continues a think turn the server closed (train 23): no gateway request.
        turn = d.get("turn_request_id")
        return "progress", {"req": d.get("request_id"), "phase": "prefill", "tokens": done,
                            **({"total": total} if total else {}), "elapsed_s": _c_num(d, "elapsed_s") or 0.0,
                            **({"turn_req": turn} if turn is not None else {})}
    if "error" in d and "prompt_tokens" not in d:  # the server could not log this turn's metrics
        return None
    prompt, cached = _c_int(d, "prompt_tokens", True), _c_int(d, "cached_tokens") or 0
    prefill = _c_int(d, "prefill_tokens")
    rec = {"req": d.get("request_id"), "failed": False, "completion": _c_int(d, "completion_tokens", True),
           "prompt": prompt, "cached": cached,
           "prefill_tokens": prefill if prefill is not None else max(0, prompt - cached),
           "finish": d.get("finish") if isinstance(d.get("finish"), str) else None}
    for key, col in C_TURN_NUMS.items():
        v = _c_num(d, key)
        if v is not None:
            rec[col] = v
    if isinstance(d.get("session_label"), str):
        rec["session_label"] = d["session_label"]
    for key in C_CACHE_INTS + C_DECODE_INTS:
        if d.get(key) is not None:
            rec[key] = _c_int(d, key)
    split = {key: _c_int(d, key) for key in C_SPLIT_INTS if d.get(key) is not None}
    # The server writes them only when 0 <= generated <= completion; anything else is left out, not the turn.
    if split.get("generated_tokens") is not None and split["generated_tokens"] <= rec["completion"]:
        rec.update(split)
    _c_draft(d, rec)
    refused = d.get("cache_save_refused")
    if refused is not None:
        if not isinstance(refused, str) or not C_REFUSAL_ID.fullmatch(refused):
            raise LogFormatError(f"crucible: cache_save_refused={refused!r}")
        rec["cache_save_refused"] = refused if refused in C_REFUSALS else "other"
    if isinstance(d.get("replay_hit"), bool):
        rec["replay_hit"] = d["replay_hit"]
    return "completion", rec


PARSERS = {"halogen": parse_halogen, "gufo": parse_gufo, "crucible": parse_crucible}


def detect_engine(msg: str) -> str | None:
    if msg.startswith(("serve_api: ", "flash_serve: ", "kv pool: ", "prompt cache: ")):
        return "halogen"
    if msg.startswith(("crucible_turn ", "crucible_prefill ", "crucible_decode ", "crucible_error ")):
        return "crucible"
    return "gufo" if G_LINE.match(msg) else None


def parse(msg: str, engine: str | None = None) -> tuple[str, str, dict] | None:
    """(engine, kind, record) for a request-related line, else None. May raise LogFormatError."""
    engine = engine or detect_engine(msg)
    parser = PARSERS.get(engine or "")
    if parser is None:
        return None
    out = parser(msg)
    return (engine, *out) if out else None
