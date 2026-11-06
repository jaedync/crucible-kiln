# Kiln architecture

Kiln is the live inference dashboard and gateway for one inference host (a
Strix Halo machine such as the Framework Desktop, Ryzen AI Max+ 395, 128 GB) that serves one local LLM engine at a time. It
answers three questions: what is the machine doing right now, who used it and
how much, and what would that usage have cost from an API.

This document is the contract between the parts. Change it when a part's
interface or a metric's meaning changes.

## Processes

```
 clients (laptops, phones, evals)
        |  OpenAI-compatible HTTP, LAN or tailnet
        v
 kiln-gateway  :8000  ── forwards bytes ──>  active engine container (127.0.0.1 only)
   |  writes requests-YYYY-MM-DD.jsonl         Halogen :8731 | Gufo :8080 | ...
   |  serves live events on 127.0.0.1:8001          |
   v                                                | logs to the user journal
 kiln-dash  :7000  <── journal (engine log lines) ──┘
   |  SQLite: requests, costs, price cards, series, hardware, engine events
   |  REST /api/v1, WebSocket /api/v1/live, static UI
   |  engine manager: renders Quadlet units, switches the active profile
   v
 browsers (LAN, tailnet)

 other hosts (remote): kiln agent ── POST /api/v1/hosts/<host>/push (tailnet, ~1 s) ──> kiln-dash
   reads the host's request ledgers, engine log, GPU sysfs and /health; controls nothing
```

- **kiln-gateway** is the only part in the request path. It stays small, has
  no database, and restarts independently of the dashboard. If it dies, a
  client can point straight at the engine on the inference host itself.
- **kiln-dash** owns everything else. Deploying it never interrupts a stream.
- Both run as systemd user services of the owner's user (lingering), like the engine
  containers, so they share one journal and one podman.
- **kiln agent** (`kiln.cli agent`) runs on each other host (see Hosts) and
  only pushes to kiln-dash. It has no database and controls nothing.
