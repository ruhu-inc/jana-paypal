"""The school licence file and the licence state the core runs with (PLT-19, PLT-20, BIZ-04, BIZ-09, SEC-28, D84).

**Nothing to activate (D84, D30).** The offline product is free and needs no activation, code or licence check: an
installation without a licence file is the whole offline product. Paid online features are bought through the
subscription (D70, `store.subscription`). The only other licence surface is the one signed JSON file a school
installs on its lab computers (artefact `licence_file`, licence key, SEC-12). Nobody types a code and no device code
exists, so the file binds to no hardware; its `seats` is the number of computers bought, stated for the school and
for support, and not counted on the device (D84 keeps setup simple; BIZ-11 accepts leakage). Applying a school seat
or lab bundle file makes the install `school_lab` (JRN-11).

**Trusted time.** A school licence's expiry follows the latest trusted time, never the wall clock alone (PLT-20):
signed times seen (the file's `created_at`) and the wall clock, which may move the trusted time forward by at most
`licence_clock_step_days` per app start and never backward. Setting the clock back changes nothing; setting it forward
cannot end a licence early in one start.

**When it applies.** The state is evaluated once at app start and kept for the life of the process, so expiry never
interrupts a session or a mock sitting (PLT-20); applying a file applies at once. Every state but `active` is the free
offline product with its data intact, which is the same product: no licence state blocks learning (BIZ-09). Nothing
here uses the network, a kernel driver, obfuscation or any identifier of the computer (SEC-28, BIZ-11)."""

from __future__ import annotations

import json
import os
import sqlite3
from collections.abc import Callable, Iterable, Mapping
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Literal

from agent_core.config import Settings
from agent_core.config.environments import Environment
from agent_core.config.trust import SignatureRefused, TrustStore, bundled_trust
from agent_core.contracts import errors as contract_errors
from agent_core.contracts.ops.licence import SchoolLicenceFile
from agent_core.store.entitlements import InstallKind, Offer, entitlements
from agent_core.store.events import iso_ms, ms_from_iso
from agent_core.store.ids import new_ulid
from agent_core.store.sqlite import transaction

KINDS: tuple[Offer, ...] = ("school_seat", "lab_bundle")
CHANNELS = ("stable", "pilot", "dev")
FOLDER = "licence"
FILE = "licence.json"
CLOCK_KEY = "licence_trusted_time"
CONTRACT = "ops/licence#/$defs/SchoolLicenceFile"
Status = Literal["none", "active", "expired", "invalid"]


class LicenceRefused(ValueError):
    """A licence file that cannot be used. `reason` is a fixed code the client words."""

    def __init__(self, reason: str, message: str) -> None:
        super().__init__(f"{reason}: {message}")
        self.reason = reason
        self.message = message


@dataclass(frozen=True)
class Licence:
    licence_id: str
    kind: Offer
    seats: int
    features: tuple[str, ...]
    channel: str
    expires_at_ms: int | None
    issued_at_ms: int


def read_file(document: Mapping[str, Any], trust: TrustStore, environment: Environment) -> Licence:
    """A signed licence file, verified offline (signature first, then its `SchoolLicenceFile` contract, D39), or
    `LicenceRefused` (`invalid`)."""
    try:
        trust.verify("licence_file", document, environment, str(document.get("created_at") or ""))
    except SignatureRefused as refused:
        raise LicenceRefused("invalid", str(refused)) from None
    except (AttributeError, TypeError, ValueError) as error:
        raise LicenceRefused("invalid", f"the licence file is incomplete ({type(error).__name__})") from None
    if document.get("policy"):  # layer 4 (ARC-27): no key may be set by a licence yet
        raise LicenceRefused("invalid", "the licence sets configuration this version does not allow")
    problems = contract_errors(CONTRACT, dict(document))
    if problems:
        raise LicenceRefused("invalid", f"the licence file does not match its contract ({problems[0][:160]})")
    file = SchoolLicenceFile.model_validate(dict(document))
    try:
        expires = ms_from_iso(file.expires_at) if file.expires_at else None
        issued = ms_from_iso(file.created_at)
    except ValueError as error:
        raise LicenceRefused("invalid", f"the licence file is incomplete ({type(error).__name__})") from None
    return Licence(file.licence_id, file.kind, file.seats, tuple(file.features), file.channel, expires, issued)


# --- trusted time (PLT-20) ----------------------------------------------------------------------------------------


def trusted_time(device: sqlite3.Connection) -> int | None:
    row = device.execute("SELECT value FROM app_settings WHERE key = ?", (CLOCK_KEY,)).fetchone()
    return int(json.loads(row[0])["high_water_ms"]) if row else None


def advance_trusted_time(device: sqlite3.Connection, wall_ms: int, signed_ms: Iterable[int], settings: Settings
                         ) -> tuple[int, bool]:  # fmt: skip
    """Move the trusted time on from the wall clock (at most one step per start) and from signed times; returns it and
    whether the wall clock is set well behind it."""
    high = trusted_time(device)
    floor = max([t for t in (high, *signed_ms) if t is not None], default=None)
    step = settings.licence_clock_step_days * 86_400_000
    new = wall_ms if floor is None else max(floor, min(wall_ms, floor + step))
    behind = high is not None and wall_ms < high - settings.licence_clock_behind_hours * 3_600_000
    with transaction(device):
        device.execute(
            "INSERT INTO app_settings (key, value, updated_at) VALUES (?, ?, ?) ON CONFLICT (key) DO UPDATE SET"
            " value = excluded.value, updated_at = excluded.updated_at",
            (CLOCK_KEY, json.dumps({"high_water_ms": new}), new),
        )
    return new, behind


