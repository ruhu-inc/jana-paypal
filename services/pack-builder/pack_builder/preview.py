"""Preview as a student (AUT-29) with preview packs that never reach students (AUT-30).

A preview pack is built from the working copy by the same build as a release pack, and left unsigned: student builds
verify every pack before loading it (CUR-08), so a preview pack copied to a student device is refused as damaged. Only
`open_preview`, part of the authoring toolchain and absent from student builds, reads one. A preview profile holds
fictional data only and is deleted when the preview closes.
"""

from __future__ import annotations

import shutil
import tempfile
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from agent_core.store.packs import read_pack

from pack_builder.build import Built, build


class NotAPreviewPack(ValueError):
    """A signed pack handed to the preview: preview opens only unsigned working-copy packs."""


def build_preview(pack_dir: Path, out_dir: Path) -> Built:
    """The working copy as an unsigned preview pack (drafts included, as the author sees them)."""
    return build(pack_dir, out_dir)


@dataclass
class PreviewSession:
    pack_id: str
    tables: dict[str, list[dict[str, Any]]]
    profile_dir: Path
    level: str
    hardware_class: str
    mastery: str

    def record(self, record_id: str) -> tuple[str, dict[str, Any]]:
        for table, records in self.tables.items():
            for record in records:
                if record.get("id") == record_id:
                    return table, record
        raise KeyError(record_id)

    def problem_statement(self, item_id: str) -> str:
        """The item as the tutor receives it when the student opens it: stem, then each part's prompt."""
        _, item = self.record(item_id)
        parts = [f"({p['id']}) {p['prompt']}" for p in item.get("parts", [])]
        return " ".join([item.get("stem", ""), *parts]).strip()


@contextmanager
def open_preview(pack_path: Path, *, level: str = "o", hardware_class: str = "mid",
                 mastery: str = "new") -> Iterator[PreviewSession]:  # fmt: skip
    manifest, tables = read_pack(pack_path)
    if manifest.get("signature"):
        raise NotAPreviewPack(f"{pack_path.name}: a signed pack; the preview opens working-copy packs only")
    profile = Path(tempfile.mkdtemp(prefix="jana-preview-"))
    try:
        (profile / "PREVIEW_PROFILE").write_text("fictional preview profile; deleted when the preview closes\n")
        yield PreviewSession(str(manifest["pack_id"]), tables, profile, level, hardware_class, mastery)
    finally:
        shutil.rmtree(profile, ignore_errors=True)