- Listen addresses in `kiln.toml` are `host:port`, `[ipv6]:port` or `*:port`.
  `*` binds every interface with one IPv4 and one IPv6 socket. `0.0.0.0` is
  IPv4 only and `[::]` is IPv6 only. The gateway on the inference host uses `*:8000`: when
  a client's mDNS cache entry for `strix.local` expires (every 120 s),
  systemd-resolved can answer with the IPv6 addresses alone (systemd#19003).
  An IPv4-only gateway refused those connections, and the client showed a
  connection error.

## Setup on a new machine (`kiln init`, `kiln doctor`)

- `kiln init --crucible DIR --pack DIR [--port N] [--dash-port N] [--engine-port N] [--ctx N] [--slots N]
  [--lan] [--force] [--no-start]` asks no questions. `DIR` for `--crucible` is a checkout of the public Crucible repo
  with `build/engine/crucible` built. `--pack` is the EXL3 checkpoint: a directory with `config.json` (init
  refuses an empty flag value and a directory without that file). The command writes
  `$XDG_CONFIG_HOME/kiln/kiln.toml` (default `~/.config/kiln/kiln.toml`) with comments and one native
  profile `crucible` that runs `python -m server PACK --engine ... --ctx N --streams N --port N`. It
  uses the checkout's `.venv/bin/python` when it exists. It also writes the user units `kiln-gateway`
  and `kiln-dash`, which run the installed `kiln` entry point, enables them and restarts them (a restart
  also reaches units that run with an older kiln.toml; `--no-start` leaves them stopped). It refuses to
  replace a changed file without `--force`. Then it prints the next commands: `kiln doctor`, `kiln switch
  crucible`, and `loginctl enable-linger` only when `loginctl show-user` does not report `Linger=yes`.
- The gateway and the dashboard listen on 127.0.0.1 by default. `--lan` writes `*:PORT` (every
  interface). They have no login of their own, so a LAN bind is an explicit choice.
- `kiln doctor` prints one line per check: `PASS`, `WARN` or `FAIL`, with a fix. It exits with 1 if a
  check fails. The checks: Python 3.11 or later, aiohttp, the config, the user manager, the journal,
  the amdgpu sysfs files (WARN), Linux 6.5 or later for cachestat (WARN), the pack directory of each
  Crucible profile (the argument after `-m server` holds `config.json`), the gateway and dashboard
  ports (free, or held by our own unit; WARN when systemd --user does not answer), the active engine's
  port (FAIL when it answers but the engine's unit is not active: another process holds it), and
  `crucible_*` lines in the journal of the unit's current run (`_SYSTEMD_INVOCATION_ID`, WARN). Each
  check is a pure function in `kiln/doctor.py`. `kiln check` prints a broken kiln.toml as one line
  (`kiln: PATH: problem`, exit 2) and fails (exit 1) on a pack directory without `config.json`.
- Defaults for a new user: `host` is the short host name. `container_uid` is the uid of the user that
  runs Kiln. A profile without `kv` reserves `slots x context` positions. `served_id` is the `model`.
  Without `[pricing.reference]` no request gets a reference cost, the snapshot has `pricing: null`, and
  the UI hides the "Via API" figures. Explicit values always win.
- Native profiles only: the manager does not run `podman` when no profile has a `[container]` table, so a
  machine without podman can switch engines. The manager enables the active native unit and disables the
  others (a Quadlet unit boots by its `[Install]` section alone), so the active engine starts at boot and
  none starts after `kiln stop`. A profile name can not be `gateway` or `dash` (the unit
  names of Kiln itself).
- Optional parts: the GPU schedule (`[schedule]`: `engines/fleet.py`, `collect/gpu_queue.py`) and
  remote hosts (`[hosts]`: the `hosts` package and the agent) are imported only when kiln.toml has
  their table. The public package leaves them out, and its tests pass without them.

## Security model

Kiln has no login. It trusts the network address of a request, so these rules decide who can use and see
what. The settings below are in `kiln.toml`.

### Host names (DNS rebinding)

- A web page can make its own name resolve to this machine. The browser then sends the page's name in
  `Host` and `Origin`, so a same-origin check alone passes. The page could then switch engines through the
  dashboard and use the model through the gateway.
- The gateway (also its events feed) and the dashboard refuse a request whose `Host` is not a name of this
  machine. The refusal is 403, also for a WebSocket handshake (`kiln/netguard.py`).
- These names are always accepted: `localhost`, the configured `host`, `<host>.local`, the machine's own
  host name and its short form, `host.docker.internal` and `host.containers.internal`. The MagicDNS name
  is added at start when `tailscale status` answers.
- Every IP address literal is accepted. A rebinding attack needs a name that the attacker controls, and a
  browser that sends an address connected to that address.
- `[dash] allowed_hosts` and `[gateway] allowed_hosts` add names (default: none). Add each other name that
  clients use, for example a DNS name of the LAN or the MagicDNS name when tailscaled starts after Kiln.
  `"*"` turns the check off.

### Web pages (cross-site requests)

- A web page can send a `text/plain` POST to `127.0.0.1:8000` from the owner's browser. Such a request
  needs no CORS preflight, so the engine would run it. The browser adds an `Origin` header to it.
- The gateway refuses with 403 every request that has an `Origin` header, unless `[gateway]
  allowed_origins` lists that origin (`scheme://host[:port]`, case does not matter). `"*"` allows every
  origin. The default is `[]`.
- API clients (the OpenAI SDK, curl, a server-side chat UI) send no `Origin` and pass. Add the origin of a
  chat UI that calls the gateway from the browser page itself.

### Who may act (switch, stop, the panel's writes)

- A direct request from loopback may act. `[actions] allow_loopback = false` removes this right. Then only
  `allow_nodes` and `allow_users` may act, also for `kiln switch` and the rack kiosk.
- A request that has a `Forwarded`, `X-Forwarded-For`, `X-Real-IP`, `Tailscale-User-Login` or
  `CF-Connecting-IP` header came through a proxy. It may never act, and it is not the kiosk.
- `allow_nodes` compares the full MagicDNS name. An entry with a dot is a full name
  (`laptop.example.ts.net`). An entry without a dot is a device of this host's own tailnet. A device shared
  in from another tailnet can have the same first label, so it does not match. When the own tailnet is not
  known (no `tailscale status` at start), an entry without a dot matches nothing.
- `allow_users` compares the Tailscale login of the device.
- A request to a `.local` name may read but never act. Any device on the same link can answer mDNS for that
  name, and then rebind it to this machine's tailnet or loopback address. Use the MagicDNS name (or an
  address) from the devices that act. A single-label name (`strix`) can be answered by LLMNR on some
  clients; prefer the full MagicDNS name there too.
- A browser must also send `X-Kiln-Action: 1` and an `Origin` of the dashboard itself. The live WebSocket
  refuses a foreign `Origin`.

### Reverse proxies on the same machine

- A proxy on this machine (`tailscale serve`, `tailscale funnel`, Caddy, nginx, a container port mapping)
  makes every client arrive from loopback. Without the proxy headers above, Kiln can not tell such a client
  from the owner.
- Before you put a proxy in front of the dashboard, set `[actions] allow_loopback = false`. Kiln also
  refuses actions through a proxy that adds one of the headers above (Caddy adds `X-Forwarded-For` by
  default), but do not rely on that for a proxy whose headers you have not checked. With `tailscale serve`
  set `allow_loopback = false`. Never use `tailscale funnel` for the dashboard: it makes the dashboard
  public.
- Any local account and any local process can also reach loopback. On a machine with other users, set
  `allow_loopback = false` and name your own devices in `allow_nodes`.

### Who can see what when the dashboard listens on the LAN

- Every client that reaches the dashboard can read all of it. Reads need no right.
- This includes the device names, addresses and labels of all clients, their live streams, the hourly
  activity for up to a year, conversation ids and turn figures, the engine log and the hardware readings.
- Message content is never stored or shown (see Cache diagnostics), but who used the model and when is
  visible to every viewer.
- Keep the dashboard on loopback or on the tailnet when other people use the same LAN. The gateway shows
  less: `/health` gives totals only, and `/v1/models` gives the loaded model.

### Gateway limits

- `[gateway] max_body_mib` (default 32) is the largest request body. Four phone photos of 4 MB as base64
  make about 20 MiB. `[gateway] max_held` (default 32) is the number of requests that may wait for an
  engine switch. Their bodies may use at most 10 times the body cap together. A held request keeps the
  raw bytes, the decoded text and the parsed values. The gateway estimates this memory from the size, the
  widest character and the count of `{`, `[` and `,` (`gateway.app.held_cost`). Above either limit a
  request gets 503 at once. A body of ASCII text costs about 3 times its size. One character above U+FFFF
  makes each character of the text and the values 4 bytes, so such a body costs about 9 times its size.
  One such body at the default cap (288 MiB) fits the budget (320 MiB) alone. A body of 2 million tiny
  values can also fill the budget alone.
- The gateway frees the parsed body before it sends the request on. A stream keeps only the bytes that
  the engine got.
- A request with a `Content-Encoding` other than `identity` (gzip, deflate, br) gets 415
  (`unsupported_content_encoding`). The gateway reads none of its body. A compressed body could pass the
  body cap and the parse slots, which count the compressed size.
- A body with more than 200,000 `{` and `[` or 2,000,000 commas gets 413. Parsed JSON uses up to 37 times
  the body size for a body of tiny objects (`tools/measure_body_memory.py`). The bound keeps one parse
  below about 230 MB.
- Bodies over 1 MiB, and smaller bodies with more than 3,000 `{` and `[` or 50,000 commas, are parsed in a
  worker thread. The worst smaller body measured holds the event loop for about 17 ms.
- Bodies over 1 MiB are read and parsed 2 at a time. A body of text takes about 4 times its size while it
  is parsed. A body with one character above U+FFFF takes about 17 times its size, because its text and
  values are 4 bytes per character (`gateway.app.parse_slot_mib`). `kiln init` sets `MemoryMax` of
  kiln-gateway from the body cap (`gateway.app.memory_max_mib`: 1728M for 32 MiB). Raise it when you
  raise `max_body_mib`.
- A request with more than 4,096 messages gets no cache diagnostics.
- The relayed path must stay under `/v1/`. A path with an encoded `/`, `\`, `#`, `?` or control character,
  or with a `.` or `..` segment, gets 400.
- The events feed must listen on loopback (`events_listen`). It refuses a request with an `Origin` header,
  so a web page can not read it.
- A record keeps `model`, `finish` and `reasoning_effort` only as a short plain name, else null. It keeps
  `max_tokens` only as an integer from 0 to 2^31 - 1, else null. kiln-dash stores an integer outside the
  SQLite range as null, reads the record files in chunks of 8 MiB, and skips a line longer than that. It keeps the Tailscale device
  name, never the login or the OS.

### Files

- Kiln creates the gateway's record directory and the database directory at 0700, and their files at 0600.
  It does not change the mode of a directory or file that exists.
- Kiln writes unit files only from kiln.toml, never from a request. Labels are written with `%%` for `%`.
  A unit value that ends in a backslash is refused. `kiln init` quotes each `ExecStart` argument.

## Gateway

- Routes `/v1/*` to the active profile (see Engines). Clients use canonical
  model ids (`qwen3.8-flash-next`); the gateway rewrites to the engine's served
  id. A model that is not loaded gets 409 with the loaded model's id.
- Passes bytes through unchanged, with one exception: streamed requests get
  `stream_options.include_usage = true` so every request ends with an exact
  usage report. A `stream_options` that is not an object is replaced by
  `{"include_usage": true}`. A client disconnect closes the upstream request.
- Observes the SSE stream without modifying it: arrival time of every event,
  token-bearing events (content, reasoning, tool-call deltas), final usage
  and finish reason.
- Attribution: `client_key` is the peer address, so each device is one
  client. The device name comes from `tailscale whois` on the peer address
  (cached 10 min; the record keeps the device name, never the login or the OS); loopback is named after
  the Kiln host; LAN addresses keep
  the address until named. A LAN device that connects over IPv6 is keyed by
  that address, apart from its IPv4 key, so a turn sent over IPv6 also starts
  its own conversation in the cache diagnostics, unless `[devices]` lists both
  addresses (below). The optional `X-Kiln-Client`
  header (`eval-<run>` tags evals) is stored as `client_label`, metadata
  shown beside the device name, never the key.
- A machine that connects from several addresses (LAN, Wi-Fi, link-local
  IPv6, tailnet) can be listed under `[devices.<name>]` in kiln.toml. Its
  requests are stored under its first address with the configured name, live
  streams from any of its addresses show as that device, and kiln-dash moves
  rows stored under its other addresses when it starts (`store/devices.py`).
- Every request belongs to one device, so the usage table's Device rows are
  machines. A request that bypassed the gateway came from the inference host itself (the
  engines listen on loopback): it is keyed `127.0.0.1` like the gateway's
  local requests, with its container as the label (`kiln-probe` for the
  engine manager's health check). Migration 008 moved the older
  `direct:<container>`, `eval:<run>` and `kiln:probe` keys.
- Writes one JSON line per request to
  `~/.local/share/kiln/gateway/requests-YYYY-MM-DD.jsonl`. Content (messages,
  output text) is never written.
- Serves live events over WebSocket on `127.0.0.1:8001/events` (see Live
  events). Nothing outside the inference host can reach it.

### `GET /health`: capacity and recent use (port 8000)

One GET tells a LAN or tailnet client if the inference host has free capacity, and how busy
it was in the last 5 minutes and the last hour. It gives aggregate numbers
only: no client key, address, label, device name, prompt or output.

- Old keys (unchanged; switch scripts drain on them): `ok`, `v`, `profile`,
  `state`, `model`, `in_flight`, `held`, `admission` (see Admission hold).
  `in_flight` counts generation requests (`gateway/stream.py`
  `GENERATION_PATHS`) from the forward to the end. Requests that wait for a
  switch are in `held`, not in `in_flight`.
- `capacity` (now):
  - `slots`: the `slots` of the active profile; null when no profile serves.
  - `in_flight`: the same number as the top-level key.
  - `free = max(0, slots - in_flight)`.
  - `queued = max(0, in_flight - slots)`: requests that wait inside the engine.
  - `utilization = in_flight / slots` (above 1 when requests are queued).
  - `busy`: true when `state` is not `ready`, the admission is held, or
    `free` is 0. With `slots` null, `free`, `queued` and `utilization` are
    null and `busy` is true.
- `last_5m` and `last_1h` have the same keys (`gateway/usage_window.py`):
  - `covered_s`: the seconds of the window that the gateway knows. It is less
    than the window when the startup seed failed, or when the count cap (10,000
    entries per series) dropped entries still inside the hour. All other keys
    describe these seconds only.
  - `started`: generation requests that arrived. `finished`: generation
    requests that ended, also the ones refused before the forward (503, 409)
    and the ones whose client left while they waited. By outcome: `ok` (2xx),
    `client_error` (4xx except 499), `cancelled` (499), `server_error` (5xx,
    and a request with no status).
  - `in_flight_mean` (time-weighted), `in_flight_peak`, `utilization_mean`
    (time-weighted `in_flight / slots`) and `saturated_s` (seconds with
    `in_flight >= slots`). Switch rule: each moment counts against the slots
    in force at that moment, which are the slots of the profile that got the
    last forwarded request. A moment with unknown slots adds nothing to
    `utilization_mean` and `saturated_s`.
  - `held_s_total`, `held_s_max` (null with no wait), `ttft_s_p50`,
    `ttft_s_p95` (linear between ranks, as the Today table; they include a
    wait for a switch), `prompt_tokens`, `completion_tokens`,
    `completion_tok_s` (completion tokens over `covered_s`) and
    `request_tok_s_p50` (per request: the completion tokens after the first
    stream event over the first to last token event; replies of 32 tokens or
    more). Everything except `started` and the in-flight keys is counted at
    the request end, so a long reply that ends in the window brings all its
    tokens.
- Cost: memory only. Each series is in time order with a running sum, so a
  call is a few bisects and one sort of the window's TTFTs (p99 0.2 ms in
  process after a busy hour of 3,000 requests). The state holds one hour and
  at most 10,000 entries per series.
- Restart: at startup the gateway reads the tails of yesterday's and today's
  record files back to one hour ago (records are in end order; a request goes
  to the file of its start day) and rebuilds the windows. A forwarded record
  was in flight from `ts_start + held_s` to `ts_end`. Lines that do not parse
  are skipped. If the seed fails, the windows start at the startup and
  `covered_s` shows it; the startup goes on. A busy hour (3,000 records) takes
  about 60 ms. Requests that the old process did not record are missing.

Example (Crucible daily profile, 4 slots):

```json
{"ok": true, "v": 1, "profile": "crucible-405", "state": "ready",
 "model": "qwen3.8-flash-next", "in_flight": 3, "held": 0, "admission": "open",
 "capacity": {"slots": 4, "in_flight": 3, "free": 1, "queued": 0, "utilization": 0.75, "busy": false},
 "last_5m": {"covered_s": 300.0, "started": 21, "finished": 19, "ok": 18, "client_error": 0, "cancelled": 1,
             "server_error": 0, "in_flight_mean": 2.412, "in_flight_peak": 4, "utilization_mean": 0.603,
             "saturated_s": 41.3, "held_s_total": 0.0, "held_s_max": null, "ttft_s_p50": 0.84,
             "ttft_s_p95": 6.212, "prompt_tokens": 812345, "completion_tokens": 14210,
             "completion_tok_s": 47.4, "request_tok_s_p50": 31.8},
 "last_1h": {"covered_s": 3600.0, "started": 160, "finished": 158, "ok": 151, "client_error": 2,
             "cancelled": 4, "server_error": 1, "in_flight_mean": 1.07, "in_flight_peak": 5,
             "utilization_mean": 0.268, "saturated_s": 212.6, "held_s_total": 48.1, "held_s_max": 44.7,
             "ttft_s_p50": 0.91, "ttft_s_p95": 9.87, "prompt_tokens": 6120044, "completion_tokens": 98112,
             "completion_tok_s": 27.3, "request_tok_s_p50": 32.4}}
```

## Metric definitions and correctness rules

Every displayed number follows these rules. A number that can't meet them is
not shown, or is shown with an explicit `≈` and a tooltip saying how it was
estimated.

1. **Exact or labelled.** Token counts come from the engine's usage report.
   Live counts come from token-bearing stream events, reconciled at request
   end; the per-profile ratio of usage tokens to events is tracked, and when it
   is not 1.0 the live figures carry `≈`.
2. **Live decode rate** = for each active stream, tokens received in the last
   `W = 2 s` divided by `min(W, now - first_token_time)`; summed over active
   streams. A finished stream leaves the sum immediately and no streams means
   exactly 0. No exponential smoothing. One exception: while an engine's
   decode line for the stream (Crucible `crucible_decode`, about once a second)
   is at most `ENGINE_FRESH_S = 2.5 s` old, the stream's rate is the engine's
   (tokens between its last two lines), and its token count is the engine's
   when that is higher. Crucible holds a tool call's non-string parameters
   back until they close (`server/tool_stream.py`), so a stream can get no
   events for a minute while the engine decodes at full speed; arrivals alone
   showed such a stream at 0 tok/s. In a measured run (12 h, 951
   requests), 10.4% of the decode ticks were at 0 tok/s before, 0.006% after.
   Each stream keeps the engine request number of its first paired line (or
   its prefill pairing), and a line with another number is not its own.
3. **Per-second history** counts tokens by their arrival second (exact
   integers) while a request streams, so a chart never lags or eases down.
   The live 15 min chart is append-only: each second is sent to browsers once,
   about 0.4 s after it ends, and never changes; `series_1s` in a snapshot ends
   at the last second already sent, and a browser joins the broadcast at that
   point. A token batch that arrives after its second
   was sent counts in the next second not yet sent (`live/rates.py`). When
   kiln-dash restarts, the live seconds are seeded from the stored history, so
   a browser that reconnects draws the last 15 minutes once more, with the
   exact counts. An engine decode line that reports more tokens than a
   stream has charted (arrivals times the ratio) counts the difference in
   the next open second; arrivals then count only past that, so no token is
   counted twice. A stream restored from a hello after a kiln-dash restart
   takes its first engine line as the start, so its earlier tokens do not
   land in one second. A thin
   line across both plots marks each request's start, from the first tick that
   shows it running; the snapshot lists the starts in its window (`starts.js`).
   A start after the chart's newest whole second waits at the right edge until
   its second is drawn (`startLineAt`). The live y axes grow for a new peak and
   never shrink while the view is open (`holdMax`), so drawn seconds keep their
   height. When a request ends, a light column spans its decode window at its
   average rate (`columns.js`). It is the one mark that appears over seconds
   already drawn; its window and height never change, also when the engine's
   line joins with a different window. Columns stack (`stackBands`): the widest
   is at the bottom, and between two column edges each one stands on the sum of
   the wider ones running then, so the stack's top is the summed rate at every
   moment (no overhang); a band steps where a request under it starts or ends.
   A wider request that ends later goes under the columns already drawn and
   lifts them. A brighter blue line is the decode rate smoothed by a Gaussian of
   sigma 4 s (`smoothRate`, gaps left out); its newest seconds use only the past,
   so they move a little as the next seconds arrive. Token counts on
   screen stream between ticks at each stream's rate, at most 0.75 s past the tick, and never move back
   (`web/static/js/count.js`); rates are shown as the server computed them.
   Arrival is not generation: Halogen holds a tool call's arguments back until
   the call is complete and sometimes flushes output in bursts. So in the stored
   history (`series_1s` in the database, which the 1h and longer ranges read),
   once the engine's completion line joins, such a request's exact token count
   is spread evenly over the engine's decode time, ending at the last token. A
   stream with at least 0.9 events per token and no second above 3 times its
   mean rate keeps its arrival shape, scaled to the exact count.
   Until the engine line joins (or if it never does), any other stream is
   spread over its own first-to-last token window from the gateway, so a
   held-back tool call never shows its whole output in one second. These
   corrections, prefill on the seconds the engine ran each chunk, and traffic that bypassed the
   gateway reach only the stored history, never the live chart's drawn seconds.
   The live tokens-per-event ratio is the median over recent requests' events (each
   request's ratio weighted by its events), so tool calls, which send many
   tokens in few events, don't inflate it. series_1s is derived entirely from `requests`
   (`collect/spread.py`); bumping `SERIES_VERSION` rebuilds it at the next
   kiln-dash start.
4. **Prefill** is only known when it ends. Its rate is
   `computed prompt tokens / prefill seconds` using the engine's own figures
   (log line) or, if absent, `(prompt - cached) / TTFT`, and is shown as "last
   request". While a request is prefilling, the UI shows elapsed time and, for
   engines that report progress (Crucible every 4,096 computed tokens, Halogen
   every 32K), the tokens computed of those to compute, the cached start and
   the time left (`live/prefill.py`, `web/static/js/progress.js`). A report's
   `done` counts the cached prefix, so the rate comes from the stream's own
   recent reports, or the profile's recent cold prefills before it has two;
   between reports the count moves on at that rate, at most one chunk plus
   1 s (the report's delivery) past the last report. The cached start is
   decided once, at the first report, from the work the prompt could have
   done by then: the recent rate times its share of the engine since its
   slot (each second counts 1 / prompts prefilling), less 0.05 s per engine
   command and 8 us per token of restored prefix (Crucible restores a cached
   prefix before it computes the 8 rows after it: 1.9 s at 230K). It is exact
   when that is about one chunk (0.5-1.5) and the report ends a full chunk,
   or when it is under 0.4 of a chunk: the 8 tail rows after a restore. A
   tail is never taken for a report inside the first chunk or on a chunk
   boundary, where a full chunk ends. A report between the two bands, a
   report from later in the prefill, a prompt that fits in one chunk and
   Halogen's fixed boundaries give an estimate, and an estimate under 10% of
   that work counts as no cache. Before the first report a lone prompt counts at the recent
   rate from 1.5 s after it could begin (its start, or when it left the
   engine's queue), without a size; the count ends if the report is more than
   twice overdue. While another prompt prefills there is no such count: the
   first report can wait behind the other prompt's chunks. A gateway reconnect
   keeps each stream's reports; a report for another prompt size after two
   that agree is ignored as a wrong pairing.
   Reports reach the dashboard about 0.3-0.6 s after the engine logs them:
   journald batches its change notices (250 ms), the journal follower delivers
   a batch 50 ms after its last line (at most 100 ms after its first), and a
   report ticks the dashboard at once.
   The live chart counts prefill while it runs: each second gets the stream's
   rate from its own reports (or the profile's recent rate, shared by the
   prompts prefilling) for the part of the second it prefilled; once a report
   gives the prompt's size, at most the prompt left to read. Two prompts
   prefilling at once cannot draw more than one engine's rate between them:
   the ones with figures of their own are slowed (by the same factor) until
   their sum is the profile's recent rate, which is what the prompts without
   figures already share. One prompt alone keeps its own rate: nothing can
   double count there. Before its first
   report a stream counts nothing for its first 1.5 s: a turn of a cached
   conversation reads a few hundred tokens in less, and the recent rate over
   its whole time to first token would draw several times that. A long
   prompt's first 1.5 s are missing instead. It is an estimate (≈) and is not
   corrected when the prefill ends.
   The stored history does know when the engine worked. An engine reports its
   progress (Crucible every 4,096 computed tokens, Halogen every 32K); ingest
   keeps each report's `done` and its time and, at the join, turns the reports
   into the windows the engine prefilled in: a window ends at a report and
   reaches back the seconds that report names since the one before it (since
   the engine took the request, for the first). The Crucible server pauses a
   long prompt to run a shorter one to first token, so a window reaches over
   another request's chunks, which is how the chart came to count two prompts
   over the same second. Each chunk's tokens are spread over its own
   window less the windows, on that engine, of the chunks that ended before it:
   the engine ran them then, so that time is theirs. A request before this
   column, or without reports, keeps the even spread over its prefill interval,
   trimmed the same way. An unpaired request (direct, agent) reads its window
   from the engine's `prefill_s` and completion tokens.
   A prefill above `max_prefill_tps` (default 20,000, `[dash]` in kiln.toml,
   `spread.impossible_prefill`) is impossible for any of these engines. It is
   not charted, it does not become the live chart's recent rate, and
   `queries.recent_prefill_rates` skips it; the request row keeps its own
   figures, ingest counts it (`skipped_prefill`) and prints one line, because a
   wrong engine pairing or a bad log line is the likelier cause than a fast
   engine.
   `kiln backfill-prefill --since --until [--dry-run]` recomputes the prefill
   of that range from the rows and reports the seconds it would change
   (`collect/backfill.py`), for a range whose rows arrived before the engine's
   reports were kept. `kiln-dash` does the same for the whole history once
   after a rules change (`ensure_current`, `SERIES_VERSION`).
5. **KV cache.** Capacity comes from the profile (positions and bytes per
   position from the model architecture). Occupancy is shown only from exact
   sources: the engine's own report with its timestamp (Halogen's `pool
   used/total` after each request) or per-request context
   (`prompt + generated so far`) once the prompt size is known. Engine metrics
   known to be wrong are never shown (Gufo 0.3.0 `kv_cache_usage_ratio` reads
   0 while generating).
6. **Time to first token** is measured at the gateway (request received to the
   first token-bearing event). Queue time is included and reported separately
   when the engine logs it.
7. **Costs are sticky.** Each request is priced once, at ingest, with the price
   card in force at the request's start time, and the result is stored. A
   price change creates a new card; it never rewrites history. A request older
   than any card for its model is priced with the earliest card and flagged.
   One exception, by hand only: `kiln backdate-reference --since <ISO time>`
   (with kiln-dash stopped) applies `kiln.toml`'s reference prices from a past
   time, for a price change Kiln learned of late. It re-prices the requests
   from that time that used an older card, and no others.
8. **Times** are stored as UTC epoch seconds; the browser formats local time.

## Pricing

- Sources: `reference` (Claude Sonnet 5.5 from `[pricing.reference]`: $2 input,
  $0.10 cached input since 2026-10-07 17:00 UTC and $0.20 before, $10 output per
  million; the UI names the source from the configured `name`, sent in the snapshot, so
  past cards show the current name too) and `openrouter` (the model-level
  list price of the OpenRouter entry whose `hugging_face_id` matches the
  profile's `hf_id`, excluding `:free` and `:batch` variants and ignoring any
  discount field).
- `price_cards(id, source, model, provider_model, input_per_m, cached_per_m,
  output_per_m, effective_from, fetched_at, raw)`. The refresher (daily, and
  on demand) inserts a card only when a price differs from the latest card.
- `request_costs(request_id, source, card_id, usd, flags)`: one row per source.
- **The live "Via API" figure** (masthead `web/static/js/panels/today.js` on digit wheels
  `odometer.js`; rack panel `js/panel/today.js`) is today's stored reference cost plus the running
  requests' cost so far (`cost.js` `liveParts`): output at the output price, and the prompt while the
  engine reads it, from the stream's `prompt` and prefill progress (cached prefix at the cached price at
  the first report, new tokens at the input price as they are computed). A prompt whose start the server
  could not tell from work (`start_known: false`) is not priced live, so cached tokens never get the
  input price. There is no cache-write price. A finished request's stored cost replaces its estimate.
  The shown figure rolls toward that target (`chase`), never moves back within a day, and rests only on
  a whole 1/10,000 dollar: when the target falls mid-roll (a stream ended before its record, a tick
  below the estimate) it finishes the digit it turns first, so no wheel stops between two digits.
  The bar under it splits today's cost by kind (`daySplit`: the stored figure by today's tokens at the
  card's prices, which is that sum, plus the running parts). Each turning digit takes the colour of the
  kind it rolls in: every rise joins a queue by kind and the figure pays it oldest first (`owedStep`,
  `tintStep`); blue output, ochre new prompt tokens, ochre stripes cache reads, and a settling record
  keeps the digits' own colour.

## Engines

- Profiles live in `kiln.toml` (`[profiles.<name>]`): engine, canonical model
  id, served id, `hf_id`, image (pinned), podman options, command, port,
  context, slots, KV capacity, expected memory.
- The manager renders one Quadlet file per profile into
  `~/.config/containers/systemd/kiln-<profile>.container` (container
  `kiln-<profile>`, published on 127.0.0.1 only, `Pull=never`). All engine
  units conflict with each other. Only the active profile gets
  `WantedBy=default.target`, so the choice survives reboots.
- A switch refuses while an evaluation holds the GPU (active user units that
  match `[engines] busy_units`, default none, or `eval-*` containers), stops the current unit, waits for GPU memory to be
  released, starts the target, and waits for a real generation before marking
  it active (`~/.local/state/kiln/active.json`, read by the gateway). A switch
  that fails before the target starts records `failed` with the old profile
  and no target when that engine was not confirmed stopped or its memory did
  not come back: a later stop then stops that engine, not the target that
  never ran. With no old engine, a memory timeout records `failed` with no
  profile: the target's unit was never installed, so a stop of it would fail.
- While a switch waits for the target, every poll reads `systemctl show` of its unit (ActiveState,
  SubState, Result, NRestarts, Job). A queued job comes first: a unit left `failed` by an earlier run,
  with a new start job, is not down. A unit that failed, waits in `auto-restart`, or is inactive, with no
  queued job, ends the switch as `failed` at once: the manager stops the unit (so it does not restart every
  10 s) and the detail gives the reason and the last journal line of the unit's own process. A test
  generation counts only while the unit is active, and before the start the manager refuses a target
  port that already accepts connections (all of Kiln's engines are stopped then, so it is another
  process). That failure names no engine. When the target's own unit holds the port (started by hand
  while the record said otherwise), the switch stops it first and starts it again.
