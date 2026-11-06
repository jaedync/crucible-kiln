"""The CLI and kiln init when kiln-dash is not listening yet (newcomer re-test N1, N4, N6)."""
import http.server
import socket
import threading
import time

import pytest

from kiln import cli, config, doctor, install
from tests.test_init import run, tree  # noqa: F401 (tree is a fixture)


def _free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def _cfg(make_cfg, port):
    return make_cfg(**{"dash.listen": f"127.0.0.1:{port}"})


def test_a_dash_that_never_answers_gives_one_clean_line(make_cfg, capsys):
    port = _free_port()
    t = time.monotonic()
    assert cli._action(_cfg(make_cfg, port), "switch", {"profile": "x"}, retry_s=0.6) == 1
    assert time.monotonic() - t >= 0.5  # it waited for a dash that starts
    err = capsys.readouterr().err
    assert "Traceback" not in err and err.count("\n") == 1
    assert f"kiln-dash does not answer on 127.0.0.1:{port}" in err and "systemctl --user status kiln-dash" in err


def test_a_dash_that_starts_late_gets_the_request(make_cfg, capsys):
    port = _free_port()

    class Handler(http.server.BaseHTTPRequestHandler):
        def do_POST(self):
            self.rfile.read(int(self.headers["Content-Length"]))
            self.send_response(200)
            self.end_headers()
            self.wfile.write(b'{"accepted": true}')

        def log_message(self, *a):
            pass

    def serve_later():
        time.sleep(0.4)
        srv = http.server.HTTPServer(("127.0.0.1", port), Handler)
        srv.timeout = 5
        srv.handle_request()
        srv.server_close()

    th = threading.Thread(target=serve_later, daemon=True)
    th.start()
    assert cli._action(_cfg(make_cfg, port), "switch", {"profile": "x"}, retry_s=5) == 0
    th.join(timeout=6)
    assert '{"accepted": true}' in capsys.readouterr().out


def test_init_waits_for_both_ports_before_it_says_started(tree):
    asked = []
    code, out, _ = run(tree, wait_ready=lambda addrs: asked.append(addrs) or [])
    assert code == 0 and asked == [[("127.0.0.1", 8000), ("127.0.0.1", 7000)]]
    assert "Started kiln-gateway and kiln-dash." in out


def test_init_says_which_port_does_not_answer_yet(tree):
    code, out, _ = run(tree, wait_ready=lambda addrs: [addrs[1]])
    assert code == 0
    assert "Started kiln-gateway and kiln-dash." not in out
    assert "127.0.0.1:7000 does not answer yet" in out and "journalctl --user -u kiln-dash" in out


def test_init_without_start_does_not_wait(tree):
    asked = []
    run(tree, start=False, wait_ready=lambda addrs: asked.append(addrs) or [])
    assert asked == []


def test_wait_for_ports_returns_the_ports_that_stay_closed():
    closed = _free_port()
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        s.listen()
        open_ = s.getsockname()[1]
        missing = install.wait_for_ports([("127.0.0.1", open_), ("127.0.0.1", closed)], timeout_s=0.5)
    assert missing == [("127.0.0.1", closed)]


@pytest.mark.parametrize("key, value", [("context", 0), ("context", -5), ("slots", 0), ("port", 0),
                                        ("port", 70000)])
def test_a_profile_number_out_of_range_is_refused(make_cfg, key, value):
    with pytest.raises(config.ConfigError, match=key):
        make_cfg(**{f"profiles.gufo-qwen27.{key}": value})


def test_the_pack_fix_names_the_edit_that_keeps_other_profiles():
    r = doctor.check_pack("crucible", "/", True, False)
    assert "ExecStart" in r.fix and "--force" in r.fix and "replaces" in r.fix
