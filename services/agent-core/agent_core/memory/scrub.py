"""The egress scrub of the tutor's notes (SPEC §19.6, W8 phase 3, JM-D21).

While `tutor_memory_online` is effective, a note may ride a cloud request only after `scrub_for_egress` passes it:
the do-not-capture filter (`memory/capture.py`), the write policy's text rules (`memory/policy.py`, as the render
check applies them), the provider-key patterns of the gateway's secret scan (`cloud_gateway/secret_scan.py`, mirrored
here because the device never imports the gateway; `tests/test_w8_memory_online.py` keeps the two in step), and the
Redactor's profile terms (`config/personal_data.py`). A note that matches is withheld from the cloud variant and
flagged on the knows page. The evaluation grader (`evals/memory/grader.en.yaml`) is independent and never used here.

The scrub returns a reason code, never the matched text."""

from __future__ import annotations

import re
from collections.abc import Iterable

from agent_core.config import Settings
from agent_core.config.personal_data import Redactor
from agent_core.memory.capture import CaptureFilter
from agent_core.memory.policy import render_check

# cloud_gateway.secret_scan.PATTERNS, as text patterns (the header pattern there runs over a lower-cased copy).
SECRET_PATTERNS: dict[str, re.Pattern[str]] = {
    "google_api_key": re.compile(r"AIza[0-9A-Za-z_\-]{35}"),
    "anthropic_key": re.compile(r"sk-ant-[A-Za-z0-9_\-]{8,}"),
    "openai_key": re.compile(r"sk-(?:proj-|svcacct-|admin-)?[A-Za-z0-9_\-]{20,}"),
    "nvidia_api_key": re.compile(r"nvapi-[A-Za-z0-9_\-]{20,}"),
    "meta_api_key": re.compile(r"LLM_[0-9]{4,}_[A-Za-z0-9_\-]{16,}"),
    "legacy_search_key": re.compile(r"tvly-[A-Za-z0-9_\-]{20,}"),
    "goog_api_key_header": re.compile(r"x-goog-api-key[\"']?\s*[:=]\s*[\"']?([a-z0-9_\-]{20,})", re.IGNORECASE),
}


def scrub_for_egress(
    text: str, settings: Settings, capture: CaptureFilter | None, terms: Iterable[str] = ()
) -> str | None:
    """None when the note may leave the device under the consent; otherwise why not (`captured`, a policy rule's
    code, `secret` or `profile_term`). A missing capture filter withholds every note: nothing is sent unscreened."""
    if capture is None:
        return "capture_filter_missing"
    found = render_check(text, settings, capture)
    if found:
        return str(found[0].code)
    if any(pattern.search(text) for pattern in SECRET_PATTERNS.values()):
        return "secret"
    if Redactor([t for t in terms if t]).blocks(text):
        return "profile_term"
    return None
