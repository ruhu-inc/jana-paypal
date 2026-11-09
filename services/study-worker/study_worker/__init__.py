"""Fixed, bounded document codecs. Called only inside a disposable OS sandbox."""

from __future__ import annotations

import base64
import json
from typing import Any


class DocumentError(ValueError):
    pass


def confident_text(lines: list[dict[str, Any]]) -> str:
    """The text a page read from an image gives as evidence. A low-confidence line never becomes evidence, but one stray
    mark must not throw away a page of good lines. A page where most lines are uncertain counts as unreadable: its few
    confident fragments would mislead, so the learner is told instead."""
    kept = [line["text"] for line in lines if line["confidence"] >= 0.85 and not line.get("low_confidence_spans")]
    return "\n".join(kept) if len(kept) * 2 >= len(lines) else ""


def decode(data: bytes, kind: str, name: str, max_units: int = 30) -> list[dict[str, Any]]:
    """Called only inside the sandbox; never execute document actions or fetch linked resources."""
    import io
    import posixpath
    import zipfile
    from xml.etree import ElementTree

    from pypdf import PdfReader

    if kind in ("raster_pdf", "raster_photo"):
        from PIL import Image, ImageOps

        if kind == "raster_pdf":
            import pypdfium2 as pdfium  # type: ignore[import-untyped]

            with pdfium.PdfDocument(data) as document:
                page_number = int(name)
                if page_number < 1 or page_number > len(document):
                    raise DocumentError("unreadable_source")
                page = document[page_number - 1]
                width, height = page.get_size()
                if min(width, height) <= 0 or max(width, height) > 20000:
                    raise DocumentError("source_too_large")
                # No form environment, JavaScript, external resources or annotations are activated.
                bitmap = page.render(scale=min(2.5, 2400 / max(width, height)), draw_annots=False)
                image = bitmap.to_pil().convert("RGB")
                bitmap.close()
                page.close()
        else:
            Image.MAX_IMAGE_PIXELS = 25_000_000
            with Image.open(io.BytesIO(data)) as original:
                if original.format not in ("JPEG", "PNG", "WEBP") or original.width * original.height > 25_000_000:
                    raise DocumentError("unsupported_format")
                image = ImageOps.exif_transpose(original).convert("RGB")
                image.thumbnail((2400, 2400))
        output = io.BytesIO()
        image.save(output, format="PNG")
        if output.tell() > 2_500_000:
            raise DocumentError("source_too_large")
        return [{"png": base64.b64encode(output.getvalue()).decode(), "text": ""}]
    if kind == "photo":
        return [{"locator": {"kind": "page", "page": 1}, "text": "", "ocr": True}]
    if kind == "pdf":
        if not data.startswith(b"%PDF-"):
            raise DocumentError("corrupt_source")
        reader = PdfReader(io.BytesIO(data), strict=True)
        if reader.is_encrypted:
            raise DocumentError("encrypted_source")
        if len(reader.pages) > max_units:
            raise DocumentError("source_too_large")
        units = []
        from study_worker.pdf_text import normalize_pdf_text

        for i, page in enumerate(reader.pages):
            text = normalize_pdf_text(page.extract_text() or "")
            if len(text) > 100_000:
                raise DocumentError("source_too_large")
            units.append({"locator": {"kind": "page", "page": i + 1}, "text": text, "ocr": not bool(text.strip())})
        return units
    # Source titles can be renamed. Detect the safe container from bytes, never from a display title.
    if kind == "document" and not data.startswith(b"PK\x03\x04"):
        try:
            text = data.decode("utf-8-sig")
        except UnicodeDecodeError:
            raise DocumentError("corrupt_source") from None
        if "\x00" in text:
            raise DocumentError("corrupt_source")
        text = notebook_text(text)
        if len(text) > max_units * 4000:
            raise DocumentError("source_too_large")
        return [{"locator": {"kind": "text", "paragraph": 1}, "text": text}]
    if kind not in ("document", "slides"):
        raise DocumentError("unsupported_format")
    with zipfile.ZipFile(io.BytesIO(data)) as archive:
        entries = archive.infolist()
        if len(entries) > 2000 or sum(e.file_size for e in entries) > 20_000_000:
            raise DocumentError("source_too_large")
        if any(e.flag_bits & 1 for e in entries):
            raise DocumentError("encrypted_source")
        if any("vbaProject" in e.filename or e.filename.endswith(".bin") for e in entries):
            raise DocumentError("unsupported_format")
        paths = set(archive.namelist())
        if kind == "document" and "word/document.xml" in paths:
            suffix = ".docx"
        elif kind == "slides" and "ppt/presentation.xml" in paths:
            suffix = ".pptx"
        elif "content.xml" in paths:
            suffix = ".odp" if kind == "slides" else ".odt"
        else:
            raise DocumentError("unsupported_format")

        def xml(entry: str) -> Any:
            raw = archive.read(entry)
            if b"<!DOCTYPE" in raw.upper() or b"<!ENTITY" in raw.upper():
                raise DocumentError("corrupt_source")
            return ElementTree.fromstring(raw)  # noqa: S314 - bounded XML, DTD/entities refused

        def paragraphs(element: Any) -> list[str]:
            return ["".join(e.itertext()).strip() for e in element.iter() if e.tag.rsplit("}", 1)[-1] in ("p", "h")]

        def relations(entry: str, root: str) -> dict[str, str]:
            if entry not in paths:
                return {}
            result = {}
            for rel in xml(entry):
                if rel.get("TargetMode") == "External":
                    continue
                target = posixpath.normpath(posixpath.join(root, rel.get("Target", "")))
                if target in paths and not target.startswith("/") and ".." not in target.split("/"):
                    result[rel.get("Id")] = target
            return result

        if suffix == ".pptx":
            links = relations("ppt/_rels/presentation.xml.rels", "ppt")
            names = []
            for e in xml("ppt/presentation.xml").iter():
                if e.tag.rsplit("}", 1)[-1] == "sldId":
                    rid = e.get("{http://schemas.openxmlformats.org/officeDocument/2006/relationships}id")
                    target = links.get(rid)
                    if target is None or not target.startswith("ppt/slides/"):
                        raise DocumentError("corrupt_source")
                    names.append(target)
            if len(names) > max_units:
                raise DocumentError("source_too_large")
            units = []
            for entry in names:
                element = xml(entry)
                if element.get("show") == "0":
                    continue
                texts = paragraphs(element)
                note_links = relations(
                    posixpath.join(posixpath.dirname(entry), "_rels", posixpath.basename(entry) + ".rels"),
                    posixpath.dirname(entry),
                )
                for target in note_links.values():
                    if target.startswith("ppt/notesSlides/"):
                        notes = paragraphs(xml(target))
                        if any(notes):
                            texts += ["Speaker notes:\n" + "\n".join(notes)]
                units.append({"locator": {"kind": "slide", "slide": len(units) + 1}, "text": "\n\n".join(texts)})
            return units
        root = xml("word/document.xml" if suffix == ".docx" else "content.xml")
        if suffix == ".odp":
            pages = [e for e in root.iter() if e.tag == "{urn:oasis:names:tc:opendocument:xmlns:drawing:1.0}page"]
            if len(pages) > max_units:
                raise DocumentError("source_too_large")
            return [
                {"locator": {"kind": "slide", "slide": i + 1}, "text": "\n\n".join(paragraphs(page))}
                for i, page in enumerate(pages)
            ]
        values = paragraphs(root)
        if len(values) > 10000 or sum(len(v) for v in values) > max_units * 4000:
            raise DocumentError("source_too_large")
        return [{"locator": {"kind": "text", "paragraph": i + 1}, "text": text} for i, text in enumerate(values)]


def notebook_text(text: str) -> str:
    """A Jupyter notebook's cells as text, in order, without its outputs and metadata; any other text as it is."""
    if '"cells"' not in text[:4096] and '"nbformat"' not in text:
        return text
    try:
        cells = json.loads(text)["cells"]
        parts = ["".join(c["source"]) if isinstance(c["source"], list) else str(c["source"]) for c in cells]
    except (ValueError, KeyError, TypeError):
        return text
    return "\n\n".join(p for p in parts if p.strip()) or text
