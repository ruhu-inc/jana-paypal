"""Meta Model API: Muse Spark through the OpenAI-compatible Chat Completions endpoint.

The gateway owns the API key and resolves model aliases; devices never receive it. Only Standard mode model ids are
configured (`muse-spark-1.3`): Contributor mode is a separate id and Meta trains on its data (JM-D16). Behaviour
checked against https://dev.meta.ai/docs on 2026-10-26:

- `tool_choice` accepts only `"auto"`; `"required"`, `"none"` and a named function are 400s.
- `reasoning_effort` accepts minimal, low, medium, high, xhigh and max; Muse Spark always reasons, so `"none"` is a 400.
- `response_format: json_schema` is decoded under constraint, so no schema copy in the prompt is needed. `strict: true`
  additionally demands that every property be required at every level, which the engine's contracts (with optional
  fields) do not meet; `strict: false` is still decoded under the schema, so strict is sent as false (verify day 1).
- `completion_tokens` already includes `completion_tokens_details.reasoning_tokens`: usage is billed as reported.
- Some OpenAI fields are refused (`stop`, `n`, `logprobs`, ...): they are dropped here rather than sent.
"""

from __future__ import annotations

from collections.abc import AsyncGenerator
from typing import Any

from cloud_gateway.providers.base import ProviderReply, ProviderRequest, StreamPart
from cloud_gateway.providers.openai_compat import OpenAICompatAdapter

# Request fields Meta's Chat Completions refuses, or that belong to another provider's dialect.
UNSUPPORTED = frozenset({
    "stop", "n", "logprobs", "top_logprobs", "logit_bias", "modalities", "audio", "prediction", "verbosity",
    "web_search_options", "max_output_tokens", "chat_template_kwargs",
})  # fmt: skip
EFFORTS = frozenset({"minimal", "low", "medium", "high", "xhigh", "max"})
REASONING_FIELDS = ("reasoning_content", "reasoning")


def prepare_body(body: dict[str, Any]) -> dict[str, Any]:
    """The neutral OpenAI body in the dialect Muse Spark accepts."""
    out = {k: v for k, v in body.items() if k not in UNSUPPORTED}
    choice = out.get("tool_choice")
    if choice == "none" or not out.get("tools"):
        # Spark has no "none": a turn that must not call a tool is sent without tools. The engine still validates
        # whatever comes back, so a tool call here is refused downstream rather than run.
        out.pop("tools", None)
        out.pop("tool_choice", None)
        out.pop("parallel_tool_calls", None)
    elif choice is not None and choice != "auto":
        # "required" and a named function are refused; "auto" with the engine's own tool nudge does the same job,
        # and Jana still requires an executed, validated tool result before it accepts a computed plan.
        out["tool_choice"] = "auto"
    effort = out.get("reasoning_effort")
    if effort is not None and effort not in EFFORTS:
        out["reasoning_effort"] = "minimal"  # "none": Spark always reasons; minimal is its lightest setting
    response_format = out.get("response_format")
    if isinstance(response_format, dict) and isinstance(response_format.get("json_schema"), dict):
        if response_format["json_schema"].get("strict"):
            out["response_format"] = {
                **response_format,
                "json_schema": {**response_format["json_schema"], "strict": False},
            }
    if out.get("top_p") == 0:
        out.pop("top_p")
    messages = out.get("messages")
    if isinstance(messages, list):
        out["messages"] = [
            {k: v for k, v in m.items() if k not in REASONING_FIELDS} if isinstance(m, dict) else m for m in messages
        ]
    out["store"] = False  # no stored completions at Meta
    return out


def _without_reasoning(container: dict[str, Any] | None) -> None:
    """A model's reasoning never reaches the device: only the released text and tool calls do."""
    if isinstance(container, dict):
        for name in REASONING_FIELDS:
            container.pop(name, None)


class MetaAdapter(OpenAICompatAdapter):
    def _body(self, request: ProviderRequest, stream: bool) -> dict[str, Any]:
        body = super()._body(request, stream)
        # A model alias may set the Spark effort each requested effort becomes (`thinking` in the config): the device
        # only says low, medium or high, and the alias decides how much Spark reasons for it (SPEC §12 latency).
        thinking, effort = request.model.thinking, body.get("reasoning_effort")
        if thinking is not None and thinking.kind == "level" and effort in ("none", "low", "medium", "high"):
            mapped = thinking.value(effort)
            if isinstance(mapped, str):
                body["reasoning_effort"] = mapped
        return prepare_body(body)

    async def complete(self, request: ProviderRequest) -> ProviderReply:
        reply = await super().complete(request)
        for choice in reply.body.get("choices") or []:
            _without_reasoning(choice.get("message"))
        return reply

    async def stream(self, request: ProviderRequest) -> AsyncGenerator[StreamPart]:
        async for part in super().stream(request):
            if part.chunk is not None:
                for choice in part.chunk.get("choices") or []:
                    _without_reasoning(choice.get("delta"))
            yield part

    def __repr__(self) -> str:
        return f"MetaAdapter(base_url={self.spec.base_url!r})"
