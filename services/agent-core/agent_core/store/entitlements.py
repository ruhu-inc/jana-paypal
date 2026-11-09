"""Entitlements (BIZ-06, BIZ-01, BIZ-02, D30, D84): TABLE BIZ-06a for this device, decided offline from `offers.json`,
the install kind (a school licence file makes it `school_lab`) and whether the online subscription is active (D70).

Offline features are never checked: every install has all of them, with no limit, counter or allowance (BIZ-02), and
local tutoring is never metered by use (BIZ-01). `check_table` refuses a table that says otherwise. Online features are
`available` (the subscription can switch them on), `subscribed` (it has) or `no` (never on this install, such as a
`school_lab` one, where no online scope can be granted; PRV-06, JRN-11). Each still needs its own switch or consent
(ARC-11)."""

from __future__ import annotations

import json
from collections.abc import Mapping
from functools import cache
from pathlib import Path
from typing import Any, Literal

OFFERS = Path(__file__).with_name("offers.json")
Offer = Literal["free", "school_seat", "lab_bundle"]
InstallKind = Literal["family", "school_lab", "school_server"]
OFFLINE = ("lessons", "practice", "tool_checks", "progress", "local_tutor", "mock_exams", "offline_study_area")
ONLINE = ("cloud_tier", "study_materials", "cloud_backup", "multi_device", "parent_remote_view", "web_client")
CELLS: dict[str, frozenset[str]] = {
    **{name: frozenset({"yes"}) for name in OFFLINE},  # never checked, so the table cannot say no (BIZ-02, BIZ-09)
    "content_updates": frozenset({"all"}),
    "student_profiles": frozenset({"all"}),
    "parent_summary": frozenset({"yes", "no"}),
    **{name: frozenset({"subscription", "no"}) for name in ONLINE},
}
# Words that would meter local tutoring (BIZ-01) or limit an offline feature (BIZ-02).
METERED = ("question", "turn", "message", "token", "request")
LIMITS = ("limit", "per_day", "per_month", "per_profile", "allowance", "count", "quota", "max", "window")


class OfferTableError(ValueError):
    pass


def check_table(table: Mapping[str, Any]) -> list[str]:
    """Problems with the entitlement table: unknown or missing cells, values outside a cell's vocabulary, any meter on
    local tutoring (BIZ-01) and any limit on an offline feature (BIZ-02)."""
    problems = []
    for column in ("every_install", "school_lab"):
        cells = table.get(column)
        if not isinstance(cells, Mapping):
            problems.append(f"{column}: missing")
            continue
        for cell, value in cells.items():
            text = f"{cell} {value}"
            if any(word in text for word in METERED):
                problems.append(f"{column}.{cell}: local tutoring is never metered by use (BIZ-01)")
            elif any(word in text for word in LIMITS):
                problems.append(f"{column}.{cell}: an offline feature has no limit, counter or allowance (BIZ-02)")
            elif cell not in CELLS:
                problems.append(f"{column}.{cell}: not a feature of TABLE BIZ-06a")
            elif value not in CELLS[cell]:
                problems.append(f"{column}.{cell}: {value!r} is not one of {sorted(CELLS[cell])}")
        if column == "every_install":
            problems += [f"every_install.{cell}: missing" for cell in CELLS if cell not in cells]
        elif any(cell in OFFLINE for cell in cells):
            problems.append("school_lab: a licence file never changes an offline feature (PLT-19)")
    return problems


@cache
def table() -> dict[str, Any]:
    loaded: dict[str, Any] = json.loads(OFFERS.read_text())
    problems = check_table(loaded)
    if problems:
        raise OfferTableError("; ".join(problems))
    return loaded


def entitlements(install_kind: InstallKind, subscribed: bool,
                 source: Mapping[str, Any] | None = None) -> dict[str, Any]:  # fmt: skip
    """TABLE BIZ-06a for an install of `install_kind`, with or without an active subscription."""
    offers = source if source is not None else table()
    cells = dict(offers["every_install"])
    if install_kind == "school_lab":
        cells.update(offers.get("school_lab", {}))
    out: dict[str, Any] = {}
    for cell, value in cells.items():
        if cell in ONLINE:
            out[cell] = "no" if value == "no" else "subscribed" if subscribed else "available"
        elif CELLS[cell] <= {"yes", "no"}:
            out[cell] = value == "yes"
        else:
            out[cell] = value
    return out
