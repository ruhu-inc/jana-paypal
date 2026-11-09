"""M42 cloud coordinator transport. Every send uses the existing pinned, consent-gated egress client."""

from __future__ import annotations

import base64
import hashlib
import json
from collections.abc import Callable
from typing import Any, cast

from agent_core.contracts import errors
from agent_core.net.client import NetError
from agent_core.net.policy import EgressRefused
from agent_core.net.redaction import Redactor
from agent_core.store import consents, subscription
from agent_core.store.egress import DeviceSpend


class Extracted(list):  # type: ignore[type-arg]
    """A video file's extraction (SPEC §20.4): the units, plus the frames read as text (screened ones counted with
    no text) and whether the container had a video stream (False downgrades the source to a recording)."""

    def __init__(self, units: Any = (), *, frames: list[dict[str, Any]] | None = None,
                 has_video: bool | None = None) -> None:  # fmt: skip
        super().__init__(units)
        self.frames: list[dict[str, Any]] = list(frames or [])
        self.has_video: bool | None = has_video


class StudyCloudError(ValueError):
    def __init__(self, reason: str):
        super().__init__(reason)
        self.reason = reason


class StudyCloud:
    def __init__(self, layer: Any, profile: Callable[[], str | None], connection: Callable[[], Any]):
        self.layer, self.profile, self.connection = layer, profile, connection

    def meter(self, conn: Any, output: dict[str, Any]) -> None:
        usage = output.get("usage")
        if usage is None:
            return  # Compatible with an older gateway; release qualification requires usage receipts.
        if errors("study/workspace#/$defs/UsageReceipt", usage):
            raise StudyCloudError("processing_failed")
        pid = self.guard(conn)
        DeviceSpend(
            self.layer.device,
            pid,
            subscription.allowance_period(self.layer.subscription(), self.layer.clock_ms()),
            self.layer.clock_ms,
        ).add_study(usage["receipt_id"], usage["cost_usd"], usage["input_tokens"] + usage["output_tokens"])

    def guard(self, conn: Any, service: str = "study") -> str:
        pid = self.profile()
        if conn is not self.connection() or pid is None:
            raise StudyCloudError("consent_required")
        if not self.layer.service_allowed(conn, service):
            raise StudyCloudError("model_unavailable")
        if not {"study_materials", "cloud_tier"} <= self.layer.features_on():
            raise StudyCloudError("consent_required")
        if not subscription.covers(self.layer.subscription(), pid, self.layer.clock_ms()):
            raise StudyCloudError("allowance_exhausted")
        band = conn.execute("SELECT age_band FROM profile").fetchone()
        for scope in ("study_materials", "cloud_tier"):
            row = consents.active(conn, scope)
            if row is None or row[5] is not None:
                raise StudyCloudError("consent_required")
            if band and band[0] != "18_plus" and (row[1] != "guardian" or row[2] != "checkout_guardian"):
                raise StudyCloudError("consent_required")
            expiry = conn.execute("SELECT expires_at FROM consents WHERE consent_id=?", (row[0],)).fetchone()
            if expiry and expiry[0] is not None and expiry[0] <= self.layer.clock_ms():
                raise StudyCloudError("consent_required")
        return pid

    async def send(
        self,
        conn: Any,
        method: str,
        path: str,
        body: Any = None,
        *,
        cleanup: bool = False,
        attempt: int = 1,
        request_id: str | None = None,
    ) -> Any:
        service = (
            "research" if path.startswith("/v1/research") else "narration" if path == "/v1/podcast/speech" else "study"
        )
        pid = self.profile() if cleanup else self.guard(conn, service)
        try:
            token = await self.layer.credential.token()
            if not cleanup:
                self.guard(conn, service)  # token renewal may have yielded through a profile/consent change
            reply = await self.layer.client.send(
                "study_materials",
                method,
                "https://" + self.layer.host_for("study_materials") + path,
                "tombstone" if cleanup else "study_material",
                body=body,
                headers={"authorization": "Bearer " + token, "x-study-attempt": str(attempt)},
                profile_id=pid,
                timeout_s=9000 if path == "/v1/study/extract" else 360,
            )
        except EgressRefused as error:
            raise StudyCloudError("waiting_online" if error.reason == "offline" else "consent_required") from None
        except NetError:
            raise StudyCloudError("waiting_online") from None
        if not cleanup:
            self.guard(conn, service)
            identity = request_id or (body.get("request_id") if isinstance(body, dict) else None)
            if identity:
                conn.execute(
                    "UPDATE study_cloud_cleanup SET expires_at=? WHERE request_id=?",
                    (self.layer.clock_ms() + 900000, identity),
                )
        if not 200 <= reply.status < 300:
            code = reply.body.get("error", {}).get("code", "") if isinstance(reply.body, dict) else ""
            raise StudyCloudError(
                code
                if code
                in (
                    "allowance_exhausted",
                    "consent_required",
                    "insufficient_evidence",
                    "source_too_large",
                    "encrypted_source",
                    "corrupt_source",
                    "unreadable_source",
                    "model_unavailable",
                    "neural_speech_not_configured",
                    "neural_speech_key_rejected",
                    "neural_speech_limit",
                    "neural_speech_already_sent",
                    "neural_speech_unavailable",
                    "neural_speech_invalid_audio",
                    "safety_blocked",
                    "research_not_found",
                )
                else "research_not_found"
                if reply.status == 404 and path.startswith("/v1/research")
                else "processing_failed"
            )
        return reply.body

    async def podcast_audio(self, conn: Any, text: str, request_id: str, voice_index: int) -> bytes:
        """The API supplies a frozen, validated Podcast passage; no raw source or browser-supplied text leaves."""
        self.guard(conn, "narration")
        if not 1 <= len(text) <= 600:
            raise StudyCloudError("neural_speech_unavailable")
        profile = conn.execute("SELECT display_name,profile_id FROM profile").fetchone()
        if Redactor(profile or ()).blocks({"text": text}):
            raise StudyCloudError("safety_blocked")
        reply = await self.send(
            conn,
            "POST",
            "/v1/podcast/speech",
            {
                "request_id": request_id,
                "text": text,
                "voice_index": voice_index,
            },
        )
        if not isinstance(reply, dict) or not isinstance(reply.get("audio_base64"), str):
            raise StudyCloudError("neural_speech_invalid_audio")
        try:
            if len(reply["audio_base64"]) > 8_000_000:
                raise ValueError
            data = base64.b64decode(reply["audio_base64"], validate=True)
            if not 44 <= len(data) <= 6_000_000 or data[:4] != b"RIFF" or data[8:12] != b"WAVE":
                raise ValueError
            return data
        except ValueError:
            raise StudyCloudError("neural_speech_invalid_audio") from None

    def owe(self, conn: Any, jid: str, sources: list[dict[str, Any]]) -> None:
        # Opaque ids only. A receipt survives source erasure; it never contains source content or a credential.
        for source in sources:
            conn.execute(
                "INSERT INTO study_cloud_cleanup VALUES (?,?,?) "
                "ON CONFLICT(request_id,source_id) DO UPDATE SET expires_at=excluded.expires_at",
                (jid, source["source_id"], self.layer.clock_ms() + 900000),
            )

    async def cleanup(self, conn: Any) -> None:
        for jid, expiry in conn.execute("SELECT DISTINCT request_id,expires_at FROM study_cloud_cleanup").fetchall():
            if expiry > self.layer.clock_ms():
                try:
                    await self.send(conn, "DELETE", "/v1/study/requests/" + jid, cleanup=True)
                except Exception:  # noqa: S112 - retain receipt; transport bodies are never logged
                    continue
            conn.execute("DELETE FROM study_cloud_cleanup WHERE request_id=?", (jid,))

    async def process(
        self,
        conn: Any,
        jid: str,
        request: dict[str, Any],
        chunks: list[dict[str, Any]],
        attempt: int,
        batch_index: int = 0,
    ) -> dict[str, Any]:
        self.guard(conn)
        original_jid = jid
        alphabet = "0123456789ABCDEFGHJKMNPQRSTVWXYZ"
        number = int(hashlib.sha256(f"{jid}:{batch_index}".encode()).hexdigest()[:32], 16)
        chars = []
        for _ in range(26):
            chars.append(alphabet[number % 32])
            number //= 32
        jid = "stj_" + "".join(reversed(chars))
        profile = conn.execute("SELECT display_name,profile_id FROM profile").fetchone()
        body: dict[str, Any] = {
            "request_id": jid,
            "kind": request["kind"],
            "chunks": chunks,
            "output_locale": "en",
            **{
                k: request[k]
                for k in ("prompt", "count", "settings", "syllabus_outline", "previous_response", "learner")
                if k in request
            },
        }
        if Redactor(profile or ()).blocks(body):
            raise StudyCloudError("safety_blocked")
        self.owe(conn, jid, request["sources"])
        output = await self.send(conn, "POST", "/v1/study/process", body, attempt=attempt)
        if errors("study/workspace#/$defs/ProcessingResponse", output):
            raise StudyCloudError("insufficient_evidence")
        if output["request_id"] != jid:
            raise StudyCloudError("insufficient_evidence")
        self.meter(conn, output)
        return {**output, "request_id": original_jid}

    async def concept_topic(self, conn: Any, request_id: str, topic: str) -> dict[str, Any]:
        """Muse Spark's no-source concept map of a typed topic (`ConceptTopicDraft`): consent, subscription and the
        Study allowance as any Study request; the topic is the only learner text sent."""
        self.guard(conn)
        body = {"request_id": request_id, "topic": topic, "output_locale": "en"}
        profile = conn.execute("SELECT display_name,profile_id FROM profile").fetchone()
        if Redactor(profile or ()).blocks(body):
            raise StudyCloudError("safety_blocked")
        output = await self.send(conn, "POST", "/v1/study/concept-topic", body)
        if errors("study/workspace#/$defs/ConceptTopicDraft", output) or output["request_id"] != request_id:
            raise StudyCloudError("insufficient_evidence")
        self.meter(conn, output)
        return cast(dict[str, Any], output)

    async def mark_answer(self, conn: Any, request_id: str, body: dict[str, Any]) -> str:
        """A written answer marked online (`AnswerMarking`): consent, subscription and the redaction check as any Study
        request. Only the question, the answer, the reference and the passages the question cites are sent."""
        self.guard(conn)
        payload = {"request_id": request_id, **body, "output_locale": "en"}
        profile = conn.execute("SELECT display_name,profile_id FROM profile").fetchone()
        if Redactor(profile or ()).blocks(payload):
            raise StudyCloudError("safety_blocked")
        output = await self.send(conn, "POST", "/v1/study/mark", payload)
        if errors("study/workspace#/$defs/AnswerVerdict", output) or output["request_id"] != request_id:
            raise StudyCloudError("insufficient_evidence")
        self.meter(conn, output)
        return str(output["verdict"])

    async def research_start(self, conn: Any, request: dict[str, Any]) -> dict[str, Any]:
        """The approved brief, and a photographed diagram's confirmed part names, to the gateway's research job (SPEC
        §18.3): consent, subscription and the redaction check as any Study request. The brief's fields are the only
        learner text sent; the conversation and the photo stay on the device."""
        self.guard(conn, "research")
        profile = conn.execute("SELECT display_name,profile_id FROM profile").fetchone()
        if Redactor(profile or ()).blocks(request):
            raise StudyCloudError("safety_blocked")
        return self._research_job(await self.send(conn, "POST", "/v1/research", request))

    async def research_job(self, conn: Any, research_id: str) -> dict[str, Any]:
        return self._research_job(await self.send(conn, "GET", f"/v1/research/{research_id}"))

    async def research_cancel(self, conn: Any, research_id: str) -> dict[str, Any]:
        return self._research_job(await self.send(conn, "POST", f"/v1/research/{research_id}/cancel"))

    async def research_forget(self, conn: Any, research_id: str) -> None:
        """The gateway deletes a job's pages once they are fetched (SPEC §18.6); this asks it to now."""
        await self.send(conn, "DELETE", f"/v1/research/{research_id}")

    @staticmethod
    def _research_job(job: Any) -> dict[str, Any]:
        if errors("study/research#/$defs/Job", job):
            raise StudyCloudError("processing_failed")
        return cast(dict[str, Any], job)

    async def extract(
        self, conn: Any, jid: str, request: dict[str, Any], source: dict[str, Any], data: bytes
    ) -> list[dict[str, Any]]:
        """The gateway's extraction as units; for a video file (SPEC §20.4) the list is an `Extracted` carrying the
        frames read as text and whether the container had a video stream."""
        self.guard(conn)
        self.owe(conn, jid, request["sources"])
        attempt = conn.execute("SELECT attempt FROM study_jobs WHERE job_id=?", (jid,)).fetchone()[0]
        grant = await self.send(
            conn,
            "POST",
            "/v1/study/uploads",
            {
                "request_id": jid,
                "kind": source["kind"],
                "mime_type": "application/octet-stream",
                "byte_length": len(data),
                "sha256": hashlib.sha256(data).hexdigest(),
            },
            attempt=attempt,
        )
        if errors("study/workspace#/$defs/CloudUploadGrant", grant):
            raise StudyCloudError("processing_failed")
        for offset in range(0, len(data), 1048576):
            part = data[offset : offset + 1048576]
            await self.send(
                conn,
                "PUT",
                f"/v1/study/uploads/{grant['upload_id']}/chunks/{offset // 1048576}",
                {
                    "offset": offset,
                    "sha256": hashlib.sha256(part).hexdigest(),
                    "data_base64": base64.b64encode(part).decode(),
                },
                attempt=attempt,
                request_id=jid,
            )
        page = await self.send(
            conn,
            "POST",
            "/v1/study/extract",
            {
                "request_id": jid,
                "upload_id": grant["upload_id"],
                "source_id": source["source_id"],
                "revision": source["revision"],
                "locale": "en",
                **({"frames": source["frames_choice"]} if source.get("frames_choice") else {}),
            },
            attempt=attempt,
        )
        chunks, unreadable = [], []
        duration_ms = page.get("duration_ms")
        frames, has_video = page.get("frames", []), page.get("has_video")
        seen = set()
        while True:
            if (
                errors("study/workspace#/$defs/ExtractionResponse", page)
                or page["sha256"] != hashlib.sha256(data).hexdigest()
            ):
                raise StudyCloudError("processing_failed")
            chunks += page["chunks"]
            unreadable = page["unreadable_units"]
            cursor = page["next_cursor"]
            if cursor is None:
                break
            if cursor in seen or len(chunks) > 10000:
                raise StudyCloudError("processing_failed")
            seen.add(cursor)
            page = await self.send(
                conn, "GET", f"/v1/study/extractions/{jid}/chunks?cursor={cursor}", attempt=attempt, request_id=jid
            )
        self.meter(conn, page)
        # Only pages read from an image need the learner's review: a text-layer PDF is ready as it stands.
        scanned = page.get("scanned_units")
        kind_has_text_layer = source["kind"] in ("pdf", "document", "slides")
        # The local store owns final chunk identities, normalization and redaction. Never trust remote offsets.
        units: dict[str, dict[str, Any]] = {}
        for chunk in chunks:
            if chunk["source_id"] != source["source_id"] or chunk["revision"] != source["revision"]:
                raise StudyCloudError("processing_failed")
            key = json.dumps(chunk["locator"], sort_keys=True)
            number = chunk["locator"].get("page") or chunk["locator"].get("slide")
            read_as_text = scanned is not None and (number is None or number not in scanned) and kind_has_text_layer
            unit = units.setdefault(key, {"locator": chunk["locator"], "text": "", "ocr": not read_as_text})
            unit["text"] += chunk["text"]
        result = Extracted(units.values())
        result.frames, result.has_video = [dict(f) for f in frames], has_video
        for page_number in unreadable:
            result.insert(
                min(page_number - 1, len(result)),
                {
                    "locator": (
                        {
                            "kind": "time",
                            "start_ms": (page_number - 1) * 60000,
                            "end_ms": min(page_number * 60000, duration_ms),
                        }
                        if source["kind"] == "recording" and duration_ms
                        else {"kind": "page", "page": page_number}
                    ),
                    "text": "",
                    "ocr": True,
                },
            )
        return result
