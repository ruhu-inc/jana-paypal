"""Reading a GGUF model file's header (MDL-17 subset), defensively (SEC-24 model-file rules).

Only the key-value header is read; tensor data is never loaded. Every count and length is bounded before it is
used, so a malformed or hostile file fails with `GgufError` instead of exhausting memory."""

from __future__ import annotations

import struct
from dataclasses import dataclass
from pathlib import Path
from typing import Any, BinaryIO

MAGIC = b"GGUF"
MAX_KV = 100_000
MAX_STRING = 16 * 1024 * 1024
MAX_ARRAY = 10_000_000
# GGUF value types: id -> (struct format, size) for scalars.
SCALARS = {
    0: ("<B", 1),
    1: ("<b", 1),
    2: ("<H", 2),
    3: ("<h", 2),
    4: ("<I", 4),
    5: ("<i", 4),
    6: ("<f", 4),
    7: ("<?", 1),
    10: ("<Q", 8),
    11: ("<q", 8),
    12: ("<d", 8),
}
STRING, ARRAY = 8, 9
WANTED_SUFFIXES = (
    ".context_length",
    ".block_count",
    ".embedding_length",
    ".attention.head_count",
    ".attention.head_count_kv",
    ".attention.key_length",
    ".attention.value_length",
)


class GgufError(ValueError):
    pass


@dataclass(frozen=True)
class GgufInfo:
    architecture: str
    native_context: int
    layers: int
    embedding: int
    heads: int
    kv_heads: int
    key_length: int
    value_length: int
    file_type: int | None
    file_bytes: int
    kv_heads_per_layer: tuple[int, ...] = ()

    def kv_bytes_per_token(self, bytes_per_value: int = 2) -> int:
        """K and V caches at f16 for every layer."""
        heads = sum(self.kv_heads_per_layer) if self.kv_heads_per_layer else self.layers * self.kv_heads
        return heads * (self.key_length + self.value_length) * bytes_per_value


def _read(f: BinaryIO, n: int) -> bytes:
    data = f.read(n)
    if len(data) != n:
        raise GgufError("the file ends inside its header")
    return data


def _scalar(f: BinaryIO, kind: int) -> Any:
    fmt, size = SCALARS[kind]
    return struct.unpack(fmt, _read(f, size))[0]


def _string(f: BinaryIO) -> str:
    (length,) = struct.unpack("<Q", _read(f, 8))
    if length > MAX_STRING:
        raise GgufError("a header string is too long")
    return _read(f, length).decode("utf-8", "replace")


def _value(f: BinaryIO, kind: int, keep: bool) -> Any:
    if kind in SCALARS:
        return _scalar(f, kind)
    if kind == STRING:
        return _string(f)
    if kind == ARRAY:
        (inner,) = struct.unpack("<I", _read(f, 4))
        (count,) = struct.unpack("<Q", _read(f, 8))
        if count > MAX_ARRAY or (inner not in SCALARS and inner != STRING):
            raise GgufError("a header array is too large or of an unknown type")
        if inner in SCALARS and not keep:
            f.seek(SCALARS[inner][1] * count, 1)
            return None
        values = [_value(f, inner, keep) for _ in range(count)]
        return values if keep else None
    raise GgufError(f"unknown header value type {kind}")


def read_header(path: Path) -> GgufInfo:
    size = path.stat().st_size
    with path.open("rb") as f:
        if _read(f, 4) != MAGIC:
            raise GgufError("not a GGUF file")
        (version,) = struct.unpack("<I", _read(f, 4))
        if version not in (2, 3):
            raise GgufError(f"unsupported GGUF version {version}")
        _tensors, kv_count = struct.unpack("<QQ", _read(f, 16))
        if kv_count > MAX_KV:
            raise GgufError("too many header entries")
        values: dict[str, Any] = {}
        for _ in range(kv_count):
            key = _string(f)
            (kind,) = struct.unpack("<I", _read(f, 4))
            keep = key in ("general.architecture", "general.file_type") or key.endswith(WANTED_SUFFIXES)
            value = _value(f, kind, keep)
            if keep:
                values[key] = value
    arch = values.get("general.architecture")
    if not isinstance(arch, str):
        raise GgufError("the header names no architecture")

    def get(suffix: str, default: int = 0) -> int:
        value = values.get(f"{arch}{suffix}", default)
        if type(value) is not int or value < 0:
            raise GgufError(f"expected a non-negative integer for {arch}{suffix}")
        return value

    heads = get(".attention.head_count", 1) or 1
    layers = get(".block_count")
    raw_kv_heads = values.get(f"{arch}.attention.head_count_kv", heads)
    per_layer: tuple[int, ...] = ()
    if isinstance(raw_kv_heads, list):
        if (
            not 1 <= layers <= 4096
            or len(raw_kv_heads) != layers
            or any(type(value) is not int or not 1 <= value <= heads for value in raw_kv_heads)
        ):
            raise GgufError("per-layer KV heads must match the block count and fit the attention heads")
        per_layer = tuple(raw_kv_heads)
        kv_heads = max(per_layer)
    else:
        kv_heads = get(".attention.head_count_kv", heads) or heads
    embedding = get(".embedding_length")
    head_dim = embedding // heads if embedding else 0
    return GgufInfo(
        architecture=arch,
        native_context=get(".context_length"),
        layers=layers,
        embedding=embedding,
        heads=heads,
        kv_heads=kv_heads,
        key_length=get(".attention.key_length", head_dim) or head_dim,
        value_length=get(".attention.value_length", head_dim) or head_dim,
        file_type=values.get("general.file_type"),
        file_bytes=size,
        kv_heads_per_layer=per_layer,
    )


def write_header(path: Path, values: dict[str, Any], pad_to: int = 0) -> None:
    """Write a minimal GGUF header (test fixtures). `pad_to` extends the file sparsely to a model-like size."""
    out = bytearray(MAGIC + struct.pack("<IQQ", 3, 0, len(values)))
    for key, value in values.items():
        encoded = key.encode()
        out += struct.pack("<Q", len(encoded)) + encoded
        if isinstance(value, str):
            raw = value.encode()
            out += struct.pack("<I", STRING) + struct.pack("<Q", len(raw)) + raw
        elif isinstance(value, (list, tuple)):
            out += struct.pack("<IIQ", ARRAY, 4, len(value))
            out += b"".join(struct.pack("<I", int(item)) for item in value)
        else:
            out += struct.pack("<I", 4) + struct.pack("<I", int(value))
    path.write_bytes(bytes(out))
    if pad_to > len(out):
        with path.open("r+b") as f:
            f.truncate(pad_to)
