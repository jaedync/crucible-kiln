"""Summarize py-spy raw (collapsed) stacks for one process of a `--subprocesses` recording.

Usage: pyspy_summary.py RAW --proc "kiln.cli" [--rate 1000] [--requests N] [--top 40]

Prints inclusive and self time per function in ms per request, so a profile of the gateway
during the bench reads as a cost table. Only stack frame names are read; no content.
"""
from __future__ import annotations

import argparse
import collections
import re


def frames_of(line: str, proc: str):
    stack, _, count = line.rstrip("\n").rpartition(" ")
    parts = stack.split(";")
    for i, p in enumerate(parts):
        if p.startswith("process ") and proc in p:
            return parts[i + 1:], int(count)
    return None, 0


def short(frame: str) -> str:
    # "func (path/file.py:123)" -> "func (path/file.py)" so lines of one function merge
    return re.sub(r":\d+\)$", ")", frame)


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("raw")
    ap.add_argument("--proc", default="kiln.cli")
    ap.add_argument("--rate", type=float, default=1000)
    ap.add_argument("--requests", type=int, default=1)
    ap.add_argument("--top", type=int, default=40)
    ap.add_argument("--lines", action="store_true", help="keep line numbers")
    a = ap.parse_args()
    incl, self_ = collections.Counter(), collections.Counter()
    total = 0
    with open(a.raw) as f:
        for line in f:
            fr, n = frames_of(line, a.proc)
            if fr is None:
                continue
            total += n
            names = fr if a.lines else [short(x) for x in fr]
            for name in set(names):
                incl[name] += n
            if names:
                self_[names[-1]] += n
    per = 1000 / a.rate / a.requests
    print(f"samples {total}, {total * per:.2f} ms per request over {a.requests} requests\n")
    print("inclusive ms/request  function")
    for name, n in incl.most_common(a.top):
        print(f"{n * per:8.3f}  {name}")
    print("\nself ms/request  function")
    for name, n in self_.most_common(a.top):
        print(f"{n * per:8.3f}  {name}")


if __name__ == "__main__":
    main()
