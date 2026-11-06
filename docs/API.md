# Kiln dashboard API (v1)

Served by kiln-dash on port 7000. All times are UTC epoch seconds (float);
the browser formats them in local time. Token counts are integers. Money is
USD (float). Every message and response carries `"v": 1`.

Every request must name this machine in `Host` (ARCHITECTURE.md, Security model); others get 403. A
WebSocket handshake with an `Origin` of another site gets 403.

Fields marked `exact: false` (or values inside an object with
`"estimate": true`) must be rendered with a leading `≈` and a tooltip that
shows the object's `note`.

## WebSocket `/api/v1/live`

On connect the server sends one `snapshot`, then a `tick` about every 500 ms,
plus `request` and `engine` events as they happen. A new engine prefill report
sends a tick at once (at least 100 ms after the last one). The feed is one way,
except for the rack panel's shared screen (`panel` below).

### `tick`

```json
{
  "v": 1, "type": "tick", "ts": 1790870000.5,
  "live": {
    "decode_tps": 57.3,            // rule 2: sum over active streams, 0 when none
    "decode_exact": true,          // false -> render with ≈ (event/token ratio != 1)
    "decode_note": null,           // when decode_exact is false: why, for the ≈ tooltip
    "in_flight": 1, "slots": 4,
    "streams": [{
      "id": "9f2c...", "client": {"key": "198.51.100.10", "name": "laptop", "label": null},
                                   // key = device (peer address); label = X-Kiln-Client, metadata only
      "model": "qwen3.8-flash-next", "profile": "halogen-flashnext",
      "started": 1790869990.1, "first_token": 1790869991.0,
      "phase": "decode",           // "prefill" until the first token, then "decode"
      "tokens": 512,               // tokens streamed so far, or the engine's count from its decode lines
                                   // when that is higher (a tool call it holds back)
      "tps": 58.1,                 // this stream's live rate: the engine's, while its last decode line is
                                   // at most 2.5 s old, else from the gateway's arrivals (rule 2)
      "prefill_progress": null     // in prefill (live/prefill.py), else null:
                                   //  {"done": 25847, "total": 29989,  // the engine's last report: prompt
                                   //     tokens in its cache, the cached prefix included
                                   //   "elapsed_s": 2.7, "at": <engine line ts>,
                                   //   "start": 21743,              // cached tokens when the prefill began
                                   //   "start_estimated": false,    // false: a full first chunk or the tail rows
                                   //   "rate": 1551.5,              // tok/s: the stream's own recent reports
                                   //   "rate_from": "engine",       //  ("engine"), or the profile's recent
                                   //                                //  cold prefills ("recent"), or null
                                   //   "next": 29943,               // where the next report lands
                                   //   "reports": 2}
                                   //  Before the first report: done 0, total/start/elapsed_s null, at = when
                                   //  the engine could begin (the start, or when the stream left the queue)
                                   //  + 0.25 s, rate from recent cold prefills, next = the engine's chunk.
                                   //  null before the first report while another prompt prefills, without a
                                   //  rate or a chunk, or while queued. The browser moves the count on from
                                   //  `at` at `rate` (web/static/js/progress.js). start_known: false
                                   //  when the first report came with no rate, so `start` (0) is a guess.
      "prompt": {"total": 48872, "cached": 12288},  // the prompt's size and cached prefix from the
                                   // engine's reports, kept while it decodes; null before a report or
                                   // without a known start. The live cost prices the prompt from it
      "queued": false,             // past the engine's slots: it waits in the engine's queue
      "conv": "a1b2c3d4e5f6",      // the gateway's random conversation id (gateway/prefix.py), null if none
      "draft": {"accepted": 4120, "proposed": 5000, "turns": 5}
                                   // drafts accepted and proposed over the conversation's last finished
                                   // turns (at most 5, live/drafts.py); the running turn is not in them.
                                   // null before the conversation's first turn with drafts ends
      "draft_turn": {"accepted": 511, "proposed": 690}
                                   // the running turn's drafts so far, from the engine's decode lines
                                   // (Crucible crucible_decode, about once a second); null for an engine
                                   // without them, before the first line, or a line not paired with
                                   // this stream. The page shows it from 50 drafts on, else `draft`.
                                   // A request that does not stream turns to "decode" at its first
                                   // decode line
    }]
  },
  "second": {"t": 1790869999, "decode": 58, "prefill": 0},   // last completed second, exact
  "seconds": [{"t": 1790869999, "decode": 58, "prefill": 0}], // every second completed since the last tick
  "last": {                        // the most recently finished request
    "id": "9f2c...", "ts": 1790869950.2, "ttft_s": 0.41, "decode_tps": 56.8, "prefill_tps": 1310.0,
    "prompt": 69711, "cached": 68353, "completion": 733, "finish": "stop", "think_close": null,
    "draft_accepted": 512, "draft_proposed": 701  // null when the engine reported none
  },
  "engine": {
    "profile": "halogen-flashnext", "label": "Halogen / Qwen3.8 Flash-Next",
    "engine": "halogen", "model": "qwen3.8-flash-next",
    "state": "ready",              // ready | starting | stopping | stopped | failed
    "since": 1790860000.0, "target": null, "detail": "",
    "kv": {"capacity_positions": 524288, "capacity_bytes": 13958643712,   // capacity_bytes null if unknown
           "pool_used": 70144, "pool_total": 524288, "pool_ts": 1790869950.2}  // pool_* null if unknown
    "blocked": null,               // or why a switch would be refused now ("evaluation running (bench-hard)")
    "schedule": {                  // null without [schedule]; {"error": "..."} if it could not be read
      "day": "09:00-18:00", "tz": "UTC",
      "in_day": true, "next_change": 1790877600.0,    // when in_day flips next
      "day_profile": "crucible-flashnext",            // what the 09:00 start uses (null: the default)
      "holds": ["kiln", "schedule"],                   // files that pause the other jobs' GPU queue
      "dev_jobs": [{"pid": 4242, "unit": null, "cmd": "gpu-job -- ..."}],  // other GPU jobs that run now
      "drain_timeout_s": 900
    }
  },
  "hw": {"ts": 1790870000.0, "tctl_c": 68.5, "edge_c": 64.0, "mem_board_c": 49.9,
         "power_w": 100.1, "fan_rpm": 1784, "gpu_busy_pct": 99, "gtt_used_bytes": 29400000000,
         "gtt_total_bytes": 133143986176, "ram_avail_bytes": 85000000000, "profile": "balanced",
         "vram_used_bytes": 268435456, "vram_total_bytes": 536870912,   // amdgpu mem_info_vram_*
         "sclk_mhz": 2900,              // the pp_dpm_sclk level marked current; absent when none is marked
         "mem": {"ts": 1790869995.0,    // RAM by part and the engine's disk use, read every 10 s
                                        // (collect/memory.py); absent until the first reading, or when stale
           "ram": {"weights": 69428838400, "kv_state": 27987890176, "cache": 7561598112,
                   "cache_free": 617294688, "checkpoints": 1989256192, "stage": 2279813939,
                   "vision_draft": 475960000, "gpu_other": 4930000000, "ple_ram": 2650000000,
                   "file_cache": 1620000000, "programs": 1410000000, "kernel": 1730000000,
                   "free": 11400000000, "total": 134304514048},   // bytes; the parts add up to total.
                   // The GPU parts split GTT by the sizes the engine printed (collect/memory.py
                   // START_LINES): weights (arena), kv_state (one slot's sequence storage x slots; or, when
                   // the engine prints fixed_state_bytes, kv and recurrent in its place, and kv_state 0), cache
                   // and cache_free (cache_bytes, and cache_pinned_bytes less it, from its last turn line),
                   // checkpoints (slot checkpoints + prefill cut buffer), stage (stage arena), vision_draft
                   // (vision tower + draft head); gpu_other is the rest of GTT. Without the arena figure, or
                   // while the unit has no processes, gpu_other is all of GTT. ple_ram: the PLE file's pages
                   // in the page cache (cachestat, every 30 s); kernel: MemTotal less free, GTT, page cache
                   // and anon.
           "disk": {"ple_file": 39040193720, "states": 70500000000, "states_cap": 161061273600},
                   // the PLE file the engine maps, its --state-dir tree (allocated blocks) and
                   // --state-disk-gib; each absent when the engine has none
           "read_bps": 9600000, "write_bps": 136000000}},   // the engine unit's disk I/O since the last reading
  "today": {"requests": 43, "prompt": 1200000, "cached": 1100000, "completion": 54000,
            "cost": {"reference": 1.23, "openrouter": 0.05},
            "since": 1790812800.0,   // the local midnight the figures count from (requests by start time)
            "speeds": {"crucible-t22-405-long256-vision": {   // each profile with a good request since midnight
              "decode": {"avg": 60.7, "p5": 34.6, "p95": 87.2, "n": 212},   // tok/s; null without a rate
              "prefill": {"avg": 1470.0, "p5": 760.0, "p95": 1591.0, "n": 18},
              "ttft": {"avg": 1.84, "p5": 0.31, "p95": 12.4, "n": 230}}}},   // seconds
                                   // avg: all tokens over all seconds; p5, p95: of the request rates. Decode
                                   // from 32 output tokens, prefill from 1,024 new prompt tokens
                                   // (store/speeds.py). TTFT: every request with a first token; avg is the
                                   // plain mean. The decode panel shows the serving profile's
  "health": {"gateway": "up", "dash_started": 1790860000.0, "parse_errors_24h": 0,
             "parse_error_last_ts": null,   // the newest of those parse errors, epoch seconds
             "last_request_ts": 1790869950.2},
  "host": "strix",                  // this Kiln's own host: live, second(s), last, engine, hw, today and
                                   // health describe it only
  "hosts": {"remote": <host>}     // other hosts whose agents push (ARCHITECTURE.md, Hosts); {} without any
}
```

