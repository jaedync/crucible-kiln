import json

from kiln.gateway.stream import SSEObserver, normalize_usage, observe_json_body


def sse(obj) -> bytes:
    return b"data: " + json.dumps(obj).encode() + b"\n\n"


def delta(**d):
    return {"choices": [{"index": 0, "delta": d}]}


def test_counts_token_events_across_arbitrary_chunk_boundaries():
    stream = (sse(delta(role="assistant")) + sse(delta(reasoning_content="Let")) + sse(delta(reasoning_content=" me")) +
              sse(delta(content="Hi")) + sse({"choices": [{"index": 0, "delta": {}, "finish_reason": "stop"}]}) +
              sse({"choices": [], "usage": {"prompt_tokens": 10, "completion_tokens": 3,
                                             "prompt_tokens_details": {"cached_tokens": 4},
                                             "completion_tokens_details": {"reasoning_tokens": 2}}}) +
              b"data: [DONE]\n\n")
    obs = SSEObserver()
    for i in range(0, len(stream), 7):  # 7-byte chunks cut through lines and JSON
        obs.feed(stream[i:i + 7], 100.0 + i / 1000)
    s = obs.stats
    assert (s.token_events, s.chars, s.finish, s.done) == (3, 8, "stop", True)
    assert s.usage == {"prompt": 10, "cached": 4, "completion": 3, "reasoning": 2}
    assert s.first_byte == 100.0 and s.first_token < s.last_token
    assert len(s.take_pending()) == 3 and s.take_pending() == []


def test_role_only_and_empty_deltas_are_not_tokens():
    obs = SSEObserver()
    obs.feed(sse(delta(role="assistant")) + sse(delta(content="")) + b": keepalive\n\n", 1.0)
    assert obs.stats.token_events == 0 and obs.stats.first_token is None


def test_tool_call_deltas_count_and_crlf_lines_parse():
    obs = SSEObserver()
    chunk = b"data: " + json.dumps(delta(tool_calls=[{"index": 0, "function": {"arguments": "{"}}])).encode() + b"\r\n\r\n"
    assert obs.feed(chunk, 5.0) == 1
    assert obs.stats.pending == [5.0]


def test_malformed_payloads_are_ignored():
    obs = SSEObserver()
    obs.feed(b"data: {not json\n\ndata: [1,2]\n\nevent: x\n\n", 1.0)
    assert obs.stats.token_events == 0


def test_usage_variants():
    assert normalize_usage({"prompt_tokens": 5, "completion_tokens": 1, "cached_tokens": 2}) == \
        {"prompt": 5, "cached": 2, "completion": 1, "reasoning": None}
    assert normalize_usage({"prompt_tokens": True})["prompt"] is None


def test_json_body():
    s = observe_json_body(json.dumps({"choices": [{"finish_reason": "length", "message": {}}],
                                      "usage": {"prompt_tokens": 7, "completion_tokens": 9}}).encode())
    assert (s.finish, s.usage["completion"]) == ("length", 9)
    assert observe_json_body(b"nope").usage is None


def test_halogen_reasoning_close_is_kept():
    u = normalize_usage({"prompt_tokens": 67, "completion_tokens": 1500, "completion_tokens_details": {
        "reasoning_tokens": 501, "reasoning_closed_at": 476, "reasoning_closed_by": "answer_room"}})
    assert (u["think_close"], u["think_closed_at"], u["reasoning"]) == ("answer_room", 476, 501)


def test_completions_text_events_count_and_errors_drop_their_message():
    from kiln.gateway.stream import safe_error
    obs = SSEObserver()
    obs.feed(sse({"choices": [{"index": 0, "text": "Hel"}]}) + sse({"choices": [{"index": 0, "text": "lo"}]}), 1.0)
    assert obs.stats.token_events == 2
    obs.feed(sse({"error": {"message": "Invalid message: secret prompt", "type": "invalid_request", "code": 400}}), 2.0)
    assert "secret" not in obs.stats.error and obs.stats.error == "upstream: invalid_request / 400"
    assert safe_error("boom secret") == "upstream: error"


def test_tool_call_fallback_is_a_boolean_in_usage():
    assert normalize_usage({"prompt_tokens": 5, "completion_tokens": 1, "tool_call_fallback": True})[
        "tool_call_fallback"] is True
    for v in (False, None, "yes", 1, {"text": "x"}):
        assert "tool_call_fallback" not in normalize_usage({"prompt_tokens": 5, "tool_call_fallback": v})
