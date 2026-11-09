"""The embedding stage of pack search (CUR-30, MEM-21): pluggable, local, and optional.

An `Embedder` turns text into vectors on the device; none ships yet (the model and where it runs, the llama.cpp
router or in-process, are open on the low hardware class), so search runs on keywords alone until one is installed.
The build writes a pack's `search_vectors` table only when it is given an embedder, and names that model in the
manifest's `embedding_model` (id, version, dimensions). On the device a pack's vectors are used only when its model
matches the installed embedder exactly; otherwise that pack is searched by keyword only and the mismatch is logged.

Vectors are stored as little-endian half floats in base64, about 2 bytes per dimension.
"""

from __future__ import annotations

import base64
import math
import struct
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from typing import Any, Protocol

from agent_core.retrieval.passages import Passage


@dataclass(frozen=True)
class EmbeddingModel:
    """The manifest's `embedding_model` (CUR-07, CUR-30)."""

    id: str
    version: str
    dimensions: int

    def manifest(self) -> dict[str, Any]:
        return {"id": self.id, "version": self.version, "dimensions": self.dimensions}

    @classmethod
    def from_manifest(cls, value: Any) -> EmbeddingModel | None:
        if not isinstance(value, Mapping):
            return None
        try:
            return cls(str(value["id"]), str(value["version"]), int(value["dimensions"]))
        except (KeyError, TypeError, ValueError):
            return None


class Embedder(Protocol):
    """A local embedding model: no network, deterministic for the same text."""

    @property
    def model(self) -> EmbeddingModel: ...

    def embed(self, texts: Sequence[str]) -> list[list[float]]: ...


def encode(vector: Sequence[float]) -> str:
    return base64.b64encode(struct.pack(f"<{len(vector)}e", *vector)).decode("ascii")


def decode(text: str, dimensions: int) -> tuple[float, ...] | None:
    """A stored vector, or None when it is not `dimensions` half floats."""
    try:
        raw = base64.b64decode(text, validate=True)
    except (ValueError, TypeError):
        return None
    if len(raw) != 2 * dimensions:
        return None
    return struct.unpack(f"<{dimensions}e", raw)


def unit(vector: Sequence[float]) -> tuple[float, ...]:
    norm = math.sqrt(sum(v * v for v in vector))
    return tuple(v / norm for v in vector) if norm > 0 else tuple(vector)


def cosine(a: Sequence[float], b: Sequence[float]) -> float:
    """Of two unit vectors."""
    return sum(x * y for x, y in zip(a, b, strict=True))


def search_vectors_table(passages: Sequence[Passage], embedder: Embedder) -> list[dict[str, Any]]:
    """The pack's `search_vectors` rows: one unit vector per passage, by passage id (CUR-30)."""
    texts = [f"{p.title}\n{p.text}" if p.title else p.text for p in passages]
    vectors = embedder.embed(texts)
    dims = embedder.model.dimensions
    if len(vectors) != len(passages) or any(len(v) != dims for v in vectors):
        raise ValueError(f"the embedder returned vectors that are not {dims}-dimensional, one per passage")
    return [{"id": p.id, "vector": encode(unit(v))} for p, v in zip(passages, vectors, strict=True)]
