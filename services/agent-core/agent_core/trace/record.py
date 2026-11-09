"""The per-turn trace record (OBS-01 to OBS-06, OBS-14), assembled as the turn runs and committed at `turn.end`.

Metadata only by default (capture `hashes`, OBS-07): prompts, completions, tool arguments and results appear as sha256,
never as text. Phase timings are offsets from the turn start on a monotonic clock, so a system clock change never
distorts a duration; wall-clock time is recorded once per turn for display."""

from __future__ import annotations

import datetime
import hashlib
import json
import time
from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Any

from agent_core.contracts import errors as contract_errors
from agent_core.trace.ids import new_ulid

CONTRACT = "trace/turn-trace"
TRACE_VERSION = "1.3"  # INP-33: closed capture metadata; 1.2 adds speech cues.
REDACTION_POLICY_VERSION = "1.0"
MAX_DELEGATIONS = 16  # `delegations` of the turn-trace contract
MAX_POLICY_DECISIONS = 64  # the turn-trace contract's `policy_decisions` maxItems (LRN-15, trace 1.1)
REPAIR_CLASSES = (
    ("id:", "unique_id"),
    ("name:", "name_repair"),
    ("json:", "json_repair"),
    ("coerce:", "type_coercion"),
    ("alias:", "normalize"),
    ("salvaged", "salvaged_from_text"),
)


def sha256_of(value: Any) -> str:
    text = value if isinstance(value, str) else json.dumps(value, sort_keys=True, separators=(",", ":"), default=str)
    return hashlib.sha256(text.encode()).hexdigest()


def repair_classes(repairs: list[str]) -> list[str]:
    out = []
    for repair in repairs:
        for prefix, name in REPAIR_CLASSES:
            if repair.startswith(prefix) and name not in out:
                out.append(name)
    return out


