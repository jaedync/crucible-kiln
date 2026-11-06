"""`kiln demo`: the real gateway and dashboard on fake edges, in one process, with no GPU and no systemd.

What is real: kiln.gateway.app.Gateway (the request path, records, live events, /health) and
kiln.dash.app.Dash (ingest, live state, the store, the REST and WebSocket API, the engine manager, the UI).
What is fake (kiln/demo/edges.py and engine.py): the engine servers, their log lines, the sensors, the
engine's processes and systemd. Everything binds 127.0.0.1, and the data folder is removed at exit.
"""
from __future__ import annotations

import asyncio
import errno
import os
import shutil
import signal
import socket
import sys
import tempfile
import time
from pathlib import Path

from aiohttp import web

from kiln import config
from kiln.collect.hw import Sensors
from kiln.collect.memory import MemorySampler
from kiln.dash.app import Dash
from kiln.demo import history, scenario
from kiln.demo.edges import FakeHardware, FakeProbe, FakeUnits, LogBus
from kiln.engines.state import StateFile
from kiln.gateway.app import Gateway, start as start_gateway
from kiln.gateway.attribution import Whois, is_loopback, normalize_ip
from kiln.pricing.cards import PriceBook
from kiln.store.db import connect

HW_EVERY_S = 0.5
ONCE_SETTLE_S = 2.0  # after the last reply: its record and engine line reach the store


def free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def demo_peer(request: web.Request) -> str | None:
    """The demo's clients all connect from loopback. A device header from loopback names one of the three
    demo devices; anything else keeps its own address, as on a real gateway."""
    remote = normalize_ip(request.remote)
    device = request.headers.get(scenario.DEVICE_HEADER)
    if is_loopback(remote) and device in scenario.DEVICES:
        return scenario.DEVICES[device]
    return remote


async def _no_lookup(_ip: str, full: bool = False) -> None:
    return None  # no tailscale in the demo


class Demo:
    def __init__(self, port: int = 7000, speed: float = 1.0, seed: int = 0, out=print, history: bool = True):
        self.port, self.speed, self.seed, self.out, self.history = port, speed, seed, out, history
        self.data = Path(tempfile.mkdtemp(prefix="kiln-demo-"))
        self.gateway_port = free_port()
        engine_ports = (free_port(), free_port())
        self.cfg = config.parse(scenario.config_dict(self.data, port, self.gateway_port, free_port(), engine_ports,
                                                     speed))
        self.bus = LogBus()
        self.units = FakeUnits(self.cfg, emit=self.bus.push, speed=speed, seed=seed)
        self.hw = FakeHardware(self.data / "hw", seed=seed)
        self.traffic = scenario.Traffic(f"http://127.0.0.1:{self.gateway_port}", speed=speed, seed=seed,
                                        log=self.out)
        self._runners: list[web.AppRunner] = []
        self._tasks: list[asyncio.Task] = []

    async def start(self) -> None:
        await self.units.boot(scenario.DEFAULT_PROFILE, StateFile(self.cfg.state_file))
        if self.history:
            self._seed_history()
        gw = Gateway(self.cfg, whois=Whois(lookup=_no_lookup), peer=demo_peer)
        self._runners += await start_gateway(self.cfg, gw)
        self.dash = Dash(self.cfg, sensors=Sensors(root=self.hw.sys, proc=self.hw.proc),
                         whois=Whois(lookup=_no_lookup), host_whois=Whois(lookup=_no_lookup),
                         mem_sampler=MemorySampler(proc=self.hw.proc, probe=FakeProbe(self.hw)),
                         log_source=self.bus.source, start_facts=self.bus.start_facts, unit_runner=self.units.run)
        # Open dashboard sockets would hold the exit for up to a minute; the demo has nothing to finish.
        runner = web.AppRunner(self.dash.app(), access_log=None, shutdown_timeout=1.0)
        await runner.setup()
        self._runners.insert(0, runner)  # the dashboard stops first: its feed client then sees the gateway go
        await web.TCPSite(runner, "127.0.0.1", self.port).start()
        self._tasks.append(asyncio.create_task(self._hw_loop()))

    def _seed_history(self) -> None:
        """Past records and engine lines (demo/history.py), ingested by the dashboard as a backlog. The reference
        card starts before them, so they are priced as they would have been then, not flagged."""
        now = time.time()
        records, lines = history.generate(now, self.seed)
        history.write(self.cfg.gateway_data, records)
        for container, line, ts in lines:
            self.bus.push(container, line, ts)
        r = self.cfg.reference
        conn = connect(self.cfg.db_path)
        try:
            PriceBook(conn).add("reference", "*", r.provider_model, r.input_per_m, r.cached_per_m, r.output_per_m,
                                now=now - (history.DAYS + 1) * 86400, raw={"name": r.name})
        finally:
            conn.close()

    async def _hw_loop(self) -> None:
        loop = asyncio.get_running_loop()
        last = loop.time()
        while True:
            await asyncio.sleep(HW_EVERY_S)
            now = loop.time()
            try:
                self.hw.update(self.units.load(now - last), now - last)
            except Exception as e:  # the sensors stay at their last reading; the demo goes on
                self.out(f"kiln demo: sensor update failed: {type(e).__name__}: {e}")
            last = now

    def banner(self, once: bool = False) -> None:
        self.out(f"kiln demo: dashboard http://127.0.0.1:{self.port}/\n"
                 f"  OpenAI-compatible gateway http://127.0.0.1:{self.gateway_port}/v1 (model {scenario.MODEL})\n"
                 f"  data folder {self.data} (removed at exit)\n"
                 f"  speed {self.speed:g}, seed {self.seed}; the scenario takes about"
                 f" {scenario.LOOP_S / self.speed:.0f} s"
                 + (" and plays once; then the demo exits." if once else " and repeats. Press Ctrl-C to stop."))

    async def stop(self) -> None:
        for t in self._tasks:
            t.cancel()
        await asyncio.gather(*self._tasks, return_exceptions=True)
        for r in self._runners:
            await r.cleanup()
        await self.units.close()
        shutil.rmtree(self.data, ignore_errors=True)


