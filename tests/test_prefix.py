"""Prefix tracking: content-free, memory-only comparison of a client's consecutive requests."""
import json

import pytest

from tests.test_gateway import setup  # noqa: F401 (fixture)

from kiln.gateway.prefix import PrefixTracker, cache_math

SYS = {"role": "system", "content": "You are a coding agent."}
TOOLS = [{"type": "function", "function": {"name": "read", "parameters": {"type": "object"}}}]


def chat(*texts, system=SYS, tools=TOOLS):
    msgs = [system] if system else []
    for i, t in enumerate(texts):
        msgs.append({"role": "user" if i % 2 == 0 else "assistant", "content": t})
    return {"messages": msgs, "tools": tools}


def test_a_kept_prefix_continues_the_conversation():
    tr = PrefixTracker()
    t1 = tr.start("pi", chat("hi"))
    assert t1.fields["new_conversation"] and t1.fields["prefix_msgs_kept"] == 0
    tr.finish(t1, {"prompt": 1000, "completion": 50})
    t2 = tr.start("pi", chat("hi", "hello", "read a.py"))
    f = t2.fields
    assert (f["conv"], f["prefix_msgs_kept"], f["prev_n_messages"], f["n_messages"]) == (t1.fields["conv"], 2, 2, 4)
    assert (f["system_changed"], f["tools_changed"], f["prev_prompt"], f["prev_completion"]) == (False, False, 1000, 50)
    assert not f["new_conversation"]


def test_a_rewritten_prefix_is_reported_and_kept_apart():
    tr = PrefixTracker()
    tr.finish(tr.start("pi", chat("a", "b", "c")), {"prompt": 900, "completion": 10})
    t = tr.start("pi", chat("a", "SUMMARY", "c", "d"))  # compaction rewrote message 2
    assert (t.fields["prefix_msgs_kept"], t.fields["prev_n_messages"]) == (2, 4)
    # The old line is still remembered: a retry of the original continues it.
    t3 = tr.start("pi", chat("a", "b", "c", "x", "y"))
    assert (t3.fields["prefix_msgs_kept"], t3.fields["prev_n_messages"]) == (4, 4)


def test_system_and_tools_changes():
    tr = PrefixTracker()
    tr.finish(tr.start("pi", chat("a")), {"prompt": 10, "completion": 1})
    t = tr.start("pi", chat("a", "b", "c", tools=[*TOOLS, {"type": "function", "function": {"name": "x"}}]))
    assert t.fields["tools_changed"] is True and t.fields["system_changed"] is False


def test_same_setup_new_task_is_a_new_conversation():
    tr = PrefixTracker()
    first = tr.start("pi", chat("task one", "ok", "more"))
    t = tr.start("pi", chat("task two"))  # shares only the system message
    assert t.fields["new_conversation"] and t.fields["conv"] != first.fields["conv"]


def test_clients_and_parallel_sessions_are_separate():
    tr = PrefixTracker()
    a = tr.start("pi", chat("alpha"))
    b = tr.start("pi", chat("beta"))
    c = tr.start("hermes", chat("alpha"))
    assert len({a.fields["conv"], b.fields["conv"], c.fields["conv"]}) == 3
    assert tr.start("pi", chat("beta", "ok", "go")).fields["conv"] == b.fields["conv"]


def test_nothing_derived_from_content_leaves_the_tracker():
    tr = PrefixTracker()
    t = tr.start("pi", chat("my secret prompt"))
    tr.finish(t, {"prompt": 5, "completion": 1})
    out = json.dumps(tr.start("pi", chat("my secret prompt", "x", "y")).fields)
    assert "secret" not in out
    # Only counts, booleans and a random id; the id is not a function of the content.
    other = PrefixTracker().start("pi", chat("my secret prompt")).fields["conv"]
    assert other != t.fields["conv"]
    assert set(json.loads(out)) == {"n_messages", "prefix_msgs_kept", "prev_n_messages", "system_changed",
                                    "tools_changed", "prev_prompt", "prev_completion", "new_conversation", "conv"}


