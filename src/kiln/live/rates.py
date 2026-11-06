"""Live state from the gateway's event feed (ARCHITECTURE.md rules 2-4).

Rates come from exact token arrival times. A stream's rate is the tokens that
arrived in the last WINDOW_S divided by min(WINDOW_S, time since its first
token); the aggregate is the sum over active streams, so it drops to zero the
instant the last stream ends. Per-second history counts tokens by arrival
second, so it never lags.

The per-second history is append-only: a second sent to browsers never
changes. A token batch that arrives after its second was sent counts in the
next second not yet sent. Prefill is counted as it happens, at the stream's
reported (or the profile's recent) rate, because its exact count is only
known when it ends. The exact, respread counts go to series_1s
(collect/spread.py), which the longer chart ranges read.

Engines don't always send exactly one stream event per token (Halogen sends
about 0.98: some tokens carry no text of their own). Each profile's recent
finished requests give a measured tokens-per-event ratio; live figures are
events times that ratio and are marked approximate.

Arrival is not generation: Crucible holds a tool call's non-string parameters
back until they close, so a stream can be silent for a minute while the engine
decodes. Its decode lines (about one a second) give the stream's real count and
rate: while its last line is recent the stream shows them, and the chart counts
the tokens they report beyond those already counted from arrivals.
"""
from __future__ import annotations

import time
from collections import OrderedDict, deque
from dataclasses import dataclass, field

from kiln.live.pairing import ProgressPairs
from kiln.live.prefill import (COMMAND_S, Report, Track, add_report, chunk_of, progress_view, prompt_of,
                               report_rate)

WINDOW_S = 2.0
HISTORY_S = 15 * 60  # per-second history kept in memory for the live chart
EXACT_TOLERANCE = 1  # usage may count one token (e.g. end of sequence) that is never streamed
RATIO_HISTORY = 20  # recent requests per profile used to decide whether live counts are exact
# Token batches arrive every 250 ms, so a second is only emitted once this much later; a batch that
# still lands in an emitted second counts in the next open one, so a drawn second never changes.
EMIT_LAG_S = 0.4
# Before a prompt's first progress report, its first seconds count no prefill: a turn of a cached
# conversation reads a few hundred tokens in well under this, and the profile's rate over its whole time to
# first token (command overhead, a cache restore) would draw far more prefill than it did. The browser's own
# count waits as long (rule 4). A long prompt's first seconds are missing from the live chart instead.
PRE_REPORT_WAIT_S = 1.5
# Engine progress lines arrive in journal batches, after the stream may have moved on or ended: who was
# prefilling is judged at the line's own time, from live streams and those that ended this recently.
ENDED_KEPT_S = 60.0
# The engine writes a decode line about once a second and the journal delivers it 0.3-0.6 s later, so a
# line this old means the engine stopped (a cancelled turn, a pause for another prompt's prefill) or the
# lines did: the gateway's arrivals decide the rate again.
ENGINE_FRESH_S = 2.5


def _num(v):
    """Exact counts stay integers; calibrated estimates keep two decimals."""
    return v if isinstance(v, int) else (round(v) if abs(v - round(v)) < 1e-6 else round(v, 2))


