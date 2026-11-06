"""Spread a token count over the epoch seconds of a time interval."""
from __future__ import annotations


def apportion(start: float | None, end: float | None, tokens: int | None) -> list[tuple[int, int]]:
    """[(second, tokens)] in proportion to each second's overlap with [start, end].

    Integer shares that sum exactly to tokens; seconds that get 0 are left out.
    A zero-length interval puts everything in its one second.
    """
    if not tokens or tokens <= 0 or start is None or end is None or end < start:
        return []
    if end == start:
        return [(int(start), tokens)]
    shares = []
    for sec in range(int(start), int(end) + 1):
        overlap = min(end, sec + 1) - max(start, sec)
        if overlap > 0:
            shares.append((sec, overlap))
    span = sum(o for _, o in shares)
    # Round the running total, not each share: per-share rounding errors add up, and the last
    # second used to absorb all of them (hundreds of tokens over a long request).
    out, assigned, covered = [], 0, 0.0
    for i, (sec, overlap) in enumerate(shares):
        covered += overlap
        target = tokens if i == len(shares) - 1 else round(tokens * covered / span)
        n = max(0, min(target, tokens) - assigned)
        assigned += n
        if n:
            out.append((sec, n))
    return out
