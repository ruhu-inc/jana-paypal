"""M21 capture state machine: volatile originals, screened derivatives, explicit student confirmation."""

from __future__ import annotations

import asyncio
import base64
import contextlib
import hashlib
import io
import json
import math
import re
import time
from collections.abc import Sequence
from email import policy
from email.parser import BytesParser
from pathlib import Path
from typing import Any, cast

from fastapi import FastAPI, Request, Response
from fastapi.responses import JSONResponse
from PIL import Image

from agent_core.api.core import CURRENT_PROFILE, CoreService, require
from agent_core.api.errors import ApiError
from agent_core.api.profiles import Mutating
from agent_core.contracts import errors as contract_errors
from agent_core.contracts.input.captured_question import CapturedQuestion
from agent_core.engine.capture_observability import capture_observation, new_capture_trace
from agent_core.engine.input import normalise_input, photo_text
from agent_core.engine.safety import InputGuard, redact_personal
from agent_core.store.checkpoints import CheckpointStore
from agent_core.store.ids import new_ulid
from agent_core.store.images import SafePhoto


def edit_distance(left: str, right: str) -> int:
    """Exact Levenshtein distance with bit vectors: bounded 8,000-character inputs avoid quadratic Python work."""
    if not left:
        return len(right)
    masks: dict[str, int] = {}
    for index, character in enumerate(left):
        masks[character] = masks.get(character, 0) | (1 << index)
    mask, highest = (1 << len(left)) - 1, 1 << (len(left) - 1)
    positive, negative, distance = mask, 0, len(left)
    for character in right:
        equal = masks.get(character, 0)
        vertical = equal | negative
        horizontal = (((equal & positive) + positive) ^ positive) | equal
        plus = negative | ~(horizontal | positive)
        minus = positive & horizontal
        distance += int(bool(plus & highest)) - int(bool(minus & highest))
        plus, minus = (plus << 1) | 1, minus << 1
        positive, negative = (minus | ~(vertical | plus)) & mask, (plus & vertical) & mask
    return distance


# Photos of the student's own handwriting: printed-text OCR cannot vouch for them (SPEC §17, W2), see `handwriting`.
HANDWRITTEN = ("working", "sketch")
# Photos read as a drawing's parts, one region each, never as a question: a sketch to check, a diagram to learn.
DRAWINGS = ("sketch", "diagram")
PHOTO_SOURCES = {"photo_file", "photo_paste", "photo_drop", "webcam", "device_camera", "handwriting_photo"}