`today` covers requests served on this host only (`source` gateway or direct, `host` = `tick.host`),
from local midnight (server zone) to now, errors included; utility calls and remote hosts' work are not in
it. That was its meaning before hosts existed, so it is unchanged. Each remote host has its own `today`.

#### `<host>` (one entry of `tick.hosts`)

```json
{
  "name": "remote", "label": "remote · Example 8B (llama.cpp)",
  "engine": "llama.cpp", "model": "example-8b", "profile": "remote-example", "context": 131072,
  "state": "serving",              // serving | standby | down | stale (no push for more than 10 s)
  "seen": 1790870000.1,            // when the last push arrived (dashboard clock); null: none since start
  "detail": "",                    // why, for down and stale
  "live": {
    "decode_tps": 47.16,           // llama.cpp tg_3s from the engine log: estimated; null when unknown or
                                   // older than 4.5 s; 0 when idle
    "decode_exact": false,         // false -> render with ≈ and decode_note as the tooltip
    "decode_note": "Estimated. llama.cpp's own decode rate over its last 3 seconds ...",
    "in_flight": 1, "slots": 1,    // in_flight null while stale
    "phase": "decode",             // idle | prefill | decode | null (not known yet)
    "started": 1790869980.0,       // when the agent saw the task start (null if it started before the agent)
    "tokens": 142                  // tokens generated so far by this task (engine log, every ~3 s)
  },
  "hw": {"ts": 1790870000.1, "vram_used_bytes": 7516192768, "vram_total_bytes": 8589934592,
         "gtt_used_bytes": 536870912, "gtt_total_bytes": 17179869184, "gpu_busy_pct": 97,
         "junction_c": 71.0, "edge_c": 63.0, "mem_c": 80.0, "power_w": 150.0},  // null before any push
  "today": {"requests": 12, "prompt": 520000, "cached": 480000, "completion": 9100,
            "cost": {"reference": 0.27, "openrouter": 0.06}},   // this host only, same rules as tick.today
  "last": <same shape as tick.last, this host only, or null>
}
```

