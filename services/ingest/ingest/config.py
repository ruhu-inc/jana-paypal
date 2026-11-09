"""Ingest configuration: every key declared once with its default, unit and the requirement it serves.

Proposal values from the specification are defaults here and never literals in code; values the specification does
not set are engineering assumptions, marked as such. A deployment's TOML file is merged over the packaged
`defaults.toml`. Secrets are never configuration values: the storage key and each staff member's token are named
here and read from the secret source (`custody`)."""

from __future__ import annotations

import tomllib
from pathlib import Path
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator

DEFAULTS = Path(__file__).with_name("defaults.toml")
Environment = Literal["dev", "staging", "pilot", "production"]
SupportLevel = Literal["L1", "L2", "L3"]


def key(default: Any, *, serves: str, unit: str | None = None, ge: float | None = None, le: float | None = None) -> Any:
    """A declared key: its default, unit and the requirement it serves."""
    return Field(default, ge=ge, le=le, json_schema_extra={"serves": serves, "unit": unit})


class _Strict(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)


class Limits(_Strict):
    # A crash report holds at most 200 frames and 1,000 log lines of 300 characters (the outbox CrashReport contract).
    max_crash_report_bytes: int = key(524_288, serves="OBS-17", unit="bytes", ge=4096)
    # A telemetry report is closed counters and histograms; series multiply it (engineering assumption).
    max_telemetry_bytes: int = key(1_048_576, serves="OBS-20", unit="bytes", ge=4096)
    # A support bundle: three days of redacted logs and 50 turns of trace metadata (engineering assumption).
    max_bundle_bytes: int = key(52_428_800, serves="OPS-24", unit="bytes", ge=65536)
    # Unzipped, for the re-scan: refuses a decompression bomb before it is expanded (engineering assumption).
    max_bundle_unzipped_bytes: int = key(209_715_200, serves="OPS-24", unit="bytes", ge=65536)
    max_bundle_files: int = key(500, serves="OPS-24", unit="files", ge=1)
    max_admin_body_bytes: int = key(16_384, serves="OPS-24", unit="bytes", ge=1024)
    max_bundles_per_ticket: int = key(5, serves="OPS-24", unit="bundles", ge=1)


class Retention(_Strict):
    # OPS-24: deleted at ticket close or after this many days, whichever is first (proposal: 30 days).
    bundle_days: int = key(30, serves="OPS-24", unit="days", ge=1)
    # OBS-17 and OBS-20 (D99): proposals for the owner to confirm (crash reports 90 days, telemetry 400 days, for the
    # dashboards of OBS-21). Neither holds a personal identifier.
    crash_report_days: int = key(90, serves="OBS-17", unit="days", ge=1)
    telemetry_days: int = key(400, serves="OBS-20", unit="days", ge=1)
    # The access log outlives what it logs, so every opening of a deleted bundle stays accountable; as long as the
    # device keeps its own audit rows (TABLE DAT-08a, 400 days).
    access_log_days: int = key(400, serves="OPS-24", unit="days", ge=1)


class Rates(_Strict):
    """Anonymous endpoints cannot be limited per device (SEC-21: no credential, no identifier), so each kind has one
    service-wide token bucket that protects storage and the operators' attention (engineering assumption)."""

    outbox_per_minute: float = key(600.0, serves="OPS-17", unit="requests/min", ge=1)
    outbox_burst: float = key(200.0, serves="OPS-17", unit="requests", ge=1)
    support_uploads_per_minute: float = key(30.0, serves="OPS-24", unit="requests/min", ge=1)
    support_burst: float = key(10.0, serves="OPS-24", unit="requests", ge=1)


class StaffMember(_Strict):
    """A support or operations person: the secret holding their bearer token, and their support level. Only L3
    opens a bundle with conversation (OPS-24); dashboards read reports at any level."""

    secret: str
    level: SupportLevel


class IngestConfig(_Strict):
    environment: Environment = "production"
    storage_dir: Path = Path("/var/lib/jana-ingest")
    log_path: Path | None = None
    secrets_dir: Path | None = None  # the secret manager's mount: one file per secret, named after it
    # The name of the secret holding the storage key (base64 of 32 random bytes; AES-GCM for stored bundles).
    storage_key_secret: str = "ingest_storage_key"  # noqa: S105 (a name, not a value)
    staff: dict[str, StaffMember] = Field(default_factory=dict)
    limits: Limits = Field(default_factory=Limits)
    retention: Retention = Field(default_factory=Retention)
    rates: Rates = Field(default_factory=Rates)
    purge_interval_s: int = key(3600, serves="OPS-24", unit="s", ge=10)

    @model_validator(mode="after")
    def _consistent(self) -> IngestConfig:
        for name in self.staff:
            if not name.replace("_", "").replace("-", "").replace(".", "").isalnum():
                raise ValueError(f"staff name {name!r} must be letters, digits, '.', '-' or '_'")
        return self


def _merge(base: dict[str, Any], over: dict[str, Any]) -> dict[str, Any]:
    merged = dict(base)
    for name, value in over.items():
        if isinstance(value, dict) and isinstance(merged.get(name), dict) and name != "staff":
            merged[name] = _merge(merged[name], value)
        else:
            merged[name] = value
    return merged


def load_config(path: Path | None = None, **overrides: Any) -> IngestConfig:
    """The packaged defaults, the deployment file over them, then keyword overrides. `staff` is replaced whole."""
    data = tomllib.loads(DEFAULTS.read_text())
    if path is not None:
        data = _merge(data, tomllib.loads(path.read_text()))
    return IngestConfig.model_validate(_merge(data, overrides))
