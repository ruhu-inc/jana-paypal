"""Google Gemini adapter (D7 primary provider): the neutral OpenAI-shaped request to the Gemini REST API
(`models/{id}:generateContent`, `models/{id}:streamGenerateContent?alt=sse`) and the reply back.

The API key goes in the `x-goog-api-key` header only, never in a URL. The session pseudonym is not sent: the API has
no field for it. Function-call thought signatures travel to the device in `tool_calls[].extra_content.google` and
come back the same way; a replayed call without one carries the API's documented skip value instead."""

from __future__ import annotations

import json
import re
import secrets
import time
from collections.abc import AsyncGenerator
from typing import Any

import httpx

from cloud_gateway.config import ModelSpec, ProviderSpec
from cloud_gateway.custody import Secret
from cloud_gateway.providers.base import ProviderReply, ProviderRequest, StreamPart, UpstreamError, Usage
from cloud_gateway.providers.http import post_json, sse_events

SKIP_SIGNATURE = "skip_thought_signature_validator"
FINISH = {
    "STOP": "stop",
    "MAX_TOKENS": "length",
    "SAFETY": "content_filter",
    "RECITATION": "content_filter",
    "BLOCKLIST": "content_filter",
    "PROHIBITED_CONTENT": "content_filter",
    "SPII": "content_filter",
    "IMAGE_SAFETY": "content_filter",
}
_DATA_URL = re.compile(r"^data:(?P<mime>image/[a-z0-9.+-]+);base64,(?P<data>[A-Za-z0-9+/=\s]+)$")
_EFFORT = {"none": "none", "minimal": "none", "low": "low", "medium": "medium", "high": "high"}


def _text_of(content: Any) -> str:
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        return "".join(p.get("text", "") for p in content if isinstance(p, dict) and p.get("type") == "text")
    return ""


def _user_parts(content: Any) -> list[dict[str, Any]]:
    if isinstance(content, str):
        return [{"text": content}] if content else []
    parts: list[dict[str, Any]] = []
    for part in content or []:
        if not isinstance(part, dict):
            continue
        if part.get("type") == "text" and part.get("text"):
            parts.append({"text": part["text"]})
        elif part.get("type") == "image_url":
            url = (part.get("image_url") or {}).get("url", "")
            found = _DATA_URL.match(url) if isinstance(url, str) else None
            if found is None:
                raise UpstreamError("format_error", "images must be data URLs", param="messages")
            parts.append({"inlineData": {"mimeType": found["mime"], "data": re.sub(r"\s", "", found["data"])}})
        elif part.get("type") == "input_audio":
            audio = part.get("input_audio") or {}
            if (
                audio.get("format") != "wav"
                or not isinstance(audio.get("data"), str)
                or not re.fullmatch(r"[A-Za-z0-9+/=]+", audio["data"])
            ):
                raise UpstreamError("format_error", "audio must be bounded inline WAV", param="messages")
            parts.append({"inlineData": {"mimeType": "audio/wav", "data": audio["data"]}})
    return parts


def _arguments(raw: Any) -> dict[str, Any]:
    if isinstance(raw, dict):
        return raw
    try:
        parsed = json.loads(raw or "{}")
    except (TypeError, ValueError):
        return {}
    return parsed if isinstance(parsed, dict) else {}


def _response_object(content: Any) -> dict[str, Any]:
    text = _text_of(content)
    try:
        parsed = json.loads(text)
    except ValueError:
        return {"result": text}
    return parsed if isinstance(parsed, dict) else {"result": parsed}


def _model_parts(message: dict[str, Any]) -> list[dict[str, Any]]:
    parts: list[dict[str, Any]] = []
    text = _text_of(message.get("content"))
    if text:
        parts.append({"text": text})
    first_call = True
    for call in message.get("tool_calls") or []:
        function = call.get("function") or {}
        part: dict[str, Any] = {
            "functionCall": {"name": function.get("name", ""), "args": _arguments(function.get("arguments"))}
        }
        google = (call.get("extra_content") or {}).get("google") or {}
        if google.get("function_call_id"):
            part["functionCall"]["id"] = google["function_call_id"]
        signature = google.get("thought_signature")
        if signature:
            part["thoughtSignature"] = signature
        elif first_call:
            part["thoughtSignature"] = SKIP_SIGNATURE
        first_call = False
        parts.append(part)
    return parts


