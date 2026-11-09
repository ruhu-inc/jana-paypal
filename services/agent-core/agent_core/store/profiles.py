"""Local profiles open with a tap; profile tokens isolate learner data (D87, API-04, DAT-14)."""

from __future__ import annotations

import json
import shutil
import sqlite3
from collections.abc import Callable
from pathlib import Path
from typing import Any

from agent_core.config import Settings
from agent_core.store.consents import queue_withdrawal
from agent_core.store.ids import new_ulid
from agent_core.store.integrity import open_checked
from agent_core.store.keystore import KeyStore
from agent_core.store.sqlite import open_database, record_migrations, schema_version, transaction
from agent_core.store.tokens import TokenBook

PROFILE_UPDATE = {
    name: f"UPDATE profile SET {name} = ?, updated_at = ? WHERE profile_id = ?"  # noqa: S608 (fixed names)
    for name in ("display_name", "avatar", "level", "subjects", "exam_date", "age_band")
}
INDEX_UPDATE = {
    name: f"UPDATE profiles_index SET {name} = ? WHERE profile_id = ?"  # noqa: S608 (fixed names)
    for name in ("display_name", "avatar")
}


ADULT_BAND = "18_plus"
SUPPORTED_BANDS = frozenset({"13_15", "16_17", ADULT_BAND})


def require_supported_age(age_band: object) -> None:
    """The integrated product supports teenage and adult learners; only the band is stored."""
    if not isinstance(age_band, str) or age_band not in SUPPORTED_BANDS:
        raise ProfileError("validation_failed", "Jana supports learners aged 13 or over")


class ProfileError(Exception):
    def __init__(self, code: str, message: str = "", retry_after_ms: int | None = None) -> None:
        super().__init__(f"{code}: {message}")
        self.code = code
        self.message = message
        self.retry_after_ms = retry_after_ms


def api_id(internal: str) -> str:
    """The API names a profile `prf_<ULID>`; storage and events use the bare ULID."""
    return f"prf_{internal}"


def internal_id(profile_id: str) -> str:
    if not profile_id.startswith("prf_"):
        raise ProfileError("not_found", "no such profile")
    return profile_id[4:]


