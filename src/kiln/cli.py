"""kiln <command>: run the gateway, dashboard or host agent, render engine units, inspect config."""
from __future__ import annotations

import argparse
import asyncio
import json
import sys
import urllib.error
import urllib.request

from kiln import __version__, config


def _has_agent() -> bool:
    """Help text only: the public package leaves the host agent out."""
    return config._has_module("kiln.agent.run")


def _gateway(cfg, _args) -> int:
    from kiln.gateway.app import serve
    asyncio.run(serve(cfg))
    return 0


def _dash(cfg, _args) -> int:
    from kiln.dash.app import serve
    asyncio.run(serve(cfg))
    return 0


def _units(cfg, args) -> int:
    from kiln.engines.manager import render_unit
    if args.write:
        from kiln.engines.manager import Manager
        from kiln.engines.state import StateFile
        m = Manager(cfg, StateFile(cfg.state_file), on_event=None, gtt_used=lambda: None)
        changed = m.write_units(args.active)
        print(f"{'wrote' if changed else 'unchanged'}: {cfg.quadlet_dir} (run systemctl --user daemon-reload)")
        return 0
    for p in cfg.profiles.values():
        print(f"# {p.container_name}.container\n{render_unit(cfg, p, p.name == args.active)}")
    return 0


def _check(cfg, _args) -> int:
    from kiln import doctor
    print(f"host {cfg.host}; gateway {cfg.gateway_listen}; dash {cfg.dash_listen}; db {cfg.db_path}")
    for p in cfg.profiles.values():
        kv = f"{p.kv.positions:,} positions" + (f", {p.kv.bytes / 2**30:.1f} GiB" if p.kv.bytes else "")
        print(f"  {p.name}: {p.engine} serving {p.model} on 127.0.0.1:{p.port}, ctx {p.context:,}, KV {kv}")
    for h in cfg.hosts.values():
        print(f"  host {h.name}: {h.engine} serving {h.model} as {h.profile}; pushes from tailnet node {h.node}")
    bad = [r for r in doctor.check_packs(cfg) if r.status == doctor.FAIL]
    for r in bad:
        print(r.line())
    return 1 if bad else 0


# kiln-dash needs about half a second after `systemctl start` to listen; a refused connection sent nothing,
# so the CLI tries again for this long before it gives up.
DASH_START_S = 10.0


def _action(cfg, path: str, body: dict, timeout: float = 200, retry_s: float = DASH_START_S) -> int:
    import time
    host, port = cfg.dash_listen
    host = config.url_host(config.local_host(host))
    where = f"{host}:{port}"
    deadline = time.monotonic() + retry_s
    while True:
        req = urllib.request.Request(f"http://{where}/api/v1/engines/{path}", method="POST",
                                     data=json.dumps(body).encode(),
                                     headers={"Content-Type": "application/json", "X-Kiln-Action": "1"})
        try:
            with urllib.request.urlopen(req, timeout=timeout) as r:
                print(r.read().decode())
            return 0
        except urllib.error.HTTPError as e:
            print(f"refused ({e.code}): {e.read().decode()}", file=sys.stderr)
            return 1
        except urllib.error.URLError as e:
            if isinstance(e.reason, ConnectionRefusedError) and time.monotonic() < deadline:
                time.sleep(0.25)
                continue
            why = e.reason
        except OSError as e:  # a timeout or a reset while kiln-dash works on the request
            why = e
        print(f"kiln: kiln-dash does not answer on {where} ({why}); check it with"
              " `systemctl --user status kiln-dash`", file=sys.stderr)
        return 1


def _switch(cfg, args) -> int:
    body = {"profile": args.profile, "confirm": args.confirm, "remember": args.remember}
    if args.token_file:
        with open(args.token_file) as f:
            body["eval_token"] = f.read().strip()
    return _action(cfg, "switch", body)


def _stop(cfg, args) -> int:
    # A stop waits for the unit (up to 150 s) and then for the GPU memory (up to 120 s).
    return _action(cfg, "stop", {"reason": args.reason}, timeout=330)


def _hold(cfg, args) -> int:
    # Only the file: a switch script must be able to hold while kiln-dash restarts.
    from kiln.engines.admission import MAX_HOLD_S, AdmissionHold
    if not 1 <= args.seconds <= MAX_HOLD_S:
        print(f"kiln: --seconds must be from 1 to {MAX_HOLD_S}, got {args.seconds}", file=sys.stderr)
        return 2
    print(json.dumps(AdmissionHold(cfg.admission_file).write(args.seconds, args.reason)))
    return 0