def _contents(messages: list[dict[str, Any]]) -> tuple[list[str], list[dict[str, Any]]]:
    system: list[str] = []
    contents: list[dict[str, Any]] = []
    call_names: dict[str, str] = {}
    provider_ids: dict[str, str] = {}

    def add(role: str, parts: list[dict[str, Any]]) -> None:
        if not parts:
            return
        same_role = contents and contents[-1]["role"] == role
        # A tool-result turn and the next instruction are distinct protocol turns, even though both use `user`.
        # Combining the instruction into the functionResponse content makes strict Gemini runtimes treat the
        # conversation as ending at the preceding model tool call.
        result_boundary = (
            role == "user"
            and same_role
            and (
                any("functionResponse" in p for p in contents[-1]["parts"])
                != any("functionResponse" in p for p in parts)
            )
        )
        if same_role and not result_boundary:
            contents[-1]["parts"].extend(parts)
        else:
            contents.append({"role": role, "parts": parts})

    for message in messages:
        role = message.get("role")
        if role in ("system", "developer"):
            system.append(_text_of(message.get("content")))
        elif role == "user":
            add("user", _user_parts(message.get("content")))
        elif role == "assistant":
            for call in message.get("tool_calls") or []:
                call_names[str(call.get("id"))] = (call.get("function") or {}).get("name", "")
                provider_id = ((call.get("extra_content") or {}).get("google") or {}).get("function_call_id")
                if isinstance(provider_id, str) and provider_id:
                    provider_ids[str(call.get("id"))] = provider_id
            add("model", _model_parts(message))
        elif role == "tool":
            name = message.get("name") or call_names.get(str(message.get("tool_call_id")), "")
            response = _response_object(message.get("content"))
            result = {"name": name, "response": response}
            if str(message.get("tool_call_id")) in provider_ids:
                result["id"] = provider_ids[str(message.get("tool_call_id"))]
            add("user", [{"functionResponse": result}])
        else:
            raise UpstreamError("format_error", "unknown message role", param="messages")
    return [s for s in system if s], contents


def _tool_config(choice: Any) -> dict[str, Any] | None:
    if choice in (None, "auto"):
        return None
    if choice == "none":
        return {"functionCallingConfig": {"mode": "NONE"}}
    if choice == "required":
        return {"functionCallingConfig": {"mode": "ANY"}}
    if isinstance(choice, dict):
        name = (choice.get("function") or {}).get("name")
        if name:
            return {"functionCallingConfig": {"mode": "ANY", "allowedFunctionNames": [name]}}
    raise UpstreamError("format_error", "unsupported tool_choice", param="tool_choice")


def translate(model: ModelSpec, body: dict[str, Any]) -> dict[str, Any]:
    """The Gemini generateContent request for an OpenAI chat-completions body."""
    system, contents = _contents(body.get("messages") or [])
    request: dict[str, Any] = {"contents": contents}
    if system:
        request["systemInstruction"] = {"parts": [{"text": "\n\n".join(system)}]}
    tools = [t["function"] for t in body.get("tools") or [] if t.get("type") == "function" and "function" in t]
    if tools:
        request["tools"] = [
            {
                "functionDeclarations": [
                    {k: v for k, v in (("name", t.get("name")), ("description", t.get("description")),
                                       ("parametersJsonSchema", t.get("parameters"))) if v is not None}
                    for t in tools
                ]
            }
        ]  # fmt: skip
    tool_config = _tool_config(body.get("tool_choice"))
    if tool_config:
        request["toolConfig"] = tool_config
    generation: dict[str, Any] = {}
    for source, target in (("temperature", "temperature"), ("top_p", "topP"), ("seed", "seed")):
        if body.get(source) is not None:
            generation[target] = body[source]
    limit = body.get("max_completion_tokens", body.get("max_tokens"))
    if limit is not None:
        generation["maxOutputTokens"] = limit
    stop = body.get("stop")
    if stop:
        generation["stopSequences"] = [stop] if isinstance(stop, str) else list(stop)
    response_format = body.get("response_format") or {}
    if response_format.get("type") == "json_schema":
        generation["responseMimeType"] = "application/json"
        generation["responseJsonSchema"] = (response_format.get("json_schema") or {}).get("schema", {})
    elif response_format.get("type") == "json_object":
        generation["responseMimeType"] = "application/json"
    effort = _EFFORT.get(str(body.get("reasoning_effort")))
    if effort is not None and model.thinking is not None:
        value = model.thinking.value(effort)  # type: ignore[arg-type]
        if value is not None:
            field = "thinkingLevel" if model.thinking.kind == "level" else "thinkingBudget"
            generation["thinkingConfig"] = {field: value}
    if generation:
        request["generationConfig"] = generation
    return request


def usage_of(metadata: dict[str, Any] | None) -> Usage:
    metadata = metadata or {}
    thoughts = int(metadata.get("thoughtsTokenCount", 0) or 0)
    return Usage(
        input_tokens=int(metadata.get("promptTokenCount", 0) or 0),
        output_tokens=int(metadata.get("candidatesTokenCount", 0) or 0) + thoughts,
        cached_tokens=int(metadata.get("cachedContentTokenCount", 0) or 0),
        reasoning_tokens=thoughts,
    )


def _call_id(call: dict[str, Any]) -> str:
    given = call.get("id")
    return given if isinstance(given, str) and given else "call_" + secrets.token_hex(8)


