"""The cloud tier subscription on this device (D70, BIZ-07, PRV-06).

Subscribing turns the cloud tier on; ending the subscription turns it off. The subscriber is an adult (or a student
aged 18 or over paying for themselves). The checkout hands the device a signed entitlement (licence key, SEC-12): its
subscription id, plan, price band and billing, paid period, seats (the students it covers), the one token allowance
per student per month (BIZ-07, BIZ-08) and the plan's features and fair-use limits (TABLE BIZ-18a). The plans are
priced by region and sold for a month, a term of three months paid at once, or a year (D33, D121). Whatever the plan,
the allowance is monthly and the same for every covered student (no premium quota, BIZ-18): `allowance_window`
gives the allowance month of the paid period that holds a moment, calendar months from the period's start, so a
prepaid term or year resets its allowance each month and an exhausted allowance comes back within weeks. For each
profile under 18 it names, the guardian's consent is recorded in the same action; until every named minor has it the
subscription is `pending_consent` and the tier stays off for them.

States: `none` (never subscribed), `pending_consent`, `active`, `ended` (cancelled, or past the period's end). The
record lives in `app_settings` under `cloud_subscription`; payment never happens on the device and students never see
it (BIZ-07)."""

from __future__ import annotations

import calendar
import json
import sqlite3
from collections.abc import Iterable, Mapping
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any, Literal

from agent_core.config.environments import Environment
from agent_core.config.trust import SignatureRefused, TrustStore, bundled_trust
from agent_core.contracts import errors as contract_errors
from agent_core.contracts.ops.licence import CloudTierEntitlement
from agent_core.store.events import iso_ms, ms_from_iso

KEY = "cloud_subscription"
KIND = "cloud_tier_subscription"
ARTEFACT = "licence_file"  # the entitlement is part of the device's licence (ARC-27 layer 4)
CONTRACT = "ops/licence#/$defs/CloudTierEntitlement"
State = Literal["none", "pending_consent", "active", "ended"]


class EntitlementRefused(ValueError):
    pass


@dataclass(frozen=True)
class Entitlement:
    subscription_id: str
    period_start_ms: int
    period_end_ms: int
    seats: int
    allowance_tokens: int  # per covered student per allowance month (D33)
    licence_id: str | None
    profile_ids: tuple[str, ...] = ()  # the profiles the checkout covered, when it names them
    plan: str = "jana_online"  # TABLE BIZ-18a (D121)
    band: str = "international"
    billing: str = "month"
    features: tuple[str, ...] = ()
    limits: Mapping[str, int] | None = None  # the plan's fair-use limits per student


def verify(document: Mapping[str, Any], environment: Environment, trust: TrustStore | None = None) -> Entitlement:
    """The checkout's signed entitlement: its signature first, then its `CloudTierEntitlement` contract (D39)."""
    if document.get("kind") != KIND:
        raise EntitlementRefused("not a cloud tier entitlement")
    try:
        (trust or bundled_trust()).verify(ARTEFACT, document, environment, str(document.get("created_at") or ""))
    except SignatureRefused as refused:
        raise EntitlementRefused(str(refused)) from None
    except (AttributeError, TypeError, ValueError) as error:
        raise EntitlementRefused(f"the entitlement is incomplete ({type(error).__name__})") from None
    problems = contract_errors(CONTRACT, dict(document))
    if problems:
        raise EntitlementRefused(f"the entitlement does not match its contract ({problems[0][:160]})")
    signed = CloudTierEntitlement.model_validate(dict(document))
    try:
        start, end = ms_from_iso(signed.period_start), ms_from_iso(signed.period_end)
    except ValueError as error:
        raise EntitlementRefused(f"the entitlement is incomplete ({type(error).__name__})") from None
    if end <= start:
        raise EntitlementRefused("the entitlement's period is not valid")
    return Entitlement(signed.subscription_id, start, end, signed.seats, signed.allowance_tokens, signed.licence_id,
                       tuple(signed.profile_ids or ()), signed.plan, signed.band, signed.billing,
                       tuple(signed.features), signed.limits.model_dump(exclude_none=True))  # fmt: skip


def load(device: sqlite3.Connection) -> dict[str, Any]:
    row = device.execute("SELECT value FROM app_settings WHERE key = ?", (KEY,)).fetchone()
    return json.loads(row[0]) if row else {}


def save(device: sqlite3.Connection, record: Mapping[str, Any], now_ms: int) -> None:
    device.execute(
        "INSERT INTO app_settings (key, value, updated_at) VALUES (?, ?, ?) ON CONFLICT (key) DO UPDATE SET"
        " value = excluded.value, updated_at = excluded.updated_at",
        (KEY, json.dumps(dict(record), sort_keys=True), now_ms),
    )