While `stale`, `live` fields are null and `hw` is the last report: grey it out.

### `snapshot`

Everything in `tick`, plus:

```json
{
  "type": "snapshot",
  "series_1s": {"t": [epoch_second, ...], "decode": [n, ...], "prefill": [n, ...],  // last 15 min, as sent
                "starts": [{"id": "...", "t": epoch}, ...]},  // requests that started in it
  "profiles": [{"name": "halogen-flashnext", "label": "...", "engine": "halogen", "model": "...",
                "context": 262144, "slots": 4, "memory_gib": 94, "active": true,
                "disabled": null}],   // or why it can't be selected yet; show it, don't offer it
  "me": {"can_act": true, "identity": "owner-laptop", "sid": 7},  // sid: this socket (panel echo)
  "panel": {"screen": "glance", "seq": 1042, "at": epoch,  // the rack panel's shared screen
            "by": {"source": "idle", "who": null, "sid": null, "req": null},
            "settings": {"idle_home_s": 300, "manual_quiet_s": 300, "min_dwell_s": 30, "stable_s": 3,
                         "alarm_home": true, "auto": {"enabled": false, "rules": {
                           "engine_switch": {"on": false, "screen": "glance", "hold_s": 300},
                           "night_start": {"on": false, "screen": "fleet", "hold_s": 120},
                           "day_start": {"on": false, "screen": "glance", "hold_s": 0}}}}},
  "build": "3fa9c1d0e2b4",  // hash of the server's code and static files; when a reconnect
                            // brings a different build, the page reloads itself
  "pricing": {"reference": "Claude Sonnet 5.5",  // [pricing.reference] name, for cost labels
              "input_per_m": 2.0, "cached_per_m": 0.1, "output_per_m": 10.0}
                                   // its prices: the live cost adds each running request's prompt (as
                                   // the engine reads it) and output so far (web/static/js/cost.js);
                                   // null when kiln.toml has no [pricing.reference]: the UI then hides
                                   // the API-cost figures
}
```

### `request` (a request finished)

```json
{"v": 1, "type": "request", "request": <row as in GET /api/v1/requests>}
```

### `engine` (switch progress)

```json
{"v": 1, "type": "engine", "engine": <same as tick.engine>, "log": "starting kiln-gufo-flashnext ..."}
```

