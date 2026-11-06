import pytest

from kiln.pricing import openrouter
from kiln.pricing.cards import CARD_AFTER_REQUEST, PriceBook, cost_usd
from kiln.store.db import connect


@pytest.fixture
def conn():
    return connect(":memory:")


def add_request(conn, rid, ts, prompt=1_000_000, cached=0, completion=100_000, model="qwen3.8-flash-next"):
    conn.execute("INSERT INTO requests (id, source, host, ts_start, model, prompt, cached, completion)"
                 " VALUES (?, 'gateway', 'strix', ?, ?, ?, ?, ?)", (rid, ts, model, prompt, cached, completion))


def test_cost_formula_prices_cached_tokens_at_the_cache_rate(conn):
    book = PriceBook(conn)
    card, _ = book.add("reference", "*", "anthropic/claude-sonnet-5.5", 2.0, 0.2, 10.0, now=0)
    assert cost_usd(card, 1_000_000, 900_000, 100_000) == pytest.approx(0.2 + 0.18 + 1.0)
    assert cost_usd(card, 10, 50, 0) == pytest.approx(10 * 0.2 / 1e6)  # cached can't exceed prompt
    assert cost_usd(card, None, 0, 5) is None


def test_costs_are_sticky_across_price_changes(conn):
    book = PriceBook(conn)
    book.add("openrouter", "qwen3.8-flash-next", "qwen/qwen3.8-flash", 0.15, 0.016, 0.47, now=100)
    add_request(conn, "old", ts=150)
    assert book.price_request("old")["openrouter"] == pytest.approx(0.15 + 0.047)
    book.add("openrouter", "qwen3.8-flash-next", "qwen/qwen3.8-flash", 0.30, 0.03, 0.94, now=200)  # price doubles
    add_request(conn, "new", ts=250)
    assert book.price_request("new")["openrouter"] == pytest.approx(0.30 + 0.094)
    # Re-pricing the old request (say, refined token counts) keeps its original card.
    assert book.price_request("old")["openrouter"] == pytest.approx(0.15 + 0.047)


def test_same_prices_do_not_create_a_new_card(conn):
    book = PriceBook(conn)
    a, created_a = book.add("openrouter", "m", "x/m", 1, 0.1, 2, now=1)
    b, created_b = book.add("openrouter", "m", "x/m", 1.0000001, 0.1, 2, now=2)
    assert created_a and not created_b and a.id == b.id


def test_requests_before_any_card_are_priced_and_flagged_when_one_appears(conn):
    book = PriceBook(conn)
    add_request(conn, "early", ts=10)
    assert book.price_request("early") == {"reference": None, "openrouter": None}
    book.add("reference", "*", "anthropic/claude-sonnet-5.5", 2.0, 0.2, 10.0, now=50)
    row = conn.execute("SELECT usd, flags FROM request_costs WHERE request_id='early' AND source='reference'").fetchone()
    assert row["usd"] == pytest.approx(2.0 + 1.0) and row["flags"] == CARD_AFTER_REQUEST


def test_backdated_card_reprices_only_requests_from_its_start(conn):
    book = PriceBook(conn)
    book.add("reference", "*", "anthropic/claude-sonnet-5.5", 2.0, 0.2, 10.0, now=100)
    for rid, ts in (("before", 150), ("at", 300), ("after", 450)):
        add_request(conn, rid, ts=ts, prompt=1_000_000, cached=1_000_000, completion=0)
        book.price_request(rid)
    card, ids = book.backdate("reference", "*", "anthropic/claude-sonnet-5.5", 2.0, 0.1, 10.0,
                              effective_from=300, now=500)
    assert card.effective_from == 300 and card.fetched_at == 500 and sorted(ids) == ["after", "at"]
    usd = {r[0]: r[1] for r in conn.execute("SELECT request_id, usd FROM request_costs WHERE source='reference'")}
    assert usd == {"before": pytest.approx(0.2), "at": pytest.approx(0.1), "after": pytest.approx(0.1)}
    # The startup sync sees the config's prices in force already and adds nothing.
    assert book.add("reference", "*", "anthropic/claude-sonnet-5.5", 2.0, 0.1, 10.0, now=600)[1] is False
    # The OpenRouter costs were not touched.
    assert conn.execute("SELECT COUNT(*) FROM request_costs WHERE source='openrouter' AND card_id IS NOT NULL").fetchone()[0] == 0


def test_backdated_card_refuses_when_a_card_starts_at_or_after_it(conn):
    book = PriceBook(conn)
    book.add("openrouter", "m", "x/m", 1.0, 0.1, 2.0, now=100)
    book.add("openrouter", "m", "x/m", 3.0, 0.3, 6.0, now=400)
    add_request(conn, "late", ts=450, model="m")
    book.price_request("late")
    for eff in (200, 400):
        with pytest.raises(ValueError, match="#2"):
            book.backdate("openrouter", "m", "x/m", 2.0, 0.2, 4.0, effective_from=eff, now=500)
    assert len(book.history()) == 2
    conn.execute("BEGIN")
    with pytest.raises(RuntimeError):
        book.backdate("openrouter", "m", "x/m", 2.0, 0.2, 4.0, effective_from=450, now=500)
    conn.execute("ROLLBACK")


