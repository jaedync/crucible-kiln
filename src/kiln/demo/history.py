"""Two weeks of past work for the demo, so the history views (today, usage, activity, cache, speculation)
are not empty on the first screen.

It writes what the real sources would have left behind: the gateway's record files (gateway.records.RecordLog)
and the engine's crucible_turn lines, which go through the demo's log source with their past times. The
dashboard's own ingest joins and prices them, as it does with a backlog after a restart. Only numbers:
the records carry no content, as real ones do not.
"""
from __future__ import annotations

import json
import random
from datetime import datetime, timedelta
from pathlib import Path

from kiln import SCHEMA_VERSION
from kiln.demo import scenario
from kiln.demo.engine import CONTEXT, DRAFT_K, PACKS, TAIL_ROWS
from kiln.gateway.records import RecordLog

DAYS = 14
END_GAP_S = 20 * 60  # history ends before the live chart's 15 minutes, which the dashboard seeds at its start
CLIENTS = (("agent", 0.5), ("laptop", 0.3), ("desktop", 0.2))


def _conv_id(rng: random.Random) -> str:
    return "%012x" % rng.getrandbits(48)


def generate(now: float, seed: int = 0, days: int = DAYS) -> tuple[list[dict], list[tuple[str, str, float]]]:
    """(gateway records in end order, engine lines as (container, line, ts)) for the `days` before `now`."""
    rng = random.Random(f"history:{seed}")
    records, lines = [], []
    req_no = 0
    today = datetime.fromtimestamp(now).replace(hour=0, minute=0, second=0, microsecond=0)
    for back in range(days, -1, -1):
        day = today - timedelta(days=back)
        weekend = day.weekday() >= 5
        profile = "crucible-305" if back in (9, 10) else scenario.DEFAULT_PROFILE
        pack = PACKS[scenario.PROFILE_PACKS[profile]]
        container = f"kiln-{profile}"
        sessions = rng.randint(1, 3) if weekend else rng.randint(4, 8)
        lo, hi = day.timestamp() + 8.5 * 3600, day.timestamp() + 21 * 3600
        if back == 0:  # today: the hours before the demo starts, so the day's totals have work in them
            lo, hi, sessions = max(day.timestamp(), now - 10 * 3600), now - END_GAP_S - 600, 7
            if hi <= lo:
                continue
        for _ in range(sessions):
            start = rng.uniform(lo, hi)
            client = rng.choices([c for c, _ in CLIENTS], [w for _, w in CLIENTS])[0]
            t, conv, n_msgs, prev = start, _conv_id(rng), 1, None
            for turn in range(rng.randint(2, 12)):
                if t > now - END_GAP_S:
                    break
                req_no += 1
                if prev is None:
                    prompt = rng.choice((rng.randint(6_000, 30_000), rng.randint(30_000, 70_000),
                                         rng.randint(90_000, 140_000)))
                    cached = 0
                else:
                    appended = rng.randint(300, 3_500)
                    prompt = min(CONTEXT - 8_000, prev[0] + prev[1] + appended)
                    cached = prev[0] + prev[1] - 8 if rng.random() > 0.06 else 0  # now and then a miss
                tool = client == "agent" and rng.random() < 0.45
                completion = rng.randint(40, 160) if tool else rng.randint(150, 1_600)
                rec, line = _request(rng, t, client, profile, pack, container, req_no, prompt, cached, completion,
                                     tool, conv, n_msgs, prev)
                records.append(rec)
                lines.append(line)
                prev = (prompt, completion)
                n_msgs += 2
                t = rec["ts_end"] + rng.uniform(4, 90 if client == "agent" else 300)
    records.sort(key=lambda r: r["ts_end"])
    lines.sort(key=lambda x: x[2])
    return records, lines


