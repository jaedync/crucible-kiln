"""Live event feed from the gateway to the dashboard (127.0.0.1 only).

One broadcaster task drains a queue so events reach every subscriber in the
order they happened (start, tokens..., end). Token arrival times are batched
every TICK_S; each carries its exact timestamp, so batching adds no error to
rates computed downstream.
"""
from __future__ import annotations

import asyncio
import json

from aiohttp import WSMsgType, web

from kiln import SCHEMA_VERSION

TICK_S = 0.25
SEND_TIMEOUT_S = 2.0


class LiveHub:
    def __init__(self):
        self.clients: set[web.WebSocketResponse] = set()
        self.active: dict[str, dict] = {}  # id -> start event + observer
        self._queue: asyncio.Queue = asyncio.Queue(maxsize=10_000)
        self._tasks: list[asyncio.Task] = []

    def start_tasks(self) -> None:
        loop = asyncio.get_running_loop()
        self._tasks = [loop.create_task(self._broadcast()), loop.create_task(self._ticker())]

    async def stop_tasks(self) -> None:
        for t in self._tasks:
            t.cancel()
        await asyncio.gather(*self._tasks, return_exceptions=True)

    def publish(self, event: dict, target: web.WebSocketResponse | None = None) -> None:
        if not self.clients:
            return
        try:
            self._queue.put_nowait(({"v": SCHEMA_VERSION, **event}, target))
        except asyncio.QueueFull:
            # A stuck subscriber must never stall requests, and a silently dropped event (a lost
            # req.end) would leave it with a wrong picture. Drop everything and make subscribers
            # reconnect: their hello is a full reset.
            while not self._queue.empty():
                self._queue.get_nowait()
            for ws in list(self.clients):
                self.clients.discard(ws)
                asyncio.get_running_loop().create_task(ws.close())

    def request_started(self, rid: str, info: dict, observer) -> None:
        self.active[rid] = {"info": info, "obs": observer}
        self.publish({"type": "req.start", "id": rid, **info})

    def first_token(self, rid: str, ts: float) -> None:
        self.publish({"type": "req.first_token", "id": rid, "ts": ts})

    def request_ended(self, rid: str, record: dict) -> None:
        entry = self.active.pop(rid, None)
        if entry:
            self._flush(rid, entry)
        self.publish({"type": "req.end", "id": rid, "record": record})

    def _flush(self, rid: str, entry: dict) -> None:
        stats = entry["obs"].stats
        times = stats.take_pending()
        if times:
            self.publish({"type": "req.tokens", "id": rid, "t": times, "total": stats.token_events})

    async def _ticker(self) -> None:
        while True:
            await asyncio.sleep(TICK_S)
            for rid, entry in list(self.active.items()):
                self._flush(rid, entry)

    async def _broadcast(self) -> None:
        while True:
            event, target = await self._queue.get()
            data = json.dumps(event, separators=(",", ":"))
            for ws in [target] if target else list(self.clients):
                if ws not in self.clients:
                    continue
                try:
                    await asyncio.wait_for(ws.send_str(data), SEND_TIMEOUT_S)
                except Exception:  # one bad subscriber must not end the feed for the others
                    self.clients.discard(ws)
                    try:
                        await ws.close()
                    except Exception:
                        pass

    async def handle_ws(self, request: web.Request) -> web.StreamResponse:
        # The dashboard's client sends no Origin; a browser always does. A web page must not read the records.
        if "Origin" in request.headers:
            return web.Response(status=403, text="403 the events feed is for kiln-dash only\n")
        ws = web.WebSocketResponse(heartbeat=20)
        await ws.prepare(request)
        # The hello goes through the same queue as every other event, so a subscriber
        # sees a consistent order: anything queued before it, then the snapshot (which
        # the subscriber treats as a reset), then everything after.
        self.clients.add(ws)
        snapshot = [{"id": rid, **e["info"], "first_token": e["obs"].stats.first_token,
                     "total": e["obs"].stats.token_events} for rid, e in self.active.items()]
        self.publish({"type": "hello", "requests": snapshot}, target=ws)
        try:
            async for msg in ws:
                if msg.type == WSMsgType.ERROR:
                    break
        finally:
            self.clients.discard(ws)
        return ws
