"""hw.mem in the live feed."""
from kiln.dash.app import Dash
from kiln.engines.state import READY, StateFile
from kiln.gateway.attribution import Whois
from tests.test_compat import CRUCIBLE
from tests.test_dash import NOW, FakeManager, FakeSensors, _no_lookup

RAM = {"weights": 69e9, "cache": 7e9, "gpu_other": 39e9, "ple_ram": 2e9, "file_cache": 2e9, "programs": 1e9,
       "kernel": 1e9, "free": 13e9, "total": 134e9}


class FakeSampler:
    def __init__(self):
        self.calls = []

    def sample(self, unit, facts, gtt, slots=None):
        self.calls.append((unit, facts, gtt))
        return {"ram": RAM, "disk": {"ple_file": 39e9, "states": 60e9, "states_cap": 150 * 2 ** 30},
                "read_bps": 1000, "write_bps": 0}


async def make(make_cfg, aiohttp_client):
    cfg = make_cfg(**{"profiles.crucible-flashnext": CRUCIBLE})
    StateFile(cfg.state_file).set("crucible-flashnext", READY, now=NOW - 3600)
    sampler = FakeSampler()
    d = Dash(cfg, clock=lambda: NOW, sensors=FakeSensors(), whois=Whois(lookup=_no_lookup), manager=FakeManager(),
             sources=False, mem_sampler=sampler)
    d.mem_start_facts = lambda unit: {}  # no journal in tests
    return d, sampler, await aiohttp_client(d.app())


async def test_sample_mem_reads_the_active_unit_with_its_engine_sizes(make_cfg, aiohttp_client):
    d, sampler, _ = await make(make_cfg, aiohttp_client)
    d.ingest.engine_line("kiln-crucible-flashnext", "PLE table on host: 39040187392 bytes not resident", NOW - 60)
    d.hw = {"ts": NOW, "gtt_used_bytes": 115e9}
    await d.sample_mem(NOW)
    assert sampler.calls == [("kiln-crucible-flashnext.service", {"ple_bytes": 39040187392}, 115e9)]
    assert d.mem["ts"] == NOW and d.mem["ram"] == RAM and d.mem["read_bps"] == 1000


async def test_a_dashboard_started_after_the_engine_reads_its_start_sizes_once(make_cfg, aiohttp_client):
    d, sampler, _ = await make(make_cfg, aiohttp_client)
    looked = []
    d.mem_start_facts = lambda unit: looked.append(unit) or {"weights_bytes": 69e9, "ple_bytes": 39e9}
    d.ingest.engine_line("kiln-crucible-flashnext", "PLE table on host: 1 bytes not resident", NOW - 60)
    d.hw = {"ts": NOW, "gtt_used_bytes": 115e9}
    await d.sample_mem(NOW)
    await d.sample_mem(NOW + 10)
    assert looked == ["kiln-crucible-flashnext.service"]
    assert sampler.calls[-1][1] == {"ple_bytes": 1, "weights_bytes": 69e9}  # a line it saw itself wins


async def test_a_new_dashboard_fills_a_size_that_an_older_parser_missed(make_cfg, aiohttp_client):
    # Once the stored sizes had weights and stage but no PLE (the BF16 line did not parse), so the
    # PLE table stayed out of the maps after the parser fix until the engine restarted.
    d, sampler, _ = await make(make_cfg, aiohttp_client)
    looked = []
    d.mem_start_facts = lambda unit: looked.append(unit) or {"weights_bytes": 1, "ple_bytes": 102e9}
    d.ingest.engine_line("kiln-crucible-flashnext", "weight payload 6 bytes, arena reserved 69 bytes", NOW - 60)
    d.ingest.engine_line("kiln-crucible-flashnext", "stage arena: 1.0 MiB (shared)", NOW - 60)
    d.hw = {"ts": NOW, "gtt_used_bytes": 115e9}
    await d.sample_mem(NOW)
    await d.sample_mem(NOW + 10)
    assert looked == ["kiln-crucible-flashnext.service"]
    assert sampler.calls[-1][1] == {"weights_bytes": 69, "stage_bytes": 2 ** 20, "ple_bytes": 102e9}
