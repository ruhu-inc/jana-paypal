"""Bounded on-device image preprocessing. Originals and OCR buffers never touch disk."""

from __future__ import annotations

import base64
import hashlib
import io
import json
import re
import warnings
from pathlib import Path
from typing import Any

import cv2
import numpy as np
from PIL import Image, ImageOps

from perception_worker.ocr import words

cv2.setNumThreads(1)
CV: Any = cv2
OCR_DATA = json.loads((Path(__file__).parent / "ocr-assets.json").read_text())
PERSON_DATA = json.loads((Path(__file__).parent / "detectors.json").read_text())
PERSONAL = re.compile(
    r"\b(?:name\s*:|student\s*(?::|id|name|number)|school\s*(?::|id|name|number)|address\s*:|phone\s*:|email\s*:)|"
    r"\bmy (?:full )?name is\b|\bi live (?:at|on|in|near)\b|[\w.+-]+@[\w.-]+\.[A-Za-z]{2,}|(?:\+?\d[\d ()-]{7,}\d)",
    re.I,
)


class ImageRejected(ValueError):
    def __init__(self, code: str) -> None:
        self.code = code
        super().__init__(code)


def decode(data: bytes, max_bytes: int = 20 * 1024 * 1024, max_side: int = 8000) -> np.ndarray[Any, Any]:
    if len(data) > max_bytes:
        raise ImageRejected("image_too_large")
    try:
        with warnings.catch_warnings():
            warnings.simplefilter("error", Image.DecompressionBombWarning)
            with Image.open(io.BytesIO(data), formats=("JPEG", "PNG", "WEBP")) as opened:
                if max(opened.size) > max_side or min(opened.size) < 1:
                    raise ImageRejected("image_too_large")
                return np.array(ImageOps.exif_transpose(opened).convert("RGB"))
    except ImageRejected:
        raise
    except (Image.DecompressionBombError, Image.DecompressionBombWarning):
        raise ImageRejected("image_too_large") from None
    except (ValueError, OSError, SyntaxError):
        raise ImageRejected("unsupported_format") from None


def shrink(image: np.ndarray[Any, Any], long_edge: int) -> np.ndarray[Any, Any]:
    height, width = image.shape[:2]
    ratio = min(1.0, long_edge / max(width, height))
    return (
        image
        if ratio == 1
        else cv2.resize(
            image, (max(1, round(width * ratio)), max(1, round(height * ratio))), interpolation=cv2.INTER_AREA
        )
    )


def face_score(image: np.ndarray[Any, Any], floor: float | None = None) -> float:
    """The best face score of the pinned detector (0 below `floor`); the hashed model bytes are the bytes loaded."""
    face = PERSON_DATA["face_detector"]
    if cv2.__version__ != PERSON_DATA["opencv_version"]:
        raise ImageRejected("photo_reading_unavailable")
    model = (Path(__file__).parent / face["file"]).read_bytes()
    if hashlib.sha256(model).hexdigest() != face["sha256"]:
        raise ImageRejected("photo_reading_unavailable")
    sample = cv2.cvtColor(shrink(image, face["long_edge"]), cv2.COLOR_RGB2BGR)
    detector = CV.FaceDetectorYN.create(
        "onnx",
        np.frombuffer(model, np.uint8),
        np.zeros(0, np.uint8),
        (sample.shape[1], sample.shape[0]),
        face["score_threshold"] if floor is None else floor,
        face["nms_threshold"],
        face["top_k"],
    )
    _, faces = detector.detect(sample)
    return 0.0 if faces is None else float(faces[:, 14].max())


def contains_person(image: np.ndarray[Any, Any]) -> bool:
    # Calibration and the measured trade-off: PERSON_DATA["calibration"] (INP-20).
    return bool(face_score(image) >= PERSON_DATA["face_detector"]["score_threshold"])


def ordered_quad(points: np.ndarray[Any, Any]) -> np.ndarray[Any, Any]:
    sums, diffs = points.sum(axis=1), np.diff(points, axis=1).ravel()
    return np.array(
        [points[np.argmin(sums)], points[np.argmin(diffs)], points[np.argmax(sums)], points[np.argmax(diffs)]],
        dtype=np.float32,
    )


