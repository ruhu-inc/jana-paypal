"""Signed device policy and turn-boundary decisions (OPS-02, OPS-03, OPS-10, OPS-11, PRV-21).

The device's only reader of the signed safety configuration, whether it arrives in a pack, an update, the catalogue
or a USB kit. Consumers apply TargetDecision at their execution boundary. Learner records are never removed.
"""

from __future__ import annotations

import json
import logging
import os
import re
import tempfile
import threading
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from functools import total_ordering
from pathlib import Path
from typing import Any

from agent_core.config.environments import Environment
from agent_core.config.trust import TrustSource, TrustStore, current_trust
from agent_core.contracts import validate
from agent_core.contracts.ops.safety_configuration import Entry

_LOCKS: dict[Path, Any] = {}
_LOCKS_GUARD = threading.Lock()
LOG = logging.getLogger(__name__)


def _policy_lock(folder: Path) -> Any:
    with _LOCKS_GUARD:
        return _LOCKS.setdefault(folder.resolve(), threading.RLock())


ACTIONS = {
    "item": {"withdraw"},
    "lesson": {"withdraw"},
    "worked_example": {"withdraw"},
    "diagram": {"withdraw"},
    "visual_kind": {"disable"},
    "skill": {"disable", "pin_previous"},
    "model_pack": {"disable"},
    "router_candidate": {"disable"},
    "tool": {"disable"},
    "online_feature": {"force_off"},
    "app_version": {"advise_update"},
}
FALLBACKS = {
    "item": "another_item",
    "lesson": "correction_placeholder",
    "worked_example": "correction_placeholder",
    "diagram": "correction_placeholder",
    "visual_kind": "board_text",
    "skill": "reviewed_previous_or_omit",
    "model_pack": "base_smaller_or_content_only",
    "router_candidate": "next_candidate",
    "tool": "omit_unverified_result",
    "online_feature": "purge_queue_keep_tombstones",
    "app_version": "update_advice",
}


# The safety configuration contract (OPS-10, OPS-11, D92): the file's envelope is validated as a whole; each entry
# applies only when it is a valid `$defs/Entry` (TABLE OPS-11a), otherwise it is ignored while the others apply.
SafetyEntry = Entry


@total_ordering
@dataclass(frozen=True)
class VersionKey:
    core: tuple[int, ...]
    prerelease: tuple[str, ...]

    def __lt__(self, other: VersionKey) -> bool:
        if self.core != other.core:
            return self.core < other.core
        if not self.prerelease:
            return False
        if not other.prerelease:
            return True
        for left, right in zip(self.prerelease, other.prerelease, strict=False):
            if left == right:
                continue
            if left.isdigit() and right.isdigit():
                return int(left) < int(right)
            if left.isdigit() != right.isdigit():
                return left.isdigit()
            return left < right
        return len(self.prerelease) < len(other.prerelease)


def version(value: str) -> VersionKey:
    match = re.fullmatch(
        r"(0|[1-9]\d*)\.(0|[1-9]\d*)\.(0|[1-9]\d*)(?:-([A-Za-z0-9.-]+))?(?:\+([A-Za-z0-9.-]+))?", value
    )
    if not match:
        raise ValueError("OPS-02: invalid version")
    prerelease = tuple(match[4].split(".")) if match[4] else ()
    build = tuple(match[5].split(".")) if match[5] else ()
    if any(not part for part in (*prerelease, *build)) or any(
        part.isdigit() and len(part) > 1 and part.startswith("0") for part in prerelease
    ):
        raise ValueError("OPS-02: invalid version identifier")
    return VersionKey(tuple(int(match[i]) for i in (1, 2, 3)), prerelease)


def atomic_json(path: Path, data: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, temporary = tempfile.mkstemp(dir=path.parent, prefix=path.name)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            json.dump(data, handle, sort_keys=True)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, path)
    finally:
        Path(temporary).unlink(missing_ok=True)


def _read(path: Path, fallback: Any) -> Any:
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return fallback


@dataclass(frozen=True)
class TargetDecision:
    action: str = "allow"
    fallback: str | None = None
    entry_id: str | None = None
    incident_id: str | None = None
    notice_key: str | None = None


