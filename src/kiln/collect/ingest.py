"""Turn gateway records and engine log lines into stored requests (ARCHITECTURE.md, Storage).

Two sources describe the same request: the gateway's record (who, when, exact
streamed tokens, usage) and the engine's completion line (prefill time and
computed tokens, speculation, KV pool, thinking cut-off). They arrive in
either order, about a second apart, so both are written to SQLite at once
and joined there, which survives restarts:

- A gateway record becomes a `gateway` row with its decode tokens in
  series_1s by arrival second. Once its engine line joins, they are respread
  over the engine's decode time (spread.py explains why).
- An engine line joins an unjoined gateway row of the same container with the
  same completion count that ended within JOIN_WINDOW_S. If none exists yet,
  it waits as an `engine_pending` row, which a later gateway record absorbs.
- settle() finishes what is left once both sources have been read past the
  request's end plus SETTLE_S: gateway rows that never got an engine line
  get prefill from usage and gateway timing; pending engine lines become
  `direct` requests (traffic that bypassed the gateway).

Every method returns notes for the live view: ("request", id),
("progress", profile, done, total, elapsed_s, ts, step, engine request number or None),
("continuation", profile, engine request number), ("decode_drafts", profile, engine request number, proposed,
accepted, ts, completion tokens, seconds since the first token), ("kv", profile, used, total, ts) and
("exactness", profile, completion, events).
"""
from __future__ import annotations

import json
import sqlite3
import time

from kiln.collect import engines, memory
from kiln.config import Config
from kiln.gateway.prefix import cache_math
from kiln.gateway.stream import ended_whole, is_generation
from kiln.collect import spread
from kiln.collect.spread import scale_counts  # noqa: F401 (kept importable from here)
from kiln.pricing.cards import PriceBook
from kiln.store import queries

JOIN_WINDOW_S = 5.0
SETTLE_S = 8.0
CONTAINERS_KEY = "containers"  # ingest_offsets row holding what each container serves
# Engine progress events wait in prefill_chunks until the request's engine line joins them to its row.
# A request's engine line comes at the end of its decode, which can take 45 minutes, so they are pruned
# only well past that; the ones attached to a row are deleted at once.
CHUNK_KEEP_S = 12 * 3600.0
# Chunks of another request are looked up this far around a request's own prefill window: an interval
# estimate can reach back before the request started, never far.
CHUNK_LOOKUP_S = 5.0

# A cached follow-up prefills a few hundred tokens at a rate that says nothing about a cold
# prompt, so only prefills at least this large set the live progress estimate's starting rate.
PREFILL_HINT_MIN_TOKENS = 4096
# Engines whose usage reports tool_call_fallback. For the others the flag is not known (NULL), not "no".
FALLBACK_FLAG_ENGINES = ("crucible",)

ENGINE_COLS = {"prefill_tokens": "engine_prefill_tokens", "prefill_s": "engine_prefill_s",
               "decode_s": "engine_decode_s", "queue_s": "engine_queue_s", "spec_rounds": "spec_rounds",
               "draft_proposed": "draft_proposed", "draft_accepted": "draft_accepted",
               "think_close": "think_close", "pool_used": "kv_pool_used", "pool_total": "kv_pool_total",
               "session_label": "cache_session_label", "cache_saved_tokens": "cache_saved_tokens",
               "cache_tail_tokens": "cache_tail_tokens", "cache_save_refused": "cache_save_refused",
               "cache_entries": "cache_entries", "cache_bytes": "cache_bytes", "cache_evictions": "cache_evictions",
               "host_free_mib": "host_free_mib", "slot_context": "slot_context", "replay_hit": "replay_hit",
               "closest_shared_tokens": "closest_shared_tokens", "closest_age_s": "closest_age_s",
               "closest_outcome": "closest_outcome", "generated_tokens": "engine_generated_tokens",
               "forced_tokens": "engine_forced_tokens", "draft_pos_proposed": "draft_pos_proposed",
               "draft_pos_tested": "draft_pos_tested", "draft_pos_accepted": "draft_pos_accepted",
               "draft_len_hist": "draft_len_hist", "draft_passes": "draft_passes", "draft_ms": "draft_ms"}
