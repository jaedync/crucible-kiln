"""The rack display page (/panel) and its data endpoint (GET /api/v1/panel)."""
import os
from pathlib import Path

import pytest

from kiln.collect.hw import Sensors
from kiln.dash.app import Dash
from kiln.engines.state import READY, StateFile
from kiln.gateway.attribution import Whois
from tests.test_dash import NOW, FakeManager, FakeSensors, _no_lookup


# -- GPU sysfs readings added for the panel ---------------------------------------
def test_sensors_read_vram_and_the_current_shader_clock(tmp_path):
    dev = tmp_path / "class/drm/card1/device"
    dev.mkdir(parents=True)
    for name, value in {"mem_info_gtt_total": "133143986176", "mem_info_gtt_used": "29400000000",
                        "mem_info_vram_total": "536870912", "mem_info_vram_used": "268435456",
                        "gpu_busy_percent": "97"}.items():
        (dev / name).write_text(value + "\n")
    (dev / "pp_dpm_sclk").write_text("0: 600Mhz \n1: 1100Mhz \n2: 2900Mhz *\n")
    s = Sensors(root=tmp_path, proc=tmp_path).sample()
    assert s["vram_used_bytes"] == 268435456 and s["vram_total_bytes"] == 536870912
    assert s["sclk_mhz"] == 2900 and s["gpu_busy_pct"] == 97
    (dev / "pp_dpm_sclk").write_text("0: 600Mhz \n1: 1100Mhz \n")  # no level marked: unknown, not 0
    assert "sclk_mhz" not in Sensors(root=tmp_path, proc=tmp_path).sample()


# -- endpoint and page ---------------------------------------------------------------
class CountingQueue:
    def __init__(self):
        self.calls = 0

    def view(self):
        self.calls += 1
        return {"lease": None, "queue": [], "holds": ["kiln"], "recent": [], "n": self.calls}


@pytest.fixture
async def panel_client(make_cfg, aiohttp_client):
    async def build(*, schedule=True, clock=None):
        # No [schedule] in the config: the endpoint tests use a fake queue, not collect/gpu_queue.py.
        cfg = make_cfg()
        StateFile(cfg.state_file).set("halogen-flashnext", READY, now=NOW - 3600)
        d = Dash(cfg, clock=clock or (lambda: NOW), sensors=FakeSensors(), whois=Whois(lookup=_no_lookup),
                 manager=FakeManager(), sources=False)
        q = CountingQueue()
        if schedule:
            d.gpu_queue = q
        return d, q, await aiohttp_client(d.app())
    return build


async def test_panel_endpoint_is_cached_for_about_a_second(panel_client):
    now = [NOW]
    d, q, client = await panel_client(clock=lambda: now[0])
    a = await (await client.get("/api/v1/panel")).json()
    b = await (await client.get("/api/v1/panel")).json()
    assert a["v"] == 1 and a["ts"] == NOW and a["fleet"]["holds"] == ["kiln"]
    assert q.calls == 1 and b == a
    now[0] += 1.5
    c = await (await client.get("/api/v1/panel")).json()
    assert q.calls == 2 and c["ts"] == NOW + 1.5


async def test_the_kiosks_last_poll_is_kept_for_its_watchdog(panel_client):
    from kiln.dash.app import is_loopback
    now = [NOW]
    _, _, client = await panel_client(clock=lambda: now[0])
    assert await (await client.get("/api/v1/panel/seen")).json() == {"v": 1, "age_s": None, "uptime_s": 0.0}
    await client.get("/api/v1/panel")  # the test client is on loopback, as the kiosk is
    now[0] += 12.5
    assert await (await client.get("/api/v1/panel/seen")).json() == {"v": 1, "age_s": 12.5, "uptime_s": 12.5}
    # Only the rack's own browser counts: a panel open on a laptop must not hide a dead kiosk.
    assert is_loopback("127.0.0.1") and is_loopback("::1") and not is_loopback("198.51.100.10")
    assert not is_loopback(None) and not is_loopback("not an address")


async def test_panel_endpoint_without_a_schedule_has_no_fleet(panel_client):
    d, _, client = await panel_client(schedule=False)
    body = await (await client.get("/api/v1/panel")).json()
    assert body == {"v": 1, "ts": NOW, "fleet": None}


