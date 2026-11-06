import pytest
"""kiln doctor: each check is a pure function; gather runs them on injected facts only."""
import json

from kiln import cli, doctor
from kiln.doctor import FAIL, PASS, WARN
from kiln.engines.state import READY, StateFile


def test_python_and_aiohttp():
    assert doctor.check_python((3, 11, 4)).status == PASS
    r = doctor.check_python((3, 10, 12))
    assert r.status == FAIL and "3.11" in r.fix
    assert doctor.check_aiohttp("3.13.2").status == PASS
    assert doctor.check_aiohttp(None).status == FAIL


def test_config():
    assert doctor.check_config("/c/kiln.toml", None).status == PASS
    r = doctor.check_config("/c/kiln.toml", "profile x: missing port")
    assert r.status == FAIL and "missing port" in r.detail and "kiln init" in r.fix


def test_systemd_user_manager():
    assert doctor.check_systemd(0, "running\n").status == PASS
    assert doctor.check_systemd(1, "degraded\n").status == WARN
    r = doctor.check_systemd(1, "Failed to connect to bus: No medium found")
    assert r.status == FAIL and "linger" in r.fix


def test_journal():
    assert doctor.check_journal(0, '{"MESSAGE": "x"}\n').status == PASS
    assert doctor.check_journal(0, "").status == WARN
    assert doctor.check_journal(1, "No journal files were opened due to insufficient permissions.").status == FAIL


def test_amdgpu_and_kernel(tmp_path):
    assert doctor.check_amdgpu(tmp_path).status == PASS
    assert doctor.check_amdgpu(None).status == WARN
    assert doctor.check_kernel("6.17.2-1-generic").status == PASS
    assert doctor.check_kernel("6.5.0").status == PASS
    assert doctor.check_kernel("6.4.12").status == WARN
    assert doctor.check_kernel("weird").status == WARN
    assert doctor.kernel_version("10.1.0") == (10, 1)


def test_ports_free_or_ours():
    assert doctor.check_port("gateway", ("127.0.0.1", 8000), True, False, "kiln-gateway").status == PASS
    r = doctor.check_port("gateway", ("127.0.0.1", 8000), False, True, "kiln-gateway")
    assert r.status == PASS and "kiln-gateway" in r.detail
    r = doctor.check_port("dash", ("127.0.0.1", 7000), False, False, "kiln-dash")
    assert r.status == FAIL and "[dash] listen" in r.fix


def test_engine_port():
    assert doctor.check_engine(None, "stopped", None, None).status == WARN
    assert doctor.check_engine("crucible", READY, 8790, True).status == PASS
    r = doctor.check_engine("crucible", READY, 8790, False)
    assert r.status == FAIL and "journalctl --user -u kiln-crucible" in r.fix


def test_engine_lines():
    lines = [json.dumps({"MESSAGE": "crucible_decode req=3 tokens=40"}), json.dumps({"MESSAGE": "other"}),
             json.dumps({"MESSAGE": list(b"crucible_turn req=3")}), "not json"]
    assert doctor.count_engine_lines(lines) == 2
    assert doctor.check_engine_lines("kiln-crucible.service", 2).status == PASS
    assert doctor.check_engine_lines("kiln-crucible.service", 0).status == WARN
    assert doctor.check_engine_lines(None, None).status == WARN
    assert doctor.check_engine_lines("kiln-gufo.service", 0, "gufo").status == PASS


def test_a_line_has_the_status_and_a_fix_unless_it_passed():
    assert doctor.Result("x", FAIL, "broken", "do this").line() == "FAIL  x: broken  fix: do this"
    assert doctor.Result("x", PASS, "fine", "do this").line() == "PASS  x: fine"


class Runner:
    def __init__(self, answers):
        self.answers, self.calls = answers, []

    def __call__(self, cmd):
        self.calls.append(cmd)
        for key, value in self.answers.items():
            if key in " ".join(cmd):
                return value
        return 0, ""


