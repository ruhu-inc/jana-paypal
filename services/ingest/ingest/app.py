"""The ingest's HTTP API (`/v1`).

From devices, anonymous (SEC-21: telemetry and crash reports carry no credential and no device identifier):
  POST /v1/outbox/{kind}              kind crash_report | telemetry; header Idempotency-Key; the body as the outbox
                                      sends it (application/octet-stream holding JSON). 201 kept; 409 seen already
                                      (the outbox counts both as delivered, ARC-09); 4xx refused, nothing kept.
  GET  /v1/ping -> 204;  GET /v1/status -> {status}
From a person, with a ticket reference (OPS-24):
  GET  /support                       the support upload page
  PUT  /v1/support/tickets/{ref}/bundle   the report file the app saved; optional Jana-Support-Metadata header
Staff (bearer token per person; every call is in the access log):
  POST /admin/v1/tickets {assignees}               -> {ticket_ref}
  GET  /admin/v1/tickets/{ref}/bundles             the ticket's bundles (no content)
  POST /admin/v1/tickets/{ref}/close               deletes its bundles unless an incident holds them
  POST /admin/v1/tickets/{ref}/hold {incident_id}  OPS-31 evidence hold
  POST /admin/v1/incidents/{id}/close              releases the hold and deletes the held bundles
  GET  /admin/v1/bundles/{id}[?defect=REF]         the stored file (sealed reports: L3 and a named defect)
  GET  /admin/v1/bundles/{id}/metadata             its ticket metadata
  GET  /admin/v1/bundles/{id}/access-log
  POST /admin/v1/bundles/{id}/erase                a deletion request (PRV-10), held or not
  GET  /admin/v1/reports/{kind}?from=DAY&to=DAY    received reports for the dashboards (OBS-21)
  POST /admin/v1/reports/crash_report/{crash_id}/erase   a deletion request naming a crash report

Nothing here reads or stores a client address: the server runs with proxy headers and its access log off. A refusal
names a code and, for the scanner, the kind of finding, never the text it found."""

from __future__ import annotations

import asyncio
import contextlib
import hmac
import json
import logging
import time
from collections.abc import AsyncIterator, Callable
from dataclasses import dataclass, field
from importlib import resources
from typing import Any

from fastapi import FastAPI, Request, Response
from fastapi.responses import HTMLResponse, JSONResponse

from ingest import validation
from ingest.config import IngestConfig
from ingest.custody import SecretSource, secret_source, storage_key
from ingest.store import IngestStore, day_of, days_before
from ingest.support import REFERENCE_PATTERN, Staff, SupportDesk, SupportRefused

VERSION = "1.0"
log = logging.getLogger("ingest")
NO_STORE = {"Cache-Control": "no-store"}
PAGE_HEADERS = {
    **NO_STORE,
    "Content-Security-Policy": "default-src 'none'; script-src 'self'; style-src 'self'; connect-src 'self'; "
    "form-action 'none'; frame-ancestors 'none'; base-uri 'none'",
    "X-Content-Type-Options": "nosniff",
    "Referrer-Policy": "no-referrer",
}


def _error(status: int, code: str, message: str) -> JSONResponse:
    return JSONResponse({"error": {"code": code, "message": message}}, status_code=status, headers=NO_STORE)


@dataclass
class Bucket:
    """A token bucket: `rate` per minute, up to `burst` at once."""

    rate: float
    burst: float
    tokens: float = -1.0
    at: float = 0.0

    def take(self, now: float) -> bool:
        if self.tokens < 0:
            self.tokens, self.at = self.burst, now
        self.tokens = min(self.burst, self.tokens + (now - self.at) * self.rate / 60)
        self.at = now
        if self.tokens < 1:
            return False
        self.tokens -= 1
        return True