def _tool_call(part: dict[str, Any]) -> dict[str, Any]:
    call = part["functionCall"]
    out: dict[str, Any] = {
        "id": _call_id(call),
        "type": "function",
        "function": {"name": call.get("name", ""), "arguments": json.dumps(call.get("args") or {})},
    }
    if part.get("thoughtSignature"):
        out["extra_content"] = {"google": {"thought_signature": part["thoughtSignature"]}}
    if isinstance(call.get("id"), str) and call["id"]:
        out.setdefault("extra_content", {}).setdefault("google", {})["function_call_id"] = call["id"]
    return out


def _candidate(raw: dict[str, Any]) -> tuple[list[dict[str, Any]], str | None]:
    candidates = raw.get("candidates") or []
    if not candidates:
        blocked = (raw.get("promptFeedback") or {}).get("blockReason")
        return [], "SAFETY" if blocked else None
    first = candidates[0]
    return list((first.get("content") or {}).get("parts") or []), first.get("finishReason")


def _finish(reason: str | None, has_calls: bool) -> str:
    if has_calls and reason in (None, "STOP"):
        return "tool_calls"
    return FINISH.get(reason or "STOP", "stop")


def reply(raw: dict[str, Any], model: ModelSpec) -> ProviderReply:
    parts, reason = _candidate(raw)
    text = "".join(p.get("text", "") for p in parts if "text" in p and not p.get("thought"))
    calls = [_tool_call(p) for p in parts if "functionCall" in p]
    usage = usage_of(raw.get("usageMetadata"))
    message: dict[str, Any] = {"role": "assistant", "content": text if text or not calls else None}
    if calls:
        message["tool_calls"] = calls
    body = {
        "id": "chatcmpl-" + str(raw.get("responseId") or secrets.token_hex(12)),
        "object": "chat.completion",
        "created": int(time.time()),
        "model": raw.get("modelVersion") or model.id,
        "choices": [{"index": 0, "message": message, "finish_reason": _finish(reason, bool(calls))}],
        "usage": usage.openai(),
    }
    return ProviderReply(body, usage)


class GeminiAdapter:
    def __init__(self, spec: ProviderSpec, key: Secret | None, client: httpx.AsyncClient) -> None:
        self.spec = spec
        self._key = key
        self.client = client
        self.timeout = httpx.Timeout(spec.timeout_s, connect=spec.connect_timeout_s)

    def __repr__(self) -> str:
        return f"GeminiAdapter(base_url={self.spec.base_url!r})"

    def _headers(self) -> dict[str, str]:
        if self._key is None:
            raise UpstreamError("upstream_auth", "no provider key in the secret source")
        return {"x-goog-api-key": self._key.reveal(), "content-type": "application/json"}

    async def _request_headers(self) -> dict[str, str]:
        return self._headers()

    def _url(self, model: ModelSpec, stream: bool) -> str:
        method = "streamGenerateContent?alt=sse" if stream else "generateContent"
        return f"{self.spec.base_url.rstrip('/')}/models/{model.id}:{method}"

    async def complete(self, request: ProviderRequest) -> ProviderReply:
        raw = await post_json(
            self.client, self._url(request.model, False), await self._request_headers(),
            translate(request.model, request.body),
            self.timeout,
        )  # fmt: skip
        if not isinstance(raw, dict):
            raise UpstreamError("bad_response", "reply is not an object")
        return reply(raw, request.model)

    async def stream(self, request: ProviderRequest) -> AsyncGenerator[StreamPart]:
        headers = await self._request_headers()
        chunk_id = "chatcmpl-" + secrets.token_hex(12)
        created = int(time.time())
        served = request.model.id
        usage = Usage()
        calls = 0
        reason: str | None = None
        started = False

        def chunk(delta: dict[str, Any], finish: str | None = None) -> dict[str, Any]:
            return {"id": chunk_id, "object": "chat.completion.chunk", "created": created, "model": served,
                    "choices": [{"index": 0, "delta": delta, "finish_reason": finish}]}  # fmt: skip

        events = sse_events(
            self.client, self._url(request.model, True), headers, translate(request.model, request.body), self.timeout
        )
        async for event in events:
            served = event.get("modelVersion") or served
            if event.get("usageMetadata"):
                usage = usage_of(event["usageMetadata"])
            parts, finish = _candidate(event)
            reason = finish or reason
            for part in parts:
                delta: dict[str, Any] = {}
                if not started:
                    delta["role"] = "assistant"
                if "functionCall" in part:
                    call = _tool_call(part)
                    delta["tool_calls"] = [{"index": calls, **call}]
                    calls += 1
                elif "text" in part and not part.get("thought") and part["text"]:
                    delta["content"] = part["text"]
                else:
                    continue
                started = True
                yield StreamPart(chunk(delta))
        yield StreamPart(chunk({} if started else {"role": "assistant"}, _finish(reason, calls > 0)))
        yield StreamPart(None, usage)

    async def aclose(self) -> None:
        await self.client.aclose()
