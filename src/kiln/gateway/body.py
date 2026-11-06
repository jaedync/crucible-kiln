"""Parse a request body once, and keep where each part sits in the client's bytes.

The engine gets the client's own bytes for every value the rewrite leaves alone (gateway/app.py). Only
the changed values are serialized again, so a large conversation costs one parse and one copy, not a
parse and a full `json.dumps`. When nothing changes, the engine gets the body byte for byte.

`parse` gives the same dict as `json.loads`. A body it cannot map (not UTF-8, a byte order mark, not a
JSON object, invalid JSON) takes the plain `json.loads` path and keeps no layout.
"""
from __future__ import annotations

import json
from array import array
from dataclasses import dataclass, field
from json.decoder import WHITESPACE, scanstring
from json.scanner import make_scanner

_ws = WHITESPACE.match
_scan = make_scanner(json.JSONDecoder())  # the settings json.loads uses
VIEW_MIN = 256  # bytes; a shorter piece is copied, not viewed


@dataclass
class Layout:
    """Where the parts of a parsed body sit in its text. Spans are (start, end) offsets in `text`."""
    raw: bytes
    text: str
    body: dict
    spans: dict  # top-level key -> span of its value
    # Start and end of each item of body["messages"] in turn, when it is a list. An array: a list of tuples
    # cost about 110 bytes per item, and a body can have 2 million items.
    messages: array | None
    unique: bool  # no top-level key appears twice
    _message_bytes: list | None = field(default=None, repr=False)

    @property
    def ascii(self) -> bool:
        return len(self.text) == len(self.raw)  # then text offsets are byte offsets

    def piece(self, span: tuple[int, int]) -> str:
        return self.text[span[0]:span[1]]

    def piece_bytes(self, span: tuple[int, int]):
        """The client's bytes for a span: a view when the body is ASCII, else the text encoded again. A short
        span is copied: a view costs 184 bytes, and a body can have 2 million short messages."""
        if self.ascii:
            if span[1] - span[0] < VIEW_MIN:
                return self.raw[span[0]:span[1]]
            return memoryview(self.raw)[span[0]:span[1]]
        return self.text[span[0]:span[1]].encode()

    def message_spans(self) -> list | None:
        """The span of each message, or None."""
        m = self.messages
        return None if m is None else [(m[j], m[j + 1]) for j in range(0, len(m), 2)]

    def message_bytes(self) -> list | None:
        """The client's bytes for each message, or None. Computed once: the prefix tracker and encode share it."""
        if self.messages is None:
            return None
        if self._message_bytes is None:
            m = self.messages
            self._message_bytes = [self.piece_bytes((m[j], m[j + 1])) for j in range(0, len(m), 2)]
        return self._message_bytes

    def value_bytes(self, key: str) -> bytes | None:
        span = self.spans.get(key)
        return None if span is None else self.piece_bytes(span)


def parse(raw: bytes) -> tuple[dict | None, Layout | None]:
    """(body, layout): the body as json.loads gives it, or None if it is not a JSON object."""
    if not raw:
        return None, None
    try:
        layout = _layout(raw)
    except Exception:  # noqa: BLE001 (anything odd takes the plain path below, which decides)
        layout = None
    if layout is not None:
        return layout.body, layout
    try:
        body = json.loads(raw)
    except ValueError:
        return None, None
    return (body if isinstance(body, dict) else None), None


def _layout(raw: bytes) -> Layout | None:
    if json.detect_encoding(raw) != "utf-8":
        return None
    s = raw.decode("utf-8")  # strict: invalid bytes or encoded surrogates raise, then json.loads decides
    i = _ws(s, 0).end()
    if s[i:i + 1] != "{":
        return None
    body, spans, messages, unique = {}, {}, None, True
    i = _ws(s, i + 1).end()
    if s[i:i + 1] == "}":
        end = i + 1
    else:
        while True:
            if s[i:i + 1] != '"':
                return None
            key, i = scanstring(s, i + 1)
            i = _ws(s, i).end()
            if s[i:i + 1] != ":":
                return None
            i = _ws(s, i + 1).end()
            if key in body:
                unique = False
            if key == "messages":
                messages = None
                if s[i:i + 1] == "[":
                    value, end, messages = _array(s, i)
                else:
                    value, end = _scan(s, i)
            else:
                value, end = _scan(s, i)
            body[key], spans[key] = value, (i, end)
            i = _ws(s, end).end()
            c = s[i:i + 1]
            if c == "}":
                end = i + 1
                break
            if c != ",":
                return None
            i = _ws(s, i + 1).end()
    if _ws(s, end).end() != len(s):
        return None
    return Layout(raw, s, body, spans, messages, unique)


def _array(s: str, i: int) -> tuple[list, int, array]:
    """Parse the array at s[i] item by item: (items, end, item starts and ends)."""
    items, spans = [], array("q")
    i = _ws(s, i + 1).end()
    if s[i:i + 1] == "]":
        return items, i + 1, spans
    while True:
        value, end = _scan(s, i)
        items.append(value)
        spans.append(i)
        spans.append(end)
        i = _ws(s, end).end()
        c = s[i:i + 1]
        if c == "]":
            return items, i + 1, spans
        if c != ",":
            raise ValueError("bad array")
        i = _ws(s, i + 1).end()


_PLAIN = (str, int, bool, type(None))  # equal values of one of these types serialize the same


def _same(a, b) -> bool:
    return a is b or (type(a) is type(b) and type(a) in _PLAIN and a == b)


def _shallow_same(a, b) -> bool:
    """a is b, or a dict copy of b whose values are the same objects (compat.py copies messages)."""
    if a is b:
        return True
    if type(a) is not dict or type(b) is not dict or len(a) != len(b):
        return False
    return all(k in b and v is b[k] for k, v in a.items())


def encode(out: dict, layout: Layout | None) -> bytes:
    """`out` as JSON bytes for the engine. Without a layout this is json.dumps(out).encode()."""
    if layout is None:
        return json.dumps(out).encode()
    body = layout.body
    if layout.unique and len(out) == len(body) and all(
            k1 == k2 and _same(out[k1], body[k2]) for k1, k2 in zip(out, body)):
        return layout.raw
    chunks, piece = [], layout.piece_bytes
    for key, value in out.items():
        chunks.append((", " if chunks else "{") + json.dumps(key) + ": ")
        old = body.get(key, chunks)  # chunks: a sentinel that no value is
        messages = key == "messages" and layout.messages is not None
        if _same(value, old) and not (messages and not layout.ascii):
            chunks.append(piece(layout.spans[key]))
        elif messages and type(value) is list and type(old) is list and len(value) == len(old):
            # Reuse each message's bytes (already encoded for the prefix tracker when the body is not ASCII).
            items = [raw if _shallow_same(new, prev) else json.dumps(new).encode()
                     for new, prev, raw in zip(value, old, layout.message_bytes())]
            chunks += ["[", b", ".join(items), "]"]
        else:
            chunks.append(json.dumps(value))
    chunks.append("}" if chunks else "{}")
    return b"".join(c.encode() if type(c) is str else c for c in chunks)