### `panel` (the rack panel's shared screen changed)

Sent to every socket at once. `settings` comes only after a settings change. `by.source` is `touch` (the
kiosk on this machine), `remote`, `idle`, `alarm`, `auto:<rule>` or `start`.

```json
{"v": 1, "type": "panel", "seq": 1043, "screen": "gpu", "at": epoch,
 "by": {"source": "remote", "who": "owner-laptop", "sid": 7, "req": 12}}
```

A client sends these (text, 4096 bytes or less, at most 10 a second). Only a socket with `can_act` and an
`Origin` that matches the host may change the screen; a refused message gets `panel.refused` (sender only):

```json
{"type": "panel.set", "screen": "gpu", "req": 12}
{"type": "panel.touch"}
{"type": "panel.settings", "req": 13, "settings": {"auto": {"enabled": true}}}
{"v": 1, "type": "panel.refused", "req": 12, "error": "this device may not change the rack panel"}
```

## REST

- `GET /api/v1/state`: the snapshot as JSON.
Every history endpoint below (`usage`, `series`, `requests`, `activity`, `speculation`) takes `host=<name>|all`
(default `all`: this host and every remote host; an unknown name is 400). Before hosts existed all rows
were this host's, so `host=<tick.host>` gives the old view.

- `GET /api/v1/usage?period=today|7d|30d|all&group=device|model|profile|host&host=all`
  ```json
  {"period": "today", "from": 1790812800.0, "group": "device", "rows": [{
     "key": "198.51.100.10", "name": "laptop", "requests": 43,
     "prompt": 1200000, "cached": 1100000, "computed": 100000, "completion": 54000, "reasoning": 21000,
     "busy_s": 1520.4, "cost": {"reference": 1.23, "openrouter": 0.05}, "errors": 0,
     "tool_call_fallbacks": 0}],
   "totals": {same fields without key/name}, "host": "all",
   "buckets": {"step_s": 3600, "tz": "UTC", "t": [1790812800.0, ...],
               "local": ["2026-10-01 00:00", ...], "cost": {"<row key>": [usd, ...]},
               "tokens": {"<row key>": [n, ...]}, "busy": {"<row key>": [s, ...]}, "busy_all": [s, ...],
               "requests": [n, ...]}}
  ```
  `buckets` drives the usage bars: local clock hours for `today` (every hour of the day, 23 or 25 on a
  daylight saving day) and `7d` (from the period start's hour to now's), local days for `30d` and `all`
  (`all` from the oldest request's day). A request's cost and tokens (prompt plus output) count in the
  bucket where it started, so each key's costs add up to its row's `cost.reference`. Busy seconds are split
  at bucket edges; `busy_all` is the union over every key, so overlapping requests count once.
  `local` is each bucket's start in the server's zone.
  `busy_s` is the union of request intervals (overlaps count once, across hosts too: use `group=host`
  for each host's own busy time). A row's `name` is the device's name for `group=device` (its alias, else
  its `[devices]` name from kiln.toml, else its Tailscale name); for the other groups it is the key, except `container:<name>` (direct traffic to a
  container without a profile), named "<name> (container)". Every request belongs to the device that sent
  it: a gateway request to its peer, a direct request (it bypassed the gateway) to the Kiln host's
  loopback device with its container as the label, and a remote host's record to that host's own node.
- `GET /api/v1/series?range=15m|1h|6h|24h|7d|30d&group=none|device|model|host&host=all`
  ```json
  {"range": "1h", "step": 10, "t": [...], "series": {"<key>": {"name": "...", "decode": [...], "prefill": [...]}}}
  ```
  Values are tokens per bucket; divide by `step` for tokens/s. Steps: 15m→1 s, 1h→10 s, 6h→60 s,
  24h→300 s, 7d→3600 s, 30d→3600 s. `range=15m&group=none&host=<tick.host>` comes from live memory
  (includes requests still streaming); everything else from stored history. The UI chart asks for
  `host=<tick.host>` so it matches the live view.
- `GET /api/v1/requests?limit=50&before=<ts>`
  ```json
  {"rows": [{"id": "...", "ts": 1790869940.1, "client": {"key": "...", "name": "...", "label": null},
     "model": "qwen3.8-flash-next", "profile": "halogen-flashnext", "status": 200, "finish": "stop",
     "prompt": 69711, "cached": 68353, "completion": 733, "reasoning": 400,
     "ttft_s": 1.3, "decode_tps": 53.2, "prefill_tps": 1104.0, "latency_s": 15.1,
     "decode_from": 1790869941.4, "decode_to": 1790869955.2,  // its decode: the engine's decode time ending
                                                  // at the last token, or first to last token without the
                                                  // engine's line; null without completion tokens. The live
                                                  // chart draws completion / (to - from) across it.
     "think_close": null, "source": "gateway",   // or "direct" (bypassed the gateway; engine log only)
                                                  // or "agent" (served on a remote host, reported by its agent)
     "host": "strix",                              // the host whose engine served it
     "tool_call_fallback": false,                 // true: Crucible returned the model's non-tool-call output as
                                                  // content (usage.tool_call_fallback); null: not known
                                                  // (an engine without the flag, such as Halogen or Gufo,
                                                  // a direct or agent row, or a row from before the column)
     "spec_rounds": 40, "draft_accepted": 360, "draft_proposed": null,   // engine decode counts, null if unknown
     "conv": "a1b2c3d4e5f6",                      // the gateway's random conversation id, null if none
     "draft_pos_proposed": null, "draft_pos_tested": null, "draft_pos_accepted": null,
                                                  // Crucible's per-position draft counters: one integer per
                                                  // position 1..7, of the drafts proposed, the ones the target
                                                  // tested and the ones it accepted. null: unknown (an engine
                                                  // without them, or a request without speculation)
     "draft_len_hist": null,                     // verify rounds whose draft length was L, for L = 0..7 (8
                                                  // values); L = 0 is a round with no draft, not zero accepted
     "draft_passes": null, "draft_ms": null,    // MTP head passes the request took part in, and the wall time
                                                  // of those passes in ms, shared between the requests of a pass.
                                                  // Decode-loop head passes only (no warm-up, flush or settle
                                                  // pass, no logits copy): a trend, not a full cost
     "generated_tokens": null, "forced_tokens": null,  // Crucible think turn closed by the server: the engine's
                                                       // tokens and the close text written at once (both part of
                                                       // completion); decode_tps then uses generated_tokens
     "cost": {"reference": 0.021, "openrouter": 0.0004}, "error": null}]}
  ```
- `GET /api/v1/activity?days=31`: `{"slot_s": 3600, "tz": "UTC", "days": [{"date": "2026-01-15", "hours":
  [{"busy_s": 120.0, "top": "<client key>"}, ... 24]}], "clients": {"<key>": {"name": "...", "color": "#..."}}}`
- `GET /api/v1/signals?days=14&host=all`: Crucible server signals, counts per local day and engine (newest
  first; `days` 1 to 90; the engine name is `unknown` for a row that never named one):
  ```json
  {"days": 14, "host": "all", "rows": [{"date": "2026-01-15", "engine": "crucible", "requests": 40,
     "errors": 2, "stream_errors": 1, "client_errors": 5, "tool_call_fallbacks": 3}]}
  ```
  `errors` counts `crucible_error` log lines (HTTP 500 and above, and any error after the stream began);
  `stream_errors` is the part that ended a stream after HTTP 200. `client_errors` counts the lines of
  requests the server refused with HTTP 400-499 before the stream (for example a path that the server does
  not have). They are not in `errors`. `tool_call_fallbacks` counts requests
  whose usage had `tool_call_fallback: true`; the matching log line (kind `tool_call_fallback`) is not
  counted in `errors`, and a day's figure is the larger of the flagged requests and those lines, so one
  event never counts twice. A flagged request counts on the day it ended (`ts_end`, else `ts_start`),
  which is the day of its log line, also near midnight. `requests` counts by start time, as elsewhere.
  No message text is stored or returned for either signal.

  Known limits:
  - With `host=all`, the larger-of-two rule joins hosts. Agent rows have no flag, and Kiln reads log
    lines only from its own host. If Crucible serves on a remote host, the count can be too low. Today
    Crucible runs only on strix.
  - On a narrow screen, the "tool call as text" badge is in the part of the requests table that scrolls
    sideways, beside the finish column.
- `GET /api/v1/speculation?period=today|7d|30d|all&host=all`:
  Crucible's per-position draft counters summed per profile over the period (`host` as in the other history
  views). Only requests whose turn line carried at least one of the counters count, so the view is empty
  until the engine sends the fields.
  ```json
  {"period": "7d", "from": 1790265200.0, "host": "all", "positions": 7,
   "rows": [{"profile": "crucible-flashnext", "engine": "crucible", "requests": 40, "rounds": 1200,
     "passes": 480, "proposed": [400, 300, 210, 120, 60, 20, 4],
     "tested": [400, 260, 150, 70, 20, 4, 1], "accepted": [320, 180, 90, 40, 10, 2, 0],
     "accept_rate": [0.8, 0.6923, 0.6, 0.5714, 0.5, 0.5, 0.0],
     "len_hist": [12, 30, 80, 200, 300, 320, 180, 78],
     "draft_ms": 34500.0, "generated_tokens": 8000, "ms_per_token": 4.312}]}
  ```
  `accept_rate[i]` is the conditional rate `accepted_i / tested_i` over the summed counters, not the mean of
  the requests' own rates; it is null where nothing was tested at that depth. `ms_per_token` is `draft_ms`
  over `generated_tokens`, null without either. A bad period or host is 400. The dashboard's Speculation
  panel draws this view.