@dataclass
class Ingest:
    config: IngestConfig
    secrets: SecretSource
    clock: Callable[[], float]
    store: IngestStore = field(init=False)
    desk: SupportDesk = field(init=False)
    buckets: dict[str, Bucket] = field(default_factory=dict)
    _purged_at: float = 0.0

    def __post_init__(self) -> None:
        self.store = IngestStore(self.config.storage_dir)
        self.desk = SupportDesk(self.config, self.store, storage_key(self.config, self.secrets), self.clock)
        rates = self.config.rates
        for kind in validation.KINDS:
            self.buckets[kind] = Bucket(rates.outbox_per_minute, rates.outbox_burst)
        self.buckets["support"] = Bucket(rates.support_uploads_per_minute, rates.support_burst)
        self.purge()

    def purge(self) -> dict[str, int]:
        now = self.clock()
        self._purged_at = now
        retention = self.config.retention
        return {
            "reports": self.store.purge_reports(now, retention.crash_report_days, retention.telemetry_days),
            "bundles": len(self.desk.purge()),
            "access_log": self.store.purge_access_log(now, retention.access_log_days),
        }

    def maybe_purge(self) -> None:
        if self.clock() - self._purged_at >= self.config.purge_interval_s:
            self.purge()

    async def body(self, request: Request, limit: int) -> bytes | JSONResponse:
        """The raw body, read no further than `limit` bytes (the declared length first, then the stream)."""
        declared = request.headers.get("content-length")
        if declared is not None and (not declared.isdigit() or int(declared) > limit):
            return _error(413, "payload_too_large", "The body is larger than this service accepts.")
        chunks, size = [], 0
        async for chunk in request.stream():
            size += len(chunk)
            if size > limit:
                return _error(413, "payload_too_large", "The body is larger than this service accepts.")
            chunks.append(chunk)
        return b"".join(chunks)

    def staff(self, authorization: str | None) -> Staff | None:
        """The staff member a bearer token belongs to (compared in constant time against every token)."""
        if not authorization or not authorization.startswith("Bearer "):
            return None
        offered = authorization.removeprefix("Bearer ").encode()
        found = None
        for actor, member in self.config.staff.items():
            secret = self.secrets.get(member.secret)
            if secret is not None and hmac.compare_digest(offered, secret.reveal().encode()):
                found = Staff(actor, member.level)
        return found

    async def outbox(self, kind: str, request: Request) -> Response:
        self.maybe_purge()
        if kind not in validation.KINDS:
            return _error(404, "unknown_kind", "This service receives crash reports and telemetry only.")
        if "authorization" in request.headers or "cookie" in request.headers:
            # SEC-21: these uploads are anonymous; a credential is refused, never read or kept.
            return _error(400, "credential_not_accepted", "Crash reports and telemetry are sent without a credential.")
        if not self.buckets[kind].take(time.monotonic()):
            return JSONResponse({"error": {"code": "rate_limited", "message": "Try again later."}}, status_code=429,
                                headers={**NO_STORE, "Retry-After": "60"})  # fmt: skip
        limits = self.config.limits
        raw = await self.body(request, limits.max_telemetry_bytes if kind == "telemetry" else
                              limits.max_crash_report_bytes)  # fmt: skip
        if isinstance(raw, JSONResponse):
            return raw
        key = request.headers.get("idempotency-key")
        try:
            body = validation.accept(kind, key, raw)
        except validation.Refused as refused:
            log.info("outbox refused kind=%s code=%s", kind, refused.code)
            return _error(refused.status, refused.code, refused.detail)
        assert key is not None  # accept() refuses a missing key
        if not self.store.add_report(kind, key, self.clock(), body):
            return _error(409, "seen_already", "This item was delivered already.")
        log.info("outbox kept kind=%s", kind)
        return JSONResponse({"kept": True, "kind": kind}, status_code=201, headers=NO_STORE)

    async def upload(self, ticket_ref: str, request: Request) -> Response:
        self.maybe_purge()
        if not self.buckets["support"].take(time.monotonic()):
            return _error(429, "rate_limited", "Too many uploads just now. Try again in a minute.")
        raw = await self.body(request, self.config.limits.max_bundle_bytes)
        if isinstance(raw, JSONResponse):
            return raw
        try:
            receipt = self.desk.upload(ticket_ref, raw, request.headers.get("jana-support-metadata"))
        except SupportRefused as refused:
            return _error(refused.status, refused.code, refused.detail)
        return JSONResponse(receipt, status_code=201, headers=NO_STORE)


def _file_logging(config: IngestConfig) -> None:
    if config.log_path is None:
        return
    config.log_path.parent.mkdir(parents=True, exist_ok=True)
    target = str(config.log_path.resolve())
    if any(isinstance(h, logging.FileHandler) and h.baseFilename == target for h in log.handlers):
        return
    handler = logging.FileHandler(target)
    handler.setFormatter(logging.Formatter("%(asctime)s %(levelname)s %(message)s"))
    log.addHandler(handler)
    log.setLevel(logging.INFO)


