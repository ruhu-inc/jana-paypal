"""The model router: role → tier → candidates, retry, fallback and normalisation (MDL-02, MDL-04 to MDL-15, MDL-22,
MDL-30; the tool profile of TOL-17).

One `complete` call is one model call as the engine sees it. The router never executes tools: the engine runs tool
calls only after a call has finished and been normalised, and a retried or re-issued call carries the same message
state (tool results included), so no tool runs twice (MDL-10). Requests to one provider never carry another
provider's private fields or any reasoning text (MDL-02)."""

from __future__ import annotations

import asyncio
import json
import random
import time
from collections import Counter
from collections.abc import Awaitable, Callable, Sequence
from dataclasses import dataclass, field, replace
from typing import Any, Literal

from jsonschema import Draft202012Validator

from agent_core.config import Settings
from agent_core.providers.base import (
    AssistantTurn,
    FinalTurn,
    Message,
    ModelCapabilities,
    ProviderError,
    ReasoningDelta,
    TextDelta,
    ToolCallDelta,
    ToolSchema,
)
from agent_core.router.capabilities import layered
from agent_core.router.config import SLOTS, Candidate, RouterConfig
from agent_core.router.effort import as_level
from agent_core.router.effort import plan as plan_effort
from agent_core.router.features import FeatureRejections
from agent_core.router.normalise import ThinkScrubber, normalise
from agent_core.router.power import PowerPlan
from agent_core.router.recovery import RECOVERY, Backoff, retries_for
from agent_core.router.schedule import Scheduler
from agent_core.router.structured import (
    MODE_PARAM,
    OUTPUT_TOOL,
    check,
    correction,
    pick_mode,
    prompt_instruction,
    request_settings,
)
from agent_core.router.trace import CallRecord, attempt_record
from agent_core.trace import current_scope

Profile = Literal["small", "full"]
# What a slot needs whatever its configuration says, and the one-line notice when it resolves to nothing (PRM-17).
SLOT_REQUIRES: dict[str, dict[str, Any]] = {"vision": {"vision": True}}
SLOT_OFF_NOTICES = {"vision": "Photo reading isn't available on this computer yet."}
DEFAULT_PRIORITY = 5


def tool_profile(tier: str, locality: str, fast_model: bool = False) -> Profile:
    """TOL-17: the local fast tier gets the small tool profile; the local strong tier and the cloud get the full
    one. The judge tier is neither in the specification and is given the full profile. A local candidate of another
    tier that is itself a fast-tier model (on the low class the strong tier falls back to the 8B-class model) is the
    same small model and gets the small profile: the full one's schemas alone overflow its context (D128)."""
    return "small" if locality == "local" and (tier == "fast" or fast_model) else "full"


# Output tokens a reasoning cloud model may spend thinking before its reply, by effort (Muse Spark measured 2026-10-26:
# about 250-950 reasoning tokens at minimal and low on a tutor turn).
REASONING_HEADROOM = {"none": 1536, "low": 2048, "medium": 4096, "high": 8192}
# A local model that always reasons (Muse Glimmer at reasoning_strength low, JM-D11) uses ~150-600 output tokens on
# reasoning before its reply (SPEC §7), so the role's cap gets this much room on top.
LOCAL_REASONING_HEADROOM = 1024
# A cloud call still unanswered after this long has the network probed again; if the probe finds it gone the call is
# dropped and served locally, so a power cut costs seconds rather than the request timeout (W3, MDL-24).
CLOUD_STALL_CHECK_S = 5.0
USABLE_NETWORK = ("online", "metered")


class RouterOutsideEngineContextError(RuntimeError):
    """A model call was made outside an engine turn or job (ARC-20)."""


class RouterError(Exception):
    """The call failed after every permitted retry and fallback. `reason` is from TABLE MDL-07a."""

    def __init__(self, reason: str, record: CallRecord | None = None) -> None:
        trigger = reason
        reason = "bad_response" if reason.endswith("_twice") else reason
        super().__init__(f"{reason} ({trigger})" if trigger != reason else reason)
        self.reason = reason
        self.trigger = trigger  # the fallback trigger, for bad responses (empty_twice, ...)
        self.record = record


@dataclass(frozen=True)
class Prepared:
    """What `prepare` returns when it also narrows the tools for a candidate (None keeps the call's own)."""

    messages: list[Message]
    tools: list[ToolSchema] | None = None
    # The decode schema for this candidate when it differs from the call's (an online candidate's tutor turn may carry
    # `sketch_wanted`, VSL-43); None keeps the call's own.
    output: tuple[str, dict[str, Any], Draft202012Validator] | None = None


