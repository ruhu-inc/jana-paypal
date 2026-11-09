"""Photo segmentation for the device (JM-D10, SPEC §5): `POST /v1/vision/segment`, SAM 3.1 behind the gateway.

The device sends the photo it already screened on the device (the local privacy pass and the photo notice come
first, INP-20) and either object prompts or one text concept:

    {"image": "data:image/jpeg;base64,...", "width": W, "height": H,
     "objects": [{"id": 1, "box": [left, top, right, bottom], "points": [[x, y, "positive"]]}, ...]}   (at most 16)
    {"image": ..., "width": W, "height": H, "concept": "force arrow"}

Boxes are half-open integer pixels of that image. The reply is
`{"objects": [{"id", "box": [l, t, r, b], "confidence", "mask": {"width", "height", "runs"}}], "images": 1}` where
`runs` are row-major run lengths over the box, starting with zeros. No match is an empty list, not an error.

Every call is metered like chat: the pseudonym and minimisation checks, the subscription's quotas, the shared
inference budget (a hold of the per-image price, settled at what was billed) and a request record with metadata only.
Images are also counted against `sam.daily_images` and `sam.total_images` (§8.3), counted before the send so an
uncertain call stays counted. Nothing here logs or stores the image, the prompts or the masks."""

from __future__ import annotations

import asyncio
import re
from typing import Any

import httpx
from fastapi import FastAPI, Request, Response
from fastapi.responses import JSONResponse

from cloud_gateway import minimise
from cloud_gateway.auth import AuthError
from cloud_gateway.config import ModelSpec
from cloud_gateway.errors import auth_error, device_error, error_body, gateway_quota
from cloud_gateway.providers.base import UpstreamError, Usage
from cloud_gateway.providers.json_segment import JsonSegmenter
from cloud_gateway.providers.segmentation import ObjectPrompt, SegmentationBackend
from cloud_gateway.store import day_of

FEATURE = "vision_segment"
IMAGE_URL = re.compile(r"^data:image/(jpeg|png|webp);base64,[A-Za-z0-9+/]+={0,2}$")
CONCEPT = re.compile(r"^[A-Za-z][A-Za-z -]{0,59}$")
SCHEMA = "CREATE TABLE IF NOT EXISTS sam_images (day TEXT PRIMARY KEY, images INTEGER NOT NULL);"


class Refused(ValueError):
    def __init__(self, status: int, code: str, message: str) -> None:
        super().__init__(message)
        self.status, self.code = status, code

    def reply(self) -> JSONResponse:
        return JSONResponse(error_body(self.code, str(self)), status_code=self.status)


def _int(value: Any, low: int, high: int) -> int:
    if type(value) is not int or not low <= value <= high:
        raise Refused(400, "invalid_request", "A coordinate or size is out of range.")
    return value


