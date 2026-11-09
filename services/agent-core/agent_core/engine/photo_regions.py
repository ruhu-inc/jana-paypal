"""Exact places on the student's own photo (JM-D10, SPEC §5): SAM 3.1 sharpens the regions Muse Spark read.

After a cloud read, each region Spark boxed (its 0..1000 grid, already 0..1 here) becomes a box prompt in the
screened image's own pixels; the gateway's `/v1/vision/segment` returns a mask per region, and each mask becomes a
small outline polygon (0..1 of the same image) and a tight box. The outline is what the student's photo shows when
Jana points at a line of working or a part of a sketch. A region SAM did not find, or found with low confidence,
keeps Spark's box and gets no outline: nothing is guessed.

When the first read marked a region uncertain (`[?]` or a low-confidence span), its SAM box is cropped from the same
screened image and Spark reads the crops once more, all in one call. A crop's re-read replaces the region's text only
when it is complete (no `[?]`); the student still confirms every line before anything uses it (INP-01).

Only the screened image ever leaves the device (INP-20). Nothing here logs an image, a mask or a transcription."""

from __future__ import annotations

import base64
import io
import math
import re
from collections.abc import Awaitable, Callable
from typing import Any

from PIL import Image

Segment = Callable[[dict[str, Any]], Awaitable[dict[str, Any]]]

MAX_OBJECTS = 16  # SAM 3.1's limit of prompted objects per request
MIN_CONFIDENCE = 0.4  # below this a mask is not trusted to show the student where something is
PAD_SHARE = 0.01  # Spark's boxes are approximate: each prompt box is widened by this share of the image
OUTLINE_ROWS = 24  # at most this many rows on each side of an outline (so at most 48 points)
MAX_CROPS = 4


def prompt_box(bbox: dict[str, float], width: int, height: int) -> tuple[int, int, int, int] | None:
    """A region's 0..1 box as a padded, half-open pixel box inside the image, or None when it is empty."""
    pad_x, pad_y = math.ceil(PAD_SHARE * width), math.ceil(PAD_SHARE * height)
    left = max(0, math.floor(bbox["x"] * width) - pad_x)
    top = max(0, math.floor(bbox["y"] * height) - pad_y)
    right = min(width, math.ceil((bbox["x"] + bbox["w"]) * width) + pad_x)
    bottom = min(height, math.ceil((bbox["y"] + bbox["h"]) * height) + pad_y)
    return (left, top, right, bottom) if right > left and bottom > top else None