async def test_a_failed_fleet_read_is_reported_not_raised(panel_client):
    d, _, client = await panel_client()

    class Broken:
        def view(self):
            raise OSError("disk gone")
    d.gpu_queue = Broken()
    body = await (await client.get("/api/v1/panel")).json()
    assert body["fleet"] == {"error": "could not read the GPU queue (OSError)"}


async def test_panel_page_is_served(panel_client):
    _, _, client = await panel_client(schedule=False)
    r = await client.get("/panel")
    text = await r.text()
    assert r.status == 200 and "<html" in text.lower() and "js/panel/main.js" in text
    assert r.headers["Cache-Control"] == "no-cache"


@pytest.mark.parametrize("path", ["/js/panel/glance.js", "/css/panel.css", "/js/ws.js"])
async def test_static_files_are_checked_again_on_every_load(panel_client, path):
    # Without Cache-Control a browser may reuse a cached file for hours (10% of its age since Last-Modified):
    # the kiosk reloaded on a new build and still ran the old scripts. An unchanged file costs one 304.
    _, _, client = await panel_client(schedule=False)
    r = await client.get(path)
    assert r.status == 200 and r.headers["Cache-Control"] == "no-cache"
    again = await client.get(path, headers={"If-None-Match": r.headers["ETag"]})
    assert again.status == 304


def test_schedule_config_has_queue_and_lease_log_paths(make_cfg):
    pytest.importorskip("kiln.engines.fleet")  # the public package refuses [schedule]
    cfg = make_cfg(schedule={"day": "09:00-18:00"})
    assert cfg.schedule.lease_log == Path(os.path.expanduser("~/.local/state/crucible/gpu-leases.jsonl"))
    assert cfg.schedule.queue_dir == Path("/tmp/crucible-gpu-queue")
    assert cfg.schedule.plan_file is None  # the fleet's own default file (collect/gpu_queue.py)
    cfg = make_cfg(schedule={"day": "09:00-18:00", "queue_dir": "/q", "lease_log": "/l.jsonl",
                             "plan_file": "/plan.json"})
    assert cfg.schedule.queue_dir == Path("/q") and cfg.schedule.lease_log == Path("/l.jsonl")
    assert cfg.schedule.plan_file == Path("/plan.json")


# -- the self-contained demo (tools/build_panel_demo.py) --------------------------------
def _demo_module():
    import importlib.util
    path = Path(__file__).resolve().parents[1] / "tools" / "build_panel_demo.py"
    spec = importlib.util.spec_from_file_location("build_panel_demo", path)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def test_demo_is_one_file_without_network_or_private_names():
    demo = _demo_module()
    html = demo.build()
    assert 'src="' not in html and 'href="' not in html and "fetch(" in html  # api.js is bundled, never called
    assert "window.KILN_PANEL_DEMO" in html and "data:font/woff2;base64," in html
    assert demo.PRIVATE.search(html) is None


def test_demo_bundle_lists_every_imported_module_before_its_importer():
    demo = _demo_module()
    assert demo.bundle(demo.MODULES)  # the real list is complete and in order
    with pytest.raises(ValueError, match="js/cost.js"):
        demo.bundle([m for m in demo.MODULES if m != "js/cost.js"])


def test_demo_bundler_refuses_module_forms_it_cannot_translate():
    demo = _demo_module()
    with pytest.raises(ValueError, match="export"):
        demo.as_iife("js/x.js", "export default {};\n")
    out = demo.as_iife("js/panel/a.js", "import * as f from '../format.js';\nimport { h, svg as s } from '../dom.js';\n"
                                       "export function go() {}\nexport const K = 1;\n")
    assert "const f = __mods[\"js/format.js\"];" in out and "const { h, svg: s } = __mods[\"js/dom.js\"];" in out
    assert "return { go, K };" in out