def parse(body: dict[str, Any], max_prompts: int) -> tuple[str, int, int, tuple[ObjectPrompt, ...], str | None]:
    """The checked request: image, size, object prompts and concept. Exactly one of objects and concept."""
    if not set(body) <= {"image", "width", "height", "objects", "concept"}:
        raise Refused(400, "invalid_request", "Unknown fields in the segmentation request.")
    image = body.get("image")
    if not isinstance(image, str) or IMAGE_URL.fullmatch(image) is None:
        raise Refused(400, "invalid_request", "The image must be a base64 JPEG, PNG or WebP data URL.")
    width, height = _int(body.get("width"), 1, 10_000), _int(body.get("height"), 1, 10_000)
    concept = body.get("concept")
    raw = body.get("objects")
    if (concept is None) == (raw is None):
        raise Refused(400, "invalid_request", "Send one text concept or object prompts, never both.")
    if concept is not None:
        if not isinstance(concept, str) or CONCEPT.fullmatch(concept.strip()) is None:
            raise Refused(400, "invalid_request", "A concept is one short noun phrase.")
        return image, width, height, (), concept.strip()
    if not isinstance(raw, list) or not 1 <= len(raw) <= max_prompts:
        raise Refused(400, "invalid_request", f"Send 1 to {max_prompts} objects.")
    objects, seen = [], set()
    for item in raw:
        if not isinstance(item, dict) or not set(item) <= {"id", "box", "points"} or "id" not in item:
            raise Refused(400, "invalid_request", "Each object has an id and a box or points.")
        oid = _int(item["id"], 0, 9999)
        if oid in seen:
            raise Refused(400, "invalid_request", "Object ids are unique.")
        seen.add(oid)
        box = None
        if item.get("box") is not None:
            if not isinstance(item["box"], list) or len(item["box"]) != 4:
                raise Refused(400, "invalid_request", "A box is [left, top, right, bottom].")
            left, top = _int(item["box"][0], 0, width - 1), _int(item["box"][1], 0, height - 1)
            right, bottom = _int(item["box"][2], left + 1, width), _int(item["box"][3], top + 1, height)
            box = (left, top, right, bottom)
        raw_points = item.get("points")
        if raw_points is None:
            raw_points = []
        if not isinstance(raw_points, list) or len(raw_points) > 64:
            raise Refused(400, "invalid_request", "Send at most 64 points per object.")
        points = []
        for point in raw_points:
            if not isinstance(point, list) or len(point) != 3 or point[2] not in ("positive", "negative"):
                raise Refused(400, "invalid_request", "A point is [x, y, positive|negative].")
            points.append((_int(point[0], 0, width - 1), _int(point[1], 0, height - 1), point[2]))
        if box is None and not points:
            raise Refused(400, "invalid_request", "Each object has a box or points.")
        objects.append(ObjectPrompt(oid, box, tuple(points)))
    return image, width, height, tuple(objects), None