class CaptureDesk:
    def __init__(self, core: CoreService):
        self.core = core
        self._connection: Any = None
        self.raw: dict[str, bytes] = {}
        self.prepared: dict[str, dict[str, Any]] = {}
        self.tasks: dict[str, asyncio.Task[Any]] = {}
        self.owners: dict[str, str | None] = {}
        self.trace_builders: dict[str, Any] = {}
        self.preprocess_ms: dict[str, int] = {}
        self.trace_failures = 0
        self.traced_terminal: dict[str, str] = {}
        self.semaphore = asyncio.Semaphore(1)
        self.late: dict[str, asyncio.Future[None]] = {}  # SAM outlines still arriving (JM-D10)
        core.on_profile_closing.append(self.forget)
        core.closers.insert(0, self.close)

    def observe(self, record: dict[str, Any]) -> None:
        sink = self.core.capture_trace_sink
        if sink is None or record["source"] not in PHOTO_SOURCES:
            return
        cap = record["capture_id"]
        if cap in self.traced_terminal:
            return
        if cap not in self.trace_builders:
            entry = self.core.entry(record["session_id"])
            hardware = record["reader"]["hardware_class"]
            self.trace_builders[cap] = new_capture_trace(
                record["session_id"],
                entry.profile_id or "implicit",
                entry.engine_state.mode,
                hardware,
                cap,
                self.core.settings.model_dump(mode="json"),
            )
        try:
            observation = capture_observation(self.trace_builders[cap], record, self.preprocess_ms.get(cap, 0))
        except (ValueError, TypeError, KeyError):
            self.trace_failures += 1
            return
        if contract_errors("trace/turn-trace", observation):
            self.trace_failures += 1
            return
        try:
            sink(observation)
        except Exception:
            self.trace_failures += 1  # optional observations never prevent capture/confirmation
        if record["status"] in ("confirmed", "cancelled", "refused", "unreadable"):
            self.traced_terminal[cap] = self.trace_builders[cap].profile_ref
            self.trace_builders.pop(cap, None)
            self.preprocess_ms.pop(cap, None)

    def forget(self, profile_id: str) -> None:
        self.traced_terminal = {cap: owner for cap, owner in self.traced_terminal.items() if owner != profile_id}
        for cap in [cap for cap, owner in self.owners.items() if owner == profile_id]:
            with contextlib.suppress(ApiError):
                record = self.get(cap)
                if record["status"] != "confirmed":
                    self.delete(cap)
            task = self.tasks.pop(cap, None)
            if task is not None:
                task.cancel()
            self.raw.pop(cap, None)
            self.prepared.pop(cap, None)
            self.owners.pop(cap, None)

    async def close(self) -> None:
        tasks = [*self.tasks.values(), *self.late.values()]
        for task in tasks:
            task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)
        for cap, owner in list(self.owners.items()):
            scope = CURRENT_PROFILE.set(owner)
            try:
                with contextlib.suppress(ApiError):
                    self.delete(cap)
            finally:
                CURRENT_PROFILE.reset(scope)
        self.raw.clear()
        self.prepared.clear()
        self.tasks.clear()
        self.late.clear()
        self.owners.clear()
        self.traced_terminal.clear()
        self.trace_builders.clear()
        self.preprocess_ms.clear()

    def conn(self) -> Any:
        if not isinstance(self.core.store, CheckpointStore):
            raise ApiError("feature_disabled", "capture requires profile storage")
        connection = self.core.store.conn
        if connection is not self._connection:
            self._connection = connection
            db = connection.execute("PRAGMA database_list").fetchone()[2]
            if db:
                root = Path(db).parent / "attachments"
                for cap, raw in connection.execute("SELECT capture_id, record FROM captures").fetchall():
                    record = json.loads(raw)
                    if record["status"] != "confirmed":
                        for image in record["attachments"]:
                            (root / f"{image['image_id']}.jpg").unlink(missing_ok=True)
                            connection.execute("DELETE FROM attachments WHERE attachment_id=?", (image["image_id"],))
                        connection.execute("DELETE FROM captures WHERE capture_id=?", (cap,))
        return connection

    def root(self) -> Path:
        db = self.conn().execute("PRAGMA database_list").fetchone()[2]
        if not db:
            raise ApiError("photo_reading_unavailable")
        return Path(db).parent / "attachments"

    def list(self, sid: str) -> dict[str, Any]:
        self.core.entry(sid)
        records = [
            json.loads(row[0])
            for row in self.conn().execute(
                "SELECT record FROM captures WHERE session_id=? AND expires_at>? ORDER BY capture_id DESC LIMIT 50",
                (sid[4:], self.core.clock_ms()),
            )
        ]
        return {"captures": [r for r in records if r["status"] == "confirmed"]}

    def get(self, capture_id: str) -> dict[str, Any]:
        row = (
            self.conn().execute("SELECT record, expires_at FROM captures WHERE capture_id=?", (capture_id,)).fetchone()
        )
        if row is None or row[1] <= self.core.clock_ms():
            raise ApiError("not_found")
        record: dict[str, Any] = json.loads(row[0])
        self.core.entry(record["session_id"])
        # The source vanished on process/profile close. Never pretend preprocessing is still alive.
        if (
            record["source"] in PHOTO_SOURCES
            and record["status"] in ("preprocessing", "reading", "drafting")
            and capture_id not in self.raw
        ):
            record["status"] = "refused"
            record["refusal"] = {"reason": "photo_reading_unavailable"}
        return record

    def put(self, record: dict[str, Any], *, event: bool = True, record_trace: bool = True) -> dict[str, Any]:
        record["updated_at"] = self.core.clock_ms()
        CapturedQuestion.model_validate(record)
        self.core.entry(record["session_id"])
        self.conn().execute(
            "UPDATE captures SET record=? WHERE capture_id=?",
            (json.dumps(record, ensure_ascii=False), record["capture_id"]),
        )
        if record_trace:
            self.observe(record)
        if event:
            self.core.app_channel.publish(
                {
                    "type": "capture.updated",
                    "capture_id": record["capture_id"],
                    "status": record["status"],
                    "progress": None,
                    "error_code": None,
                }
            )
        return record

    def locked(self, session_id: str) -> None:
        entry = self.core.entry(session_id)
        if self.core.storage_low is not None and self.core.storage_low():
            raise ApiError("storage_full", "free disk space before capturing a photo")
        if entry.engine_state.mock_running or (self.core.mock_active is not None and self.core.mock_active()):
            raise ApiError("capture_locked")
        if entry.engine_state.hardware_class == "content_only":
            raise ApiError("photo_reading_unavailable")

    def create(self, meta: Any, data: bytes) -> dict[str, Any]:
        if self.core.mock_active is not None and self.core.mock_active():
            raise ApiError("capture_locked", "photos are off during the mock exam")
        self.core.assessment_guard()
        require("CreateCapture", meta)
        sid = meta.get("session_id")
        if not isinstance(sid, str):
            raise ApiError("validation_failed", "choose a session before capture")
        self.core.entry(sid)
        digest = self.core.idempotency.digest("POST", "/v1/captures", [meta, hashlib.sha256(data).hexdigest()])
        cached = (
            self.conn()
            .execute(
                "SELECT request_sha256, response FROM idempotency_responses WHERE idempotency_key=? AND created_at>?",
                (
                    meta["client_capture_id"],
                    self.core.clock_ms() - int(self.core.settings.api_idempotency_hours * 3600000),
                ),
            )
            .fetchone()
        )
        if cached is not None:
            if cached[0] != digest:
                raise ApiError("request_conflict", "capture key was used with another request")
            try:
                return self.get(json.loads(cached[1])["capture_id"])
            except ApiError:
                raise ApiError("request_expired", "choose the photo again") from None
        typed = meta["source"] in ("typed", "pasted_text")
        if not typed:
            self.locked(sid)
        if not typed and (meta["source"] not in PHOTO_SOURCES or not data):
            raise ApiError("unsupported_format")
        if len(data) > 20 * 1024 * 1024:
            raise ApiError("image_too_large")
        # Durable client identity prevents duplicate creation even after core restart.
        for row in self.conn().execute("SELECT record FROM captures WHERE session_id=?", (sid[4:],)):
            existing = json.loads(row[0])
            if existing.get("client_capture_id") == meta["client_capture_id"]:
                return self.get(existing["capture_id"])
        if not typed and len(self.raw) >= 2:
            raise ApiError("rate_limited", "finish or cancel a capture first")
        now = self.core.clock_ms()
        cap = f"cap_{new_ulid(now)}"
        record = {
            "schema_version": 1,
            "capture_id": cap,
            "client_capture_id": meta["client_capture_id"],
            "session_id": sid,
            "source": meta["source"],
            "purpose": meta["purpose"],
            "locale": "en",
            "status": "drafting",
            "text": "",
            "regions": [],
            "questions": [],
            "attachments": [],
            "reader": {
                "path": "none",
                "model_id": None,
                "hardware_class": self.core.entry(sid).engine_state.hardware_class,
                "duration_ms": 0,
            },
            "quality": {"checks": [], "overridden": False},
            "confirmation": {"confirmed": False, "method": "pending", "edit_distance": 0, "confirmed_at": None},
            "injection": {"flagged": False, "spans_removed": 0},
            "refusal": None,
            "egress": {"left_device": False, "destination": "none"},
            "created_at": now,
            "updated_at": now,
        }
        if typed:
            record["text"] = redact_personal(normalise_input(meta.get("text", "")))[0]
            record["status"] = "awaiting_confirmation"
        CapturedQuestion.model_validate(record)
        self.core.persist(self.core.entry(sid))
        self.conn().execute(
            "INSERT INTO captures VALUES (?, ?, ?, ?)",
            (cap, sid[4:], json.dumps(record), now + self.core.settings.retention_attachment_days * 86400000),
        )
        self.conn().execute(
            "INSERT OR REPLACE INTO idempotency_responses VALUES (?, ?, ?, ?, ?)",
            (meta["client_capture_id"], digest, 200, json.dumps({"capture_id": cap}), now),
        )
        if not typed:
            self.raw[cap] = data
        self.owners[cap] = CURRENT_PROFILE.get()
        self.observe(record)
        return record

    async def crop(self, cap: str, body: Any) -> dict[str, Any]:
        require("CaptureCrop", body)
        record = self.get(cap)
        self.locked(record["session_id"])
        if cap not in self.raw or cap in self.tasks or record["status"] in ("confirmed", "cancelled", "refused"):
            raise ApiError("capture_not_ready")
        if "image_id" in body and body["image_id"] not in {a["image_id"] for a in record["attachments"]}:
            raise ApiError("not_found")
        record["status"] = "preprocessing"
        self.put(record)
        current = asyncio.current_task()
        if current is not None:
            self.tasks[cap] = current
        preprocess_started = time.monotonic()
        async with self.semaphore:
            try:
                prepared = await self.core._engine(self.core.entry(record["session_id"])).preprocess_photo(
                    self.raw[cap],
                    None
                    if body["corners"] == [{"x": 0, "y": 0}, {"x": 1, "y": 0}, {"x": 1, "y": 1}, {"x": 0, "y": 1}]
                    else body["corners"],
                    body["rotation"],
                    **({"preview_only": True} if body.get("preview_only") else {}),
                )
            finally:
                if self.core.capture_trace_sink is not None and cap in self.raw:
                    self.preprocess_ms[cap] = max(0, int((time.monotonic() - preprocess_started) * 1000))
                self.tasks.pop(cap, None)
        # A concurrent delete/profile switch must never persist a worker's late result.
        if cap not in self.raw or self.owners.get(cap) != CURRENT_PROFILE.get():
            raise ApiError("not_found")
        if "error" in prepared:
            record["status"] = "refused"
            record["refusal"] = {"reason": "photo_reading_unavailable"}
            self.raw.pop(cap, None)
            self.put(record)
            raise ApiError(prepared["error"])
        if body.get("preview_only"):
            record["outline"] = prepared.get(
                "outline", [{"x": 0, "y": 0}, {"x": 1, "y": 0}, {"x": 1, "y": 1}, {"x": 0, "y": 1}]
            )
            record["status"] = "drafting"
            return self.put(record)
        if prepared.get("refusal"):
            record["status"] = "refused"
            record["refusal"] = {"reason": prepared["refusal"]}
            self.raw.pop(cap, None)
            return self.put(record)
        if record["purpose"] in HANDWRITTEN:
            prepared = handwriting(prepared)
        self.prepared[cap] = prepared
        record["quality"]["checks"] = prepared["checks"]
        record["status"] = "quality_failed" if any(c["outcome"] != "pass" for c in prepared["checks"]) else "drafting"
        self.delete_images(record)
        record["attachments"] = []
        if prepared["privacy_available"]:
            jpeg = base64.b64decode(prepared["jpeg"], validate=True)
            image_id = f"img_{new_ulid()}"
            root = self.root()
            root.mkdir(parents=True, exist_ok=True)
            name = f"{image_id}.jpg"
            path = root / name
            path.write_bytes(jpeg)
            attachment = {
                "image_id": image_id,
                "kind": "photo",
                "width": prepared["width"],
                "height": prepared["height"],
                "bytes": len(jpeg),
                "sha256": hashlib.sha256(jpeg).hexdigest(),
                "scale_from_original": prepared["scale_from_original"],
            }
            try:
                self.conn().execute(
                    "INSERT INTO attachments (attachment_id,session_id,kind,file_path,sha256,bytes,"
                    "width,height,created_at,delete_after) VALUES (?,?,?,?,?,?,?,?,?,?)",
                    (
                        image_id,
                        record["session_id"][4:],
                        "photo",
                        name,
                        attachment["sha256"],
                        len(jpeg),
                        attachment["width"],
                        attachment["height"],
                        self.core.clock_ms(),
                        self.core.clock_ms() + self.core.settings.retention_attachment_days * 86400000,
                    ),
                )
            except Exception:
                path.unlink(missing_ok=True)
                raise
            record["attachments"] = [attachment]
        return self.put(record)

    def read(self, cap: str, body: Any) -> dict[str, Any]:
        require("CaptureRead", body)
        record = self.get(cap)
        self.locked(record["session_id"])
        prepared = self.prepared.get(cap)
        if prepared is None or cap in self.tasks or record["status"] not in ("drafting", "quality_failed"):
            raise ApiError("capture_not_ready")
        outcomes = [c["outcome"] for c in record["quality"]["checks"]]
        if "blocking" in outcomes or ("advisory" in outcomes and not body.get("override_quality")):
            raise ApiError("capture_not_ready", "retake the photo or accept advisory quality warnings")
        if body.get("send_to_cloud") and not prepared["privacy_available"]:
            raise ApiError("photo_reading_unavailable", "this photo can only be read locally")
        record["quality"]["overridden"] = bool(body.get("override_quality"))
        record["status"] = "reading"
        self.put(record)
        self.tasks[cap] = asyncio.create_task(self._read(record, prepared, bool(body.get("send_to_cloud"))))
        return {"capture_id": cap, "status": "reading"}

    async def _read(self, record: dict[str, Any], prepared: dict[str, Any], send_to_cloud: bool = False) -> None:
        cap = record["capture_id"]
        started = time.monotonic()
        try:
            engine = self.core._engine(self.core.entry(record["session_id"]))
            # Local vision is preferred. Cloud is never attempted without an explicit per-photo notice.
            jpeg = base64.b64decode(prepared["jpeg"])
            safe = SafePhoto(jpeg, prepared["width"], prepared["height"])
            reading = await engine.read_photo(
                jpeg,
                send_to_cloud=send_to_cloud,
                screen=(lambda _: safe) if prepared["privacy_available"] else None,
                structured=True,
                purpose=record["purpose"],
                outline_later=True,  # SAM outlines the lines while the student confirms them (JM-D10, SPEC §12)
            )
            if reading.status == "read" and reading.refusal:
                record["status"] = "refused"
                record["refusal"] = reading.refusal
                self.delete_images(record)
                record["attachments"] = []
                self.put(record)
                return
            if reading.status == "read" and reading.regions:
                lines: list[dict[str, Any]] = reading.regions
                record["reader"] = reading.reader
                record["egress"] = reading.egress
            else:
                if not prepared["privacy_available"]:
                    record["status"] = "refused"
                    record["refusal"] = {"reason": "photo_reading_unavailable"}
                    self.put(record)
                    return
                lines = prepared["lines"]
                record["reader"] = {
                    "path": "ocr_pipeline",
                    "model_id": prepared["ocr_model"],
                    "model_version": prepared.get("ocr_model_version"),
                    "hardware_class": engine.state.hardware_class,
                    "duration_ms": int((time.monotonic() - started) * 1000),
                }

            # Disagreement is a reason to ask the student, never to substitute one reader's guess for another.
            def comparable(value: str) -> str:
                return re.sub(r"\s+", "", normalise_input(value).replace("\\(", "").replace("\\)", ""))

            ocr_lines = [comparable(line["text"]) for line in prepared["lines"]]
            ocr_page = "".join(ocr_lines)
            regions = []
            hits_total = 0
            personal_found = False
            image_id = record["attachments"][0]["image_id"] if record["attachments"] else None
            for i, line in enumerate(lines[:200]):
                line = dict(line)
                if reading.regions and ocr_lines and comparable(line["text"]) not in ocr_page:
                    line["confidence"] = min(line["confidence"], 0.84)
                    line["needs_confirmation"] = True
                text, hits = self.screen(line["text"], record)
                hits_total += hits
                text, redacted = redact_personal(text)
                personal_found |= bool(redacted)
                bbox = {**line["bbox"], "image_id": image_id} if line.get("bbox") and image_id else None
                spans = line["low_confidence_spans"] if hits == 0 and text == line["text"] else []
                number_label = line.get("number_label")
                if number_label is not None:
                    number_label, count = self.screen(str(number_label), record)
                    hits_total += count
                    number_label = number_label.strip()
                    if not re.fullmatch(r"\d{1,3}(?:[.)]|(?:\([a-z0-9]{1,3}\))+)?", number_label):
                        number_label = None
                regions.append(
                    {
                        "region_id": f"rgn_{i}",
                        "kind": "working_line"
                        if record["purpose"] == "working"
                        else "diagram"
                        if record["purpose"] in DRAWINGS
                        else line.get("kind")
                        or (
                            "working_line"
                            if record["purpose"] == "working"
                            else "question_stem"
                            if i == 0
                            else "sub_part"
                        ),
                        "order": i,
                        "number_label": number_label,
                        "text": text,
                        "confidence": line["confidence"],
                        "needs_confirmation": line["confidence"] < 0.85
                        or bool(spans)
                        or bool(line.get("needs_confirmation")),
                        "low_confidence_spans": spans[:100],
                        "language": line.get("language", "en"),
                        "direction": line.get("direction", "ltr"),
                        "diagram_description": line.get("diagram_description"),
                        "latex": line.get("latex"),
                        "table": line.get("table"),
                        "bbox": bbox,
                        "outline": line.get("outline") if bbox is not None else None,
                        "sketch_element": line.get("sketch_element") if record["purpose"] == "sketch" else None,
                        "label_box": line.get("label_box") if record["purpose"] == "diagram" and bbox else None,
                    }
                )
            text = "\n".join(r["text"] for r in regions)[:8000]
            for region in regions:
                if region.get("diagram_description"):
                    clean, count = self.screen(region["diagram_description"], record)
                    region["diagram_description"], redacted = redact_personal(clean)
                    personal_found |= bool(redacted)
                    hits_total += count
                if region["kind"] != "diagram":
                    region["diagram_description"] = None
                if region.get("latex"):
                    region["latex"], count = self.screen(region["latex"], record)
                    hits_total += count
                    region["latex"], redacted = redact_personal(region["latex"])
                    personal_found |= bool(redacted)
                if region.get("table"):
                    for row in [region["table"]["columns"], *region["table"]["rows"]]:
                        for index, cell in enumerate(row):
                            clean, count = self.screen(cell, record)
                            row[index], redacted = redact_personal(clean)
                            hits_total += count
                            personal_found |= bool(redacted)
                region["low_confidence_spans"] = [
                    s for s in region["low_confidence_spans"] if 0 <= s["start"] < s["end"] <= len(region["text"])
                ]
                # Inconsistent brackets reduce confidence; never fill missing symbols.
                if any(
                    region["text"].count(a) != region["text"].count(b) for a, b in (("(", ")"), ("[", "]"), ("{", "}"))
                ):
                    region["confidence"] = min(region["confidence"], 0.59)
                    region["needs_confirmation"] = True
                if region["bbox"] is not None:
                    box = region["bbox"]
                    if (
                        box["w"] <= 0
                        or box["h"] <= 0
                        or box["x"] >= 1
                        or box["y"] >= 1
                        or box["x"] + box["w"] > 1
                        or box["y"] + box["h"] > 1
                    ):
                        region["bbox"] = None
                        region["outline"] = None
                        region["label_box"] = None
                label = region.get("label_box")
                if label is not None and (
                    label["w"] <= 0 or label["h"] <= 0 or label["x"] + label["w"] > 1 or label["y"] + label["h"] > 1
                ):
                    region["label_box"] = None
            if record["purpose"] == "diagram":
                ocr_label_boxes(regions, prepared["lines"])
            if any(r["language"] not in ("en", "und") for r in regions):
                record.update(status="refused", refusal={"reason": "unsupported_language"})
                self.delete_images(record)
                record["attachments"] = []
                self.put(record)
                return
            if record["purpose"] not in (*HANDWRITTEN, *DRAWINGS) and not re.search(
                r"[?=+*/^]|\b(find|solve|calculate|explain|describe|state|show|evaluate|simplify|balance|prepare|what|which|how|why|determine|prove|journal|debit|credit)\b",
                text,
                re.I,
            ):
                record.update(status="refused", refusal={"reason": "not_a_question"})
                self.delete_images(record)
                record["attachments"] = []
                self.put(record)
                return
            if personal_found:
                # A later local reader found details the OCR pass missed. Discard derivatives rather than retaining
                # a partly redacted image; confirmed text stays editable. No image is passed to the tutor again.
                self.delete_images(record)
                record["attachments"] = []
                for region in regions:
                    region["bbox"] = None
                    region["outline"] = None
                    region["label_box"] = None
            # Separate diagram crops use only the already-screened derivative.
            if prepared["privacy_available"] and not personal_found and record["purpose"] not in DRAWINGS:
                for region in regions:
                    if region["kind"] != "diagram" or not region.get("bbox") or len(record["attachments"]) >= 40:
                        continue
                    box = region["bbox"]

                    def crop_diagram(box: dict[str, float] = box) -> bytes:
                        with Image.open(io.BytesIO(jpeg)) as image:
                            bounds = (
                                int(box["x"] * image.width),
                                int(box["y"] * image.height),
                                max(1, int((box["x"] + box["w"]) * image.width)),
                                max(1, int((box["y"] + box["h"]) * image.height)),
                            )
                            cropped = image.crop(bounds)
                            out = io.BytesIO()
                            cropped.save(out, "JPEG", quality=85, optimize=True)
                            return out.getvalue()

                    data = await asyncio.to_thread(crop_diagram)
                    if cap not in self.raw:
                        return
                    image_id = f"img_{new_ulid()}"
                    width = max(1, int(box["w"] * prepared["width"]))
                    height = max(1, int(box["h"] * prepared["height"]))
                    attachment = {
                        "image_id": image_id,
                        "kind": "diagram_crop",
                        "width": width,
                        "height": height,
                        "bytes": len(data),
                        "sha256": hashlib.sha256(data).hexdigest(),
                    }
                    name = f"{image_id}.jpg"
                    path = self.root() / name
                    path.write_bytes(data)
                    self.conn().execute(
                        "INSERT INTO attachments (attachment_id,session_id,kind,file_path,sha256,bytes,"
                        "width,height,created_at,delete_after) VALUES (?,?,?,?,?,?,?,?,?,?)",
                        (
                            image_id,
                            record["session_id"][4:],
                            "diagram_crop",
                            name,
                            attachment["sha256"],
                            len(data),
                            width,
                            height,
                            self.core.clock_ms(),
                            self.core.clock_ms() + self.core.settings.retention_attachment_days * 86400000,
                        ),
                    )
                    record["attachments"].append(attachment)
                    # The normalized box remains relative to the page image for highlighting.
            text = "\n".join(
                "\n".join(
                    filter(
                        None,
                        [
                            r["text"],
                            r.get("diagram_description"),
                            "\n".join(" | ".join(row) for row in [r["table"]["columns"], *r["table"]["rows"]])
                            if r.get("table")
                            else None,
                        ],
                    )
                )
                for r in regions
            )[:8000]
            # Count uncertain transcription characters once. Descriptions/table metadata cannot dilute the
            # unreadable fraction, and overlapping spans must not count the same character twice.
            low_chars = 0
            for region in regions:
                covered_until = 0
                for span in sorted(region["low_confidence_spans"], key=lambda item: item["start"]):
                    low_chars += max(0, span["end"] - max(covered_until, span["start"]))
                    covered_until = max(covered_until, span["end"])
            transcription_chars = sum(len(region["text"]) for region in regions)
            threshold = self.core.settings.vision_confirm_threshold
            if record["purpose"] in DRAWINGS:
                # A sketch's unsure parts are labelled and never judged (`sketch_photo`), and a diagram's are left out
                # of its drill; either is unreadable only when no part was read with confidence.
                unreadable = not any(r["confidence"] >= threshold for r in regions)
            else:
                unreadable = (
                    not regions
                    or any(r["confidence"] < threshold for r in regions)
                    or low_chars > transcription_chars * 0.3
                )
            record.update(
                text=text,
                regions=regions,
                overall_confidence=min((r["confidence"] for r in regions), default=0),
                injection={"flagged": hits_total > 0, "spans_removed": hits_total},
                status="unreadable" if unreadable else "awaiting_confirmation",
                refusal={"reason": "unreadable"} if unreadable else None,
            )
            # Numbered questions require a choice; no automatic first-question submission.
            questions: list[dict[str, Any]] = []
            current: dict[str, Any] | None = None
            for region in regions if record["purpose"] not in (*HANDWRITTEN, *DRAWINGS) else []:
                match = (
                    re.match(r"^(\d{1,3})", region["number_label"])
                    if region.get("number_label")
                    else re.match(r"^\s*(\d{1,3})[.)]\s+", region["text"])
                )
                if match and (current is None or current["label"] != match[1]):
                    current = {"label": match[1], "region_ids": [], "selected": False}
                    questions.append(current)
                if current is not None:
                    current["region_ids"].append(region["region_id"])
            if 1 < len(questions) <= 50:
                first_ids = set().union(*(set(q["region_ids"]) for q in questions))
                shared = [r["region_id"] for r in regions if r["region_id"] not in first_ids]
                for question in questions:
                    question["region_ids"] = [*shared, *question["region_ids"]]
                record["questions"] = questions
            if cap in self.raw and self.owners.get(cap) == CURRENT_PROFILE.get():
                self.put(record)
                if reading.outlining is not None and reading.regions and record["attachments"]:
                    self.late[cap] = asyncio.ensure_future(self._late_outlines(cap, reading.outlining, reading.regions))
        except asyncio.CancelledError:
            raise
        except Exception:
            if cap in self.raw:
                record["status"] = "refused"
                record["refusal"] = {"reason": "photo_reading_unavailable"}
                with contextlib.suppress(ApiError):
                    self.put(record)
        finally:
            self.tasks.pop(cap, None)
            # The derivative remains for confirmation, but the original is no longer needed.
            self.raw.pop(cap, None)
            self.prepared.pop(cap, None)

    async def _late_outlines(self, cap: str, outlining: Any, read: Sequence[dict[str, Any]]) -> None:
        """SAM's outlines, arriving while the student confirms what was read: added to the regions that still show
        on the photo (a deleted or redacted capture gets none). Region i is the reader's region i (`_read`)."""
        try:
            await asyncio.wait_for(outlining, timeout=30)
            record = self.get(cap)
            if not record["attachments"] or record["status"] in ("cancelled", "refused"):
                return
            changed = False
            for region in record["regions"]:
                source = read[region["order"]] if region["order"] < len(read) else None
                if source is None or region.get("bbox") is None or not source.get("outline"):
                    continue
                region["outline"] = source["outline"]
                region["bbox"] = {**source["bbox"], "image_id": region["bbox"]["image_id"]}
                changed = True
            if changed:
                self.put(record, record_trace=False)
        except (TimeoutError, ApiError):
            return  # no outline: the photo still shows each line's box
        except Exception:
            return  # a failed segmentation never touches the capture
        finally:
            self.late.pop(cap, None)

    def screen(self, text: str, record: dict[str, Any]) -> tuple[str, int]:
        """PRM-18 for a capture's text: words read from a photo lose instruction-like spans; typed, pasted and
        dictated text is the student's own and is kept whole. Stripping never removes what the input guard must see:
        when the whole text routes to a guard template (distress first), it is kept whole, so the turn's guard answers
        it and no model ever reads it (PRV-13, PRV-14)."""
        if record["source"] not in PHOTO_SOURCES:
            return normalise_input(text), 0  # the turn's own intake scans and records it as student text
        clean, hits = photo_text(text, self.core._learning, record["session_id"])
        if hits and self._guarded(normalise_input(text), record):
            return normalise_input(text), 0  # nothing removed: the guard answers it in the turn
        return clean, hits

    def _guarded(self, text: str, record: dict[str, Any]) -> bool:
        entry = self.core.entry(record["session_id"])
        guard = getattr(entry.engine, "guard", None) or InputGuard(self.core.settings)
        result = guard.check(text, entry.engine_state.level)
        return result.template is not None and result.category != "personal_data"

    def confirm(self, cap: str, body: Any) -> dict[str, Any]:
        require("CaptureConfirm", body)
        record = self.get(cap)
        if record["source"] in PHOTO_SOURCES:
            self.locked(record["session_id"])
        if record["status"] != "awaiting_confirmation":
            raise ApiError("capture_not_ready")
        text, hits = self.screen(body["text"], record)
        text = redact_personal(text.strip())[0]
        if not text:
            raise ApiError("validation_failed", "confirm a question or type it instead")
        if record["questions"]:
            selected = body.get("selected_question")
            if selected not in [q["label"] for q in record["questions"]]:
                raise ApiError("validation_failed", "choose one question")
            for question in record["questions"]:
                question["selected"] = question["label"] == selected
        if "regions" in body:
            # Edits retain server region identity/bounds; no arbitrary attachment references.
            if [r["region_id"] for r in body["regions"]] != [r["region_id"] for r in record["regions"]]:
                raise ApiError("validation_failed", "region identity changed")
            for before, edit in zip(record["regions"], body["regions"], strict=True):
                before["text"] = redact_personal(self.screen(edit["text"], record)[0])[0]
                before["needs_confirmation"] = False
        distance = edit_distance(record["text"], text)
        record["confirmation"] = {
            "confirmed": True,
            "method": "retyped" if record["status"] == "unreadable" else "edited" if distance else "accepted",
            "edit_distance": distance,
            "confirmed_at": self.core.clock_ms(),
        }
        record["text"] = text
        record["status"] = "confirmed"
        record["refusal"] = None
        record["injection"]["flagged"] |= hits > 0
        record["injection"]["spans_removed"] += hits
        self.owners.pop(cap, None)
        return self.put(record)

    def turn_body(self, sid: str, body: dict[str, Any]) -> dict[str, Any]:
        cap = body.get("capture_id")
        if cap is None:
            return body
        record = self.get(cap)
        if record["session_id"] != sid or record["status"] != "confirmed" or not record["confirmation"]["confirmed"]:
            raise ApiError("capture_not_ready")
        if record["purpose"] == "diagram":
            raise ApiError("validation_failed", "a diagram to learn is practised in its drill, not sent as a turn")
        if (record["purpose"] == "working") != (body["intent"] == "board_line") or (
            (record["purpose"] == "sketch") != (body["intent"] == "check_my_working")
        ):
            raise ApiError("validation_failed", "capture purpose does not match the turn")
        return {**body, "content": [{"type": "text", "text": record["text"]}]}

    def delete_images(self, record: dict[str, Any]) -> None:
        for attachment in record["attachments"]:
            name = attachment["image_id"]
            row = self.conn().execute("SELECT file_path FROM attachments WHERE attachment_id=?", (name,)).fetchone()
            if row:
                (self.root() / Path(row[0]).name).unlink(missing_ok=True)
                self.conn().execute("DELETE FROM attachments WHERE attachment_id=?", (name,))

    def delete(self, cap: str) -> None:
        record = self.get(cap)
        terminal = record["status"] in ("confirmed", "refused", "unreadable", "cancelled")
        task = self.tasks.pop(cap, None)
        if task:
            task.cancel()
        late = self.late.pop(cap, None)
        if late:
            late.cancel()
        self.raw.pop(cap, None)
        self.prepared.pop(cap, None)
        self.owners.pop(cap, None)
        self.delete_images(record)
        record["status"] = "cancelled"
        self.put(record, record_trace=not terminal)
        self.conn().execute("DELETE FROM captures WHERE capture_id=?", (cap,))