class ProfileDirectory:
    def __init__(
        self,
        device: sqlite3.Connection,
        data_dir: Path,
        settings: Settings,
        keys: KeyStore,
        tokens: TokenBook | None = None,
        clock_ms: Callable[[], int] | None = None,
        app_version: str = "0.0.0",
    ) -> None:
        import time

        self.device = device
        self.data_dir = data_dir
        self.settings = settings
        self.keys = keys
        self.tokens = tokens or TokenBook(settings.profile_auto_lock_s)
        self.clock_ms = clock_ms or (lambda: int(time.time() * 1000))
        self.app_version = app_version

    # --- reading ------------------------------------------------------------------------------------------------

    def _row(self, profile_id: str) -> tuple[Any, ...]:
        row: tuple[Any, ...] | None = self.device.execute(
            "SELECT profile_id, display_name, avatar, profile_auth_epoch, db_file, keystore_ref"
            " FROM profiles_index WHERE profile_id = ?",
            (internal_id(profile_id),),
        ).fetchone()
        if row is None:
            raise ProfileError("not_found", "no such profile")
        return row

    def exists(self) -> bool:
        return self.device.execute("SELECT 1 FROM profiles_index LIMIT 1").fetchone() is not None

    def summaries(self) -> dict[str, Any]:
        rows = self.device.execute(
            "SELECT profile_id, display_name, avatar FROM profiles_index ORDER BY created_at"
        ).fetchall()
        return {"profiles": [{"profile_id": api_id(r[0]), "display_name": r[1], "avatar": r[2]}
                             for r in rows]}  # fmt: skip

    def epoch(self, profile_id: str) -> int | None:
        row = self.device.execute(
            "SELECT profile_auth_epoch FROM profiles_index WHERE profile_id = ?", (profile_id[4:],)
        ).fetchone()
        return int(row[0]) if row else None

    def learner_path(self, profile_id: str) -> Path:
        return self.data_dir / str(self._row(profile_id)[4])

    def profile(self, profile_id: str) -> dict[str, Any]:
        """The profile's own record (`Profile`), with its age band: read only with that profile's token (PRV-06,
        API-28, D93); the profile list keeps to name and avatar."""
        self._row(profile_id)
        with self._learner(profile_id) as learner:
            data = learner.execute(
                "SELECT display_name, avatar, level, subjects, exam_date, age_band FROM profile WHERE profile_id = ?",
                (internal_id(profile_id),),
            ).fetchone()
        return {"profile_id": profile_id, "display_name": data[0], "avatar": data[1], "level": data[2],
                "subjects": json.loads(data[3]), "exam_date": data[4], "age_band": data[5]}  # fmt: skip

    def age_band(self, profile_id: str) -> str:
        with self._learner(profile_id) as learner:
            return str(learner.execute("SELECT age_band FROM profile").fetchone()[0])

    def _stored_band(self, profile_id: str) -> str | None:
        """The band as stored, read without recovering the database: a damaged one is recovered when the profile is
        activated, which reports it (DAT-13, UXS-47); until then its band is unknown."""
        path = self.learner_path(profile_id)
        try:
            conn = sqlite3.connect(f"{path.as_uri()}?mode=ro", uri=True)
            try:
                row = conn.execute("SELECT age_band FROM profile").fetchone()
            finally:
                conn.close()
        except sqlite3.Error:
            return None
        return None if row is None else str(row[0])

    def self_consents(self, profile_id: str) -> bool:
        """The profile grants its own consents as a student aged 18 or over (PRV-06): its band is 18_plus, however it
        was set; the product owner chose to trust a corrected band (2026-10-15)."""
        return self.age_band(profile_id) == ADULT_BAND

    # --- the learner database of one profile -------------------------------------------------------------------------

    class _Open:
        def __init__(self, path: Path) -> None:
            self.conn, _, _ = open_checked(path, "learner")

        def __enter__(self) -> sqlite3.Connection:
            return self.conn

        def __exit__(self, *_: object) -> None:
            self.conn.close()

    def _learner(self, profile_id: str) -> ProfileDirectory._Open:
        return ProfileDirectory._Open(self.data_dir / str(self._row(profile_id)[4]))

    def newer_app(self, profile_id: str) -> bool:
        """The profile's learner database was written by a newer app (DAT-12): read without opening or changing it."""
        from agent_core.store.sqlite import MIGRATIONS, recorded_version

        path = self.learner_path(profile_id)
        latest = max(m.version for m in MIGRATIONS["learner"])
        try:
            return path.exists() and path.stat().st_size > 0 and recorded_version(path) > latest
        except sqlite3.DatabaseError:
            return False  # a damaged file is not a newer one: opening it recovers it (DAT-13, UXS-47)

    def open_learner(self, profile_id: str) -> ProfileDirectory._Open:
        """A short-lived connection to one profile's learner database (closed when the `with` block ends)."""
        return self._learner(profile_id)

    @property
    def device_id(self) -> str:
        return str(self.device.execute("SELECT device_id FROM device").fetchone()[0])

    # --- student profiles ----------------------------------------------------------------------------------------

    def create(self, body: dict[str, Any]) -> dict[str, Any]:
        """A supported learner's own folder and database, independently of their course level."""
        require_supported_age(body.get("age_band"))
        now = self.clock_ms()
        if len(self.summaries()["profiles"]) >= 12:
            raise ProfileError("validation_failed", "a device holds at most 12 profiles")
        internal = new_ulid(now)
        profile_id = api_id(internal)
        folder = self.data_dir / "profiles" / internal
        folder.mkdir(parents=True, exist_ok=False)
        keystore_ref = f"jana-meta.profile.{profile_id.lower()}"  # the contract's column; no key is stored (D68)
        learner, applied = open_database(folder / "learner.db", "learner")
        try:
            record_migrations(self.device, "learner", f"profiles/{internal}/learner.db", applied, self.app_version,
                              now)  # fmt: skip
            with transaction(learner):
                learner.execute(
                    "INSERT INTO meta (singleton, profile_id, schema_version, created_at, created_app_ver,"
                    " last_opened_at, last_app_ver) VALUES (1, ?, ?, ?, ?, ?, ?)",
                    (internal, schema_version(learner), now, self.app_version, now, self.app_version),
                )
                learner.execute(
                    "INSERT INTO profile (profile_id, display_name, avatar, age_band, level, subjects, exam_date,"
                    " created_at, updated_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
                    (internal, body["display_name"], body["avatar"], body["age_band"], body["level"],
                     json.dumps(body["subjects"]), body.get("exam_date"), now, now),
                )  # fmt: skip
        finally:
            learner.close()
        with transaction(self.device):
            self.device.execute(
                "INSERT INTO profiles_index (profile_id, kind, display_name, avatar, db_file, keystore_ref,"
                " created_at) VALUES (?, 'student', ?, ?, ?, ?, ?)",
                (internal, body["display_name"], body["avatar"], f"profiles/{internal}/learner.db", keystore_ref, now),
            )  # fmt: skip
            self.audit("profile.create", internal)
        return self.profile(profile_id)

    def unlock(self, profile_id: str) -> dict[str, Any]:
        row = self._row(profile_id)
        stored = self._stored_band(profile_id)
        if stored is not None:
            require_supported_age(stored)
        self.device.execute(
            "UPDATE profiles_index SET last_used_at = ? WHERE profile_id = ?",
            (self.clock_ms(), internal_id(profile_id)),
        )
        token = self.tokens.issue(profile_id, int(row[3]))
        return {"profile_token": token, "expires_in_s": self.tokens.expires_in(token)}

    def lock(self, profile_id: str) -> None:
        self.tokens.revoke(subject=profile_id)

    def profile_of(self, token: str | None) -> str | None:
        return self.tokens.check(token, self.epoch)

    def update(self, profile_id: str, body: dict[str, Any]) -> dict[str, Any]:
        return self.edit(profile_id, body)[0]

    def edit(self, profile_id: str, body: dict[str, Any]) -> tuple[dict[str, Any], list[str]]:
        """The owning learner edits their profile, with the scopes the edit withdrew. When the band is
        lowered from 18 or over, every scope the profile granted itself is withdrawn,
        each queued for its destinations (PRV-06, D86, DAT-11)."""
        now = self.clock_ms()
        fields = {
            k: body[k] for k in ("display_name", "avatar", "level", "subjects", "exam_date", "age_band") if k in body
        }
        if "age_band" in fields:
            require_supported_age(fields["age_band"])
        internal = internal_id(profile_id)
        before = self.age_band(profile_id)
        withdrawn: list[str] = []
        with self._learner(profile_id) as learner, transaction(learner):
            for name, value in fields.items():
                stored = json.dumps(value) if name == "subjects" else value
                learner.execute(PROFILE_UPDATE[name], (stored, now, internal))
            if before == ADULT_BAND and fields.get("age_band", ADULT_BAND) != ADULT_BAND:
                withdrawn = [
                    str(r[0])
                    for r in learner.execute(
                        "SELECT scope FROM consents WHERE granted_by_role = 'student_18_plus' AND withdrawn_at IS NULL"
                    )
                ]
                learner.execute("UPDATE consents SET withdrawn_at = ?, withdrawn_by_role = 'student' WHERE"
                                " granted_by_role = 'student_18_plus' AND withdrawn_at IS NULL", (now,))  # fmt: skip
        with transaction(self.device):
            for name in ("display_name", "avatar"):
                if name in fields:
                    self.device.execute(INDEX_UPDATE[name], (fields[name], internal))
            for scope in withdrawn:
                queue_withdrawal(self.device, internal, scope, now,
                                 now + int(self.settings.retention_tombstone_days * 86_400_000))  # fmt: skip
        return self.profile(profile_id), withdrawn

    # --- deletion (DAT-09; the full sweep of outboxes and tombstones is M10.d) ------------------------------------

    def delete(self, profile_id: str) -> dict[str, Any]:
        row = self._row(profile_id)
        folder = (self.data_dir / str(row[4])).parent
        files = sum(1 for p in folder.rglob("*") if p.is_file()) if folder.is_dir() else 0
        self.tokens.revoke(subject=profile_id)
        internal = internal_id(profile_id)
        with transaction(self.device):
            self.device.execute("DELETE FROM profiles_index WHERE profile_id = ?", (internal,))
            self.device.execute("DELETE FROM migrations WHERE db_file = ?", (str(row[4]),))
            self.device.execute("DELETE FROM egress_ledger WHERE profile_id IN (?, ?)", (internal, profile_id))
            # Undelivered items go, except the notices that tell a personal copy to delete this profile (DAT-09).
            self.device.execute("DELETE FROM outbox WHERE profile_id IN (?, ?) AND kind NOT IN ('tombstone',"
                                " 'consent_withdrawal')", (internal, profile_id))  # fmt: skip
            self.device.execute("DELETE FROM idempotency_responses WHERE response LIKE ? OR response LIKE ?",
                                (f"%{internal}%", f"%{profile_id}%"))  # fmt: skip
            # Earlier audit rows keep their action and time but no longer name the profile: only the deletion row does.
            self.device.execute("UPDATE device_audit SET profile_id = NULL WHERE profile_id = ?", (internal,))
            self._forget_device_settings(internal, profile_id)
            self.audit("profile.delete", internal)
        self.device.execute("PRAGMA wal_checkpoint(TRUNCATE)")
        shutil.rmtree(folder, ignore_errors=True)
        self.keys.delete(str(row[5]))
        return {"deleted": True, "rows_removed": 1, "files_removed": files}

    def _forget_device_settings(self, internal: str, profile_id: str) -> None:
        """Device settings keyed by the deleted profile go with it (DAT-09): its cloud spend, its place in the
        subscription's covered profiles and in the self-raised age bands."""
        from agent_core.store import subscription
        from agent_core.store.egress import SPEND_KEY

        names = {internal, profile_id}
        now = self.clock_ms()
        row = self.device.execute("SELECT value FROM app_settings WHERE key = ?", (SPEND_KEY,)).fetchone()
        spend = json.loads(row[0]) if row else None
        if isinstance(spend, dict) and names & set(spend):
            kept = {k: v for k, v in spend.items() if k not in names}
            self.device.execute("UPDATE app_settings SET value = ?, updated_at = ? WHERE key = ?",
                                (json.dumps(kept, sort_keys=True), now, SPEND_KEY))  # fmt: skip
        stored = subscription.load(self.device)
        if stored:
            pruned = {**stored, **{k: [p for p in stored.get(k) or [] if p not in names]
                                   for k in ("profiles", "consent_needed")}}  # fmt: skip
            if pruned != stored:
                subscription.save(self.device, pruned, now)

    # --- device audit (SEC-19: adult actions logged with time and actor) ------------------------------------------

    def audit(self, action: str, profile_id: str | None, detail: dict[str, Any] | None = None,
              actor: str = "user") -> None:  # fmt: skip
        role = "system" if action in ("tombstone.expired", "consent_withdrawal.expired") else actor
        self.device.execute(
            "INSERT INTO device_audit (audit_id, actor_role, action, profile_id, detail, at) VALUES (?, ?, ?, ?, ?, ?)",
            (f"aud_{new_ulid(self.clock_ms())}", role, action, profile_id, json.dumps(detail) if detail else None,
             self.clock_ms()),
        )  # fmt: skip
