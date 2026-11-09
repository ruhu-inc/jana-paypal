"""SAM 3.1 through Meta's Responses API (JM-D10, SPEC §5): an image and either object prompts (boxes and points) or one
text concept in, boxes and masks out.

Behaviour checked against https://dev.meta.ai/docs/sam/segmenting and /docs/sam/reading-segmentation on 2026-10-26,
and with a real call the same day:

- `POST /v1/responses`, `model: "sam-3.1"`, one user message with an `input_text` and an `input_image` (a data URL).
- Object prompts are not JSON fields: they are written in the output grammar inside the `input_text`
  (`<0f>1<|box;x1;y1;x2;y2;w;h|>,2<|point;...|>`), in integer pixels of the image, `x2`/`y2` inclusive. A request
  carries a noun phrase **or** object prompts, never both; one concept per request; at most 16 prompted objects. Box
  and point prompts always come back as `one_bit` masks.
- The reply is special-token text in `output_text`: per object a box and a mask whose raster covers exactly the box,
  range-coded in Meta's own base85 alphabet. It is decoded with Meta's reference parser (`meta-sam-parser`, SAM
  License). An empty `output_text` is "no match", never an error.
- Billing is per image ($2.50 per 1,000); the reply's `usage.images_processed` says how many (undocumented; seen on
  every call). Token counts are zero.

Nothing here logs an image, a prompt or a mask. The key goes in the `Authorization` header only."""

from __future__ import annotations

from collections.abc import Callable, Sequence
from typing import Any

import httpx
from meta_sam_parser import (
    CompletedOutcome,
    FramePrompt,
    InvalidSegmentationMaskError,
    PromptBox,
    PromptObject,
    PromptPoint,
    SegmentationBoxRecord,
    SegmentationMaskRecord,
    build_object_prompt,
    decode_mask_to_raster,
    image_segmentation_format,
)

from cloud_gateway.config import Sam
from cloud_gateway.custody import Secret
from cloud_gateway.providers.base import UpstreamError
from cloud_gateway.providers.http import post_json
from cloud_gateway.providers.segmentation import ObjectPrompt, Segment, SegmentReply


def object_prompt(width: int, height: int, objects: Sequence[ObjectPrompt]) -> str:
    """The `input_text` for box and point prompts on an image (frame 0)."""
    return build_object_prompt(
        size=(width, height),
        objects=[
            PromptObject(
                id=o.id,
                prompts=[
                    FramePrompt(
                        frame=0,
                        box=PromptBox(*o.box) if o.box is not None else None,
                        points=[PromptPoint(x, y, label) for x, y, label in o.points],
                    )
                ],
            )
            for o in objects
        ],
    )


def decode(text: str, *, max_pixels: int = 16_000_000) -> tuple[Segment, ...]:
    """The segments in a SAM `output_text`. Empty text is no match. A record whose mask does not decode is dropped
    (never guessed); text that is not the grammar at all is a `bad_response`."""
    if not text.strip():
        return ()
    parser = image_segmentation_format().create_parser()
    parser.push(text, emit=False)
    result = parser.finish(CompletedOutcome()).result
    if not any(isinstance(r, (SegmentationBoxRecord, SegmentationMaskRecord)) for r in result.records):
        # Text that holds no record at all is not "no match" (that is empty text): the reply is not segmentation.
        raise UpstreamError("bad_response", "segmentation text did not parse")
    boxes = {r.object_id: r for r in result.records if isinstance(r, SegmentationBoxRecord)}
    out = []
    pixels = 0
    for record in result.records:
        if not isinstance(record, SegmentationMaskRecord):
            continue
        bounds = record.bounds
        pixels += record.mask.width * record.mask.height
        if pixels > max_pixels:
            raise UpstreamError("bad_response", "segmentation masks exceed the pixel limit")
        box = tuple(int(v) for v in (bounds.left, bounds.top, bounds.right, bounds.bottom))
        try:
            raster = decode_mask_to_raster(record.mask)
        except InvalidSegmentationMaskError:
            continue
        if len(raster) != record.mask.width * record.mask.height:
            continue
        if (box[2] - box[0], box[3] - box[1]) != (record.mask.width, record.mask.height):
            # The raster covers its box in every documented example; anything else is placed at the box's corner
            # with the raster's own size, so a mask is never stretched.
            box = (box[0], box[1], box[0] + record.mask.width, box[1] + record.mask.height)
        confidence = record.confidence
        if confidence is None and record.object_id in boxes:
            confidence = boxes[record.object_id].confidence
        out.append(Segment(record.object_id, box, confidence, raster))  # type: ignore[arg-type]
    return tuple(out)


def _output_text(raw: Any) -> str:
    if not isinstance(raw, dict) or raw.get("status") not in (None, "completed"):
        raise UpstreamError("bad_response", "the segmentation response did not complete")
    parts = []
    for item in raw.get("output") or []:
        if not isinstance(item, dict) or item.get("type") != "message":
            continue
        for content in item.get("content") or []:
            if isinstance(content, dict) and content.get("type") == "output_text":
                parts.append(str(content.get("text") or ""))
    return "".join(parts)


class SamClient:
    def __init__(
        self, spec: Sam, key: Secret | None, client: httpx.AsyncClient, permitted: Callable[[], bool] = lambda: True
    ) -> None:
        self.spec = spec
        self.permitted = permitted
        self._key = key
        self.client = client
        self.timeout = httpx.Timeout(spec.timeout_s, connect=5.0)

    def __repr__(self) -> str:
        return f"SamClient(base_url={self.spec.base_url!r}, model={self.spec.model!r})"

    @property
    def configured(self) -> bool:
        return self.spec.enabled and self._key is not None and self.permitted()

    def body(self, image_url: str, prompt: str, *, one_bit: bool) -> dict[str, Any]:
        return {
            "model": self.spec.model,
            "input": [{"type": "message", "role": "user", "content": [
                {"type": "input_text", "text": prompt}, {"type": "input_image", "image_url": image_url}]}],
            # Values are strings: a JSON boolean is a 400.
            "metadata": {"mask_encoding": "one_bit" if one_bit else "lossless", "include_confidence": "true"},
            "store": False,
        }  # fmt: skip

    async def segment(
        self,
        image_url: str,
        width: int,
        height: int,
        *,
        objects: Sequence[ObjectPrompt] = (),
        concept: str | None = None,
    ) -> SegmentReply:
        if (concept is None) == (not objects):
            raise UpstreamError(
                "format_error", "one text concept or object prompts, never both", extra={"unbilled": True}
            )
        if len(objects) > self.spec.max_prompts:
            raise UpstreamError("format_error", "too many prompted objects", extra={"unbilled": True})
        if self._key is None or not self.permitted():
            raise UpstreamError("upstream_auth", "no provider key in the secret source", extra={"unbilled": True})
        prompt = concept if concept is not None else object_prompt(width, height, objects)
        headers = {"authorization": f"Bearer {self._key.reveal()}", "content-type": "application/json"}
        url = f"{self.spec.base_url.rstrip('/')}/responses"
        raw = await post_json(self.client, url, headers, self.body(image_url, prompt, one_bit=True), self.timeout)
        usage = raw.get("usage") if isinstance(raw, dict) else None
        images = usage.get("images_processed") if isinstance(usage, dict) else None
        billed = images if type(images) is int and images >= 0 else 1  # the documented price is per image
        return SegmentReply(decode(_output_text(raw), max_pixels=self.spec.max_pixels), billed)

    async def aclose(self) -> None:
        await self.client.aclose()
