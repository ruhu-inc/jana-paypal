"""The content safety review sample of a release (CUR-27), on the authoring side: what must be reviewed, and whether a
review record covers it. No key is held here (D34, AUT-33): a subject lead signs the review with their own key, and the
signing service checks that signature against the reviewers it trusts before it publishes to a pilot or stable channel
(its safety review module).

The sample of a release is every new or changed reviewed record (items, worked examples, misconceptions, lessons,
templates, mock papers) on a sensitive topic (the `sensitive_topics` list of the content scan, PRV-17), plus the
configured share of the other new or changed records, drawn in a fixed hash order of pack, version and record id so
anyone can recompute it. A record is new or changed when its content sha256 (AUT-03) differs from the previous release.

A review record lives at `content/curriculum/safety-reviews/<pack id>/<version>.json`:

    {"format": "jana.safety-review.1", "pack_id": ..., "version": ...,
     "reviewer": {"role_id": "rev_..."}, "reviewed_at": "<ISO time>",
     "records": [{"id": ..., "content_sha256": ..., "sensitive": true|false,
                  "decision": "approved"|"changes_requested"}],
     "signature": {"alg": "ed25519", "key_id": ..., "value": ...}}

The signature covers the canonical record without its `signature` field.
"""

from __future__ import annotations

import hashlib
import math
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from agent_core.config import Settings
from agent_core.store.packs import row_id

from pack_builder import scan
from pack_builder import source as tree
from pack_builder.build import REVIEWED

FORMAT = "jana.safety-review.1"
DIRECTORY = "safety-reviews"


def review_path(content_root: Path, pack_id: str, version: str) -> Path:
    """`content_root` is `content/curriculum`."""
    return content_root / DIRECTORY / pack_id / f"{version}.json"


def _hashes(tables: Mapping[str, list[dict[str, Any]]]) -> dict[str, str]:
    return {row_id(t, r): tree.content_sha256(r) for t in REVIEWED for r in tables.get(t, [])}


@dataclass(frozen=True)
class Sample:
    pack_id: str
    version: str
    records: dict[str, str]  # id -> content sha256 of every record that must be reviewed
    sensitive: frozenset[str]
    new: int  # new or changed records in the release

    def unsigned(self, role_id: str, reviewed_at: str, decision: str = "approved") -> dict[str, Any]:
        """A review record for this sample with every record given `decision`, ready for the reviewer to check and
        sign."""
        return {"format": FORMAT, "pack_id": self.pack_id, "version": self.version, "reviewer": {"role_id": role_id},
                "reviewed_at": reviewed_at,
                "records": [{"id": rid, "content_sha256": sha, "sensitive": rid in self.sensitive,
                             "decision": decision} for rid, sha in sorted(self.records.items())]}  # fmt: skip


def sample(
    pack: Mapping[str, Any],
    tables: Mapping[str, list[dict[str, Any]]],
    previous: Mapping[str, list[dict[str, Any]]] | None,
    lists: scan.Lists,
    settings: Settings,
) -> Sample:
    """The records a release's safety review must cover (CUR-27)."""
    now = _hashes(tables)
    before = _hashes(previous) if previous is not None else {}
    new = {rid: sha for rid, sha in now.items() if before.get(rid) != sha}
    by_id = {row_id(t, r): (t, r) for t in REVIEWED for r in tables.get(t, [])}
    sensitive = frozenset(rid for rid in new if scan.sensitive_records({by_id[rid][0]: [by_id[rid][1]]}, lists))

    def order(rid: str) -> str:
        return hashlib.sha256(f"{pack['pack_id']}:{pack['version']}:{rid}".encode()).hexdigest()

    others = sorted((rid for rid in new if rid not in sensitive), key=order)
    drawn = others[: math.ceil(settings.safety_review_sample_share * len(new))] if new else []
    chosen = {rid: new[rid] for rid in [*sensitive, *drawn]}
    return Sample(str(pack["pack_id"]), str(pack["version"]), chosen, sensitive, len(new))


def coverage_problems(review: Mapping[str, Any] | None, wanted: Sample) -> list[str]:
    """What a review record leaves uncovered: a missing record, the wrong pack or version, a sampled record missing
    or reviewed in another version of its content, or a record not approved. The signature is the signing service's
    to check."""
    where = f"{wanted.pack_id} {wanted.version}"
    if review is None:
        return [f"{where}: no signed content safety review (CUR-27)"]
    problems = []
    if review.get("format") != FORMAT:
        problems.append(f"{where}: the safety review is not a {FORMAT} record (CUR-27)")
    if review.get("pack_id") != wanted.pack_id or review.get("version") != wanted.version:
        problems.append(f"{where}: the safety review is for {review.get('pack_id')} {review.get('version')} (CUR-27)")
    reviewed = {str(r.get("id")): r for r in review.get("records") or []}
    for rid, sha in sorted(wanted.records.items()):
        entry = reviewed.get(rid)
        if entry is None:
            problems.append(f"{rid}: in the safety review sample but not reviewed (CUR-27)")
        elif entry.get("content_sha256") != sha:
            problems.append(f"{rid}: changed since its safety review (CUR-27)")
        elif entry.get("decision") != "approved":
            problems.append(f"{rid}: the safety review did not approve it ({entry.get('decision')}) (CUR-27)")
    return problems
