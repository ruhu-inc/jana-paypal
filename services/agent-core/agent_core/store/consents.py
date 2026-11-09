"""Consent, one scope per feature (PRV-05, PRV-06, ARC-11, D43, D53).

Profile scopes live in the profile's learner database (`consents`); the device scopes `telemetry` and `crash_reports`
in `device_consents`. A grant records who gave it, how, the version of the plain-words notice shown and the DPIA
reference; with no DPIA reference configured nothing can be granted (PRV-08, D13). Withdrawal is one step: the active
row is marked withdrawn, and a scope whose data may sit at a destination queues a withdrawal notice in the outbox
(DAT-11). Refusing every scope leaves local tutoring working: nothing on the local path reads a consent."""

from __future__ import annotations

import json
import sqlite3
from collections.abc import Collection
from pathlib import Path
from typing import Any

from agent_core.contracts.cloud.outbox import ConsentWithdrawal
from agent_core.store.events import iso_ms
from agent_core.store.ids import new_ulid

STAGES = ("S1", "S2", "S3", "S4", "S5")
# TABLE PRV-05a: scope -> (stage, an online feature of TABLE ARC-07a or None)
PROFILE_SCOPES: dict[str, tuple[str, str | None]] = {
    "cloud_tier": ("S2", "cloud_tier"),
    "research_use": ("S2", None),
    "study_materials": ("S3", "study_materials"),
    "cloud_backup": ("S4", "cloud_backup"),
    "multi_device": ("S4", "multi_device"),
    "parent_remote_view": ("S4", "parent_remote_view"),
    "cloud_voice": ("S4", "cloud_voice"),
    "model_training_research": ("S5", None),
    # SPEC §19.3.5 (W8, JM-D21): screened notes and code-built evidence lines may reach the cloud tutor (needs cloud).
    "tutor_memory_online": ("S2", "tutor_memory_online"),
}
DEVICE_SCOPES = ("telemetry", "crash_reports")
# TABLE ARC-07a: feature -> stage
FEATURES = {"cloud_tier": "S2", "downloads": "S2", "update_checks": "S2", "telemetry": "S2", "crash_reports": "S2",
            "study_materials": "S3", "cloud_backup": "S4", "multi_device": "S4", "parent_remote_view": "S4",
            "cloud_voice": "S4", "tutor_memory_online": "S2"}  # fmt: skip
# The stage whose consent and connectivity surface ships: these endpoints arrive with M10 (S2).
SHIPPED = "S2"


class ConsentError(Exception):
    def __init__(self, code: str, message: str) -> None:
        super().__init__(f"{code}: {message}")
        self.code = code
        self.message = message


def load_notices(path: Path) -> dict[str, list[str]]:
    """The versions of each shipped plain-words notice, oldest first (PRV-12)."""
    if not path.is_file():
        return {}
    found: dict[str, list[str]] = {}
    for entry in json.loads(path.read_text())["notices"]:
        found.setdefault(str(entry["id"]), []).append(str(entry["version"]))
    return found


def shipped(stage: str) -> bool:
    return STAGES.index(stage) <= STAGES.index(SHIPPED)


def shipped_scopes(include_study: bool = False, early: Collection[str] = ()) -> list[str]:
    """`early`: scopes of a later stage this build carries as an explicit capability (cloud voice, VOC-28)."""
    return [
        s
        for s, (stage, _) in PROFILE_SCOPES.items()
        if shipped(stage) or (include_study and s == "study_materials") or s in early
    ]


def shipped_features() -> list[str]:
    return [f for f, stage in FEATURES.items() if shipped(stage)]


def active(conn: sqlite3.Connection, scope: str) -> tuple[Any, ...] | None:
    row: tuple[Any, ...] | None = conn.execute(
        "SELECT consent_id, granted_by_role, method, notice_version, granted_at, paused_at FROM consents"
        " WHERE scope = ? AND"
        " withdrawn_at IS NULL ORDER BY granted_at DESC LIMIT 1",
        (scope,),
    ).fetchone()
    return row


def states(conn: sqlite3.Connection, features_on: set[str], include_study: bool = False,
           early: Collection[str] = ()) -> list[dict[str, Any]]:  # fmt: skip
    out = []
    for scope in shipped_scopes(include_study, early):
        row = active(conn, scope)
        feature = PROFILE_SCOPES[scope][1]
        granted = row is not None
        out.append({"scope": scope, "granted": granted, "granted_by_role": row[1] if row else None,
                    "method": row[2] if row else None, "notice_version": row[3] if row else None,
                    "granted_at": iso_ms(row[4]) if row else None,
                    "paused": row is not None and row[5] is not None,
                    "effective": row is not None and row[5] is None
                    and (feature is None or feature in features_on)})  # fmt: skip
    return out


