"""Structured logs and redaction at the writer (OBS-11, OBS-12, OBS-19, SEC-30).

Every line is JSON with `ts`, `level`, `process`, `epoch`, `component`, `event`, correlation ids and typed fields.
Redaction runs before a line is written: secrets never appear; student text, model text and tool values are reduced
to a code or a hash; home directories become `~`. Files rotate per process at a size cap, keeping a fixed count.

Free-form lines (a process's stderr, the Python logging of libraries, a crash record's log tail) are allow-listed,
not redacted (OBS-16, OBS-19, PRV-09): a line is kept only when every plain word in it is in the diagnostic
vocabulary (`diagnostic_words.json`, shared with the shell); identifiers, numbers, paths and hex stay. Any other line
is withheld, and a run of withheld lines leaves only its count. The core's own logging writes the code's message
templates, never their arguments, and an exception as its type and frames, never its message, so neither a request
body nor a prompt or answer can reach stderr."""

from __future__ import annotations

import hashlib
import json
import logging
import os
import re
import sys
import time
import traceback
from collections.abc import Iterable
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

POLICY_VERSION = "1.0"
LEVELS = ("debug", "info", "warning", "error")
CORRELATION = ("session_id", "turn_id", "request_id", "call_id", "delegation_id", "check_id")
# Field names whose values are never written (secrets) or written only as a hash (learner and model content).
SECRET_FIELDS = re.compile(r"(token|secret|password|passwd|api_?key|pin|licence_code|license_code|authorization)", re.I)
CONTENT_FIELDS = re.compile(
    r"(text|message|content|prompt|completion|answer|arguments|args|result|latex|say|"
    r"display_name|name_of_student)$",
    re.I,
)
SECRET_VALUES = re.compile(
    r"(sk-[A-Za-z0-9_-]{10,}|Bearer\s+[A-Za-z0-9._-]{8,}|eyJ[A-Za-z0-9_-]{10,}\.[A-Za-z0-9_-]{5,})"
)
HOME = str(Path.home())
LINE_MAX = 300
DIAGNOSTIC_WORDS = frozenset(json.loads(Path(__file__).with_name("diagnostic_words.json").read_text())["words"])
WITHHELD = re.compile(r"^\[withheld \d+ lines?\]$")
_PART = re.compile(r"[^A-Za-z0-9_]+")
_CAMEL = re.compile(r"[a-z][A-Z]")
_DOTTED = re.compile(r"[A-Za-z0-9_]\.[A-Za-z_]")


def redact_value(key: str, value: Any) -> Any:
    if SECRET_FIELDS.search(key):
        return "[redacted]"
    if isinstance(value, dict):
        return {k: redact_value(k, v) for k, v in value.items()}
    if isinstance(value, list):
        return [redact_value(key, v) for v in value]
    if isinstance(value, str):
        if CONTENT_FIELDS.search(key):
            return "sha256:" + hashlib.sha256(value.encode()).hexdigest()[:16]
        value = SECRET_VALUES.sub("[redacted]", value)
        return value.replace(HOME, "~") if HOME and HOME != "/" else value
    return value


@dataclass
class JsonLogger:
    directory: Path
    process: str
    epoch: str
    level: str = "info"
    max_bytes: int = 5 * 1024 * 1024
    keep: int = 5
    fields: dict[str, Any] = field(default_factory=dict)

    @property
    def path(self) -> Path:
        return self.directory / f"{self.process}.jsonl"

    def log(self, level: str, component: str, event: str, **values: Any) -> None:
        if LEVELS.index(level) < LEVELS.index(self.level):
            return
        line: dict[str, Any] = {
            "ts": int(time.time() * 1000),
            "level": level,
            "process": self.process,
            "epoch": self.epoch,
            "component": component,
            "event": event,
        }
        for key, value in {**self.fields, **values}.items():
            line[key] = value if key in CORRELATION else redact_value(key, value)
        self._write(json.dumps(line, ensure_ascii=False, sort_keys=False))

    def _write(self, text: str) -> None:
        self.directory.mkdir(parents=True, exist_ok=True)
        if self.path.exists() and self.path.stat().st_size + len(text) + 1 > self.max_bytes:
            self._rotate()
        with self.path.open("a", encoding="utf-8") as handle:
            handle.write(text + "\n")

    def _rotate(self) -> None:
        for n in range(self.keep - 1, 0, -1):
            older = self.directory / f"{self.process}.{n}.jsonl"
            if older.exists():
                if n + 1 >= self.keep:
                    older.unlink()
                else:
                    os.replace(older, self.directory / f"{self.process}.{n + 1}.jsonl")
        os.replace(self.path, self.directory / f"{self.process}.1.jsonl")

    def files(self) -> list[Path]:
        return sorted(self.directory.glob(f"{self.process}*.jsonl"))


