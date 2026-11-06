"""The fleet and remote-host parts are optional: with no [schedule] and no [hosts] Kiln never imports
them, so the public tree (which leaves them out) runs the dashboard, manager and CLI."""
import subprocess
import sys
import textwrap
from pathlib import Path

DROPPED = ("kiln.collect.gpu_queue", "kiln.engines.fleet", "kiln.hosts", "kiln.agent")
SRC = Path(__file__).resolve().parents[1] / "src"

BLOCK = f"""
import importlib.abc, sys
class Block(importlib.abc.MetaPathFinder):
    def find_spec(self, name, path=None, target=None):
        if any(name == d or name.startswith(d + ".") for d in {DROPPED!r}):
            raise ImportError(f"{{name}} is not in this tree")
sys.meta_path.insert(0, Block())
"""


def run(code: str, tmp_path) -> subprocess.CompletedProcess:
    script = BLOCK + textwrap.dedent(code)
    return subprocess.run([sys.executable, "-c", script], capture_output=True, text=True, cwd=tmp_path,
                          env={"PYTHONPATH": str(SRC), "HOME": str(tmp_path), "PATH": "/usr/bin:/bin"}, timeout=60)


def test_dash_and_manager_run_without_the_fleet_and_host_modules(tmp_path):
    r = run(f"""
        import asyncio, json
        from kiln import config
        from kiln.dash.app import Dash
        cfg = config.parse({{"host": "strix", "dash": {{"db": "{tmp_path}/kiln.db"}},
                             "gateway": {{"state_file": "{tmp_path}/state/active.json", "data_dir": "{tmp_path}/gw"}},
                             "profiles": {{"crucible": {{"engine": "crucible", "model": "m", "port": 18790,
                                                        "context": 4096, "native": {{"ExecStart": "/bin/true"}}}}}}}})
        async def main():
            d = Dash(cfg, sources=False)
            assert d.manager.fleet is None and d.gpu_queue is None and d.hosts is None
            snap = d.snapshot_now({{"can_act": False, "identity": None}})
            assert snap["hosts"] == {{}} and snap["pricing"] is None and snap["engine"]["schedule"] is None
            assert d.panel_view()["fleet"] is None
            async def ok(*cmd, timeout=60):
                return 0, ""
            d.manager.run = ok
            assert await d.manager.blocked() is None
            print("ok")
        asyncio.run(main())
    """, tmp_path)
    assert r.returncode == 0 and r.stdout.strip().endswith("ok"), r.stderr


def test_the_cli_says_the_agent_is_not_in_this_tree(tmp_path):
    r = run("""
        import sys
        from kiln import cli
        sys.exit(cli.main(["agent"]))
    """, tmp_path)
    assert r.returncode == 2 and "not part of this install" in r.stderr, r.stderr



def test_a_schedule_without_the_fleet_module_is_a_config_error(tmp_path):
    """config.parse accepted [schedule], then kiln-dash failed with ModuleNotFoundError kiln.engines.fleet."""
    r = run("""
        import contextlib, io
        from kiln import cli, config
        try:
            config.parse({"host": "strix", "schedule": {"day": "09:00-18:00"}, "profiles": {}})
        except config.ConfigError as e:
            print("refused:", e)
        out = io.StringIO()
        with contextlib.redirect_stdout(out), contextlib.suppress(SystemExit):
            cli.main(["--help"])
        print("help:", out.getvalue())
        out = io.StringIO()
        with contextlib.redirect_stdout(out), contextlib.suppress(SystemExit):
            cli.main(["switch", "--help"])
        print(out.getvalue())
    """, tmp_path)
    assert "refused: [schedule] needs" in r.stdout, r.stdout + r.stderr
    help_text = r.stdout.split("help:", 1)[1]
    assert "fleet" not in help_text.lower(), help_text
    agent = [line for line in help_text.splitlines() if line.strip().startswith("agent")]
    assert agent and "not part of this install" in agent[0], help_text


def test_the_schedule_zone_defaults_to_the_system_zone(make_cfg):
    import pytest
    pytest.importorskip("kiln.engines.fleet")  # the public package refuses [schedule]
    from kiln import config
    from kiln.store.queries import local_tz
    cfg = make_cfg(schedule={"day": "09:00-18:00"})
    assert cfg.schedule.tz == config.system_tz()
    assert config.system_tz() in (local_tz(), "UTC")
