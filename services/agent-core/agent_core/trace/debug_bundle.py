"""Debug bundle export (OBS-18): Settings → Help → "Save a report for support" makes a local zip the app never sends.

The default bundle holds: the app, build and hardware summary; the resolved configuration with every secret removed;
the health panel snapshot; redacted logs of the last `debug_bundle_log_days`; trace metadata (no payloads) of the last
`debug_bundle_trace_turns` turns; the crash records (never a minidump); the egress ledger summary; the installed pack
and model manifest ids; the student's saved problem reports as category, date and turn reference, with a report's
note only when its stored `allow_support_export` is true; and `manifest.json` with the sha256 of every other file.
A preview lists every file before anything is saved.

The last session's conversation (questions and answers) is added only when the adult ticks the box, and the box is
enabled only after the student, signed in to their own profile, confirmed "Let support see this session" for that
session (a per-session student action, like a transcript share): an adult PIN alone can never include it. Including
it forces passphrase encryption (Argon2id and AES-GCM, as profile archives are sealed), and the student is then told
the session was included in a support report.
"""

from __future__ import annotations

import hashlib
import io
import json
import os
import time
import zipfile
from collections.abc import Callable, Iterable, Mapping
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from agent_core.config import Settings
from agent_core.store.exports import seal_with_passphrase
from agent_core.trace.logs import SECRET_FIELDS, SECRET_VALUES, redact_value

DAY_MS = 86_400_000
SHARES = "support_shares.json"
CONVERSATION = "conversation.json"
EXTENSION_PLAIN = ".zip"
EXTENSION_SEALED = ".janadiag"


class BundleRefused(ValueError):
    def __init__(self, code: str, detail: str) -> None:
        super().__init__(detail)
        self.code = code


@dataclass
class SupportShares:
    """The student's per-session "Let support see this session" confirmations, in their own profile folder (a
    proposed `support_shares` table carries them once the data model has it)."""

    folder: Path
    clock_ms: Callable[[], int] = lambda: int(time.time() * 1000)

    @property
    def path(self) -> Path:
        return self.folder / SHARES

    def _read(self) -> dict[str, Any]:
        if not self.path.is_file():
            return {}
        loaded: dict[str, Any] = json.loads(self.path.read_text(encoding="utf-8"))
        return loaded

    def _write(self, data: Mapping[str, Any]) -> None:
        self.folder.mkdir(parents=True, exist_ok=True)
        temp = self.path.with_name(SHARES + ".tmp")
        temp.write_text(json.dumps(dict(data), sort_keys=True), encoding="utf-8")
        os.replace(temp, self.path)

    def confirm(self, session_id: str, acting_profile: str | None, owner_profile: str) -> None:
        """The student's own confirmation: only a request made with that student's profile token counts."""
        if acting_profile is None or acting_profile != owner_profile:
            raise BundleRefused("student_confirmation_required", "only the student can share their session")
        data = self._read()
        data[session_id] = {"confirmed_at": self.clock_ms(), "included_at": None}
        self._write(data)

    def confirmed(self, session_id: str) -> bool:
        return session_id in self._read()

    def mark_included(self, session_id: str) -> None:
        data = self._read()
        if session_id in data:
            data[session_id]["included_at"] = self.clock_ms()
            self._write(data)

    def included_notices(self) -> list[str]:
        """Sessions the student is told were included in a support report."""
        return sorted(s for s, entry in self._read().items() if entry.get("included_at"))


@dataclass
class BundleInputs:
    """Everything the bundle reads, gathered by the core; nothing here reaches a network."""

    summary: Mapping[str, Any]  # app, build and hardware
    config: Mapping[str, Any]  # the resolved configuration
    health: Mapping[str, Any]
    log_dir: Path | None
    traces: list[dict[str, Any]]
    crash_records: list[dict[str, Any]]
    ledger: list[dict[str, Any]]
    packs: list[str]
    models: list[str]
    problem_reports: list[dict[str, Any]]  # rows: report_id, session_id, content (JSON), created_at
    now_ms: int
    conversations: Callable[[str], list[dict[str, Any]]] = field(default=lambda _: [])  # a session's payloads


def _strip_secrets(value: Any, key: str = "") -> Any:
    if SECRET_FIELDS.search(key):
        return "[removed]"
    if isinstance(value, Mapping):
        return {k: _strip_secrets(v, k) for k, v in value.items()}
    if isinstance(value, list):
        return [_strip_secrets(v, key) for v in value]
    if isinstance(value, str):
        return SECRET_VALUES.sub("[removed]", value)
    return value


