"""The request body: one parse that keeps the client's bytes, and a rewrite that reuses them."""
import json
import random

import pytest

from kiln.gateway.body import encode, parse
from kiln.gateway.compat import crucible

SAMPLES = [
    b'{"model": "m", "stream": true, "messages": [{"role": "user", "content": "hi"}]}',
    b'  {"a":1,"b":[1,2,{"c":null}],"d":"x"}  \n',
    b'{}', b'{ }', b'[]', b'"text"', b'42', b'null', b'', b'   ',
    b'{"a": 1,}', b'{"a" 1}', b'{"a": 1} x', b'{"a": [1,]}', b'{a: 1}', b'{"a": 1',
    b'{"messages": []}', b'{"messages": [ ]}', b'{"messages": {"x": 1}}', b'{"messages": [1, 2,]}',
    b'{"a": 1, "a": 2}', b'{"messages": [1], "messages": "x"}', b'{"messages": "x", "messages": [3]}',
    b'{"x": NaN, "y": -Infinity, "z": 1e400, "w": -0.0, "v": 0.1000000000000000055511151231257827}',
    '{"emoji": "\U0001F600 caf\u00e9", "messages": [{"content": "\u00fcber"}]}'.encode(),
    b'{"lone": "\\ud800 cut", "messages": [{"content": "\\ud83d"}]}',
    b'\xef\xbb\xbf{"bom": 1}', '{"utf16": 1}'.encode("utf-16"), '{"utf16": 1}'.encode("utf-16-le"),
    b'{"bad": "\xff\xfe"}', b'{"ctrl": "a\x01b"}', b'{"deep": ' + b'[' * 50 + b']' * 50 + b'}',
    b'{"a":1}\n\r\t ',
]


def expected(raw: bytes):
    try:
        v = json.loads(raw)
    except ValueError:
        return None
    return v if isinstance(v, dict) else None


def same(a, b) -> bool:
    """Equal as JSON values, NaN included."""
    return json.dumps(a, sort_keys=True) == json.dumps(b, sort_keys=True)


@pytest.mark.parametrize("raw", SAMPLES)
def test_parse_gives_what_json_loads_gives(raw):
    body, layout = parse(raw)
    assert same(body, expected(raw))
    if layout is not None:
        assert layout.body is body


def test_parse_agrees_with_json_loads_on_mutated_bodies():
    rng = random.Random(7)
    base = json.dumps({"model": "m", "stream": True, "tools": [{"t": [1, 2.5, None, False]}],
                       "messages": [{"role": "user", "content": "a \"q\" \\ b"}, {"role": "assistant", "x": {}}]})
    alphabet = '{}[]:,"\\ 0123456789.eE+-truefalsnul\n'
    for _ in range(3000):
        s = list(base)
        for _ in range(rng.randint(1, 3)):
            pos = rng.randrange(len(s) + 1)
            op = rng.random()
            if op < 0.4 and s:
                del s[min(pos, len(s) - 1)]
            elif op < 0.8:
                s.insert(pos, rng.choice(alphabet))
            elif s:
                s[min(pos, len(s) - 1)] = rng.choice(alphabet)
        raw = "".join(s).encode()
        assert same(parse(raw)[0], expected(raw)), raw


def test_messages_layout_points_at_each_message():
    raw = b'{"model": "m", "messages": [ {"role": "user", "content": "a"} ,{"role":"assistant","content":"b"}]}'
    body, layout = parse(raw)
    assert [json.loads(layout.piece(s)) for s in layout.message_spans()] == body["messages"]
    assert [json.loads(bytes(b)) for b in layout.message_bytes()] == body["messages"]
    _, layout = parse('{"messages": [{"c": "\u00e9"}, {"c": "x"}]}'.encode())
    assert [json.loads(b) for b in layout.message_bytes()] == [{"c": "\u00e9"}, {"c": "x"}]


def test_no_change_forwards_the_client_bytes():
    raw = b'{"model":"served","stream":false,\n "messages":[{"role":"user","content":"\\u00e9 x"}]}'
    body, layout = parse(raw)
    assert encode({**body}, layout) is raw


def test_duplicate_keys_are_never_forwarded_as_they_came():
    raw = b'{"model": "a", "model": "b", "messages": []}'
    body, layout = parse(raw)
    data = encode({**body}, layout)
    assert data != raw and json.loads(data) == {"model": "b", "messages": []}


@pytest.mark.parametrize("raw", SAMPLES)
def test_a_rewrite_reaches_the_engine_as_json_dumps_would_send_it(raw):
    body, layout = parse(raw)
    if body is None:
        return
    out = {**body, "model": "served"}
    out["stream_options"] = {**(out.get("stream_options") or {}), "include_usage": True}
    assert same(json.loads(encode(out, layout)), json.loads(json.dumps(out)))


def test_compat_changes_to_some_messages_reach_the_engine():
    msgs = [{"role": "developer", "content": "sys"}, {"role": "user", "content": "\u00fcber"},
            {"role": "assistant", "content": "a", "reasoning": "r", "refusal": None, "tool_calls": []},
            {"role": "assistant", "content": "kept", "tool_calls": [{"id": "1"}]}]
    raw = json.dumps({"model": "m", "store": True, "reasoning_effort": "high", "messages": msgs},
                     ensure_ascii=False, separators=(",", ":")).encode()
    body, layout = parse(raw)
    out, changes = crucible({**body, "model": "served"}, 4096)
    assert changes
    data = encode(out, layout)
    assert json.loads(data) == json.loads(json.dumps(out))
    assert b'"content":"kept"' in data  # an unchanged message keeps the client's bytes


