"""Before/after table from two bench_gateway.py JSON files: median of the repeats (lowest to highest).

Usage: bench_compare.py BEFORE.json AFTER.json
"""
import json
import sys


def spread(vals, d=2):
    v = sorted(vals)
    return f"{v[len(v) // 2]:.{d}f} ({v[0]:.{d}f} to {v[-1]:.{d}f})"


def main(before_path: str, after_path: str) -> None:
    b, a = json.load(open(before_path)), json.load(open(after_path))

    def row(label, metric, f, d=2):
        rows.append((label, metric, spread([f(r) for r in b["runs"]], d), spread([f(r) for r in a["runs"]], d)))

    rows = []
    for case, label in (("small", "1 KB"), ("large", "597 KB"), ("xlarge", "2049 KB")):
        for metric, (k, q) in (("Added time to first token, p50", ("ttft", "p50")),
                               ("Added time to first token, p99", ("ttft", "p99")),
                               ("Added time to first token, max", ("ttft", "max")),
                               ("Added delay between tokens, p99", ("gap", "p99")),
                               ("Added total request time, p50", ("total", "p50"))):
            row(label, metric, lambda r, c=case, k=k, q=q: r["latency"][c]["added"][k][q])
        row(label, "Gateway CPU per request", lambda r, c=case: r["latency"][c]["gateway_cpu_ms_per_request"], 1)
    for case, label in (("small", "4 streams, 1 KB"), ("large", "4 streams, 597 KB")):
        row(label, "Added time to first token, p50", lambda r, c=case: r["concurrency"][c]["added_ttft_ms"]["p50"])
        row(label, "Added time to first token, p99", lambda r, c=case: r["concurrency"][c]["added_ttft_ms"]["p99"])
        row(label, "Through / direct token rate", lambda r, c=case: r["concurrency"][c]["rate_ratio"], 4)
    for kind, label in (("small", "Stall probe, small arrivals"), ("large", "Stall probe, 597 KB arrivals")):
        row(label, "Largest probe gap", lambda r, k=kind: r["stall"][k]["gap"]["max"])
        row(label, "Worst gap per probe, median", lambda r, k=kind: r["stall"][k]["worst_gap_per_probe"]["p50"])
        row(label, "Probe gap p99", lambda r, k=kind: r["stall"][k]["gap"]["p99"])
        row(label, "Gaps over 30 ms", lambda r, k=kind: r["stall"][k]["gaps_over_30ms"], 0)
    print("| Case | Metric | Before | After |")
    print("|---|---|---|---|")
    for r in rows:
        print("| " + " | ".join(r) + " |")
    for name, res in (("before", b), ("after", a)):
        print(f"\n{name}: load average " + ", ".join(f"{r['loadavg_start']:.1f} to {r['loadavg_end']:.1f}"
                                                for r in res["runs"])
              + "; records written / requests " + ", ".join(f"{r['records_written']}/{r['through_requests']}"
                                                          for r in res["runs"]))


if __name__ == "__main__":
    main(*sys.argv[1:3])
