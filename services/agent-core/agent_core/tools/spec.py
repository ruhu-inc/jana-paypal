"""One typed spec per tool (TOL-01) with the metadata the engine acts on (TOL-03, TABLE TOL-03a).

Metadata comes from the tool register and is never sent to a model: `model_definition` exposes only the name, the
description and the argument schema."""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field, fields
from typing import Any, Literal

from agent_core.tools.unroll import unrolled

Profile = Literal["small", "full"]


# Field names as the tool register spells them.
REGISTER_NAMES: dict[str, str] = {
    "side_effects": "sideEffects", "parallel_safe": "parallelSafe", "timeout_ms": "timeoutMs",
    "max_result_chars": "maxResultChars", "small_model_friendly": "smallModelFriendly",
    "activity_label": "activityLabel", "uses_network": "usesNetwork", "heartbeat_ms": "heartbeatMs",
    "offline_behaviour": "offlineBehaviour",
}  # fmt: skip


@dataclass(frozen=True)
class ToolMeta:
    risk: str
    side_effects: bool
    parallel_safe: bool
    interactive: bool
    idempotent: bool
    dedupe: bool
    executor: str
    timeout_ms: int | None
    max_result_chars: dict[str, int]
    small_model_friendly: bool
    subjects: tuple[str, ...]
    levels: tuple[str, ...]
    activity_label: str
    uses_network: bool
    heartbeat_ms: int | None = None
    offline_behaviour: str | None = None

    @classmethod
    def from_register(cls, entry: dict[str, Any]) -> ToolMeta:
        values: dict[str, Any] = {}
        for f in fields(cls):
            if f.name.startswith("_"):
                continue
            value = entry.get(REGISTER_NAMES.get(f.name, f.name))
            values[f.name] = tuple(value) if isinstance(value, list) else value
        return cls(**values)

    def to_register(self) -> dict[str, Any]:
        out: dict[str, Any] = {}
        for f in fields(self):
            if f.name.startswith("_"):
                continue
            value = getattr(self, f.name)
            if value is None and f.name in ("heartbeat_ms", "offline_behaviour"):
                continue
            out[REGISTER_NAMES.get(f.name, f.name)] = list(value) if isinstance(value, tuple) else value
        return out


@dataclass(frozen=True)
class InitCtx:
    model_family: str = "generic"
    profile: Profile = "full"
    locale: str = "en"
    level: str = "O"
    subject: str = "mathematics"


@dataclass(frozen=True)
class SessionCtx(InitCtx):
    session_id: str = "ses_00000000000000000000000000"
    role: str = "solver"
    worker_circuit_open: bool = False
    online: bool = False
    enabled_online_features: tuple[str, ...] = ()
    stage: str = "S1"
    # Tools a build carries ahead of their stage (registry.EARLY_TOOLS) that this session has released: the engine
    # releases the memory tools only once their handlers are bound to the session's profile (TOL-23).
    released: frozenset[str] = frozenset()
    # The plausibility bands of the teaching skill the engine pushes (front matter `plausibility`, SKL-08), sent with
    # every worker call so out-of-band results come back flagged `implausible` (VER-26).
    plausibility: tuple[dict[str, Any], ...] = ()
    conventions: dict[str, Any] = field(default_factory=dict)


Description = str | Callable[[InitCtx], str]
Executor = Callable[[dict[str, Any], "CallContext"], Awaitable[Any]]


class InvalidArguments(Exception):
    """Raised by a host executor for arguments that pass the schema but not the tool's own rules (`invalid_args`,
    no action taken)."""


@dataclass(frozen=True)
class CallContext:
    call_id: str
    session: SessionCtx
    check_id: str | None = None
    progress: Callable[[], None] = lambda: None


@dataclass(frozen=True)
class ToolSpec:
    name: str
    toolset: str
    version: int
    stage: str
    description: Description
    params: dict[str, Any]
    meta: ToolMeta
    execute: Executor | None = field(default=None, compare=False)
    params_small: dict[str, Any] | None = None
    available: Callable[[SessionCtx], bool] | None = field(default=None, compare=False)

    def describe(self, ctx: InitCtx) -> str:
        return self.description(ctx) if callable(self.description) else self.description

    def describe_ok(self) -> bool:
        return bool(self.describe(InitCtx()).strip())

    def model_definition(self, ctx: InitCtx) -> dict[str, Any]:
        """What a model sees: name, description and argument schema, never recursive (SPEC §16). Never the metadata
        (TOL-01)."""
        params = self.params_small if ctx.profile == "small" and self.params_small else self.params
        return {"name": self.name, "description": self.describe(ctx), "parameters": unrolled(params)}

    def data(self) -> dict[str, Any]:
        """The serialisable part of the spec, for the contract round trip (TOL-01)."""
        out = {"name": self.name, "toolset": self.toolset, "version": self.version, "stage": self.stage,
               "description": self.describe(InitCtx()), "params": self.params,
               "meta": self.meta.to_register()}  # fmt: skip
        if self.params_small:
            out["paramsSmall"] = self.params_small
        return out

    @classmethod
    def from_data(cls, data: dict[str, Any]) -> ToolSpec:
        return cls(
            name=data["name"], toolset=data["toolset"], version=data["version"], stage=data["stage"],
            description=data["description"], params=data["params"], meta=ToolMeta.from_register(data["meta"]),
            params_small=data.get("paramsSmall"),
        )  # fmt: skip
