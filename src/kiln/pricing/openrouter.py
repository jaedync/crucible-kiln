"""OpenRouter list prices, matched to local models by Hugging Face repo id.

Only the model-level list price counts: variants such as ':free', ':batch' or
':nitro' and '~' aliases are skipped, and any discount field is ignored.
"""
from __future__ import annotations

import aiohttp

MODELS_URL = "https://openrouter.ai/api/v1/models"
TIMEOUT = aiohttp.ClientTimeout(total=60)


def _per_m(v) -> float | None:
    try:
        return round(float(v) * 1e6, 6)
    except (TypeError, ValueError):
        return None


def select(models: list[dict], hf_id: str) -> dict | None:
    """The standard OpenRouter entry for a Hugging Face repo id, or None."""
    want = hf_id.lower()
    cands = [m for m in models
             if (m.get("hugging_face_id") or "").lower() == want
             and ":" not in m.get("id", "") and not m.get("id", "").startswith("~")]
    cands.sort(key=lambda m: m.get("created") or 0, reverse=True)
    return cands[0] if cands else None


def prices(entry: dict) -> tuple[float, float, float] | None:
    p = entry.get("pricing") or {}
    inp, out = _per_m(p.get("prompt")), _per_m(p.get("completion"))
    if inp is None or out is None:
        return None
    cached = _per_m(p.get("input_cache_read"))
    return inp, (cached if cached is not None else inp), out


async def fetch_models(session: aiohttp.ClientSession | None = None) -> list[dict]:
    own = session is None
    session = session or aiohttp.ClientSession(timeout=TIMEOUT)
    try:
        async with session.get(MODELS_URL) as r:
            r.raise_for_status()
            return (await r.json()).get("data", [])
    finally:
        if own:
            await session.close()


def refresh(book, models: list[dict], profiles, now: float) -> list[str]:
    """Add cards for every profile model whose OpenRouter price changed. Returns log lines."""
    log, seen = [], set()
    for p in profiles:
        if not p.hf_id or p.model in seen:
            continue
        seen.add(p.model)
        entry = select(models, p.hf_id)
        if entry is None:
            log.append(f"openrouter: no standard listing for {p.hf_id}")
            continue
        pr = prices(entry)
        if pr is None:
            log.append(f"openrouter: {entry['id']} has no usable price")
            continue
        card, created = book.add("openrouter", p.model, entry["id"], *pr, now,
                                 raw={"id": entry["id"], "pricing": entry.get("pricing")})
        if created:
            log.append(f"openrouter: {p.model} -> {entry['id']} {pr[0]}/{pr[1]}/{pr[2]} per M (card {card.id})")
    return log