def outline(image: np.ndarray[Any, Any]) -> list[dict[str, float]]:
    sample = shrink(image, 1000)
    gray = cv2.cvtColor(sample, cv2.COLOR_RGB2GRAY)
    edges = cv2.Canny(cv2.GaussianBlur(gray, (5, 5), 0), 30, 100)
    contours, _ = cv2.findContours(edges, cv2.RETR_EXTERNAL, cv2.CHAIN_APPROX_SIMPLE)
    height, width = sample.shape[:2]
    for contour in sorted(contours, key=cv2.contourArea, reverse=True)[:8]:
        if cv2.contourArea(contour) < height * width * 0.35:
            continue
        approx = cv2.approxPolyDP(contour, 0.02 * cv2.arcLength(contour, True), True)
        if len(approx) == 4 and cv2.isContourConvex(approx):
            return [{"x": float(x / width), "y": float(y / height)} for x, y in ordered_quad(approx.reshape(4, 2))]
    return [{"x": 0.0, "y": 0.0}, {"x": 1.0, "y": 0.0}, {"x": 1.0, "y": 1.0}, {"x": 0.0, "y": 1.0}]


def rectify(image: np.ndarray[Any, Any], corners: list[dict[str, float]]) -> np.ndarray[Any, Any]:
    height, width = image.shape[:2]
    quad = np.array([[p["x"] * (width - 1), p["y"] * (height - 1)] for p in corners], dtype=np.float32)
    if (
        quad.shape != (4, 2)
        or not np.isfinite(quad).all()
        or any(not 0 <= p[k] <= 1 for p in corners for k in ("x", "y"))
    ):
        raise ImageRejected("validation_failed")
    if not cv2.isContourConvex(quad.reshape(4, 1, 2)) or cv2.contourArea(quad) < 100:
        raise ImageRejected("validation_failed")
    out_width = min(8000, max(1, round(max(np.linalg.norm(quad[1] - quad[0]), np.linalg.norm(quad[2] - quad[3])))))
    out_height = min(8000, max(1, round(max(np.linalg.norm(quad[3] - quad[0]), np.linalg.norm(quad[2] - quad[1])))))
    target = np.array(
        [[0, 0], [out_width - 1, 0], [out_width - 1, out_height - 1], [0, out_height - 1]], dtype=np.float32
    )
    return cv2.warpPerspective(
        image, cv2.getPerspectiveTransform(quad, target), (out_width, out_height), borderValue=(255, 255, 255)
    )


def lines_from_words(items: list[dict[str, Any]], width: int, height: int) -> list[dict[str, Any]]:
    groups: dict[str, list[dict[str, Any]]] = {}
    for item in items:
        groups.setdefault(item["line"], []).append(item)
    result = []
    for group in groups.values():
        group.sort(key=lambda word: word["x"])
        original_text = " ".join(word["text"] for word in group)
        tokens = ["[?]" if word["confidence"] < 0.6 else word["text"] for word in group]
        text = " ".join(tokens)
        x, y = min(w["x"] for w in group), min(w["y"] for w in group)
        right, bottom = max(w["x"] + w["w"] for w in group), max(w["y"] + w["h"] for w in group)
        confidence = sum(w["confidence"] for w in group) / len(group)
        spans = []
        cursor = 0
        for word, token in zip(group, tokens, strict=True):
            if word["confidence"] < 0.85:
                spans.append({"start": cursor, "end": cursor + len(token)})
            cursor += len(token) + 1
        result.append(
            {
                "text": text[:4000],
                "personal": bool(PERSONAL.search(original_text)),
                "confidence": confidence,
                "low_confidence_spans": spans,
                "bbox": {"x": x / width, "y": y / height, "w": (right - x) / width, "h": (bottom - y) / height},
            }
        )
    return sorted(result, key=lambda line: (round(line["bbox"]["y"] * height / 20), line["bbox"]["x"]))[:200]


