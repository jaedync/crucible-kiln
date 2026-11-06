"""The rack panel's shared screen on the dashboard: client messages on /api/v1/live, the broadcast, the file.

A following /panel session sends `panel.set`, `panel.touch`, `panel.settings` and `panel.mute`; the dashboard applies a
change and queues one `panel` message for every socket in the same event-loop step, then writes panel.json.
The rules themselves are pure (live/panel_screen.py). The socket now reads untrusted input, so each message
is small (MSG_MAX_BYTES, set on the WebSocketResponse), of a known type, and rate limited per socket.
"""
from __future__ import annotations

import asyncio
import json
import os
from dataclasses import dataclass
from pathlib import Path

from kiln.engines.state import FAILED, STARTING, ActiveState
from kiln.live import panel_screen as ps

MSG_MAX_BYTES = 4096
RATE_PER_S = 10.0
RATE_BURST = 20.0
MAX_REQ = 2 ** 31 - 1
NOT_ALLOWED = "this device may not change the rack panel"
_BAD_REQ = object()


@dataclass(frozen=True)
class Bucket:
    tokens: float
    at: float

    def take(self, now: float) -> tuple[bool, "Bucket"]:
        tokens = min(RATE_BURST, self.tokens + max(0.0, now - self.at) * RATE_PER_S)
        return (True, Bucket(tokens - 1, now)) if tokens >= 1 else (False, Bucket(tokens, now))


class PanelClient:
    """One socket: who it is (fixed for the life of the socket) and its rate limit."""

    def __init__(self, sid: int, who: str | None, source: str, may_write: bool, now: float):
        self.sid, self.who, self.source, self.may_write = sid, who, source, may_write
        self.bucket = Bucket(RATE_BURST, now)
        self.throttled = False  # refused once already: further messages over the limit get no answer

    def admit(self, now: float) -> bool | None:
        """True: go on. False: refuse this one. None: drop it without an answer."""
        ok, self.bucket = self.bucket.take(now)
        if ok:
            self.throttled = False
            return True
        first, self.throttled = not self.throttled, True
        return False if first else None


def refused(req, error: str) -> dict:
    return {"type": "panel.refused", "req": req, "error": error}


def _parse(raw) -> dict | None:
    if not isinstance(raw, str):
        return None
    try:
        msg = json.loads(raw)
    except ValueError:
        return None
    return msg if isinstance(msg, dict) else None


def _req(msg: dict | None):
    req = None if msg is None else msg.get("req")
    if req is None or (isinstance(req, int) and not isinstance(req, bool) and 0 <= req <= MAX_REQ):
        return req
    return _BAD_REQ


def _plan_failed(fleet) -> tuple[int | None, str | None]:
    """The night plan's failed items and the last one's label (collect/gpu_queue.py checked both). None
    without fleet data: a missing reading must not clear a flag."""
    if not isinstance(fleet, dict) or fleet.get("error"):
        return None, None
    plan = fleet.get("plan")
    items = plan.get("items") if isinstance(plan, dict) else None
    items = items if isinstance(items, list) else []
    failed = [it for it in items if isinstance(it, dict) and it.get("state") == "fail"]
    label = failed[-1].get("label") if failed else None
    return len(failed), (label[:80] if isinstance(label, str) else None)


def tick_inputs(st: ActiveState, schedule: dict | None, hw: dict, fleet: dict | None = None) -> ps.Inputs:
    """The rules' inputs from what the tick already has: the engine state, the day window, the sensors, and
    the fleet view (/api/v1/panel, cached) for the night plan's failures."""
    in_day = schedule.get("in_day") if isinstance(schedule, dict) else None
    edge = hw.get("edge_c")
    failed, label = _plan_failed(fleet)
    return ps.Inputs(failed=st.state == FAILED,
                     edge_c=float(edge) if isinstance(edge, (int, float)) and not isinstance(edge, bool) else None,
                     switching=st.target is not None or st.state == STARTING,
                     night=(not in_day) if isinstance(in_day, bool) else None,
                     plan_failed=failed, plan_failed_label=label)