# -- the kiosk's blank cursor theme (deploy/panel/blank_cursors.py) -----------------------------------
def test_blank_cursor_theme_is_valid_xcursor_with_only_transparent_pixels(tmp_path):
    import importlib.util
    import struct
    path = Path(__file__).resolve().parents[1] / "deploy" / "panel" / "blank_cursors.py"
    spec = importlib.util.spec_from_file_location("blank_cursors", path)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    cursors = mod.write_theme(tmp_path)
    assert {"default", "left_ptr"} <= {p.name for p in cursors.iterdir()}  # what cage and Chromium ask for
    data = (cursors / "default").read_bytes()
    magic, hdr, version, ntoc = struct.unpack_from("<4sIII", data)
    assert (magic, hdr, version) == (b"Xcur", 16, 0x10000)
    sizes = []
    for i in range(ntoc):
        kind, size, pos = struct.unpack_from("<III", data, 16 + 12 * i)
        chunk = struct.unpack_from("<IIIIIIIII", data, pos)
        assert kind == mod.IMAGE and chunk[:3] == (36, mod.IMAGE, size) and chunk[4:6] == (1, 1)
        assert struct.unpack_from("<I", data, pos + 36)[0] == 0  # alpha 0: nothing drawn
        sizes.append(size)
    assert sizes == list(mod.SIZES) and len(data) == 16 + 12 * ntoc + 40 * ntoc


def test_kiosk_watchdog_restarts_only_a_page_that_stopped_polling_a_running_dashboard():
    import importlib.util
    from importlib.machinery import SourceFileLoader
    path = Path(__file__).resolve().parents[1] / "deploy" / "panel" / "kiln-panel-watchdog"
    loader = SourceFileLoader("kiln_panel_watchdog", str(path))  # no .py suffix: name the loader
    wd = importlib.util.module_from_spec(importlib.util.spec_from_loader(loader.name, loader))
    loader.exec_module(wd)
    assert not wd.stale({"age_s": 2.0, "uptime_s": 900}, 90)
    assert wd.stale({"age_s": 91.0, "uptime_s": 900}, 90)  # crashed or hung page
    assert not wd.stale({"age_s": None, "uptime_s": 60}, 90)  # the dashboard just restarted
    assert wd.stale({"age_s": None, "uptime_s": 181}, 90)  # it never saw the page
    assert wd.alive(os.getpid()) and not wd.alive(2 ** 22 + 7)


def kiosk_tools(tmp_path, missing=()):
    """Stand-ins for /usr/bin/cage and /usr/bin/chromium, which a test machine may not have."""
    tools = tmp_path / "tools"
    tools.mkdir(exist_ok=True)
    env = {}
    for name in ("cage", "chromium"):
        path = tools / name
        if name not in missing:
            path.write_text("#!/bin/sh\n")
            path.chmod(0o755)
        env[f"KILN_PANEL_{name.upper()}"] = str(path)
    return env


@pytest.mark.parametrize("missing, package", [(("cage",), "cage"), (("chromium",), "chromium")])
def test_install_kiosk_checks_cage_and_chromium_before_it_writes_anything(tmp_path, missing, package):
    import getpass
    import subprocess
    root = Path(__file__).resolve().parents[1] / "deploy" / "panel"
    env = {"PATH": os.environ["PATH"], "KILN_PANEL_BIN": str(tmp_path / "bin"), "SUDO": "",
           "KILN_PANEL_UNIT_DIR": str(tmp_path / "units"), "HOME": str(tmp_path / "home"),
           "KILN_PANEL_HOME": str(tmp_path / "home"), **kiosk_tools(tmp_path, missing)}
    r = subprocess.run([str(root / "install-kiosk"), getpass.getuser()], env=env, capture_output=True, text=True,
                       timeout=60)
    assert r.returncode == 2 and f"install the package {package}" in r.stderr
    assert not (tmp_path / "bin").exists() and not (tmp_path / "home").exists()


def test_install_kiosk_checks_sudo_before_it_writes_anything(tmp_path):
    import getpass
    import subprocess
    root = Path(__file__).resolve().parents[1] / "deploy" / "panel"
    env = {"PATH": os.environ["PATH"], "KILN_PANEL_BIN": str(tmp_path / "bin"), "SUDO": "false",
           "KILN_PANEL_UNIT_DIR": str(tmp_path / "units"), "HOME": str(tmp_path / "home"),
           "KILN_PANEL_HOME": str(tmp_path / "home"), **kiosk_tools(tmp_path)}
    r = subprocess.run([str(root / "install-kiosk"), getpass.getuser()], env=env, capture_output=True, text=True,
                       timeout=60)
    assert r.returncode == 2 and "sudo" in r.stderr
    assert not (tmp_path / "bin").exists()


