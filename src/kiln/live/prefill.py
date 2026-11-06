"""One stream's prefill progress, from the engine's progress reports (ARCHITECTURE.md rule 4).

An engine reports (done, total, elapsed) after each prefill chunk: Crucible every 4,096 computed rows
from where its cache left off, Halogen on fixed 32,768-token boundaries. `done` counts the cached
prefix, so done / elapsed is not a rate: a prompt resumed from 21,743 cached tokens reports 21,751
after 0.06 s (362,000 tok/s), and a browser that used it showed 100% for the whole prefill. The rate
comes from the stream's own recent reports, or from the profile's recent cold prefills until it has
two. The browser moves the figure on from the last report at that rate (web/static/js/progress.js).

Before the first report (a cold prompt's first chunk takes about 2.7 s on a Strix Halo test machine) the view still has
the recent rate, so the browser can count the tokens read so far.
"""
from __future__ import annotations

from dataclasses import dataclass

RECENT_REPORTS = 3  # the rate spans the last two chunks, so it follows a change in load within ~5 s
MIN_SPAN_S = 1.0  # reports closer than this (a head chunk and a short last chunk) give no rate
# The gateway's start to the engine's first computed row: a Strix Halo test machine, Crucible t15, median 0.22 s.
LEAD_S = 0.25
# Each prefill command costs this much before its rows: an 8-row head chunk reports after 0.057-0.197 s
# (a Strix Halo test machine, 108 resumed prompts). Without it the cached start came out 77 tokens low (median);
# with it, 16.
COMMAND_S = 0.05
# An estimated start smaller than this share of the work it was taken from is within the rate's own
# error: a cold prompt, not a small cache.
NOISE = 0.1
# Crucible restores a cached prefix before it computes the rows after it, and the restore grows with the
# prefix: 8 rows after 25k cached report in 0.06 s, after 230k in up to 1.9 s (in a measured run, 524 first
# reports). Without this allowance a long restore looked like half a chunk of work and passed for a full
# chunk, so 4,088 cached tokens were priced as new. 4 to 9 us a token all separate the two in that run; the
# slowest restore was 8.3 us a token.
RESTORE_S_PER_TOKEN = 8e-6
# Work under this share of a chunk is the tail rows. Between it and half a chunk the report could be either,
# so the start is an estimate: with the recent rate half the real one, 3 wrong exact starts of 534, not 11.
TAIL_WORK = 0.4
# The rows Crucible computes after a restored prefix, before its first report: 523 of 539 first reports
# in a measured run (the others: 13 full chunks and one of 26 rows).
TAIL_ROWS = 8


@dataclass(frozen=True)
class Report:
    done: int  # prompt tokens in the cache now, the cached prefix included
    total: int
    elapsed: float  # engine seconds since its prefill began
    at: float  # when the report was logged (journal time)


@dataclass(frozen=True)
class Track:
    """A stream's reports (the first and the recent ones) and its cached start. The start is decided when
    the first report attaches, with the rate in force then: decided again later, at a rate that changed
    because another prompt started or ended, an exact start turned into a wrong estimate."""
    reports: tuple[Report, ...] = ()
    start: int = 0
    start_estimated: bool = True
    start_known: bool = True  # False: the first report came with no rate, so its cached prefix is unknown


def add_report(track: Track, rep: Report, *, rate: float | None, chunk: int | None,
               step: int | None = None) -> Track:
    """The track with `rep`. rate: tok/s for the start when this is the first report (the profile's recent
    rate, shared by the prompts prefilling now); chunk, step: the engine's chunk and fixed boundary."""
    reports = track.reports
    if reports and reports[-1].total != rep.total and len(reports) >= 2:
        # One stream is one prompt, so another size means a pairing went wrong (live/pairing.py). After
        # two reports that agree the new one is the stray; after one, the old one may be.
        return track
    if not reports or reports[-1].total != rep.total:
        start, estimated = _start(rep, rate, chunk, step)
        return Track((rep,), start, estimated, rate is not None)
    if rep.elapsed <= reports[-1].elapsed:
        return track  # a repeated or late line says nothing new
    kept = (*reports, rep)
    kept = kept if len(kept) <= RECENT_REPORTS + 1 else (kept[0], *kept[-RECENT_REPORTS:])
    return Track(kept, track.start, track.start_estimated, track.start_known)