def write_config(tmp_path, gw=18000, dash=17000, pack=None):
    if pack is None:
        pack = tmp_path / "packs" / "Model-EXL3"
        pack.mkdir(parents=True, exist_ok=True)
        (pack / "config.json").write_text("{}")
    path = tmp_path / "kiln.toml"
    path.write_text(f"""host = "strix"
[gateway]
listen = "127.0.0.1:{gw}"
state_file = "{tmp_path / 'active.json'}"
data_dir = "{tmp_path / 'gw'}"
[dash]
listen = "127.0.0.1:{dash}"
db = "{tmp_path / 'kiln.db'}"
[profiles.crucible]
engine = "crucible"
model = "m"
port = 18790
context = 4096
[profiles.crucible.native]
ExecStart = "/usr/bin/python3 -m server {pack} --port 18790"
""")
    return path


def test_gather_on_a_healthy_setup_passes(tmp_path):
    path = write_config(tmp_path)
    StateFile(tmp_path / "active.json").set("crucible", READY, now=1.0)
    gpu = tmp_path / "sys" / "class" / "drm" / "card1" / "device"
    gpu.mkdir(parents=True)
    (gpu / "mem_info_gtt_total").write_text("1\n")
    run = Runner({"is-system-running": (0, "running\n"), "-n 1": (0, '{"MESSAGE": "x"}'),
                  "-u kiln-crucible.service": (0, json.dumps({"MESSAGE": "crucible_prefill done=4096"}))})
    lines = []
    code = doctor.main(str(path), print_=lines.append, run=run, sys_root=tmp_path / "sys", release="6.12.1",
                       free=lambda h, p: p != 18000, answers=lambda port: port == 18790, version=(3, 12, 1))
    assert code == 0, lines
    assert len(lines) == 12 and all(line.startswith("PASS") for line in lines), lines
    assert any("in use by kiln-gateway" in line for line in lines)


def test_gather_fails_on_a_busy_port_and_warns_without_gpu_or_engine(tmp_path):
    path = write_config(tmp_path)
    run = Runner({"is-system-running": (0, "running\n"), "is-active": (3, "inactive")})
    lines = []
    code = doctor.main(str(path), print_=lines.append, run=run, sys_root=tmp_path / "none", release="6.1.0",
                       free=lambda h, p: p != 17000, answers=lambda port: False, version=(3, 12, 1))
    assert code == 1
    status = {line.split(":")[0].split("  ", 1)[1]: line.split()[0] for line in lines}
    assert status["dash port 17000"] == FAIL and status["gateway port 18000"] == PASS
    assert status["amdgpu"] == WARN and status["kernel"] == WARN and status["engine"] == WARN
    assert status["journal"] == WARN and status["engine log"] == WARN


def test_gather_stops_after_a_bad_config(tmp_path):
    path = tmp_path / "kiln.toml"
    path.write_text("[profiles.x]\nengine = 'crucible'\n")
    lines = []
    code = doctor.main(str(path), print_=lines.append, run=Runner({}), sys_root=tmp_path, release="6.12",
                       free=lambda h, p: True, answers=lambda p: True)
    assert code == 1 and any(line.startswith("FAIL  config") for line in lines) and len(lines) == 7


def test_the_cli_runs_doctor_even_when_the_config_does_not_load(monkeypatch):
    seen = []
    monkeypatch.setattr(doctor, "main", lambda path: seen.append(path) or 1)
    assert cli.main(["--config", "/nowhere/kiln.toml", "doctor"]) == 1
    assert seen == ["/nowhere/kiln.toml"]


# -- newcomer test: H1, M4, L4 ------------------------------------------------------------------------

def status_of(lines):
    return {line.split(":")[0].split("  ", 1)[1]: line.split()[0] for line in lines}


def healthy(tmp_path, **kw):
    return dict(run=Runner({"is-system-running": (0, "running\n")}), sys_root=tmp_path / "sys", release="6.12.1",
                free=lambda h, p: True, answers=lambda port: True, version=(3, 12, 1)) | kw


@pytest.mark.parametrize("make", ["missing", "empty", "root"])
def test_a_pack_without_config_json_fails(tmp_path, make):
    pack = {"missing": tmp_path / "nowhere", "empty": tmp_path / "empty", "root": tmp_path}[make]
    if make == "empty":
        pack.mkdir()
    path = write_config(tmp_path, pack=pack)
    lines = []
    assert doctor.main(str(path), print_=lines.append, **healthy(tmp_path)) == 1
    line = next(ln for ln in lines if ln.startswith("FAIL  pack crucible"))
    assert str(pack) in line and "config.json" in line and "--pack" in line