- `kiln stop` always works: it cancels a switch in progress, then stops the engine the record names.
- Every 5 s (the dashboard's blocked check) `Manager.watch` looks at a `ready` engine's unit. A failed
  query changes nothing, and nothing changes while a switch or stop runs.
  - The unit crashed and systemd restarts it (`auto-restart`, or `activating` with a start job): the
    record becomes `starting` at once, so the gateway holds new requests. The manager then waits for a
    test generation, as a switch does, and the record becomes `ready` again with no user action. When
    NRestarts grows by 3 or more first, the manager stops the unit and the record becomes `failed`.
    While this runs, a switch is refused and `kiln stop` cancels it.
  - The unit does not run (inactive or failed, with no queued job): the record becomes `failed` with the
    reason and the last journal line. The manager checks again after it reads the journal, so a stop
    or switch that began meanwhile keeps its record.
- A stop that cancels a switch waits up to 5 s for a `systemctl` command in progress, then kills it. So a
  `systemctl start` can not reach systemd after the stop.
- At startup the manager reconciles the record with systemd
  (`Manager.reconcile`): a `failed` record is kept as it is and takes the
  GPU hold of `[schedule]`. A unit that is not active now can still start or restart, so
  Kiln trusts no `is-active` answer here; only a successful stop or a
  successful switch settles a failed record.
- Switching is an action: allowed from localhost, or from tailnet nodes whose
  Tailscale identity is in `actions.allow`; it requires the `X-Kiln-Action`
  header and a same-origin `Origin`.
- Native profiles (`[profiles.X.native]`) run as plain user units
  `kiln-<profile>.service`. Their journal lines use the unit name in place of
  a container name. A profile with `disabled = "<reason>"` is listed but
  cannot be selected (for example a checkpoint that is not delivered yet).
- A stop rewrites the units with no `WantedBy`, so a reboot after a stop
  starts no engine.
- Rootless podman makes the user namespace at its first call after a boot,
  with `newuidmap`. kiln-dash runs with `NoNewPrivileges=yes`, so that call
  fails there ("newuidmap: Could not set caps"). When `podman ps` fails this
  way, the manager runs `systemd-run --user --wait podman unshare true` (a
  unit of the user manager, without those limits) one time and tries again.
  The namespace then stays until the next boot. A failed repair is tried
  again after 5 minutes at the earliest, and a repair that worked is not
  repeated within 60 s (monotonic time, so a clock step does not matter).

### Admission hold: a planned switch without 503 (`engines/admission.py`)

While no engine serves, the gateway answers 503 `engine_unavailable`. A coding
agent can try again only 3 times in about 14 s, and a switch takes 40 to
60 s. The admission hold makes new requests wait for the switch instead.

- File: `admission-hold.json` next to the state file (default
  `~/.local/state/kiln/admission-hold.json`), content
  `{"v": 1, "until": <epoch s>, "reason": "<text>"}`. A hold is active while
  the file exists, parses and `until` is in the future. A missing, broken or
  expired file is no hold, so a script that stops mid-switch blocks nobody
  after `until`.
- CLI: `kiln hold --seconds N [--reason TEXT]` writes or refreshes the file
  (N from 1 to 3600) and prints its JSON. `kiln release` removes it (exit 0
  also when there is no hold). Both touch only the file, so they work while
  kiln-dash is down.
- Gateway rule (`Gateway._hold`): after the evaluation-lock check, a request
  waits while a hold is active or the state is `stopping` or `starting` (a
  switch from the dashboard, the CLI or a timer). It checks again
  every 0.25 s. When neither is true, it takes the normal path: forward if an
  engine serves, else 503. A request that waited checks the evaluation lock
  again and goes to the profile that serves now. In state `stopped` or
  `failed` with no hold, the 503 comes at once, as before.
- Cap: `[gateway] hold_max_s` (default 150, 0 turns the hold off), counted
  from the arrival of the request. At the cap the normal path decides, also
  when the hold is still active. A typical client waits 300 s for response headers.
- A waiting request is not in `in_flight`, so a drain can reach 0 while new
  requests wait. `/health` gives `held` (requests that wait now) and
  `admission` (`held` while a hold is active, else `open`).
- A client that leaves while it waits is recorded as 499
  `client_disconnected`, and nothing goes to the engine. The record of a
  request that waited has `held_s` (kept in the `raw` column; no migration).
  Its `latency_s` and `ttft_s` include the wait, because they count from the
  arrival.

Intended use by a switch script:

1. Run `kiln hold --seconds 600 --reason "switch to PROFILE"`.
2. Drain: wait for `/health` `in_flight` 0, with the hold on, for at most
   about 90 s. If it does not reach 0, run `kiln release`, wait about 30 s,
   and try again from step 1.
3. Run `kiln stop`, then wait for the GPU memory.
4. Run `kiln switch PROFILE`, then wait for state `ready`.
5. Run `kiln release`. Do this on every exit path (for example a shell
   `trap`), also when a step fails.

Refresh the hold (step 1 again) if the switch can take longer than the hold.

### Schedule (`[schedule]`)

An optional `[schedule]` lends the GPU to other jobs outside set hours; this needs a module that is not
part of the public package. The public package refuses `[schedule]` with a config error.

### Rack panel (`/panel`)

A page for the 1424 x 280 touch display in the inference host's rack (6.91 inch, 172 x 34 mm). It is mostly read from
2-3 m, and sometimes at arm's length. The kiosk (`deploy/panel`: `kiln-panel-kiosk.service`, cage and
Chromium with software rendering) opens it. The page is built for that one screen size at device pixel
ratio 1: no breakpoints and no scrolling.

