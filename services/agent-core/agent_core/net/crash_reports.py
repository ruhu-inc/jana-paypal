"""Crash report upload, opt-in (OBS-17, D14): built from a crash record, shown in full, sent only when the adult says.

A report leaves the device only while the adult has `crash_reports` on, and only after the adult has seen the exact
report ("Send this report?") and chosen to send it. This is fixed: there is no "send automatically" option and no
other per-feature setting (D53). With the feature off, nothing is built for upload, nothing is queued and no request
is ever made.

A report holds the crash record's allowed fields only (`ALLOWED`): process, kind, app version, error type, stack (file,
line and function; module and offset), the redacted log tail, a few numeric details (exit code, the limit hit) and
the hardware class. Never minidump memory, correlation ids, student text, names or photos. Before a report is queued
the redaction scanner runs over the whole body (contact details, profile ids, the device's identifying terms); a hit
refuses the report, as does a report the outbox `CrashReport` contract does not accept (D39). Approved reports wait
in the outbox (ARC-09, at most `crash_reports_queued_max`) and go on reconnect; turning the feature off deletes them
(they are payloads, not tombstones; DAT-11).
"""

from __future__ import annotations

import json
from collections.abc import Iterable, Mapping
from typing import Any

from agent_core.contracts import errors as contract_errors
from agent_core.net.redaction import Redactor
from agent_core.store import egress as egress_store

FEATURE = "crash_reports"
KIND = "crash_report"
ALLOWED = ("crash_id", "process", "kind", "version", "error_type", "stack", "log_tail", "detail", "hardware_class",
           "day")  # fmt: skip
STACK_FIELDS = ("file", "line", "function", "module", "offset")
DETAIL_FIELDS = ("exit_code", "limit", "tool")
DAY_MS = 86_400_000
CONTRACT = "cloud/outbox#/$defs/CrashReport"


class CrashReportRefused(ValueError):
    def __init__(self, code: str, detail: str) -> None:
        super().__init__(detail)
        self.code = code


def build_report(record: Mapping[str, Any], hardware_class: str | None) -> dict[str, Any]:
    """The report exactly as the adult sees it and as it would be sent: allowed fields only."""
    detail = {k: v for k, v in dict(record.get("detail") or {}).items() if k in DETAIL_FIELDS}
    return {
        "crash_id": str(record["crash_id"]),
        "process": str(record["process"]),
        "kind": str(record["kind"]),
        "version": str(record["version"]),
        "error_type": record.get("error_type"),
        "stack": [{k: v for k, v in frame.items() if k in STACK_FIELDS} for frame in record.get("stack", [])],
        "log_tail": [str(line) for line in record.get("log_tail", [])],
        "detail": detail,
        "hardware_class": hardware_class,
        "day": int(record.get("at_ms", 0)) // DAY_MS * DAY_MS,  # the day only, not the moment
    }


def report_problems(report: Mapping[str, Any], identifying_terms: Iterable[str] = ()) -> list[str]:
    """Why a report may not leave: a field outside the allowed list, or a redaction-scanner hit."""
    problems = [f"field {key!r} is not allowed" for key in report if key not in ALLOWED]
    for frame in report.get("stack", []):
        problems += [f"stack field {key!r} is not allowed" for key in frame if key not in STACK_FIELDS]
    problems += [f"detail {key!r} is not allowed" for key in report.get("detail", {}) if key not in DETAIL_FIELDS]
    problems += [f"the redaction scanner found {f.kind}" for f in Redactor(identifying_terms).findings(report)]
    return problems


def approve(layer: Any, report: Mapping[str, Any], identifying_terms: Iterable[str], max_queued: int,
            expires_ms: int) -> str:  # fmt: skip
    """The adult chose "Send" on this exact report: queue it for upload. Refused (and nothing queued) while the
    feature is off, or when the report fails the allow-list or the redaction scanner."""
    if FEATURE not in layer.features_on():
        raise CrashReportRefused("feature_disabled", "crash reports are off")
    problems = report_problems(report, identifying_terms)
    if problems:
        raise CrashReportRefused("redaction_blocked", problems[0])
    if shape := contract_errors(CONTRACT, dict(report)):
        raise CrashReportRefused("invalid_report", f"the report does not match its contract ({shape[0][:160]})")
    queued = [item for item in egress_store.pending(layer.device, FEATURE) if item.kind == KIND]
    for item in queued[: max(0, len(queued) - max_queued + 1)]:
        egress_store.drop(layer.device, item.outbox_id)  # the oldest go first; the newest report is kept
    payload = json.dumps(dict(report), sort_keys=True, separators=(",", ":")).encode()
    outbox_id: str = layer.outbox.put(KIND, FEATURE, payload, expires_ms=expires_ms,
                                      idempotency_key=f"crash-{report['crash_id']}")  # fmt: skip
    return outbox_id