def grant(conn: sqlite3.Connection, scope: str, role: str, label: str, notice_version: str, dpia_ref: str,
          now_ms: int, include_study: bool = False, early: Collection[str] = ()) -> None:  # fmt: skip
    """One active grant per scope; granting again replaces nothing (the earlier grant stays the record)."""
    if scope not in PROFILE_SCOPES or scope not in shipped_scopes(include_study, early):
        raise ConsentError("validation_failed", f"{scope} is not a consent scope of this version")
    if not dpia_ref:
        raise ConsentError("feature_disabled", "consent cannot be recorded before its DPIA reference is set (D13)")
    if active(conn, scope) is not None:
        return
    conn.execute(
        "INSERT INTO consents (consent_id, scope, granted_by_role, granted_by_label, notice_version, dpia_ref, method,"
        " granted_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
        (
            new_ulid(now_ms),
            scope,
            role,
            label[:60],
            notice_version,
            dpia_ref,
            "in_app_notice"
            if role == "student_18_plus"
            else "in_app_guardian"
            if scope in ("research_use", "model_training_research")
            else "checkout_guardian",
            now_ms,
        ),
    )


def withdraw(conn: sqlite3.Connection, scope: str, role: str, now_ms: int) -> bool:
    changed = conn.execute(
        "UPDATE consents SET withdrawn_at = ?, withdrawn_by_role = ? WHERE scope = ? AND withdrawn_at IS NULL",
        (now_ms, role, scope),
    ).rowcount
    return bool(changed)


def pause(conn: sqlite3.Connection, scope: str, now_ms: int) -> None:
    conn.execute("UPDATE consents SET paused_at = ? WHERE scope = ? AND withdrawn_at IS NULL", (now_ms, scope))


def resume(conn: sqlite3.Connection, scope: str) -> None:
    conn.execute("UPDATE consents SET paused_at = NULL WHERE scope = ? AND withdrawn_at IS NULL", (scope,))


def queue_withdrawal(device: sqlite3.Connection, profile_internal: str, scope: str, now_ms: int,
                     expires_ms: int) -> None:  # fmt: skip
    """The withdrawal reaches every destination that may hold data under the scope (DAT-11 delivers it)."""
    destination = PROFILE_SCOPES[scope][1] or scope
    # The upload body is the outbox contract's `ConsentWithdrawal` (D39).
    payload = json.dumps(ConsentWithdrawal(scope=scope, withdrawn_at=iso_ms(now_ms)).model_dump()).encode()
    outbox_id = new_ulid(now_ms)
    device.execute(
        "INSERT INTO outbox (outbox_id, idempotency_key, kind, feature, profile_id, destination, payload, bytes,"
        " created_at, expires_at) VALUES (?, ?, 'consent_withdrawal', ?, ?, ?, ?, ?, ?, ?)",
        (outbox_id, f"withdraw:{profile_internal}:{scope}:{outbox_id}", PROFILE_SCOPES[scope][1], profile_internal,
         destination, payload, len(payload), now_ms, expires_ms),
    )  # fmt: skip


def device_grant(device: sqlite3.Connection, scope: str, notice_version: str, dpia_ref: str, now_ms: int) -> None:
    if not dpia_ref:
        raise ConsentError("feature_disabled", "consent cannot be recorded before its DPIA reference is set (D13)")
    if device.execute("SELECT 1 FROM device_consents WHERE scope = ? AND withdrawn_at IS NULL", (scope,)).fetchone():
        return
    device.execute(
        "INSERT INTO device_consents (consent_id, scope, granted_by_role, notice_version, dpia_ref, method,"
        " granted_at) VALUES (?, ?, 'device_user', ?, ?, 'in_app_notice', ?)",
        (new_ulid(now_ms), scope, notice_version, dpia_ref, now_ms),
    )


def device_withdraw(device: sqlite3.Connection, scope: str, now_ms: int) -> None:
    device.execute("UPDATE device_consents SET withdrawn_at = ? WHERE scope = ? AND withdrawn_at IS NULL",
                   (now_ms, scope))  # fmt: skip
