"""Request and reply mapping for OpenAI-compatible chat APIs (llama-server; cloud providers from S2).

Building a request never sends reasoning text, and reads only the extras of the provider's own kind (MDL-02).
Parsing a reply never interprets text: think blocks, text-written tool calls and empty replies are the router's
normalisation (MDL-13, MDL-14, MDL-09)."""

from __future__ import annotations

import json
import re
import time
from collections.abc import AsyncIterator, Callable
from typing import Any

from agent_core.providers import model_json
from agent_core.providers.base import (
    AssistantTurn,
    EffortRecord,
    FinalTurn,
    Message,
    ReasoningDelta,
    StreamChunk,
    TextDelta,
    Timings,
    TokenUsage,
    ToolCall,
    ToolCallDelta,
    ToolSchema,
)

FINISH = {
    "stop": "stop",
    "length": "length",
    "tool_calls": "tool_calls",
    "function_call": "tool_calls",
    "content_filter": "content_filter",
}
UNPARSED = "__unparsed__"  # key holding tool-call arguments that are not a JSON object


def message_body(message: Message, kind: str) -> dict[str, Any]:
    body: dict[str, Any] = {"role": message.role, "content": message.content}
    if message.images:  # the OpenAI-style parts a vision model (llama-server with its projector) reads (MDL-23)
        body["content"] = [{"type": "text", "text": message.content},
                           *({"type": "image_url", "image_url": {"url": url}} for url in message.images)]  # fmt: skip
    if message.tool_calls:
        body["tool_calls"] = [
            {"id": c.id, "type": "function", "function": {"name": c.name, "arguments": json.dumps(c.arguments)}}
            for c in message.tool_calls
        ]
        for call, wire in zip(message.tool_calls, body["tool_calls"], strict=True):
            own_call = call.extras.get(kind)
            if isinstance(own_call, dict) and isinstance(own_call.get("extra_content"), dict):
                wire["extra_content"] = own_call["extra_content"]
    if message.tool_call_id:
        body["tool_call_id"] = message.tool_call_id
    if message.name and message.role == "tool":
        body["name"] = message.name
    own = message.extras.get(kind)
    if isinstance(own, dict):
        body.update({k: v for k, v in own.items() if k not in body})
    return body


GRAMMAR_HEAVY = ("maxLength", "minLength", "maxItems", "minItems", "pattern", "uniqueItems", "maxProperties",
                 "minProperties", "format", "$comment")  # fmt: skip


def grammar_friendly(schema: Any) -> Any:
    """A tool schema a decoding grammar can compile: size and pattern keywords removed. The host validates the full
    schema on every call (TOL-08), so nothing is lost but grammar size. A list of types becomes the equivalent `anyOf`:
    the Jinja engine in llama-server cannot render a list-valued `type` through some models' chat templates."""
    if isinstance(schema, dict):
        friendly = {
            k: (
                {name: grammar_friendly(child) for name, child in v.items()}
                if k in ("properties", "patternProperties") and isinstance(v, dict)
                else grammar_friendly(v)
            )
            for k, v in schema.items()
            if k not in GRAMMAR_HEAVY
        }
        if isinstance(friendly.get("type"), list) and "anyOf" not in friendly:
            friendly["anyOf"] = [{"type": t} for t in friendly.pop("type")]
        return friendly
    if isinstance(schema, list):
        return [grammar_friendly(v) for v in schema]
    return schema


def tool_body(tool: ToolSchema) -> dict[str, Any]:
    return {
        "type": "function",
        "function": {"name": tool.name, "description": tool.description, "parameters": tool.parameters},
    }


_FORMAT_NAME = re.compile(r"[^A-Za-z0-9_-]")


def format_name(name: object) -> str:
    """A `response_format` schema name as OpenAI-style endpoints accept it: letters, digits, `_` and `-`, at most 64
    characters. Muse Spark refuses anything else with 400 on `name` (a contract path such as
    `agents/companion-turn` did, §19 fix 2026-10-29)."""
    cleaned = _FORMAT_NAME.sub("_", str(name or ""))[:64]
    return cleaned or "output"


