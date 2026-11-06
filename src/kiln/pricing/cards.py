"""Effective-dated price cards and sticky per-request costs (ARCHITECTURE.md rule 7).

A card applies from its effective_from until the next card for the same
source and model. A request is priced once, with the card in force at its
start, and the card id is stored with the cost. If its token counts are
refined later (an engine log line arrives), it is re-priced with the same card;
a newer card never touches it.
"""
from __future__ import annotations

import json
import sqlite3
from dataclasses import dataclass

REFERENCE_MODEL = "*"  # the reference card prices every model the same way
CARD_AFTER_REQUEST = "card_after_request"  # no card existed yet when the request ran


@dataclass(frozen=True)
class Card:
    id: int
    source: str
    model: str
    provider_model: str | None
    input_per_m: float
    cached_per_m: float
    output_per_m: float
    effective_from: float
    fetched_at: float

    def prices(self) -> tuple[float, float, float]:
        return (self.input_per_m, self.cached_per_m, self.output_per_m)

    def view(self) -> dict:
        return {k: getattr(self, k) for k in self.__dataclass_fields__}


def cost_usd(card: Card, prompt: int | None, cached: int | None, completion: int | None) -> float | None:
    if prompt is None or completion is None:
        return None
    cached = min(max(cached or 0, 0), prompt)
    return ((prompt - cached) * card.input_per_m + cached * card.cached_per_m
            + completion * card.output_per_m) / 1e6


def _card(row) -> Card | None:
    return Card(*row) if row else None


_COLS = "id, source, model, provider_model, input_per_m, cached_per_m, output_per_m, effective_from, fetched_at"