- Screens, in tab order: Home (`glance`), Engine, Fleet (only with `[schedule]`), Today, Settings. Home is the adaptive resting
  screen. The old Live and GPU pages are gone: Engine took their useful parts. The ids `live` and `gpu` are
  retired: a `panel.set` with one is refused ("reload the page"), and in `panel.json` one becomes Home.
- Tab strip (`panel/tabs.js`, rules in `panel/tabs_model.js`): a 60 px row at the foot of every screen but
  Home, with the connection cell (KILN), five 166 x 60 tabs (the current one is cream), the sync cell, the
  LOCAL or VIEW ONLY mark and the clock. The sync cell says who set the rack's screen and when; the age is
  coarse ("just now", "3 min ago", then a clock time), so it changes at most once a minute. A pinned or
  view-only session that shows another screen says "Only here" and what the rack shows. On Home the strip
  lies over the page's foot for 10 s after a tap, and while a change by another device or the dashboard is
  news. The other screens are 216 px high above it.
- Navigation: a tap on a tab, or a swipe (60 px or more, mostly sideways). A tab or a button fires only on
  a tap that moved less than 20 px in total, so a swipe never presses one. On Home the first tap only shows
  the strip. The tap thirds are gone. The arrow keys still page.

- Glance (Home, `panel/glance.js`, rules in `panel/glance_model.js`): the whole screen; the strip only after a tap.
  - In both layouts, three cells under the state block give the package power (W), the GPU edge and the CPU
    Tctl (°C). The CPU cores are on their own dies, so Tctl can be about 30 °C above the GPU edge. A
    reading that is not available shows a dash. Each cell is a gauge: it fills from the left to its level
    and the fill's colour is its zone (`VITAL_SCALES`). Power: 0-140 W, orange from 120 W (the Framework
    Desktop's sustained limit; 140 W is its boost), never red. GPU: 30-90 °C, orange from 80, red from 85
    (the alarm starts at 90). CPU: 30-100 °C (Tjmax), orange from 85, red from 95. In range the fill is a
    light blue with a solid blue edge at the level.
  - Each colour has one job: blue is work that runs normally (decode, a
    ready engine, a running lease, a gauge in range, the charts), yellow is work before output or a change
    of state (prefill, starting, stopping), orange is warm, red is a fault only, and green is OK, as text.
    The yellow corner block on a page's key tile is the one decoration.
  - While an engine serves:
    - A colour block gives the state: blue ready, yellow starting or stopping.
    - One 196 px number (17.5 mm, easy to read to about 3 m) is the decode rate. When only prefill runs, it
      is the prefill percentage of the tokens to compute, with those tokens, the cached start and the time
      left. Before the engine's first report it is the seconds waited and a count of the tokens read.
      Under the decode rate, 60 blue columns show the last 10 minutes, one 10 s window each (the mean
      decode tok/s), with a yellow mark at the foot of a window that also prefilled. Windows follow the
      clock, so only the newest column changes and the strip shifts once in 10 s.
    - One lane per engine slot, at most 4, in start order. Decode lanes are blue, with the tokens so far and
      the rate. Prefill lanes have yellow stripes that fill as the engine reads the prompt. A prefill without
      a report has light stripes, its time and a count of the tokens read. A free slot is a dashed outline.
  - The panel draws when a tick arrives (twice a second, and at once for a prefill report). One timer,
    restarted by each tick, adds draws ten times a second while the glance page shows a count that moves
    (a decode lane's tokens, a prefill's progress), and once a second when no tick comes. Lane token
    counts show every token up to 99,999, so they visibly climb.
  - With no engine: the GPU busy % (the same 60 columns of the mean busy %; they fill from the page's
    start, as the page keeps no older readings). With `[schedule]`, the page also shows the other GPU jobs.
  - An alarm turns the whole screen red: no data for 20 s after the last tick, a failed engine, or the GPU
    edge at 90 °C or more. When a failed engine or a hot GPU begins, the dashboard shows this page on every
    following session (the hot flag clears below 87 °C, so one wobble is one jump); a touch can still page
    away. NO DATA is local: only the session can see that its feed stopped, so it shows this page itself.
  - Alarm mute: the red screen has one button, MUTE 30 MIN. It mutes one kind (hot or
    failed) on every session for 30 minutes (`MUTE_S`); NO DATA cannot be muted. While muted, Home is the
    normal page with a red tag "Alarm muted · 28 m" over the corner block, and the red gauge or Engine
    still shows the fault. The mute ends at its time or when its alarm clears; an alarm that still holds at
    the end is a new alarm (red screen, jump to Home). A new kind of alarm is never muted by the old mute.
    The mute is in `panel.json`, so a deploy keeps it. A session that may not act shows the button dashed.
  - Screen at night (`night_display`, Settings: Normal or Dim; no Off, because the display's power control is not checked): Dim is the class
    `is-dim` on the body (a dark theme, one CSS change, no extra draw). It applies in the night window and
    never while an alarm holds, a muted one included.
  - The idle return: 5 minutes (`idle_home_s`; Settings: 1, 5 or 15 minutes, or Never) after the last person
    input on any device, the dashboard shows the resting screen (this page).
  - A draw that throws shows "PANEL ERROR" over the whole screen, so the last good frame never looks live.
- Engine (`panel/engine.js`, rules in `panel/engine_model.js`): no buttons: the panel shows state and never acts on the engine. The state
  block of Home on the left; in the middle what the state means; on the right the GPU busy % and power (with
  2 minute trend lines), the shader clock and the GPU temperature. Serving: the profile, its slots and
  context, the KV pool, the GPU memory and the last TTFT (dim after 10 minutes). Switching or starting: the
  steps Stop old, Drain, Load (the GPU memory toward the profile's size), Test, with the step's clock, from
  the manager's `engine.phase` and `engine.phase_since`; `detail` stays the free text. Stopped: by day why no
  engine runs (`blocked`); with `[schedule]`, when the day engine starts and the other GPU jobs. Failed: the
  detail. The manager sets the phase with each state change and inside one state (drain after the stop,
  test once the server answers with other than 503); a step told again keeps its clock.
- Fleet (`panel/fleet.js`, rules in `panel/fleet_model.js`): shows only with `[schedule]`: the other GPU
  jobs that use the GPU outside the day window.
- Today (`panel/today.js`, rules in `panel/today_model.js`): the requests today (with errors and clients),
  24 hour columns from the `buckets` of `/api/v1/usage?period=today` (the current hour light and outlined,
  the peak named), the tokens (prompt, cached, output) and the GPU busy time, and the cost at API prices
  with the busiest clients. The usage poll runs every 15 s, so the page changes then.
- Tab flags (e02): a corner mark on a tab says that something happened there that the panel did not open.
  The dashboard decides them (`_flagged` in `live/panel_screen.py`), because only it knows the quiet time,
  the dwell and the edges exactly, and every session then shows the same flags. A yellow flag comes when an
  auto rule's edge (engine switch, night, day) did not change the screen: auto is off, or the rule is on
  but the manual quiet time or the dwell held it. A red flag comes on Fleet when a night plan item fails,
  whatever the rules say. One flag per tab; red beats yellow. A flag goes when its screen shows (any
  source) or when its state ends (the switch ends, the day starts, the plan has no failure). The sync cell
  names a red flag first, else the newest ("Engine switch: see Engine", with the quiet time left). Flags
  are not in `panel.json`: news is about now.
- Settings (`panel/settings.js`, rules in `panel/settings_model.js`): the only buttons on the panel. They
  change only the panel's own behaviour, so a tap acts at once, with no confirm. Auto-switch Off or On (On
  says how many rules are on), Back to Home after 1 m, 5 m, 15 m or Never, Screen at night Normal or Dim,
  Rules, and the Alarm tile (Mute 30 min during an alarm, Unmute during a mute, else a dashed "no alarm
  now"). Rules is a sub-view of
  this session only (a change of screen closes it): a toggle for each of `alarm_home`, `engine_switch`,
  `night_start` and `day_start` with its target, and the manual quiet time (2 m, 5 m, 15 m, 1 h). A change
  sends `panel.settings` and shows at once; the shared settings come back with the echo, and a refusal or
  no echo in 2 s shows them again. A session that may not act shows dashed, grey controls that do nothing.
- Prefill progress: engine progress lines carry the engine's request number, not the gateway's id.
  `live/pairing.py` pairs a number with a stream: alone, a prefilling stream owns the line. With several,
  the number's offset from the last known pairing counts gateway starts. A stream that started within
  0.25 s of another gets no progress. Lines arrive in journal batches, often after the first token, so
  who was prefilling is judged at the line's own time and a finished stream's number stays retired for
  30 s. Replayed against 26 h of the inference host's logs with late lines: 97.8% of single-prefill lines and 77% of
  overlapping ones got progress; one request (6 lines, four prompts at once) went to the wrong stream.
  Requests sent straight to the engine shift the count until the next lone prefill.
- One shared screen (`live/panel_screen.py`, pure; `dash/panel_sync.py`): the dashboard holds which page
  the rack shows, and every `/panel` session follows it. A swipe on a laptop moves the rack, and the other
  way round.
  - Protocol, on `/api/v1/live`: the snapshot carries `panel` (`screen`, `seq`, `at`, `by`, `settings`) and
    `me.sid` (one per socket). A session sends `panel.set` (`screen`, `req`), `panel.touch` (person input, at
    most once in 5 s), `panel.settings` (`req`, a partial settings object; from Settings) and `panel.mute`
    (`kind`: `hot`, `failed` or null to unmute; `req`). The dashboard applies a change and sends one `panel`
    message (`seq`, `screen`, `at`, `by`: `source`, `who`, `sid`, `req`; `what`: `screen`, `settings`,
    `mute` or `flags`; `mute`: `kind` and `until` (wall time) or null; `flags`; `settings` after a settings
    change) to every socket at once, not at the next tick. The end of a mute and a change of the flags are
    changes by the dashboard: `seq` goes up and `by` stays. A refusal goes
    to the sender only (`panel.refused`: `req`, `error`). `by.source` is `touch` (the kiosk on loopback),
    `remote`, `idle`, `alarm`, `auto:<rule>` or `start`; the dashboard sets it, not the client.
  - The sender shows its change at once and keeps it until its own echo (`sid` and `req`) comes; after a
    refusal, or without an echo in 2 s, it shows the shared screen. A message with an old `seq` is ignored, a
    snapshot replaces everything, and a session sends only for a person's input, so no loop is possible.
  - Who may change it: `can_act` and an `Origin` that matches the host at the handshake (browsers apply no
    CORS to a WebSocket; the POST endpoints share the check). Messages are text of 4 KB or less (a larger
    one closes the socket), of a known type, and at most 10 a second per socket (burst 20; over that, one
    refusal and then silence).
  - `/panel#<id>` pins that page in that session only (LOCAL in the strip): it never sends, ignores
    the shared screen, and has no idle return or alarm jump, so `tools/shoot_panel.py` never moves the rack.
    The pin follows `hashchange`. A session that may not act (VIEW ONLY) follows the shared screen; its
    paging is local and goes back after 60 s without input. After a change by another device or by the
    dashboard, the sync cell says who made it ("Set by owner-laptop", "Back home: idle"), and Home shows the
    strip for 10 s.
  - A window that is not 1424 x 280 (a laptop or phone that follows the rack) shows the same screen scaled
    to fit and centred (`model.fitScreen`, a CSS transform on the body), with the pointer visible. The
    kiosk's window is exactly 1424 x 280, so it gets no transform and keeps the hidden pointer.
  - Auto rules (`step`, once a tick): `alarm_home` (on), and `engine_switch`, `night_start`, `day_start`
    (off; auto is off by default). A rule fires on a change of its input that held for `stable_s`, not in
    the `manual_quiet_s` after a person's input, and not within `min_dwell_s` of another auto change. Each
    rule's target screen is a setting (`engine_switch`: Engine; `panel.json` v1 files with Home there
    move to Engine). With `night_start` on, the resting screen is Fleet at night.
  - `panel.json` beside the engine state file keeps the screen and the settings (atomic replace, written
    after the broadcast, never for a touch). A deploy keeps the rack's screen, unless it was set longer ago
    than `idle_home_s` (with Never, at any age it keeps it); then the start shows the resting screen.
- Data: the WebSocket `/api/v1/live` and `GET /api/v1/panel` (with `[schedule]` only: the other GPU jobs,
  cached 1 s). Only labels and numbers leave the server: never ticket text, command lines or paths.
- The page has no animation loop and no CSS animation, because Chromium renders on the CPU: it makes a
  frame only when a draw changes something, about two a second (measured on the inference host: 1.95-2.0
  screen updates a second, 0.96% of one core idle, 2.4% over 15 minutes of traffic).
- Kiosk (`deploy/panel/install-kiosk [USER]`, then enable `kiln-panel-kiosk`; the script checks cage, Chromium
  and sudo before it writes a file, and names the package to install): Chromium runs in the user scope `kiln-panel-chromium.scope`
  (MemoryHigh 512M, MemoryMax 768M, CPUWeight 10, CPUQuota 50%). Steady use is about 200 MiB and 1-2.5% of
  one core. At MemoryMax the scope stops (`OOMPolicy=stop`) and the kiosk restarts on a fresh page.
  `kiln-panel-watchdog` reads `GET /api/v1/panel/seen` (the last poll from this machine): no poll for
  90 s while the dashboard runs means a crashed or hung page, so it stops Chromium and the kiosk
  restarts. Cage exits when the pipe its client inherited hangs up, so the watchdog closes its inherited
  descriptors. The kiosk runs at nice 10 with idle I/O and `OOMScoreAdjust=300` (Chromium's lowest
  renderer score, so it never fails to set one). The touchscreen also reports a mouse, so cage would draw a
  cursor that touch never moves. A theme of transparent cursors (`blank_cursors.py`) hides it.
- `tools/build_panel_demo.py` writes a one-file demo with sample data; `tools/shoot_panel.py` takes
  exact 1424 x 280 screenshots.

### Request dialects (`request_compat`)

The gateway forwards requests unchanged, except for the model id and
`include_usage`, unless a profile names a dialect. `crucible`
(`gateway/compat.py`) removes fields that only steer caching or storage
(Crucible rejects unknown fields), handles thinking controls by the profile's
`thinking` (`drop`, the default: remove them, because the train 8 server
returns thinking inside `content` with a literal `</think>`; `pass`: map
`reasoning_effort`, Hermes' `reasoning` object and `chat_template_kwargs` to
`enable_thinking` plus `reasoning_effort` in xhigh/medium/low), removes fields
that hold their default value, renames assistant `reasoning` to
`reasoning_content`, and adds `max_tokens = default_max_tokens` when the client
sends none. Fields that change the answer stay, so the engine refuses them
openly. The gateway record lists each change by name in `adapted`, and the
response carries them in the `X-Kiln-Adapted` header.

## Cache diagnostics (no content)

A slow turn is either a client prefix change (the client edited or dropped
earlier messages, its system prompt or its tools, so any engine must read them
again) or an engine cache miss (the client kept them and the engine read them
again anyway). Kiln tells the two apart without storing content:

- Privacy rule (user decision): Kiln never records transcripts and never
  persists a hash of a message. The gateway (`gateway/prefix.py`) keeps, in
  memory only, an HMAC per message under a random key made at process start
  and never written. Per client it remembers up to 16 conversations for 6 h and
  matches each request to the one with the longest common message prefix.
- The record gets `prefix`: `conv` (a random id, not derived from content),
  `n_messages`, `prefix_msgs_kept`, `prev_n_messages`, `system_changed`,
  `tools_changed`, `new_conversation`, `prev_prompt`, `prev_completion`.
- Ingest derives `appended_tokens = prompt - prev_prompt` and
  `reread_tokens = (prompt - cached) - appended_tokens` for turns that kept the
  whole prefix. A turn is an engine miss when `prompt - cached > appended +
  4096` (the soak rule). The previous completion is not counted as appended,
  because clients often drop the reply's reasoning.
- Content-free counting cannot see one case: a chat template that renders older
  turns differently (for example it drops old reasoning) changes the token
  prefix while the messages stay the same. It shows as an engine miss.
- A tracker error drops the diagnostics of that request only and logs a fixed
  text (never an exception message, which could quote content). Bodies above
  1 MiB are hashed in a worker thread.
- Engine reports joined per request: Crucible `crucible_turn` cache fields
  (saved and tail tokens, `cache_save_refused`, entries, bytes, evictions,
  host free MiB, slot context, replay hit, session label) and Halogen's
  "closest earlier" (shared tokens, age, whether the prompt or reply differs).
  Halogen's cache housekeeping lines (kv pool evictions and moves, prompt cache
  cap evictions) become `engine_events` of kind `cache:*` that store only the
  parsed numbers, never the line; its `GET /cache` counters show on the engine
  view. A line that fails to parse is stored as a fixed reason, not its text.
