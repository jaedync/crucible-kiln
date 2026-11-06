"""kiln init writes Exec lines that systemd splits back into the same arguments (security review L1)."""
import shlex
import tomllib

from kiln import config, install
from tests.test_init import Systemctl, tree  # noqa: F401 (fixture)


def systemd_split(line: str) -> list[str]:
    """Close enough to systemd's own word splitting for these values, with its escapes undone."""
    return [w.replace("%%", "%").replace("$$", "$") for w in shlex.split(line)]


def test_paths_with_spaces_and_percent_signs_stay_whole(tree, tmp_path):
    pack = tmp_path / "packs" / "Qwen 27B 100%"
    pack.mkdir(parents=True)
    (pack / "config.json").write_text("{}")
    home = tmp_path / "user name"
    home.mkdir()
    kiln = "/home/user name/.local/bin/kiln"
    code = install.init(crucible_dir=tree["crucible"], pack=pack, home=home, env={}, systemctl=Systemctl(),
                        kiln=kiln, host="strix", python="/usr/bin/python3", user="jane", start=False,
                        out=lambda s: None)
    assert code == 0
    conf = home / ".config" / "kiln" / "kiln.toml"
    for name, cmd in (("kiln-gateway", "gateway"), ("kiln-dash", "dash")):
        text = (home / ".config" / "systemd" / "user" / f"{name}.service").read_text()
        line = next(x for x in text.splitlines() if x.startswith("ExecStart="))
        assert systemd_split(line.removeprefix("ExecStart=")) == [kiln, "--config", str(conf), cmd]
    p = config.parse(tomllib.loads(conf.read_text())).profiles["crucible"]
    args = systemd_split(p.native["ExecStart"])
    assert args[:4] == ["/usr/bin/python3", "-m", "server", str(pack)]


def test_the_module_fallback_is_three_arguments():
    text = install.render_service("kiln-dash", "/opt/my python/bin/python3 -m kiln.cli",
                                  install.Path("/c/kiln.toml"))
    line = next(x for x in text.splitlines() if x.startswith("ExecStart="))
    assert systemd_split(line.removeprefix("ExecStart=")) == ["/opt/my python/bin/python3", "-m", "kiln.cli",
                                                              "--config", "/c/kiln.toml", "dash"]