# Gateway prefix fields (gateway/prefix.py): counts, booleans and a random conversation id only.
PREFIX_INTS = ("n_messages", "prefix_msgs_kept", "prev_n_messages", "prev_prompt", "prev_completion")
PREFIX_BOOLS = ("system_changed", "tools_changed", "new_conversation")
# A turn that read this many unchanged tokens again is flagged as an engine cache miss.
REREAD_FLAG_TOKENS = 4096


SQLITE_INT = (-(2 ** 63), 2 ** 63)  # a larger int raised OverflowError at the bind and lost the row (M3)
# A larger record is stored with a small raw copy: only the numbers that readers use (spread.py reads
# tokens_by_s; held_s), never the rest, which holds client data (delta review L1).
RAW_MAX = 64 * 1024
RAW_KEEP_MAX = 1 << 20  # tokens_by_s of a 24 h stream at 99 events a second is about 350 KB


def _raw_copy(rec: dict) -> str | None:
    """The record as the raw column keeps it: whole up to RAW_MAX, else only its numbers."""
    raw = json.dumps(rec)
    if len(raw) <= RAW_MAX:
        return raw
    small = {}
    hist = rec.get("tokens_by_s")
    if isinstance(hist, list) and len(hist) == 2 and _int(hist[0]) is not None and isinstance(hist[1], list) \
            and all(_int(n) is not None for n in hist[1]):
        small["tokens_by_s"] = hist
    if _num(rec.get("held_s")) is not None:
        small["held_s"] = rec["held_s"]
    raw = json.dumps(small)
    return raw if small and len(raw) <= RAW_KEEP_MAX else None


def _int(v):
    return v if isinstance(v, int) and not isinstance(v, bool) and SQLITE_INT[0] <= v < SQLITE_INT[1] else None


TEXT_MAX = 200


def _text(v, limit: int = TEXT_MAX) -> str | None:
    """A text column's value. Records come from the network (the gateway keeps what a client sent on some
    paths), and one dict or list bound to SQLite failed the whole batch on every retry (security review H2)."""
    return v[:limit] if isinstance(v, str) else None


def _row_get(row, key):
    """A dict's or an sqlite3.Row's value, None when neither holds the key."""
    try:
        return row[key]
    except (KeyError, IndexError):
        return None


def engine_number(req) -> int | None:
    """An engine's own request number (Crucible: int, Halogen: digits), for pairing progress with streams.

    It is not the engine's whole id: see engine_key for matching its progress lines to a request row.
    """
    if isinstance(req, str) and req.isascii() and req.isdecimal() and len(req) <= 18:  # "²" is a digit too
        return int(req)
    return _int(req)


def engine_key(req) -> str | None:
    """An engine's own request id as text, for matching its progress lines to the row they belong to.

    Crucible numbers its requests and Halogen uses digits; an engine may use the gateway's id instead.
    """
    if isinstance(req, bool) or req is None:
        return None
    if isinstance(req, int):
        return str(req)
    return req if isinstance(req, str) and 0 < len(req) <= 64 else None


def _fallback_flag(engine, usage: dict) -> int | None:
    if engine not in FALLBACK_FLAG_ENGINES:
        return None
    return int(usage.get("tool_call_fallback") is True)


def _num(v):
    return v if isinstance(v, float) else _int(v)


def _engine_cols(rec: dict) -> dict:
    """One engine line's figures as columns. The per-position draft lists are stored as compact JSON text
    (migration 013): the queries sum them per position in Python, and no row needs one column per depth."""
    return {col: json.dumps(v, separators=(",", ":")) if isinstance(v, list) else v
            for key, col in ENGINE_COLS.items() if (v := rec.get(key)) is not None}


def _prefix_cols(p) -> dict:
    p = p if isinstance(p, dict) else {}
    conv = p.get("conv")
    return {"conv": conv if isinstance(conv, str) and conv.isalnum() and len(conv) <= 32 else None,
            **{k: _int(p.get(k)) for k in PREFIX_INTS},
            **{k: int(p[k]) if isinstance(p.get(k), bool) else None for k in PREFIX_BOOLS}}


# The engines listen on loopback, so a request that bypassed the gateway came from this host: its device is
# the host, keyed like the gateway's own local requests, and its container (or the health check) is its label.
LOCAL_DEVICE = "127.0.0.1"
PROBE_LABEL = "kiln-probe"