# --- the state the core runs with ---------------------------------------------------------------------------------


@dataclass(frozen=True)
class LicenceState:
    status: Status
    licence: Licence | None
    install_kind: InstallKind
    trusted_now_ms: int
    clock_behind: bool = False
    reason: str | None = None

    @property
    def offer(self) -> Offer:
        return self.licence.kind if self.status == "active" and self.licence is not None else "free"

    def view(self, subscribed: bool = False) -> dict[str, Any]:
        """`LicenceStatus`, with TABLE BIZ-06a for this install and whether the online
        subscription is active now (an ended one applies at once to online features, never to offline ones; PLT-20).
        The licence id and expiry are shown for support; nothing about a student is in it."""
        lic = self.licence if self.status in ("active", "expired") else None
        return {
            "state": self.status,
            "offer": self.offer,
            "licence_id": lic.licence_id if lic else None,
            "seats": lic.seats if lic else None,
            "expires_at": iso_ms(lic.expires_at_ms) if lic and lic.expires_at_ms is not None else None,
            "channel": lic.channel if lic and self.status == "active" else "stable",
            "install_kind": self.install_kind,
            "clock_behind": self.clock_behind,
            "entitlements": entitlements(self.install_kind, subscribed),
        }


def _install_kind(device: sqlite3.Connection) -> InstallKind:
    row = device.execute("SELECT install_kind FROM device").fetchone()
    kind = str(row[0]) if row else "family"
    return "school_lab" if kind == "school_lab" else "school_server" if kind == "school_server" else "family"


class LicenceDesk:
    """The licence of one installation: evaluated once at start (`state` is then fixed for the process) and replaced
    only by applying a licence file that is active."""

    def __init__(self, data_dir: Path, device: sqlite3.Connection, settings: Settings, environment: Environment,
                 trust: TrustStore | None = None, wall_ms: Callable[[], int] | None = None) -> None:  # fmt: skip
        self.folder = data_dir / FOLDER
        self.device = device
        self.settings = settings
        self.environment = environment
        self.trust = trust or bundled_trust()
        self.wall_ms = wall_ms or (lambda: int(datetime.now(UTC).timestamp() * 1000))
        self.state = self._evaluate()

    @property
    def path(self) -> Path:
        return self.folder / FILE

    def _state(self, licence: Licence | None, status: Status, now: int, behind: bool, reason: str | None = None
               ) -> LicenceState:  # fmt: skip
        return LicenceState(status, licence, _install_kind(self.device), now, behind, reason)

    def _judge(self, licence: Licence, now: int, behind: bool) -> LicenceState:
        if licence.expires_at_ms is not None and now >= licence.expires_at_ms:
            return self._state(licence, "expired", now, behind, "expired")
        return self._state(licence, "active", now, behind)

    def _evaluate(self) -> LicenceState:
        if not self.path.is_file():
            now, behind = advance_trusted_time(self.device, self.wall_ms(), [], self.settings)
            return self._state(None, "none", now, behind)
        try:
            licence: Licence | None = read_file(json.loads(self.path.read_text(encoding="utf-8")), self.trust,
                                                self.environment)  # fmt: skip
        except (OSError, ValueError):  # LicenceRefused is a ValueError, as is a file that is not JSON
            licence = None
        signed = [licence.issued_at_ms] if licence is not None else []
        now, behind = advance_trusted_time(self.device, self.wall_ms(), signed, self.settings)
        if licence is None:
            return self._state(None, "invalid", now, behind, "invalid")
        return self._judge(licence, now, behind)

    def apply(self, document: Mapping[str, Any]) -> LicenceState:
        """Apply a school's licence file. A refused one changes nothing; an accepted one applies now."""
        licence = read_file(document, self.trust, self.environment)
        now, behind = advance_trusted_time(self.device, self.wall_ms(), [licence.issued_at_ms], self.settings)
        judged = self._judge(licence, now, behind)
        if judged.status != "active":
            raise LicenceRefused(judged.reason or judged.status, f"the licence is {judged.status}")
        self.folder.mkdir(parents=True, exist_ok=True)
        temp = self.path.with_name(FILE + ".tmp")
        temp.write_text(json.dumps(dict(document), indent=1, sort_keys=True) + "\n", encoding="utf-8")
        os.replace(temp, self.path)
        with transaction(self.device):
            self.device.execute("UPDATE device SET install_kind = 'school_lab'")  # JRN-11: a school's install
            self.device.execute(
                "INSERT INTO device_audit (audit_id, actor_role, action, profile_id, detail, at) VALUES"
                " (?, 'system', 'licence.activate', NULL, ?, ?)",
                (f"aud_{new_ulid(now)}", json.dumps({"licence_id": licence.licence_id, "kind": licence.kind}), now),
            )
        self.state = self._judge(licence, now, behind)
        return self.state