def record(entitlement: Entitlement, profiles: Iterable[str], minors: Iterable[str], consented: Iterable[str],
           payer: str, document: Mapping[str, Any] | None = None) -> dict[str, Any]:  # fmt: skip
    """The stored record: the profiles the subscription covers and which minors still need the guardian's consent,
    with the signed entitlement itself, which the device presents to the gateway to register its credential (SEC-21,
    D70)."""
    covered = list(dict.fromkeys(profiles))
    if len(covered) > entitlement.seats:
        raise EntitlementRefused("more profiles than the subscription's seats")
    needed = [p for p in covered if p in set(minors) and p not in set(consented)]
    return {"subscription_id": entitlement.subscription_id, "period_start": entitlement.period_start_ms,
            "period_end": entitlement.period_end_ms, "seats": entitlement.seats,
            "allowance_tokens": entitlement.allowance_tokens, "licence_id": entitlement.licence_id,
            "plan": entitlement.plan, "band": entitlement.band, "billing": entitlement.billing,
            "features": list(entitlement.features), "limits": dict(entitlement.limits or {}),
            "profiles": covered, "consent_needed": needed, "payer": payer, "ended_at": None,
            "entitlement": dict(document) if document is not None else None}  # fmt: skip


def state(stored: Mapping[str, Any], now_ms: int) -> State:
    if not stored:
        return "none"
    if stored.get("ended_at") is not None or now_ms >= int(stored["period_end"]):
        return "ended"
    if stored.get("consent_needed"):
        return "pending_consent"
    return "active"


def add_months(moment_ms: int, months: int) -> int:
    """`moment_ms` moved by whole calendar months (UTC), the day of month kept where the month has it."""
    at = datetime.fromtimestamp(moment_ms / 1000, UTC)
    index = at.month - 1 + months
    year, month = at.year + index // 12, index % 12 + 1
    moved = at.replace(year=year, month=month, day=min(at.day, calendar.monthrange(year, month)[1]))
    return int(moved.timestamp() * 1000)


def allowance_window(stored: Mapping[str, Any], now_ms: int) -> tuple[int, int, int]:
    """(month index, start, end) of the allowance month that holds `now_ms`: month `k` runs from `k` calendar months
    after the paid period's start to `k + 1` months after it, and ends at the period's end at the latest (D33)."""
    start = int(stored["period_start"])
    at, began = datetime.fromtimestamp(now_ms / 1000, UTC), datetime.fromtimestamp(start / 1000, UTC)
    months = max(0, (at.year - began.year) * 12 + at.month - began.month)
    if months and add_months(start, months) > now_ms:
        months -= 1
    return months, add_months(start, months), min(add_months(start, months + 1), int(stored["period_end"]))


def allowance_period(stored: Mapping[str, Any], now_ms: int) -> str:
    """The name a profile's metered tokens are kept under: the subscription and its current allowance month."""
    if not stored:
        return "none"
    return f"{stored.get('subscription_id')}#m{allowance_window(stored, now_ms)[0]}"


def covers(stored: Mapping[str, Any], profile_id: str, now_ms: int) -> bool:
    """A current subscription covers this approved profile; another profile may still await consent."""
    return (
        state(stored, now_ms) in ("active", "pending_consent")
        and profile_id in stored.get("profiles", [])
        and profile_id not in stored.get("consent_needed", [])
    )


def granted(stored: Mapping[str, Any], profile_id: str, now_ms: int) -> dict[str, Any] | None:
    """What the subscription gives this profile now (TABLE BIZ-18a): its plan, features and fair-use limits, from the
    signed entitlement; None when it does not cover the profile. Free's online taste is held by the gateway."""
    if not covers(stored, profile_id, now_ms):
        return None
    return {"plan": stored.get("plan"), "features": list(stored.get("features", [])),
            "limits": dict(stored.get("limits", {}))}  # fmt: skip


def consent_given(stored: Mapping[str, Any], profiles: Iterable[str]) -> dict[str, Any]:
    """The guardian's consent recorded for these profiles: they no longer wait."""
    given = set(profiles)
    return {**stored, "consent_needed": [p for p in stored.get("consent_needed", []) if p not in given]}


def view(stored: Mapping[str, Any], now_ms: int, allowance_used_up: bool = False) -> dict[str, Any]:
    """`CloudSubscription` (BIZ-07, D70)."""
    current = state(stored, now_ms)
    needed = set(stored.get("consent_needed", []))
    return {
        "state": current,
        "subscription_id": stored.get("subscription_id"),
        "period_end": iso_ms(int(stored["period_end"])) if stored else None,
        "allowance_used_up": allowance_used_up,
        "profiles": [{"profile_id": p, "consent": "needed" if p in needed else "recorded"}
                     for p in stored.get("profiles", [])],
    }  # fmt: skip
