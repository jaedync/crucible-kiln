"""kiln init: a first kiln.toml and the kiln-gateway and kiln-dash user units, in a temp home only."""
import tomllib
from pathlib import Path

import pytest

from kiln import cli, config, install
from kiln.engines.manager import render_unit


@pytest.fixture
def tree(tmp_path):
    crucible = tmp_path / "crucible-strix-halo"
    (crucible / "server").mkdir(parents=True)
    (crucible / "server" / "__main__.py").write_text("")
    (crucible / "build" / "engine").mkdir(parents=True)
    (crucible / "build" / "engine" / "crucible").write_text("")
    pack = tmp_path / "packs" / "Qwen3.8-Flash-Next-EXL3-3.05bpw"
    pack.mkdir(parents=True)
    (pack / "config.json").write_text("{}")
    home = tmp_path / "home" / "user"
    home.mkdir(parents=True)
    return {"crucible": crucible, "pack": pack, "home": home}


class Systemctl:
    def __init__(self, fail=None, linger="yes"):
        self.calls, self.fail, self.linger = [], fail, linger

    def __call__(self, cmd):
        self.calls.append(cmd)
        if self.fail and self.fail in cmd:
            return 1, "no user manager"
        return 0, (self.linger if cmd[0] == "loginctl" else "")


def run(tree, env=None, systemctl=None, **kw):
    lines = []
    sc = systemctl or Systemctl()
    code = install.init(crucible_dir=tree["crucible"], pack=tree["pack"], home=tree["home"], env=env or {},
                        systemctl=sc, kiln="/home/user/.local/bin/kiln", host="strix", python="/usr/bin/python3",
                        user="user",
                        out=lines.append, **kw)
    return code, "\n".join(lines), sc


def test_init_writes_a_config_that_parses_with_one_native_crucible_profile(tree):
    code, out, sc = run(tree)
    assert code == 0, out
    path = tree["home"] / ".config" / "kiln" / "kiln.toml"
    text = path.read_text()
    assert text.count("\n#") > 10  # commented for the reader
    cfg = config.parse(tomllib.loads(text))
    assert cfg.host == "strix" and cfg.reference is None
    assert cfg.gateway_listen == ("127.0.0.1", 8000) and cfg.dash_listen == ("127.0.0.1", 7000)
    assert cfg.db_path == tree["home"] / ".local" / "share" / "kiln" / "kiln.db"
    assert cfg.state_file == tree["home"] / ".local" / "state" / "kiln" / "active.json"
    assert cfg.user_unit_dir == tree["home"] / ".config" / "systemd" / "user"
    (p,) = cfg.profiles.values()
    assert p.name == "crucible" and p.engine == "crucible" and p.port == 8790 and p.served_id == "crucible"
    assert p.model == "qwen3.8-flash-next-exl3-3.05bpw" and p.request_compat == "crucible"
    assert p.kv.positions == p.slots * p.context == 32768
    start = p.native["ExecStart"]
    assert start.startswith("/usr/bin/python3 -m server ") and str(tree["pack"]) in start
    assert f"--engine {tree['crucible'] / 'build' / 'engine' / 'crucible'}" in start
    assert "--port 8790" in start and "--ctx 32768" in start and "--streams 1" in start
    assert p.native["WorkingDirectory"] == str(tree["crucible"])
    unit = render_unit(cfg, p, True)  # the engine manager can render it as a plain user unit
    assert "[Service]" in unit and "ExecStart=/usr/bin/python3 -m server" in unit


def test_init_installs_enables_and_starts_the_gateway_and_dash_units_with_the_installed_entry_point(tree):
    code, out, sc = run(tree)
    units = tree["home"] / ".config" / "systemd" / "user"
    conf = tree["home"] / ".config" / "kiln" / "kiln.toml"
    for name, cmd in (("kiln-gateway", "gateway"), ("kiln-dash", "dash")):
        text = (units / f"{name}.service").read_text()
        assert f"ExecStart=/home/user/.local/bin/kiln --config {conf} {cmd}\n" in text
        assert "PYTHONPATH" not in text and "WantedBy=default.target" in text
    # restart, not start: an init --force with a new kiln.toml must reach units that already run
    assert sc.calls == [["systemctl", "--user", "daemon-reload"],
                        ["systemctl", "--user", "enable", "kiln-gateway.service", "kiln-dash.service"],
                        ["systemctl", "--user", "restart", "kiln-gateway.service", "kiln-dash.service"],
                        ["loginctl", "show-user", "user", "--property=Linger", "--value"]]
    assert "Started kiln-gateway and kiln-dash." in out
    assert "kiln doctor" in out and "kiln switch crucible" in out
    assert "systemctl --user start" not in out and "enable-linger" not in out  # lingers already