class Ingestor:
    def __init__(self, conn: sqlite3.Connection, cfg: Config, book: PriceBook, clock=time.time):
        self.conn, self.cfg, self.book, self.clock = conn, cfg, book, clock
        self.by_container = {p.container_name: p for p in cfg.profiles.values()}
        row = conn.execute("SELECT position FROM ingest_offsets WHERE source=?", (CONTAINERS_KEY,)).fetchone()
        self.containers: dict[str, dict] = json.loads(row[0]) if row else {}
        # Each source's watermark: everything it will ever deliver up to this time has been read.
        self.watermarks = {"journal": 0.0, "gateway": 0.0}
        # (container, ts) -> True for the engine manager's own health-check generations
        self.is_probe = lambda container, ts: False
        self.skipped_prefill = 0  # requests whose engine figures put their prefill above any real rate

    # -- helpers -----------------------------------------------------------
    def _series(self, pairs, kind: str, profile: str, model: str, client: str) -> None:
        spread.add_series(self.conn, self.cfg.host, kind, pairs, profile, model, client)

    def _row(self, rid: str) -> sqlite3.Row | None:
        return self.conn.execute("SELECT * FROM requests WHERE id = ?", (rid,)).fetchone()

    def _client(self, key: str, name: str | None, node: str | None, now: float) -> None:
        cur = self.conn.execute(
            "INSERT INTO clients (key, name, node, updated) VALUES (?, ?, ?, ?) ON CONFLICT (key) DO UPDATE SET"
            " name = COALESCE(excluded.name, name), node = COALESCE(excluded.node, node),"
            " updated = MAX(updated, excluded.updated) RETURNING color", (key, name, node, now))
        if cur.fetchone()[0] is None:
            queries.assign_colors(self.conn)

    def _container_meta(self, container: str, engine: str) -> dict:
        prof = self.by_container.get(container)
        if prof:
            return {"profile": prof.name, "engine": prof.engine, "model": prof.model}
        model = self.containers.get(container, {}).get("model")
        if model is None and engine == "halogen":
            model = engines.HALOGEN_MODEL
        return {"profile": None, "engine": engine, "model": model}

    def _learn(self, container: str, **kv) -> None:
        self.containers[container] = {**self.containers.get(container, {}), **kv}
        self.conn.execute("INSERT INTO ingest_offsets (source, position) VALUES (?, ?) ON CONFLICT (source)"
                          " DO UPDATE SET position = excluded.position",
                          (CONTAINERS_KEY, json.dumps(self.containers)))

    # -- gateway records -----------------------------------------------------
    def gateway_record(self, rec: dict) -> list[tuple]:
        if not isinstance(rec, dict) or not _text(rec.get("id"), 128) or _num(rec.get("ts_start")) is None:
            return []
        usage = rec.get("usage") if isinstance(rec.get("usage"), dict) else {}
        client = rec.get("client") if isinstance(rec.get("client"), dict) else {}
        client = {k: _text(client.get(k)) for k in ("key", "name", "label", "node", "peer_ip")}
        path, finish = _text(rec.get("path")), _text(rec.get("finish"))
        status, error = _int(rec.get("status")), _text(rec.get("error"))
        if status == 499 and error == "client_disconnected" and ended_whole(finish, usage):
            status, error = 200, None  # a gateway before ended_whole recorded these as cancels
        profile = _text(rec.get("profile"))
        prof = self.cfg.profiles.get(profile or "")
        # Older records keyed labelled requests by the label; the device is the key now.
        key = client["peer_ip"] or client["key"] or "unknown"
        device = self.cfg.device_for(key)  # one machine, several addresses: one key and its configured name
        row = {"id": rec["id"], "source": "gateway" if is_generation(path) else "utility",
               "host": self.cfg.host,
               "container": prof.container_name if prof else None,
               "ts_start": rec["ts_start"], "ts_first_token": _num(rec.get("ts_first_token")),
               "ts_last_token": _num(rec.get("ts_last_token")), "ts_end": _num(rec.get("ts_end")),
               "client_key": device.key if device else key,
               "client_name": device.name if device else client["name"],
               "client_label": client["label"], "client_node": client["node"],
               "peer_ip": client["peer_ip"], "profile": profile, "engine": _text(rec.get("engine")),
               "model": _text(rec.get("model")), "path": path, "status": status,
               "finish": finish, "error": error,
               "prompt": _int(usage.get("prompt")), "cached": _int(usage.get("cached")),
               "completion": _int(usage.get("completion")), "reasoning": _int(usage.get("reasoning")),
               "think_close": _text(usage.get("think_close")),
               "tool_call_fallback": _fallback_flag(_text(rec.get("engine")), usage),
               "token_events": _int(rec.get("token_events")), "ttft_s": _num(rec.get("ttft_s")),
               "latency_s": _num(rec.get("latency_s")), "max_tokens": _int(rec.get("max_tokens")),
               "reasoning_effort": _text(rec.get("reasoning_effort")), **_prefix_cols(rec.get("prefix")),
               "raw": _raw_copy(rec)}
        cols = ", ".join(row)
        cur = self.conn.execute(f"INSERT OR IGNORE INTO requests ({cols}) VALUES ({', '.join('?' * len(row))})",
                                tuple(row.values()))
        if cur.rowcount == 0:
            return []  # already ingested (the live feed and the JSONL file both deliver records)
        self._client(row["client_key"], row["client_name"], row["client_node"], row["ts_start"])
        if row["source"] == "utility":
            return []  # no tokens to count or engine line to join
        notes = []
        self._cache_math(row["id"])
        stored = self._row(row["id"])
        if spread.streamed(stored):
            # Arrival seconds for now; an engine line joined below or later replaces them.
            pairs = spread.decode_pairs(stored)
            self._series(pairs, "decode", row["profile"] or "?", row["model"], row["client_key"])
        if row["profile"] and row["completion"] is not None and row["token_events"] is not None:
            notes.append(("exactness", row["profile"], row["completion"], row["token_events"]))
        pending = self._match_pending(row)
        if pending:
            notes += self._join(row["id"], pending, pending["id"])
            self.conn.execute("DELETE FROM requests WHERE id = ?", (pending["id"],))
        self.book.price_request(row["id"])
        notes.append(("request", row["id"]))
        return notes

    def _match_pending(self, g: dict) -> sqlite3.Row | None:
        """The waiting engine line for a new gateway row, if it already arrived."""
        if g["container"] is None or g["ts_end"] is None:
            return None
        if g["completion"] is not None:
            cond, arg = "completion = ?", g["completion"]
        elif g["token_events"] is not None:  # cancelled: no usage report, the streamed count is close
            cond, arg = "ABS(completion - ?) <= 2", g["token_events"]
        else:
            return None
        return self.conn.execute(
            f"SELECT * FROM requests WHERE source = 'engine_pending' AND container = ? AND {cond}"
            # Known prompt sizes must agree: a near miss is a different request, not a match.
            " AND (prompt IS NULL OR ? IS NULL OR prompt = ?)"
            " AND ts_end BETWEEN ? AND ? ORDER BY ABS(ts_end - ?) LIMIT 1",
            (g["container"], arg, g["prompt"], g["prompt"], g["ts_end"] - JOIN_WINDOW_S,
             g["ts_end"] + JOIN_WINDOW_S, g["ts_end"])).fetchone()

    def _match_gateway(self, container: str, rec: dict, ts: float) -> sqlite3.Row | None:
        """The unjoined gateway row an engine completion line belongs to."""
        return self.conn.execute(
            "SELECT * FROM requests WHERE source = 'gateway' AND container = ? AND engine_joined_at IS NULL"
            " AND ts_end BETWEEN ? AND ?"
            " AND (completion = ? OR (completion IS NULL AND ABS(token_events - ?) <= 2))"
            " AND (prompt IS NULL OR ? IS NULL OR prompt = ?)"
            " ORDER BY (completion IS NOT ?), ABS(ts_end - ?) LIMIT 1",
            (container, ts - JOIN_WINDOW_S, ts + JOIN_WINDOW_S, rec["completion"], rec["completion"],
             rec.get("prompt"), rec.get("prompt"), rec["completion"], ts)).fetchone()

    def _join(self, gateway_id: str, eng: sqlite3.Row | dict, line_id: str) -> list[tuple]:
        """Copy an engine line's figures onto a gateway row, respread its decode, attribute its prefill.
        The history (series_1s) changes; the live chart's seconds are final once drawn (live/rates.py)."""
        before = self._row(gateway_id)
        # An engine figure that is missing must not erase one the gateway already has.
        sets = {col: v for col in ENGINE_COLS.values() if (v := _row_get(eng, col)) is not None}
        req = _row_get(eng, "req")  # the log line's own field, or a pending row's already-parsed one
        req = _row_get(eng, "engine_req") if req is None else req
        if req is not None and (key := engine_key(req)) is not None:
            # A replayed line, or a pending row from before the column, may have no usable id.
            sets["engine_req"] = key
        sets["engine_joined_at"] = self.clock()
        sets["engine_line_id"] = line_id
        self.conn.execute(f"UPDATE requests SET {', '.join(f'{k} = ?' for k in sets)},"
                          " cached = COALESCE(cached, ?) WHERE id = ?",
                          (*sets.values(), eng["cached"], gateway_id))
        self._cache_math(gateway_id)  # the engine may have supplied the cached count
        after = self._row(gateway_id)
        if spread.streamed(after):  # its arrival seconds are already in series_1s
            moved = spread.diff_pairs(spread.decode_pairs(before), spread.decode_pairs(after))
            self._series(moved, "decode", after["profile"] or "?", after["model"], after["client_key"])
        return self._attribute_prefill(gateway_id)

    def _cache_math(self, rid: str) -> None:
        r = self._row(rid)
        if r is None or r["prefix_msgs_kept"] is None:
            return
        appended, reread = cache_math(r["prompt"], r["cached"], r["prefix_msgs_kept"], r["prev_n_messages"],
                                      r["prev_prompt"], r["prev_completion"])
        self.conn.execute("UPDATE requests SET appended_tokens = ?, reread_tokens = ? WHERE id = ?",
                          (appended, reread, rid))

    def _attribute_prefill(self, rid: str) -> list[tuple]:
        """Put a finished gateway request's prefill (and, if it wasn't streamed, its output) in the history."""
        r = self._row(rid)
        if r is None or r["series_done"] or r["ts_end"] is None:
            return []
        self.conn.execute("UPDATE requests SET series_done = 1 WHERE id = ?", (rid,))
        r, notes = self._with_progress(r)
        pairs = spread.prefill_pairs(r, self.cfg.max_prefill_tps, self._engine_blocks(r))
        self._series(pairs, "prefill", r["profile"] or "?", r["model"], r["client_key"])
        if not spread.streamed(r):
            out = spread.decode_pairs(r)
            self._series(out, "decode", r["profile"] or "?", r["model"], r["client_key"])
        return notes

    # -- prefill chunks ------------------------------------------------------
    def _store_chunk(self, container: str, rec: dict, ts: float) -> None:
        """Keep one engine prefill report until the request's engine line joins it to a row."""
        req = engine_key(rec.get("req"))
        if req is None or not container:
            return
        self.conn.execute("INSERT OR REPLACE INTO prefill_chunks (container, req, ts, elapsed_s, done, total)"
                          " VALUES (?, ?, ?, ?, ?, ?)",
                          (container, req, ts, rec["elapsed_s"], rec["tokens"], rec.get("total")))

    def _attach_progress(self, r: sqlite3.Row) -> sqlite3.Row:
        """Move the engine's reports for this request onto its row, as [end, seconds, tokens] each.

        The row is then enough to rebuild its prefill (spread.prefill_chunks), and the engine's chunk
        windows stay available to the other requests of that engine (spread.chunk_pairs).
        """
        if r["container"] is None or spread.stored_chunks(r):
            return r
        events = self._progress_events(r)
        if not events:
            return r
        chunks = spread.chunks_from_reports([(ts, elapsed, done) for ts, elapsed, done, _ in events], r["cached"],
                                           r["engine_prefill_tokens"])
        if not chunks:
            return r
        self.conn.execute("UPDATE requests SET prefill_progress = ? WHERE id = ?",
                          (json.dumps([[round(c.end, 3), round(c.end - c.start, 3), c.tokens] for c in chunks]),
                           r["id"]))
        for key in {e[3] for e in events}:
            self.conn.execute("DELETE FROM prefill_chunks WHERE container = ? AND req = ?", (r["container"], key))
        return self._row(r["id"])

    def _progress_events(self, r: sqlite3.Row) -> list[tuple]:
        """This request's engine progress events as (ts, elapsed_s, done, engine request id).

        The engine's request number matches the one on its completion line (Crucible puts it there).
        Halogen numbers its progress lines but not its completion line, so its reports are taken from
        the request's own prefill window and must name its prompt size to be trusted; several engine
        request numbers in that window mean the reports cannot be told apart, and none are used.
        """
        lo, hi = r["ts_start"] - CHUNK_LOOKUP_S, (r["ts_first_token"] or r["ts_end"]) + CHUNK_LOOKUP_S
        cols = "ts, elapsed_s, done, req"
        if (key := engine_key(r["engine_req"])) is not None:
            rows = self.conn.execute(f"SELECT {cols} FROM prefill_chunks WHERE container = ? AND req = ?"
                                     " AND ts BETWEEN ? AND ? ORDER BY ts",
                                     (r["container"], key, lo, hi)).fetchall()
            return [(x[0], x[1], x[2], x[3]) for x in rows]
        if r["prompt"] is None:
            return []
        rows = self.conn.execute(f"SELECT {cols} FROM prefill_chunks WHERE container = ? AND ts BETWEEN ? AND ?"
                                 " AND total = ? ORDER BY ts", (r["container"], lo, hi, r["prompt"])).fetchall()
        events = [(x[0], x[1], x[2], x[3]) for x in rows]
        return events if len({e[3] for e in events}) <= 1 else []

    def _engine_blocks(self, r: sqlite3.Row) -> spread.Blocks:
        """The prefill windows of the other requests on this request's engine (spread.chunk_pairs).

        Requests that finished earlier keep theirs on their row; the ones still running have only their
        reports so far, in prefill_chunks. A request's reports are pruned once its row holds them.
        """
        container = r["container"]
        if container is None or r["ts_end"] is None:
            return spread.Blocks()
        lo, hi = r["ts_start"], r["ts_first_token"] or r["ts_end"]
        cols = ("id, source, ts_start, ts_first_token, ts_end, cached, prompt, engine_prefill_tokens,"
                " engine_prefill_s, engine_decode_s, prefill_progress")
        chunks = []
        for row in self.conn.execute(f"SELECT {cols} FROM requests WHERE container = ? AND id <> ?"
                                     " AND prefill_progress IS NOT NULL AND ts_end >= ? AND ts_start <= ?",
                                     (container, r["id"], lo - CHUNK_LOOKUP_S, hi + CHUNK_LOOKUP_S)):
            chunks += spread.prefill_chunks(row, self.cfg.max_prefill_tps)
        for ts, elapsed, done, req in self.conn.execute(
                "SELECT ts, elapsed_s, done, req FROM prefill_chunks WHERE container = ? AND ts BETWEEN ? AND ?",
                (container, lo - CHUNK_LOOKUP_S, hi + CHUNK_LOOKUP_S)):
            # Only the interval matters for another request's trim, so the token counts can be rough here.
            chunks += spread.chunks_from_reports([(ts, elapsed, done)], None)
        return spread.block_index(chunks)

    def _skip_prefill(self, r: sqlite3.Row) -> bool:
        """Whether the engine's own figures put this request's prefill above any real rate (config)."""
        rate = spread.prefill_rate(r)
        if rate is None or rate <= self.cfg.max_prefill_tps:
            return False
        self.skipped_prefill += 1
        print(f"kiln-dash: {r['id']}: the engine reported {rate:,.0f} tok/s of prefill, over the"
              f" {self.cfg.max_prefill_tps:,.0f} tok/s limit: left out of the chart", flush=True)
        return True

    def _with_progress(self, r: sqlite3.Row) -> tuple[sqlite3.Row, list[tuple]]:
        """The row with its engine progress attached, or a note that its prefill cannot be believed."""
        if self._skip_prefill(r):
            return r, [("prefill_impossible", r["id"])]
        return self._attach_progress(r), []

    # -- engine log lines ----------------------------------------------------
    def engine_facts(self, container: str) -> dict:
        """Sizes the engine printed (collect.memory.engine_facts) and its latest conversation cache bytes."""
        return self.containers.get(container, {}).get("mem", {})

    def _learn_sizes(self, container: str, facts: dict) -> None:
        known = self.engine_facts(container)
        new = {**known, **facts}
        if "weights_bytes" in facts:  # a new start: the conversation cache starts empty
            new.pop("cache_bytes", None)
            new.pop("cache_pinned_bytes", None)
        if "slot_state_bytes" in facts and "slot_fixed_bytes" not in facts:  # one line prints both, or one
            new.pop("slot_fixed_bytes", None)
        if new != known:
            self._learn(container, mem=new)

    def seed_sizes(self, container: str, facts: dict) -> None:
        """Start sizes read back from the journal: they fill gaps and never replace a newer figure."""
        known = self.engine_facts(container)
        new = {**facts, **known}
        if new != known:
            self._learn(container, mem=new)

    def engine_line(self, container: str, msg: str, ts: float) -> list[tuple]:
        facts = memory.engine_facts(msg)
        if facts:
            self._learn_sizes(container, facts)
            return []
        try:
            parsed = engines.parse(msg, self.containers.get(container, {}).get("engine"))
        except engines.LogFormatError:
            # A fixed reason: the line itself is not stored (engine lines are numbers today, but need not stay so).
            self.conn.execute("INSERT INTO engine_events (ts, host, profile, kind, detail) VALUES (?, ?, ?, ?, ?)",
                              (ts, self.cfg.host, container, "parse_error",
                               "an engine line looked like a request report but did not parse"))
            return [("parse_error", container)]
        if parsed is None:
            return []
        engine, kind, rec = parsed
        if self.containers.get(container, {}).get("engine") != engine:
            self._learn(container, engine=engine)
        if kind == "model":
            self._learn(container, model=rec["model"])
            return []
        if kind == "error":  # numbers, flags and a fixed kind only (engines._c_error)
            return self._crucible_error(container, engine, rec, ts)
        if kind == "cache_event":  # engine numbers only, never request content
            self.conn.execute("INSERT INTO engine_events (ts, host, profile, kind, detail) VALUES (?, ?, ?, ?, ?)",
                              (ts, self.cfg.host, self._container_meta(container, engine)["profile"] or container,
                               f"cache:{rec['event']}", json.dumps(rec["values"])))
            return []
        meta = self._container_meta(container, engine)
        if kind == "decode":
            # Live only, nothing stored: a line a second for each decoding slot, for a figure whose final value
            # the turn's own line stores. Without an engine number the line cannot be paired with a stream,
            # and without both counts it has no figure.
            req = engine_number(rec["req"])
            if req is None or not meta["profile"] or rec["draft_proposed"] is None or rec["draft_accepted"] is None:
                return []
            return [("decode_drafts", meta["profile"], req, rec["draft_proposed"], rec["draft_accepted"], ts,
                     rec["tokens"], rec["elapsed_s"])]
        if kind == "progress":
            if rec["phase"] == "prefill":
                self._store_chunk(container, rec, ts)  # the request's history reads it when its line joins
            if rec.get("turn_req") is not None:
                # A job the engine started itself (Crucible's continuation of a closed think turn): no stream
                # to show, but its number must not count as a gateway start (live/pairing.py).
                req = engine_number(rec.get("req"))
                return [("continuation", meta["profile"], req)] if req is not None and meta["profile"] else []
            if rec.get("total") and meta["profile"]:
                step = engines.HALOGEN_PREFILL_STEP if engine == "halogen" else None
                return [("progress", meta["profile"], rec["tokens"], rec["total"], rec["elapsed_s"], ts, step,
                         engine_number(rec.get("req")))]
            return []
        if kind != "completion":
            return []
        sizes = {k: rec[k] for k in ("cache_bytes", "cache_pinned_bytes") if rec.get(k) is not None}
        if sizes:
            self._learn_sizes(container, sizes)
        notes = []
        prefill_rate = (rec.get("prefill_tokens") or 0) / rec["prefill_s"] if rec.get("prefill_s") else None
        if meta["profile"] and prefill_rate and (rec.get("prefill_tokens") or 0) >= PREFILL_HINT_MIN_TOKENS \
                and prefill_rate <= self.cfg.max_prefill_tps:
            # A rate above the limit is no rate at all (config.DEFAULT_MAX_PREFILL_TPS): the live
            # estimate must not learn from it.
            notes.append(("prefill_rate", meta["profile"], prefill_rate))
        if rec.get("pool_total") and meta["profile"]:
            notes.append(("kv", meta["profile"], rec["pool_used"], rec["pool_total"], ts))
        if rec.get("failed"):
            if meta["profile"] is None:  # the gateway records failures of its own requests
                self._insert_engine_row(container, meta, rec, ts, "direct", status=500,
                                        error=rec.get("outcome") or f"status {rec.get('status')}")
            return notes
        line_id = self._line_id(container, ts)
        if self.conn.execute("SELECT 1 FROM requests WHERE engine_line_id = ? OR id = ?",
                             (line_id, line_id)).fetchone():
            return notes  # a replayed line that was already used
        target = self._match_gateway(container, rec, ts)
        if target:
            eng = {**_engine_cols(rec), "cached": rec.get("cached"), "req": rec.get("req")}
            notes += self._join(target["id"], eng, line_id)
            self.book.price_request(target["id"])  # the engine may add cached tokens; same card
            return notes + [("request", target["id"])]
        self._insert_engine_row(container, meta, rec, ts, "engine_pending")
        return notes

    def _crucible_error(self, container: str, engine: str, rec: dict, ts: float) -> list[tuple]:
        meta = self._container_meta(container, engine)
        self.conn.execute(
            "INSERT OR IGNORE INTO crucible_errors (id, ts, host, profile, engine, container, request_id, status,"
            " streamed, kind, completion_tokens) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (f"err-{container}-{ts:.6f}-{rec['kind']}", ts, self.cfg.host, meta["profile"], engine, container,
             rec["req"], rec["status"], int(rec["streamed"]), rec["kind"], rec["completion_tokens"]))
        return []

    @staticmethod
    def _line_id(container: str, ts: float) -> str:
        return f"eng-{container}-{ts:.6f}"

    def _insert_engine_row(self, container, meta, rec, ts, source, status=200, error=None) -> str:
        rid = self._line_id(container, ts)
        label = PROBE_LABEL if self.is_probe(container, ts) else container
        decode_s = rec.get("decode_s") or 0.0
        prefill_s = rec.get("prefill_s") or 0.0
        first = ts - decode_s
        row = {"id": rid, "source": source, "host": self.cfg.host, "container": container,
               "ts_start": first - prefill_s - (rec.get("queue_s") or 0.0), "ts_first_token": first,
               "ts_end": ts, "client_key": LOCAL_DEVICE, "client_name": self.cfg.host, "client_label": label,
               "profile": meta["profile"], "engine": meta["engine"], "model": meta["model"],
               "status": status, "error": error,
               "finish": rec.get("finish"), "prompt": rec.get("prompt"), "cached": rec.get("cached"),
               "completion": rec.get("completion"), "ttft_s": rec.get("ttft_s"),
               "engine_req": engine_key(rec.get("req")),
               "latency_s": ts - (first - prefill_s - (rec.get("queue_s") or 0.0)),
               **_engine_cols(rec)}
        cols = ", ".join(row)
        self.conn.execute(f"INSERT OR IGNORE INTO requests ({cols}) VALUES ({', '.join('?' * len(row))})",
                          tuple(row.values()))
        return rid

    # -- settling -----------------------------------------------------------
    def settle(self, now: float | None = None) -> list[tuple]:
        now = self.clock() if now is None else now
        horizon = min(now, self.watermarks["journal"], self.watermarks["gateway"]) - SETTLE_S
        notes = []
        self.conn.execute("DELETE FROM prefill_chunks WHERE ts < ?", (now - CHUNK_KEEP_S,))
        for r in self.conn.execute("SELECT id FROM requests WHERE source = 'gateway' AND series_done = 0"
                                   " AND ts_end < ?", (horizon,)).fetchall():
            notes += self._attribute_prefill(r["id"])
        for r in self.conn.execute("SELECT * FROM requests WHERE source = 'engine_pending' AND ts_end < ?",
                                   (horizon,)).fetchall():
            notes += self._make_direct(r)
        return notes

    def _make_direct(self, r: sqlite3.Row) -> list[tuple]:
        self.conn.execute("UPDATE requests SET source = 'direct', series_done = 1 WHERE id = ?", (r["id"],))
        if r["profile"] is not None and r["client_label"] != PROBE_LABEL:
            # A Kiln engine served a request the gateway didn't record: either someone bypassed the
            # gateway on the host, or the join failed. Keep a trace so a format change can't hide.
            self.conn.execute("INSERT INTO engine_events (ts, host, profile, kind, detail) VALUES (?, ?, ?, ?, ?)",
                              (r["ts_end"], self.cfg.host, r["profile"], "join_miss",
                               f"engine line without a gateway record: prompt {r['prompt']}, "
                               f"completion {r['completion']}"))
        self._client(r["client_key"], r["client_name"], None, r["ts_end"])
        r, notes = self._with_progress(self._row(r["id"]))  # now a direct row
        profile = spread.series_profile(r)
        self._series(spread.decode_pairs(r), "decode", profile, r["model"], r["client_key"])
        self._series(spread.prefill_pairs(r, self.cfg.max_prefill_tps, self._engine_blocks(r)),
                     "prefill", profile, r["model"], r["client_key"])
        self.book.price_request(r["id"])
        return [("request", r["id"]), *notes]
