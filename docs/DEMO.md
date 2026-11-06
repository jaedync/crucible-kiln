# kiln demo

`kiln demo` shows the real Kiln dashboard with one command. It needs no GPU, no engine, no systemd and no
journal. It runs on Linux and macOS.

## Run it

1. Install Kiln (`uv tool install git+https://github.com/jaedync/crucible-kiln`, or `uv sync` in a checkout).
2. Run `kiln demo`.
3. Open the URL that it prints (`http://127.0.0.1:7000/`).
4. Press Ctrl-C to stop it.

Options:

| Option | Default | What it does |
|---|---|---|
| `--port N` | 7000 | The dashboard port on 127.0.0.1. |
| `--speed X` | 1.0 | Plays the scenario X times faster. Times and token counts become smaller. Rates stay real. |
| `--seed N` | 0 | The same seed gives the same token counts, so screenshots repeat. |
| `--once` | off | Plays the scenario one time and exits. The exit code is 0 when all requests succeed. |
| `--no-history` | off | Starts with an empty store. By default the demo adds two weeks of past work. |

The demo binds only 127.0.0.1. It keeps its files in a temporary folder and removes that folder at exit.
It also prints the URL of its OpenAI-compatible gateway. You can point a client at it (model
`qwen3.8-flash-next`). The fake engine then answers with filler text.

## What is real and what is fake

The demo runs the real `kiln gateway` code (`kiln.gateway.app.Gateway`) and the real `kiln dash` code
(`kiln.dash.app.Dash`). This includes ingest, the live state, the store, the REST and WebSocket API, the
engine manager and the web UI. It does not use `tools/mock_dash.py`.

Only the edges are fake. Each fake feeds the real reader through one parameter:

| Edge | Fake (`src/kiln/demo/`) | Seam |
|---|---|---|
| The engine (Crucible) | `engine.FakeEngine`: chat completions with SSE and usage, `/v1/models`, `/health` | A real HTTP server on 127.0.0.1 |
| The engine log (journal) | `edges.LogBus`: the engine's `crucible_prefill`, `crucible_decode`, `crucible_turn` and start lines | `Dash(log_source=, start_facts=)` |
| GPU and CPU sensors (sysfs) | `edges.FakeHardware`: a sysfs tree with k10temp, amdgpu and cros_ec files | `Sensors(root=, proc=)` |
| RAM parts (/proc) | `edges.FakeHardware`: `/proc/meminfo`; `edges.FakeProbe`: the engine's disk use | `MemorySampler(proc=, probe=)` |
| systemd | `edges.FakeUnits`: `systemctl --user` and `podman ps` for the fake engines | `Dash(unit_runner=)`, then `Manager(runner=)` |
| Client addresses | `run.demo_peer`: the three demo devices at RFC 5737 addresses | `Gateway(peer=)` |
| Traffic | `scenario.Traffic`: the looped scenario through the gateway | None (a normal HTTP client) |

The fake lines use the formats of `collect/engines.py` and `collect/memory.py`. The tests parse them with
that code (`tests/test_demo.py`).

## The scenario

One loop takes about 3.6 minutes at speed 1. Then it starts again with new conversations.

1. The engine is idle.
2. The "laptop" sends a cold prompt of about 120K tokens. Prefill runs at about 1,790 tok/s, with a
   progress report every 4,096 tokens.
3. The laptop decodes alone at 56 to 72 tok/s, with draft acceptance.
4. The "desktop" and the "agent" join. Three of the four slots decode at the same time, at about
   115 tok/s in total.
5. The laptop sends the same prompt again. The conversation cache gives the first token in about 20 ms.
6. The agent makes a tool call, then sends the tool result.
7. The engine is idle again.

The numbers come from the v0.33.0 table of the public Crucible README (4.05 pack: prefill 1,789 to
1,851 tok/s, decode 56 to 72 tok/s at 32K, 4 sessions about 114 tok/s in total, a re-sent prompt gets
its first token in 0.020 s). The memory map is that of a 4 x 256K engine on a 128 GB machine: a weight
arena of 69.4 GB, sequence storage of 6,996,972,544 bytes for each slot, a conversation cache budget of
10,171,187,200 bytes and a PLE table of 102,400,491,520 bytes.

The prompts and replies are synthetic filler. The client names are neutral: "laptop", "desktop" and
"agent". Kiln stores no content in any case.

## Switch engines

The engine card lists two profiles: the 4.05 pack and the 3.05 pack. The switch button works. The fake
stop takes about 3 s, and the fake load takes about 6 s. Then the manager sends its test generation.
Requests that arrive during the switch wait, as on a real machine.

## The end-to-end test

`tests/test_demo_e2e.py` starts `kiln demo --speed 8 --seed 7 --once` in a subprocess. It reads
`/api/v1/state` and the live WebSocket. It checks for a prefill with progress, a decode with a rate, a
cache hit, a tool call, the three clients, the memory map and the history. It takes about 30 s.

- Run only this test: `uv run pytest -m demo`.
- Run all tests except this one: `uv run pytest -m "not demo"`.

## Screenshots

Use Playwright on a running demo. Wait for a state on the page, then take the screenshot. For example,
`document.querySelectorAll('.stream[data-phase=decode]').length >= 3` is true when three streams decode.
Use the same `--seed` for the same numbers.