def test_no_start_leaves_the_units_stopped_and_says_how_to_start_them(tree):
    code, out, sc = run(tree, start=False)
    assert code == 0 and ["systemctl", "--user", "restart", "kiln-gateway.service", "kiln-dash.service"] not in sc.calls
    assert "systemctl --user start kiln-gateway kiln-dash" in out


@pytest.mark.parametrize("answer", [(0, "no"), (1, "Failed to get user: User user is not logged in")])
def test_init_names_enable_linger_only_when_the_user_does_not_linger(tree, answer):
    class NoLinger(Systemctl):
        def __call__(self, cmd):
            self.calls.append(cmd)
            return answer if cmd[0] == "loginctl" else (0, "")
    code, out, _ = run(tree, systemctl=NoLinger())
    assert code == 0 and 'loginctl enable-linger "$USER"' in out


def test_init_respects_the_xdg_directories(tree, tmp_path):
    env = {"XDG_CONFIG_HOME": str(tmp_path / "cfg"), "XDG_DATA_HOME": str(tmp_path / "data"),
           "XDG_STATE_HOME": "relative/is/ignored"}
    assert run(tree, env=env)[0] == 0
    cfg = config.parse(tomllib.loads((tmp_path / "cfg" / "kiln" / "kiln.toml").read_text()))
    assert cfg.db_path == tmp_path / "data" / "kiln" / "kiln.db"
    assert cfg.state_file == tree["home"] / ".local" / "state" / "kiln" / "active.json"
    assert (tmp_path / "cfg" / "systemd" / "user" / "kiln-dash.service").exists()
    assert not (tree["home"] / ".config").exists()


def test_init_refuses_to_overwrite_without_force(tree):
    assert run(tree)[0] == 0
    path = tree["home"] / ".config" / "kiln" / "kiln.toml"
    path.write_text("# my edits\n")
    code, out, sc = run(tree)
    assert code == 1 and "--force" in out and str(path) in out and sc.calls == []
    assert path.read_text() == "# my edits\n"
    assert run(tree, force=True)[0] == 0 and "my edits" not in path.read_text()


def test_init_again_with_the_same_flags_is_not_an_overwrite(tree):
    assert run(tree)[0] == 0
    assert run(tree)[0] == 0


def test_lan_opts_in_to_every_interface_and_flags_set_the_ports(tree):
    code, out, _ = run(tree, lan=True, gateway_port=9000, dash_port=9001, engine_port=9790, ctx=65536, slots=2)
    assert code == 0, out
    cfg = config.parse(tomllib.loads((tree["home"] / ".config" / "kiln" / "kiln.toml").read_text()))
    assert cfg.gateway_listen == ("*", 9000) and cfg.dash_listen == ("*", 9001)
    p = cfg.profiles["crucible"]
    assert p.port == 9790 and "--port 9790" in p.native["ExecStart"] and p.kv.positions == 2 * 65536


def test_init_names_what_is_missing_in_the_crucible_checkout(tree):
    (tree["crucible"] / "build" / "engine" / "crucible").unlink()
    code, out, sc = run(tree)
    assert code == 2 and "build the engine first" in out and sc.calls == []
    assert not (tree["home"] / ".config").exists()
    code, out, _ = run({**tree, "crucible": tree["home"]})
    assert code == 2 and "server/__main__.py" in out


def test_init_needs_a_pack_directory(tree):
    code, out, _ = run({**tree, "pack": tree["pack"] / "missing"})
    assert code == 2 and "--pack" in out


def test_init_uses_the_checkouts_own_venv(tree):
    venv = tree["crucible"] / ".venv" / "bin"
    venv.mkdir(parents=True)
    (venv / "python").write_text("")
    run(tree)
    cfg = config.parse(tomllib.loads((tree["home"] / ".config" / "kiln" / "kiln.toml").read_text()))
    assert cfg.profiles["crucible"].native["ExecStart"].startswith(f"{venv / 'python'} -m server ")