@dataclass
class TraceBuilder:
    session_ref: str
    profile_ref: str
    kind: str = "turn"
    turn_id: str | None = None
    mode: str = "guided"
    subject: str | None = None
    level: str | None = None
    build: dict[str, Any] = field(default_factory=dict)
    assets: dict[str, Any] = field(default_factory=dict)
    capture_mode: str = "hashes"
    # The connectivity layer's state when the trace began (ARC-08), read by the engine; "off" is "no layer to read".
    connectivity: str = "off"
    monotonic: Callable[[], float] = time.monotonic
    wall: Callable[[], datetime.datetime] = lambda: datetime.datetime.now(datetime.UTC)
    trace_id: str = field(default_factory=new_ulid)
    prompts: list[dict[str, Any]] = field(default_factory=list)
    skills: list[dict[str, Any]] = field(default_factory=list)
    model_calls: list[dict[str, Any]] = field(default_factory=list)
    tool_calls: list[dict[str, Any]] = field(default_factory=list)
    gates: list[dict[str, Any]] = field(default_factory=list)
    guards: list[dict[str, Any]] = field(default_factory=list)
    phases: list[dict[str, Any]] = field(default_factory=list)
    delegations: list[dict[str, Any]] = field(default_factory=list)
    scan_hits: list[dict[str, Any]] = field(default_factory=list)  # PRM-18, OBS-06: pattern id and source, no text
    # LRN-15: each adaptation policy's decision this turn, with its inputs and output (numbers, ids and flags only).
    policy_decisions: list[dict[str, Any]] = field(default_factory=list)
    status: str = "running"
    compaction: dict[str, Any] | None = None
    degraded: bool = False
    degraded_reason: str | None = None
    _start_wall: datetime.datetime | None = None
    _start: float = 0.0

    def __post_init__(self) -> None:
        self._start_wall = self.wall()
        self._start = self.monotonic()

    def offset_ms(self) -> int:
        return max(0, int((self.monotonic() - self._start) * 1000))

    # --- recording ------------------------------------------------------------------------------------------

    def phase(self, name: str, iteration: int, started_offset_ms: int, verdict: str | None = None) -> None:
        entry: dict[str, Any] = {
            "phase": name,
            "iteration": iteration,
            "start_offset_ms": started_offset_ms,
            "duration_ms": max(0, self.offset_ms() - started_offset_ms),
        }
        if verdict:
            entry["verdict"] = verdict
        self.phases.append(entry)

    def prompt(
        self, request_id: str, role: str, fingerprints: dict[str, Any], prompt_mode: str, tool_count: int
    ) -> None:
        self.prompts.append(
            {
                "request_id": request_id,
                "role": role,
                "prompt_mode": prompt_mode,
                "tiers": [
                    {
                        "tier": t["tier"],
                        "name": t["name"],
                        "sha256": t["sha256"],
                        "bytes": t["bytes"],
                        "tokens_est": t["tokens"],
                    }
                    for t in fingerprints["tiers"]
                ],
                "tool_schema_sha256": fingerprints["tools_sha256"],
                "tool_count": tool_count,
            }
        )

    def skill(self, delivered: dict[str, Any]) -> None:
        self.skills.append(
            {
                "name": delivered["name"],
                "kind": delivered["kind"],
                "version": delivered["version"],
                "sha256": delivered["sha256"],
                "delivery": delivered["route"],
                "receipt": bool(delivered.get("receipt", False)),
            }
        )

    def tool_call(self, entry: dict[str, Any]) -> None:
        self.tool_calls.append(entry)

    def guard(self, guard: str, action: str) -> None:
        for held in self.guards:
            if held["guard"] == guard and held["action"] == action:
                held["count"] += 1
                return
        self.guards.append({"guard": guard, "action": action, "count": 1})

    def policy_decision(self, entry: dict[str, Any]) -> None:
        """One `policy_decision` (LRN-15): `{"policy", "inputs", "output"}` as `learner.policies.Decision.entry`."""
        if len(self.policy_decisions) < MAX_POLICY_DECISIONS:
            self.policy_decisions.append(entry)

    def delegation(self, role: str, outcome: str, stop_reason: str, duration_ms: int) -> None:
        if len(self.delegations) >= MAX_DELEGATIONS:  # the trace contract's limit; the engine's logs keep the rest
            return
        self.delegations.append(
            {"role": role, "outcome": outcome, "stop_reason": stop_reason, "duration_ms": duration_ms}
        )

    # --- commit ---------------------------------------------------------------------------------------------

    def record(self) -> dict[str, Any]:
        assert self._start_wall is not None
        ended = self._start_wall + datetime.timedelta(milliseconds=self.offset_ms())
        out: dict[str, Any] = {
            "trace_version": TRACE_VERSION,
            "trace_id": self.trace_id,
            "kind": self.kind,
            "session_ref": self.session_ref,
            "profile_ref": self.profile_ref,
            "started_at": self._start_wall.isoformat().replace("+00:00", "Z"),
            "ended_at": ended.isoformat().replace("+00:00", "Z"),
            "duration_ms": self.offset_ms(),
            "status": self.status,
            "mode": self.mode,
            "degraded": self.degraded,
            "degraded_reason": self.degraded_reason,
            "build": self.build,
            "assets": self.assets,
            "prompts": self.prompts,
            "skills": self.skills,
            "model_calls": self.model_calls,
            "tool_calls": self.tool_calls,
            "gates": self.gates,
            "guards": self.guards,
            "phases": self.phases,
            "delegations": self.delegations,
            "connectivity": {"state_at_start": self.connectivity, "features_used": self.features_used()},
            "capture": {"mode": self.capture_mode, "redaction_policy_version": REDACTION_POLICY_VERSION},
        }
        if self.turn_id:
            out["turn_id"] = self.turn_id.split("_", 1)[-1]
        if self.compaction:
            out["compaction"] = self.compaction
        if self.scan_hits:
            out["scan_hits"] = self.scan_hits
        if self.subject:
            out["subject"] = self.subject
        if self.level:
            out["level"] = self.level
        if self.policy_decisions:
            out["policy_decisions"] = self.policy_decisions
        return out

    def features_used(self) -> list[str]:
        """The online features this turn's model calls used: the cloud tier when any call was served there."""
        return ["cloud_tier"] if any(c.get("provider_kind") == "cloud" for c in self.model_calls) else []

    def errors(self) -> list[str]:
        return contract_errors(CONTRACT, self.record())


def default_build(hardware_class: str = "mid") -> dict[str, Any]:
    import platform

    system = {"Darwin": "macos", "Linux": "linux", "Windows": "windows"}.get(platform.system(), "linux")
    arch = "arm64" if platform.machine().lower() in ("arm64", "aarch64") else "x64"
    return {
        "app_version": "0.0.0",
        "build_id": "dev",
        "channel": "dev",
        "os_family": system,
        "arch": arch,
        "host": "device",
        "hardware_class": hardware_class,
        "runtime_variant": "none",
    }