def request_body(
    model: str, messages: list[Message], tools: list[ToolSchema], kind: str, settings: dict[str, Any], stream: bool
) -> dict[str, Any]:
    body: dict[str, Any] = {"model": model, "messages": [message_body(m, kind) for m in messages], "stream": stream}
    if stream:
        body["stream_options"] = {"include_usage": True}
    if tools and settings.get("tools_off") and kind != "local":
        tools = []  # a cloud endpoint gets no tools when none may be called; a local one keeps them (below)
    if tools:
        body["tools"] = [tool_body(t) for t in tools]
        if settings.get("tools_off"):
            # The same tools stay in the prompt so a local runtime reuses its cached prefix (MDL-22); none is callable.
            body["tool_choice"] = "none"
        elif settings.get("require_tool"):
            body["tool_choice"] = "required"
    for key in ("temperature", "max_tokens", "stop", "seed", "top_p"):
        if settings.get(key) is not None:
            body[key] = settings[key]
    structured = settings.get("structured") or {}
    mode = structured.get("mode")
    if mode == "json_schema":
        body["response_format"] = {
            "type": "json_schema",
            "json_schema": {
                "name": format_name(structured.get("name")),
                "strict": True,
                "schema": structured["schema"],
            },
        }
    elif mode == "json_object":
        body["response_format"] = {"type": "json_object"}
    elif mode == "grammar":
        body["grammar"] = structured["grammar"]
    elif mode == "tool":
        body["tool_choice"] = {"type": "function", "function": {"name": structured["tool"]}}
    control = (settings.get("reasoning") or {}).get("control")
    if control in ("low", "medium", "high"):  # an effort-level model (MDL-12); toggles are the provider's own field
        body["reasoning_effort"] = control
    # Parameters this endpoint rejected before: renamed or dropped, as the router remembered (MDL-04, MDL-11).
    for name, replacement in (settings.get("param_fixes") or {}).items():
        if name in body:
            value = body.pop(name)
            if replacement:
                body[replacement] = value
    return body


def _arguments(raw: Any) -> dict[str, Any]:
    if isinstance(raw, dict):
        return raw
    try:
        parsed = model_json.loads(raw or "{}")  # LaTeX in arguments: `\frac` stays a command, not a form feed
    except (TypeError, ValueError):
        return {UNPARSED: str(raw)}
    return parsed if isinstance(parsed, dict) else {UNPARSED: str(raw)}


def _usage(raw: dict[str, Any] | None) -> TokenUsage:
    raw = raw or {}
    details = raw.get("prompt_tokens_details") or {}
    completion = raw.get("completion_tokens_details") or {}
    return TokenUsage(
        input=int(raw.get("prompt_tokens", 0)),
        output=int(raw.get("completion_tokens", 0)),
        cache_read=int(details.get("cached_tokens", 0) or 0),
        reasoning=int(completion.get("reasoning_tokens", 0) or 0),
    )


def _timings(raw: dict[str, Any] | None, total_ms: int, ttft_ms: int | None) -> Timings:
    # Some runtimes report cumulative slot timings after cache reuse. Keep the measured request duration
    # authoritative; inconsistent/non-finite counters must not become speed or model-comparison evidence.
    raw = raw if isinstance(raw, dict) else {}

    def bounded(value: Any) -> int | None:
        if type(value) not in (int, float) or not 0 <= value <= total_ms:
            return None
        return int(value)

    prompt, predicted = bounded(raw.get("prompt_ms")), bounded(raw.get("predicted_ms"))
    if prompt is not None and predicted is not None and prompt + predicted > total_ms:
        prompt = predicted = None
    return Timings(ttft_ms=bounded(ttft_ms), prompt_eval_ms=prompt, generation_ms=predicted, total_ms=total_ms)