async def run(port: int = 7000, speed: float = 1.0, seed: int = 0, once: bool = False, out=print,
              history: bool = True) -> int:
    demo = Demo(port, speed, seed, out, history)
    stop = asyncio.Event()
    loop = asyncio.get_running_loop()
    for sig in (signal.SIGINT, signal.SIGTERM):
        try:
            loop.add_signal_handler(sig, stop.set)
        except (NotImplementedError, RuntimeError):  # no signal handlers on this platform: Ctrl-C still raises
            pass
    try:
        await demo.start()
        demo.banner(once)
        play = asyncio.create_task(demo.traffic.play(1 if once else None, tail=not once))
        stopped = asyncio.create_task(stop.wait())
        await asyncio.wait({play, stopped}, return_when=asyncio.FIRST_COMPLETED)
        completed = play.done()
        if completed:
            play.result()  # a failure in the traffic generator is a failed run
            await asyncio.sleep(ONCE_SETTLE_S)
        for t in (play, stopped):
            t.cancel()
        await asyncio.gather(play, stopped, return_exceptions=True)
        failed = [r for r in demo.traffic.results if r["status"] != 200]
        for r in failed:
            out(f"kiln demo: a {r['client']} request failed: {r.get('error') or r['status']}")
        return 1 if once and (failed or not completed) else 0
    finally:
        await demo.stop()


def main(port: int, speed: float, seed: int, once: bool, history: bool = True) -> int:
    if speed <= 0:
        print("kiln demo: --speed must be above 0", file=sys.stderr)
        return 2
    # The demo's data is made up: show it in UTC, not in the zone of the machine that runs it.
    os.environ["TZ"] = "UTC"
    time.tzset()
    try:
        return asyncio.run(run(port, speed, seed, once, out=lambda s: print(s, flush=True), history=history))
    except OSError as e:  # the port is taken, most likely
        if e.errno == errno.EADDRINUSE:
            print(f"kiln demo: port {port} is in use; stop the program that uses it, or give another port, for"
                  f" example `kiln demo --port {port + 1}`", file=sys.stderr)
        else:
            print(f"kiln demo: {e}", file=sys.stderr)
        return 1
    except KeyboardInterrupt:
        return 0
