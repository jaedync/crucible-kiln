"""Which gateway stream an engine's prefill progress line belongs to.

Engine progress lines carry the engine's own request number, never the gateway's id. Crucible and Halogen
number requests in the order they arrive, and the gateway forwards them in the order it receives them. So
once one number is known to be one stream (a moment when only that stream prefills), the stream for another
number is that many starts later in the gateway's start order.

Lines reach the dashboard in journal batches, often after the stream's first token, so the caller
judges who was prefilling at the line's own time, a stream's number is retired (not freed) when it
leaves prefill, and the line that ends a prompt makes a new pairing only with a stream that started before
that prompt began. A short prompt has that one line only, and a request that does not stream needs it: its
decode lines go only to a stream its own prefill line paired.

Replayed against 26 h of the test machine's logs with lines arriving 0.3 s late (the same within 0 to 3 s): 97.8% of
the single-prefill lines the panel would show got progress, and 77% with prefills overlapping. One
request's lines (6) went to the wrong stream, with four prompts prefilling at once. Limits: requests sent
straight to the engine (probes, direct requests) take an engine number but are no gateway start, and a
gateway request that never reaches the engine is a start without a number; either shifts the count until
a lone prefill sets a new anchor. A job the engine starts itself is skipped when the engine says so
(Crucible's continuation of a think turn it closed: `continuation`).

The last-line pairing, replayed against 3 h of a test machine's logs (a batch client with 1k-token prompts
that do not stream, two at a time): 1303 of 1410 such requests reached decode live (before it: 1), 2 went to
the wrong stream, and the streamed requests kept their pairings.
"""
from __future__ import annotations

from bisect import insort
from collections import OrderedDict

CLOSE_S = 0.25  # two starts this close may reach the engine in either order
RETIRED_S = 30.0  # a finished prefill's number keeps its stream this long: its last lines arrive late
# A number with no gateway start is skipped this long: newer pairings replace any older anchor well before,
# and an engine that restarts numbers from 1 again must not skip it.
EXTRA_S = 600.0


