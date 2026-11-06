"""tests/fixtures/fake_crucible: a stand-in for a checkout of the public Crucible repo, for the install tests.

It has the checkout's shape (`server/` run as `python -m server CKPT --engine build/engine/crucible --port N`
and an executable `build/engine/crucible`), and its server is the demo's fake engine. It prints the lines
that the real server prints on stderr, so a native unit of it fills the journal as the real engine does.
"""
import json
import os
import socket
import subprocess
import sys
import time
import urllib.request
from pathlib import Path

from kiln.collect import engines, memory
from kiln.install import find_crucible

FAKE = Path(__file__).parent / "fixtures" / "fake_crucible"


def free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def test_the_fixture_has_the_shape_of_a_crucible_checkout():
    found = find_crucible(FAKE, sys.executable)
    assert found.engine == FAKE.resolve() / "build" / "engine" / "crucible"
    assert os.access(found.engine, os.X_OK)


def _wait_ready(proc, deadline: float) -> list[str]:
    lines = []
    while time.monotonic() < deadline:
        line = proc.stderr.readline()
        if not line:
            break
        lines.append(line.rstrip("\n"))
        if line.startswith("Crucible is ready at "):
            return lines
    raise AssertionError(f"no ready line; stderr so far: {lines[-5:]}")


def test_the_server_serves_a_streamed_chat_and_prints_the_engine_lines(tmp_path):
    port = free_port()
    pack = tmp_path / "Qwen3.8-Flash-Next-EXL3-4.05bpw"
    pack.mkdir()
    env = {k: v for k, v in os.environ.items() if not k.startswith("PYTHON")}
    proc = subprocess.Popen([sys.executable, "-m", "server", str(pack), "--engine", "build/engine/crucible",
                             "--ctx", "32768", "--streams", "1", "--port", str(port)],
                            cwd=FAKE, env=env, stderr=subprocess.PIPE, text=True)
    try:
        start = _wait_ready(proc, time.monotonic() + 30)
        facts = {}
        for line in start:
            facts.update(memory.engine_facts(line) or {})
        assert facts.get("weights_bytes"), start
        assert start[-1] == f"Crucible is ready at http://127.0.0.1:{port}/v1 (content special tokens: off)"
        with urllib.request.urlopen(f"http://127.0.0.1:{port}/v1/models", timeout=5) as r:
            assert json.load(r)["data"][0]["id"] == "crucible"
        body = {"model": "crucible", "stream": True, "max_tokens": 40,
                "messages": [{"role": "user", "content": "hello " * 2000}]}
        req = urllib.request.Request(f"http://127.0.0.1:{port}/v1/chat/completions", data=json.dumps(body).encode(),
                                     headers={"Content-Type": "application/json"})
        with urllib.request.urlopen(req, timeout=30) as r:
            events = [ln for ln in r.read().decode().splitlines() if ln.startswith("data: ")]
        assert events[-1] == "data: [DONE]"
        assert "usage" in json.loads(events[-2][6:])
        seen = set()
        deadline = time.monotonic() + 10
        while time.monotonic() < deadline and "crucible_turn" not in seen:
            line = proc.stderr.readline()
            kind = line.split(" ", 1)[0]
            if kind.startswith("crucible_"):
                seen.add(kind)
                assert engines.parse_crucible(line.rstrip("\n")) is not None, line
        assert {"crucible_prefill", "crucible_decode", "crucible_turn"} <= seen
    finally:
        proc.terminate()
        proc.wait(10)
    assert proc.returncode in (0, -15)


def test_a_wrong_flag_fails_as_the_real_cli_does(tmp_path):
    proc = subprocess.run([sys.executable, "-m", "server", str(tmp_path), "--no-such-flag"], cwd=FAKE,
                          capture_output=True, text=True, timeout=30)
    assert proc.returncode == 2
    assert "unrecognized arguments" in proc.stderr


def test_it_reports_a_fake_version_of_the_current_server():
    proc = subprocess.run([sys.executable, "-m", "server", "--version"], cwd=FAKE, capture_output=True, text=True,
                          timeout=30)
    assert proc.returncode == 0 and proc.stdout.strip() == "crucible 0.33.1-fake"


def test_it_accepts_the_flags_of_the_public_run_commands():
    # The Crucible QUICKSTART's full run command, and flags the README and docs name.
    sys.path.insert(0, str(FAKE))
    try:
        from server.__main__ import parse_args
    finally:
        sys.path.remove(str(FAKE))
    args = parse_args(["/packs/Model-EXL3", "--engine", "build/engine/crucible", "--streams", "4", "--ctx", "262144",
                       "--prefill", "4096", "--conversation-cache-mib", "9700", "--host-reserve-mib", "11264",
                       "--state-dir", "/home/user/.cache/crucible/states", "--state-disk-gib", "200", "--vision",
                       "--clamp-max-tokens", "--default-thinking", "on", "--max-body-mib", "16", "--serial",
                       "--slot-resume", "1", "--session-replay", "0", "--slot-checkpoints", "1",
                       "--stream-tool-calls", "off", "--min-thinking-tokens", "64", "--max-requests-per-client", "4",
                       "--no-update-check", "--port", "8791"])
    assert args.port == 8791 and args.ctx == 262144 and args.streams == 4