def test_memory_is_bounded_and_forgets():
    t = [0.0]
    tr = PrefixTracker(clock=lambda: t[0])
    for i in range(100):
        tr.start(f"c{i}", chat(f"m{i}"))
    assert len(tr.clients) <= 64
    for i in range(40):
        tr.start("one", chat(f"task {i}"))
    assert len(tr.clients["one"]) <= 16
    t[0] = 7 * 3600
    tr.start("late", chat("x"))
    assert set(tr.clients) == {"late"}


def test_requests_without_messages_are_not_tracked():
    assert PrefixTracker().start("pi", {"prompt": "plain completion"}) is None


def test_cache_math():
    # Kept prefix: 3,000 new tokens since the last prompt, 60,000 from cache, so 7,000 were read again.
    assert cache_math(70_000, 60_000, 4, 4, 67_000, 1_000) == (3_000, 7_000)
    assert cache_math(70_000, 67_000, 4, 4, 67_000, 1_000) == (3_000, 0)  # a full hit
    assert cache_math(70_000, 0, 2, 4, 67_000, 1_000) == (None, None)  # client changed the prefix
    assert cache_math(70_000, 0, 4, 4, None, None) == (None, None)  # previous turn still running
    assert cache_math(500, 0, 4, 4, 600, 50) == (0, 500)  # client dropped content: nothing appended


async def test_gateway_records_prefix_fields_without_content(setup):
    from tests.test_gateway import records
    gw, client, engine, cfg = await setup()
    body = {"model": "qwen3.8-flash-next", "stream": True, **chat("private words")}
    await (await client.post("/v1/chat/completions", json=body)).read()
    body2 = {**body, "messages": [*body["messages"], {"role": "assistant", "content": "zqx"},
                                  {"role": "user", "content": "more private words"}]}
    await (await client.post("/v1/chat/completions", json=body2)).read()
    r1, r2 = records(cfg)
    assert r1["prefix"]["new_conversation"] and r2["prefix"]["conv"] == r1["prefix"]["conv"]
    assert (r2["prefix"]["prefix_msgs_kept"], r2["prefix"]["prev_prompt"], r2["prefix"]["prev_completion"]) == (2, 12, 3)
    raw = "\n".join(p.read_text() for p in cfg.gateway_data.glob("*.jsonl"))
    # zqx has no hex letters: a digest in the record can never spell it (abc could, now and then).
    assert "private" not in raw and "zqx" not in raw


def test_lone_surrogate_does_not_fail_hashing():
    # JS clients (Pi) emit a lone surrogate when they cut a string inside an emoji.
    tr = PrefixTracker()
    t = tr.start("pi", chat("broken emoji \ud800 here"))
    assert t is not None and t.fields["new_conversation"]


async def test_tracker_failure_drops_diagnostics_only(setup, capsys):
    from tests.test_gateway import records
    gw, client, engine, cfg = await setup()

    def boom(*a, **k):
        raise ValueError("secret message text")
    gw.prefix.start = boom
    resp = await client.post("/v1/chat/completions", json={"model": "qwen3.8-flash-next", **chat("x")})
    assert resp.status == 200
    (rec,) = records(cfg)
    assert "prefix" not in rec
    out = capsys.readouterr()
    assert "secret" not in out.out + out.err and "prefix tracking failed" in out.out


def test_cache_math_soak_rule():
    # Dropped reasoning: the previous completion is not replayed, so it must not count as appended.
    assert cache_math(53_500, 49_152, 3, 3, 50_000, 20_500) == (3_500, 848)  # not an engine miss
    # Oct 5 incident: 40,974 prefilled, 19,501 appended -> 21,473 re-read, flagged.
    prompt, cached = 94_222, 94_222 - 40_974
    assert cache_math(prompt, cached, 7, 7, prompt - 19_501, 900) == (19_501, 21_473)


