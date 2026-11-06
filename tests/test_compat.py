"""Request dialects: Pi and Hermes requests made for Halogen, reshaped for Crucible's strict server."""
import pytest

from kiln.engines.state import StateFile, READY
from kiln.gateway.compat import adapt, crucible
from tests.test_gateway import records

CRUCIBLE = {"label": "Crucible / Flash-Next", "engine": "crucible", "model": "qwen3.8-flash-next",
            "served_id": "crucible", "aliases": ["Qwen3.8 Flash Next", "halogen-qwen3.8-flash-next"],
            "port": 18790, "context": 65536, "slots": 4, "kv": {"positions": 262144},
            "request_compat": "crucible", "default_max_tokens": 16384,
            "native": {"ExecStart": "/bin/true"}}

# What Pi 1.0 sends with a Halogen model entry (thinkingFormat openai, thinking on).
PI = {"model": "halogen-qwen3.8-flash-next", "stream": True, "stream_options": {"include_usage": True},
      "max_tokens": 32768, "reasoning_effort": "high",
      "messages": [{"role": "system", "content": "sys"}, {"role": "user", "content": "hi"},
                   {"role": "assistant", "content": None, "reasoning": "thought",
                    "tool_calls": [{"id": "c1", "type": "function", "function": {"name": "f", "arguments": "{}"}}]},
                   {"role": "tool", "tool_call_id": "c1", "content": "42"}],
      "tools": [{"type": "function", "function": {"name": "f", "parameters": {"type": "object"}}}]}

# What Hermes 0.21 (custom provider) can send.
HERMES = {"model": "qwen3.8-flash-next", "messages": [{"role": "user", "content": "hi"},
                                                      {"role": "assistant", "content": "ok", "refusal": None,
                                                       "tool_calls": []}],
          "reasoning_effort": "none", "prompt_cache_key": "abc", "temperature": 0.7}


def test_pi_request_by_default_loses_thinking_visibly():
    out, changes = crucible(PI)
    assert "reasoning_effort" not in out and "enable_thinking" not in out
    assert out["max_tokens"] == 32768 and out["tools"] == PI["tools"]
    asst = out["messages"][2]
    assert asst["reasoning_content"] == "thought" and "reasoning" not in asst and asst["tool_calls"]
    assert changes == ["drop:reasoning_effort", "messages:reasoning->reasoning_content"]
    assert PI["messages"][2]["reasoning"] == "thought"  # the caller's body is not mutated


def test_pi_request_with_thinking_pass():
    out, changes = crucible(PI, thinking="pass")
    assert (out["enable_thinking"], out["reasoning_effort"]) == (True, "xhigh")
    assert changes == ["set:enable_thinking=true", "reasoning_effort:high->xhigh",
                       "messages:reasoning->reasoning_content"]


def test_pi_without_thinking_sends_no_thinking_fields():
    body = {k: v for k, v in PI.items() if k != "reasoning_effort"}
    for mode in ("drop", "pass"):
        out, _ = crucible(body, thinking=mode)
        assert "enable_thinking" not in out and "reasoning_effort" not in out  # Crucible's default: off