def _release(cfg, _args) -> int:
    from kiln.engines.admission import AdmissionHold
    print("released" if AdmissionHold(cfg.admission_file).release() else "no hold")
    return 0


def _dash_answers(cfg) -> bool:
    """True while kiln-dash runs or starts: its port answers, or systemd reports the unit active."""
    import shutil
    import socket
    import subprocess
    if shutil.which("systemctl"):
        state = subprocess.run(["systemctl", "--user", "is-active", "kiln-dash"], capture_output=True, text=True).stdout
        if state.strip() in ("active", "activating", "reloading"):
            return True
    host, port = cfg.dash_listen
    try:
        with socket.create_connection((config.local_host(host), port), timeout=1):
            return True
    except OSError:
        return False


def _backdate_reference(cfg, args) -> int:
    """Apply kiln.toml's reference prices from a past time (a price change Kiln learned of late)."""
    from datetime import datetime
    import time
    from kiln.pricing.cards import PriceBook
    from kiln.store.db import connect
    try:
        since = datetime.fromisoformat(args.since)
    except ValueError:
        print(f"kiln: --since: not an ISO time: {args.since}", file=sys.stderr)
        return 2
    if since.tzinfo is None:
        print("kiln: --since needs a zone offset, for example 2026-10-07T17:00+00:00", file=sys.stderr)
        return 2
    # kiln-dash is the only writer while it runs (store/db.py).
    if not args.dry_run and _dash_answers(cfg):
        print("kiln: stop kiln-dash first (systemctl --user stop kiln-dash)", file=sys.stderr)
        return 1
    ts, r = since.timestamp(), cfg.reference
    if r is None:
        print("kiln: kiln.toml has no [pricing.reference]", file=sys.stderr)
        return 2
    conn = connect(cfg.db_path)
    total = ("SELECT COUNT(*), COALESCE(SUM(c.usd), 0) FROM request_costs c JOIN requests q ON q.id = c.request_id"
             " WHERE c.source = 'reference' AND q.ts_start >= ?")
    n, before = conn.execute(total, (ts,)).fetchone()
    print(f"reference {r.name}: {r.input_per_m} / {r.cached_per_m} / {r.output_per_m} per M from {since.isoformat()}")
    print(f"requests since then: {n}, reference cost now ${before:,.2f}")
    for c in conn.execute("SELECT id, effective_from, input_per_m, cached_per_m, output_per_m FROM price_cards"
                          " WHERE source = 'reference' ORDER BY effective_from"):
        print(f"  card #{c[0]} from {datetime.fromtimestamp(c[1]).astimezone().isoformat(timespec='seconds')}:"
              f" {c[2]} / {c[3]} / {c[4]}")
    if args.dry_run:
        return 0
    try:
        card, ids = PriceBook(conn).backdate("reference", "*", r.provider_model, r.input_per_m, r.cached_per_m,
                                             r.output_per_m, effective_from=ts, now=time.time(), raw={"name": r.name})
    except ValueError as e:
        print(f"kiln: refused: {e}", file=sys.stderr)
        return 2
    if card is None:
        print("unchanged: these prices were already in force then")
        return 0
    after = conn.execute(total, (ts,)).fetchone()[1]
    print(f"card {card.id} added; re-priced {len(ids)} requests; reference cost since then ${after:,.2f}")
    return 0