async def test_large_bodies_hash_in_a_worker_thread(setup, monkeypatch):
    from kiln.gateway import app as gw_app
    from tests.test_gateway import records
    monkeypatch.setattr(gw_app, "PREFIX_THREAD_BYTES", 10)
    gw, client, engine, cfg = await setup()
    await (await client.post("/v1/chat/completions", json={"model": "qwen3.8-flash-next", **chat("x")})).read()
    (rec,) = records(cfg)
    assert rec["prefix"]["new_conversation"] is True


@pytest.mark.parametrize("siphash", [True, False])
def test_digests_from_client_bytes_equal_the_plain_digests(siphash):
    from kiln.gateway.body import parse
    body = {"model": "m", **chat("a", "b é", "c \ud800")}
    raw = json.dumps(body).encode()  # the lone surrogate travels escaped, as JS clients send it
    parsed, layout = parse(raw)
    tr = PrefixTracker()
    tr._siphash = siphash
    plain = tr.digests(parsed)
    assert tr.digests(parsed, layout.message_bytes(), layout.value_bytes("tools")) == plain  # cache miss
    assert tr.digests(parsed, layout.message_bytes(), layout.value_bytes("tools")) == plain  # cache hit
    other = json.dumps({**body, "messages": [*body["messages"][:-1], {"role": "user", "content": "d"}]}).encode()
    parsed2, layout2 = parse(other)
    d2 = tr.digests(parsed2, layout2.message_bytes(), layout2.value_bytes("tools"))
    assert d2[0][:-1] == plain[0][:-1] and d2[0][-1] != plain[0][-1] and d2 == tr.digests(parsed2)


def test_same_message_in_other_bytes_has_the_same_digest():
    from kiln.gateway.body import parse
    tr = PrefixTracker()
    a = parse(b'{"messages": [{"role": "user", "content": "x"}]}')
    b = parse(b'{"messages":[{"content":"x","role":"user"}]}')
    assert tr.digests(a[0], a[1].message_bytes()) == tr.digests(b[0], b[1].message_bytes())


def test_the_digest_cache_is_bounded(monkeypatch):
    from kiln.gateway import prefix
    monkeypatch.setattr(prefix, "MAX_CACHED", 50)
    tr = PrefixTracker()
    for i in range(20):
        body = chat(*[f"m{i} {j}" for j in range(10)])
        tr.digests(body, [json.dumps(m).encode() for m in body["messages"]])
    assert len(tr._cached) <= 50


def test_common_prefix_length():
    from kiln.gateway.prefix import _common_prefix
    a = [bytes([i]) for i in range(40)]
    for n in range(41):
        b = a[:n] + [b"x"] * 5
        assert _common_prefix(a, b) == n and _common_prefix(b, a) == n
    assert _common_prefix(a, a[:7]) == 7 and _common_prefix([], a) == 0


def test_the_client_bytes_digest_cache_forgets_an_entry_after_6_hours():
    """The README says message hashes are kept in memory for at most 6 hours (release review L)."""
    from kiln.gateway.body import parse
    from kiln.gateway.prefix import FORGET_AFTER_S, PrefixTracker
    now = [0.0]
    t = PrefixTracker(clock=lambda: now[0])
    body, lay = parse(b'{"messages":[{"role":"user","content":"a private prompt"}]}')
    t.start("192.0.2.5", body, t.digests(body, lay.message_bytes()))
    assert len(t._cached) == 1
    now[0] = FORGET_AFTER_S + 1  # another client sends something else
    b2, l2 = parse(b'{"messages":[{"role":"user","content":"other"}]}')
    t.start("198.51.100.7", b2, t.digests(b2, l2.message_bytes()))
    assert len(t._cached) == 1
    now[0] = 2 * FORGET_AFTER_S + 2  # the same bytes again: an expired entry is made again, not reused
    first = t.digests(b2, l2.message_bytes())
    assert len(t._cached) == 1 and first == t.digests(b2, l2.message_bytes())
