"""The one result envelope (TOL-02) and the error contract (TOL-10, TABLE TOL-10a).

Every error message starts with the clause that matches `action_taken`, ends with one next step, is capped and
never carries a stack trace, a file path or student data."""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Any

from agent_core.contracts import errors as contract_errors

NO_ACTION = "No action was taken."
COMPLETED = "The action completed, but "
PARTLY = "The action may have partly completed: "


@dataclass(frozen=True)
class ErrorRule:
    action_taken: bool | None  # None: per receipt (cancelled)
    retryable: bool
    next_step: str


# TABLE TOL-10a: what each code means for the model, and the next step it is given.
ERROR_RULES: dict[str, ErrorRule] = {
    "unknown_tool": ErrorRule(False, False, "Call one of the listed tools."),
    "unavailable": ErrorRule(False, False, "Continue without this tool."),
    "invalid_args": ErrorRule(False, True, "Fix the named field and send the call again."),
    "truncated_args": ErrorRule(False, True, "Send shorter arguments."),
    "policy_denied": ErrorRule(False, False, "Use a tool your role is allowed to call."),
    "student_declined": ErrorRule(True, False, "Continue without the answer."),
    "timeout": ErrorRule(False, True, "Narrow the request and try once more."),
    "cancelled": ErrorRule(None, False, "Stop; the turn was interrupted."),
    "worker_crashed": ErrorRule(False, True, "Retry the call once."),
    "worker_unavailable": ErrorRule(False, False, "Continue without numbers you have not checked."),
    "limit_exceeded": ErrorRule(False, False, "Send a smaller input."),
    "domain_error": ErrorRule(False, False, "Change the request; this one has no answer of this kind."),
    "execution_error": ErrorRule(False, False, "Report that the check failed and continue."),
    "result_too_large": ErrorRule(True, False, "Ask for fewer points or a narrower result."),
    "repeated_failure_blocked": ErrorRule(False, False, "Change the arguments or the approach."),
}
_INTERNAL = re.compile(r"Traceback \(most recent call last\)|(?:[A-Za-z]:)?[\\/](?:[\w.-]+[\\/])+[\w.-]+|File \"")


def scrub(text: str) -> str:
    """Remove stack traces and file paths from text that reaches a model (TOL-10)."""
    return _INTERNAL.sub("[detail removed]", text)


def error_message(code: str, detail: str, action_taken: bool, max_chars: int = 2000) -> str:
    rule = ERROR_RULES[code]
    lead = NO_ACTION if not action_taken else COMPLETED
    body = scrub(detail.strip()).rstrip(".")
    body = body[:1].upper() + body[1:] if lead == NO_ACTION else body
    text = f"{lead} {body}. {rule.next_step}" if lead == NO_ACTION else f"{lead}{body}. {rule.next_step}"
    if len(text) > max_chars:
        keep = max_chars - len(rule.next_step) - 6
        text = text[:keep].rstrip() + " … " + rule.next_step
    return text


def error_result(code: str, detail: str, meta: dict[str, Any], *, action_taken: bool | None = None,
                 worker_code: int | None = None, max_chars: int = 2000) -> dict[str, Any]:  # fmt: skip
    rule = ERROR_RULES[code]
    taken = rule.action_taken if action_taken is None else action_taken
    taken = bool(taken)
    message = error_message(code, detail, taken, max_chars)
    error: dict[str, Any] = {"code": code, "message": message, "retryable": rule.retryable}
    if worker_code is not None:
        error["worker_code"] = worker_code
    status = "cancelled" if code == "cancelled" else "error"
    return {"status": status, "output": message, "error": error, "meta": {**meta, "action_taken": taken}}


def ok_result(output: str, meta: dict[str, Any], data: Any = None, ui: dict[str, Any] | None = None) -> dict[str, Any]:
    result: dict[str, Any] = {"status": "ok", "output": output, "meta": {**meta, "action_taken": True}}
    if data is not None:
        result["data"] = data
    if ui is not None:
        result["ui"] = ui
    return result


def envelope_errors(result: dict[str, Any]) -> list[str]:
    return contract_errors("tools/tool-result", result)