- `GET /api/v1/cache` lists conversations of the last hours with each turn's
  cause, and the daily re-read total per profile.

## Speculative decoding (Crucible)

Crucible's server drafts K = 7 tokens ahead with its MTP head, and the target accepts or rejects each one.
Its `crucible_turn` line carries the counters (`collect/engines.py`); migration 013 stores them per request:

- `draft_pos_proposed`, `draft_pos_tested`, `draft_pos_accepted`: one count per position 1..7. A draft at
  position i was tested only when the target accepted every earlier one.
- `draft_len_hist`: verify rounds by draft length L = 0..7, so 8 values. L = 0 is a round with no draft (MTP
  off or an empty chain), not a round with zero accepted.
- `draft_passes` (the head passes the request took part in) and `draft_ms` (the wall time of those passes,
  shared equally by the requests in a pass). The engine times the decode loop's head passes only: warm-up,
  flush and settle passes and the sampled logits copy are outside the timer, so `draft_ms` is a trend, not
  the full draft cost.

The engine promises `sum(draft_pos_proposed) == draft_proposed`, `sum(draft_pos_accepted) ==
draft_accepted`, `accepted_i <= tested_i <= proposed_i`, `tested_1 == proposed_1` and `sum(draft_len_hist)
<= spec_rounds`. The parser checks each and drops only the field that breaks it, with one debug line: these
counters arrive from a new engine build, and one bad list must not cost the request's other metrics. The
four lists are compact JSON text in one column each, so a request needs one row and not one row per counter
(migration 013 says why), and the API sums them per position. A request without speculation, and every
row stored before the columns, keeps NULL.

