"""What the ingest accepts from a device's outbox (OBS-17, OBS-20, PRV-09, SEC-21).

Each body is parsed strictly (UTF-8 JSON object, no duplicate keys, no NaN or infinity), validated against its
contract, matched to its idempotency key, and re-scanned for anything OBS-19 says must never be uploaded. Anything
else is refused with a code and stored nowhere. Both contracts are bundled as `ingest.contracts` by
`python -m jana_tools codegen`, so the ingest and the core's outbox check the same shapes (D39).

- `telemetry`: `cloud/outbox#/$defs/Telemetry` (the `trace/telemetry` report, OBS-20, D92); the idempotency key is
  the report's own random `report_id` (the outbox sends it so).
- `crash_report`: `cloud/outbox#/$defs/CrashReport` (the report exactly as the adult saw it, OBS-17; a whole UTC day,
  never the moment, OBS-19a); the idempotency key is `crash-<crash_id>`.

Replies are the contract's `OutboxReceipt` and `OutboxError` (D99)."""

from __future__ import annotations

import json
import math
import re
from dataclasses import dataclass
from typing import Any

from ingest import contracts, redaction

KINDS = ("crash_report", "telemetry")
KEY_PATTERN = re.compile(r"^[A-Za-z0-9_-]{8,80}$")
CONTRACT = {"crash_report": "cloud/outbox#/$defs/CrashReport", "telemetry": "cloud/outbox#/$defs/Telemetry"}


@dataclass(frozen=True)
class Refused(Exception):
    status: int
    code: str
    detail: str


def _no_duplicates(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    keys = [k for k, _ in pairs]
    if len(keys) != len(set(keys)):
        raise ValueError("duplicate key")
    return dict(pairs)


def _no_constants(name: str) -> Any:
    raise ValueError(f"{name} is not JSON")


def parse(raw: bytes) -> dict[str, Any]:
    try:
        value = json.loads(raw.decode("utf-8"), object_pairs_hook=_no_duplicates, parse_constant=_no_constants)
    except (UnicodeDecodeError, ValueError) as error:
        raise Refused(400, "invalid_json", "The body is not one strict JSON object.") from error
    if not isinstance(value, dict):
        raise Refused(400, "invalid_json", "The body must be a JSON object.")
    return value


def crash_report_problems(body: dict[str, Any]) -> list[str]:
    """Why a crash report is not one the core could have sent: its contract's errors."""
    return contracts.errors(CONTRACT["crash_report"], body)


def _finite(node: Any) -> bool:
    if isinstance(node, dict):
        return all(_finite(v) for v in node.values())
    if isinstance(node, list):
        return all(_finite(v) for v in node)
    return not isinstance(node, float) or math.isfinite(node)


def telemetry_problems(body: dict[str, Any]) -> list[str]:
    problems = contracts.errors(CONTRACT["telemetry"], body)
    if not _finite(body):
        problems.append("non-finite measurement")
    return problems


def accept(kind: str, idempotency_key: str | None, raw: bytes) -> dict[str, Any]:
    """The validated body, or `Refused`. Nothing is stored before this returns."""
    if kind not in KINDS:
        raise Refused(404, "unknown_kind", "This service receives crash reports and telemetry only.")
    if idempotency_key is None or not KEY_PATTERN.fullmatch(idempotency_key):
        raise Refused(400, "idempotency_key_required", "Send the item's idempotency key (ARC-09).")
    body = parse(raw)
    if kind == "telemetry":
        problems = telemetry_problems(body)
        if not problems and body["report_id"] != idempotency_key:
            problems = ["the idempotency key is the report's own report_id"]
    else:
        problems = crash_report_problems(body)
        if not problems and idempotency_key != f"crash-{body['crash_id']}":
            problems = ["the idempotency key is crash-<crash_id>"]
    if problems:
        # Only where the body breaks its contract: a schema message can quote the value, which is never echoed.
        where = problems[0].split(": ", 1)[0] if problems[0].startswith("$") else problems[0]
        raise Refused(422, "contract_violation", f"The {kind} breaks its contract at {where}.")
    # Telemetry has no free text to scan: every string is an enumerated value or a fixed pattern (D92), and a random
    # report id could only ever match a pattern by chance. A crash report's frames and log lines are free text.
    found = redaction.findings(body) if kind == "crash_report" else []
    if found:
        # Named by kind only; the matched text is never echoed, logged or stored (OBS-19).
        raise Refused(422, "redaction_blocked", f"The {kind} holds something that may not be uploaded ({found[0]}).")
    return body