@dataclass
class Hooks:
    """What the router asks of the engine and the runtime supervisor. A missing hook means that preparation is not
    possible, so the reason's retry is skipped."""

    compact: Callable[[list[Message]], Awaitable[list[Message]]] | None = None
    shrink: Callable[[list[Message]], Awaitable[list[Message]]] | None = None
    restart_runtime: Callable[[], Awaitable[bool]] | None = None
    lower_context: Callable[[str], Awaitable[bool]] | None = None
    on_status: Callable[[str, dict[str, Any]], None] | None = None
    on_trace: Callable[[CallRecord], None] | None = None
    # A call leaving the device (ARC-10, MDL-24): `started` before its request is prepared, `ended` once it is over;
    # `blocked` when the cloud would have answered but the network is unusable (API-15).
    on_egress: Callable[[str, Candidate], None] | None = None
    # A cloud candidate failed and a later one takes over (ARC-12, MDL-09): True when the cloud had already streamed
    # text into its private buffer (the notice is then "Continuing on this computer.").
    on_cloud_fallback: Callable[[bool], None] | None = None


@dataclass(frozen=True)
class RouterResult:
    turn: AssistantTurn
    structured: dict[str, Any] | None
    schema_valid: bool | None
    errors: list[str]
    record: CallRecord


@dataclass
class _Fallback(Exception):
    reason: str
    local_only: bool = False


class _Reissue(Exception):
    """The endpoint refused the parameter the structured mode rests on: the call is issued again on the same candidate,
    once, in the next mode (MDL-04, MDL-11)."""


@dataclass
class RoleUsage:
    calls: int = 0
    input_tokens: int = 0
    output_tokens: int = 0
    cost_usd: float = 0.0


@dataclass
class Shared:
    """What the routers of every session on one device share: the local queue (MDL-22), the learned feature
    rejections (MDL-04) and usage and cost per role (MDL-22)."""

    scheduler: Scheduler | None = None
    rejections: FeatureRejections = field(default_factory=FeatureRejections)
    usage: dict[str, RoleUsage] = field(default_factory=dict)


@dataclass(frozen=True)
class SlotResolution:
    """An auxiliary slot's candidates (MDL-06). Empty means the feature is off, with its notice (PRM-17)."""

    slot: str
    tier: str
    candidates: list[Candidate]
    dropped: dict[str, str]
    inherited: bool  # the slot runs on the Coach's local model
    notice: str | None

    @property
    def disabled(self) -> bool:
        return not self.candidates


def strip_for(messages: Sequence[Message], kind: str) -> list[Message]:
    """Messages as another provider may see them: no reasoning, only that provider's own extras (MDL-02)."""
    return [replace(m, reasoning=None, extras={kind: m.extras[kind]} if kind in m.extras else {}) for m in messages]


