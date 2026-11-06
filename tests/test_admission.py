import json

import pytest

from kiln.engines.admission import MAX_HOLD_S, AdmissionHold


class Clock:
    def __init__(self, t=1_000.0):
        self.t = t

    def __call__(self):
        return self.t


def test_the_hold_file_sits_next_to_the_state_file(make_cfg):
    cfg = make_cfg()
    assert cfg.admission_file == cfg.state_file.with_name("admission-hold.json")


def test_write_then_read_until_expiry(tmp_path):
    clock = Clock()
    hold = AdmissionHold(tmp_path / "s" / "admission-hold.json", clock)
    assert hold.active() is None
    d = hold.write(60, "switch to crucible")
    assert d == {"v": 1, "until": 1_060.0, "reason": "switch to crucible"}
    assert json.loads(hold.path.read_text()) == d
    assert not hold.path.with_name(hold.path.name + ".tmp").exists()
    assert hold.active().until == 1_060.0 and hold.active().reason == "switch to crucible"
    clock.t = 1_060.0  # an expired hold never blocks, even if nobody releases it
    assert hold.active() is None


def test_a_rewrite_refreshes_the_hold(tmp_path):
    clock = Clock()
    hold = AdmissionHold(tmp_path / "admission-hold.json", clock)
    hold.write(10)
    assert hold.active().until == 1_010.0
    clock.t = 1_005.0
    hold.write(600)
    assert hold.active().until == 1_605.0


@pytest.mark.parametrize("text", ["not json", "[]", '{"v": 1}', '{"v": 1, "until": "soon"}',
                                  '{"v": 1, "until": null}', '{"v": 1, "until": true}'])
def test_a_broken_file_is_no_hold(tmp_path, text):
    path = tmp_path / "admission-hold.json"
    path.write_text(text)
    assert AdmissionHold(path, Clock()).active() is None


def test_release_removes_the_file_and_tolerates_none(tmp_path):
    hold = AdmissionHold(tmp_path / "admission-hold.json", Clock())
    assert hold.release() is False
    hold.write(60)
    assert hold.release() is True
    assert not hold.path.exists() and hold.active() is None


def test_write_rejects_seconds_out_of_range(tmp_path):
    hold = AdmissionHold(tmp_path / "admission-hold.json", Clock())
    for seconds in (0, -5, MAX_HOLD_S + 1):
        with pytest.raises(ValueError):
            hold.write(seconds)
    assert not hold.path.exists()


def test_cli_hold_and_release(make_cfg, monkeypatch, capsys):
    from kiln import cli
    cfg = make_cfg()
    monkeypatch.setattr(cli.config, "load", lambda _path: cfg)
    assert cli.main(["hold", "--seconds", "600", "--reason", "switch to crucible"]) == 0
    printed = json.loads(capsys.readouterr().out)
    assert printed == json.loads(cfg.admission_file.read_text())
    assert printed["reason"] == "switch to crucible" and printed["v"] == 1
    assert AdmissionHold(cfg.admission_file).active() is not None
    assert cli.main(["release"]) == 0
    assert not cfg.admission_file.exists()
    assert cli.main(["release"]) == 0  # nothing to release is not an error


@pytest.mark.parametrize("seconds", ["0", "3601", "-1"])
def test_cli_hold_rejects_seconds_out_of_range(make_cfg, monkeypatch, capsys, seconds):
    from kiln import cli
    cfg = make_cfg()
    monkeypatch.setattr(cli.config, "load", lambda _path: cfg)
    assert cli.main(["hold", "--seconds", seconds]) == 2
    assert "--seconds" in capsys.readouterr().err
    assert not cfg.admission_file.exists()


def test_hold_max_s_config(make_cfg):
    from kiln.config import ConfigError
    assert make_cfg().hold_max_s == 150
    assert make_cfg(**{"gateway.hold_max_s": 0}).hold_max_s == 0
    with pytest.raises(ConfigError):
        make_cfg(**{"gateway.hold_max_s": -1})
