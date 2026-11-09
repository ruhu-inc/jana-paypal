"""Turn-scoped safety enforcement (OPS-10, OPS-11, OPS-28, OPS-29)."""

from __future__ import annotations

from collections.abc import Callable, Mapping
from dataclasses import dataclass, replace
from typing import TYPE_CHECKING, Any

from agent_core.assets.skills import SkillSet
from agent_core.config.operations import DevicePolicy, TargetDecision, version
from agent_core.store.pack_index import PackIndex

if TYPE_CHECKING:
    from agent_core.engine.turn import Engine


@dataclass
class RuntimeSafety:
    engine: Engine
    policy: DevicePolicy
    installed: Callable[[], Mapping[str, str]]
    base_skills: SkillSet
    previous_skills: SkillSet | None
    decider: Callable[[str, str, str | None], TargetDecision]
    policy_source: Callable[[], DevicePolicy] | None = None
    local_models: Callable[[], set[str]] | None = None
    forced_hardware: tuple[str, str | None] | None = None

    def blocked(self, kind: str, target: str, target_version: str | None = None) -> bool:
        return self.decider(kind, target, target_version).action not in {"allow", "advise_update"}

    def begin(self) -> None:
        if self.policy_source is not None:
            self.policy = self.policy_source()
        self.policy.turn_boundary(self.installed())
        self.decider = self.policy.frozen_decider()
        models = self.local_models() if self.local_models is not None else set()
        if models and all(self.blocked("model_pack", model) for model in models):
            if self.forced_hardware is None:
                self.forced_hardware = (self.engine.state.hardware_class, self.engine.deps.router.hardware_class)
            self.engine.state.hardware_class = self.engine.deps.router.hardware_class = "content_only"
        elif self.forced_hardware is not None:
            self.engine.state.hardware_class, self.engine.deps.router.hardware_class = self.forced_hardware
            self.forced_hardware = None
        selected = {}
        for name, skill in self.base_skills.skills.items():
            action = self.decider("skill", name, skill.version).action
            if action == "allow":
                selected[name] = skill
            elif action == "pin_previous" and self.previous_skills is not None:
                previous = self.previous_skills.skills.get(name)
                if (
                    previous is not None
                    and previous.status == "reviewed"
                    and version(previous.version) < version(skill.version)
                ):
                    selected[name] = previous
        skills = SkillSet(selected, self.base_skills.statuses)
        if skills.manifest() != self.engine.deps.assets.skills.manifest():
            self.engine.skill_loaders.clear()
        self.engine.deps.assets = replace(self.engine.deps.assets, skills=skills)


def install_operations(
    engine: Engine,
    policy: DevicePolicy,
    installed: Callable[[], Mapping[str, str]],
    *,
    previous_skills: SkillSet | None = None,
    policy_source: Callable[[], DevicePolicy] | None = None,
    local_models: Callable[[], set[str]] | None = None,
) -> RuntimeSafety:
    runtime = RuntimeSafety(
        engine,
        policy,
        installed,
        engine.deps.assets.skills,
        previous_skills,
        policy.frozen_decider(),
        policy_source,
        local_models,
    )
    prior = engine.deps.before_turn

    def boundary() -> None:
        if prior is not None:
            prior()
        runtime.begin()

    engine.deps.before_turn = boundary
    engine.deps.executor.safety_blocked = lambda name: runtime.blocked("tool", name)
    engine.deps.router.safety_blocked = lambda c: (
        runtime.blocked("router_candidate", str(c))
        or runtime.blocked("model_pack", c.model)
        or (c.locality == "cloud" and runtime.blocked("online_feature", "cloud_tier"))
    )
    engine.deps.visual_allowed = lambda request: (
        not runtime.blocked("visual_kind", str(request.get("type", "")))
        and not runtime.blocked("diagram", str(request.get("ref", "")))
    )
    return runtime


def presented_content(content: PackIndex, policy: DevicePolicy) -> PackIndex:
    kinds = {"items": "item", "lessons": "lesson", "worked_examples": "worked_example"}
    records = {
        table: {
            key: value for key, value in rows.items() if policy.decision(kinds.get(table, ""), key).action == "allow"
        }
        for table, rows in content.records.items()
    }
    visuals: dict[str, dict[str, Any]] = {}
    for subject, sources in content.visuals.items():
        diagrams = {
            key: value
            for key, value in sources.get("diagrams", {}).items()
            if policy.decision("diagram", key).action == "allow"
        }
        visuals[subject] = {**sources, "diagrams": diagrams}
    return replace(content, records=records, visuals=visuals)
