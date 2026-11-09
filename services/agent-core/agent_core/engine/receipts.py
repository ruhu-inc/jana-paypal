"""Engine-written copy: interruption receipts (TOL-14, AGT-16) and the engine's own cards (AGT-27).

Receipts say what a stopped tool call did and did not do. Each tool class has its own wording
(content/agent/templates/engine.en.yaml): compute run by the worker, the board, a question card, a visual and a memory
note; any other tool takes the generic wording. A call is `not_started` when the stop came first, `running` when it
was stopped while running, and `applied` only for an atomic change that completed before the stop."""

from __future__ import annotations

from dataclasses import dataclass, field
from functools import cache
from pathlib import Path
from typing import Any, Literal

import yaml

Phase = Literal["not_started", "running", "applied"]
CLASSES = ("compute", "update_board", "ask_student", "show_visual", "memory_note", "tool")
DEFAULT_ROOT = Path(__file__).resolve().parents[4] / "content" / "agent"


@dataclass(frozen=True)
class Receipts:
    texts: dict[str, dict[str, str]]
    cards: dict[str, str]
    methods: dict[str, str] = field(default_factory=dict)  # a student's own method in plain words (ASM-09)

    @classmethod
    def load(cls, content_root: Path) -> Receipts:
        path = content_root / "templates" / "engine.en.yaml"
        return _load(str(path if path.exists() else DEFAULT_ROOT / "templates" / "engine.en.yaml"))

    def tool_class(self, name: str, executor: str | None) -> str:
        if name in self.texts and name != "tool":
            return name
        return "compute" if executor == "worker" else "tool"

    def text(self, name: str, executor: str | None, phase: Phase) -> str:
        wording = self.texts[self.tool_class(name, executor)]
        return wording.get(phase) or wording["running"]


@cache
def _load(path: str) -> Receipts:
    data: dict[str, Any] = yaml.safe_load(Path(path).read_text())
    texts = {str(k): {str(p): str(t) for p, t in v.items()} for k, v in data["receipts"].items()}
    missing = [c for c in CLASSES if c not in texts or not {"not_started", "running"} <= set(texts[c])]
    if missing:
        raise ValueError(f"receipts missing for {missing}")
    return Receipts(
        texts,
        {str(k): str(v) for k, v in (data.get("cards") or {}).items()},
        {str(k): str(v) for k, v in (data.get("methods") or {}).items()},
    )


def default_receipts() -> Receipts:
    return Receipts.load(DEFAULT_ROOT)
