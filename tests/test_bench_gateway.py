"""The gateway bench keeps working: a fast run (N=10) against a real gateway subprocess."""
import importlib.util
import json
import math
from pathlib import Path

import pytest

# About 25 s: CI runs it with -m bench; the everyday run leaves it out.
pytestmark = pytest.mark.bench

BENCH = Path(__file__).resolve().parents[1] / "tools" / "bench_gateway.py"


def load_bench():
    spec = importlib.util.spec_from_file_location("bench_gateway", BENCH)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def test_percentile_interpolates():
    bench = load_bench()
    assert bench.pct([1, 2, 3, 4, 5], 50) == 3
    assert bench.pct([0, 10], 25) == 2.5
    assert bench.pct([7], 99) == 7


def test_bodies_have_the_requested_size():
    bench = load_bench()
    small, large = bench.Bodies(1024, seed=1), bench.Bodies(200 * 1024, seed=1)
    assert 700 <= len(small.make(0)) <= 1500
    assert 190 * 1024 <= len(large.make(0)) <= 215 * 1024
    assert json.loads(large.make(1))["messages"][-1]["content"].startswith("turn 1:")


def test_machine_names_only_a_machine_class_unless_asked_for_detail():
    bench = load_bench()
    m = bench.machine()
    assert m["cpu"] in ("x86-64", "arm64", "other") and m["os"] == bench.platform.system()
    assert "logical_cpus" not in m and "cpu_governor" not in m
    detail = bench.machine(detail=True)
    assert detail["logical_cpus"] == bench.os.cpu_count()
    assert detail["cpu"] != m["cpu"] or detail["cpu"] == "unknown"
    assert detail["os"].startswith(bench.platform.system() + " ") and "cpu_governor" in detail


def test_fast_run_writes_results(tmp_path):
    bench = load_bench()
    assert bench.main(["--fast", "--cases", "small,large", "--stall-iterations", "1", "--out-dir", str(tmp_path)]) == 0
    result = json.loads((tmp_path / "gateway-overhead.json").read_text())
    assert result["machine"]["detail"] is False and "test machine (" in (tmp_path / "gateway-overhead.md").read_text()
    (run,) = result["runs"]
    for case in ("small", "large"):
        added = run["latency"][case]["added"]
        assert all(math.isfinite(added[m][q]) for m in added for q in ("p50", "p99", "max"))
        assert run["latency"][case]["through"]["ttft"]["n"] == 10
        assert run["latency"][case]["through"]["ttft"]["p50"] > run["latency"][case]["direct"]["ttft"]["p50"] - 5
    assert run["records_written"] == run["through_requests"]
    assert run["concurrency"]["small"]["through"]["aggregate_tok_s"] > 0
    assert "Added time to first token" in (tmp_path / "gateway-overhead.md").read_text()
    assert "machine" in result and "host" not in result["machine"]


def test_the_report_names_no_cpu_count_without_machine_detail():
    bench = load_bench()
    m = {"cpu": "x86-64", "python": "3.13.0", "aiohttp": "3.14.0", "os": "Linux", "kiln_commit": "abc1234",
         "detail": False}
    line = bench.measured_line(m)
    assert "logical CPUs" not in line and "a test machine (x86-64, Linux)" in line
