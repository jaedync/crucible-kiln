"""A fake Crucible server for install tests: the public server's command line, Kiln's demo engine behind it.

Run it as the real one runs: `python -m server CKPT --engine build/engine/crucible --port N` from the
checkout. It needs aiohttp and Kiln's `kiln.demo.engine`: the installed `kiln` package, else the `src/`
directory of the Kiln tree around this fixture (or $KILN_SRC). It loads no weights and needs no GPU.

It prints on stderr what the real server and engine print there and Kiln reads from the journal: the
engine's start lines, `Crucible vX`, `Crucible is ready at http://HOST:PORT/v1 (...)`, and one
crucible_prefill, crucible_decode and crucible_turn line per request step, in the real formats.
"""
from __future__ import annotations

import argparse
import asyncio
import os
import signal
import sys
import time
from pathlib import Path

VERSION = "0.33.1-fake"
IGNORED_SWITCHES = ("--serial", "--clamp-max-tokens", "--vision", "--log-engine-errors", "--draft-stats")
IGNORED_VALUES = (
    "--prefill", "--host-reserve-mib", "--short-streams", "--short-ctx", "--packed-op", "--prefill-interleave",
    "--interleave-min-rows", "--session-replay", "--slot-resume", "--slot-checkpoints", "--cache-probation",
    "--cache-idle-minutes", "--state-disk-gib", "--state-disk-write-gib-per-day", "--state-disk-write-delay-ms",
    "--temperature", "--top-k", "--top-p", "--min-p", "--default-max-tokens", "--max-body-mib",
    "--vision-max-pixels", "--vision-max-images", "--vision-max-image-mib", "--thinking-answer-room",
    "--min-thinking-tokens", "--max-thinking-tokens", "--stream-tool-calls", "--end-of-turn-guard",
    "--max-requests-per-client", "--content-special-tokens", "--lookup", "--mtp-window", "--draft-capture",
    "--draft-capture-mode", "--draft-capture-max-gib", "--draft-blocks", "--default-thinking")


def _import_engine():
    try:
        from kiln.demo import engine
    except ImportError:
        src = os.environ.get("KILN_SRC") or str(Path(__file__).resolve().parents[4] / "src")
        sys.path.insert(0, src)
        from kiln.demo import engine
    return engine


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    """The flags of the public server. An unknown flag fails, as with the real one."""
    p = argparse.ArgumentParser(prog="python -m server", description="Fake Crucible server (no GPU)")
    p.add_argument("checkpoint", type=Path)
    p.add_argument("--version", action="version", version=f"crucible {VERSION}")
    p.add_argument("--no-update-check", action="store_true")
    p.add_argument("--engine", type=Path, default=Path(__file__).resolve().parents[1] / "build/engine/crucible")
    p.add_argument("--ctx", type=int, default=32768)
    p.add_argument("--model-id", default="crucible")
    p.add_argument("--host", default="127.0.0.1")
    p.add_argument("--port", type=int, default=8790)
    p.add_argument("--streams", type=int, choices=range(1, 9), default=None)
    p.add_argument("--conversation-cache-mib", type=int, default=0)
    p.add_argument("--state-dir", type=Path, default=None)
    p.add_argument("--allowed-host", action="append", default=[])
    p.add_argument("--client-request-id-header", default=None)
    # The other flags of the public server (server/__main__.py), so its documented run commands start the fake.
    # They change nothing here: the fake has no engine to tune.
    for flag in IGNORED_SWITCHES:
        p.add_argument(flag, action="store_true")
    for flag in IGNORED_VALUES:
        p.add_argument(flag)
    return p.parse_args(argv)


def log(line: str, _ts: float | None = None) -> None:
    print(line, file=sys.stderr, flush=True)


async def serve(args: argparse.Namespace) -> int:
    engine = _import_engine()
    engine.SERVED_ID = args.model_id  # the fake answers to the server's --model-id, as the real one does
    pack = engine.PACKS["3.05" if "3.05" in args.checkpoint.name else "4.05"]
    load_s = float(os.environ.get("FAKE_CRUCIBLE_LOAD_S", "1.0"))
    fake = engine.FakeEngine(pack, args.port, emit=log, seed=args.port, clock=time.time)
    stop = asyncio.Event()
    loop = asyncio.get_running_loop()
    for sig in (signal.SIGTERM, signal.SIGINT):
        loop.add_signal_handler(sig, stop.set)
    await fake.start(load_s=load_s)
    log(f"Crucible v{VERSION}")
    log(f"Crucible is ready at http://{args.host}:{args.port}/v1 (content special tokens: off)")
    log("Host check: off (any Host)")
    await stop.wait()
    await fake.stop()
    return 0


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    if not args.engine.is_file():
        log(f"server error: engine not found: {args.engine}")
        return 1
    if not args.checkpoint.is_dir():
        log(f"server error: checkpoint not found: {args.checkpoint}")
        return 1
    if args.host not in ("127.0.0.1", "::1", "localhost"):
        log(f"WARNING: listening on {args.host}, which is not loopback; the fake serves on 127.0.0.1 only.")
    return asyncio.run(serve(args))


if __name__ == "__main__":
    sys.exit(main())
