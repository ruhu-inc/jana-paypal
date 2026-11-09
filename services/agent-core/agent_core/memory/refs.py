"""Integer refs for stored records shown to a model (MEM-08).

A model never sees a note, event, KC or misconception id: it sees a numbered list starting at 1, and every integer it
returns is mapped back here. An integer outside the list fails the whole output.
"""

from __future__ import annotations

from collections.abc import Iterable, Sequence
from dataclasses import dataclass


class RefOutOfRange(ValueError):
    """A model referred to an integer that was not in the list it was shown."""


@dataclass(frozen=True)
class RefTable:
    """The ids behind refs 1..n, in the order they were presented."""

    ids: tuple[str, ...]

    @classmethod
    def of(cls, ids: Iterable[str]) -> RefTable:
        """A table over `ids` in order; a repeated id keeps its first ref."""
        seen: dict[str, None] = {}
        for record_id in ids:
            seen.setdefault(record_id, None)
        return cls(tuple(seen))

    def __len__(self) -> int:
        return len(self.ids)

    def ref_of(self, record_id: str) -> int:
        """The integer shown for `record_id` (1-based)."""
        try:
            return self.ids.index(record_id) + 1
        except ValueError:
            raise KeyError(record_id) from None

    def resolve(self, ref: int) -> str:
        """The id behind `ref`; raises RefOutOfRange for anything outside 1..n (booleans included)."""
        # A bool is an int in Python, and a float or string that looks like one is not a ref: only a plain integer in
        # range resolves, so a model can neither invent a record nor smuggle one in by type.
        if type(ref) is not int or not 1 <= ref <= len(self.ids):
            raise RefOutOfRange(f"ref {ref!r} is not in the list of {len(self.ids)}")
        return self.ids[ref - 1]

    def resolve_all(self, refs: Sequence[int]) -> list[str]:
        """Every ref resolved, or RefOutOfRange for the first that is not in the table."""
        return [self.resolve(ref) for ref in refs]