@dataclass
class Router:
    config: RouterConfig
    settings: Settings
    providers: dict[str, Any]  # "local", and cloud providers by name (or "cloud")
    cloud_on: Callable[[str], bool] = lambda role: False  # adult setting, connectivity and budget (from S2)
    hooks: Hooks = field(default_factory=Hooks)
    sleep: Callable[[float], Awaitable[None]] = asyncio.sleep
    rng: random.Random = field(default_factory=random.Random)
    overrides: dict[str, dict[str, Any]] = field(default_factory=dict)  # model -> capability override (layer 1)
    recommended: dict[str, dict[str, Any]] = field(default_factory=dict)  # model -> recommendations entry (layer 2)
    memory_context_cap: int | None = None
    disabled: set[str] = field(default_factory=set)
    safety_blocked: Callable[[Candidate], bool] | None = None
    cloud_offline: bool = False
    cloud_paused: bool = False
    # low, mid or high: bounds local thinking (MDL-12) and sets the slots (MDL-22); content_only: no on-device model
    # serves (PLT-29), only a school server or the online tier
    hardware_class: str | None = None
    # PLT-22: while the app saves power, the smaller model, the Coach's output cap and no thinking (None: not saving)
    power: Callable[[], PowerPlan | None] | None = None
    shared: Shared = field(default_factory=Shared)
    # The connectivity layer's state (ARC-08): offline or limited skips cloud candidates without waiting (MDL-24).
    network: Callable[[], str] | None = None
    # Probe now and return the new state: a cloud call that stalls asks whether the network is still there (W3).
    recheck: Callable[[], Awaitable[str]] | None = None
    # Probe soon while offline: a call the cloud would have served, so the cloud returns once the network does.
    recheck_soon: Callable[[], None] | None = None
    # Cloud cost budgets and the term's allowance (MDL-29, BIZ-07): over a limit, cloud candidates are skipped.
    budget: Any = None
    # UXS-03, MDL-05: the student chose "Use the faster tutor" on this computer: a role of the strong tier is served
    # by the fast tier's candidates (the Reflector, which runs in the background, keeps its own).
    prefer_fast: Callable[[], bool] | None = None
    # SPEC §19.4 (W8 phase 3): `tutor_memory_online` effective for the open profile. The Reflector is in the cloud's
    # roles_allowed, and is barred from the cloud whenever this says no (MDL-25 stays the default).
    memory_online: Callable[[], bool] = lambda: False
    _turns: int = 0
    _cloud_text: bool = False

    # --- candidates (MDL-05) ------------------------------------------------------------------------------

    def provider_for(self, candidate: Candidate) -> Any:
        if candidate.locality == "local":
            return self.providers["local"]
        return self.providers.get(candidate.provider) or self.providers.get("cloud")

    async def capabilities(self, candidate: Candidate) -> ModelCapabilities:
        reported = await self.provider_for(candidate).capabilities(candidate.model)
        return layered(
            reported,
            self.recommended.get(candidate.model),
            self.overrides.get(candidate.model),
            self.memory_context_cap,
        )

    def _cloud_block(self, role: str) -> str | None:
        """Why a cloud candidate cannot serve this role now (MDL-05, MDL-24, MDL-29), or None."""
        cloud = self.config.cloud
        if not cloud.get("enabled") or role not in cloud.get("roles_allowed", []):
            return "cloud tier off"
        if role == "reflector" and not self.memory_online():
            return "cloud tier off"  # MDL-25 unless tutor_memory_online is effective (SPEC §19.4)
        if not self.cloud_on(role):
            return "cloud tier off"
        online = self.network is None or self.network() in USABLE_NETWORK
        if self.cloud_offline and self.network is not None and online:
            self.cloud_offline = False  # a probe found the network again after the failure that marked it (W3)
        if self.cloud_offline or not online:
            if self.recheck_soon is not None:
                self.recheck_soon()
            return "offline"
        if self.cloud_paused:
            return "paused"
        if self.budget is not None and not self.budget.allows():
            return "over budget"
        return None

    def _cloud_allowed(self, role: str) -> bool:
        return self._cloud_block(role) is None

    def serves_on_device(self) -> bool:
        """A model on this computer (or a school server) exists to serve a call at all, whatever the cloud decides:
        there is a local provider, and the class is not the content-only one that runs no model here (PLT-29)."""
        local = self.providers.get("local")
        if local is None:
            return False
        return not (self.hardware_class == "content_only" and getattr(local, "kind", "local") == "local")

    def serves_locally(self, role: str) -> bool:
        """The role's call would go to the device's own runtime now: there is one, and no cloud candidate may serve."""
        return "local" in self.providers and not self._cloud_allowed(role)

    async def _drop_reason(
        self, candidate: Candidate, role: str, requires: dict[str, Any], installed: list[list[str]]
    ) -> str | None:
        """Why a candidate cannot serve this role now, or None. `installed` caches the local model list per call."""
        if self.safety_blocked is not None and self.safety_blocked(candidate):
            return "safety restriction"
        if str(candidate) in self.disabled:
            return "disabled"
        if candidate.locality == "cloud":
            blocked = self._cloud_block(role)
            if blocked is not None:
                return blocked
            if self.provider_for(candidate) is None:
                return "cloud tier off"
        else:
            local = self.providers.get("local")
            if local is None:
                return "no local runtime"
            if self.hardware_class == "content_only" and getattr(local, "kind", "local") == "local":
                return "below the minimum laptop"  # PLT-29: no model runs on this computer (a school server may)
            if not installed:
                installed.append(await local.installed())
            if candidate.model not in installed[0]:
                return "not installed"
        try:
            caps = await self.capabilities(candidate)
        except ProviderError as error:
            return f"unavailable ({error.reason})"
        lacking = [k for k in ("tools", "vision") if requires.get(k) and not getattr(caps, k)]
        if requires.get("structured") and not (caps.json_schema or caps.grammar or caps.tools):
            lacking.append("structured")
        if requires.get("min_context") and caps.context_for_agents < requires["min_context"]:
            lacking.append("min_context")
        return "lacks " + ", ".join(lacking) if lacking else None

    async def _filter(
        self, order: Sequence[Candidate], role: str, requires: dict[str, Any]
    ) -> tuple[list[Candidate], dict[str, str]]:
        kept: list[Candidate] = []
        dropped: dict[str, str] = {}
        installed: list[list[str]] = []
        for candidate in dict.fromkeys(order):
            reason = await self._drop_reason(candidate, role, requires, installed)
            if reason is None:
                kept.append(candidate)
            else:
                dropped[str(candidate)] = reason
        return kept, dropped

    def tier_of(self, role: str) -> str:
        """The tier a role's call is served from now (MDL-05): its configured tier, or `fast` in place of `strong`
        once the student chose the faster tutor (UXS-03), except for a background role. An auxiliary slot reports
        the Coach's tier, which it inherits."""
        data = self.config.data["roles"]
        entry = data.get(role) or data.get("coach") or {}
        tier = str(entry.get("tier", "fast"))
        if tier == "strong" and not entry.get("background") and self.prefer_fast is not None and self.prefer_fast():
            return "fast"
        return tier

    async def candidates(self, role: str) -> tuple[str, list[Candidate], dict[str, str]]:
        if role in SLOTS and role not in self.config.data["roles"]:
            slot = await self.resolve_slot(role)
            return slot.tier, slot.candidates, slot.dropped
        entry = self.config.role(role)
        tier = self.tier_of(role)
        kept, dropped = await self._filter(self.config.tiers[tier], role, entry.get("requires", {}))
        if not kept and tier != entry.get("tier", "fast"):  # no fast model can do this role: its own tier serves it
            tier = entry.get("tier", "fast")
            kept, dropped = await self._filter(self.config.tiers[tier], role, entry.get("requires", {}))
        prefer = entry.get("prefer", "cloud" if role == "solver" else "local")
        kept.sort(key=lambda c: c.locality != prefer)  # stable: tier order within each locality
        offline = [c for c in self.config.tiers[tier] if dropped.get(str(c)) == "offline"]
        if prefer == "cloud" and offline and self.hooks.on_egress is not None:
            self.hooks.on_egress("blocked", offline[0])  # the cloud would have answered: say so (API-15)
        saving = self.power() if self.power is not None else None
        if saving is not None and saving.model is not None and "local" in self.providers:
            # PLT-22: the next smaller installed model takes the local candidates' place.
            if saving.model in await self.providers["local"].installed():
                smaller = Candidate("local", "local", saving.model)
                kept = list(dict.fromkeys(smaller if c.locality == "local" else c for c in kept))
        return tier, kept, dropped

    # --- auxiliary slots (MDL-06) and tool profiles (TOL-17) ---------------------------------------------------

    async def main_local_model(self) -> Candidate | None:
        """The Coach's local model: the first local candidate of its tier that can run now."""
        tier = self.config.role("coach").get("tier", "fast")
        local = [c for c in self.config.tiers[tier] if c.locality == "local"]
        kept, _ = await self._filter(local, "coach", {})
        return kept[0] if kept else None

    async def resolve_slot(self, name: str) -> SlotResolution:
        """A slot set to a model or tier uses it; an unset slot inherits the Coach's local model. A slot that needs a
        capability (vision), or that says `inherit: true` and names a model too, tries the inherited model first and
        its configured model or tier as the fallback (so a computer with no local model still has one); when none
        has the capability the feature is off, with its notice."""
        entry = self.config.role(name)
        requires = {**SLOT_REQUIRES.get(name, {}), **(entry.get("requires") or {})}
        configured = [Candidate.parse(entry["model"])] if entry.get("model") else []
        if entry.get("tier"):
            configured += sorted(self.config.tiers[entry["tier"]], key=lambda c: c.locality != "local")
        main = await self.main_local_model() if entry.get("inherit", True) else None
        fallback = entry.get("inherit") is True and bool(configured)  # local first, the configured one after it
        inherited = [main] if main is not None and (requires or not configured or fallback) else []
        order = inherited + configured
        kept, dropped = await self._filter(order, name, requires)
        tier = entry.get("tier") or self.config.role("coach").get("tier", "fast")
        notice = SLOT_OFF_NOTICES.get(name) if not kept else None
        return SlotResolution(name, tier, kept, dropped, bool(kept) and kept[0] in inherited, notice)

    async def profile(self, role: str) -> Profile:
        """The tool profile the role's tier and first candidate select (TOL-17)."""
        return (await self.prompt_selection(role))[0]

    async def prompt_selection(self, role: str) -> tuple[Profile, str | None]:
        """Profile and reported family from the same eligible candidate selection."""
        tier, kept, _ = await self.candidates(role)
        if not kept:
            return tool_profile(tier, "local"), None
        candidate = kept[0]
        profile = tool_profile(tier, candidate.locality, candidate in self.config.tiers.get("fast", ()))
        return profile, (await self.capabilities(candidate)).family

    # --- the call ----------------------------------------------------------------------------------------

    def _status(self, phase: str, **info: Any) -> None:
        if self.hooks.on_status:
            self.hooks.on_status(phase, info)

    def _scheduler(self) -> Scheduler:
        """The device's local queue, made on first use from the configured slots for the class and the roles'
        concurrency limits (MDL-22)."""
        if self.shared.scheduler is None:
            slots = (self.config.local.get("slots") or {}).get(self.hardware_class or "mid") or 1
            limits = {r: int(e["concurrency"]) for r, e in self.config.data["roles"].items() if e.get("concurrency")}
            self.shared.scheduler = Scheduler(int(slots), self.settings.router_preempt_start_ms / 1000, limits)
        return self.shared.scheduler

    async def complete(
        self,
        role: str,
        messages: Sequence[Message],
        tools: Sequence[ToolSchema] = (),
        *,
        output: tuple[str, dict[str, Any], Draft202012Validator] | None = None,
        first_token_budget_s: float | None = None,
        on_text: Callable[[str], None] | None = None,
        prepare: Callable[[Candidate, ModelCapabilities, list[Message]], Awaitable[list[Message] | Prepared]]
        | None = None,
        allow: Callable[[Candidate], bool] | None = None,
        request_guard: Callable[[Candidate], None] | None = None,
        **settings: Any,
    ) -> RouterResult:
        """`output` is (schema name, decode schema, validator) for roles with an output schema. `prepare` rebuilds the
        request for every candidate, including the first local one (what reaches a cloud model is
        the engine's to decide, MEM-13, MDL-25); it may also narrow the tools offered to that candidate. `allow`
        narrows this call's candidates (a photo reaches a cloud candidate only when the engine allows it, MDL-23).
        `request_guard` checks live ownership/consent immediately before every provider attempt, after queue waits
        and retry backoff. It may raise to abandon the call; it is never forwarded to a provider."""
        if current_scope() is None:
            raise RouterOutsideEngineContextError(f"router.complete({role!r}) called outside an engine turn or job")
        tier, candidates, dropped = await self.candidates(role)
        if allow is not None:
            dropped = {**dropped, **{str(c): "not allowed for this call" for c in candidates if not allow(c)}}
            candidates = [c for c in candidates if allow(c)]
        entry = self.config.role(role)
        record = CallRecord(role, tier, [str(c) for c in candidates], dropped, entry.get("prompt_mode"))
        self._turns += 1
        local_only = False
        last_reason = "model_not_found"
        previous: str | None = None
        offered = list(tools)
        try:
            for candidate in candidates:
                if local_only and candidate.locality != "local":
                    continue
                if previous is not None and str(candidate) == previous:
                    continue
                egress = self.hooks.on_egress if candidate.locality != "local" else None
                try:
                    if egress is not None:
                        egress("started", candidate)
                    if prepare is not None:
                        # A fallback candidate may differ in family or context, and a cloud one gets only what may
                        # leave the device: the engine rebuilds (AGT-05).
                        prepared = await prepare(candidate, await self.capabilities(candidate), list(messages))
                        if isinstance(prepared, Prepared):
                            messages, tools = prepared.messages, offered if prepared.tools is None else prepared.tools
                            # A candidate's own decode schema only replaces the call's: a call without one (a tool
                            # round) stays free text, or a reply that is a tool call would be refused as bad JSON
                            # and the call never reach its executor (MEM-13: a cloud recall must reach the refusal).
                            decode = (prepared.output or output) if output is not None else None
                        else:
                            messages, tools, decode = prepared, offered, output
                    else:
                        decode = output
                    for issue in (1, 2):
                        try:
                            return await self._on_candidate(
                                role,
                                tier,
                                entry,
                                candidate,
                                list(messages),
                                list(tools),
                                decode,
                                first_token_budget_s,
                                on_text,
                                settings,
                                record,
                                previous,
                                request_guard,
                            )
                        except _Reissue as reissue:
                            record.events.append({"event": "reissue", "param": str(reissue)})
                            if issue == 2:
                                self._fall_back("format_error")
                except _Fallback as fallback:
                    if candidate.locality == "cloud" and self.hooks.on_cloud_fallback is not None:
                        self.hooks.on_cloud_fallback(self._cloud_text)
                    last_reason = fallback.reason
                    local_only = local_only or fallback.local_only
                    record.events.append({"event": "fallback", "from": str(candidate), "reason": fallback.reason})
                    self._status("retrying", reason=fallback.reason, fallback=True)
                    previous = str(candidate)
                finally:
                    if egress is not None:
                        egress("ended", candidate)
            raise RouterError(last_reason if candidates else "model_not_found", record)
        except RouterError as error:
            error.record = error.record or record
            raise
        finally:
            usage = self.shared.usage.setdefault(role, RoleUsage())  # every attempt is accounted (MDL-22)
            for attempt in record.attempts:
                usage.calls += 1
                usage.input_tokens += attempt["tokens"]["input"]
                usage.output_tokens += attempt["tokens"]["output"]
                usage.cost_usd += attempt["cost_usd"]
                if self.budget is not None and attempt.get("provider_kind") == "cloud":  # metered on the device
                    self.budget.record(attempt["cost_usd"], attempt["tokens"]["input"] + attempt["tokens"]["output"])
            if self.hooks.on_trace:
                self.hooks.on_trace(record)

    async def _on_candidate(
        self,
        role: str,
        tier: str,
        entry: dict[str, Any],
        candidate: Candidate,
        messages: list[Message],
        tools: list[ToolSchema],
        output: tuple[str, dict[str, Any], Draft202012Validator] | None,
        first_token_budget_s: float | None,
        on_text: Callable[[str], None] | None,
        settings: dict[str, Any],
        record: CallRecord,
        fallback_from: str | None,
        request_guard: Callable[[Candidate], None] | None,
    ) -> RouterResult:
        provider = self.provider_for(candidate)
        caps = await self.capabilities(candidate)
        name = str(candidate)
        self._cloud_text = False
        # The role's effort (or the caller's) mapped onto this model's control and the class's budget (MDL-12).
        requested = as_level(settings.get("effort") or entry.get("reasoning"))
        saving = self.power() if self.power is not None else None
        if saving is not None:
            requested = "none"  # PLT-22: no thinking while saving power
        effort = plan_effort(requested, caps, self.hardware_class, self.settings)
        record.efforts.append({**effort.record(), "candidate": name})
        # `role` travels with the settings for providers that answer per role (the scripted provider); OpenAI-style
        # providers send only the request fields they know.
        call_settings: dict[str, Any] = {**effort.settings(), "role": role}
        if entry.get("temperature") is not None:
            call_settings["temperature"] = entry["temperature"]
        call_settings.update({k: v for k, v in settings.items() if k not in ("effort", "stream_override")})
        cloud_cap = call_settings.pop("cloud_max_tokens", None)
        if caps.locality == "cloud" and call_settings.get("max_tokens"):
            if cloud_cap:
                call_settings["max_tokens"] = int(cloud_cap)  # the role's cloud column, whatever this computer's class
            if caps.reasoning == "effort":
                # A cloud model that always reasons (Muse Spark, JM-D9) spends output tokens on reasoning before its
                # reply, so the role's cap is for the reply and reasoning gets room on top, within the model's limit.
                headroom = REASONING_HEADROOM.get(str(call_settings.get("effort", "low")), REASONING_HEADROOM["low"])
                call_settings["max_tokens"] = min(int(call_settings["max_tokens"]) + headroom, caps.max_output)
        elif caps.reasoning == "effort" and call_settings.get("max_tokens"):
            capped = int(call_settings["max_tokens"]) + LOCAL_REASONING_HEADROOM
            call_settings["max_tokens"] = min(capped, caps.max_output) if caps.max_output else capped
        if saving is not None and role == "coach":  # PLT-22: shorter answers
            cap = saving.max_output_tokens
            call_settings["max_tokens"] = min(int(call_settings.get("max_tokens") or cap), cap)
        if self.shared.rejections.for_candidate(name):
            call_settings["param_fixes"] = self.shared.rejections.for_candidate(name)  # learned (MDL-04)
        mode = "none"
        offered = {t.name for t in tools}
        if output:
            mode = pick_mode(caps, self.shared.rejections.dropped(name))
            extra, extra_tools = request_settings(mode, output[1], output[0])
            call_settings.update(extra)
            tools = tools + extra_tools
            if mode == "prompt":
                messages = [prompt_instruction(output[1]), *messages]
        if output and mode == "tool":
            offered |= {OUTPUT_TOOL}
        state = strip_for(messages, provider.kind)
        retries: Counter[str] = Counter()
        bad: Counter[str] = Counter()
        corrected = False
        started_call = time.monotonic()
        attempt = sum(1 for a in record.attempts if a["model_requested"] == candidate.model[:128])  # after a reissue
        scheduler = self._scheduler()
        priority = int(entry.get("priority", DEFAULT_PRIORITY))
        stream = bool(entry.get("stream"))
        if isinstance(settings.get("stream_override"), bool):
            stream = settings["stream_override"]

        async def dispatch() -> AssistantTurn:
            if request_guard is not None:
                request_guard(candidate)
            return await self._call(provider, candidate.model, state, tools, call_settings, stream, on_text)

        while True:
            attempt += 1
            started = time.monotonic()
            queue_ms: int | None = None
            self._status("waiting_first_token")
            try:
                # Local calls queue for the runtime's slots by priority; every call counts against its role's limit
                # (MDL-22). Backoff waits happen outside the queue.
                turn, queue_ms = await scheduler.run(
                    role,
                    priority,
                    bool(entry.get("background")),
                    candidate.locality == "local",
                    dispatch,
                )
            except ProviderError as error:
                record.attempts.append(
                    attempt_record(
                        role=role,
                        tier=tier,
                        requested=candidate.model,
                        provider_kind=provider.kind,
                        attempt=attempt,
                        total_ms=int((time.monotonic() - started) * 1000),
                        error=error.reason,
                        fallback_from=fallback_from,
                        effort_requested=effort.requested,
                        effort_effective=effort.effective,
                        queue_ms=queue_ms,
                        structured_mode=mode,
                        correction_retry=corrected,
                    )
                )
                # What refused the call, by code (never text): read back by a caller's diagnosis (§19 W8 fix).
                record.refusals.append({"candidate": str(candidate)[:128], "reason": error.reason,
                                        "code": getattr(error, "code", None), "status": error.status})  # fmt: skip
                state = await self._recover(
                    error, candidate, role, state, call_settings, retries, record, started_call, first_token_budget_s
                )
                continue
            normal = normalise(turn, offered, f"call_{self._turns}_{attempt}")
            total_ms = int((time.monotonic() - started) * 1000)
            if normal.empty or normal.unparsed_tool_call:
                kind = "empty" if normal.empty else "unparsed_tool_call"
                bad[kind] += 1
                record.attempts.append(
                    attempt_record(
                        role=role,
                        tier=tier,
                        requested=candidate.model,
                        provider_kind=provider.kind,
                        attempt=attempt,
                        total_ms=total_ms,
                        turn=turn,
                        error=kind,
                        fallback_from=fallback_from,
                        effort_requested=effort.requested,
                        effort_effective=effort.effective,
                        queue_ms=queue_ms,
                        structured_mode=mode,
                        correction_retry=corrected,
                    )
                )
                if bad[kind] >= 2:
                    self._fall_back(f"{kind}_twice")
                record.events.append({"event": "retry", "reason": kind})
                continue
            result_turn = normal.turn
            structured: dict[str, Any] | None = None
            errors: list[str] = []
            valid: bool | None = None
            if output:
                text = result_turn.text
                if mode == "tool":
                    emitted = next((c for c in result_turn.tool_calls if c.name == OUTPUT_TOOL), None)
                    if emitted is not None:
                        text = json.dumps(emitted.arguments)
                        result_turn = replace(
                            result_turn, tool_calls=[c for c in result_turn.tool_calls if c.name != OUTPUT_TOOL]
                        )
                checked = check(text, output[2])
                valid = not checked.errors
                structured, errors = checked.value, checked.errors
                record.attempts.append(
                    attempt_record(
                        role=role,
                        tier=tier,
                        requested=candidate.model,
                        provider_kind=provider.kind,
                        attempt=attempt,
                        total_ms=total_ms,
                        turn=turn,
                        fallback_from=fallback_from,
                        effort_requested=effort.requested,
                        effort_effective=effort.effective,
                        queue_ms=queue_ms,
                        structured_mode=mode,
                        schema_valid=valid,
                        correction_retry=corrected,
                    )
                )
                # The engine owns length recovery for these roles (a Coach continuation or one Solver replan).
                # A schema correction repeats the same too-long object at the same token cap before that recovery,
                # doubling the wait. Return the invalid partial to the engine; it is never a verified output.
                truncated = role in {"coach", "solver"} and result_turn.finish_reason == "length"
                if not valid and not truncated:
                    if not corrected and entry.get("output_retries", 1) > 0:
                        corrected = True
                        state = state + correction(text, errors)
                        record.events.append({"event": "correction", "errors": len(errors)})
                        continue
                    if "schema_invalid_twice" in self.config.fallback["triggers"] and self._has_next(record, candidate):
                        self._fall_back("schema_invalid_twice")
                result_turn = replace(result_turn, structured=structured)
            else:
                record.attempts.append(
                    attempt_record(
                        role=role,
                        tier=tier,
                        requested=candidate.model,
                        provider_kind=provider.kind,
                        attempt=attempt,
                        total_ms=total_ms,
                        turn=turn,
                        fallback_from=fallback_from,
                        effort_requested=effort.requested,
                        effort_effective=effort.effective,
                        queue_ms=queue_ms,
                    )
                )
            return RouterResult(result_turn, structured, valid, errors, record)

    def _has_next(self, record: CallRecord, candidate: Candidate) -> bool:
        names = record.candidates
        return str(candidate) in names and names.index(str(candidate)) < len(names) - 1

    def _fall_back(self, trigger: str, local_only: bool = False) -> None:
        if trigger not in self.config.fallback["triggers"]:
            raise RouterError("bad_response" if trigger.endswith("_twice") else trigger)
        raise _Fallback(trigger, local_only)

    async def _call(
        self,
        provider: Any,
        model: str,
        messages: list[Message],
        tools: list[ToolSchema],
        settings: dict[str, Any],
        stream: bool,
        on_text: Callable[[str], None] | None,
    ) -> AssistantTurn:
        pending = self._request(provider, model, messages, tools, settings, stream, on_text)
        if getattr(provider, "kind", "local") != "cloud" or self.recheck is None:
            return await pending
        return await self._watched(pending)

    async def _watched(self, pending: Awaitable[AssistantTurn]) -> AssistantTurn:
        """A cloud call that has not finished after `CLOUD_STALL_CHECK_S` has the network probed (again each time);
        when the probe finds it gone the call is dropped as `network_unavailable`, which marks the cloud offline and
        serves the call locally (W3: Wi-Fi off mid-call must not wait out the request timeout)."""
        assert self.recheck is not None
        call = asyncio.ensure_future(pending)
        try:
            while True:
                done, _ = await asyncio.wait({call}, timeout=CLOUD_STALL_CHECK_S)
                if done:
                    return call.result()
                if await self.recheck() not in USABLE_NETWORK:
                    raise ProviderError("network_unavailable", "the network went while the cloud call was waiting")
        finally:
            if not call.done():
                call.cancel()
                await asyncio.gather(call, return_exceptions=True)

    async def _request(
        self,
        provider: Any,
        model: str,
        messages: list[Message],
        tools: list[ToolSchema],
        settings: dict[str, Any],
        stream: bool,
        on_text: Callable[[str], None] | None,
    ) -> AssistantTurn:
        budget = settings.get("first_token_timeout_s") if getattr(provider, "kind", "local") == "cloud" else None
        if not stream:
            try:
                async with asyncio.timeout(float(budget) if budget is not None else None):
                    turn: AssistantTurn = await provider.complete(model, messages, tools, **settings)
                    return turn
            except TimeoutError:
                raise ProviderError(
                    "timeout", "cloud short-response deadline exceeded", code="response_timeout"
                ) from None
        scrubber = ThinkScrubber()
        final: AssistantTurn | None = None
        # HTTP headers, keepalives and reasoning events are not the Coach's first content. Enforce the role's
        # deadline here as well as in the HTTP client so adapters cannot accidentally turn it into a 120s wait.
        first_content = asyncio.timeout(float(budget) if budget is not None else None)
        try:
            async with first_content:
                async for chunk in provider.stream(model, messages, tools, **settings):
                    if isinstance(chunk, TextDelta):
                        self._cloud_text = self._cloud_text or getattr(provider, "kind", "local") == "cloud"
                        visible, _ = scrubber.feed(chunk.text)
                        if visible:
                            first_content.reschedule(None)
                            if on_text:
                                on_text(visible)
                    elif isinstance(chunk, ReasoningDelta):
                        continue
                    elif isinstance(chunk, ToolCallDelta) and (chunk.name or chunk.arguments):
                        first_content.reschedule(None)
                    elif isinstance(chunk, FinalTurn):
                        final = chunk.turn
        except TimeoutError:
            raise ProviderError(
                "timeout", "cloud first-content deadline exceeded", code="first_token_timeout"
            ) from None
        if final is None:
            raise ProviderError("bad_response", "the stream ended without a final turn")
        return final

    async def _recover(
        self,
        error: ProviderError,
        candidate: Candidate,
        role: str,
        state: list[Message],
        settings: dict[str, Any],
        retries: Counter[str],
        record: CallRecord,
        started_call: float,
        first_token_budget_s: float | None,
    ) -> list[Message]:
        """Prepare a retry on the same model and return the message state to send, or raise `_Fallback` /
        `RouterError` when the reason's retries are exhausted (TABLE MDL-07a)."""
        reason = error.reason if error.reason in RECOVERY else "unknown"
        rule = RECOVERY[reason]
        if rule.first and retries[reason] == 0:
            self._first_action(rule.first, candidate)
        if retries[reason] < retries_for(reason, self.settings):
            prepared = await self._prepare(
                rule.before_retry,
                reason,
                retries[reason],
                error,
                candidate,
                state,
                settings,
                started_call,
                first_token_budget_s,
            )
            if prepared is not None:
                retries[reason] += 1
                record.events.append({"event": "retry", "reason": reason, "preparation": rule.before_retry})
                self._status("retrying", reason=reason)
                return prepared
        outcome = rule.on_exhausted
        if outcome == "fail":
            raise RouterError(reason, record)
        self._fall_back(reason, local_only=outcome.startswith("fallback_") and outcome != "fallback")
        raise AssertionError("unreachable")

    def _first_action(self, action: str, candidate: Candidate) -> None:
        if action == "mark_cloud_offline":
            self.cloud_offline = True
        elif action == "disable_candidate":
            self.disabled.add(str(candidate))
        elif action == "pause_cloud":
            self.cloud_paused = True

    async def _prepare(
        self,
        preparation: str,
        reason: str,
        retry_index: int,
        error: ProviderError,
        candidate: Candidate,
        state: list[Message],
        settings: dict[str, Any],
        started_call: float,
        first_token_budget_s: float | None,
    ) -> list[Message] | None:
        """The message state for the retry, or None when the preparation is not possible."""
        if preparation == "backoff":
            delay = Backoff(self.settings, self.rng).delay(retry_index, error.retry_after_s)
            elapsed = time.monotonic() - started_call
            if first_token_budget_s is not None and elapsed + delay > first_token_budget_s:
                return None  # waiting would break the role's first-token budget: fall back instead (MDL-08)
            self._status("retrying", reason=reason, delay_s=round(delay, 1))
            await self.sleep(delay)
            return state
        if preparation == "none":
            return state
        if preparation == "compact" and self.hooks.compact:
            return await self.hooks.compact(state)
        if preparation == "shrink" and self.hooks.shrink:
            return await self.hooks.shrink(state)
        if preparation == "param_fix":
            if error.param:  # exactly the named parameter, renamed or dropped, and remembered (MDL-11, MDL-04)
                self.shared.rejections.remember(str(candidate), error.param, error.replacement)
                if MODE_PARAM.get((settings.get("structured") or {}).get("mode", "")) == error.param:
                    raise _Reissue(error.param)  # the structured mode itself was refused: the next mode
                settings["param_fixes"] = self.shared.rejections.for_candidate(str(candidate))
                return state
            for key in ("seed", "top_p", "stop"):
                settings.pop(key, None)
            return state
        if preparation == "strip_keyword" and settings.get("structured", {}).get("mode") == "json_schema":
            settings["structured"] = {"mode": "json_object"}  # the constraint is still checked by validation
            return state
        if preparation == "restart_runtime" and self.hooks.restart_runtime and await self.hooks.restart_runtime():
            return state
        if (
            preparation == "lower_context"
            and self.hooks.lower_context
            and await self.hooks.lower_context(candidate.model)
        ):
            return state
        return None