def _logs(log_dir: Path | None, since_ms: int) -> dict[str, bytes]:
    out: dict[str, bytes] = {}
    if log_dir is None or not log_dir.is_dir():
        return out
    for path in sorted(log_dir.glob("*.jsonl")):
        kept = []
        for line in path.read_text(encoding="utf-8", errors="replace").splitlines():
            try:
                entry = json.loads(line)
            except ValueError:
                continue
            if int(entry.get("ts", 0)) >= since_ms:
                kept.append(json.dumps({k: redact_value(k, v) for k, v in entry.items()}, ensure_ascii=False))
        if kept:
            out[f"logs/{path.name}"] = ("\n".join(kept) + "\n").encode()
    return out


def _problem_reports(rows: Iterable[Mapping[str, Any]]) -> list[dict[str, Any]]:
    """Category, date and turn reference of every saved report; the note only with `allow_support_export`."""
    listed = []
    for row in rows:
        content = row["content"] if isinstance(row["content"], Mapping) else json.loads(str(row["content"]))
        entry = {"report_id": row["report_id"], "category": content.get("category"),
                 "date": time.strftime("%Y-%m-%d", time.gmtime(int(row["created_at"]) / 1000)),
                 "session_id": row.get("session_id"), "turn_id": content.get("turn_id")}  # fmt: skip
        if content.get("allow_support_export") is True and content.get("note"):
            entry["note"] = content["note"]
        listed.append(entry)
    return listed


def _json(value: Any) -> bytes:
    return (json.dumps(value, indent=1, sort_keys=True, ensure_ascii=False) + "\n").encode()


def bundle_files(inputs: BundleInputs, settings: Settings, conversation_session: str | None = None,
                 shares: SupportShares | None = None) -> dict[str, bytes]:  # fmt: skip
    """The bundle's files by name (manifest last). Raises `BundleRefused` for a conversation not confirmed."""
    files: dict[str, bytes] = {
        "summary.json": _json(dict(inputs.summary)),
        "config.json": _json(_strip_secrets(dict(inputs.config))),
        "health.json": _json(dict(inputs.health)),
        "traces.json": _json(inputs.traces[-settings.debug_bundle_trace_turns :]),
        "crashes.json": _json([{k: v for k, v in r.items() if k != "minidump"} for r in inputs.crash_records]),
        "egress.json": _json(inputs.ledger),
        "installed.json": _json({"packs": sorted(inputs.packs), "models": sorted(inputs.models)}),
        "problem_reports.json": _json(_problem_reports(inputs.problem_reports)),
    }
    files.update(_logs(inputs.log_dir, inputs.now_ms - int(settings.debug_bundle_log_days * DAY_MS)))
    if conversation_session is not None:
        if shares is None or not shares.confirmed(conversation_session):
            raise BundleRefused("student_confirmation_required",
                                "the student has not confirmed this session for support")  # fmt: skip
        files[CONVERSATION] = _json({"session_id": conversation_session,
                                     "payloads": inputs.conversations(conversation_session)})  # fmt: skip
    files["manifest.json"] = _json({"files": {name: hashlib.sha256(data).hexdigest()
                                              for name, data in sorted(files.items())}})  # fmt: skip
    return files


def preview(files: Mapping[str, bytes]) -> list[dict[str, Any]]:
    """What the adult sees before saving: every file with its size."""
    return [{"name": name, "bytes": len(data)} for name, data in sorted(files.items())]


def zip_bytes(files: Mapping[str, bytes]) -> bytes:
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w", compression=zipfile.ZIP_DEFLATED) as archive:
        for name, data in sorted(files.items()):
            info = zipfile.ZipInfo(name, date_time=(2026, 1, 1, 0, 0, 0))  # stable bytes for the same content
            archive.writestr(info, data)
    return buffer.getvalue()


def save_bundle(files: Mapping[str, bytes], folder: Path, settings: Settings, passphrase: str | None = None,
                shares: SupportShares | None = None) -> Path:  # fmt: skip
    """Write the bundle into the adult's chosen folder. With the conversation inside, a passphrase is required and
    the bundle is sealed with it; the student's included-session notice is then set."""
    has_conversation = CONVERSATION in files
    if has_conversation and not passphrase:
        raise BundleRefused("passphrase_required", "a bundle with a conversation is encrypted with a passphrase")
    data = zip_bytes(files)
    stamp = time.strftime("%Y%m%d-%H%M%S", time.gmtime())
    if passphrase:
        data = seal_with_passphrase(
            data, passphrase, settings.passphrase_argon2_memory_kib, settings.passphrase_argon2_iterations
        )
        path = folder / f"jana-support-report-{stamp}{EXTENSION_SEALED}"
    else:
        path = folder / f"jana-support-report-{stamp}{EXTENSION_PLAIN}"
    path.write_bytes(data)
    if has_conversation and shares is not None:
        session = json.loads(files[CONVERSATION])["session_id"]
        shares.mark_included(session)
    return path