class PriceBook:
    def __init__(self, conn: sqlite3.Connection, sources: tuple[str, ...] = ("reference", "openrouter")):
        self.conn, self.sources = conn, sources

    def latest(self, source: str, model: str) -> Card | None:
        return _card(self.conn.execute(
            f"SELECT {_COLS} FROM price_cards WHERE source=? AND model=? ORDER BY effective_from DESC, id DESC LIMIT 1",
            (source, model)).fetchone())

    def add(self, source: str, model: str, provider_model: str | None, input_per_m: float,
            cached_per_m: float, output_per_m: float, now: float, raw: dict | None = None) -> tuple[Card, bool]:
        """Insert a card if the prices differ from the latest one. Returns (card, created)."""
        latest = self.latest(source, model)
        new = (round(input_per_m, 6), round(cached_per_m, 6), round(output_per_m, 6))
        if latest and tuple(round(p, 6) for p in latest.prices()) == new and latest.provider_model == provider_model:
            return latest, False
        cur = self.conn.execute(
            "INSERT INTO price_cards (source, model, provider_model, input_per_m, cached_per_m, output_per_m,"
            " effective_from, fetched_at, raw) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (source, model, provider_model, *new, now, now, json.dumps(raw) if raw else None))
        card = self.get(cur.lastrowid)
        self.price_unpriced(source, model)
        return card, True

    def backdate(self, source: str, model: str, provider_model: str | None, input_per_m: float,
                 cached_per_m: float, output_per_m: float, effective_from: float, now: float,
                 raw: dict | None = None) -> tuple[Card | None, list[str]]:
        """Insert a card that took effect in the past and re-price the requests it covers.

        The one exception to sticky costs (ARCHITECTURE.md rule 7), for a price change that Kiln
        learned of late. Requests that started at or after effective_from and were priced with an
        older card of this source move to the new card; requests under a later card, and requests
        without a cost row, keep what they have.
        Returns (card, re-priced request ids); the card is None when the prices in force then match."""
        if effective_from > now:
            raise ValueError("a backdated card can't take effect in the future")
        key = REFERENCE_MODEL if source == "reference" else model
        later = self.conn.execute("SELECT id, effective_from FROM price_cards WHERE source = ? AND model = ?"
                                  " AND effective_from >= ? ORDER BY effective_from", (source, key, effective_from)).fetchall()
        if later:
            # A card at or after that time would keep its requests and stay the latest card.
            raise ValueError("cards already take effect at or after that time: "
                             + ", ".join(f"#{r[0]} from {r[1]:.0f}" for r in later))
        if self.conn.in_transaction:
            raise RuntimeError("backdate runs its own transaction")
        new = (round(input_per_m, 6), round(cached_per_m, 6), round(output_per_m, 6))
        in_force, _ = self.card_for(source, key, effective_from)
        if in_force and in_force.effective_from <= effective_from and in_force.prices() == new:
            return None, []
        where, params = ("", [source, effective_from, effective_from]) if source == "reference" \
            else ("AND r.model = ?", [source, effective_from, effective_from, model])
        self.conn.execute("BEGIN")
        try:
            cur = self.conn.execute(
                "INSERT INTO price_cards (source, model, provider_model, input_per_m, cached_per_m, output_per_m,"
                " effective_from, fetched_at, raw) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (source, key, provider_model, *new, effective_from, now, json.dumps(raw) if raw else None))
            card = self.get(cur.lastrowid)
            # Only requests already priced with an older card: unpriced ones stay as they were.
            ids = [r[0] for r in self.conn.execute(
                "SELECT r.id FROM requests r JOIN request_costs c ON c.request_id = r.id AND c.source = ?"
                " JOIN price_cards p ON p.id = c.card_id"
                f" WHERE r.ts_start >= ? AND p.effective_from < ? {where}", params).fetchall()]
            for rid in ids:
                self.conn.execute("DELETE FROM request_costs WHERE request_id = ? AND source = ?", (rid, source))
                self.price_request(rid, (source,))
            self.conn.execute("COMMIT")
        except Exception:
            self.conn.execute("ROLLBACK")
            raise
        return card, ids

    def get(self, card_id: int) -> Card | None:
        return _card(self.conn.execute(f"SELECT {_COLS} FROM price_cards WHERE id=?", (card_id,)).fetchone())

    def card_for(self, source: str, model: str, ts: float) -> tuple[Card | None, str | None]:
        key = REFERENCE_MODEL if source == "reference" else model
        row = self.conn.execute(
            f"SELECT {_COLS} FROM price_cards WHERE source=? AND model=? AND effective_from<=?"
            " ORDER BY effective_from DESC, id DESC LIMIT 1", (source, key, ts)).fetchone()
        if row:
            return Card(*row), None
        row = self.conn.execute(
            f"SELECT {_COLS} FROM price_cards WHERE source=? AND model=? ORDER BY effective_from, id LIMIT 1",
            (source, key)).fetchone()
        return (Card(*row), CARD_AFTER_REQUEST) if row else (None, None)

    def price_request(self, request_id: str, sources: tuple[str, ...] | None = None) -> dict:
        """(Re)compute a request's costs. Existing rows keep their card, so a newer price never
        reaches an older request; refined token counts are priced with the same card."""
        req = self.conn.execute("SELECT model, ts_start, prompt, cached, completion FROM requests WHERE id=?",
                                (request_id,)).fetchone()
        if req is None:
            return {}
        out = {}
        for source in sources or self.sources:
            existing = self.conn.execute("SELECT card_id, flags FROM request_costs WHERE request_id=? AND source=?",
                                         (request_id, source)).fetchone()
            if existing and existing["card_id"] is not None:
                card, flags = self.get(existing["card_id"]), existing["flags"]
            else:
                card, flags = self.card_for(source, req["model"], req["ts_start"])
            usd = cost_usd(card, req["prompt"], req["cached"], req["completion"]) if card else None
            self.conn.execute(
                "INSERT INTO request_costs (request_id, source, card_id, usd, flags) VALUES (?, ?, ?, ?, ?)"
                " ON CONFLICT (request_id, source) DO UPDATE SET card_id=excluded.card_id, usd=excluded.usd,"
                " flags=excluded.flags", (request_id, source, card.id if card else None, usd, flags))
            out[source] = usd
        return out

    def price_unpriced(self, source: str, model: str) -> int:
        """Price requests that had no card for this source when they arrived."""
        where = "" if source == "reference" else "AND r.model = ?"
        args = (source,) if source == "reference" else (source, model)
        ids = [r[0] for r in self.conn.execute(
            "SELECT r.id FROM requests r LEFT JOIN request_costs c ON c.request_id = r.id AND c.source = ?"
            f" WHERE (c.card_id IS NULL) {where}", args)]
        for rid in ids:
            self.price_request(rid, (source,))  # only the source that just got a card
        return len(ids)

    def current(self) -> list[Card]:
        rows = self.conn.execute(
            f"SELECT {_COLS} FROM price_cards p WHERE effective_from = (SELECT MAX(effective_from) FROM price_cards q"
            " WHERE q.source = p.source AND q.model = p.model) ORDER BY source, model").fetchall()
        return [Card(*r) for r in rows]

    def history(self) -> list[Card]:
        return [Card(*r) for r in self.conn.execute(
            f"SELECT {_COLS} FROM price_cards ORDER BY effective_from DESC, id DESC").fetchall()]
