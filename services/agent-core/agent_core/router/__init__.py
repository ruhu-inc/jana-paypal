"""agent_core.router: the only path to a model (ARC-20, MDL-01 to MDL-30).

Every call goes through `Router.complete`, which refuses to run outside an engine turn or job scope. The guard is
unconditional: there is no flag, environment variable or test switch that turns it off. The module-level `complete`
and `stream` reach the router the engine installed with `install`; before one is installed they fail with
`NoProviderError`."""

from typing import Any

from agent_core.providers.base import Message, ToolCall, ToolSchema
from agent_core.providers.llama import LlamaServerProvider
from agent_core.providers.model_json import loads as loads_model_json
from agent_core.providers.scripted import ScriptedProvider
from agent_core.router.core import (
    Hooks,
    Prepared,
    Profile,
    Router,
    RouterError,
    RouterOutsideEngineContextError,
    RouterResult,
    Shared,
    SlotResolution,
    tool_profile,
)
from agent_core.trace import current_scope


class NoProviderError(RuntimeError):
    """No model router has been installed."""


_installed: Router | None = None


def install(router: Router | None) -> None:
    global _installed
    _installed = router


def _require_engine_scope(call: str) -> None:
    if current_scope() is None:
        raise RouterOutsideEngineContextError(f"router.{call} called outside an engine turn or job")


def complete(request: dict[str, Any]) -> Any:
    """`request` holds `role`, `messages` and optionally `tools` and settings; returns the router's coroutine."""
    _require_engine_scope("complete")
    if _installed is None:
        raise NoProviderError("no model provider is configured")
    body = dict(request)
    return _installed.complete(body.pop("role"), body.pop("messages"), body.pop("tools", ()), **body)


def stream(request: dict[str, Any]) -> Any:
    """Streaming goes through the same call: the role's configuration decides whether the provider streams."""
    _require_engine_scope("stream")
    if _installed is None:
        raise NoProviderError("no model provider is configured")
    return complete(request)


__all__ = [
    "Hooks",
    "LlamaServerProvider",
    "Message",
    "NoProviderError",
    "Prepared",
    "Profile",
    "Router",
    "RouterError",
    "RouterOutsideEngineContextError",
    "RouterResult",
    "ScriptedProvider",
    "Shared",
    "SlotResolution",
    "ToolCall",
    "ToolSchema",
    "complete",
    "install",
    "loads_model_json",
    "stream",
    "tool_profile",
]