def test_a_large_body_reaches_the_engine_unchanged_in_value():
    rng = random.Random(3)
    msgs = [{"role": rng.choice(["user", "assistant", "tool"]), "content": "".join(
        rng.choice("abc \u00e9\U0001F600\"\\\n") for _ in range(rng.randint(0, 200)))} for _ in range(300)]
    raw = json.dumps({"model": "m", "stream": True, "messages": msgs, "tools": [{"a": 1}]}).encode()
    body, layout = parse(raw)
    assert body == json.loads(raw)
    out = {**body, "model": "served", "stream_options": {"include_usage": True}}
    assert json.loads(encode(out, layout)) == out


def test_non_ascii_messages_reach_the_engine_as_the_client_sent_them():
    msgs = [{"role": "user", "content": "über ✓"}, {"role": "assistant", "content": "\U0001F600"}]
    raw = json.dumps({"model": "m", "messages": msgs}, ensure_ascii=False).encode()
    body, layout = parse(raw)
    data = encode({**body, "model": "served"}, layout)
    assert json.loads(data) == {"model": "served", "messages": msgs}
    assert "über ✓".encode() in data  # the client's bytes, not \u escapes


# -- delta review H1: what a held request keeps ---------------------------------------------------------------
def _long_string(text: str, n: int) -> bytes:
    return json.dumps({"model": "m", "stream": True,
                       "messages": [{"role": "user", "content": text + "x" * n}]}, ensure_ascii=False).encode()


HELD_SHAPES = {
    "ascii string": lambda n: _long_string("", n),
    "one emoji": lambda n: _long_string("\U0001F600", n),
    "one e acute": lambda n: _long_string("\u00e9", n),
    "escaped emoji": lambda n: _long_string("", n).replace(b'"content": "', b'"content": "\\ud83d\\ude00', 1),
    "escaped e acute": lambda n: _long_string("", n).replace(b'"content": "', b'"content": "\\u00e9', 1),
    "tiny values": lambda n: b'{"model":"m","x":[' + b",".join([b"1"] * (n // 2)) + b"]}",
    "tiny messages": lambda n: b'{"model":"m","messages":[' + b",".join(
        [b'{"role":"user","content":"x"}'] * (n // 30)) + b"]}",
}


@pytest.mark.parametrize("shape", HELD_SHAPES)
def test_held_cost_is_at_least_what_a_held_body_keeps(shape):
    """A held request keeps the raw bytes, the decoded text and the parsed values. One character above U+FFFF
    makes the text and the values 4 bytes per character: 30 MiB of ASCII with one emoji kept 270 MiB."""
    import tracemalloc
    from kiln.gateway.app import held_cost
    n = 2 << 20
    tracemalloc.start()
    try:
        raw = HELD_SHAPES[shape](n)
        body, layout = parse(raw)
        kept, _peak = tracemalloc.get_traced_memory()
    finally:
        tracemalloc.stop()
    assert body is not None
    assert held_cost(raw, layout) >= kept, (held_cost(raw, layout) / kept)
    assert held_cost(raw, layout) <= 3 * kept or shape.startswith("escaped") or shape.startswith("tiny")


def test_a_held_worst_case_body_fits_the_held_budget():
    """One body at the default cap with one emoji must fit the held budget, and the budget is in MemoryMax."""
    from kiln import config
    from kiln.gateway.app import HELD_BODIES, HELD_X, held_cost
    cap = config.DEFAULT_MAX_BODY_MIB << 20
    raw = _long_string("\U0001F600", cap - 200)
    assert len(raw) <= cap
    body, layout = parse(raw)
    assert held_cost(raw, layout) <= HELD_BODIES * HELD_X * cap


@pytest.mark.parametrize("lead", ["\U0001F600", "é", ""])
def test_a_parse_in_flight_of_a_body_at_the_cap_fits_one_parse_slot(make_cfg, lead):
    """A body with one character above U+FFFF took 16 times its size while it was parsed and prepared (ASCII
    text: 4.3 times). The parse slots of memory_max_mib must hold that at the default cap (scaled down here:
    the cost is linear in the size)."""
    import tracemalloc
    from kiln import config
    from kiln.gateway.app import Gateway, parse_slot_mib
    cfg = make_cfg()
    gw = Gateway(cfg, tailscale=None)
    profile = cfg.profiles["halogen-flashnext"]
    size = 2 << 20
    tracemalloc.start()
    try:
        raw = _long_string(lead, size)
        body, layout = parse(raw)
        gw._prepare(body, layout, profile, True)
        _now, peak = tracemalloc.get_traced_memory()
    finally:
        tracemalloc.stop()
    cap = config.DEFAULT_MAX_BODY_MIB
    assert peak / len(raw) * cap <= parse_slot_mib(cap), peak / len(raw)


def test_the_shipped_gateway_unit_has_the_memory_max_of_the_formula():
    from pathlib import Path
    from kiln import config
    from kiln.gateway.app import memory_max_mib
    unit = (Path(__file__).parent.parent / "deploy" / "kiln-gateway.service").read_text()
    assert f"MemoryMax={memory_max_mib(config.DEFAULT_MAX_BODY_MIB)}M\n" in unit