def prompt_of(track: Track) -> dict | None:
    """{"total", "cached"}: the prompt's size and its cached prefix, once a report gave them, else None. The
    live cost prices the prompt from these until the request's record has the exact counts."""
    if not track.reports or not track.start_known:
        return None
    return {"total": track.reports[-1].total, "cached": track.start}


def chunk_of(reports: tuple[Report, ...]) -> int | None:
    """The engine's chunk: the last two steps between reports when they agree (a lost journal line or a
    head chunk gives one odd step) and the last report is not the final, short chunk."""
    if len(reports) < 3 or reports[-1].done >= reports[-1].total:
        return None
    a, b = reports[-2].done - reports[-3].done, reports[-1].done - reports[-2].done
    return b if a == b and b > 0 else None


def report_rate(reports: tuple[Report, ...]) -> float | None:
    """The prompt's own prefill tok/s over its recent reports, or None before two that span MIN_SPAN_S."""
    if len(reports) < 2:
        return None
    a, b = reports[-RECENT_REPORTS:][0], reports[-1]
    span = b.elapsed - a.elapsed
    return (b.done - a.done) / span if span >= MIN_SPAN_S and b.done > a.done else None


def _start(first: Report, rate: float | None, chunk: int | None, step: int | None) -> tuple[int, bool]:
    """(cached tokens when the prefill began, whether that is an estimate)."""
    if rate is None:
        return 0, True
    restore = 0.0 if step else RESTORE_S_PER_TOKEN * max(0, first.done - (chunk or 0))
    work = rate * max(0.0, first.elapsed - COMMAND_S - restore)
    # Crucible's first chunk is a full chunk (cold, or after a block cache) or TAIL_ROWS after a restored
    # prefix (the restore's time aside, well under a second). A first report about one chunk of work in (half
    # to one and a half, so a recent rate 1.5x off still tells them apart) ends a full chunk; one under
    # TAIL_WORK of a chunk is the tail rows. Both starts are exact. A full chunk starts cold or on a block
    # cache's boundary, so it ends on a multiple of the chunk: such a report, or one inside the first chunk
    # (the engine's chunk may be smaller than the one learned), is never taken for a tail. It is an estimate
    # for a first report from later in the prefill (it paired late, or after a reconnect), for a prompt that
    # fits in one chunk, and on Halogen's fixed boundaries.
    if not step and chunk and first.done < first.total:
        if chunk <= first.done and chunk / 2 <= work <= 1.5 * chunk:
            return first.done - chunk, False
        if chunk < first.done and first.done % chunk and work < TAIL_WORK * chunk:
            return first.done - TAIL_ROWS, False
    start = max(0, min(first.done, round(first.done - work)))
    return (0 if start < NOISE * work else start), True


def progress_view(track: Track, *, hint: float | None, chunk: int | None, step: int | None = None,
                  sharing: int = 1, since: float | None = None, queued: bool = False,
                  alone: bool = True) -> dict | None:
    """What the browser needs to show and move a stream's prefill on, or None when there is nothing to show.

    hint: the profile's recent cold prefill tok/s; sharing: prompts of the profile prefilling now (they
    share the engine); since: when the engine could begin (the request's start, or when it left the
    engine's queue); queued: the stream waits for a slot, so the engine is not reading it yet; alone: no
    other prompt of the profile prefilled when it got its slot.
    """
    shared = hint / max(1, sharing) if hint else None
    recent = None if queued else shared
    reports = track.reports
    if not reports:
        # A prompt that got its slot beside another prefill can wait behind that prompt's chunks for its
        # first report: a count would run ahead and then drop to the report's few rows. One that started
        # alone keeps its count when another prompt starts. Without a chunk the count has no cap.
        if recent is None or since is None or not alone or not chunk:
            return None
        return {"done": 0, "total": None, "elapsed_s": None, "at": since + LEAD_S, "start": None,
                "start_estimated": True, "start_known": False, "rate": recent, "rate_from": "recent",
                "next": chunk, "reports": 0}
    last = reports[-1]
    own = report_rate(reports)
    rate, source = (own, "engine") if own else (recent, "recent" if recent else None)
    if step:  # fixed boundaries
        nxt = (last.done // step + 1) * step
    else:
        nxt = last.done + chunk if chunk else last.total
    return {"done": last.done, "total": last.total, "elapsed_s": last.elapsed, "at": last.at,
            "start": track.start, "start_estimated": track.start_estimated, "start_known": track.start_known,
            "rate": rate, "rate_from": source,
            "next": min(nxt, last.total), "reports": len(reports)}