# --- free-form lines: allow-listed, never redacted (OBS-16, OBS-19) --------------------------------------------------


def diagnostic(line: str) -> bool:
    """Whether a free-form line holds diagnostics only: every plain word is in the diagnostic vocabulary. Paths,
    numbers, hex, dotted names, identifiers (with a digit, an underscore or inner capitals) and single letters other
    than "I" do not count as words."""
    for chunk in line.split():
        if "/" in chunk or "\\" in chunk or _DOTTED.search(chunk):
            continue  # a path, a file or a module name; the home folder is shortened when the line is kept
        for part in _PART.split(chunk):
            if not part or any(c.isdigit() or c == "_" for c in part) or _CAMEL.search(part):
                continue
            if len(part) == 1 and part not in "Ii":
                continue
            if part.lower() not in DIAGNOSTIC_WORDS:
                return False
    return True


def keep_line(line: str) -> str:
    """A kept line: secrets removed, home folders shortened, length capped."""
    text = SECRET_VALUES.sub("[redacted]", line.rstrip("\n"))
    if HOME and HOME != "/":
        text = text.replace(HOME, "~")
    return text[:LINE_MAX]


def diagnostic_lines(lines: Iterable[str]) -> list[str]:
    """Free-form lines as a crash record or a log may hold them: diagnostic lines kept (secrets removed, home folders
    shortened, capped), every run of other lines replaced by `[withheld N lines]`."""
    out: list[str] = []
    withheld = 0
    for raw in lines:
        line = raw.rstrip("\n")
        if WITHHELD.match(line.strip()):
            withheld += int(re.sub(r"\D", "", line) or 0)
            continue
        if diagnostic(line):
            if withheld:
                out.append(f"[withheld {withheld} line{'s' if withheld != 1 else ''}]")
                withheld = 0
            out.append(keep_line(line))
        else:
            withheld += 1
    if withheld:
        out.append(f"[withheld {withheld} line{'s' if withheld != 1 else ''}]")
    return out


class DiagnosticFormatter(logging.Formatter):
    """The core's logging on stderr: the level, the logger and the code's message template (never its arguments,
    which may quote a request body, a prompt or an answer), and an exception as its type and frames (never its
    message: a validation error quotes the input). The whole goes through the diagnostic allow-list."""

    def format(self, record: logging.LogRecord) -> str:
        lines = [f"{record.levelname} {record.name}: {record.msg if isinstance(record.msg, str) else ''}"]
        if record.exc_info and record.exc_info[0] is not None:
            kind, _, tb = record.exc_info
            lines.append(f"{kind.__name__} (message withheld)")
            lines += [f"  at {frame.filename}:{frame.lineno} in {frame.name}" for frame in traceback.extract_tb(tb)]
        return "\n".join(diagnostic_lines("\n".join(lines).splitlines()))


def diagnostic_logging(level: int = logging.WARNING) -> None:
    """Route every Python log record of this process (the core's own, uvicorn's, asyncio's, warnings) through one
    stderr handler with the diagnostic formatter, so no log line quotes student or model text."""
    handler = logging.StreamHandler(sys.stderr)
    handler.setFormatter(DiagnosticFormatter())
    root = logging.getLogger()
    root.handlers[:] = [handler]
    root.setLevel(level)
    for name in ("uvicorn", "uvicorn.error", "uvicorn.access", "asyncio", "fastapi", "httpx"):
        logger = logging.getLogger(name)
        logger.handlers[:] = []
        logger.propagate = True
    logging.getLogger("uvicorn.access").disabled = True  # request lines carry paths and queries, never needed
    logging.captureWarnings(True)


def print_exception(kind: type[BaseException], tb: Any) -> None:
    """An uncaught exception on stderr as its type and frames only (the default hook prints its message)."""
    lines = [f"fatal exception {kind.__name__} (message withheld)"]
    lines += [f"  at {frame.filename}:{frame.lineno} in {frame.name}" for frame in traceback.extract_tb(tb)]
    print("\n".join(diagnostic_lines(lines)), file=sys.stderr)