def test_a_failed_systemctl_is_reported(tree):
    code, out, _ = run(tree, systemctl=Systemctl(fail="enable"))
    assert code == 1 and "enable" in out and "no user manager" in out
    code, out, _ = run(tree, systemctl=Systemctl(fail="restart"), force=True)
    assert code == 1 and "restart" in out and "journalctl --user -u kiln-dash" in out


def test_ports_must_differ(tree):
    code, out, _ = run(tree, gateway_port=8790)
    assert code == 2 and "ports" in out


def test_kiln_command_prefers_the_entry_point_on_path(tmp_path):
    exe = tmp_path / "kiln"
    exe.write_text("")
    assert install.kiln_command(which=lambda n: str(exe)) == str(exe)
    py = tmp_path / "venv" / "bin" / "python"
    py.parent.mkdir(parents=True)
    (py.parent / "kiln").write_text("")
    assert install.kiln_command(which=lambda n: None, python=str(py)) == str(py.parent / "kiln")
    assert install.kiln_command(which=lambda n: None, python="/nowhere/python") == "/nowhere/python -m kiln.cli"


def test_the_cli_runs_init_without_a_config(tree, monkeypatch, capsys):
    seen = {}

    def fake_init(**kw):
        seen.update(kw)
        return 0
    monkeypatch.setattr(install, "init", fake_init)
    monkeypatch.setenv("HOME", str(tree["home"]))
    assert cli.main(["init", "--crucible", str(tree["crucible"]), "--pack", str(tree["pack"]), "--port", "9000",
                     "--lan", "--force", "--no-start"]) == 0
    assert seen["start"] is False and seen["user"]
    assert seen["gateway_port"] == 9000 and seen["lan"] and seen["force"] and seen["home"] == Path(tree["home"])
    assert seen["crucible_dir"] == tree["crucible"] and seen["pack"] == tree["pack"]


def test_load_reads_the_xdg_config_home(tmp_path, monkeypatch):
    path = tmp_path / "kiln" / "kiln.toml"
    path.parent.mkdir()
    path.write_text('host = "strix"\n')
    monkeypatch.delenv("KILN_CONFIG", raising=False)
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path))
    assert config.load().host == "strix"


def test_the_next_commands_name_the_kiln_that_init_installed_when_it_is_not_on_path(tree):
    """A venv install (`~/kiln-venv/bin/kiln init ...`): plain `kiln` would not be found."""
    lines = []
    install.init(crucible_dir=tree["crucible"], pack=tree["pack"], home=tree["home"], env={},
                 systemctl=Systemctl(), kiln="/home/user/kiln-venv/bin/kiln", host="strix",
                 python="/usr/bin/python3", user="user", out=lines.append, which=lambda name: None)
    out = "\n".join(lines)
    assert "/home/user/kiln-venv/bin/kiln doctor" in out and "/home/user/kiln-venv/bin/kiln switch crucible" in out
    lines.clear()
    install.init(crucible_dir=tree["crucible"], pack=tree["pack"], home=tree["home"], env={},
                 systemctl=Systemctl(), kiln="/home/user/.local/bin/kiln", host="strix", python="/usr/bin/python3",
                 user="user", out=lines.append, which=lambda name: "/home/user/.local/bin/kiln", force=True)
    assert "\n  kiln doctor" in "\n".join(lines)


@pytest.mark.parametrize("flag", ["--pack", "--crucible"])
def test_the_cli_refuses_an_empty_directory_flag(tree, monkeypatch, capsys, flag):
    # `kiln init --pack "$PACK"` with PACK unset: Path("") is the current directory, so it must not get that far.
    called = []
    monkeypatch.setattr(install, "init", lambda **kw: called.append(kw) or 0)
    values = {"--crucible": str(tree["crucible"]), "--pack": str(tree["pack"]), flag: ""}
    assert cli.main(["init", "--crucible", values["--crucible"], "--pack", values["--pack"]]) == 2
    err = capsys.readouterr().err
    assert called == [] and f"kiln init: {flag} is empty" in err


def test_init_needs_a_pack_with_a_config_json(tree):
    (tree["pack"] / "config.json").unlink()
    code, out, sc = run(tree)
    assert code == 2 and sc.calls == []
    assert "--pack" in out and "no config.json" in out
    assert not (tree["home"] / ".config").exists()