class Segmenter:
    def __init__(self, gw: Any, client: httpx.AsyncClient | None = None) -> None:
        self.gw = gw
        legacy = gw.config.sam.enabled and not gw.config.segmentation.enabled
        spec = gw.config.sam if legacy else gw.config.segmentation
        self.spec = spec

        def permitted() -> bool:
            return bool(spec.permission and spec.permission.permits(gw.clock()))

        key = gw.secrets.get(spec.secret) if spec.enabled and permitted() and spec.secret else None
        transport = client or httpx.AsyncClient(follow_redirects=False, trust_env=False)
        self.backend: SegmentationBackend
        self.provider = "meta" if legacy else gw.config.segmentation.provider
        if legacy:
            from cloud_gateway.providers.sam import SamClient

            self.backend = SamClient(gw.config.sam, key, transport, permitted=permitted)
        else:
            self.backend = JsonSegmenter(gw.config.segmentation, key, transport, gw.clock)
        self.model = ModelSpec(id=spec.model)
        gw.store._db.executescript(SCHEMA)

    def count_image(self, now: float) -> None:
        """Count one image against the daily and event caps before it is sent (§8.3)."""
        spec, day = self.spec, day_of(now)
        with self.gw.store._tx() as db:
            today = db.execute("SELECT images FROM sam_images WHERE day=?", (day,)).fetchone()
            total = db.execute("SELECT coalesce(sum(images),0) FROM sam_images").fetchone()[0]
            if (today[0] if today else 0) + 1 > spec.daily_images or total + 1 > spec.total_images:
                raise Refused(429, "quota_exceeded", "The segmentation image cap is reached.")
            db.execute("INSERT INTO sam_images VALUES (?,1) ON CONFLICT(day) DO UPDATE SET images=images+1", (day,))

    def uncount_image(self, now: float) -> None:
        """A call the provider refused before processing bills nothing and does not count."""
        with self.gw.store._tx() as db:
            db.execute("UPDATE sam_images SET images=max(0,images-1) WHERE day=?", (day_of(now),))

    def images(self) -> dict[str, int]:
        with self.gw.store._lock:
            rows = self.gw.store._db.execute("SELECT day, images FROM sam_images").fetchall()
        return {day: count for day, count in rows}

    async def handle(self, request: Request) -> Response:
        from cloud_gateway.app import CLIENT_CLOSED, DISCONNECTED, PSEUDONYM_HEADER, _Call, _error, _upstream

        gw = self.gw
        started = gw.clock()
        try:
            session = gw.auth.authenticate(request.headers.get("authorization"))
        except AuthError as exc:
            return _error(auth_error(str(exc)))
        if not self.backend.configured or gw.operations.blocked(self.provider, self.model.id, session.licence_id):
            return JSONResponse(error_body("provider_unavailable", "Segmentation is unavailable; continue without it."),
                                status_code=503)  # fmt: skip
        if not minimise.valid_pseudonym(request.headers.get(PSEUDONYM_HEADER)):
            return JSONResponse(error_body("pseudonym_required", "X-Session-Pseudonym must be a random session "
                                           "pseudonym."), status_code=400)  # fmt: skip
        body = await gw.json_body(request)
        if isinstance(body, JSONResponse):
            return body
        try:
            image, width, height, objects, concept = parse(body, self.spec.max_prompts)
            if width * height > self.spec.max_pixels:
                raise Refused(413, "payload_too_large", "The image exceeds the segmentation pixel limit.")
            # The image is base64 that can spell anything (`prf…` included); only the words are checked (MDL-25).
            refused = minimise.refusal(request.headers, {k: v for k, v in body.items() if k != "image"})
            if refused:
                raise Refused(422, "minimisation", f"Refused: {refused}.")
        except Refused as refusal:
            return refusal.reply()
        device_hash = gw.store.device_hash(session.account, started)
        over = gw.quotas.refusal(session.account, device_hash, session.licence_id, started,
                                 period_start=session.period_start, seats=session.seats)  # fmt: skip
        if over is not None:
            return _error(gateway_quota(over[0], over[1]))
        try:
            self.count_image(started)
        except Refused as refusal:
            return refusal.reply()
        price = self.spec.image_usd
        assert price is not None  # an enabled provider declares its price before the route admits a call
        budget = gw.inference_budget
        try:
            hold = budget.reserve_flat(price, role="vision", model=self.model.id) if budget is not None else None
        except UpstreamError as exhausted:
            self.uncount_image(started)
            return _error(device_error(exhausted.reason))
        call = _Call(session, device_hash, self.provider, self.backend, self.model, started, "vision", FEATURE)  # type: ignore[arg-type]
        try:
            reply = await self.backend.segment(image, width, height, objects=objects, concept=concept)
            for mask in reply.segments:
                left, top, right, bottom = mask.box
                if not (0 <= left < right <= width and 0 <= top < bottom <= height):
                    raise UpstreamError("bad_response", "segmentation box is outside its image")
        except asyncio.CancelledError:
            # The caller went away with the image sent, so SAM may bill it: charged at its price (SPEC §4.3), the
            # image stays counted, and nothing here awaits before the cancellation goes on.
            if hold is not None and budget is not None:
                budget.settle_flat(hold, price)
            gw.finish(call, CLIENT_CLOSED, DISCONNECTED, Usage(), cost=price)
            raise
        except Exception as raised:  # every failure becomes one classified reply
            failure = _upstream(raised)
            if hold is not None and budget is not None:
                budget.fail(hold, raised)  # releases an unbilled refusal; otherwise records why the hold stays
            if failure.extra.get("unbilled"):
                self.uncount_image(started)
            error = device_error(failure.reason, failure.retry_after_s, failure.param)
            gw.finish(call, error.status, failure.reason, Usage(), cost=0.0)
            return _error(error)
        cost = reply.images * price
        if hold is not None and budget is not None:
            budget.settle_flat(hold, cost)
        gw.finish(call, 200, "ok", Usage(), cost=cost)
        objects_out = [
            {"id": s.id, "box": list(s.box), "confidence": s.confidence,
             "mask": {"width": s.width, "height": s.height, "runs": s.runs()}}
            for s in reply.segments
        ]  # fmt: skip
        return JSONResponse({"objects": objects_out, "images": reply.images}, headers={"Cache-Control": "no-store"})


def add_routes(app: FastAPI, gw: Any, client: httpx.AsyncClient | None = None) -> Segmenter:
    segmenter = Segmenter(gw, client)

    @app.post("/v1/vision/segment")
    async def segment(request: Request) -> Response:
        return await segmenter.handle(request)

    return segmenter