def load(path: Path, wall: float, now: float) -> tuple[ps.PanelScreen, bool]:
    """The state from panel.json, and whether the file needs writing again (a stale screen went home)."""
    try:
        doc = json.loads(path.read_text())
    except FileNotFoundError:
        return ps.PanelScreen(), False
    except (OSError, ValueError) as e:
        print(f"kiln-dash: {path.name} unreadable, the panel starts at home: {type(e).__name__}", flush=True)
        doc = None
    st = ps.from_file(doc, wall, now)
    return st, ps.to_file(st) != doc


class PanelSync:
    def __init__(self, path: Path, broadcast, *, clock, mono):
        self.path, self._broadcast, self.clock, self.mono = Path(path), broadcast, clock, mono
        self._save_due = False
        self.state, dirty = load(self.path, clock(), mono())
        if dirty:
            self._save()
        self._handlers = {"panel.set": self._set, "panel.touch": self._touch, "panel.settings": self._settings,
                          "panel.mute": self._mute}

    def view(self) -> dict:
        return ps.view(self.state, settings=True)

    def client(self, me: dict, *, local: bool, same_origin: bool) -> PanelClient:
        # Only the kiosk on this machine is a touch on the rack; any other device changes it remotely.
        return PanelClient(me["sid"], me.get("identity"), "touch" if local else "remote",
                           bool(me.get("can_act")) and same_origin, self.mono())

    async def receive(self, client: PanelClient, raw) -> dict | None:
        """Apply one client message. Returns the answer for the sender alone (a refusal), or None."""
        msg = _parse(raw)
        req = _req(msg)
        admitted = client.admit(self.mono())
        if admitted is None:
            return None
        if not admitted:
            return refused(None if req is _BAD_REQ else req, "too many messages; wait a second")
        if msg is None:
            return refused(None, "a panel message must be a JSON object in a text frame")
        kind = msg.get("type")
        handler = self._handlers.get(kind) if isinstance(kind, str) else None
        if req is _BAD_REQ:
            return refused(None, "req must be a whole number")
        if handler is None:
            return refused(req, "unknown message type")
        if not client.may_write:
            return None if msg["type"] == "panel.touch" else refused(req, NOT_ALLOWED)
        try:
            return await handler(client, msg, req)
        except ValueError as e:
            return refused(req, str(e))

    def _by(self, client: PanelClient, req) -> ps.By:
        return ps.By(client.source, client.who, client.sid, req)

    async def _set(self, client: PanelClient, msg: dict, req) -> None:
        screen = msg.get("screen")
        if not isinstance(screen, str):
            raise ValueError("unknown screen")
        await self._commit(ps.person_set(self.state, screen, self._by(client, req), self.mono(), self.clock()))

    async def _touch(self, _client: PanelClient, _msg: dict, _req) -> None:
        self.state = ps.person_touch(self.state, self.mono())  # memory only: a touch never writes the file

    async def _settings(self, client: PanelClient, msg: dict, req) -> None:
        nxt = ps.person_settings(self.state, msg.get("settings"), self._by(client, req), self.mono(), self.clock())
        await self._commit(nxt, settings=True)

    async def _mute(self, client: PanelClient, msg: dict, req) -> None:
        kind = msg.get("kind")
        if kind is not None and not isinstance(kind, str):
            raise ValueError("unknown alarm kind")
        await self._commit(ps.person_mute(self.state, kind, self._by(client, req), self.mono(), self.clock()))

    async def tick(self, inputs: ps.Inputs) -> None:
        await self._commit(ps.step(self.state, inputs, self.mono(), self.clock()))

    async def _commit(self, nxt: ps.PanelScreen, settings: bool = False) -> None:
        changed = nxt.seq != self.state.seq
        self.state = nxt
        if changed:
            await self._broadcast({"type": "panel", **ps.view(nxt, settings=settings)})
            self._save_soon()

    def _save_soon(self) -> None:
        # After the broadcast's sends had their turn on the loop; several changes in one step write once.
        if not self._save_due:
            self._save_due = True
            asyncio.get_running_loop().call_soon(self._save)

    def _save(self) -> None:
        self._save_due = False
        try:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            tmp = self.path.with_name(self.path.name + ".tmp")
            tmp.write_text(json.dumps(ps.to_file(self.state)))
            os.replace(tmp, self.path)
        except OSError as e:  # the screen still works from memory; the next change tries again
            print(f"kiln-dash: could not write {self.path.name}: {type(e).__name__}: {e}", flush=True)

    def flush(self) -> None:
        if self._save_due:
            self._save()