- `GET /api/v1/prices`: `{"current": [card], "history": [card]}`, card =
  `{"id", "source": "reference|openrouter", "model", "provider_model", "input_per_m", "cached_per_m",
  "output_per_m", "effective_from", "fetched_at"}`.
- `GET /api/v1/engines`: `{"profiles": [...], "engine": <tick.engine>, "events": [{"ts", "kind", "profile",
  "detail"}]` (switch steps only: switch_requested, stopping, stopped, starting, ready, failed, reconcile)`, "blocked": null | "evaluation running (bench-hard)"}`.
- `POST /api/v1/engines/switch` body `{"profile": "<name>", "confirm"?: bool, "remember"?: bool}`,
  headers `X-Kiln-Action: 1` and a same-origin `Origin`. Returns 202 `{"accepted": true}`; progress
  arrives as `engine` events. 403 when the caller isn't allowed, 409 `{"error", "confirm"}` when
  blocked or already switching. `"confirm": true` in a 409 means another GPU job runs (with `[schedule]`):
  repeat with `"confirm": true` to pause the other jobs and wait for it (bounded; Kiln never stops it).
  `"remember": true` makes the profile the morning start's choice (ignored with `eval_token`).
  A disabled profile gets 409.
- `GET /api/v1/me`: `{"can_act": bool, "identity": str | null}`.
- `GET /api/v1/panel`: data for the rack panel (`/panel`) that the live feed does not carry. Read only.
  The server reads the files at most once per second, so many panels cost nothing. `fleet` is null
  without `[schedule]`, and `{"error": "..."}` when the files cannot be read.
  ```json
  {"v": 1, "ts": 1790870000.0, "fleet": {
     "lease": {"agent": "job-c", "kind": "gates", "prio": 2, "elapsed_s": 200.0, "limit_s": 360.0,
               "typical_s": 188.0},
                                   // the open lease of a live lease wrapper (lease log), or null. By day no lease
                                   // line exists: with Kiln's hold file "kiln" and no fleet lease this is
                                   // {"agent": "daily engine", "kind": "serve", "prio": null,
                                   //  "elapsed_s": null, "limit_s": null, "typical_s": null, "daily": true}
     "queue": [{"agent": "job-a", "prio": 1, "waited_s": 142.0, "kind": "bench", "limit_s": 960.0,
                "typical_s": 212.0}],   // in the lease wrapper's order
     "holds": ["kiln"],            // files that pause the queue: at most 8 names, each a label as below
     "holds_total": 1,             // the exact number of hold files
     "recent": [{"agent": "job-b", "kind": "bench", "prio": 1, "rc": 0, "held_s": 212.0,
                 "ago_s": 40.0}],  // the last 8 finished leases, newest first
     "plan": {"night": "2026-01-15", "updated_ago_s": 60.0, "items": [
                {"id": "check-1", "label": "First check", "state": "running", "expect_min": 35,
                 "elapsed_s": 720.0, "held_s": null, "rc": null}]}}}  // the night driver's plan, or null
  ```
  `agent` and each hold name is a worktree-style name (`[a-z0-9-]`, at most 32 characters) or `other`; `kind` is one of `gates`,
  `serve`, `train`, `bench`, `build`, `other`. Ticket text, command lines and paths are never returned.
  A queued job's `kind` and `limit_s` (the hard limit, where the lease wrapper kills it) come from the lease wrapper's own
  rules applied to its ticket. `typical_s` is the median `held_s` of the agent's last five finished leases
  of that kind, else of any kind; null without history and always null for `other`.
  `plan` is the night driver's own plan of its items, written by the fleet's night plan tool to the file that
  `[schedule] plan_file` names (the fleet's own default file without it). One driver script
  runs its items one at a time, so the lease wrapper shows one long lease and no queue; the plan says what runs now,
  what comes next and what the night has finished. `state` is one of `waiting`, `smoke`, `running`, `pass`,
  `fail`, `skipped`. `expect_min` is 0 to 1440 and null when the field is bad. `elapsed_s` is set for
  `smoke` and `running` (now minus the start), `held_s` for `pass`, `fail` and `skipped` (the end minus the
  start, null when an item ended without a start); `rc` is the item's exit code, -255 to 255. The writer
  moves an item's clock only when its state changes: `smoke` or `running` sets the start and clears the end
  and the rc, `pass`, `fail` or `skipped` sets the end, and `waiting` clears both times. A smoke in the evening
  therefore never reads as hours of work in a run late at night. `id` and `label` are checked with the writer's
  patterns (at most 24 and 48 characters), a bad one becomes `other`; a bad state becomes `waiting`; a
  `night` that is not `YYYY-MM-DD` becomes `other`. At most 40 items. The server reads the file again only
  when its mtime or size changes and never more than 64 KiB of it. `plan` is null when the file is
  missing, is not valid JSON, is not version 1, or was updated more than 14 hours ago (`updated_ago_s`
  carries its age). A bad plan file never affects the rest of the view.
- `GET /api/v1/panel/seen`: `{"v", "age_s", "uptime_s"}` for the kiosk's watchdog. `age_s` is the seconds
  since the last `GET /api/v1/panel` from this machine (loopback only: a panel open elsewhere does not
  count), or null when none came since the dashboard started; `uptime_s` is the dashboard's uptime.
- `POST /api/v1/hosts/<host>/push`: for the host agent only (ARCHITECTURE.md, Hosts). Allowed only from
  the tailnet node whose full MagicDNS name is `[hosts.<host>] node` (for example
  `remote.example.ts.net`); everyone else gets 403 before the body is read. Without `[hosts]` in
  kiln.toml the answer is always 404.
  `Content-Length` required (411), at most 512 KiB (413). Body:
  ```json
  {"v": 1,
   "state": {"ts": 1790870000.0, "engine": "up|loading|down", "phase": "idle|prefill|decode",
             "task_started": 1790869980.0, "tokens": 142, "decode_tps": 47.16, "decode_ts": 1790869999.2,
             "vram_used_bytes": 1, "vram_total_bytes": 1, "gtt_used_bytes": 1, "gtt_total_bytes": 1,
             "gpu_busy_pct": 97, "junction_c": 71.0, "edge_c": 63.0, "mem_c": 80.0, "power_w": 150.0},
   "records": [{"id": "chatcmpl-1f3a...", "kind": "lan", "client": "laptop",
                "ts_start": 1790869900.0, "latency_s": 30.5, "ttft_s": 2.1, "status": 200, "finish": "stop",
                "stream": true, "prompt": 43600, "cached": 40000, "completion": 1271, "prefill_tokens": 3600,
                "prefill_s": 1.9, "decode_s": 28.2, "draft_proposed": 1531, "draft_accepted": 837}]}
  ```
  Every field except `v`, `state.ts`, and a record's `id`, `kind`, `ts_start` and `latency_s` is optional
  (null or absent = unknown); `kind` names the host's ledger the record came from, and `client` is
  required for `lan`. Unknown fields are refused. Up to 200
  records. Reply 200 `{"accepted": n, "duplicate": n, "rejected": [{"index": i, "reason": "..."}]}`: a
  record already stored (same id) is a duplicate and changes nothing; a refused record does not refuse
  the others. 400 for a bad envelope or state.

## Field notes

- `think_close` (requests and `last`): `null` when thinking ended on its own
  (or the request had none); otherwise the engine's reason for cutting thinking
  off and forcing the answer, currently `"answer_room"` (Halogen, and Gufo
  with the answer-room patch). Show non-null as "thinking cut off". A
  `finish` of `"length"` means the answer itself hit `max_tokens`.
- `draft_pos_proposed`, `draft_pos_tested`, `draft_pos_accepted`, `draft_len_hist`, `draft_passes`,
  `draft_ms` (request rows): Crucible's speculative decoding, from its `crucible_turn` line. The lists hold
  one count per depth (`draft_len_hist` one per draft length 0..7). A value the line does not promise
  correctly is dropped: a list of the wrong length, a negative or non-integer entry, or a broken invariant
  (`sum(proposed) == draft_proposed`, `sum(accepted) == draft_accepted`, `sum(draft_len_hist) <=
  spec_rounds`, `accepted_i <= tested_i <= proposed_i`, `tested_1 == proposed_1`) leaves that one field
  null and keeps the turn's other figures. A request without speculation has all of them null, like a row
  stored before the columns existed. Kiln never stores the engine's per-token detail counters
  (`draft_tally`, `draft_miss`, `draft_detail_cut`), in any column and in the raw line.
- `source`: `gateway` (seen in full by the gateway) or `direct` (an engine log
  line with no gateway record: evaluations and anything that bypassed the
  gateway). Their TTFT, if any, is the engine's own figure, and their `ts` is
  derived from the engine's prefill and decode times.
- Activity `date` and hour index use the server's zone, given in `tz`.
- Client colours: every `client` object (requests, live streams) and every
  device row in `/usage` and `/activity` carries `color`, assigned by the
  server in order of first appearance from the Bauhaus palette, so the first
  twelve devices are distinct and a device keeps its colour. Use it rather than
  hashing keys in the browser.
- `decode_estimated` / `prefill_estimated` (request rows and `last`): true
  when the rate comes from gateway timing rather than the engine's own figures
  (decode assumes tokens spread evenly over stream events; prefill uses TTFT,
  which includes queueing). Show those with `≈`.
- A row whose prefill rate is above `max_prefill_tps` (`[dash]` in kiln.toml,
  20,000 by default) reports figures no engine can produce: its prefill is left
  out of the history and out of the recent-rate estimate (the live view and the
  medians), and `kiln-dash` logs one line per such row. The row itself, its
  tokens and its cost stay.
- The live per-second history (`series_1s`, `tick.seconds`) is append-only:
  each second is sent once and never changes, and `series_1s` ends at the last
  second already sent. Decode counts tokens by arrival second; for engines that
  send fewer stream events than tokens it is events × the measured ratio
  (fractional, approximate). Prefill is counted while it runs, at the stream's
  reported (or the profile's recent) rate: an estimate. A token batch that
  arrives after its second was sent counts in the next second. `series_1s`
  also lists `starts`: `[{"id", "t"}]`, the requests that started in its
  window, for the chart's start lines.
- `/series` (the longer ranges) holds the exact counts: each request's usage
  count spread over its seconds, prefill on the seconds the engine ran each of
  its chunks, and traffic
  that bypassed the gateway. It can differ a little from what the live view
  drew. Don't call the live chart "exact".
- `POST /api/v1/engines/stop` body `{"reason", "eval_token"?, "force"?}`: while
  an evaluation holds the lock, stopping needs its token or `"force": true`
  (the owner's emergency stop); 409 otherwise.
- During an evaluation the gateway serves only requests carrying the lock's
  `X-Kiln-Eval-Token`; others get 503 `evaluation_running`.
- Gateway refusals besides 409 and 503 (each with a record, except the path and size checks before
  the body is read): 400 `invalid_path` (the path leaves `/v1/`), 400 `invalid_body` (JSON nested too
  deeply), 408 `body_timeout`, 413 (body over `[gateway] max_body_mib`), 413 `body_too_complex`, 503
  `too_many_held` (more requests wait for a switch than `[gateway] max_held`), 403 (a `Host` that is not
  this machine).
- The gateway's own `GET /health` (port 8000, not kiln-dash):
  `{"ok": true, "v": 1, "profile", "state", "model", "in_flight", "held", "admission"}`.
  `in_flight` counts the requests that the engine has now. `held` counts the
  requests that wait for a planned switch; they are not in `in_flight`.
  `admission` is `"held"` while an admission hold file is active, else
  `"open"`. See ARCHITECTURE.md, Admission hold.
  New keys (the old ones do not change): `capacity` `{slots, in_flight, free, queued, utilization, busy}`
  (now; `slots` null when no profile serves), and `last_5m` and `last_1h`
  `{covered_s, started, finished, ok, client_error, cancelled, server_error, in_flight_mean, in_flight_peak,
  utilization_mean, saturated_s, held_s_total, held_s_max, ttft_s_p50, ttft_s_p95, prompt_tokens,
  completion_tokens, completion_tok_s, request_tok_s_p50}`. Aggregates only. Definitions, the switch rule and
  an example: ARCHITECTURE.md, Gateway, `GET /health`.
- Gateway records of a request that waited for a switch carry `held_s` (seconds
  of wait). Its `latency_s` and `ttft_s` include the wait.
- Gateway records carry `adapted` (a list of change names such as `drop:reasoning_effort`) when the
  active profile has a request dialect; see ARCHITECTURE.md, Request dialects.
- `GET /api/v1/cache?hours=24&days=14&open=<conv>,<conv>`:
  `{"flag_tokens": 4096, "sessions": [{"conv", "client": {key, name, color}, "last", "n_turns", "last_prompt",
  "reread", "flagged", "client_changes", "refusals", "turns" (only for the conversations listed in `open`,
  at most 30; the last 300 turns, oldest first): [{ts_start, profile, prompt, cached, completion, ttft_s, n_messages,
  prefix_msgs_kept, prev_n_messages, system_changed, tools_changed, new_conversation, appended_tokens,
  reread_tokens, cache_save_refused, cache_session_label, cache_entries, cache_bytes, cache_evictions,
  host_free_mib, replay_hit, closest_shared_tokens, closest_outcome, "cause": "new|ok|client|engine|unknown"}]}],
  "daily": [{date, profile, turns, computed, reread, engine_misses, client_changes, refusals}]}`.
  `appended_tokens` and `reread_tokens` are estimates (render with ≈). No content exists to return.
  `daily` and `GET /api/v1/signals` are computed at most every 20 s; open dashboards share the result.
- `tick.engine.cache`: Halogen's own `GET /cache` counters while a Halogen profile serves
  (`entries, max_entries, bytes, hits, misses, stores, evicted, refused, prompt_tokens_saved`, cumulative
  since the engine started), else null.
