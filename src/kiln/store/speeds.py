"""Today's decode and prefill speeds and TTFT: the decode panel's "Today" table (web/static/js/day_speed.js).

The average is all tokens over all seconds, so a long reply counts for its length. The range is the 5th to the
95th percentile of the request rates, so one odd request (a decode that ran while big prompts prefilled) does
not set it. Both describe a typical request better than the plain mean and the minimum and maximum. TTFT has no token weight: its
average is the plain mean of the waits.
"""
from __future__ import annotations

MIN_PREFILL_RATE_TOKENS = 1024  # a smaller prefill's rate is mostly fixed overhead: most prompts are cached
PERCENTILES = (0.05, 0.95)


def summary(pairs: list[tuple[float, float]], digits: int) -> dict | None:
    """{"avg", "p5", "p95", "n"} for the (tokens, tok/s) pairs of single requests, or None without one."""
    pairs = [(n, r) for n, r in pairs if n > 0 and r > 0]
    if not pairs:
        return None
    avg = sum(n for n, _ in pairs) / sum(n / r for n, r in pairs)
    rates = sorted(r for _, r in pairs)
    lo, hi = (_quantile(rates, p) for p in PERCENTILES)
    return {"avg": round(avg, digits), "p5": round(lo, digits), "p95": round(hi, digits), "n": len(pairs)}


def waits(values: list[float | None], digits: int) -> dict | None:
    """{"avg", "p5", "p95", "n"} for the TTFT seconds of single requests, or None without one."""
    xs = sorted(v for v in values if v is not None and v > 0)
    if not xs:
        return None
    lo, hi = (_quantile(xs, p) for p in PERCENTILES)
    return {"avg": round(sum(xs) / len(xs), digits), "p5": round(lo, digits), "p95": round(hi, digits), "n": len(xs)}


def _quantile(xs: list[float], p: float) -> float:
    """Linear between the closest ranks of sorted values (numpy's default)."""
    i = p * (len(xs) - 1)
    lo = int(i)
    hi = min(lo + 1, len(xs) - 1)
    return xs[lo] + (xs[hi] - xs[lo]) * (i - lo)