def _words(text: str) -> list[str]:
    return re.findall(r"[a-z0-9]+", text.lower())


def ocr_label_boxes(regions: list[dict[str, Any]], lines: Sequence[dict[str, Any]]) -> None:
    """A diagram part's printed label, placed by the device's own OCR of the photo rather than the reader's estimate
    (SPEC §5.2): the drill covers the label, and the reader's boxes are often a line off. Each part takes the OCR line
    whose words contain its name (cut to the name's share of the line when the line holds more), the closest to the
    reader's box when several do; a part no OCR line names keeps the reader's box."""
    for region in regions:
        name = _words(region["text"])
        if region["kind"] != "diagram" or not name or "[?]" in region["text"]:
            continue
        guess = region.get("label_box") or region.get("bbox")
        found: list[tuple[float, dict[str, float]]] = []
        for line in lines:
            box, text = line.get("bbox"), str(line.get("text", ""))
            words = _words(text)
            at = next((i for i in range(len(words) - len(name) + 1) if words[i : i + len(name)] == name), None)
            if box is None or at is None:
                continue
            if len(words) > len(name):  # the name's share of the line, by its characters
                lowered = text.lower()
                first = re.search(rf"(?<![a-z0-9]){re.escape(name[0])}(?![a-z0-9])", lowered)
                last = [m for m in re.finditer(rf"(?<![a-z0-9]){re.escape(name[-1])}(?![a-z0-9])", lowered)]
                if first is None or not last or last[-1].end() <= first.start():
                    continue
                start, end = first.start(), last[-1].end()
                box = {**box, "x": box["x"] + box["w"] * start / len(text), "w": box["w"] * (end - start) / len(text)}
            centre = (box["x"] + box["w"] / 2, box["y"] + box["h"] / 2)
            distance = 0.0 if guess is None else math.hypot(centre[0] - guess["x"] - guess["w"] / 2,
                                                            centre[1] - guess["y"] - guess["h"] / 2)  # fmt: skip
            found.append((distance, {k: float(box[k]) for k in ("x", "y", "w", "h")}))
        if found:
            region["label_box"] = min(found, key=lambda item: item[0])[1]


