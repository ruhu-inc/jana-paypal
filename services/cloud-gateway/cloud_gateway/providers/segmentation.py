"""Segmentation transport interface and the existing neutral object/mask representation."""

from collections.abc import Sequence
from dataclasses import dataclass
from typing import Literal, Protocol

Label = Literal["positive", "negative"]


@dataclass(frozen=True)
class ObjectPrompt:
    id: int
    box: tuple[int, int, int, int] | None = None
    points: tuple[tuple[int, int, Label], ...] = ()


@dataclass(frozen=True)
class Segment:
    id: str
    box: tuple[int, int, int, int]
    confidence: float | None
    mask: bytes = b""
    run_lengths: tuple[int, ...] | None = None

    @property
    def width(self) -> int:
        return self.box[2] - self.box[0]

    @property
    def height(self) -> int:
        return self.box[3] - self.box[1]

    def runs(self) -> list[int]:
        if self.run_lengths is not None:
            return list(self.run_lengths)
        out, current, length = [], 0, 0
        for value in self.mask:
            if value != current:
                out.append(length)
                current, length = value, 0
            length += 1
        out.append(length)
        return out


@dataclass(frozen=True)
class SegmentReply:
    segments: tuple[Segment, ...]
    images: int


class SegmentationBackend(Protocol):
    @property
    def configured(self) -> bool: ...

    async def segment(
        self,
        image_url: str,
        width: int,
        height: int,
        *,
        objects: Sequence[ObjectPrompt] = (),
        concept: str | None = None,
    ) -> SegmentReply: ...

    async def aclose(self) -> None: ...
