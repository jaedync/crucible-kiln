"""Defaults for a new user: a profile and a config that leave out what a new user cannot know."""
import os
import socket

import pytest

from kiln import config

MINIMAL_PROFILE = {"engine": "crucible", "model": "qwen3.8-flash-next", "port": 8790, "context": 65536,
                   "native": {"ExecStart": "/usr/bin/python -m server /models/pack"}}


def minimal(**top):
    return {"profiles": {"crucible": dict(MINIMAL_PROFILE)}, **top}


def test_a_profile_without_kv_reserves_slots_times_context():
    p = config.parse(minimal()).profiles["crucible"]
    assert p.kv.positions == 65536 and p.kv.bytes_per_position == 0 and p.kv.bytes is None
    d = minimal()
    d["profiles"]["crucible"]["slots"] = 3
    assert config.parse(d).profiles["crucible"].kv.positions == 3 * 65536


def test_explicit_kv_values_win():
    d = minimal()
    d["profiles"]["crucible"]["kv"] = {"positions": 524288, "bytes_per_position": 27456}
    assert config.parse(d).profiles["crucible"].kv == config.KV(524288, 27456)
    d["profiles"]["crucible"]["kv"] = {"bytes_per_position": 27456}  # positions alone may be left out
    assert config.parse(d).profiles["crucible"].kv == config.KV(65536, 27456)


def test_served_id_defaults_to_the_model_id():
    p = config.parse(minimal()).profiles["crucible"]
    assert p.served_id == "qwen3.8-flash-next" and p.accepts("qwen3.8-flash-next")
    d = minimal()
    d["profiles"]["crucible"]["served_id"] = "crucible"
    assert config.parse(d).profiles["crucible"].served_id == "crucible"


def test_the_keys_a_profile_still_needs_are_named():
    for key in ("engine", "model", "port", "context"):
        d = minimal()
        del d["profiles"]["crucible"][key]
        with pytest.raises(config.ConfigError, match=f"missing {key}"):
            config.parse(d)


def test_host_defaults_to_the_short_hostname(monkeypatch):
    monkeypatch.setattr(socket, "gethostname", lambda: "strix.example.lan")
    assert config.parse(minimal()).host == "strix"
    assert config.parse(minimal(host="box")).host == "box"


def test_host_falls_back_when_the_machine_has_no_name(monkeypatch):
    monkeypatch.setattr(socket, "gethostname", lambda: "")
    assert config.parse(minimal()).host == "localhost"


def test_listeners_default_to_loopback():
    cfg = config.parse(minimal())
    assert cfg.gateway_listen == ("127.0.0.1", 8000) and cfg.dash_listen == ("127.0.0.1", 7000)


def test_container_uid_defaults_to_this_user():
    assert config.parse(minimal()).container_uid == os.getuid()
    assert config.parse(minimal(container_uid=1000)).container_uid == 1000


def test_reference_prices_are_optional():
    cfg = config.parse(minimal())
    assert cfg.reference is None


def test_a_partial_reference_is_still_an_error():
    with pytest.raises(config.ConfigError, match="pricing.reference: missing"):
        config.parse(minimal(pricing={"reference": {"name": "Some API"}}))


def test_busy_units_default_to_none_and_must_be_globs():
    assert config.parse(minimal()).busy_units == ()
    assert config.parse(minimal(engines={"busy_units": ["bench-*"]})).busy_units == ("bench-*",)
    for bad in ("bench-*", [""], ["--all"], [3]):
        with pytest.raises(config.ConfigError, match="busy_units"):
            config.parse(minimal(engines={"busy_units": bad}))


def test_a_profile_cannot_take_the_name_of_a_kiln_unit():
    # kiln-gateway.service and kiln-dash.service live in the same user unit directory (kiln init).
    for name in ("gateway", "dash"):
        d = {"profiles": {name: dict(MINIMAL_PROFILE)}}
        with pytest.raises(config.ConfigError, match="Kiln's own"):
            config.parse(d)


def _two_packs(**b):
    """Two Crucible profiles of two packs: each server calls itself `crucible` (its --model-id default)."""
    d = minimal()
    d["profiles"]["crucible"]["served_id"] = "crucible"
    d["profiles"]["crucible-3bpw"] = {**MINIMAL_PROFILE, "model": "qwen3.8-flash-next-3.05bpw", "port": 8791,
                                      "served_id": "crucible", **b}
    return d


def test_two_profiles_of_different_models_may_share_the_engines_own_id():
    cfg = config.parse(_two_packs())
    a, b = cfg.profiles["crucible"], cfg.profiles["crucible-3bpw"]
    assert a.served_id == b.served_id == "crucible"
    assert a.accepts("crucible") and b.accepts("crucible")  # the gateway asks the active profile only
    assert cfg.profile_for_model("qwen3.8-flash-next-3.05bpw") is b


def test_a_client_id_still_must_mean_one_model():
    with pytest.raises(config.ConfigError, match="maps to both"):
        config.parse(_two_packs(aliases=["qwen3.8-flash-next"]))  # another profile's canonical id
    with pytest.raises(config.ConfigError, match="maps to both"):
        config.parse(_two_packs(served_id="qwen3.8-flash-next"))
    d = _two_packs(aliases=["flash"])
    d["profiles"]["crucible"]["aliases"] = ["flash"]
    with pytest.raises(config.ConfigError, match="maps to both"):
        config.parse(d)
