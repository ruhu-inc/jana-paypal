"""Risk classes and their policy (TOL-04, TABLE TOL-04a), applied before execution.

The engine derives policy only from metadata (TOL-03): `ui` tools are never callable by a model, `memory_write`
only by the Reflector or the engine, `egress` only for `python_run` behind the cloud tier (S3+), `external` not at
all before S5, and `compute` only for the session's subject and level."""

from __future__ import annotations

from typing import Any

from agent_core.tools.spec import SessionCtx, ToolSpec

MODEL_ROLES = frozenset({"coach", "diagnoser", "solver", "checker", "reflector", "auxiliary", "item_drafter",
                         "companion"})  # fmt: skip
# Names a model may use to try to hand work to another role. No tool by these names exists or ever will: the engine
# alone calls roles (AGT-03, AGT-13), so a call to one is refused as a policy matter, never repaired to a near name.
DELEGATION_NAMES = frozenset(
    "delegate delegate_task task handoff hand_off transfer_to_agent call_agent spawn_agent ask_agent run_agent "
    "subagent new_task".split()
)


def delegation_call(name: str) -> bool:
    """A tool call that asks for another role (AGT-14): refused with `policy_denied` before anything runs."""
    lowered = name.strip().lower().replace("-", "_")
    return lowered in DELEGATION_NAMES or lowered.startswith(("delegate_", "transfer_to_", "handoff_to_"))


def granted(role: str, spec: ToolSpec, grants: dict[str, list[str]]) -> bool:
    """Whether a role's grant in the register covers the tool (`compute:*` covers the toolset, `-name` excludes)."""
    rules = grants.get(role, [])
    if f"-{spec.name}" in rules:
        return False
    return spec.name in rules or f"{spec.toolset}:*" in rules


def denial(role: str, spec: ToolSpec, session: SessionCtx, grants: dict[str, list[str]]) -> str | None:
    """Why the call is refused (policy_denied), or None when it may run."""
    risk = spec.meta.risk
    if risk == "ui" and role in MODEL_ROLES:
        return f"{spec.name} changes what the student sees; only the engine applies it from the tutor turn"
    if not granted(role, spec, grants):
        return f"the {role} role is not granted {spec.name}"
    # SPEC §19.3.3 (W8): the companion's `make_aid` writes a Study job to the learner's own workspace, never a note;
    # the companion may run the write tools of its own `study` toolset only (its grant still names each one).
    study_write = role == "companion" and spec.toolset == "study"
    if risk == "memory_write" and role not in ("reflector", "engine") and not study_write:
        return "only the Reflector or the engine writes memory"
    if risk == "egress" and (spec.name != "python_run" or "cloud_tier" not in session.enabled_online_features):
        return "this tool sends data off the device and its online feature is off"
    if risk == "external":
        return "external tools are not available"
    if risk == "compute" and not _subject_and_level_ok(spec, session):
        return f"{spec.name} is not offered for this subject and level"
    return None


def _subject_and_level_ok(spec: ToolSpec, session: SessionCtx) -> bool:
    subjects: Any = spec.meta.subjects
    return (session.subject in subjects or "general" in subjects) and session.level in spec.meta.levels