def test_the_pack_check_reads_the_path_after_dash_m_server():
    assert doctor.pack_dir({"ExecStart": "/v/bin/python -m server '/data/My Pack' --port 1"}) == "/data/My Pack"
    assert doctor.pack_dir({"ExecStart": "/usr/bin/llama-server -m /m.gguf"}) is None
    assert doctor.pack_dir({"ExecStart": "/v/bin/python -m server /data/100%%"}) == "/data/100%"
    assert doctor.pack_dir({}) is None


def test_another_process_on_the_engine_port_fails(tmp_path):
    path = write_config(tmp_path)
    StateFile(tmp_path / "active.json").set("crucible", READY, now=1.0)
    run = Runner({"is-system-running": (0, "running\n"), "is-active --quiet kiln-crucible": (3, "")})
    lines = []
    assert doctor.main(str(path), print_=lines.append, **healthy(tmp_path, run=run)) == 1
    line = next(ln for ln in lines if ln.startswith("FAIL  engine:"))
    assert "kiln-crucible.service is not active" in line and "another process" in line and "18790" in line


def test_port_checks_do_not_blame_kiln_units_when_systemd_does_not_answer(tmp_path):
    path = write_config(tmp_path)
    run = Runner({"is-system-running": (1, "Failed to connect to bus: $DBUS_SESSION_BUS_ADDRESS and"
                                           " $XDG_RUNTIME_DIR not defined"), "is-active": (1, "")})
    lines = []
    doctor.main(str(path), print_=lines.append, **healthy(tmp_path, run=run, free=lambda h, p: False))
    st = status_of(lines)
    assert st["systemd --user"] == FAIL
    assert st["gateway port 18000"] == WARN and st["dash port 17000"] == WARN
    assert not any("is not running" in ln for ln in lines)
    assert any("see the systemd --user line" in ln for ln in lines if ln.startswith("WARN  gateway port"))


def test_engine_log_counts_only_lines_of_the_current_start(tmp_path):
    path = write_config(tmp_path)
    StateFile(tmp_path / "active.json").set("crucible", READY, now=1.0)
    line = json.dumps({"MESSAGE": "crucible_prefill done=4096"})
    run = Runner({"is-system-running": (0, "running\n"), "InvocationID": (0, "0123456789abcdef0123456789abcdef\n"),
                  "_SYSTEMD_INVOCATION_ID=0123456789abcdef0123456789abcdef": (0, ""), "-u kiln-crucible.service": (0, line)})
    lines = []
    doctor.main(str(path), print_=lines.append, **healthy(tmp_path, run=run))
    assert status_of(lines)["engine log"] == WARN


# -- kiln check (newcomer test M6, H1) ------------------------------------------------------------------

@pytest.mark.parametrize("old,new,problem", [
    ("context = 4096", 'context = "big"', "context"),
    ("context = 4096", "context = [1]", "context"),
    ("context = 4096", "context = = 4096", "line 13"),
])
def test_check_prints_one_clean_line_for_a_broken_file(tmp_path, capsys, old, new, problem):
    path = write_config(tmp_path)
    path.write_text(path.read_text().replace(old, new))
    assert cli.main(["--config", str(path), "check"]) == 2
    out = capsys.readouterr()
    err = out.err.strip()
    assert len(err.splitlines()) == 1 and "Traceback" not in out.err + out.out
    assert err.startswith(f"kiln: {path}: ") and problem in err


def test_check_fails_a_pack_without_config_json(tmp_path, capsys):
    path = write_config(tmp_path, pack=tmp_path / "nowhere")
    assert cli.main(["--config", str(path), "check"]) == 1
    out = capsys.readouterr()
    assert "FAIL  pack crucible" in out.out + out.err and "config.json" in out.out + out.err
    good = tmp_path / "good"
    good.mkdir()
    assert cli.main(["--config", str(write_config(good)), "check"]) == 0
