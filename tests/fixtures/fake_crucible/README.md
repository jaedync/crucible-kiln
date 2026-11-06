# Fake Crucible checkout

This directory has the shape of a checkout of the public Crucible repository. The install tests use it in
place of a real checkout, on machines without a GPU.

- `server/` runs as `python -m server CKPT --engine build/engine/crucible --port N`, and it accepts every
  flag of the public server (most change nothing here). `--version` prints `crucible 0.33.1-fake`. Its server is Kiln's demo engine (`src/kiln/demo/engine.py`).
- `build/engine/crucible` is an executable stand-in for the engine binary. The fake server does not run it.

The server needs `aiohttp` and the `kiln` package (installed, or the `src/` directory of this tree). Make a
`.venv` here as the Crucible quick start does:

```sh
python3 -m venv .venv
.venv/bin/python -m pip install aiohttp
```

`kiln init --crucible <this directory> --pack <a directory with a config.json>` then writes a profile that runs it. The
server prints the engine's start lines and one `crucible_prefill`, `crucible_decode` and `crucible_turn`
line for each request step, as the real server does. `FAKE_CRUCIBLE_LOAD_S` sets the load time (default 1 s).
