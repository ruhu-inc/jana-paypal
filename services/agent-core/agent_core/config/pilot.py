"""PRD-22: a pilot profile requires a complete content-signed country record."""

from __future__ import annotations

import json
import re
from datetime import datetime
from pathlib import Path
from typing import Any

from agent_core.config.environments import Environment
from agent_core.config.keys import Settings
from agent_core.config.trust import SignatureRefused, TrustStore, bundled_trust

COUNTRY_FIELDS = (
    "locale_overlay",
    "currency",
    "syllabuses_and_gaps",
    "counsel_mapping",
    "safety_resources",
    "school_calendar",
    "field_support",
    "market_evidence",
)
# The fill-in and rehearsal markers of the evidence templates (jana_tools.evidence.placeholders): a checklist row
# that still carries one is incomplete, whatever else it says.
_MARKERS = ("FILL-IN", "DRY-RUN")


def _filled(value: Any) -> bool:
    return isinstance(value, str) and bool(value.strip()) and not any(m in value for m in _MARKERS)


def missing(record: dict[str, Any]) -> list[str]:
    """The TABLE PRD-22a rows (and the record's own country fields) that are absent, empty or still a template."""
    raw = record.get("items")
    items: dict[str, Any] = raw if isinstance(raw, dict) else {}
    result = [name for name in COUNTRY_FIELDS if not _filled(items.get(name))]
    if not re.fullmatch(r"[A-Z]{2}", str(record.get("country", ""))):
        result.append("country")
    if record.get("locale") != f"en-{record.get('country')}":
        result.append("locale")
    if not re.fullmatch(r"[A-Z]{3}", str(record.get("currency_code", ""))) or not _filled(
        record.get("currency_symbol")
    ):
        result.append("currency")
    return result


def country_record_errors(record: Any, environment: Environment, trust: TrustStore | None = None) -> list[str]:
    """Every reason a pilot build refuses this country record, the missing checklist rows named first."""
    if not isinstance(record, dict) or not record:
        return ["PRD-22: missing pilot-country configuration"]
    problems = missing(record)
    errors = ["PRD-22: missing " + ", ".join(problems)] if problems else []
    from agent_core.contracts import errors as contract_errors

    if contract_errors("ops/pilot-country", record):
        return [*errors, "PRD-22: invalid signed pilot-country record"]
    try:
        (trust or bundled_trust()).verify("pilot_country", record, environment, record.get("issued_at"))
    except SignatureRefused as refused:
        errors.append(f"PRD-22: the pilot-country record is not signed by a trusted content key ({refused})")
    receipt = record["pilot_clearance"]
    registered, baseline, starts = (
        datetime.fromisoformat(receipt[key].replace("Z", "+00:00"))
        for key in ("registered_at", "baseline_at", "starts_at")
    )
    if not registered < baseline <= starts:
        errors.append("EVL-22: protocol was not registered before baseline")
    return errors


def require_country(path: str, environment: Environment, trust: TrustStore | None = None) -> dict[str, Any]:
    """The verified record a pilot profile is created under; otherwise ValueError naming every problem."""
    if not path:
        raise ValueError("PRD-22: missing pilot-country configuration")
    record: dict[str, Any] = json.loads(Path(path).read_text())
    errors = country_record_errors(record, environment, trust)
    if errors:
        raise ValueError("; ".join(errors))
    return record


def control_errors(message: dict[str, Any]) -> list[str]:
    """PRD-22: the control contract checks `control.config`, with its optional `pilot_country_file` (D92)."""
    from agent_core.contracts import errors

    path = message.get("pilot_country_file")
    if "pilot_country_file" in message and (not isinstance(path, str) or not path.strip()):
        return ["invalid pilot-country configuration path"]
    return errors("control/control#/$defs/ToCore", message)


def deployment_settings(settings: Settings, config: dict[str, Any]) -> Settings:
    if "pilot_country_file" not in config:
        return settings
    return settings.model_copy(update={"environment": "pilot", "pilot_country_file": config["pilot_country_file"]})
