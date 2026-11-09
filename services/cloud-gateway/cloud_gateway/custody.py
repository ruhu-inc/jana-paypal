"""Provider key custody (SEC-20): where the gateway reads secrets, and the wrapper that keeps them out of logs.

Sources, by environment:

- Every environment reads the secret manager's mount: one file per secret, named after it, in `secrets_dir`
  (for example a Cloud Run or Kubernetes secret volume). A mount inside a git working tree is refused, so a key
  can never be read from (or left in) the repository.
- `dev` also reads the developer's OS keychain: on macOS the Keychain item with service `jana.cloud-gateway` and
  the secret's name as account (`security find-generic-password -s jana.cloud-gateway -a provider_key -w`); on
  Linux the Secret Service (`secret-tool lookup service jana.cloud-gateway account provider_key`).
- `dev` only: a developer's own key from the environment, `JANA_DEV_<NAME>` (for example `JANA_DEV_PROVIDER_KEY`).
  Staging, pilot and production ignore environment keys.

A `Secret` never shows its value in `repr`, `str`, formatting or pickling; code asks for it with `reveal()` at the
one place it is sent (a provider request header) and nowhere else."""

from __future__ import annotations

import os
import platform
import subprocess
from collections.abc import Callable, Mapping
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Protocol

from cloud_gateway.config import GatewayConfig

KEYCHAIN_SERVICE = "jana.cloud-gateway"
Runner = Callable[[list[str]], subprocess.CompletedProcess[str]]


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

    def __eq__(self, other: object) -> bool:
        return isinstance(other, Secret) and other._value == self._value

    def __hash__(self) -> int:
        return hash(("secret", self._value))


class SecretSource(Protocol):
    def get(self, name: str) -> Secret | None: ...


def _inside_git_tree(path: Path) -> bool:
    return any((parent / ".git").exists() for parent in (path.resolve(), *path.resolve().parents))


@dataclass(frozen=True)
class MountedSecretFiles:
    """The secret manager's mount: `<directory>/<name>`, trailing newline stripped."""

    directory: Path

    def __post_init__(self) -> None:
        if _inside_git_tree(self.directory):
            raise ValueError("the secrets mount must not be inside a git working tree (SEC-20)")

    def get(self, name: str) -> Secret | None:
        if not name.replace("_", "").isalnum():
            return None
        path = self.directory / name
        if not path.is_file():
            return None
        value = path.read_text().strip()
        return Secret(value) if value else None


def _run(argv: list[str]) -> subprocess.CompletedProcess[str]:
    return subprocess.run(argv, capture_output=True, text=True, timeout=30, check=False)


@dataclass(frozen=True)
class KeychainSecrets:
    """A developer's OS keychain: macOS Keychain or the Linux Secret Service."""

    service: str = KEYCHAIN_SERVICE
    system: str = platform.system()
    run: Runner = _run

    def get(self, name: str) -> Secret | None:
        if self.system == "Darwin":
            argv = ["security", "find-generic-password", "-s", self.service, "-a", name, "-w"]
        elif self.system == "Linux":
            argv = ["secret-tool", "lookup", "service", self.service, "account", name]
        else:
            return None
        try:
            result = self.run(argv)
        except (OSError, subprocess.SubprocessError):
            return None
        value = result.stdout.strip() if result.returncode == 0 else ""
        return Secret(value) if value else None


@dataclass(frozen=True)
class DevEnvironmentSecrets:
    """A developer's own provider key from the environment (dev builds only, SEC-20, ARC-27)."""

    environ: Mapping[str, str] = field(repr=False)

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


def secret_source(
    config: GatewayConfig, environ: Mapping[str, str] | None = None, keychain: SecretSource | None = None
) -> SecretSource:
    """The sources an environment may read, most specific first."""
    runtime_environ = os.environ if environ is None else environ
    sources: list[SecretSource] = []
    if config.environment == "dev":
        sources.append(DevEnvironmentSecrets(runtime_environ))
        sources.append(keychain if keychain is not None else KeychainSecrets(config.keychain_service))
    if config.secrets_dir is not None:
        sources.append(MountedSecretFiles(config.secrets_dir))
    return ChainedSecrets(tuple(sources))
