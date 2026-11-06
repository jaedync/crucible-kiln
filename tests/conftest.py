import copy

import pytest

from kiln import config

BASE = {
    "host": "test",
    "gateway": {"listen": "127.0.0.1:18000", "events_listen": "127.0.0.1:18001"},
    "dash": {"listen": "127.0.0.1:17000"},
    "actions": {"allow_nodes": ["owner-laptop"]},
    "pricing": {"reference": {"name": "Claude Sonnet 5.5", "provider_model": "anthropic/claude-sonnet-5.5",
                              "input_per_m": 2.0, "cached_per_m": 0.2, "output_per_m": 10.0},
                "openrouter": {"enabled": False}},
    "profiles": {
        "halogen-flashnext": {
            "label": "Halogen / Flash-Next", "engine": "halogen", "model": "qwen3.8-flash-next",
            "served_id": "halogen-qwen3.8-flash-next", "aliases": ["Qwen3.8 Flash Next"],
            "hf_id": "Qwen/Qwen3.8-Flash-Next", "port": 18731, "context": 262144, "slots": 4,
            "kv": {"positions": 524288, "bytes_per_position": 26624}, "memory_gib": 94,
            "container": {"Image": "localhost/halogen:test"}},
        "gufo-qwen27": {
            "label": "Gufo / Qwen 27B", "engine": "gufo", "model": "qwen3.8-27b",
            "served_id": "Qwen3.8-27B", "hf_id": "Qwen/Qwen3.8-27B", "port": 18080, "context": 65536,
            "kv": {"positions": 65536, "bytes_per_position": 65536},
            "container": {"Image": "localhost/gufo:test"}},
    },
}


@pytest.fixture
def make_cfg(tmp_path):
    def make(**overrides):
        d = copy.deepcopy(BASE)
        d["gateway"]["data_dir"] = str(tmp_path / "gw")
        d["gateway"]["state_file"] = str(tmp_path / "state" / "active.json")
        d["dash"]["db"] = str(tmp_path / "kiln.db")
        for path, value in overrides.items():
            node = d
            *parents, leaf = path.split(".")
            for p in parents:
                node = node.setdefault(p, {})
            node[leaf] = value
        return config.parse(d)
    return make


@pytest.fixture(autouse=True)
def no_foreign_engine_ports(monkeypatch, request):
    """A switch refuses a target port that another process holds (engines/manager.py). The test profiles' ports
    are real loopback ports, which some other program on the test machine may use; only the demo runs real
    engines on them."""
    if request.node.get_closest_marker("demo") is None:
        from kiln.engines import manager
        monkeypatch.setattr(manager, "port_answers", lambda port: False)
