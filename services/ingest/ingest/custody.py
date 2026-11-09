"""Where the ingest reads its secrets: the bundle storage key and each staff member's bearer token.

Every environment reads the secret manager's mount (one file per secret, named after it, in `secrets_dir`); a mount
inside a git working tree is refused, so a secret is never read from or left in the repository. The `dev` environment
also reads `JANA_DEV_<NAME>` from the environment. A `Secret` never shows its value in `repr`, `str` or formatting."""

from __future__ import annotations

import base64
import os
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Protocol

from ingest.config import IngestConfig


class Secret:
    __slots__ = ("_value",)

    def __init__(self, value: str) -> None:
        if not value:
            raise ValueError("empty secret")
        self._value = value

    def reveal(self) -> str:
        return self._value

    def __repr__(self) -> str:
        return "Secret(<redacted>)"

    __str__ = __repr__

    def __format__(self, spec: str) -> str:
        return repr(self)

    def __reduce__(self) -> Any:
        raise TypeError("a Secret cannot be pickled")


class SecretSource(Protocol):
    def get(self, name: str) -> Secret | None: ...


def _inside_git_tree(path: Path) -> bool:
    return any((parent / ".git").exists() for parent in (path.resolve(), *path.resolve().parents))


@dataclass(frozen=True)
class MountedSecretFiles:
    directory: Path

    def __post_init__(self) -> None:
        if _inside_git_tree(self.directory):
            raise ValueError("the secrets mount must not be inside a git working tree")

    def get(self, name: str) -> Secret | None:
        if not name.replace("_", "").isalnum():
            return None
        path = self.directory / name
        if not path.is_file():
            return None
        value = path.read_text().strip()
        return Secret(value) if value else None


@dataclass(frozen=True)
class DevEnvironmentSecrets:
    environ: Mapping[str, str]

    def get(self, name: str) -> Secret | None:
        value = self.environ.get(f"JANA_DEV_{name.upper()}", "").strip()
        return Secret(value) if value else None


@dataclass(frozen=True)
class ChainedSecrets:
    sources: tuple[SecretSource, ...]

    def get(self, name: str) -> Secret | None:
        for source in self.sources:
            found = source.get(name)
            if found is not None:
                return found
        return None


def secret_source(config: IngestConfig, environ: Mapping[str, str] | None = None) -> SecretSource:
    sources: list[SecretSource] = []
    if config.environment == "dev":
        sources.append(DevEnvironmentSecrets(os.environ if environ is None else environ))
    if config.secrets_dir is not None:
        sources.append(MountedSecretFiles(config.secrets_dir))
    return ChainedSecrets(tuple(sources))


def storage_key(config: IngestConfig, secrets: SecretSource) -> bytes:
    """The 32-byte AES-GCM key bundles are stored under. The service does not start without it."""
    secret = secrets.get(config.storage_key_secret)
    if secret is None:
        raise RuntimeError(f"the storage key secret {config.storage_key_secret!r} is not available")
    try:
        key = base64.b64decode(secret.reveal(), validate=True)
    except ValueError as error:
        raise RuntimeError("the storage key is not base64") from error
    if len(key) != 32:
        raise RuntimeError("the storage key must be 32 bytes")
    return key