class ProgressPairs:
    KEEP = 1000  # starts per profile and pairings kept: far more than can prefill at once

    def __init__(self):
        self.starts: dict[str, list[tuple[float, str]]] = {}  # profile -> (started, id) in start order
        self.pairs: OrderedDict[tuple[str, int], str] = OrderedDict()  # (profile, engine number) -> id
        self.paired: dict[str, tuple[str, int]] = {}  # id -> (profile, engine number)
        self.retired: OrderedDict[tuple[str, int], tuple[str, float]] = OrderedDict()  # key -> (id, when)
        self.anchor: dict[str, tuple[int, str]] = {}  # profile -> latest (engine number, id)
        self.extra: dict[str, list[tuple[int, float]]] = {}  # profile -> (engine number, when) of no gateway start

    def started(self, profile: str, rid: str, ts: float) -> None:
        order = self.starts.setdefault(profile, [])
        if any(r == rid for _, r in order[-64:]):  # a gateway reconnect replays the open requests
            return
        insort(order, (ts, rid))
        del order[:-self.KEEP]

    def finished(self, rid: str, now: float) -> None:
        """The stream left prefill (first token or end). Its number is retired, not forgotten: the journal
        reaches us in batches, so its last progress lines often arrive after the first token, and they
        must not go to another stream. After RETIRED_S the number is free again, for an engine that
        restarted and counts from 1."""
        key = self.paired.pop(rid, None)
        if key is not None:
            self.pairs.pop(key, None)
            self.retired[key] = (rid, now)
            while len(self.retired) > self.KEEP:
                self.retired.popitem(last=False)

    def continuation(self, profile: str, req: int, now: float) -> None:
        """Engine request `req` is a job the engine started itself, not a gateway start: the start order skips it."""
        kept = [(n, t) for n, t in self.extra.get(profile, []) if now - t < EXTRA_S and n != req]
        self.extra[profile] = [*kept, (req, now)]

    def _offset(self, profile: str, req: int, now: float = 0.0) -> str | None:
        anchor = self.anchor.get(profile)
        if anchor is None:
            return None
        ids = [r for _, r in self.starts.get(profile, [])]
        if anchor[1] not in ids:
            return None
        extra = {n for n, t in self.extra.get(profile, []) if now - t < EXTRA_S}
        if req in extra:
            return None
        lo, hi = sorted((anchor[0], req))
        skipped = sum(1 for n in extra if lo < n < hi)
        i = ids.index(anchor[1]) + (req - anchor[0]) + (skipped if req < anchor[0] else -skipped)
        return ids[i] if 0 <= i < len(ids) else None

    def _close(self, profile: str, rid: str) -> bool:
        """Whether another start (prefilling or not) is too close to `rid`'s to tell their order."""
        order = self.starts.get(profile, [])
        mine = next((ts for ts, r in order if r == rid), None)
        return mine is None or any(r != rid and abs(ts - mine) < CLOSE_S for ts, r in order)

    def pair(self, profile: str, req: int, prefilling: list[tuple[str, float]], complete: bool = False,
             now: float = 0.0, live: set[str] = frozenset(), began: float | None = None) -> str | None:
        """The stream (id) a progress line for engine request `req` belongs to, or None when unsure.

        `prefilling` is every stream of this profile still waiting for its first token: (id, started).
        `complete` marks the line that ends the prompt (done == total): it has nothing left to show.
        `began` is when that prompt began (the line's time minus its elapsed time), None when unknown.
        `live` is every open stream of this profile: a retired number stays its stream's while it is open,
        however long it decodes (Crucible's second job of a think turn it closes writes the first job's number).
        """
        key = (profile, req)
        retired = self.retired.get(key)
        if retired is not None:
            if now - retired[1] < RETIRED_S or retired[0] in live:
                self.anchor[profile] = (req, retired[0])  # still a true pairing, so a good reference
                return None
            del self.retired[key]
        started = dict(prefilling)
        pick = self.pairs.get(key)
        if pick is not None and pick not in started:
            self._forget(key)  # from before an engine restart: it must not anchor the count
            return None
        if pick is None:
            if complete:
                # It may be the late last line of a stream that decodes now and was never paired. That stream
                # started before the prompt began; one that started later is not its owner.
                if began is None:
                    return None
                prefilling = [(i, st) for i, st in prefilling if st <= began + CLOSE_S]
                started = dict(prefilling)
            if len(prefilling) == 1 and prefilling[0][0] not in self.paired:
                pick = prefilling[0][0]  # the engine reports progress, and only this stream prefills
            else:
                pick = self._offset(profile, req, now)
                # An unsure match is neither kept nor used as the reference for later offsets.
                if pick is None or pick not in started or self._close(profile, pick):
                    return None
            self._remember(key, pick)
        self.anchor[profile] = (req, pick)
        return pick

    def decoding(self, profile: str, req: int, decoding: set[str], now: float = 0.0,
                 waiting: set[str] = frozenset()) -> str | None:
        """The decoding stream (id) a decode line for engine request `req` belongs to, or None when unsure.

        `decoding` is every stream of this profile past its first token. The number its prefill paired names it
        for as long as it decodes (retired at the first token, but still true); a number never paired goes by
        the start order from the latest anchor, when no other start is too close to tell. A late line of a
        stream that ended goes to no one. A decode line never moves the anchor: it comes every second from
        every decoding stream, and an older number spans more starts, so more chances of a gap.

        `waiting` is every stream of this profile with no first token yet. One of them is named only by the
        pairing its own prefill lines made: a request that does not stream has no first token at the gateway,
        and the decode line is the first sign that it decodes. The start order is not sure enough for that.
        """
        key = (profile, req)
        known = self.retired.get(key, (self.pairs.get(key),))[0]
        if known is not None:
            return known if known in decoding or known in waiting else None
        pick = self._offset(profile, req, now)
        return pick if pick in decoding and not self._close(profile, pick) else None

    def _remember(self, key: tuple[str, int], rid: str) -> None:
        self.pairs[key] = rid
        self.paired[rid] = key
        while len(self.pairs) > self.KEEP:
            self._forget(next(iter(self.pairs)))

    def _forget(self, key: tuple[str, int]) -> None:
        rid = self.pairs.pop(key, None)
        if rid is not None and self.paired.get(rid) == key:
            del self.paired[rid]
