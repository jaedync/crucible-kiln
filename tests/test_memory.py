import os
from pathlib import Path

from kiln.collect import memory as mem

# A 128 GB machine with the t32 engine serving (kB), trimmed to the fields the split reads.
MEMINFO = """MemTotal:       131156752 kB
MemFree:        11647260 kB
MemAvailable:   14570852 kB
Buffers:           71508 kB
Cached:          3770712 kB
AnonPages:       1337060 kB
Shmem:              9576 kB
HugePages_Total:       0
"""
GTT = 115_313_750_016
UNIT = "kiln-crucible-x.service"
KB = 1024


def test_engine_facts_read_sizes_from_crucible_start_lines():
    assert mem.engine_facts("weight payload 68997727232 bytes, arena reserved 69428838400 bytes, staging peak 1"
                            ) == {"weights_bytes": 69428838400}
    assert mem.engine_facts("PLE table on host: 39040187392 bytes not resident, 1 segment(s)") == {
        "ple_bytes": 39040187392}
    # Since the BF16 n-gram table (t33), the line names the table's type before its size.
    assert mem.engine_facts("PLE table on host: BF16, 102400491520 bytes not resident, 1 segment(s), "
                            "ngram_embedding_bf16.safetensors") == {"ple_bytes": 102400491520}
    assert mem.engine_facts("conversation cache: 10171187200 bytes budget, host reserve 11264 MiB") == {
        "cache_budget_bytes": 10171187200}
    assert mem.engine_facts("conversation cache: host floor 11264 MiB, spill slack 2048 MiB") is None
    assert mem.engine_facts("stage arena: 2174.2 MiB (shared; attention 2174.2 MiB, MoE 1995.5 MiB)") == {
        "stage_bytes": round(2174.2 * 2 ** 20)}
    assert mem.engine_facts("sequence storage: CRUCIBLE_COMPACT_STATE=1 raw_slots=16 mtp=lazy "
                            "logical_state_bytes=6996972544") == {"slot_state_bytes": 6996972544}
    assert mem.engine_facts("slot checkpoints: 3 per slot, 152105984 bytes each, 1837191168 bytes from the cache "
                            "budget") == {"checkpoint_bytes": 1837191168}
    assert mem.engine_facts("slot checkpoints: end outputs, 11919360 bytes of logits rows in that budget") is None
    assert mem.engine_facts("prefill cut buffer: 152065024 bytes from the cache budget") == {"cut_bytes": 152065024}
    assert mem.engine_facts("vision tower 0.35 GB loaded in 1.5 s") == {"vision_bytes": 350_000_000}
    assert mem.engine_facts("draft head 65536 tokens, 125.96 MB, original EXL3 blocks") == {
        "draft_bytes": 125_960_000}
    assert mem.engine_facts('crucible_turn {"request_id": 1}') is None


def test_parse_meminfo_gives_bytes_and_keeps_unitless_counts():
    mi = mem.parse_meminfo(MEMINFO)
    assert mi["MemTotal"] == 131156752 * KB and mi["HugePages_Total"] == 0


# The t32 engine's start sizes, and its pinned cache pool after 20 minutes of work.
T32 = {"weights_bytes": 69_428_838_400, "slot_state_bytes": 6_996_972_544, "stage_bytes": 2_279_813_939,
       "checkpoint_bytes": 1_837_191_168, "cut_bytes": 152_065_024, "vision_bytes": 350_000_000,
       "draft_bytes": 125_960_000, "cache_bytes": 7_561_598_112, "cache_pinned_bytes": 8_178_892_800}


def test_ram_bands_add_up_to_total_and_split_gtt_by_engine_figures():
    mi = mem.parse_meminfo(MEMINFO)
    b = mem.ram_bands(mi, GTT, T32, slots=4, ple_ram=1_747_673_088)
    parts = sum(b[k] for k in mem.RAM_BANDS)
    assert parts == b["total"] == mi["MemTotal"]
    assert b["weights"] == 69_428_838_400 and b["kv_state"] == 4 * 6_996_972_544
    assert b["cache"] == 7_561_598_112 and b["cache_free"] == 8_178_892_800 - 7_561_598_112
    assert b["checkpoints"] == 1_837_191_168 + 152_065_024 and b["stage"] == 2_279_813_939
    assert b["vision_draft"] == 475_960_000
    # What the engine prints no size for: work buffers, MTP caches, plans, the HIP runtime. About 5 GB here.
    assert 4.5e9 < b["gpu_other"] < 5.5e9
    assert b["ple_ram"] == 1_747_673_088
    assert b["file_cache"] == (3770712 + 71508 - 9576) * KB - 1_747_673_088
    assert b["programs"] == (1337060 + 9576) * KB
    # What meminfo leaves after free, GTT, page cache and anonymous memory: slab, page tables, and so on.
    assert 1.5e9 < b["kernel"] < 2e9