def parse_reply(
    reply: dict[str, Any],
    requested: str,
    effort: EffortRecord,
    cost: Callable[[TokenUsage], float],
    total_ms: int,
    *,
    kind: str | None = None,
) -> AssistantTurn:
    choices = reply.get("choices") or [{}]
    choice = choices[0]
    message = choice.get("message") or {}
    calls = [
        ToolCall(
            c.get("id") or "",
            (c.get("function") or {}).get("name", ""),
            _arguments((c.get("function") or {}).get("arguments")),
            _tool_extras(c, kind),
        )
        for c in message.get("tool_calls") or []
    ]
    usage = _usage(reply.get("usage"))
    return AssistantTurn(
        text=message.get("content") or "",
        tool_calls=calls,
        finish_reason=FINISH.get(choice.get("finish_reason") or "stop", "stop"),  # type: ignore[arg-type]
        reasoning=message.get("reasoning_content") or None,
        structured=None,
        usage=usage,
        cost_usd=cost(usage),
        served_by=reply.get("model") or requested,
        effort=effort,
        timings=_timings(reply.get("timings"), total_ms, None),
    )


async def assemble_stream(
    events: AsyncIterator[dict[str, Any]],
    requested: str,
    effort: EffortRecord,
    cost: Callable[[TokenUsage], float],
    started: float,
    *,
    kind: str | None = None,
) -> AsyncIterator[StreamChunk]:
    """Deltas as they arrive, then one FinalTurn assembled from them."""
    text: list[str] = []
    reasoning: list[str] = []
    calls: dict[int, dict[str, Any]] = {}
    finish = "stop"
    usage_raw: dict[str, Any] | None = None
    timings_raw: dict[str, Any] | None = None
    served = requested
    ttft_ms: int | None = None
    async for event in events:
        served = event.get("model") or served
        usage_raw = event.get("usage") or usage_raw
        timings_raw = event.get("timings") or timings_raw
        for choice in event.get("choices") or []:
            delta = choice.get("delta") or {}
            if choice.get("finish_reason"):
                finish = choice["finish_reason"]
            if delta.get("reasoning_content"):
                ttft_ms = ttft_ms if ttft_ms is not None else int((time.monotonic() - started) * 1000)
                reasoning.append(delta["reasoning_content"])
                yield ReasoningDelta(delta["reasoning_content"])
            if delta.get("content"):
                ttft_ms = ttft_ms if ttft_ms is not None else int((time.monotonic() - started) * 1000)
                text.append(delta["content"])
                yield TextDelta(delta["content"])
            for part in delta.get("tool_calls") or []:
                ttft_ms = ttft_ms if ttft_ms is not None else int((time.monotonic() - started) * 1000)
                index = int(part.get("index", 0))
                held = calls.setdefault(index, {"id": None, "name": "", "arguments": ""})
                function = part.get("function") or {}
                held["id"] = part.get("id") or held["id"]
                held["name"] += function.get("name") or ""
                held["arguments"] += function.get("arguments") or ""
                for owner, sidecar in _tool_extras(part, kind).items():
                    target = held.setdefault("extras", {}).setdefault(owner, {"extra_content": {"google": {}}})
                    target["extra_content"]["google"].update(sidecar["extra_content"]["google"])
                yield ToolCallDelta(index, part.get("id"), function.get("name"), function.get("arguments") or "")
    usage = _usage(usage_raw)
    turn = AssistantTurn(
        text="".join(text),
        tool_calls=[
            ToolCall(c["id"] or "", c["name"], _arguments(c["arguments"]), c.get("extras", {}))
            for _, c in sorted(calls.items())
        ],
        finish_reason=FINISH.get(finish, "stop"),  # type: ignore[arg-type]
        reasoning="".join(reasoning) or None,
        structured=None,
        usage=usage,
        cost_usd=cost(usage),
        served_by=served,
        effort=effort,
        timings=_timings(timings_raw, int((time.monotonic() - started) * 1000), ttft_ms),
    )
    yield FinalTurn(turn)


def _tool_extras(call: dict[str, Any], kind: str | None) -> dict[str, Any]:
    """Replay only documented opaque Google call metadata, scoped to the provider family that returned it."""
    extra = call.get("extra_content")
    google = extra.get("google") if isinstance(extra, dict) else None
    if kind != "cloud" or not isinstance(google, dict):
        return {}
    kept = {
        k: v for k, v in google.items() if k in {"thought_signature", "function_call_id"} and isinstance(v, str) and v
    }
    return {kind: {"extra_content": {"google": kept}}} if kept else {}
