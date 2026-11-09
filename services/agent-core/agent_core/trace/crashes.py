"""Local crash capture (OBS-16): a crash record for every process, encrypted minidumps, bounded retention.

What is captured, in every build:

- **Core and worker**: uncaught exceptions (`sys.excepthook`, `threading.excepthook`, the event loop's handler) and
  fatal errors (`faulthandler`, written to a per-process file and turned into a record at the next start).
- **Shell**: Rust panics and fatal signals; the shell's panic hook writes a record into `crashes/inbox/` and the core
  takes it in (`ingest_inbox`).
- **Runtime**: llama-server exits, with the exit code and the last `crash_stderr_tail_lines` lines of its stderr.
- **Worker limits**: a worker killed for a limit, with the tool name and the limit.

A record holds the process, the app version, a stack (Python frames as file, line and function; Rust frames as module
and offset), correlation ids and a log tail of diagnostic lines only (the allow-list of `trace/logs.py`: a line with
any word outside the diagnostic vocabulary is withheld and counted); never local variables, memory, student text,
prompts, answers or secrets.
Raw memory stays in a minidump only: written encrypted under the device database key (AES-256-GCM; TABLE SEC-32a),
with every keychain-derived secret the process holds zeroised in the dump before it is sealed, deleted after
`crash_minidump_retention_days`, and never put in a debug bundle or a crash report. Records are kept
`crash_record_retention_days` and listed in the health panel.
"""

from __future__ import annotations

import faulthandler
import json
import os
import re
import secrets
import sys
import threading
import time
import traceback
from collections.abc import Callable, Iterable, Mapping
from dataclasses import dataclass
from pathlib import Path
from types import TracebackType
from typing import Any

from cryptography.hazmat.primitives.ciphers.aead import AESGCM

from agent_core.config import Settings
from agent_core.store.ids import new_ulid
from agent_core.store.keystore import KeyStore
from agent_core.trace.logs import HOME, SECRET_VALUES, diagnostic_lines, print_exception

DEVICE_KEY_REF = "jana-meta.device-db"  # the device database key (TABLE SEC-32a), held by reference (SEC-17)
DUMP_MAGIC = b"JANADMP1"
PROCESSES = ("shell", "core", "worker", "runtime")
KINDS = ("panic", "signal", "exception", "fatal_error", "runtime_exit", "worker_limit")
DAY_MS = 86_400_000
LINE_MAX = 300
CORRELATION = ("epoch", "session_id", "turn_id", "request_id", "call_id")
FAULT_FRAME = re.compile(r'File "([^"]+)", line (\d+) in (\S+)')  # a frame as faulthandler writes it


def device_key(keys: KeyStore) -> bytes:
    """The device database key, created once (32 random bytes) and kept in the key store, never in the data folder."""
    key = keys.get(DEVICE_KEY_REF)
    if key is None:
        key = secrets.token_bytes(32)
        keys.put(DEVICE_KEY_REF, key)
    return key


def redact_line(line: str) -> str:
    """A log or stderr line as a crash record may hold it: secrets removed, home folders shortened, length capped."""
    text = SECRET_VALUES.sub("[redacted]", line.rstrip("\n"))
    if HOME and HOME != "/":
        text = text.replace(HOME, "~")
    return text[:LINE_MAX]


def python_stack(tb: TracebackType | None) -> list[dict[str, Any]]:
    """Frames as file (shortened), line and function: no local variables and no source text."""
    frames = []
    for frame in traceback.extract_tb(tb):
        frames.append({"file": redact_line(frame.filename), "line": frame.lineno, "function": frame.name})
    return frames