@dataclass
class Stream:
    id: str
    client: dict
    profile: str
    model: str
    started: float
    first_token: float | None = None
    total: int = 0
    times: deque = field(default_factory=deque)  # arrival times within the window
    prefill: Track = field(default_factory=Track)  # the engine's progress reports (live/prefill.py)
    prefill_step: int | None = None  # the engine reports on fixed boundaries of this size (Halogen)
    prompt: dict | None = None  # prompt_of(prefill) when the prefill ended: the decode resets the reports
    admitted: float | None = None  # when the engine could begin it: its start, or when it left the queue
    was_queued: bool = False
    alone: bool = False  # no other prompt of its profile prefilled when it got its slot
    engine_s: float = 0.0  # engine seconds it had since its slot: each second counts 1 / prompts prefilling
    # Live prefill history (accrue_prefill): counted up to this time, this many tokens, at this rate.
    pre_to: float | None = None
    pre_sum: float = 0.0
    pre_rate: float | None = None  # the engine's own rate from its reports, kept after the first token
    conv: str | None = None  # the gateway's random conversation id: its earlier turns' drafts (live/drafts.py)
    turn_drafts: tuple[int, int] | None = None  # the running turn's (accepted, proposed) drafts, cumulative
    # The engine's decode lines: (completion tokens, seconds since the first token, tok/s between the last two
    # lines). While recent they give the stream's rate, and its tokens when the engine is ahead (figures).
    engine_decode: tuple[int, float, float | None] | None = None
    engine_at: float | None = None  # when the engine wrote the line behind engine_decode
    engine_req: int | None = None  # its engine request number: a line with another number is not its own
    # Decode tokens this stream put in the per-second history: arrivals (events × ratio, summed in arrival
    # order) or the engine's count, whichever is higher, so a token is never counted twice.
    arrived: float = 0.0
    charted: float = 0.0
    chart_from_engine: bool = True  # False: restored by a hello, so its earlier tokens were never charted here

    def rate(self, now: float, ratio: float = 1.0) -> float:
        if self.first_token is None:
            return 0.0
        while self.times and self.times[0] <= now - WINDOW_S:
            self.times.popleft()
        span = min(WINDOW_S, now - self.first_token)
        return len(self.times) * ratio / span if span > 0 else 0.0

    def figures(self, now: float, ratio: float = 1.0) -> tuple[int, float]:
        """(tokens, tok/s): the engine's count when it is ahead of the arrivals (it made them, whether or not
        they were sent), and its rate while its last decode line is recent, else the arrivals' rate."""
        tokens = round(self.total * ratio)
        if self.engine_decode is not None:
            tokens = max(tokens, self.engine_decode[0])
        if self.engine_fresh(now) and self.engine_decode[2] is not None:
            return tokens, self.engine_decode[2]
        return tokens, self.rate(now, ratio)

    def engine_fresh(self, now: float) -> bool:
        return self.engine_at is not None and self.engine_decode is not None and now - self.engine_at <= ENGINE_FRESH_S

    def view(self, now: float, ratio: float = 1.0, progress: dict | None = None, queued: bool = False) -> dict:
        tokens, tps = self.figures(now, ratio)
        return {"id": self.id, "client": self.client, "model": self.model, "profile": self.profile,
                "started": self.started, "first_token": self.first_token,
                "phase": "prefill" if self.first_token is None else "decode",
                "tokens": tokens, "tps": round(tps, 2),
                "queued": queued, "prefill_progress": progress, "prompt": self.prompt or prompt_of(self.prefill),
                "conv": self.conv,
                "draft_turn": ({"accepted": self.turn_drafts[0], "proposed": self.turn_drafts[1]}
                               if self.turn_drafts else None)}


def _engine_rate(prev: tuple[int, float, float | None] | None, tokens: int, elapsed: float):
    """The next (tokens, elapsed, tok/s) from a decode line: the rate since the previous line, or since the
    first token for the first line. A line no newer than the previous one changes nothing."""
    if prev is None:
        return tokens, elapsed, tokens / elapsed if elapsed > 0 else None
    if elapsed <= prev[1] or tokens < prev[0]:
        return prev
    return tokens, elapsed, (tokens - prev[0]) / (elapsed - prev[1])