def rows_of(mask: dict[str, Any]) -> list[tuple[int, int] | None]:
    """Each mask row's leftmost and rightmost set pixel (None for an empty row), from row-major runs that start with
    zeros."""
    width, height = int(mask["width"]), int(mask["height"])
    rows: list[tuple[int, int] | None] = [None] * height
    position, value = 0, 0
    for run in mask["runs"]:
        if value == 1 and run > 0:
            start, end = position, position + run - 1
            for row in range(start // width, end // width + 1):
                if row >= height:
                    break
                first = max(start, row * width) - row * width
                last = min(end, row * width + width - 1) - row * width
                seen = rows[row]
                rows[row] = (first, last) if seen is None else (min(seen[0], first), max(seen[1], last))
        position += run
        value ^= 1
    return rows


def outline(mask: dict[str, Any], box: list[int], width: int, height: int) -> list[dict[str, float]] | None:
    """A closed polygon around the mask in 0..1 image coordinates: the left edge down, the right edge back up, over
    at most `OUTLINE_ROWS` evenly spaced rows. None for an empty mask."""
    rows = rows_of(mask)
    filled = [(index, span) for index, span in enumerate(rows) if span is not None]
    if not filled:
        return None
    step = max(1, math.ceil(len(filled) / OUTLINE_ROWS))
    picked = filled[::step]
    if picked[-1] is not filled[-1]:
        picked.append(filled[-1])
    left, top = box[0], box[1]

    def point(x: float, y: float) -> dict[str, float]:
        return {"x": round(min(1.0, max(0.0, x / width)), 4), "y": round(min(1.0, max(0.0, y / height)), 4)}

    down = [point(left + span[0], top + index) for index, span in picked]
    up = [point(left + span[1] + 1, top + index + 1) for index, span in reversed(picked)]
    polygon = down + up
    return polygon if len(polygon) >= 3 else None


def tight_box(mask: dict[str, Any], box: list[int], width: int, height: int) -> dict[str, float] | None:
    rows = rows_of(mask)
    filled = [(index, span) for index, span in enumerate(rows) if span is not None]
    if not filled:
        return None
    x0 = box[0] + min(span[0] for _, span in filled)
    x1 = box[0] + max(span[1] for _, span in filled) + 1
    y0, y1 = box[1] + filled[0][0], box[1] + filled[-1][0] + 1
    return {"x": x0 / width, "y": y0 / height, "w": (x1 - x0) / width, "h": (y1 - y0) / height}


async def sharpen(segment: Segment, image_url: str, width: int, height: int, regions: list[dict[str, Any]]) -> int:
    """Outline the regions in place (each gets `outline`, and its `bbox` tightened to the mask); returns how many were
    outlined. A failed or empty segmentation leaves every region as Spark read it."""
    objects: list[dict[str, Any]] = []
    index: dict[int, dict[str, Any]] = {}
    for number, region in enumerate(regions):
        bbox = region.get("bbox")
        box = prompt_box(bbox, width, height) if bbox else None
        if box is None or len(objects) >= MAX_OBJECTS:
            continue
        index[number] = region
        objects.append({"id": number, "box": list(box)})
    if not objects:
        return 0
    reply = await segment({"image": image_url, "width": width, "height": height, "objects": objects})
    done = 0
    for found in reply.get("objects") or []:
        try:
            region = index[int(found["id"])]
        except (KeyError, ValueError, TypeError):
            continue
        confidence = found.get("confidence")
        if confidence is not None and confidence < MIN_CONFIDENCE:
            continue
        polygon = outline(found["mask"], found["box"], width, height)
        tight = tight_box(found["mask"], found["box"], width, height)
        if polygon is None or tight is None:
            continue
        region["outline"] = polygon
        region["bbox"] = tight
        done += 1
    return done


def uncertain(region: dict[str, Any]) -> bool:
    return "[?]" in str(region.get("text", "")) or bool(region.get("low_confidence_spans"))


def crop(jpeg: bytes, bbox: dict[str, float]) -> str:
    """The region's box cut from the screened image, as a JPEG data URL (the crop leaves the device only as part of
    the image already screened and sent)."""
    with Image.open(io.BytesIO(jpeg)) as image:
        box = prompt_box(bbox, image.width, image.height) or (0, 0, image.width, image.height)
        out = io.BytesIO()
        image.crop(box).convert("RGB").save(out, "JPEG", quality=90)
    return "data:image/jpeg;base64," + base64.b64encode(out.getvalue()).decode("ascii")


def merge_rereads(regions: list[dict[str, Any]], picked: list[int], lines: list[Any]) -> int:
    """Each complete re-read replaces its region's text (the student still confirms it); returns how many did."""
    changed = 0
    for number, line in zip(picked, lines, strict=False):
        if not isinstance(line, str) or not line.strip() or "[?]" in line or len(line) > 4000:
            continue
        region = regions[number]
        if line.strip() != region["text"].strip():
            region["text"] = line.strip()
            changed += 1
        region["low_confidence_spans"] = []
        region["needs_confirmation"] = True
    return changed


def _plain(text: str) -> str:
    return re.sub(r"\s+|\\\(|\\\)", "", text)


def photo_lines(capture: dict[str, Any], lines: list[tuple[str, str]]) -> dict[str, str]:
    """Which board line came from which region of a confirmed photo of working: step id -> `cap_<id>:rgn_<n>`.

    Lines and regions pair up in order when their counts agree (the student may have corrected a line's text while
    confirming); otherwise a line pairs with the next region whose text is the same. Only regions with a place on
    the photo (an outline or a box) are pointable; a line with no such region has none."""
    regions = [r for r in capture.get("regions") or [] if str(r.get("text", "")).strip()]
    ref = capture["capture_id"] + ":"
    pairs: list[tuple[str, dict[str, Any]]] = []
    if len(regions) == len(lines):
        pairs = [(step_id, region) for (step_id, _), region in zip(lines, regions, strict=True)]
    else:
        at = 0
        for step_id, line in lines:
            match = next((i for i in range(at, len(regions)) if _plain(regions[i]["text"]) == _plain(line)), None)
            if match is not None:
                pairs.append((step_id, regions[match]))
                at = match + 1
    return {step_id: ref + str(r["region_id"]) for step_id, r in pairs if r.get("outline") or r.get("bbox")}