def test_install_kiosk_fills_in_the_user_and_paths(tmp_path):
    import getpass
    import re
    import subprocess
    root = Path(__file__).resolve().parents[1] / "deploy" / "panel"
    user = getpass.getuser()
    home = tmp_path / "home"
    env = {"PATH": os.environ["PATH"], "KILN_PANEL_BIN": str(tmp_path / "bin"), "SUDO": "",
           "KILN_PANEL_UNIT_DIR": str(tmp_path / "units"), "KILN_PANEL_URL": "http://127.0.0.1:7100/panel",
           "HOME": str(home), "KILN_PANEL_HOME": str(home), **kiosk_tools(tmp_path)}
    r = subprocess.run([str(root / "install-kiosk"), user], env=env, capture_output=True, text=True,
                       timeout=60)
    assert r.returncode == 0, r.stderr
    unit = (tmp_path / "units" / "kiln-panel-kiosk.service").read_text()
    assert f"User={user}\n" in unit and f"XDG_RUNTIME_DIR=/run/user/{os.getuid()}\n" in unit
    assert f"XCURSOR_PATH={home}/.local/share/kiln-panel/cursors\n" in unit
    assert f"ExecStart={tmp_path / 'tools' / 'cage'} -s -d -- {tmp_path / 'bin'}/kiln-panel-browser\n" in unit
    assert f"Environment=KILN_PANEL_CHROMIUM={tmp_path / 'tools' / 'chromium'}\n" in unit
    assert "Environment=KILN_PANEL_URL=http://127.0.0.1:7100/panel\n" in unit
    assert not re.search(r"@[A-Z]+@", unit)
    assert os.access(tmp_path / "bin" / "kiln-panel-browser", os.X_OK)
    assert (home / ".local" / "share" / "kiln-panel" / "cursors" / "default" / "index.theme").exists()


def test_the_kiosk_files_name_no_host_user_or_home():
    import re
    root = Path(__file__).resolve().parents[1] / "deploy" / "panel"
    private = re.compile(r"/home/[a-z]|/Users/|\bUser=[a-z]|/run/user/[0-9]")
    for f in root.iterdir():
        if f.is_file():
            assert not private.search(f.read_text()), f.name


@pytest.mark.parametrize("var, value", [
    ("KILN_PANEL_URL", "http://127.0.0.1:7000/panel?x=100%h"), ("KILN_PANEL_URL", "http://host/$HOME"),
    ("KILN_PANEL_URL", "http://host/;rm"), ("KILN_PANEL_URL", "http://host/'a"), ("KILN_PANEL_URL", 'http://host/"a'),
    ("KILN_PANEL_URL", "http://host/a\nExecStartPre=/bin/evil"), ("KILN_PANEL_BIN", "/tmp/b%n"),
    ("KILN_PANEL_HOME", "/tmp/$USER")])
def test_install_kiosk_refuses_unit_syntax_in_its_values(tmp_path, var, value):
    # Security review L2: these would reach a unit that root installs (% is a specifier, $ a variable).
    import getpass
    import subprocess
    root = Path(__file__).resolve().parents[1] / "deploy" / "panel"
    env = {"PATH": os.environ["PATH"], "KILN_PANEL_BIN": str(tmp_path / "bin"), "SUDO": "",
           "KILN_PANEL_UNIT_DIR": str(tmp_path / "units"), "KILN_PANEL_URL": "http://127.0.0.1:7100/panel",
           "HOME": str(tmp_path), "KILN_PANEL_HOME": str(tmp_path), var: value}
    r = subprocess.run([str(root / "install-kiosk"), getpass.getuser()], env=env, capture_output=True, text=True,
                       timeout=60)
    assert r.returncode == 2 and "unsupported character" in r.stderr
    assert not (tmp_path / "units").exists()
