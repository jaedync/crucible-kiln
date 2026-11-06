"""Request dialects: what an engine needs changed so existing clients work unchanged.

Most engines take the clients' requests as they are. Crucible's server is
strict: it answers HTTP 400 for any field it does not know, takes thinking only
as `enable_thinking` plus `reasoning_effort` in xhigh/medium/low, and accepts
only the field name `reasoning_content` for replayed thinking. Pi and Hermes
were configured for Halogen and send other shapes, so for profiles with
`request_compat = "crucible"` the gateway removes fields that change nothing
for this engine, translates the ones that have a direct equivalent (replayed
reasoning; the thinking level when the profile sets thinking = "pass"), and
removes thinking controls when it sets "drop" (the default). It records every change by name (never by value)
in the record and in the response header X-Kiln-Adapted. A field that would
change the answer (a penalty, a response format) is left in place, so the
engine refuses it openly instead of the gateway ignoring it silently.
"""
from __future__ import annotations

# Fields that only steer a provider's caching, storage, routing or accounting.
CRUCIBLE_DROP = ("prompt_cache_key", "prompt_cache_retention", "store", "user", "metadata", "service_tier",
                 "safety_identifier")
# Client thinking levels -> Crucible's three. None = thinking off.
CRUCIBLE_EFFORT = {"none": None, "off": None, "minimal": "low", "low": "low", "medium": "medium",
                   "high": "xhigh", "xhigh": "xhigh", "max": "xhigh"}
# Fields whose value is the documented default, so removing them changes nothing.
CRUCIBLE_NOOP = {"frequency_penalty": (0, 0.0), "presence_penalty": (0, 0.0), "logprobs": (False,),
                 "response_format": ({"type": "text"},), "tool_choice": (None,), "tools": (None,),
                 "parallel_tool_calls": (None,), "stop": (None,), "seed": (None,)}
# Assistant-message fields some clients replay that carry nothing Crucible can use.
CRUCIBLE_MSG_NULLS = ("refusal", "audio", "function_call", "annotations")


def _message(msg, index: int, changes: list[str]):
    if not isinstance(msg, dict):
        return msg
    out = dict(msg)
    role = out.get("role")
    if role == "developer" and index == 0:
        out["role"] = "system"
        changes.append("messages:developer->system")
    if role != "assistant":
        return out
    if isinstance(out.get("reasoning"), str) and "reasoning_content" not in out:
        out["reasoning_content"] = out.pop("reasoning")
        changes.append("messages:reasoning->reasoning_content")
    for key in ("reasoning_details", *(k for k in CRUCIBLE_MSG_NULLS if out.get(k) in (None, []))):
        if key in out:
            del out[key]
            changes.append(f"messages:drop:{key}")
    if "tool_calls" in out and not out["tool_calls"]:  # [] or null: strict servers reject both
        del out["tool_calls"]
        changes.append("messages:drop:tool_calls")
    return out


# Every thinking control a client may send. With thinking = "drop" all are removed.
THINKING_FIELDS = ("reasoning_effort", "reasoning", "enable_thinking", "chat_template_kwargs", "thinking",
                   "thinking_token_budget")


def _drop_thinking(out: dict, changes: list[str]) -> None:
    for key in THINKING_FIELDS:
        if key in out:
            del out[key]
            changes.append(f"drop:{key}")


def _pass_thinking(out: dict, changes: list[str]) -> None:
    """Fold every client's thinking controls into Crucible's enable_thinking + reasoning_effort.
    Change names describe the outcome, so the record never claims an effort that was dropped."""
    want = None  # True/False when the client said whether to think
    kwargs = out.get("chat_template_kwargs")
    if isinstance(kwargs, dict) and set(kwargs) <= {"enable_thinking", "preserve_thinking"}:
        want = out.pop("chat_template_kwargs").get("enable_thinking")
        changes.append("chat_template_kwargs->enable_thinking")
    effort = out.pop("reasoning_effort", None)
    r = out.get("reasoning")
    if isinstance(r, dict) and set(r) <= {"enabled", "effort"}:  # OpenRouter-style, sent by Hermes
        del out["reasoning"]
        want = r.get("enabled", want)
        effort = r.get("effort", effort)
        changes.append("reasoning->reasoning_effort")
    mapped = None
    if isinstance(effort, str) and effort.lower() in CRUCIBLE_EFFORT:
        mapped = CRUCIBLE_EFFORT[effort.lower()]
        if mapped is None:
            want = False if want is None else want
        elif want is None:
            want = True
    elif effort is not None:
        out["reasoning_effort"] = effort  # a level Crucible does not know: the engine refuses it by name
        return
    if "enable_thinking" not in out and want is not None:
        out["enable_thinking"] = bool(want)
        changes.append(f"set:enable_thinking={str(bool(want)).lower()}")
    if effort is None:
        return
    if out.get("enable_thinking") is True and mapped is not None:
        out["reasoning_effort"] = mapped
        if mapped != effort:
            changes.append(f"reasoning_effort:{effort}->{mapped}")
    else:  # thinking is off, so an effort has no effect
        changes.append("drop:reasoning_effort")


def crucible(body: dict, default_max_tokens: int | None = None, thinking: str = "drop") -> tuple[dict, list[str]]:
    """The request as Crucible accepts it, plus the names of what changed (deduplicated, in order)."""
    out, changes = dict(body), []
    for key in CRUCIBLE_DROP:
        if key in out:
            del out[key]
            changes.append(f"drop:{key}")
    # "drop" until the server returns thinking as reasoning_content: today it arrives in content with a
    # literal </think>, which clients show as answer text and replay. "pass" maps it (profile option).
    (_pass_thinking if thinking == "pass" else _drop_thinking)(out, changes)
    for key, defaults in CRUCIBLE_NOOP.items():
        if key in out and any(out[key] == d and type(out[key]) is type(d) for d in defaults):
            del out[key]
            changes.append(f"drop:{key}")
    opts = out.get("stream_options")
    if isinstance(opts, dict) and set(opts) - {"include_usage"}:
        out["stream_options"] = {k: v for k, v in opts.items() if k == "include_usage"}
        changes.append("stream_options:include_usage-only")
    if isinstance(out.get("messages"), list):
        out["messages"] = [_message(m, i, changes) for i, m in enumerate(out["messages"])]
    if default_max_tokens and "max_tokens" not in out and "max_completion_tokens" not in out:
        # Crucible's own default is 128 tokens, far below what Halogen gave the same clients.
        out["max_tokens"] = default_max_tokens
        changes.append("set:max_tokens")
    return out, list(dict.fromkeys(changes))


def adapt(body: dict, compat: str | None, default_max_tokens: int | None = None,
          thinking: str = "drop") -> tuple[dict, list[str]]:
    if compat == "crucible":
        return crucible(body, default_max_tokens, thinking)
    return body, []
