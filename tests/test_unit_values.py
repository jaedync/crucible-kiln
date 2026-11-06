"""Unit file values mean what they say (security review L1)."""
import shutil
import subprocess

import pytest

from kiln import config, systemd_quote
from kiln.engines.manager import render_unit


def test_exec_args_are_quoted_and_escaped():
    assert systemd_quote.exec_line(["/usr/bin/kiln", "--config", "/a/b.toml", "dash"]) == \
        "/usr/bin/kiln --config /a/b.toml dash"
    assert systemd_quote.exec_arg("/home/user name/.local/bin/kiln") == '"/home/user name/.local/bin/kiln"'
    assert systemd_quote.exec_arg("/m/100%") == "/m/100%%"
    assert systemd_quote.exec_arg("/m/$HOME") == '"/m/$$HOME"'
    assert systemd_quote.exec_arg('a"b') == '"a\\"b"'
    assert systemd_quote.exec_arg(";") == '";"'
    for bad in ("a\nb", "a\\", "a\x00"):
        with pytest.raises(systemd_quote.UnitValueError):
            systemd_quote.exec_arg(bad)


@pytest.mark.skipif(not shutil.which("systemd-analyze"), reason="needs systemd-analyze")
def test_systemd_reads_the_quoted_args_back(tmp_path):
    args = ["/bin/echo", "/home/user name/x", "100%", "$HOME", 'q"t', "b\\s", ";"]
    unit = tmp_path / "kiln-quote-test.service"
    unit.write_text(f"[Service]\nType=oneshot\nExecStart={systemd_quote.exec_line(args)}\n")
    # System mode: --user needs a user manager (XDG_RUNTIME_DIR), which CI and a bare HOME lack.
    # Both modes parse ExecStart quoting the same way.
    out = subprocess.run(["systemd-analyze", "verify", str(unit)], capture_output=True, text=True)
    assert out.returncode == 0, out.stderr
    assert "Invalid" not in out.stderr and "Unbalanced" not in out.stderr, out.stderr


def test_a_label_with_a_percent_is_not_a_specifier(make_cfg):
    cfg = make_cfg(**{"profiles.gufo-qwen27.label": "Gufo 100% fast"})
    assert "Description=Kiln engine Gufo 100%% fast\n" in render_unit(cfg, cfg.profiles["gufo-qwen27"], False)


@pytest.mark.parametrize("value", ["X=1\\", ["a", "b\\"]])
def test_a_trailing_backslash_is_refused(make_cfg, value):
    with pytest.raises(config.ConfigError):
        make_cfg(**{"profiles.gufo-qwen27.container": {"Image": "x", "Environment": value}})


def test_a_label_with_a_trailing_backslash_is_refused(make_cfg):
    with pytest.raises(config.ConfigError):
        make_cfg(**{"profiles.gufo-qwen27.label": "odd\\"})


def test_a_label_with_a_control_character_is_refused_at_parse(make_cfg):
    with pytest.raises(config.ConfigError):
        make_cfg(**{"profiles.gufo-qwen27.label": "a\tb"})