`GET /api/v1/speculation` (`store/queries.py`, `draft_acceptance`) sums them per profile and period and
gives the conditional accept rate at each depth, `accepted_i / tested_i`, computed from the sums rather
than as the mean of the requests' own rates. The dashboard's Speculation panel draws it. The engine's
per-token detail counters (`draft_tally`, `draft_miss`, `draft_detail_cut`) are never read and never
stored, in any column and in the raw line: they carry token ids, and Kiln stores no content. The host
agent path does not carry these fields: Crucible runs only on the inference host, whose journal Kiln reads itself, and the
agent's record schema takes what a remote engine's ledger holds (llama.cpp counters). A remote Crucible host
needs its agent to send them; add them to `hosts/validate.py` and `hosts/ingest.py` then.

## Hosts (other inference machines)

The inference host is "this host" (`host` in kiln.toml). Other machines that run their own
engine are listed as `[hosts.<name>]` (Tailscale `node` as the full MagicDNS name, label, engine,
`profile`, canonical `model`, `hf_id`, context, slots). Kiln does not manage
their engines; it records their work and shows their live state.

- A collector, `python3 -m kiln.cli --config agent.toml agent`, runs on the
  host as the user unit `kiln-agent.service` (standard library only, pinned to
  the service cores, `Nice=10`). It only reads: the request ledgers, the
  engine log, GPU sysfs and the engine's `/health`. About every second it
  sends one `POST /api/v1/hosts/<host>/push` with a live state snapshot and up
  to 200 new request records.
