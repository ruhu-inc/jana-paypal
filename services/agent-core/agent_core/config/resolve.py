"""Resolve configuration from its layers (ARC-27) with every value validated against its declaration (ARC-28).

A layer may set only the keys declared for it; a later layer never relaxes a restriction from a higher-authority
one; the safety layer only restricts; development overrides are ignored in release builds. The resolved
configuration is frozen for a turn and carries the sha256 recorded in each trace (OBS-03)."""

from __future__ import annotations

import hashlib
import json
import logging
import os
import tomllib
from collections.abc import Mapping
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Literal

from pydantic import TypeAdapter, ValidationError

from agent_core.config.keys import Settings, annotated, bounds, declaration
from agent_core.config.layers import Layer

log = logging.getLogger(__name__)
Build = Literal["development", "release"]
ENV_PREFIX = "JANA_"


class ConfigError(ValueError):
    """A configuration source names an unknown key, an out-of-range value or a key its layer may not set."""


@dataclass(frozen=True)
class Source:
    layer: Layer
    origin: str  # file path, "environment" or a store name, for error messages
    values: Mapping[str, Any]


@dataclass(frozen=True)
class Resolved:
    settings: Settings
    provenance: dict[str, Layer]
    ignored: tuple[str, ...] = field(default_factory=tuple)  # restrict-only entries that would have relaxed

    @property
    def sha256(self) -> str:
        canonical = json.dumps(self.settings.model_dump(mode="json"), sort_keys=True, separators=(",", ":"))
        return hashlib.sha256(canonical.encode()).hexdigest()


def _check(source: Source, name: str, value: Any) -> Any:
    if name not in Settings.model_fields:
        raise ConfigError(f"{source.origin}: unknown key {name!r}")
    if int(source.layer) not in declaration(name)["layers"]:
        raise ConfigError(f"{source.origin}: key {name!r} cannot be set by the {source.layer.name.lower()} layer")
    try:
        return TypeAdapter(annotated(name)).validate_python(value)
    except ValidationError:
        raise ConfigError(f"{source.origin}: key {name!r} = {value!r} is out of range ({bounds(name)})") from None


def stricter_or_equal(name: str, new: Any, old: Any) -> bool:
    rule = declaration(name)["restrict"]
    if rule == "false":
        return new is False or new == old
    if rule == "lower":
        return bool(new <= old)
    if rule == "higher":
        return bool(new >= old)
    if isinstance(rule, list):
        return rule.index(new) <= rule.index(old)
    return False  # a key with no restriction direction cannot be changed by a restrict-only layer


def resolve(sources: list[Source], build: Build) -> Resolved:
    values = {name: f.default for name, f in Settings.model_fields.items()}
    provenance = dict.fromkeys(values, Layer.DEFAULTS)
    ignored = []
    for source in sorted(sources, key=lambda s: s.layer):
        if source.layer is Layer.DEVELOPER and build == "release":
            continue  # release builds ignore JANA_* and dev.toml (ARC-27)
        for name, raw in source.values.items():
            value = _check(source, name, raw)
            if source.layer is Layer.SAFETY and not stricter_or_equal(name, value, values[name]):
                message = f"{source.origin}: safety entry {name}={value!r} would relax {values[name]!r}; ignored"
                log.warning(message)
                ignored.append(message)
                continue
            values[name], provenance[name] = value, source.layer
    return Resolved(Settings(**values), provenance, tuple(ignored))


def toml_source(layer: Layer, path: Path) -> Source:
    return Source(layer, str(path), tomllib.loads(path.read_text()))


def environment_source(environ: Mapping[str, str] = os.environ) -> Source:
    """JANA_<KEY>=<TOML value> developer overrides (development builds only)."""
    values = {}
    for variable, raw in environ.items():
        if variable.startswith(ENV_PREFIX):
            name = variable.removeprefix(ENV_PREFIX).lower()
            try:
                values[name] = tomllib.loads(f"v = {raw}")["v"]
            except tomllib.TOMLDecodeError:
                values[name] = raw
    return Source(Layer.DEVELOPER, "environment", values)


def stage_source(environ: Mapping[str, str] = os.environ) -> Source:
    """The developer layer's `JANA_BUILD_STAGE`, alone: how a development build runs an earlier stage's flows (S1
    stays testable); `resolve` drops it in a release build like every developer override (ARC-27)."""
    raw = environ.get(f"{ENV_PREFIX}BUILD_STAGE")
    values: dict[str, Any] = {"build_stage": raw.strip()} if raw else {}
    # M42 and the local tutor's lean turns (W3) are explicit development previews; do not turn on other developer
    # environment overrides here.
    previews = environment_source(environ).values
    for name in ("study_workspace_enabled", "cloud_voice_enabled", "offline_lean_classes", "cloud_catalogue_file"):
        if previews.get(name) is not None:
            values[name] = previews[name]
    return Source(Layer.DEVELOPER, "environment", values)


def app_settings(build: Build, environ: Mapping[str, str] = os.environ) -> Settings:
    """The core's configuration at bring-up: the declared defaults with the developer stage override, which only a
    development build honours (ARC-27)."""
    return resolve([stage_source(environ)], build).settings


@dataclass(frozen=True)
class WriteResult:
    ok: bool
    reason: str | None = None  # a locked reason code for the UI (keyed copy, never a literal sentence)


def check_write(name: str, value: Any, layer: Layer) -> WriteResult:
    """Whether a settings write from `layer` may change `name` (ARC-27, D53, D86). An online-feature switch is not a
    profile setting: it is written only through the online-feature store with its consent (ARC-11)."""
    try:
        _check(Source(layer, "write", {}), name, value)
    except ConfigError as error:
        decl = declaration(name) if name in Settings.model_fields else {}
        if layer is Layer.PROFILE and int(Layer.ONLINE_FEATURES) in decl.get("layers", []):
            return WriteResult(False, "online_feature_switch")
        return WriteResult(False, "not_settable" if "cannot be set" in str(error) else "validation_failed")
    return WriteResult(True)