class LiveState:
    def __init__(self, clock=time.time):
        self.clock = clock
        self.streams: dict[str, Stream] = {}
        # epoch second -> {"decode": n, "prefill": n}; prefill is filled in when a request ends
        self.seconds: dict[int, dict] = {}
        self._emitted_through = int(clock()) - 1
        self._counts: dict[str, deque] = {}  # profile -> recent (usage completion, stream events)
        self.starts: OrderedDict[str, float] = OrderedDict()  # request id -> start, for the chart's start lines
        self._slots = 0  # the active engine's slots at the last accrual: streams past them wait in its queue
        self.prefill_rates: dict[str, float] = {}  # profile -> recent cold prefill tok/s
        self.prefill_chunks: dict[str, int] = {}  # profile -> the engine's prefill chunk, seen in its reports
        self.progress_changed = False  # a report attached since the last tick: the dashboard ticks early
        self._lost: dict[str, Stream] = {}  # streams from before a lost gateway feed, until its hello
        self._viewed: float | None = None  # the last view's time: engine shares are counted between views
        self.progress_pairs = ProgressPairs()  # engine request numbers -> streams (live/pairing.py)
        self.ended: deque = deque()  # (id, profile, started, first_token, ended) of recently ended streams

    # -- gateway events ------------------------------------------------------
    def on_event(self, ev: dict) -> None:
        kind = ev.get("type")
        if kind == "hello":
            old, self.streams, self._lost = {**self._lost, **self.streams}, {}, {}
            for r in ev.get("requests", []):
                s = self._stream(r["id"], r)
                s.first_token, s.total = r.get("first_token"), r.get("total", 0)
                if (o := old.get(r["id"])) is not None:  # a reconnect must not lose a prefill's reports
                    s.prefill, s.prefill_step, s.prompt = o.prefill, o.prefill_step, o.prompt
                    s.admitted, s.was_queued, s.alone, s.engine_s = o.admitted, o.was_queued, o.alone, o.engine_s
                    s.pre_to, s.pre_sum, s.pre_rate = o.pre_to, o.pre_sum, o.pre_rate
                    s.turn_drafts, s.engine_decode = o.turn_drafts, o.engine_decode
                    s.engine_at, s.engine_req = o.engine_at, o.engine_req
                    s.arrived, s.charted, s.chart_from_engine = o.arrived, o.charted, o.chart_from_engine
                    if s.first_token is None and o.engine_decode:  # it does not stream: the engine said it decodes
                        s.first_token = o.first_token
                else:  # new here (kiln-dash restarted, or it began while the feed was lost): its tokens so far
                    # were never charted here, and must not land in one second now
                    s.arrived = s.charted = s.total * self.ratio(s.profile)
                    s.chart_from_engine = False
        elif kind == "req.start":
            self._stream(ev["id"], ev)
        elif kind == "req.first_token" and (s := self.streams.get(ev["id"])):
            self._begin_decode(s, ev["ts"])
        elif kind == "req.tokens" and (s := self.streams.get(ev["id"])):
            r = self.ratio(s.profile)
            for t in ev["t"]:
                s.times.append(t)
                s.arrived += r
                self._chart(s, int(t), s.arrived)
            s.total = ev.get("total", s.total + len(ev["t"]))
            if s.first_token is None and ev["t"]:
                s.first_token = ev["t"][0]
        elif kind == "req.end":
            now = self.clock()
            s = self.streams.get(ev["id"])
            if s is not None and s.first_token is None:
                self._accrue(s, now)  # it ended while prefilling (cancelled, or refused while queued: nothing)
            s = self.streams.pop(ev["id"], None)
            if s is not None:
                self.ended.append((s.id, s.profile, s.started, s.first_token, now))
            while self.ended and self.ended[0][4] < now - ENDED_KEPT_S:
                self.ended.popleft()
            self.progress_pairs.finished(ev["id"], now)

    def feed_lost(self) -> None:
        """The gateway feed dropped: its hello will say what still runs. Until then nothing is live."""
        self._lost, self.streams = {**self._lost, **self.streams}, {}

    def _add(self, sec: int, kind: str, n: float) -> None:
        """Count n tokens in a second; a second already sent to browsers is final, so the next open one gets them."""
        sec = max(sec, self._emitted_through + 1)
        self.seconds.setdefault(sec, {"decode": 0, "prefill": 0})[kind] += n

    def _chart(self, s: Stream, sec: int, upto: float) -> None:
        """Count a stream's decode tokens up to `upto` in the history, from the first open second on."""
        if upto > s.charted:
            self._add(sec, "decode", upto - s.charted)
            s.charted = upto

    def _stream(self, rid: str, d: dict) -> Stream:
        conv = d.get("conv")
        s = Stream(rid, d.get("client") or {}, d.get("profile") or "", d.get("model") or "",
                   d.get("ts") or d.get("started") or self.clock(),
                   conv=conv if isinstance(conv, str) and conv.isalnum() and len(conv) <= 32 else None)
        self.streams[rid] = s
        self.starts[rid] = s.started
        self.progress_pairs.started(s.profile, rid, s.started)
        return s

    def seed(self, seconds: dict[int, dict], starts: list[tuple[str, float]]) -> None:
        """The last minutes from the database when the dashboard starts: they were never sent, so they may."""
        self.seconds.update(seconds)
        for rid, ts in sorted(starts, key=lambda x: x[1]):
            self.starts.setdefault(rid, ts)

    # -- live prefill history ---------------------------------------------------
    def _queued(self, streams: list[Stream], slots: int) -> set[str]:
        """Streams past the engine's slots (by start, per profile) wait in its queue."""
        if not slots:
            return set()
        by_profile: dict[str, list[Stream]] = {}
        for s in sorted(streams, key=lambda s: s.started):
            by_profile.setdefault(s.profile, []).append(s)
        return {s.id for group in by_profile.values() for s in group[slots:]}

    def accrue_prefill(self, now: float, slots: int | None = None) -> None:
        """Count each prefilling stream's tokens up to now. Prefill is only known exactly when it ends, so the
        live history counts it as it happens, at the stream's own reported rate or the profile's recent rate
        shared by the prompts prefilling; the exact count goes to series_1s."""
        if slots is not None:
            self._slots = slots
        for s in list(self.streams.values()):
            if s.first_token is None:
                self._accrue(s, now)

    def _engine_scale(self, profile: str, streams: list[Stream], queued: set[str]) -> float:
        """How much to slow each prefilling stream so their rates sum to what that engine can do.

        Two prompts prefill "at once" only because the engine pauses one for the other, but a stream with
        its own reported rate keeps counting at that rate while it waits, so two of them would sum to
        nearly twice the engine (a Strix Halo test machine: 1,387 + 740 tok/s during one pause).
        Prompts with no figure of their own already share the profile's recent cold prefill rate, which is that engine's
        rate; this keeps the ones that have figures inside it too. One prompt alone keeps its own
        figure: nothing can double count, and the engine's report is what the chart should show. It is
        still an estimate: the drawn seconds are never corrected when the prefill ends.
        """
        hint = self.prefill_rates.get(profile)
        if not hint:
            return 1.0
        reading = [x for x in streams if x.profile == profile and x.first_token is None and x.id not in queued]
        if len(reading) < 2:  # one prompt counts at its own rate; nothing to share
            return 1.0
        shared = hint / len(reading)
        # Each stream's own rate comes from its reports, not from what a previous stream's accrual set.
        wanted = sum(report_rate(x.prefill.reports) or x.pre_rate or shared for x in reading)
        return hint / wanted if wanted > hint else 1.0

    def _accrue(self, s: Stream, until: float) -> None:
        streams = list(self.streams.values())
        queued = self._queued(streams, self._slots)
        if s.id in queued:
            return
        if s.pre_to is None and s.was_queued and s.admitted is None:
            s.pre_to = until  # it just left the queue; the next view notes when (_admit)
            return
        if (own := report_rate(s.prefill.reports)) is not None:
            s.pre_rate = own
        sharing = sum(1 for x in streams if x.profile == s.profile and x.first_token is None and x.id not in queued)
        hint = self.prefill_rates.get(s.profile)
        rate = s.pre_rate or (hint / max(1, sharing) if hint else None)
        if rate is not None:
            rate *= self._engine_scale(s.profile, streams, queued)
        start = s.admitted or s.started
        begin = s.pre_to if s.pre_to is not None else start
        if s.pre_rate is None and not s.prefill.reports:
            begin = max(begin, start + PRE_REPORT_WAIT_S)
        begin = max(begin, self._emitted_through + 1)
        if rate is None or until <= begin:
            s.pre_to = max(begin, until) if rate is not None else s.pre_to
            return
        if s.prefill.reports:  # never more than the prompt left to read
            last = s.prefill.reports[-1]
            room = max(0.0, last.total - s.prefill.start - s.pre_sum)
            until = min(until, begin + room / rate)
        t = begin
        while t < until:
            edge = min(until, int(t) + 1)
            self._add(int(t), "prefill", rate * (edge - t))
            t = edge
        s.pre_sum += rate * (until - begin)
        s.pre_to = max(until, s.pre_to or until)

    # -- facts learned when a request finishes ---------------------------------
    def record_exactness(self, profile: str, completion: int | None, events: int | None) -> None:
        if not completion or not events:
            return
        self._counts.setdefault(profile, deque(maxlen=RATIO_HISTORY)).append((completion, events))

    def ratio(self, profile: str | None) -> float:
        """Measured usage tokens per stream event for a profile (1.0 until measured or when exact).

        The median tokens per event over the recent requests' events: each request's own ratio, weighted
        by how many events it sent. Live figures multiply events by it, so it must describe a typical
        event. Tool calls hold their arguments back and send many tokens in few events; weighted by
        tokens (or pooled) they set the ratio to 9 and every live stream read hundreds of tok/s. Their
        held-back tokens land when the engine reports them (collect/spread.py)."""
        q = self._counts.get(profile or "")
        if not q or self.decode_exact(profile):
            return 1.0
        ranked = sorted((c / e, e) for c, e in q if e > 0)
        half, seen = sum(e for _, e in ranked) / 2, 0
        for r, e in ranked:
            seen += e
            if seen >= half:
                return r
        return 1.0

    def decode_exact(self, profile: str | None) -> bool:
        q = self._counts.get(profile or "")
        return bool(q) and all(abs(c - e) <= EXACT_TOLERANCE for c, e in q)

    def exactness_note(self, profile: str | None) -> str | None:
        q = self._counts.get(profile or "")
        if not q:
            return "No finished request on this engine yet to check stream events against token counts."
        if self.decode_exact(profile):
            return None
        c, e = sum(c for c, _ in q), sum(e for _, e in q)
        return (f"This engine sends about one stream event per token, not exactly one: the last {len(q)} "
                f"requests had {c:,} tokens in {e:,} events. Live figures are events × {self.ratio(profile):.3f}"
                " (the typical event's ratio); each request's seconds become exact when it ends.")

    def set_prefill_rate(self, profile: str, rate: float) -> None:
        self.prefill_rates[profile] = rate

    def set_prefill_progress(self, profile: str, done: int, total: int, elapsed_s: float | None = None,
                             at: float | None = None, step: int | None = None, req: int | None = None) -> None:
        """Attach an engine progress report. With its timing, the browser estimates between reports.

        Engine lines carry the engine's request number (`req`), not the gateway's id: with one stream
        prefilling the line is its own; with several, live/pairing.py decides or leaves it unattached.
        """
        if req is None:
            prefilling = [s for s in self.streams.values() if s.profile == profile and s.first_token is None]
            target = prefilling[0] if len(prefilling) == 1 else None
        else:
            now = self.clock()
            line_at = now if at is None else at
            complete = done >= total
            rid = self.progress_pairs.pair(profile, req, self._prefilling_at(profile, line_at),
                                           complete=complete, now=now,
                                           live={s.id for s in self.streams.values() if s.profile == profile},
                                           began=line_at - elapsed_s if complete and elapsed_s else None)
            target = self.streams.get(rid) if rid else None
        if target is None:
            return
        at = self.clock() if at is None else at
        elapsed = elapsed_s if elapsed_s is not None else max(0.0, at - target.started)
        if target.first_token is not None:
            # A late line for a stream that now decodes. A prompt that is almost all cached prefills in tenths
            # of a second, so its lines reach us after the first token: they still give its size and cached
            # start (the live cost prices it), decided once, from its first line as for a prefill.
            if target.prompt is None:
                first = add_report(Track(), Report(done, total, elapsed, at), chunk=self.prefill_chunks.get(profile),
                                   rate=self._first_chunk_rate(target, elapsed, at), step=step)
                target.prompt = prompt_of(first)
            return
        if req is not None and target.engine_req is None:
            target.engine_req = req
        track = add_report(target.prefill, Report(done, total, elapsed, at), rate=self._first_chunk_rate(target, elapsed, at),
                           chunk=self.prefill_chunks.get(profile), step=step)
        if track == target.prefill:  # a repeated, late or stray line
            return
        target.prefill, target.prefill_step = track, step
        if chunk := chunk_of(track.reports):
            self.prefill_chunks[profile] = chunk
        self.progress_changed = True

    def set_continuation(self, profile: str, req: int) -> None:
        """The engine started job `req` itself (Crucible's continuation of a think turn it closed): it took a
        request number but is no gateway start, so the pairing skips it."""
        self.progress_pairs.continuation(profile, req, now=self.clock())

    def _begin_decode(self, s: Stream, ts: float) -> None:
        """Its prefill ends at `ts`, at the rate its reports gave: the gateway's first token, or the engine's
        first decode line for a request that does not stream."""
        if s.first_token is None:
            self._accrue(s, ts)
        s.first_token = ts
        s.prompt = s.prompt or prompt_of(s.prefill)
        s.prefill = Track()
        self.progress_pairs.finished(s.id, self.clock())

    def set_decode_drafts(self, profile: str, req: int, proposed: int, accepted: int, tokens: int | None = None,
                          elapsed_s: float | None = None, at: float | None = None) -> None:
        """Attach an engine's decode line: the running turn's cumulative counts (Crucible crucible_decode).

        Like a progress line it carries the engine's request number, which live/pairing.py turns into the stream.
        The counts only grow, so a line that arrives after a newer one changes nothing. A stream still in
        prefill decodes from here on: the engine writes its first line about a second into decode, and a
        streamed request has had its first token at the gateway well before, so this one does not stream.
        """
        mine = [s for s in self.streams.values() if s.profile == profile]
        rid = self.progress_pairs.decoding(profile, req, {s.id for s in mine if s.first_token is not None},
                                           now=self.clock(), waiting={s.id for s in mine if s.first_token is None})
        s = self.streams.get(rid) if rid else None
        # The start order can name a stream whose own number is known: a request sent straight to the engine
        # took a number between them. Its lines are someone else's.
        if s is None or (s.engine_req is not None and s.engine_req != req) \
                or (s.turn_drafts and proposed < s.turn_drafts[1]):
            return
        s.engine_req = req
        at = self.clock() if at is None else at
        if s.first_token is None:
            self._begin_decode(s, max(s.started, at - (elapsed_s or 0.0)))
        s.turn_drafts = (accepted, proposed)
        if tokens is not None and elapsed_s is not None:
            line = _engine_rate(s.engine_decode, tokens, elapsed_s)
            if line is not s.engine_decode:  # a newer line
                s.engine_decode, s.engine_at = line, at
                if s.chart_from_engine:
                    self._chart(s, int(at), tokens)
                else:  # its count includes tokens from before the restart: only what follows is new
                    s.charted, s.chart_from_engine = max(s.charted, tokens), True

    def _first_chunk_rate(self, s: Stream, elapsed: float, at: float) -> float | None:
        """The prompt's rate over its first chunk, for its cached start: the recent rate times the share of
        the engine it had (another prompt prefilling halves it). The rate when the report attaches is not
        it: a prompt that shared the engine for most of its chunk and then had it alone looked twice as fast."""
        hint = self.prefill_rates.get(s.profile)
        if not hint:
            return None
        sharing = max(1, sum(1 for x in self.streams.values() if x.profile == s.profile and x.first_token is None
                             and not (x.was_queued and x.admitted is None)))
        run = elapsed - COMMAND_S
        if s.admitted is None or run <= 0:
            return hint / sharing
        share = s.engine_s + max(0.0, at - max(self._viewed or at, s.admitted)) / sharing
        return hint * min(1.0, share / run) if share > 0 else hint / sharing

    def _progress(self, s: Stream, sharing: int, queued: bool) -> dict | None:
        if s.first_token is not None:
            return None
        return progress_view(s.prefill, hint=self.prefill_rates.get(s.profile),
                             chunk=self.prefill_chunks.get(s.profile), step=s.prefill_step, sharing=sharing,
                             since=s.admitted, queued=queued, alone=s.alone)

    def _admit(self, streams: list[Stream], queued: set[str], sharing: dict[str, int], now: float) -> None:
        """Note when each stream got a slot, whether another prompt prefilled then (a count before the
        engine's first report starts at the slot, and only for a prompt that had the engine to itself),
        and the engine seconds each prefilling prompt had since the last view."""
        dt = max(0.0, now - self._viewed) if self._viewed is not None else 0.0
        self._viewed = max(now, self._viewed or now)
        for s in streams:
            share = 1 / max(1, sharing[s.profile])
            if s.id in queued:
                s.was_queued = True
            elif s.admitted is None:
                s.admitted = now if s.was_queued else s.started
                s.alone = sharing[s.profile] <= 1
                s.engine_s = max(0.0, now - s.admitted) * share
            elif s.first_token is None:
                s.engine_s += dt * share

    def _prefilling_at(self, profile: str, at: float) -> list[tuple[str, float]]:
        """(id, started) of this profile's streams that were waiting for their first token at time `at`."""
        rows = [(s.id, s.started, s.first_token, None) for s in self.streams.values() if s.profile == profile]
        rows += [(i, st, ft, end) for i, p, st, ft, end in self.ended if p == profile]
        return [(i, st) for i, st, ft, end in rows if st <= at and (ft or end or float("inf")) > at]

    # -- views -----------------------------------------------------------------
    def decode_tps(self, now: float | None = None) -> float:
        now = self.clock() if now is None else now
        return sum(s.figures(now, self.ratio(s.profile))[1] for s in self.streams.values())

    def completed_seconds(self, now: float | None = None, slots: int | None = None) -> list[dict]:
        """Seconds that ended since the last call, each exactly once (zeros included). They are final."""
        now = self.clock() if now is None else now
        self.accrue_prefill(now, slots)
        last_complete = int(now - EMIT_LAG_S) - 1
        out = []
        for sec in range(max(self._emitted_through + 1, last_complete - HISTORY_S), last_complete + 1):
            v = self.seconds.get(sec, {"decode": 0, "prefill": 0})
            out.append({"t": sec, "decode": _num(v["decode"]), "prefill": _num(v["prefill"])})
        self._emitted_through = max(self._emitted_through, last_complete)
        cutoff = last_complete - HISTORY_S
        for sec in [k for k in self.seconds if k < cutoff]:
            del self.seconds[sec]
        while self.starts and next(iter(self.starts.values())) < cutoff:
            self.starts.popitem(last=False)
        return out

    def history(self, now: float | None = None) -> dict:
        """The seconds already sent (a browser that connects now sees what the others drew) and request starts."""
        now = self.clock() if now is None else now
        end = self._emitted_through
        ts = list(range(end - HISTORY_S + 1, end + 1))
        return {"t": ts, "decode": [_num(self.seconds.get(t, {}).get("decode", 0)) for t in ts],
                "prefill": [_num(self.seconds.get(t, {}).get("prefill", 0)) for t in ts],
                "starts": [{"id": rid, "t": t} for rid, t in self.starts.items() if t >= ts[0]]}

    def view(self, now: float | None = None, slots: int = 0, profile: str | None = None) -> dict:
        """The live view. With the engine's slots known, streams past them wait in its queue: no estimate.
        engine_exact: whether the active profile's live counts are exact, also while nothing streams."""
        now = self.clock() if now is None else now
        streams = sorted(self.streams.values(), key=lambda s: s.started)
        profiles = {s.profile for s in streams}
        exact = all(self.decode_exact(p) for p in profiles) if profiles else True
        note = None if exact else next(n for p in sorted(profiles) if (n := self.exactness_note(p)))
        queued = self._queued(streams, slots)
        sharing = {p: sum(1 for s in streams if s.profile == p and s.first_token is None and s.id not in queued)
                   for p in profiles}
        self._admit(streams, queued, sharing, now)
        return {"decode_tps": round(self.decode_tps(now), 2), "decode_exact": exact, "decode_note": note,
                "engine_exact": self.decode_exact(profile) if profile else exact,
                "in_flight": len(streams),
                "streams": [s.view(now, self.ratio(s.profile), self._progress(s, sharing[s.profile], s.id in queued),
                                   s.id in queued) for s in streams]}