- Auth: only a tailnet peer whose full MagicDNS name (`tailscale whois`
  `Node.Name` without the trailing dot, for example `remote.example.ts.net`)
  equals the host's `node` may push for it. The first label alone is not
  enough: a node shared in from another tailnet, or a device that takes the
  name after a rename elsewhere, can have the same label. Loopback, LAN and
  other tailnet devices get 403 before the body is read. The endpoint stores data only; it cannot switch
  or stop engines (actions keep their own `actions.allow_nodes` check).
- Validation (`hosts/validate.py`): a fixed schema with known keys only, exact
  types, bounded numbers, pattern-checked ids and labels, at most 200 records
  and 512 KiB per push. Records hold numbers and a client label, never
  content. A bad record is refused alone (reported back by index) so it can't
  stall the agent; a bad envelope or state refuses the push (400).
- Records become `requests` rows with `source = 'agent'`, `host = <host>`, id
  `<host>:<agent id>` (INSERT OR IGNORE, so a resent batch is a no-op), the
  host's profile and model, and the pushing node as the device: its tailnet
  address, the key the gateway gives it, taken from the authorized connection
  and never from the body, so a host can never add work to another device.
  The sender the host's gateway named is the label. They are
  priced at ingest like local requests (sticky cards; the OpenRouter card of
  the host's model comes from its `hf_id`) and spread into `series_1s` under
  the host's name.
- The agent's ledger cursor (`~/.local/state/kiln-agent/cursor.json`) advances
  only after a 200 reply, so the agent resumes after a restart and loses
  nothing while Kiln is down. Its first start imports `backfill_days` of
  history.
- Requests that the host served itself become the pushing device, with the
  client that its ledger names as the label (none for loopback, which is the
  host itself). Requests that went through this host's gateway are never
  imported: the gateway already counted them. Rows stored under older
  `<host>/...` keys move to the device at the host's first push
  (`HostIngest.adopt`). Ledger lines without token fields are imported with
  unknown (null) tokens: they count as requests and busy time but have no cost.
- Live: the dashboard keeps each host's latest snapshot. The state is
  `serving` (a request in flight), `standby` (engine up, idle), `down`
  (`/health` fails or the model loads) or `stale` (no push for more than
  10 s). The decode rate is llama.cpp's own `tg_3s` from its log, an estimate
  that trails by up to 3 s, so it carries `≈`; it is shown only while the
  line is less than 4.5 s old. llama.cpp's log has no wall clock: a line's
  time is when the agent read it.
- Views: `tick.today`, `tick.last`, the live chart and the hardware strip stay
  the inference host's own. Each host gets `tick.hosts.<name>` with its own `today` and
  `last`. The REST history views (`usage`, `requests`, `series`, `activity`)
  cover all hosts unless `?host=` names one, and `usage`/`series` can group by
  `host`. The cache diagnostics view stays the inference host's gateway traffic.

## Memory and disk (`collect/memory.py`, panel `memory`)

Every 10 s, on a worker thread, kiln-dash reads where the host's RAM goes and what the active engine keeps
on disk. The reading travels in `tick.hw.mem` and is stored with the other hardware readings in
`hw_samples`. The panel draws the newest reading as two area maps (squarified treemaps,
`js/memory_model.js`): each part's area is its share of RAM, or of the engine's disk use.

- RAM: `/proc/meminfo` plus amdgpu's GTT counter. On the APU, GTT pages are system RAM that meminfo counts
  in no other field, so the kernel's share is the remainder: MemTotal less free, GTT, page cache and anon.
- The engine's own sizes split GTT. Crucible prints at start its weight arena, PLE table size, stage arena,
  one slot's sequence storage (attention KV and recurrent state for the whole context; times the profile's
  slots; with `fixed_state_bytes=` on that line, requested from Crucible, the fixed recurrent
  part shows apart from the KV), slot checkpoints, prefill cut buffer, vision tower and draft head (`START_LINES`), and in each
  `crucible_turn` its cache content (`cache_bytes`) and pinned cache pool (`cache_pinned_bytes`, which does
  not shrink). What it prints no size for (prefill workspace, scratch rows, MTP caches, plans, the HIP
  runtime: about 5 GB on t32, from a source audit) stays "Unitemized GPU memory". Ingest keeps the latest of each per container in `ingest_offsets` (`containers.mem`),
  so a dashboard restart keeps them. A new start line drops the old cache figure. Each dashboard run also
  reads the running unit's start lines from the journal once, so a parser fix fills a size it missed
  (the BF16 PLE line); those fill gaps only. The figures count only
  while the profile's unit has processes and GTT holds the arena; otherwise GTT shows whole.
