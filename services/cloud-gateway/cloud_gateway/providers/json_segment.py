"""A deployment-selected segmentation service using the device's existing boxes/points and RLE mask shape."""

import asyncio
import json
import math
import re
from collections.abc import Callable, Sequence
from typing import Any

import httpx

from cloud_gateway.config import Segmentation
from cloud_gateway.custody import Secret
from cloud_gateway.errors import classify_upstream
from cloud_gateway.providers.base import UpstreamError
from cloud_gateway.providers.http import upstream_error
from cloud_gateway.providers.segmentation import ObjectPrompt, Segment, SegmentReply

IDENTIFIER = re.compile(r"^[A-Za-z0-9_.:-]{1,64}$")


def decode_objects(
    raw: Any, width: int, height: int, spec: Segmentation, prompts: Sequence[ObjectPrompt]
) -> tuple[Segment, ...]:
    if (
        not isinstance(raw, dict)
        or not isinstance(raw.get("objects"), list)
        or type(raw.get("images", 1)) is not int
        or raw.get("images", 1) != 1
    ):
        raise UpstreamError("bad_response", "invalid segmentation result")
    rows = raw["objects"]
    if len(rows) > spec.max_prompts:
        raise UpstreamError("bad_response", "too many returned objects")
    expected, seen, segments = {str(prompt.id) for prompt in prompts}, set(), []
    pixels = 0
    for row in rows:
        if not isinstance(row, dict):
            raise UpstreamError("bad_response", "invalid segmentation object")
        raw_id = row.get("id")
        if type(raw_id) not in (str, int):
            raise UpstreamError("bad_response", "segmentation object identity mismatch")
        identity = str(raw_id)
        if not IDENTIFIER.fullmatch(identity) or identity in seen or (expected and identity not in expected):
            raise UpstreamError("bad_response", "segmentation object identity mismatch")
        seen.add(identity)
        box = row.get("box")
        if (
            not isinstance(box, list)
            or len(box) != 4
            or any(type(n) is not int for n in box)
            or not (0 <= box[0] < box[2] <= width and 0 <= box[1] < box[3] <= height)
        ):
            raise UpstreamError("bad_response", "segmentation box is outside its image")
        w, h = box[2] - box[0], box[3] - box[1]
        pixels += w * h
        if pixels > spec.max_pixels:
            raise UpstreamError("bad_response", "segmentation masks exceed the pixel limit")
        mask = row.get("mask")
        if (
            not isinstance(mask, dict)
            or type(mask.get("width")) is not int
            or type(mask.get("height")) is not int
            or mask.get("width") != w
            or mask.get("height") != h
        ):
            raise UpstreamError("bad_response", "segmentation mask dimensions mismatch")
        runs = mask.get("runs")
        if (
            not isinstance(runs, list)
            or not runs
            or len(runs) > w * h + 1
            or any(type(n) is not int or n < 0 or n > w * h for n in runs)
            or sum(runs) != w * h
        ):
            raise UpstreamError("bad_response", "segmentation mask runs are invalid")
        confidence = row.get("confidence")
        if confidence is not None and (
            type(confidence) not in (int, float) or not math.isfinite(confidence) or not 0 <= confidence <= 1
        ):
            raise UpstreamError("bad_response", "segmentation confidence is invalid")
        segments.append(Segment(identity, (box[0], box[1], box[2], box[3]), confidence, run_lengths=tuple(runs)))
    return tuple(segments)


class JsonSegmenter:
    def __init__(
        self, spec: Segmentation, key: Secret | None, client: httpx.AsyncClient, clock: Callable[[], float]
    ) -> None:
        self.spec, self._key, self.client, self.clock = spec, key, client, clock

    @property
    def configured(self) -> bool:
        return bool(
            self.spec.enabled
            and self.spec.permission
            and self.spec.permission.permits(self.clock())
            and (self.spec.secret is None or self._key is not None)
        )

    async def segment(
        self,
        image_url: str,
        width: int,
        height: int,
        *,
        objects: Sequence[ObjectPrompt] = (),
        concept: str | None = None,
    ) -> SegmentReply:
        if not self.configured:
            raise UpstreamError("overloaded", "segmentation service permission unavailable", extra={"unbilled": True})
        if width * height > self.spec.max_pixels:
            raise UpstreamError("payload_too_large", "segmentation pixel limit", extra={"unbilled": True})
        body: dict[str, Any] = {"model": self.spec.model, "image": image_url, "width": width, "height": height}
        if concept is not None:
            body["concept"] = concept
        else:
            body["objects"] = [
                {
                    "id": obj.id,
                    "box": list(obj.box) if obj.box else None,
                    "points": [list(point) for point in obj.points],
                }
                for obj in objects
            ]
        headers = {"content-type": "application/json"}
        if self._key is not None:
            headers["authorization"] = "Bearer " + self._key.reveal()
        try:
            async with asyncio.timeout(self.spec.timeout_s):
                async with self.client.stream(
                    "POST",
                    self.spec.base_url,
                    headers=headers,
                    json=body,
                    timeout=httpx.Timeout(self.spec.timeout_s, connect=5),
                ) as response:
                    chunks, size = [], 0
                    async for chunk in response.aiter_bytes():
                        size += len(chunk)
                        if size > self.spec.max_response_bytes:
                            raise UpstreamError("bad_response", "segmentation response exceeds its byte limit")
                        chunks.append(chunk)
                    raw = b"".join(chunks)
                    if response.status_code >= 400:
                        raise upstream_error(response.status_code, raw, response.headers, classify_upstream)
        except TimeoutError:
            raise UpstreamError("timeout", "segmentation deadline exceeded") from None
        except httpx.HTTPError as exc:
            reason = "timeout" if isinstance(exc, httpx.TimeoutException) else "network_unavailable"
            raise UpstreamError(reason, "segmentation transport failed") from None
        try:
            result = json.loads(raw)
        except ValueError:
            raise UpstreamError("bad_response", "segmentation response is not JSON") from None
        return SegmentReply(decode_objects(result, width, height, self.spec, objects), 1)

    async def aclose(self) -> None:
        await self.client.aclose()
