"""Peak Python memory of the gateway's body work (parse, prefix digests, rewrite, encode) per body shape.

Sizes the default body cap and the gateway's MemoryMax (security review M3). Run:
    uv run python tools/measure_body_memory.py
Each case runs in a fresh process so one peak does not hide the next. Images are random bytes of a
realistic size (random data does not compress, as JPEG data barely does).
"""
from __future__ import annotations

import base64
import json
import os
import subprocess
import sys

MIB = 1 << 20


def image(nbytes: int, seed: int) -> str:
    return "data:image/jpeg;base64," + base64.b64encode(os.urandom(nbytes)).decode()


def vision(n: int, nbytes: int) -> bytes:
    content = [{"type": "text", "text": "What differs between these photos?"}]
    content += [{"type": "image_url", "image_url": {"url": image(nbytes, i)}} for i in range(n)]
    return json.dumps({"model": "m", "stream": True, "messages": [{"role": "user", "content": content}]}).encode()


def shape(kind: str, size: int) -> bytes:
    if kind == "ints":
        n = size // 2
        return b'{"model":"m","messages":[{"role":"user","content":"x"}],"x":[' + b",".join([b"1"] * n) + b"]}"
    if kind == "msgs":
        one = b'{"role":"user","content":"x"}'
        n = size // (len(one) + 1)
        return b'{"model":"m","stream":true,"messages":[' + b",".join([one] * n) + b"]}"
    if kind in ("text", "emoji"):
        lead = "\U0001F600" if kind == "emoji" else ""  # one character above U+FFFF: 4 bytes per character
        msg = {"role": "user", "content": lead + "lorem ipsum, dolor sit amet; " * (size // 30)}
        return json.dumps({"model": "m", "stream": True, "messages": [msg]}, ensure_ascii=False).encode()
    raise ValueError(kind)


CASES = {
    "4 screenshots of 0.8 MB": lambda: vision(4, 800_000),
    "4 phone photos of 4 MB": lambda: vision(4, 4_000_000),
    "8 phone photos of 4 MB": lambda: vision(8, 4_000_000),
    "text 16 MiB": lambda: shape("text", 16 * MIB),
    "text 16 MiB with one emoji": lambda: shape("emoji", 16 * MIB),
    "tiny messages 16 MiB": lambda: shape("msgs", 16 * MIB),
    "int array 16 MiB": lambda: shape("ints", 16 * MIB),
}


def child(name: str) -> None:
    import tracemalloc
    from kiln import config
    from kiln.gateway.app import Gateway
    raw = CASES[name]()
    cfg = config.parse({"host": "t", "gateway": {"data_dir": "/tmp/kiln-measure-gw", "state_file": "/tmp/kiln-measure/a.json"},
                        "dash": {"db": "/tmp/kiln-measure/k.db"}, "profiles": {"p": {
                            "engine": "crucible", "model": "m", "port": 9, "context": 1, "native": {"ExecStart": "x"},
                            "request_compat": "crucible", "default_max_tokens": 100}}})
    gw = Gateway(cfg)
    from kiln.gateway.body import parse
    tracemalloc.start()
    base = tracemalloc.get_traced_memory()[0]
    body, layout = parse(raw)
    out = gw._prepare(body, layout, cfg.profiles["p"], os.environ.get("TRACK", "1") == "1")
    peak = tracemalloc.get_traced_memory()[1] - base
    del out, body, layout
    print(json.dumps({"case": name, "body_mib": round(len(raw) / MIB, 2), "peak_mib": round(peak / MIB, 1),
                      "x_body": round(peak / len(raw), 1)}))


if __name__ == "__main__":
    if len(sys.argv) > 1:
        child(sys.argv[1])
    else:
        for name in CASES:
            subprocess.run([sys.executable, __file__, name], check=True)
