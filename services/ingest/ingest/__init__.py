"""The ingest: telemetry, crash-report and support-bundle intake (OBS-17, OBS-20, OPS-24; ARC-16 `services/ingest`).

Devices send approved crash reports and daily telemetry reports from their outbox to `POST /v1/outbox/{kind}`
anonymously (SEC-21: no credential and no device identifier); each body is validated against its contract and
re-scanned (OBS-19) before it is kept, and it is kept for a bounded time. Support bundles arrive only by a person's
deliberate upload with a ticket reference, are stored encrypted, opened only by the ticket's assignees with every
opening logged, and deleted at ticket close or after the retention period unless an incident holds them (OPS-24)."""
