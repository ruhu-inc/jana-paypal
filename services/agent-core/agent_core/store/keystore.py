"""Device secrets by reference (SEC-17, DAT-02, D97).

Device secrets (the device database key, the service credential and its id) are held by reference, never in the app
data directory. In the app the shell keeps them in the OS keychain (macOS Keychain, Windows Credential Manager, the
Linux Secret Service) and hands them over the control channel: `ShellKeyStore` starts from the shell's
`control.keys` and asks the shell to store or delete a secret with `control.key_put` and `control.key_delete`; the
core never reads or writes the keychain or a key file itself. Where no keychain service exists, the shell writes the
SEC-17 file fallback, one file per secret, mode 0600, outside the app data directory, in the layout `FileKeyStore`
reads, which the uninstall command uses to clear it (PLT-24). `MemoryKeyStore` serves tests and development builds
only; a release core never runs on it."""

from __future__ import annotations

import base64
import os
import re
from collections.abc import Callable, Mapping
from pathlib import Path
from typing import Any, Literal, Protocol

_REF = re.compile(r"^[a-z][a-z0-9._-]{2,80}$")
INSTALL_MARKER = "jana-meta.install"
StoreKind = Literal["os_keychain", "user_file"]


class KeyStore(Protocol):
    def get(self, ref: str) -> bytes | None: ...
    def put(self, ref: str, secret: bytes) -> None: ...
    def delete(self, ref: str) -> None: ...


class MemoryKeyStore:
    def __init__(self) -> None:
        self.secrets: dict[str, bytes] = {}

    def get(self, ref: str) -> bytes | None:
        return self.secrets.get(ref)

    def put(self, ref: str, secret: bytes) -> None:
        self.secrets[ref] = secret

    def delete(self, ref: str) -> None:
        self.secrets.pop(ref, None)

    def refs(self) -> list[str]:
        """Every held reference (uninstall deletes them all, PLT-24)."""
        return sorted(self.secrets)


class FileKeyStore:
    def __init__(self, root: Path, data_dir: Path | None = None) -> None:
        if data_dir is not None and root.resolve().is_relative_to(data_dir.resolve()):
            raise ValueError("the key store must be outside the app data directory (SEC-17)")
        self.root = root
        root.mkdir(parents=True, exist_ok=True, mode=0o700)
        os.chmod(root, 0o700)

    def _path(self, ref: str) -> Path:
        if not _REF.match(ref):
            raise ValueError("not a key reference")
        return self.root / ref

    def get(self, ref: str) -> bytes | None:
        path = self._path(ref)
        return path.read_bytes() if path.is_file() else None

    def put(self, ref: str, secret: bytes) -> None:
        path = self._path(ref)
        tmp = path.with_name(path.name + ".tmp")
        fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
        with os.fdopen(fd, "wb") as handle:
            handle.write(secret)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(tmp, path)

    def delete(self, ref: str) -> None:
        self._path(ref).unlink(missing_ok=True)

    def refs(self) -> list[str]:
        """Every held reference (uninstall deletes them all, PLT-24)."""
        return sorted(p.name for p in self.root.iterdir() if p.is_file() and _REF.match(p.name))


def encode_secret(secret: bytes) -> str:
    """A secret as the control channel carries it: base64url without padding."""
    return base64.urlsafe_b64encode(secret).rstrip(b"=").decode("ascii")


def decode_secret(value: str) -> bytes:
    return base64.urlsafe_b64decode(value + "=" * (-len(value) % 4))


class ShellKeyStore:
    """The shell's keychain bridge on the control channel (SEC-17, D97).

    Holds the secrets the shell sent in `control.keys` for this run; `put` and `delete` take effect here at once and
    ask the shell to persist them (`control.key_put`, `control.key_delete`). The shell answers each with
    `control.key_result`; a failure keeps the value for this run and makes the keychain health component degraded.
    Values are never logged and never appear in `repr`."""

    def __init__(self, store: StoreKind, entries: Mapping[str, bytes], send: Callable[[dict[str, Any]], None]) -> None:
        self.store: StoreKind = store
        self._secrets: dict[str, bytes] = dict(entries)
        self._send = send
        self.failed: set[str] = set()  # refs the shell could not persist this run

    @classmethod
    def from_control(cls, message: Mapping[str, Any], send: Callable[[dict[str, Any]], None]) -> ShellKeyStore:
        """From a validated `control.keys` frame."""
        entries = {str(e["ref"]): decode_secret(str(e["value"])) for e in message["entries"]}
        return cls(message["store"], entries, send)

    def __repr__(self) -> str:
        return f"ShellKeyStore(store={self.store!r}, refs={sorted(self._secrets)!r})"

    @property
    def os_keychain(self) -> bool:
        """The health panel's keychain line: ok only while every secret sits in the OS keychain (OBS-15)."""
        return self.store == "os_keychain" and not self.failed

    @staticmethod
    def _check(ref: str) -> str:
        if not _REF.match(ref) or not ref.startswith("jana-meta."):
            raise ValueError("not a key reference")
        return ref

    def get(self, ref: str) -> bytes | None:
        return self._secrets.get(self._check(ref))

    def put(self, ref: str, secret: bytes) -> None:
        self._secrets[self._check(ref)] = secret
        self._send({"type": "control.key_put", "ref": ref, "value": encode_secret(secret)})

    def delete(self, ref: str) -> None:
        self._secrets.pop(self._check(ref), None)
        self._send({"type": "control.key_delete", "ref": ref})

    def refs(self) -> list[str]:
        """Every held reference (uninstall deletes them all, PLT-24)."""
        return sorted(self._secrets)

    def on_result(self, message: Mapping[str, Any]) -> None:
        """The shell's `control.key_result`: where the secret now is, and whether it was persisted."""
        ref = str(message["ref"])
        self.store = message["store"]
        if message["ok"]:
            self.failed.discard(ref)
        else:
            self.failed.add(ref)