def test_engine_facts_read_the_fixed_state_beside_the_slot_size():
    # Asked of Crucible: the fixed recurrent part of one slot's storage.
    assert mem.engine_facts("sequence storage: CRUCIBLE_COMPACT_STATE=1 raw_slots=16 mtp=lazy "
                            "logical_state_bytes=6996972544 fixed_state_bytes=151770112") == {
        "slot_state_bytes": 6996972544, "slot_fixed_bytes": 151770112}


def test_gpu_parts_split_kv_from_the_recurrent_state_when_the_engine_prints_it():
    b = mem.gpu_parts(GTT, {**T32, "slot_fixed_bytes": 151_770_112}, 4)
    assert b["kv"] == 4 * (6_996_972_544 - 151_770_112) and b["recurrent"] == 4 * 151_770_112
    assert b["kv_state"] == 0 and sum(b.values()) == GTT
    whole = mem.gpu_parts(GTT, T32, 4)  # no split printed: one combined part
    assert whole["kv"] == whole["recurrent"] == 0 and whole["kv_state"] == 4 * 6_996_972_544
    odd = mem.gpu_parts(GTT, {**T32, "slot_fixed_bytes": 8e9}, 4)  # larger than the slot: not believed
    assert odd["kv"] == odd["recurrent"] == 0 and odd["kv_state"] == 4 * 6_996_972_544


def test_ram_bands_without_engine_figures_keep_gtt_whole():
    b = mem.ram_bands(mem.parse_meminfo(MEMINFO), GTT)
    assert b["weights"] == b["cache"] == b["kv_state"] == 0 and b["gpu_other"] == GTT


def test_gpu_parts_refuse_an_arena_larger_than_gtt_and_never_exceed_it():
    assert mem.gpu_parts(int(1e9), T32, 4)["gpu_other"] == 1e9  # the engine is not loaded now
    b = mem.gpu_parts(int(80e9), T32, 4)
    assert sum(b.values()) == 80e9 and b["weights"] == T32["weights_bytes"] and b["gpu_other"] == 0
    assert b["kv_state"] == 80e9 - T32["weights_bytes"] and b["cache"] == 0


def test_gpu_parts_without_the_pool_figure_use_the_cache_bytes():
    b = mem.gpu_parts(GTT, {"weights_bytes": 69e9, "cache_bytes": 5e9}, None)
    assert b["cache"] == 5e9 and b["cache_free"] == 0 and b["kv_state"] == 0


def test_ram_bands_need_meminfo():
    assert mem.ram_bands({"MemTotal": 1}, GTT) is None


def test_tree_bytes_counts_allocated_blocks(tmp_path):
    (tmp_path / "a").mkdir()
    (tmp_path / "a" / "x").write_bytes(b"1" * 10000)
    (tmp_path / "y").write_bytes(b"2" * 5000)
    expect = sum(os.stat(p).st_blocks * 512 for p in (tmp_path / "a" / "x", tmp_path / "y"))
    assert mem.tree_bytes(tmp_path) == expect > 0
    assert mem.tree_bytes(tmp_path / "missing") is None


def test_page_cache_bytes_reads_a_file_it_just_wrote(tmp_path):
    p = tmp_path / "f"
    p.write_bytes(b"1" * (64 * 1024))
    got = mem.page_cache_bytes(p)
    assert got is None or got > 0  # None only on a kernel without cachestat
    assert mem.page_cache_bytes(tmp_path / "missing") is None


class Clock:
    def __init__(self):
        self.t = 100.0

    def __call__(self):
        return self.t