def test_backdated_card_refuses_the_future_and_skips_unchanged_prices(conn):
    book = PriceBook(conn)
    book.add("reference", "*", "p", 2.0, 0.2, 10.0, now=100)
    with pytest.raises(ValueError):
        book.backdate("reference", "*", "p", 2.0, 0.1, 10.0, effective_from=700, now=500)
    assert book.backdate("reference", "*", "p", 2.0, 0.2, 10.0, effective_from=300, now=500) == (None, [])
    assert len(book.history()) == 1


MODELS = [
    {"id": "qwen/qwen3.8-flash", "hugging_face_id": "Qwen/Qwen3.8-Flash-Next", "created": 2,
     "pricing": {"prompt": "0.00000015", "completion": "0.00000047", "input_cache_read": "0.000000016"}},
    {"id": "qwen/qwen3.8-flash:free", "hugging_face_id": "Qwen/Qwen3.8-Flash-Next", "created": 3,
     "pricing": {"prompt": "0", "completion": "0"}},
    {"id": "~qwen/qwen3.8-flash-latest", "hugging_face_id": "Qwen/Qwen3.8-Flash-Next", "created": 4,
     "pricing": {"prompt": "0.0000001", "completion": "0.0000001"}},
    {"id": "qwen/qwen3.8-27b", "hugging_face_id": "Qwen/Qwen3.8-27B", "created": 1,
     "pricing": {"prompt": "0.00000042", "completion": "0.000003"}},
]


def test_openrouter_selects_the_standard_listing_by_hf_id():
    assert openrouter.select(MODELS, "qwen/qwen3.8-flash-next")["id"] == "qwen/qwen3.8-flash"
    assert openrouter.prices(MODELS[0]) == (0.15, 0.016, 0.47)
    assert openrouter.prices(MODELS[3]) == (0.42, 0.42, 3.0)  # no cache price listed: input price
    assert openrouter.select(MODELS, "nobody/none") is None


def test_openrouter_refresh_adds_cards_only_on_change(conn, make_cfg):
    cfg = make_cfg()
    book = PriceBook(conn)
    log1 = openrouter.refresh(book, MODELS, cfg.profiles.values(), now=1)
    log2 = openrouter.refresh(book, MODELS, cfg.profiles.values(), now=2)
    assert len(log1) == 2 and log2 == []
    assert {c.model for c in book.current()} == {"qwen3.8-flash-next", "qwen3.8-27b"}


def test_backdate_reference_cli_applies_the_config_prices_from_a_past_time(make_cfg, monkeypatch, capsys):
    from kiln import cli
    old = make_cfg()
    book = PriceBook(connect(old.db_path))
    book.add("reference", "*", "anthropic/claude-sonnet-5.5", 2.0, 0.2, 10.0, now=1_000)
    add_request(book.conn, "before", ts=1_791_390_000, cached=1_000_000, completion=0)
    add_request(book.conn, "after", ts=1_791_400_000, cached=1_000_000, completion=0)
    for rid in ("before", "after"):
        book.price_request(rid)
    cfg = make_cfg(**{"pricing.reference.cached_per_m": 0.1})
    monkeypatch.setattr(cli.config, "load", lambda _path: cfg)
    monkeypatch.setattr(cli, "_dash_answers", lambda _cfg: True)
    assert cli.main(["backdate-reference", "--since", "2026-10-07T17:00+00:00"]) == 1  # dash still running
    assert cli.main(["backdate-reference", "--since", "2026-10-07T17:00"]) == 2  # no zone offset
    monkeypatch.setattr(cli, "_dash_answers", lambda _cfg: False)
    assert cli.main(["backdate-reference", "--since", "2026-10-07T17:00+00:00"]) == 0
    assert "re-priced 1 requests" in capsys.readouterr().out
    usd = {r[0]: r[1] for r in book.conn.execute("SELECT request_id, usd FROM request_costs WHERE source='reference'")}
    assert usd == {"before": pytest.approx(0.2), "after": pytest.approx(0.1)}
    # Run again: the new card now starts at that time, so it is refused instead of doubled.
    assert cli.main(["backdate-reference", "--since", "2026-10-07T17:00+00:00"]) == 2
    assert "already take effect" in capsys.readouterr().err
    assert cli.main(["backdate-reference", "--since", "2026-10-07T18:00+00:00"]) == 0  # a later time, same prices
    assert "unchanged" in capsys.readouterr().out