def preprocess(
    data: bytes, *, corners: list[dict[str, float]] | None = None, rotation: int = 0, preview_only: bool = False
) -> dict[str, Any]:
    image = decode(data)
    original_height, original_width = image.shape[:2]
    if preview_only:
        if rotation not in (0, 90, 180, 270):
            raise ImageRejected("validation_failed")
        return {"outline": outline(np.ascontiguousarray(np.rot90(image, -rotation // 90)))}
    if contains_person(image):
        return {"refusal": "contains_person", "privacy_available": True, "checks": [], "lines": []}
    if rotation not in (0, 90, 180, 270):
        raise ImageRejected("validation_failed")
    image = np.ascontiguousarray(np.rot90(image, -rotation // 90))
    chosen = corners or outline(image)
    image = shrink(rectify(image, chosen), 2048)
    gray = cv2.cvtColor(image, cv2.COLOR_RGB2GRAY)
    edges = cv2.Canny(gray, 50, 150)
    segments = cv2.HoughLinesP(
        edges, 1, np.pi / 180, threshold=80, minLineLength=max(40, image.shape[1] // 8), maxLineGap=15
    )
    angles = []
    for x1, y1, x2, y2 in segments[:, 0] if segments is not None else []:
        angle = float(np.degrees(np.arctan2(y2 - y1, x2 - x1)))
        if abs(angle) <= 25:
            angles.append(angle)
    skew = float(np.median(angles)) if angles else 0.0
    if 0.5 < abs(skew) <= 20:
        h, w = gray.shape
        matrix = cv2.getRotationMatrix2D((w / 2, h / 2), skew, 1)
        image = cv2.warpAffine(image, matrix, (w, h), borderValue=(255, 255, 255))
        gray = cv2.cvtColor(image, cv2.COLOR_RGB2GRAY)
    try:
        # Contrast normalization preserves the pixel grid used by region boxes. Stored colour pixels and the
        # physical blur/brightness checks keep the original intensities; only local OCR receives the stretched view.
        items = words(cv2.normalize(gray, None, 0, 255, cv2.NORM_MINMAX))
    except (OSError, AttributeError):
        items = None
    height, width = image.shape[:2]
    text_lines = lines_from_words(items or [], width, height)
    # Names, contact details and school ids are masked before any JPEG is returned to the host.
    kept = []
    for line in text_lines:
        if line.pop("personal", False):
            box = line["bbox"]
            left, top = max(0, int(box["x"] * width) - 8), max(0, int(box["y"] * height) - 8)
            right, bottom = (
                min(width, int((box["x"] + box["w"]) * width) + 8),
                min(height, int((box["y"] + box["h"]) * height) + 8),
            )
            image[top:bottom, left:right] = 255
        else:
            kept.append(line)
    sample = cv2.cvtColor(shrink(image, 1000), cv2.COLOR_RGB2GRAY)
    blur = float(cv2.Laplacian(sample, cv2.CV_64F).var())
    dark = float(np.median(sample))
    glare = float(np.mean(sample >= 250)) if dark < 245 else 0.0
    x_height = float(np.median([w["h"] for w in items or []])) if items else 0.0
    checks = []

    def check(name: str, value: float, advisory: bool, blocking: bool) -> None:
        checks.append(
            {"check": name, "value": value, "outcome": "blocking" if blocking else "advisory" if advisory else "pass"}
        )

    check("blur", blur, blur < 120, blur < 40)
    check("glare", glare, glare > 0.05, glare > 0.20)
    check("darkness", dark, dark < 70, dark < 30)
    check("skew", abs(skew), abs(skew) > 10, abs(skew) > 20)
    check("small_text", x_height, items is not None and x_height < 14, items is not None and x_height < 8)
    check("resolution", float(min(width, height)), min(width, height) < 600, min(width, height) < 300)
    check("no_text", float(len(kept)), False, items is not None and not kept)
    stored = shrink(image, 1600)
    out = io.BytesIO()
    Image.fromarray(stored).save(out, "JPEG", quality=85, optimize=True)
    if out.tell() > 2_000_000:
        raise ImageRejected("image_too_large")
    return {
        "refusal": None,
        "privacy_available": items is not None and bool(kept),
        "checks": checks,
        "lines": kept,
        "width": stored.shape[1],
        "height": stored.shape[0],
        "scale_from_original": min(1.0, max(stored.shape[:2]) / max(original_width, original_height)),
        "jpeg": base64.b64encode(out.getvalue()).decode("ascii"),
        "outline": chosen,
        "ocr_model": "tesseract-eng",
        "ocr_model_version": OCR_DATA["eng.traineddata"]["version"] if items is not None else None,
        "reader_width": width,
        "reader_height": height,
    }