def _request(rng, t0, client, profile, pack, container, req_no, prompt, cached, completion, tool, conv, n_msgs,
             prev) -> tuple[dict, tuple[str, str, float]]:
    computed = prompt - cached
    f = min(1.0, max(0.0, (prompt - 32_768) / (131_072 - 32_768)))
    prefill_tps = (pack.prefill_32k - (pack.prefill_32k - pack.prefill_130k) * f) * rng.uniform(0.99, 1.01)
    prefill_s = (0.006 if cached else 0.0) + computed / prefill_tps  # a restore, then the new rows
    queue_s = round(rng.uniform(0.001, 0.004), 4)
    decode_tps = rng.uniform(pack.decode_prose, pack.decode_code)
    decode_s = completion / decode_tps
    first = t0 + queue_s + prefill_s + 0.003
    end = first + decode_s
    rounds = max(1, round(completion / 3.1))
    accepted = completion - rounds
    events = 2 if tool else completion
    reasoning = completion - 24 if tool else round(completion * 0.3)
    per_s: dict[int, int] = {}
    for i in range(events):
        sec = int(first + decode_s * i / max(1, events))
        per_s[sec] = per_s.get(sec, 0) + 1
    lo = min(per_s)
    tokens_by_s = [lo, [per_s.get(s, 0) for s in range(lo, max(per_s) + 1)]]
    addr = scenario.DEVICES[client]
    prefix = {"conv": conv, "n_messages": n_msgs, "new_conversation": prev is None,
              "prefix_msgs_kept": 0 if prev is None else n_msgs - 2,
              "prev_n_messages": None if prev is None else n_msgs - 2,
              "system_changed": None if prev is None else False, "tools_changed": None if prev is None else False,
              "prev_prompt": None if prev is None else prev[0], "prev_completion": None if prev is None else prev[1]}
    rec = {"v": SCHEMA_VERSION, "id": "%032x" % rng.getrandbits(128),
           "ts_start": t0, "method": "POST", "path": "chat/completions", "body_bytes": prompt * 4,
           "client": {"key": addr, "label": None, "peer_ip": addr}, "profile": profile, "engine": "crucible",
           "model": scenario.MODEL, "stream": True, "user_agent": "kiln-demo", "max_tokens": 32768,
           "reasoning_effort": None, "n_messages": n_msgs, "n_tools": 1 if client == "agent" else None,
           "served_model": "crucible", "prefix": prefix, "status": 200, "error": None, "ts_end": end + 0.002,
           "latency_s": round(end + 0.002 - t0, 4), "ts_first_byte": t0 + queue_s, "ts_first_token": first,
           "ts_last_token": end, "token_events": events, "chars": completion * 4,
           "usage": {"prompt": prompt, "cached": cached, "completion": completion, "reasoning": reasoning},
           "finish": "tool_calls" if tool else "stop", "tokens_by_s": tokens_by_s, "ttft_s": round(first - t0, 4)}
    tested = [rounds]
    for p in (0.80, 0.70, 0.62, 0.55, 0.47, 0.40):
        tested.append(round(tested[-1] * p))
    acc = [round(x * p) for x, p in zip(tested, (0.80, 0.70, 0.62, 0.55, 0.47, 0.40, 0.33))]
    acc[0] += accepted - sum(acc)  # the counters must add up (collect/engines._c_draft)
    acc[0] = min(acc[0], tested[0])
    accepted = sum(acc)
    turn = {"request_id": req_no, "prompt_tokens": prompt, "cached_tokens": cached, "prefill_tokens": computed,
            "completion_tokens": completion, "queue_s": queue_s, "prefill_s": round(prefill_s, 4),
            "decode_s": round(decode_s, 4), "ttft_s": round(first - t0, 4), "finish": rec["finish"],
            "session_label": "miss" if not cached else "hit_replay", "replay_hit": bool(cached),
            "cache_saved_tokens": prompt + completion, "cache_tail_tokens": TAIL_ROWS, "slot_context": CONTEXT,
            "spec_rounds": rounds, "draft_proposed": rounds * DRAFT_K, "draft_accepted": accepted,
            "draft_pos_proposed": [rounds] * DRAFT_K, "draft_pos_tested": tested, "draft_pos_accepted": acc,
            "draft_len_hist": [0] * DRAFT_K + [rounds], "draft_passes": rounds, "draft_ms": round(rounds * 2.1, 3)}
    return rec, (container, "crucible_turn " + json.dumps(turn), end)


def write(data_dir: Path, records: list[dict]) -> None:
    log = RecordLog(data_dir)
    for rec in records:
        log.write(rec)