def _backfill_prefill(cfg, args) -> int:
    """Recompute the prefill part of series_1s for a range with the current rules (spread.SERIES_VERSION)."""
    from datetime import datetime
    from kiln.collect import backfill
    from kiln.store.db import connect
    times = []
    for name in ("since", "until"):
        try:
            t = datetime.fromisoformat(getattr(args, name))
        except ValueError:
            print(f"kiln: --{name}: not an ISO time: {getattr(args, name)}", file=sys.stderr)
            return 2
        if t.tzinfo is None:
            print(f"kiln: --{name} needs a zone offset, for example 2026-01-15T14:00+00:00", file=sys.stderr)
            return 2
        times.append(t)
    if times[1] < times[0]:
        print("kiln: --until is before --since", file=sys.stderr)
        return 2
    # kiln-dash is the only writer while it runs (store/db.py).
    if not args.dry_run and _dash_answers(cfg):
        print("kiln: stop kiln-dash first (systemctl --user stop kiln-dash)", file=sys.stderr)
        return 1
    conn = connect(cfg.db_path)
    changes = backfill.plan(conn, times[0].timestamp(), times[1].timestamp(), cfg.max_prefill_tps)
    for c in changes:
        when = datetime.fromtimestamp(c.t).astimezone().isoformat(timespec="seconds")
        print(f"{when}  {c.profile:<24} {c.model:<24} {c.client:<20} {c.was:>8,} -> {c.now:>8,}"
              f"  ({c.now - c.was:+,})")
    print(f"{len(changes)} seconds change, {sum(c.now - c.was for c in changes):+,} tokens of prefill")
    if args.dry_run:
        print("(dry run: nothing written)")
    else:
        backfill.apply(conn, changes)
        print(f"applied to {cfg.db_path}")
    return 0


