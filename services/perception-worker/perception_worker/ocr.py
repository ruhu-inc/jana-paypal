"""Local Tesseract native binding: no subprocess, temp file, learner store or network access."""

from __future__ import annotations

import csv
import hashlib
import io
import json
from pathlib import Path
from typing import Any

import numpy as np
import tesserocr  # type: ignore[import-untyped]
from PIL import Image


def assets() -> str | None:
    # The binding bundles native Tesseract. Traineddata is an independent, licence-gated model asset.
    folders = [
        Path(__file__).parent / "native" / "tessdata",
        Path("/opt/homebrew/share/tessdata"),
        Path("/usr/share/tesseract-ocr/5/tessdata"),
        Path("/usr/share/tessdata"),
        Path("/usr/local/share/tessdata"),
    ]
    expected = json.loads((Path(__file__).parent / "ocr-assets.json").read_text())["eng.traineddata"]["sha256"]
    data = next(
        (
            folder
            for folder in folders
            if (folder / "eng.traineddata").is_file()
            and hashlib.sha256((folder / "eng.traineddata").read_bytes()).hexdigest() == expected
        ),
        None,
    )
    return str(data) if data is not None else None


def words(image: np.ndarray[Any, Any]) -> list[dict[str, Any]] | None:
    folder = assets()
    if folder is None:
        return None
    try:
        with tesserocr.PyTessBaseAPI(path=folder, lang="eng", psm=tesserocr.PSM.AUTO) as api:
            api.SetImage(Image.fromarray(np.ascontiguousarray(image)))
            text = api.GetTSVText(0)
        if len(text) > 2_000_000:
            return None
        if not text.startswith("level\t"):
            text = (
                "level\tpage_num\tblock_num\tpar_num\tline_num\tword_num\tleft\ttop\twidth\theight\tconf\ttext\n"
            ) + text
        rows = []
        for row in csv.DictReader(io.StringIO(text), delimiter="\t"):
            if row.get("level") != "5" or not (row.get("text") or "").strip():
                continue
            rows.append(
                {
                    "text": row["text"][:200],
                    "confidence": max(0.0, min(1.0, float(row["conf"]) / 100)),
                    "line": ":".join(row.get(k, "0") for k in ("page_num", "block_num", "par_num", "line_num")),
                    "x": int(row["left"]),
                    "y": int(row["top"]),
                    "w": int(row["width"]),
                    "h": int(row["height"]),
                }
            )
            if len(rows) >= 2000:
                break
        return rows
    except (RuntimeError, OSError):
        return None