- The engine's processes: the unit's cgroup, the sibling of kiln-dash's own. Its `--state-dir` and
  `--state-disk-gib` come from the command line. The PLE file is the mapping at least as long as the
  printed PLE size. Its pages in RAM come from `cachestat(2)`, which takes about 0.2 s for the 39 GB
  table, so that part is read every 30 s. Disk rates are the deltas of `/proc/<pid>/io` over the unit.
- A reading that fails is left out, never shown as 0; an exception in the reader is logged and the tick
  goes on.

## Storage (SQLite, WAL, owned by kiln-dash)

`requests`, `request_costs`, `price_cards`, `series_1s` (per second, per
profile and client: decode tokens and prefill tokens), `prefill_chunks` (an
engine's prefill reports for requests still running, keyed by container,
request and time; a row's own are copied to it at the join and pruned after
12 h), `hw_samples`,
`engine_events`, `crucible_errors`, `ingest_offsets`, `schema_migrations`. Migrations are numbered
SQL files applied in order at startup. Every table with work or readings has a
`host` column; `requests.source` is `gateway`, `direct`, `agent` (a remote
host's agent), `utility` or `engine_pending`, and views count `gateway`,
`direct` and `agent`.

Engine log lines are joined to gateway records by container, completion and
prompt token counts, and end time (within 5 s). Engine lines with no gateway
record (traffic that bypassed the gateway) are stored as `direct` requests so
totals stay complete.

kiln-dash serves ticks from one event loop, so no read that can wait on the disk runs there. On the inference host,
other GPU jobs can push the database's pages out of memory; once, rebuilding today's usage on the
loop waited 4-16 s on page reads, and the panel showed "NO DATA" 11 times in 15 minutes. Now:
- History views (`usage`, `series`, `requests`, `activity`, `cache`, `signals`, engine switches) query on
  worker threads with their own read-only connections (`store/reader.py`). A slow query delays only its
  own reply.
- The tick's slower parts (today's totals, client names, parse errors, each remote host's today and last
  request: `queries.today_bundle`) are refreshed on a worker thread when a request or a name changes, at
  most every 2 s and at least every 30 s. The tick reads them from memory.
- Writes (ingest, settle, host pushes) stay on the loop's own connection. Their queries read only pending
  or recent rows: settle and the engine-line join use partial indexes that hold only unsettled, pending
  or unjoined rows (migration 010). Before, settle scanned `requests` every second. A test
  (`test_the_event_loops_own_queries_do_not_grow_with_history`) checks that the loop's work stays the same
  with 9,000 old rows. The same index pattern covers `requests.prefill_progress` (rows that carry their
  engine's chunk times), because the join and settle read those on the loop (migration 012).
- `MemoryLow` keeps kiln-dash (512M), kiln-gateway (64M) and the kiosk (256M) in memory when other GPU
  jobs need memory. The deploy script gives their parent cgroups 1G, because a unit's protection works only up to
  its parents'.
- `requests_host_end (host, ts_end)` lets `last_request` read a host's newest rows instead of sorting all
  of them (about 7,000 pages before).

## Live events

Gateway to dash (`127.0.0.1:8001/events`), JSON objects with a `type`:
`hello {requests}`, `req.start {id, ts, client, profile, model, max_tokens, conv}`,
`req.first_token {id, ts}`, `req.tokens {id, t: [arrival times], total}`
(every 250 ms while streaming; `total` is cumulative), `req.end {record}`.
`hello` is a reset: the receiver replaces its stream table with it.

Dash to browsers (`/api/v1/live`): `snapshot` on connect, then `tick` about
twice a second with live rates, active requests, hardware and engine state,
plus `request` when a request completes and `engine` on state changes.
Messages carry `v` (schema version). The browser marks the feed stale after
2.5 s without a snapshot or tick and drops the socket after 10 s: it opens a
new one at once, because a hung server or a half-open link never answers the
close handshake. The sticky health line names an engine that is not ready
(red when it failed), since the engine card is below a phone's first screen.

## Extension points

- New engine: add a log parser (`kiln.collect.engines.<engine>`), a profile,
  and, if it has one, a metrics probe. The gateway needs no change unless the
  engine needs a request dialect (`gateway/compat.py`).
- Crucible's server prints `crucible_turn {json}` per finished turn and
  `crucible_prefill {json}` per prefill chunk to stderr; `parse_crucible`
  reads both (null values stay unknown). Both lines name the server's request
  id, so a chunk line is matched to its turn (and so to the gateway row,
  `requests.engine_req`) without guessing. Halogen's chunk lines name no
  request id, so its reports are matched to a row by the prompt size they
  repeat, and are dropped when two requests in the window share it. A turn line can carry decode counts
  (`spec_rounds`, `draft_accepted`, later `draft_proposed` and the per-position counters above) and, for a
  think turn the server closed, `generated_tokens` and `forced_tokens` (the close
  text the server writes at once). The engine decode rate is then
  `generated_tokens / decode_s`; `completion / decode_s` overstated it.
- Crucible's server also prints `crucible_error {json}` for each server error (HTTP 500 and above, and any
  error after the stream began) and for each tool-call fallback. Ingest stores one `crucible_errors` row
  per line: time, host, profile, engine, the server's request id (only if it matches a fixed id pattern),
  status, `streamed`, a `kind` and the completion count. The line's `error` text is read for one fixed
  marker (`tool_call_fallback`) and never stored, because it is the server's message and could echo client
  input. Kinds: `server_error`, `stream_error`, `client_error`, `tool_call_fallback`. A malformed line
  becomes a `parse_error` event with a fixed reason. The gateway copies `usage.tool_call_fallback` (a
  boolean) into its record; ingest stores it as `requests.tool_call_fallback`: 1 or 0 for an engine that
  reports the flag (`FALLBACK_FLAG_ENGINES` in `collect/ingest.py`, today only Crucible), NULL (not known)
  for other engines, direct and agent rows, and rows from before the column. `GET /api/v1/signals` and the "Server signals" panel count both per day and engine.
- From Crucible train 23, the server prints
  `crucible_decode {json}` for each decoding request with speculation, at most once a second and once when
  its decode ends: `request_id`, `completion_tokens`, `spec_rounds`, `draft_proposed`, `draft_accepted`,
  `elapsed_s`, all cumulative. Ingest stores nothing (the turn line has the final counts) and notes
  `decode_drafts`; the live view finds the stream by the request number its prefill lines paired
  (`ProgressPairs.decoding`), else by the start order, and keeps the largest counts as the stream's
  `draft_turn`. A wrong field type is a parse error; an absent or null count is unknown and the line gives
  no figure; more accepted than proposed drops the line. A cancelled turn writes no final line, so the last
  value stays until the stream ends. Both engine jobs of a think turn the server closes write the first
  job's `request_id`, so a retired number stays its stream's while the stream is open
  (`ProgressPairs.pair(live=...)`), not only for `RETIRED_S`. The second job takes its own request number
  from the same counter, with no gateway start; from train 23 its `crucible_prefill` lines carry
  `turn_request_id` (the first job's number). Ingest then notes `continuation` instead of progress, and the
  start order skips that number for `EXTRA_S` (`ProgressPairs.continuation`). A decode line never moves the
  pairing anchor, so the anchor stays the newest pairing. A request that does not stream has no first token
  at the gateway: its first decode line (about 1 s into decode) ends its prefill at `ts - elapsed_s`, but
  only through the pairing its own prefill lines made. Every stream, streamed or not, takes its tokens and
  rate from its decode lines while they are recent (rule 2, `Stream.figures`).
- New panel: a frontend module registered in `web/static/js/panels/`, fed by a
  REST endpoint or a field in `tick`.
- New price source: a module in `kiln.pricing` that yields cards.
- More hosts: add `[hosts.<name>]` to kiln.toml and install the agent there
  with an `agent.toml` and a `kiln-agent.service` user unit. A host whose ledgers differ needs
  its own ledger reader in the agent; the push schema stays the same. The agent and the
  `hosts` package are not in the public package.

## Demo (`kiln demo`, `src/kiln/demo/`)

`kiln demo` runs the real gateway and the real dashboard in one process, with fakes at the edges only:
a fake Crucible server (`engine.py`), its log lines through a log source in place of the journal, a sysfs
and /proc tree for the sensors, a disk probe for the engine's files, and a command runner in place of
systemd (`edges.py`). Each fake enters through one parameter: `Dash(log_source=, start_facts=,
unit_runner=)`, `Gateway(peer=)`, `Sensors(root=, proc=)` and `MemorySampler(proc=, probe=)`. Nothing
is monkeypatched, so the demo also tests the parsers, ingest, the live state and the API end to end
(`tests/test_demo_e2e.py`, marker `demo`). A traffic generator plays a looped scenario through the
gateway (`scenario.py`), and two weeks of past records and engine lines go through the normal ingest at
the start (`history.py`). docs/DEMO.md has the details.