def _page(name: str) -> str:
    return resources.files("ingest").joinpath("page", name).read_text(encoding="utf-8")


def create_app(config: IngestConfig, *, secrets: SecretSource | None = None,
               clock: Callable[[], float] = time.time) -> FastAPI:  # fmt: skip
    _file_logging(config)
    ingest = Ingest(config, secrets if secrets is not None else secret_source(config), clock)

    @contextlib.asynccontextmanager
    async def lifespan(app: FastAPI) -> AsyncIterator[None]:
        async def purge_loop() -> None:
            while True:
                await asyncio.sleep(config.purge_interval_s)
                ingest.purge()

        task = asyncio.create_task(purge_loop())
        try:
            yield
        finally:
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)
            ingest.store.close()

    app = FastAPI(title="Jana ingest", docs_url=None, redoc_url=None, openapi_url=None, lifespan=lifespan)
    app.state.ingest = ingest

    @app.middleware("http")
    async def version_header(request: Request, call_next: Callable[[Request], Any]) -> Response:
        response: Response = await call_next(request)
        response.headers["jana-ingest-version"] = VERSION
        return response

    @app.get("/v1/ping")
    async def ping() -> Response:
        return Response(status_code=204, headers=NO_STORE)

    @app.get("/v1/status")
    async def status() -> Response:
        return JSONResponse({"status": "ok"}, headers=NO_STORE)

    @app.post("/v1/outbox/{kind}")
    async def outbox(kind: str, request: Request) -> Response:
        return await ingest.outbox(kind, request)

    @app.get("/support")
    async def support_page() -> Response:
        page = _page("support.html").replace("{{BUNDLE_DAYS}}", str(config.retention.bundle_days))
        return HTMLResponse(page, headers=PAGE_HEADERS)

    @app.get("/support/upload.js")
    async def support_script() -> Response:
        return Response(_page("upload.js"), media_type="text/javascript", headers=PAGE_HEADERS)

    @app.get("/support/style.css")
    async def support_style() -> Response:
        return Response(_page("style.css"), media_type="text/css", headers=PAGE_HEADERS)

    @app.put("/v1/support/tickets/{ticket_ref}/bundle")
    async def upload(ticket_ref: str, request: Request) -> Response:
        return await ingest.upload(ticket_ref, request)

    # --- staff ------------------------------------------------------------------------------------------------------

    async def staff_json(request: Request) -> tuple[Staff, dict[str, Any]] | JSONResponse:
        staff = ingest.staff(request.headers.get("authorization"))
        if staff is None:
            return _error(401, "unauthorised", "Staff authorisation required.")
        raw = await ingest.body(request, config.limits.max_admin_body_bytes)
        if isinstance(raw, JSONResponse):
            return raw
        try:
            body = json.loads(raw or b"{}")
        except ValueError:
            return _error(400, "invalid_json", "The body is not JSON.")
        if not isinstance(body, dict):
            return _error(400, "invalid_json", "The body must be a JSON object.")
        return staff, body

    def staff_only(request: Request) -> Staff | JSONResponse:
        staff = ingest.staff(request.headers.get("authorization"))
        return staff if staff is not None else _error(401, "unauthorised", "Staff authorisation required.")

    def refused(error: SupportRefused) -> JSONResponse:
        return _error(error.status, error.code, error.detail)

    @app.post("/admin/v1/tickets")
    async def open_ticket(request: Request) -> Response:
        got = await staff_json(request)
        if isinstance(got, JSONResponse):
            return got
        staff, body = got
        assignees = body.get("assignees")
        if not isinstance(assignees, list) or not all(isinstance(a, str) for a in assignees):
            return _error(400, "invalid_request", "assignees is a list of staff names.")
        try:
            ref = ingest.desk.open_ticket(staff, assignees)
        except SupportRefused as error:
            return refused(error)
        return JSONResponse({"ticket_ref": ref}, status_code=201, headers=NO_STORE)

    @app.get("/admin/v1/tickets/{ticket_ref}/bundles")
    async def ticket_bundles(ticket_ref: str, request: Request) -> Response:
        staff = staff_only(request)
        if isinstance(staff, JSONResponse):
            return staff
        try:
            return JSONResponse({"bundles": ingest.desk.listing(staff, ticket_ref)}, headers=NO_STORE)
        except SupportRefused as error:
            return refused(error)

    @app.post("/admin/v1/tickets/{ticket_ref}/close")
    async def close_ticket(ticket_ref: str, request: Request) -> Response:
        staff = staff_only(request)
        if isinstance(staff, JSONResponse):
            return staff
        try:
            erased = ingest.desk.close_ticket(staff, ticket_ref)
        except SupportRefused as error:
            return refused(error)
        return JSONResponse({"closed": True, "erased": erased}, headers=NO_STORE)

    @app.post("/admin/v1/tickets/{ticket_ref}/hold")
    async def hold(ticket_ref: str, request: Request) -> Response:
        got = await staff_json(request)
        if isinstance(got, JSONResponse):
            return got
        staff, body = got
        try:
            ingest.desk.hold(staff, ticket_ref, str(body.get("incident_id", "")))
        except SupportRefused as error:
            return refused(error)
        return JSONResponse({"held": True}, headers=NO_STORE)

    @app.post("/admin/v1/incidents/{incident_id}/close")
    async def close_incident(incident_id: str, request: Request) -> Response:
        staff = staff_only(request)
        if isinstance(staff, JSONResponse):
            return staff
        if not REFERENCE_PATTERN.fullmatch(incident_id):
            return _error(400, "invalid_request", "Name an incident reference.")
        return JSONResponse({"erased": ingest.desk.close_incident(staff, incident_id)}, headers=NO_STORE)

    @app.get("/admin/v1/bundles/{bundle_id}")
    async def open_bundle(bundle_id: str, request: Request) -> Response:
        staff = staff_only(request)
        if isinstance(staff, JSONResponse):
            return staff
        try:
            data, bundle = ingest.desk.open(staff, bundle_id, request.query_params.get("defect"))
        except SupportRefused as error:
            return refused(error)
        name = f"{bundle_id}.janadiag" if bundle.sealed else f"{bundle_id}.zip"
        return Response(data, media_type="application/octet-stream",
                        headers={**NO_STORE, "Content-Disposition": f'attachment; filename="{name}"'})  # fmt: skip

    @app.get("/admin/v1/bundles/{bundle_id}/metadata")
    async def bundle_metadata(bundle_id: str, request: Request) -> Response:
        staff = staff_only(request)
        if isinstance(staff, JSONResponse):
            return staff
        try:
            return JSONResponse({"metadata": ingest.desk.metadata(staff, bundle_id)}, headers=NO_STORE)
        except SupportRefused as error:
            return refused(error)

    @app.get("/admin/v1/bundles/{bundle_id}/access-log")
    async def access_log(bundle_id: str, request: Request) -> Response:
        staff = staff_only(request)
        if isinstance(staff, JSONResponse):
            return staff
        ingest.store.log(clock(), staff.actor, "bundle.access_log", bundle_id, True)
        return JSONResponse({"entries": ingest.store.access_log(bundle_id)}, headers=NO_STORE)

    @app.post("/admin/v1/bundles/{bundle_id}/erase")
    async def erase_bundle(bundle_id: str, request: Request) -> Response:
        staff = staff_only(request)
        if isinstance(staff, JSONResponse):
            return staff
        if not ingest.desk.erase(staff, bundle_id):
            return _error(404, "not_found", "No such bundle.")
        return JSONResponse({"erased": True}, headers=NO_STORE)

    @app.get("/admin/v1/reports/{kind}")
    async def reports(kind: str, request: Request) -> Response:
        staff = staff_only(request)
        if isinstance(staff, JSONResponse):
            return staff
        if kind not in validation.KINDS:
            return _error(404, "unknown_kind", "crash_report or telemetry.")
        now = clock()
        first = request.query_params.get("from", days_before(now, 1))
        last = request.query_params.get("to", day_of(now))
        rows = ingest.store.reports(kind, first, last, 10_000)
        ingest.store.log(now, staff.actor, "reports.read", f"{kind}:{first}..{last}", True, str(len(rows)))
        return JSONResponse({"reports": rows}, headers=NO_STORE)

    @app.post("/admin/v1/reports/crash_report/{crash_id}/erase")
    async def erase_crash(crash_id: str, request: Request) -> Response:
        staff = staff_only(request)
        if isinstance(staff, JSONResponse):
            return staff
        erased = ingest.store.erase_report("crash_report", f"crash-{crash_id}")
        ingest.store.log(clock(), staff.actor, "report.erase", f"crash_report:{crash_id}", erased,
                         "deletion request")  # fmt: skip
        return JSONResponse({"erased": erased}, status_code=200 if erased else 404, headers=NO_STORE)

    return app