@pytest.mark.parametrize("sent,expect,names", [
    ({"reasoning_effort": "low"}, {"enable_thinking": True, "reasoning_effort": "low"}, ["set:enable_thinking=true"]),
    ({"reasoning_effort": "max"}, {"enable_thinking": True, "reasoning_effort": "xhigh"},
     ["set:enable_thinking=true", "reasoning_effort:max->xhigh"]),
    ({"reasoning_effort": "none"}, {"enable_thinking": False},
     ["set:enable_thinking=false", "drop:reasoning_effort"]),
    ({"reasoning": {"enabled": True, "effort": "high"}}, {"enable_thinking": True, "reasoning_effort": "xhigh"},
     ["reasoning->reasoning_effort", "set:enable_thinking=true", "reasoning_effort:high->xhigh"]),
    ({"reasoning": {"enabled": False, "effort": "none"}}, {"enable_thinking": False},
     ["reasoning->reasoning_effort", "set:enable_thinking=false", "drop:reasoning_effort"]),
    ({"chat_template_kwargs": {"enable_thinking": True}}, {"enable_thinking": True},
     ["chat_template_kwargs->enable_thinking", "set:enable_thinking=true"]),
    # The record names the outcome (review8): an effort with thinking off is dropped, not "mapped".
    ({"reasoning_effort": "high", "chat_template_kwargs": {"enable_thinking": False}}, {"enable_thinking": False},
     ["chat_template_kwargs->enable_thinking", "set:enable_thinking=false", "drop:reasoning_effort"]),
    ({"reasoning_effort": "none", "enable_thinking": True}, {"enable_thinking": True}, ["drop:reasoning_effort"]),
    ({"reasoning_effort": "ultra"}, {"reasoning_effort": "ultra"}, []),  # unknown: the engine refuses it by name
])
def test_thinking_pass_maps_controls_to_crucible(sent, expect, names):
    out, changes = crucible({"messages": [], **sent}, thinking="pass")
    out.pop("messages")
    assert out == expect and changes == names


def test_thinking_drop_removes_every_control():
    out, changes = crucible({"messages": [], "reasoning_effort": "high", "enable_thinking": True,
                             "reasoning": {"effort": "high"}, "thinking": {"type": "enabled"}})
    assert out == {"messages": []}
    assert changes == ["drop:reasoning_effort", "drop:reasoning", "drop:enable_thinking", "drop:thinking"]


def test_hermes_request_and_missing_max_tokens():
    out, changes = crucible(HERMES, default_max_tokens=16384)
    assert set(out) == {"model", "messages", "temperature", "max_tokens"} and out["max_tokens"] == 16384
    assert out["messages"][1] == {"role": "assistant", "content": "ok"}
    assert changes == ["drop:prompt_cache_key", "drop:reasoning_effort", "messages:drop:refusal",
                       "messages:drop:tool_calls", "set:max_tokens"]


def test_fields_that_change_the_answer_are_left_for_the_engine_to_refuse():
    out, changes = crucible({"messages": [], "presence_penalty": 1.5, "frequency_penalty": 0,
                             "response_format": {"type": "json_object"}, "logprobs": 0})
    assert out["presence_penalty"] == 1.5 and out["response_format"] == {"type": "json_object"}
    assert out["logprobs"] == 0  # 0 is not the boolean default; keep it rather than guess
    assert changes == ["drop:frequency_penalty"]


def test_other_engines_get_the_body_unchanged():
    assert adapt(PI, None) == (PI, [])


async def test_gateway_adapts_for_crucible(make_cfg, aiohttp_server, aiohttp_client):
    from aiohttp import web
    from tests.test_gateway import FakeEngine, STREAM, Gateway, Whois, _no_lookup
    engine = FakeEngine()
    eapp = web.Application()
    eapp.router.add_post("/v1/chat/completions", engine.chat)
    eserver = await aiohttp_server(eapp)
    cfg = make_cfg(**{"profiles.crucible-flashnext": {**CRUCIBLE, "port": eserver.port}})
    StateFile(cfg.state_file).set("crucible-flashnext", READY)
    client = await aiohttp_client(Gateway(cfg, whois=Whois(lookup=_no_lookup)).app())
    resp = await client.post("/v1/chat/completions", json=PI)
    assert resp.status == 200 and await resp.read() == STREAM
    assert resp.headers["X-Kiln-Adapted"] == "drop:reasoning_effort, messages:reasoning->reasoning_content"
    sent = engine.bodies[0]
    assert sent["model"] == "crucible" and "reasoning_effort" not in sent and "enable_thinking" not in sent
    assert sent["stream_options"] == {"include_usage": True}
    (rec,) = records(cfg)
    assert rec["adapted"] == ["drop:reasoning_effort", "messages:reasoning->reasoning_content"]
    assert rec["profile"] == "crucible-flashnext" and rec["reasoning_effort"] == "high"  # what the client asked