@dataclass
class CrashStore:
    """`folder` is the data folder's `crashes/`."""

    folder: Path
    key: bytes
    app_version: str
    settings: Settings
    clock_ms: Callable[[], int] = lambda: int(time.time() * 1000)

    def __post_init__(self) -> None:
        for sub in ("records", "minidumps", "inbox"):
            (self.folder / sub).mkdir(parents=True, exist_ok=True)

    # --- records ------------------------------------------------------------------------------------------------

    def record(self, process: str, kind: str, stack: list[dict[str, Any]], *, error_type: str | None = None,
               correlation: Mapping[str, Any] | None = None, log_tail: Iterable[str] = (),
               detail: Mapping[str, Any] | None = None, version: str | None = None) -> dict[str, Any]:  # fmt: skip
        """Write one crash record (the exception's message is not kept: it may quote student text)."""
        if process not in PROCESSES or kind not in KINDS:
            raise ValueError(f"unknown crash {process}/{kind}")
        now = self.clock_ms()
        # Only diagnostic lines: a runtime's or a process's stderr may echo a prompt or an answer (OBS-16, OBS-19).
        tail = diagnostic_lines(str(line) for line in log_tail)[-self.settings.crash_stderr_tail_lines :]
        record = {"crash_id": new_ulid(now), "at_ms": now, "process": process, "kind": kind,
                  "version": version or self.app_version, "error_type": error_type, "stack": stack,
                  "correlation": {k: str(v) for k, v in (correlation or {}).items() if k in CORRELATION},
                  "log_tail": tail, "detail": dict(detail or {}), "minidump": None}  # fmt: skip
        self._write(record)
        return record

    def _write(self, record: Mapping[str, Any]) -> None:
        path = self.folder / "records" / f"{record['crash_id']}.json"
        temp = path.with_name(path.name + ".tmp")
        temp.write_text(json.dumps(record, sort_keys=True), encoding="utf-8")
        os.replace(temp, path)

    def records(self) -> list[dict[str, Any]]:
        """Every kept record, newest first (the health panel)."""
        found = []
        for path in sorted((self.folder / "records").glob("*.json"), reverse=True):
            try:
                found.append(json.loads(path.read_text(encoding="utf-8")))
            except (OSError, ValueError):
                continue
        return found

    def record_exception(self, process: str, error: BaseException, correlation: Mapping[str, Any] | None = None,
                         log_tail: Iterable[str] = ()) -> dict[str, Any]:  # fmt: skip
        return self.record(process, "exception", python_stack(error.__traceback__), error_type=type(error).__name__,
                           correlation=correlation, log_tail=log_tail)  # fmt: skip

    def record_runtime_exit(self, exit_code: int, stderr_lines: Iterable[str], model: str | None = None,
                            correlation: Mapping[str, Any] | None = None) -> dict[str, Any]:  # fmt: skip
        return self.record("runtime", "runtime_exit", [], correlation=correlation, log_tail=stderr_lines,
                           detail={"exit_code": exit_code, "model": model})  # fmt: skip

    def record_worker_kill(self, tool: str, limit: str, correlation: Mapping[str, Any] | None = None
                           ) -> dict[str, Any]:  # fmt: skip
        return self.record("worker", "worker_limit", [], correlation=correlation, detail={"tool": tool, "limit": limit})

    # --- other processes' records and fatal errors --------------------------------------------------------------

    def fault_file(self, process: str) -> Path:
        return self.folder / f"fault-{process}.log"

    def ingest_inbox(self) -> list[dict[str, Any]]:
        """Take in records other processes wrote (the shell's panic hook); each is checked and re-written here."""
        taken = []
        for path in sorted((self.folder / "inbox").glob("*.json")):
            try:
                raw = json.loads(path.read_text(encoding="utf-8"))
                stack = [{"module": redact_line(str(f.get("module", ""))), "offset": str(f.get("offset", ""))}
                         for f in raw.get("stack", [])][:200]  # fmt: skip
                taken.append(self.record(str(raw["process"]), str(raw["kind"]), stack,
                                         error_type=raw.get("error_type"), correlation=raw.get("correlation"),
                                         log_tail=raw.get("log_tail", []), version=raw.get("version")))  # fmt: skip
            except (OSError, ValueError, KeyError, TypeError, AttributeError):
                pass  # an unreadable record is dropped, never shown half-parsed
            path.unlink(missing_ok=True)
        return taken

    def ingest_faults(self) -> list[dict[str, Any]]:
        """A fatal error `faulthandler` wrote in an earlier run becomes a record at this start."""
        taken = []
        for process in ("core", "worker"):
            path = self.fault_file(process)
            if not path.is_file() or path.stat().st_size == 0:
                continue
            text = path.read_text(errors="replace")
            frames = [{"file": redact_line(m.group(1)), "line": int(m.group(2)), "function": m.group(3)}
                      for m in FAULT_FRAME.finditer(text)]  # fmt: skip
            taken.append(self.record(process, "fatal_error", frames))
            path.write_bytes(b"")
        return taken

    # --- minidumps ------------------------------------------------------------------------------------------------

    def store_minidump(self, crash_id: str, raw: bytes, held_secrets: Iterable[bytes]) -> Path:
        """Seal a minidump: every held keychain-derived secret is zeroised in it first, then it is encrypted under
        the device key. The plaintext never touches the disk."""
        scrubbed = bytearray(raw)
        for secret in held_secrets:
            if not secret:
                continue
            at = scrubbed.find(secret)
            while at != -1:
                scrubbed[at : at + len(secret)] = bytes(len(secret))
                at = scrubbed.find(secret, at + len(secret))
        nonce = secrets.token_bytes(12)
        header = DUMP_MAGIC + crash_id.encode()
        sealed = DUMP_MAGIC + nonce + AESGCM(self.key).encrypt(nonce, bytes(scrubbed), header)
        path = self.folder / "minidumps" / f"{crash_id}.dmp.enc"
        path.write_bytes(sealed)
        os.chmod(path, 0o600)
        return path

    def read_minidump(self, path: Path) -> bytes:
        """For a developer on this device, with the device key: the sealed dump opened."""
        blob = path.read_bytes()
        if not blob.startswith(DUMP_MAGIC):
            raise ValueError("not a sealed minidump")
        crash_id = path.name.split(".", 1)[0]
        nonce = blob[len(DUMP_MAGIC) : len(DUMP_MAGIC) + 12]
        return AESGCM(self.key).decrypt(nonce, blob[len(DUMP_MAGIC) + 12 :], DUMP_MAGIC + crash_id.encode())

    # --- retention ----------------------------------------------------------------------------------------------

    def prune(self) -> int:
        """Delete records past their retention and minidumps past theirs; returns how many files went."""
        now = self.clock_ms()
        removed = 0
        for path in (self.folder / "records").glob("*.json"):
            try:
                at = int(json.loads(path.read_text(encoding="utf-8"))["at_ms"])
            except (OSError, ValueError, KeyError, TypeError):
                at = 0  # an unreadable record is past any retention
            if now - at > self.settings.crash_record_retention_days * DAY_MS:
                path.unlink(missing_ok=True)
                removed += 1
        for path in (self.folder / "minidumps").glob("*.dmp.enc"):
            if now - int(path.stat().st_mtime * 1000) > self.settings.crash_minidump_retention_days * DAY_MS:
                path.unlink(missing_ok=True)
                removed += 1
        return removed

    # --- hooks ----------------------------------------------------------------------------------------------------

    def install_hooks(self, process: str, correlation: Callable[[], Mapping[str, Any]] = dict) -> None:
        """Uncaught exceptions on any thread become records; fatal errors go to `faulthandler`'s file."""
        previous = sys.excepthook

        def on_exception(kind: type[BaseException], error: BaseException, tb: TracebackType | None) -> None:
            if not isinstance(error, KeyboardInterrupt):
                self.record(process, "exception", python_stack(tb), error_type=kind.__name__,
                            correlation=correlation())  # fmt: skip
            if previous is sys.__excepthook__:
                print_exception(kind, tb)  # type and frames only: the default hook would print the message
            else:
                previous(kind, error, tb)

        def on_thread(args: threading.ExceptHookArgs) -> None:
            if args.exc_value is not None:
                self.record(process, "exception", python_stack(args.exc_traceback), error_type=args.exc_type.__name__,
                            correlation=correlation())  # fmt: skip

        sys.excepthook = on_exception
        threading.excepthook = on_thread
        handle = self.fault_file(process).open("ab")
        faulthandler.enable(file=handle, all_threads=True)
        self._fault_handle = handle  # kept open for the life of the process