def _init(args) -> int:
    """kiln init: runs before any kiln.toml exists, so it takes no config."""
    import getpass
    import os
    import shutil
    import socket
    from pathlib import Path
    from kiln import install
    for flag, value in (("--crucible", args.crucible), ("--pack", args.pack)):
        if not value.strip():  # an unset shell variable: Path("") would be the current directory
            what = ("a checkout of crucible-strix-halo with build/engine/crucible built" if flag == "--crucible"
                    else "the EXL3 checkpoint directory (the one that holds config.json)")
            print(f"kiln init: {flag} is empty; give it {what}", file=sys.stderr)
            return 2
    return install.init(crucible_dir=Path(args.crucible), pack=Path(args.pack), home=Path.home(), env=os.environ,
                        systemctl=install.run_systemctl, kiln=install.kiln_command(),
                        host=socket.gethostname().split(".")[0] or "localhost",
                        python=shutil.which("python3") or sys.executable, user=getpass.getuser(),
                        gateway_port=args.port,
                        dash_port=args.dash_port, engine_port=args.engine_port, ctx=args.ctx, slots=args.slots,
                        lan=args.lan, force=args.force, start=not args.no_start, wait_ready=install.wait_for_ports)


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(prog="kiln", description="Kiln: inference gateway and dashboard for a Crucible engine")
    ap.add_argument("--config", help="path to kiln.toml (default $KILN_CONFIG or ~/.config/kiln/kiln.toml)")
    ap.add_argument("--version", action="version", version=__version__)
    sub = ap.add_subparsers(dest="cmd", required=True)
    sub.add_parser("gateway", help="run the OpenAI-compatible gateway")
    sub.add_parser("dash", help="run the dashboard, ingest and engine manager")
    u = sub.add_parser("units", help="print (or --write) the engine Quadlet units")
    u.add_argument("--active", help="profile wanted at boot")
    u.add_argument("--write", action="store_true")
    sub.add_parser("check", help="validate kiln.toml and print a summary")
    dm = sub.add_parser("demo", help="run the real dashboard and gateway on a fake engine and fake sensors"
                                      " (no GPU, engine or systemd; no kiln.toml)")
    dm.add_argument("--port", type=int, default=7000, help="dashboard port on 127.0.0.1 (default 7000)")
    dm.add_argument("--speed", type=float, default=1.0,
                    help="play the scenario N times faster: times and token counts shrink, rates stay real")
    dm.add_argument("--seed", type=int, default=0, help="the same seed gives the same numbers")
    dm.add_argument("--once", action="store_true", help="play the scenario once and exit (0 if every request"
                                                       " succeeded)")
    dm.add_argument("--no-history", action="store_true", help="start with an empty store (no past two weeks)")
    s = sub.add_parser("switch", help="ask the running dashboard to switch engines")
    s.add_argument("profile")
    s.add_argument("--token-file", help="evaluation lock token (lets the evaluation holding the lock switch)")
    s.add_argument("--remember", action="store_true",
                   help="make this the profile the morning start uses (as a dashboard selection does)")
    s.add_argument("--confirm", action="store_true",
                   help="with [schedule]: pause the shared GPU queue and wait for its running jobs instead of"
                        " refusing")
    st = sub.add_parser("stop", help="ask the running dashboard to stop the active engine")
    st.add_argument("--reason", default="stopped from the command line")
    h = sub.add_parser("hold", help="make new gateway requests wait (a planned switch); run again to refresh")
    h.add_argument("--seconds", type=int, required=True, help="how long the hold lasts if nobody releases it"
                                                              " (1 to 3600)")
    h.add_argument("--reason", default="")
    sub.add_parser("release", help="end the admission hold (no error if there is none)")
    b = sub.add_parser("backdate-reference",
                       help="apply kiln.toml's reference prices from a past time and re-price the requests since"
                            " (stop kiln-dash first)")
    b.add_argument("--since", required=True, help="ISO time with zone offset, for example 2026-10-07T17:00+00:00")
    b.add_argument("--dry-run", action="store_true", help="print what would change and write nothing")
    bp = sub.add_parser("backfill-prefill",
                        help="recompute the prefill part of series_1s for a range from the stored requests"
                             " (a rebuild of the whole history follows at the next kiln-dash start anyway)")
    bp.add_argument("--since", required=True, help="ISO time with zone offset, for example 2026-01-15T14:00+00:00")
    bp.add_argument("--until", required=True, help="ISO time with zone offset, for example 2026-01-15T15:00+00:00")
    bp.add_argument("--dry-run", action="store_true", help="print what would change and write nothing")
    sub.add_parser("agent", help="run the host collector that pushes to a Kiln dashboard"
                                 " (--config is its agent.toml, default ~/.config/kiln/agent.toml)"
                   if _has_agent() else "the host agent (not part of this install)")
    i = sub.add_parser("init", help="write ~/.config/kiln/kiln.toml with a Crucible profile and install the"
                                    " kiln-gateway and kiln-dash user units (no prompts)")
    i.add_argument("--crucible", required=True, metavar="DIR",
                   help="a checkout of crucible-strix-halo with build/engine/crucible built")
    i.add_argument("--pack", required=True, metavar="DIR", help="the EXL3 checkpoint directory the engine loads")
    i.add_argument("--port", type=int, default=8000, help="gateway port (default 8000)")
    i.add_argument("--dash-port", type=int, default=7000, help="dashboard port (default 7000)")
    i.add_argument("--engine-port", type=int, default=8790, help="Crucible server port (default 8790)")
    i.add_argument("--ctx", type=int, default=32768, help="context per slot, the server's --ctx (default 32768)")
    i.add_argument("--slots", type=int, default=1, help="parallel requests, the server's --streams (default 1)")
    i.add_argument("--lan", action="store_true",
                   help="listen on every interface (default: 127.0.0.1 only for the gateway and dashboard)")
    i.add_argument("--force", action="store_true", help="replace files that exist")
    i.add_argument("--no-start", action="store_true",
                   help="enable the units but do not start them (default: start or restart them now)")
    sub.add_parser("doctor", help="check the setup: one PASS, WARN or FAIL line per check (exit 1 on a FAIL)")
    args = ap.parse_args(argv)
    if args.cmd == "init":  # writes the config, so it cannot need one
        return _init(args)
    if args.cmd == "doctor":  # reports a config that does not load as one of its checks
        from kiln import doctor
        return doctor.main(args.config)
    if args.cmd == "agent":  # runs on another host: it has no kiln.toml
        try:
            from kiln.agent.run import main as agent_main
        except ImportError:  # the public package leaves the host agent out
            print("kiln: the host agent is not part of this install", file=sys.stderr)
            return 2
        return agent_main(args.config)
    if args.cmd == "demo":  # builds its own config in a temporary folder
        from kiln.demo.run import main as demo_main
        return demo_main(args.port, args.speed, args.seed, args.once, not args.no_history)
    try:
        cfg = config.load(args.config)
    except OSError as e:
        print(f"kiln: config: {e}", file=sys.stderr)
        return 2
    except config.ConfigError as e:
        print(f"kiln: {args.config or config.default_path()}: {e}", file=sys.stderr)
        return 2
    handlers = {"gateway": _gateway, "dash": _dash, "units": _units, "check": _check, "switch": _switch,
                "stop": _stop, "hold": _hold, "release": _release, "backdate-reference": _backdate_reference,
                "backfill-prefill": _backfill_prefill}
    return handlers[args.cmd](cfg, args)


if __name__ == "__main__":
    sys.exit(main())