class DevicePolicy:
    def __init__(
        self, folder: Path, environment: Environment, channel: str, app_version: str, trust: TrustSource = None
    ) -> None:
        if channel not in {"dev", "pilot", "stable"}:
            raise ValueError("OPS-02: invalid channel")
        version(app_version)
        self.folder = folder / "config" / "operations" / channel
        self.environment, self.channel, self.app_version, self.trust = environment, channel, app_version, trust
        self._lock = _policy_lock(self.folder.parent)

    def _verified(self, kind: str, doc: Any, *, previously_accepted: bool = False) -> bool:
        try:
            if not isinstance(doc, dict) or doc.get("channel") != self.channel:
                return False
            if kind == "compatibility_matrix":
                validate("ops/compatibility-matrix", doc)
            else:
                validate("ops/safety-configuration", doc)
            trust = current_trust(self.trust)
            if previously_accepted and kind == "safety_configuration":
                # OPS-10: revocation cannot enable a feature by discarding an accepted restriction.
                # Only held restrictions use this path; incoming files always use current revocations.
                trust = TrustStore(trust.roots, trust.certificates)
            trust.verify(kind, doc, self.environment, str(doc["issued_at"]))
        except (ValueError, KeyError, TypeError):
            return False
        return True

    def matrix(self) -> dict[str, Any]:
        doc = _read(self.folder / "matrix.json", {})
        return doc if self._verified("compatibility_matrix", doc) else {}

    def adopt_matrix(self, doc: Mapping[str, Any]) -> bool:
        with self._lock:
            if not self._verified("compatibility_matrix", doc):
                return False
            if doc["matrix_version"] <= self.matrix().get("matrix_version", 0):
                return False
            atomic_json(self.folder / "matrix.json", dict(doc))
            return True

    def _state(self) -> dict[str, Any]:
        state = _read(self.folder / "active.json", {})
        return state if isinstance(state, dict) else {}

    def _retired(self) -> frozenset[str]:
        return frozenset(self._state().get("retired", [])) | frozenset(_read(self.folder.parent / "retired.json", []))

    def summary(self) -> dict[str, Any]:
        entries, rejected = self._entries()
        return {
            "sequence": max((d["sequence"] for d in self._documents()), default=0),
            "pending_sequence": max((d["sequence"] for d in self._pending()), default=0),
            "ignored_entries": len(rejected),
            "entries": [
                e.model_dump(include={"entry_id", "target_kind", "target", "action", "notice_key"}) for e in entries
            ],
        }

    def _documents(self) -> list[dict[str, Any]]:
        return [
            d
            for d in self._state().get("documents", [])
            if self._verified("safety_configuration", d, previously_accepted=True)
        ]

    def _pending(self) -> list[dict[str, Any]]:
        value = _read(self.folder / "pending.json", [])
        candidates = value if isinstance(value, list) else [value]
        return [d for d in candidates if self._verified("safety_configuration", d, previously_accepted=True)]

    def adopt_safety(self, doc: Mapping[str, Any]) -> bool:
        with self._lock:
            if not self._verified("safety_configuration", doc):
                return False
            previous = self._documents()
            pending = self._pending()
            previous.extend(pending)
            if doc["sequence"] <= max((d["sequence"] for d in previous), default=0):
                return False
            atomic_json(self.folder / "pending.json", [*pending, dict(doc)])
            return True

    def apply_online(self, online: Any) -> None:
        """The one place accepted online-feature restrictions reach the connectivity layer (OPS-10, OPS-11, PRV-21):
        a forced-off feature stops, its queued sends are purged (tombstones kept) and its telemetry queue cleared;
        a change is announced. Called at start and at every turn boundary, after `turn_boundary`."""
        decide = self.frozen_decider()
        forced = {f for f in online.policy.features if decide("online_feature", f, None).action == "force_off"}
        changed = forced != online.safety_forced_off
        online.safety_forced_off = forced
        for feature in forced:
            online.outbox.purge(feature)
            if feature == "telemetry":
                online.telemetry.clear()
        if changed:
            online.announce()

    def adopt_folder(self, folder: Path) -> dict[str, bool]:
        return {
            "matrix": self.adopt_matrix(_read(folder / "compatibility-matrix.json", {})),
            "safety": self.adopt_safety(_read(folder / "safety-configuration.json", {})),
        }

    def turn_boundary(self, installed: Mapping[str, str]) -> list[str]:
        with self._lock:
            for v in installed.values():
                version(v)
            documents = self._documents()
            for pending in self._pending():
                if pending["sequence"] > max((d["sequence"] for d in documents), default=0):
                    documents.append(pending)
            if not documents and not self._state():
                return []
            valid, rejected = self._parsed_entries(documents)
            retired = set(self._retired())
            retired.update(
                entry.target
                for entry in valid
                if entry.target_kind == "item"
                and entry.action == "withdraw"
                and entry.reason_code in {"wrong_key", "unsafe", "unsafe_content"}
            )
            state = {**self._state(), "documents": documents, "installed": dict(installed), "retired": sorted(retired)}
            atomic_json(self.folder.parent / "retired.json", sorted(retired))
            atomic_json(self.folder / "active.json", state)
            if (self.folder / "pending.json").is_file() and rejected:
                LOG.warning("OPS-10: ignored %d invalid safety entries", len(rejected))
            (self.folder / "pending.json").unlink(missing_ok=True)
            return rejected

    def _parsed_entries(self, documents: list[dict[str, Any]]) -> tuple[list[SafetyEntry], list[str]]:
        entries: dict[str, SafetyEntry] = {}
        rejected = []
        for doc in documents:
            for raw in doc["entries"]:
                try:
                    validate("ops/safety-configuration#/$defs/Entry", raw)
                    entry = SafetyEntry.model_validate(raw)
                    if entry.action not in ACTIONS.get(entry.target_kind, set()):
                        raise ValueError("unsupported restriction")
                    for v in (entry.min_version, entry.max_version_exclusive):
                        if v is not None:
                            version(v)
                    previous = entries.get(entry.entry_id)
                    if previous is not None and previous != entry:
                        identity = {
                            "target_kind",
                            "target",
                            "reason_code",
                            "incident_id",
                            "min_version",
                            "max_version_exclusive",
                        }
                        same_scope = previous.model_dump(include=identity) == entry.model_dump(include=identity)
                        stronger = previous.action == entry.action or (previous.action, entry.action) == (
                            "pin_previous",
                            "disable",
                        )
                        old_lift, new_lift = previous.lift_when, entry.lift_when
                        safe_lift = new_lift is None or (
                            old_lift is not None
                            and old_lift.artefact_id == new_lift.artefact_id
                            and version(new_lift.version) >= version(old_lift.version)
                        )
                        if not (same_scope and stronger and safe_lift):
                            raise ValueError("OPS-10: changed entry weakens or changes restriction scope")
                    entries[entry.entry_id] = entry
                except ValueError:
                    rejected.append(str(raw.get("entry_id", "unknown")) if isinstance(raw, dict) else "unknown")
        return list(entries.values()), rejected

    def _entries(self) -> tuple[list[SafetyEntry], list[str]]:
        entries, rejected = self._parsed_entries(self._documents())
        installed = self._state().get("installed", {})
        active = []
        for entry in entries:
            lift = entry.lift_when
            if lift is not None and lift.artefact_id in installed:
                if version(installed[lift.artefact_id]) >= version(lift.version):
                    continue
            active.append(entry)
        return active, rejected

    def retire_item(self, item_id: str) -> None:
        if not item_id.strip():
            raise ValueError("CUR-36: item id required")
        with self._lock:
            state = self._state()
            state["retired"] = sorted(self._retired() | {item_id})
            atomic_json(self.folder.parent / "retired.json", state["retired"])
            atomic_json(self.folder / "active.json", state)

    def frozen_decider(self) -> Callable[[str, str, str | None], TargetDecision]:
        entries = self._entries()[0]
        retired = self._retired()

        def decide(kind: str, target: str, target_version: str | None = None) -> TargetDecision:
            return self.decision(kind, target, target_version, entries=entries, retired=retired)

        return decide

    def decision(
        self,
        kind: str,
        target: str,
        target_version: str | None = None,
        *,
        entries: list[SafetyEntry] | None = None,
        retired: frozenset[str] | None = None,
    ) -> TargetDecision:
        retired = self._retired() if retired is None else retired
        if kind == "item" and target in retired:
            return TargetDecision("withdraw", FALLBACKS[kind], "retired-item")
        matching = []
        for entry in self._entries()[0] if entries is None else entries:
            if entry.target_kind != kind or entry.target != target:
                continue
            if entry.min_version is not None or entry.max_version_exclusive is not None:
                if target_version is None:
                    continue
                if entry.min_version and version(target_version) < version(entry.min_version):
                    continue
                if entry.max_version_exclusive and version(target_version) >= version(entry.max_version_exclusive):
                    continue
            matching.append(entry)
        if not matching:
            return TargetDecision()
        # A narrower restriction cannot override an existing disable/withdraw (OPS-10).
        chosen = next((e for e in matching if e.action in {"withdraw", "disable", "force_off"}), matching[-1])
        return TargetDecision(chosen.action, FALLBACKS[kind], chosen.entry_id, chosen.incident_id, chosen.notice_key)

    def teaching_allowed(self) -> bool:
        return True

    def offered(self, kind: str, artefact_id: str, artefact_version: str, *, format_major: int) -> bool:
        matrix = self.matrix()
        app = next((a for a in matrix.get("apps", []) if a["app_version"] == self.app_version), None)
        if not app or app["status"] not in {"supported", "deprecated"}:
            return False
        table, id_key, version_key, contract = {
            "content_pack": ("content_packs", "pack_id", "version", "content_pack_format"),
            "model_pack": ("model_packs", "model_pack_id", "manifest_version", "model_pack_format"),
            "agent_bundle": ("agent_bundles", "bundle_version", "bundle_version", "agent_bundle_schema"),
        }.get(kind, ("", "", "", ""))
        if not table or format_major != int(app["contracts"][contract].split(".")[0]):
            return False
        if any(
            w["artefact_kind"] == kind and w["artefact_id"] == artefact_id and artefact_version in w["versions"]
            for w in matrix.get("withdrawals", [])
        ):
            return False
        for row in matrix.get(table, []):
            if (
                str(row[id_key]) != artefact_id
                or str(row[version_key]) != artefact_version
                or row["status"] not in {"supported", "deprecated"}
            ):
                continue
            if kind == "agent_bundle":
                return bool(row["app_major_minor"] == ".".join(self.app_version.split(".")[:2]))
            bounds = row["app_range"]
            return version(self.app_version) >= version(bounds["min_app_version"]) and (
                "max_app_version_exclusive" not in bounds
                or version(self.app_version) < version(bounds["max_app_version_exclusive"])
            )
        return False

    def skill_version(self, name: str, current: str, previous: list[dict[str, Any]]) -> tuple[str, str] | None:
        decision = self.decision("skill", name, current)
        if decision.action != "pin_previous":
            return None
        candidates = [
            p
            for p in previous
            if p.get("name") == name
            and p.get("reviewed") is True
            and re.fullmatch(r"[a-f0-9]{64}", str(p.get("sha256", "")))
            and version(str(p["version"])) < version(current)
        ]
        if not candidates:
            return None
        chosen = max(candidates, key=lambda p: version(str(p["version"])))
        return str(chosen["version"]), str(chosen["sha256"])

    def model_fallback(self, disabled: str, installed_largest_first: list[str], bases: dict[str, str]) -> str | None:
        base = bases.get(disabled)
        smaller = (
            installed_largest_first[installed_largest_first.index(disabled) + 1 :]
            if disabled in installed_largest_first
            else []
        )
        candidates = ([base] if base in installed_largest_first else []) + smaller
        for candidate in candidates:
            if (
                candidate is not None
                and candidate != disabled
                and self.decision("model_pack", candidate).action == "allow"
            ):
                return candidate
        return None
