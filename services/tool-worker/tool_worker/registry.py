"""The worker's static function table (TOL-05: one static list, no discovery). Each function has a version and a
JSON Schema for its arguments, shipped with the worker; `schema_sha256` lets the host hide a function whose schema
differs from its own registry (TOL-24, TOL-06)."""

from __future__ import annotations

import hashlib
import json
from collections.abc import Callable
from dataclasses import dataclass
from importlib import resources
from typing import Any

from jsonschema import Draft202012Validator

# A function returns (value, extras); extras may carry latex, working, warnings, method and confidence.
Implementation = Callable[[dict[str, Any], "CallContext"], tuple[Any, dict[str, Any]]]


@dataclass(frozen=True)
class CallContext:
    call_id: str
    check_id: str
    conventions: dict[str, Any]
    limits: dict[str, int]
    cancelled: Callable[[], bool]
    seed: int = 0
    settings: dict[str, Any] | None = None

    def setting(self, name: str, default: Any) -> Any:
        return (self.settings or {}).get(name, default)


@dataclass(frozen=True)
class Function:
    name: str
    version: int
    schema: dict[str, Any]
    implementation: Implementation

    @property
    def schema_sha256(self) -> str:
        canonical = json.dumps(self.schema, sort_keys=True, separators=(",", ":"), ensure_ascii=True)
        return hashlib.sha256(canonical.encode()).hexdigest()

    def validator(self) -> Draft202012Validator:
        return Draft202012Validator(self.schema)


def load_schema(name: str) -> dict[str, Any]:
    """A function's argument schema from the package data (tool_worker/schemas/<name>.json)."""
    text = resources.files("tool_worker").joinpath("schemas", f"{name}.json").read_text()
    schema: dict[str, Any] = json.loads(text)
    return schema