def handwriting(prepared: dict[str, Any]) -> dict[str, Any]:
    """The local privacy pass for a photo of the student's own working or sketch (SPEC §5, §17 W2).

    The pass vouches for a photo by reading its printed text on the device; handwriting defeats that reader, so a
    photo of working would never count as screened. For these photos the pass counts when it ran in full: the
    person and face screen found nobody, and every line the device could read was checked and any personal detail
    in it masked. Lines it could not read are still read back to the student and redacted before use, and a read that
    finds a personal detail deletes the image (`_read`). Without OCR at all nothing changes. No text found is not a
    blocking quality failure for handwriting: the reader decides whether anything is legible."""
    if prepared.get("refusal") or prepared.get("ocr_model_version") is None or "checks" not in prepared:
        return prepared
    checks = [{**check, "outcome": "pass"} if check["check"] == "no_text" else check for check in prepared["checks"]]
    return {**prepared, "privacy_available": True, "checks": checks}


def add_routes(app: FastAPI, core: CoreService, mutating: Mutating) -> CaptureDesk:
    desk = CaptureDesk(core)

    @app.get("/v1/sessions/{session_id}/captures")
    async def list_captures(session_id: str) -> dict[str, Any]:
        return desk.list(session_id)

    @app.post("/v1/captures")
    async def create(request: Request) -> Response:
        key = request.headers.get("idempotency-key", "")
        if not re.fullmatch(r"cli_[0-9A-HJKMNP-TV-Z]{26}", key):
            raise ApiError("invalid_request", "Idempotency-Key required")
        core._guard()
        core.limit_command(request.url.path)
        content_type = request.headers.get("content-type", "")
        if content_type.startswith("application/json"):
            raw_text = await request.body()
            if len(raw_text) > 32768:
                raise ApiError("payload_too_large")
            try:
                meta = json.loads(raw_text)
            except ValueError:
                raise ApiError("validation_failed") from None
            if not isinstance(meta, dict) or meta.get("client_capture_id") != key:
                raise ApiError("validation_failed")
            return JSONResponse(desk.create(meta, b""))
        if not content_type.startswith("multipart/form-data;") or len(content_type) > 200:
            raise ApiError("unsupported_format")
        raw = bytearray()
        async for chunk in request.stream():
            raw.extend(chunk)
            if len(raw) > 20 * 1024 * 1024 + 8192:
                raise ApiError("image_too_large")
        message = BytesParser(policy=policy.default).parsebytes(
            b"Content-Type: " + content_type.encode("ascii") + b"\r\nMIME-Version: 1.0\r\n\r\n" + bytes(raw)
        )
        parts = list(message.iter_parts())
        if len(parts) != 2 or {p.get_param("name", header="content-disposition") for p in parts} != {"meta", "file"}:
            raise ApiError("validation_failed", "photo and meta required")
        values = {p.get_param("name", header="content-disposition"): p.get_payload(decode=True) for p in parts}
        try:
            meta = json.loads(cast(bytes, values["meta"]))
        except (ValueError, TypeError):
            raise ApiError("validation_failed") from None
        if not isinstance(meta, dict) or meta.get("client_capture_id") != key:
            raise ApiError("validation_failed", "capture idempotency key must match client capture id")
        return JSONResponse(desk.create(meta, cast(bytes, values["file"])), status_code=200)

    @app.get("/v1/captures/{capture_id}")
    async def get(capture_id: str) -> dict[str, Any]:
        return desk.get(capture_id)

    @app.get("/v1/captures/{capture_id}/images/{image_id}")
    async def image(capture_id: str, image_id: str) -> Response:
        record = desk.get(capture_id)
        attachment = next((a for a in record["attachments"] if a["image_id"] == image_id), None)
        if attachment is None:
            raise ApiError("not_found")
        path = desk.root() / f"{image_id}.jpg"
        if not path.is_file():
            raise ApiError("not_found")
        data = path.read_bytes()
        if hashlib.sha256(data).hexdigest() != attachment["sha256"]:
            raise ApiError("not_found")
        return Response(
            data, media_type="image/jpeg", headers={"cache-control": "no-store", "x-content-type-options": "nosniff"}
        )

    @app.patch("/v1/captures/{capture_id}/crop")
    async def crop(capture_id: str, request: Request) -> Response:
        async def run(body: Any) -> tuple[int, Any]:
            return 200, await desk.crop(capture_id, body)

        return await mutating(request, run)

    @app.post("/v1/captures/{capture_id}/read")
    async def read(capture_id: str, request: Request) -> Response:
        async def run(body: Any) -> tuple[int, Any]:
            return 202, desk.read(capture_id, body)

        return await mutating(request, run)

    @app.post("/v1/captures/{capture_id}/confirm")
    async def confirm(capture_id: str, request: Request) -> Response:
        async def run(body: Any) -> tuple[int, Any]:
            return 200, desk.confirm(capture_id, body)

        return await mutating(request, run)

    @app.delete("/v1/captures/{capture_id}")
    async def delete(capture_id: str, request: Request) -> Response:
        async def run(_: Any) -> tuple[int, Any]:
            desk.delete(capture_id)
            return 204, None

        return await mutating(request, run)

    return desk