def fake_host(tmp_path, ple_size=4096 * 3, with_engine=True):
    """A /proc and cgroup tree with kiln-dash and one engine unit of two processes."""
    proc, cg = tmp_path / "proc", tmp_path / "cg"
    app = cg / "user.slice/user@1000.service/app.slice"
    (proc / "self").mkdir(parents=True)
    (proc / "self/cgroup").write_text("0::/user.slice/user@1000.service/app.slice/kiln-dash.service\n")
    (proc / "meminfo").write_text(MEMINFO)
    states = tmp_path / "states"
    (states / "run").mkdir(parents=True)
    (states / "run" / "block").write_bytes(b"k" * 8192)
    ple = tmp_path / "ngram_embedding.safetensors"
    ple.write_bytes(b"p" * ple_size)
    (app / "kiln-dash.service").mkdir(parents=True)
    if with_engine:
        unit = app / UNIT
        (unit / "payload").mkdir(parents=True)
        (unit / "cgroup.procs").write_text("10\n")
        (unit / "payload" / "cgroup.procs").write_text("11\n")
        argv = ["python", "-m", "server", "--state-dir", str(states), "--state-disk-gib", "150"]
        for pid, maps in ((10, ""), (11, f"7f00000000-{0x7f00000000 + ple_size:x} r--s 00000000 103:02 5 {ple}\n")):
            d = proc / str(pid)
            d.mkdir()
            (d / "cmdline").write_bytes("\0".join(argv).encode() + b"\0")
            (d / "maps").write_text("55f1-55f2 r-xp 00000000 103:02 9 /usr/bin/python\n" + maps)
            (d / "io").write_text(f"rchar: 1\nread_bytes: {1000 * pid}\nwrite_bytes: {500 * pid}\n")
    return proc, cg, states, ple


def test_probe_finds_the_unit_states_ple_file_and_io_rates(tmp_path):
    proc, cg, states, ple = fake_host(tmp_path)
    clock = Clock()
    probe = mem.EngineProbe(proc=proc, cgroup_root=cg, cache_bytes=lambda p: 4096, clock=clock)
    assert probe.pids(UNIT) == [10, 11]
    d = probe.disk(UNIT, ple_bytes=4096 * 3 - 100)
    assert d["states"] == mem.tree_bytes(states) and d["states_cap"] == 150 * mem.GIB
    assert d["ple_file"] == 4096 * 3 and d["ple_ram"] == 4096
    assert "read_bps" not in d  # a rate needs two readings
    (proc / "11" / "io").write_text("read_bytes: 31000\nwrite_bytes: 5500\n")  # +20000 read, +0 written
    clock.t += 10
    d = probe.disk(UNIT, ple_bytes=4096 * 3 - 100)
    assert d["read_bps"] == 2000 and d["write_bps"] == 0


def test_probe_reads_ple_residency_at_most_every_30_s(tmp_path):
    proc, cg, _, _ = fake_host(tmp_path)
    calls, clock = [], Clock()
    probe = mem.EngineProbe(proc=proc, cgroup_root=cg, cache_bytes=lambda p: calls.append(p) or 1, clock=clock)
    for step in (0, 10, 10, 15):
        clock.t += step
        probe.disk(UNIT, ple_bytes=1)
    assert len(calls) == 2


def test_probe_without_ple_size_or_engine(tmp_path):
    proc, cg, _, _ = fake_host(tmp_path)
    d = mem.EngineProbe(proc=proc, cgroup_root=cg, cache_bytes=lambda p: 1).disk(UNIT, ple_bytes=None)
    assert "ple_file" not in d and "states" in d
    proc2, cg2, _, _ = fake_host(tmp_path / "b", with_engine=False)
    assert mem.EngineProbe(proc=proc2, cgroup_root=cg2).disk(UNIT, 1) == {}


def test_sampler_uses_engine_figures_only_while_the_engine_runs(tmp_path):
    proc, cg, _, _ = fake_host(tmp_path)
    facts = {"weights_bytes": 69_428_838_400, "cache_bytes": 7_000_000_000, "ple_bytes": 4096 * 3}
    probe = mem.EngineProbe(proc=proc, cgroup_root=cg, cache_bytes=lambda p: 8192)
    s = mem.MemorySampler(proc=proc, probe=probe).sample(UNIT, facts, GTT)
    assert s["ram"]["weights"] == 69_428_838_400 and s["ram"]["ple_ram"] == 8192
    assert set(s["disk"]) == {"ple_file", "states", "states_cap"} and "running" not in s["disk"]
    proc2, cg2, _, _ = fake_host(tmp_path / "b", with_engine=False)
    s = mem.MemorySampler(proc=proc2, probe=mem.EngineProbe(proc=proc2, cgroup_root=cg2)).sample(UNIT, facts, 2e7)
    assert s["ram"]["weights"] == 0 and s["ram"]["gpu_other"] == 2e7 and s["disk"] == {}


def test_sampler_without_meminfo(tmp_path):
    assert mem.MemorySampler(proc=Path(tmp_path)).sample(None, {}, None) is None


def test_probe_looks_again_for_a_ple_file_mapped_after_the_first_reading(tmp_path):
    proc, cg, _, ple = fake_host(tmp_path)
    maps = (proc / "11" / "maps").read_text()
    (proc / "11" / "maps").write_text(maps.splitlines()[0] + "\n")  # the engine has not mapped it yet
    probe = mem.EngineProbe(proc=proc, cgroup_root=cg, cache_bytes=lambda p: 1)
    assert "ple_file" not in probe.disk(UNIT, ple_bytes=4096)
    (proc / "11" / "maps").write_text(maps)
    assert probe.disk(UNIT, ple_bytes=4096)["ple_file"] == 4096 * 3


def test_ram_bands_without_a_gtt_reading_are_not_made():
    assert mem.ram_bands(mem.parse_meminfo(MEMINFO), None) is None


def test_ram_bands_still_add_up_when_gtt_counts_memory_already_freed():
    mi = mem.parse_meminfo(MEMINFO.replace("MemFree:        11647260", "MemFree:        81647260"))
    b = mem.ram_bands(mi, GTT, {"weights_bytes": 69_428_838_400})
    assert sum(b[k] for k in mem.RAM_BANDS) == b["total"] and b["kernel"] == 0
    assert b["weights"] == 0  # GTT shrank below the arena: the engine is unloading


def test_probe_sums_a_table_mapped_in_segments_and_skips_a_larger_file(tmp_path):
    proc, cg, _, ple = fake_host(tmp_path)
    big = tmp_path / "weights.bin"
    big.write_bytes(b"w" * 4096 * 8)
    (proc / "11" / "maps").write_text(
        f"7f0000000000-7f0000008000 r--s 00000000 103:02 6 {big}\n"
        f"7f1000000000-7f1000001000 r--s 00000000 103:02 5 {ple}\n"
        f"7f1000002000-7f1000004000 r--s 00001000 103:02 5 {ple}\n")
    d = mem.EngineProbe(proc=proc, cgroup_root=cg, cache_bytes=lambda p: 7).disk(UNIT, ple_bytes=4096 * 3)
    assert d["ple_file"] == 4096 * 3


def test_probe_finds_a_unit_outside_its_own_slice(tmp_path):
    proc, cg, _, _ = fake_host(tmp_path)
    (proc / "self/cgroup").write_text("0::/system.slice/kiln-dash.service\n")
    (proc / "11" / "cgroup").write_text(f"0::/user.slice/user@1000.service/app.slice/{UNIT}/payload\n")
    assert mem.EngineProbe(proc=proc, cgroup_root=cg).pids(UNIT) == [10, 11]


class FakeRun:
    def __init__(self, since="@1768485600", lines=""):
        self.since, self.lines, self.calls = since, lines, []

    def __call__(self, argv, **kw):
        self.calls.append(argv)
        out = self.since if argv[0] == "systemctl" else self.lines
        return type("R", (), {"stdout": out + "\n"})()


def test_start_facts_reads_the_running_units_start_lines_back():
    run = FakeRun(lines="PLE table on host: 39040187392 bytes not resident, 1 segment(s)\n"
                        "weight payload 1 bytes, arena reserved 69428838400 bytes, staging peak 1\n")
    assert mem.start_facts(UNIT, run=run) == {"ple_bytes": 39040187392, "weights_bytes": 69428838400}
    assert "--since=@1768485600" in run.calls[1]
    assert mem.start_facts(UNIT, run=FakeRun(since="@0")) == {}  # not running
    assert mem.start_facts(UNIT, run=FakeRun(since="")) == {}
