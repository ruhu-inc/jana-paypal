"""Building a pack from its source directory (CUR-07, CUR-08, CUR-19 to CUR-24).

A pack's sources are the source tree of AUT-03 (`pack_builder.source`): `pack.yaml`, the map, KCs per topic and one
file per record. An authored record may carry two source-only fields that never reach the pack: `checks` (build check
id → tool and arguments) and `authoring` (drafter role id, pipeline history and field provenance).

The build collects every problem before failing, so one run reports them all. A clean build writes the SQLite pack,
its JSON export (validated against the contract), the content-credits page and a build report with the check records.
The build never holds a signing key (D34, AUT-33): it writes an unsigned pack, which only the authoring preview loads,
unless the signing service hands it a key to sign with.
"""

from __future__ import annotations

from typing import Optional

import json
import re
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from agent_core.assets.resolvers import resolver
from agent_core.assets.visuals import Resolver
from agent_core.config import Settings
from agent_core.contracts import errors as contract_errors
from agent_core.contracts import validate
from agent_core.engine.assessment.adaptive import probe_problems
from agent_core.engine.marking import MARKERS
from agent_core.retrieval.embedding import Embedder, search_vectors_table
from agent_core.retrieval.passages import derive
from agent_core.store import packs
from agent_core.store.packs import row_id, table_sha256, to_document, write_pack
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

from pack_builder import (
    curriculum,
    diagrams,
    drafting,
    figures,
    follow_through,
    licence,
    mocks,
    pipeline,
    qa,
    scan,
    structure,
    versioning,
)
from pack_builder import source as tree
from pack_builder.diagrams import diagram_svg
from pack_builder.heldout import HeldOut
from pack_builder.signing import sign
from pack_builder.verify import Verifier

CONTRACT = "content/content-pack"
DEFINITION = tree.DEFINITION
LIBRARY = ("diagrams", "visual_presets")  # the diagram branch of the pipeline (VSL-34)
REVIEWED = ("lessons", "worked_examples", "items", "item_templates", "misconceptions", "mock_papers")
SOURCE_ONLY = ("checks", "authoring")
SCHEMA_VERSION = "1.4.0"  # the content-pack contract version the build writes (TABLE ARC-29a; 1.4: Item.corrects)


class BuildError(Exception):
    def __init__(self, problems: list[str]) -> None:
        super().__init__(f"{len(problems)} problem(s):\n" + "\n".join(problems))
        self.problems = problems


@dataclass
class Source:
    pack: dict[str, Any]
    tables: dict[str, list[dict[str, Any]]]
    checks: dict[str, dict[str, Any]] = field(default_factory=dict)
    authoring: dict[str, dict[str, Any]] = field(default_factory=dict)
    # The diagram library (VSL-22, VSL-29): entries and presets go through the diagram branch before the pack.
    library: dict[str, list[dict[str, Any]]] = field(default_factory=lambda: {t: [] for t in LIBRARY})
    directory: Path | None = None
    passed: dict[str, Any] = field(default_factory=lambda: {"svg": {}, "resolved": {}})


def load_source(directory: Path) -> Source:
    try:
        pack, found = tree.read_pack(directory)
    except tree.SourceError as error:
        raise BuildError([str(error)]) from error
    # The search tables are derived from the records at build time (CUR-30), never authored.
    tables: dict[str, list[dict[str, Any]]] = {t: [] for t in packs.TABLES if t not in packs.SEARCH_TABLES}
    source = Source(pack, tables, directory=directory)
    unsupported = [f"{located[0].path.parent}: {table} need the {DEFINITION[table]} record of the content-pack "
                   "contract, which this build's contract does not have yet (D72)"
                   for table, located in found.items()
                   if located and table not in tables and table not in LIBRARY]  # fmt: skip
    if unsupported:
        raise BuildError(unsupported)
    for table, located in found.items():
        for item in located:
            record = json.loads(json.dumps(item.record))  # a copy: the source-only keys are taken off below
            for check_id, spec in (record.pop("checks", None) or {}).items():
                source.checks[check_id] = spec
            authoring = record.pop("authoring", None)
            if authoring is not None:
                source.authoring[row_id(table, record)] = authoring
            if table in LIBRARY:
                source.library[table].append(record)
            else:
                tables[table].append(record)
    return source


def charts_of(source: Source) -> list[dict[str, Any]]:
    """The pack's charts of accounts (the `pack.ref` chart, CUR-07): its `charts_of_accounts` table."""
    return list(source.tables.get("charts_of_accounts", []))


def library_problems(source: Source, settings: Settings) -> tuple[list[str], list[str], dict[str, Any]]:
    """The diagram branch (VSL-34): (contract problems, pipeline problems, what passed: sanitised SVG by entry id and
    pre-resolved spec by preset id)."""
    shape: list[str] = []
    lint: list[str] = []
    passed: dict[str, Any] = {"svg": {}, "resolved": {}}
    seen: set[str] = set()
    for entry in source.library["diagrams"]:
        rid = str(entry.get("id"))
        if rid in seen:
            shape.append(f"{rid}: duplicate diagram id")
        seen.add(rid)
        svg, missing = diagram_svg(source.directory, entry) if source.directory is not None else (None, [])
        found = missing + diagrams.diagram_problems(entry, svg, settings)
        shape += [p for p in found if ": $" in p]
        lint += [p for p in found if ": $" not in p]
        lint += licence.gate_problems("diagrams", entry, rid) + licence.attribution_problems("diagrams", entry, rid)
        if not found and svg is not None:
            passed["svg"][rid] = svg
    presets = {str(p.get("id")): p for p in source.library["visual_presets"]}
    ctx = diagrams.preset_context(source.pack, source.tables.get("conventions", []), presets)
    for rid, preset in presets.items():
        if rid in seen:
            shape.append(f"{rid}: the id is already a library diagram (CUR-02)")
        seen.add(rid)
        call = diagrams.build_call(re.sub(r"[^a-z0-9_]", "_", rid.removeprefix("dgm.")))
        found, resolved = diagrams.preset_problems(preset, ctx, Resolver(resolver(call, ctx.conventions)), settings)
        lint += (
            found
            + licence.gate_problems("diagrams", preset, rid)
            + licence.attribution_problems("diagrams", preset, rid)
        )
        if resolved is not None:
            passed["resolved"][rid] = resolved
    return shape, lint, passed


def carry_library(source: Source, passed: dict[str, Any]) -> list[dict[str, str]]:
    """Library records into the pack's tables where the bundled contract can hold them; the rest, held back, with why
    (VSL-22, VSL-29: an older bundled contract cannot)."""
    held: list[dict[str, str]] = []
    for entry in source.library["diagrams"]:
        record, reason = diagrams.pack_record(entry, passed["svg"][entry["id"]])
        if record is None:
            held.append({"id": entry["id"], "reason": str(reason)})
        else:
            source.tables["diagrams"].append(record)
    for preset in source.library["visual_presets"]:
        record, reason = diagrams.preset_record(preset, passed["resolved"].get(preset["id"]))
        if record is None or "visual_presets" not in source.tables:
            held.append({"id": preset["id"], "reason": str(reason or "this build writes no visual_presets table")})
        else:
            source.tables["visual_presets"].append(record)
    return held


@dataclass
class Built:
    pack_path: Path
    json_path: Path
    credits_path: Path
    report_path: Path
    manifest: dict[str, Any]


def problems_of(source: Source, settings: Settings, retired: list[str] | None = None) -> tuple[list[str], Verifier]:
    grouped, verifier = categorised_problems(source, settings, retired)
    return [p for category in CATEGORIES for p in grouped[category]], verifier


CATEGORIES = ("validate", "verify", "lint", "review", "qa", "scan")


def categorised_problems(
    source: Source, settings: Settings, retired: list[str] | None = None
) -> tuple[dict[str, list[str]], Verifier]:
    """Every problem, by the `aut` command that reports it: validate (contract, identifiers and references, map, KCs,
    markers, lesson, worked-example, hint and convention structure: CUR-11, CUR-12, CUR-13, CUR-25, CUR-28), verify
    (CUR-20 tool verification), lint (licence gate and attribution, CUR-23, CUR-24; AI provenance, AUT-19), review
    (pipeline history, CUR-19), qa (the automated checks of TABLE CUR-25a) and scan (the age-appropriateness content
    scan and sensitivity lint, PRV-17, CUR-27)."""
    pack, tables = source.pack, source.tables
    grouped: dict[str, list[str]] = {c: [] for c in CATEGORIES}
    shape = grouped["validate"]
    for table, records in tables.items():
        for record in records:
            for error in contract_errors(f"{CONTRACT}#/$defs/{DEFINITION[table]}", record):
                shape.append(f"{row_id(table, record) if _has_id(table, record) else table}: {error}")
    retired_ids = [*pack.get("retired_ids", []), *(retired or [])]
    if shape:  # the deeper checks assume well-formed records; identifiers are still worth reporting
        identified = {t: [r for r in rs if _has_id(t, r)] for t, rs in tables.items()}
        shape += curriculum.id_problems(pack, identified, retired_ids)
        return grouped, Verifier({}, [])
    shape += curriculum.id_problems(pack, tables, retired_ids)
    shape += curriculum.map_problems(tables["curriculum_nodes"], tables["kcs"],
                                     settings.curriculum_topic_weight_tolerance)  # fmt: skip
    shape += curriculum.prerequisite_problems(tables["kcs"], pack)
    shape += curriculum.root_gap_problems(tables["misconceptions"], tables["kcs"], pack)
    shape += curriculum.mapping_problems(tables["curriculum_nodes"], tables["kcs"])
    shape += versioning.migration_problems(tables, str(pack["version"]))
    shape += curriculum.kc_problems(tables["kcs"], tables["items"], tables["worked_examples"],
                                    max_chars=settings.kc_statement_max_chars,
                                    published=pack["channel"] != "dev")  # fmt: skip
    for item in tables["items"]:
        if item["type"] not in MARKERS:
            shape.append(f"{item['id']}: no marker for item type {item['type']} in this build (ASM-03)")
    library_ids = [str(r.get("id")) for t in LIBRARY for r in source.library[t]]
    shape += structure.reference_problems(tables, structure.Known(tables, library_ids), pack)
    by_id = {str(i["id"]): i for i in tables["items"]}
    for lesson in tables["lessons"]:
        shape += structure.lesson_problems(lesson, settings, by_id)
    for example in tables["worked_examples"]:
        shape += structure.worked_example_problems(example)
    constants = structure.constants_of(tables["conventions"])
    for item in tables["items"]:
        shape += structure.hint_problems(item) + structure.convention_problems(item, constants)
    shape += probe_problems(tables["items"], tables["misconceptions"], settings)
    grouped["verify"] += follow_through.problems(tables["items"])  # recipes reproduce their keys (ASM-06)
    shape += structure.chart_problems(charts_of(source), tables, source.checks)
    shape += mocks.paper_problems(tables.get("mock_papers", []), source.pack, tables["items"],
                                  tables["curriculum_nodes"], tables["kcs"],
                                  settings.mock_blueprint_weight_tolerance)  # fmt: skip
    verifier = Verifier(source.checks, tables["conventions"])
    verify_owner: dict[str, int] = {}
    for item in tables["items"]:
        before = len(verifier.problems)
        verifier.item(item)
        verify_owner[item["id"]] = len(verifier.problems) - before
    for example in tables["worked_examples"]:
        before = len(verifier.problems)
        verifier.worked_example(example)
        verify_owner[example["id"]] = len(verifier.problems) - before
    grouped["verify"] += verifier.problems
    for table in packs.TABLES:
        for record in tables.get(table, []):
            rid = row_id(table, record)
            authoring = source.authoring.get(rid, {})
            licensed = (
                {**record, "provenance": authoring["import_provenance"]}
                if "provenance" not in record and authoring.get("import_provenance")
                else record
            )
            licence_problems = licence.gate_problems(table, licensed, rid) + licence.attribution_problems(
                table, licensed, rid
            )
            grouped["lint"] += licence_problems
            authoring = source.authoring.get(rid, {})
            grouped["lint"] += drafting.provenance_errors({**record, "authoring": authoring})
            if table not in REVIEWED:
                continue
            drafted_by = authoring.get("drafted_by")
            grouped["review"] += pipeline.review_problems(rid, record["review"], drafted_by)
            grouped["review"] += pipeline.history_problems(
                rid, authoring.get("history", []), record["review"], drafted_by,
                verified=verify_owner.get(rid, 0) == 0, licensed=not licence_problems,
            )  # fmt: skip
            if pack["channel"] != "dev" and record["review"]["status"] != "published":
                grouped["review"].append(f"{rid}: a {pack['channel']} pack holds published records only")
    shape, lint, source.passed = library_problems(source, settings)
    grouped["validate"] += shape
    grouped["lint"] += lint
    found, source.passed["figures"] = figures.figure_problems(source, verifier)  # checked visuals on records
    grouped["verify"] += found
    grouped["qa"] += qa.qa_problems(tables, source.library, pack, source.directory, settings)
    grouped["scan"] += scan.scan_problems({**tables, **source.library}, scan.lists_for(source.directory))
    return grouped, verifier


def _has_id(table: str, record: dict[str, Any]) -> bool:
    try:
        row_id(table, record)
    except (KeyError, TypeError):
        return False
    return True


def default_held_out(source_dir: Path) -> HeldOut:
    for parent in source_dir.resolve().parents:
        if (parent / "evals" / "held-out").is_dir():
            return HeldOut.load(parent / "evals" / "held-out")
    return HeldOut()


_PLACEHOLDER = {"alg": "ed25519", "key_id": "key_unsigned", "value": "A" * 86 + "=="}


def build(
    source_dir: Path,
    out_dir: Path,
    *,
    key: Optional[Ed25519PrivateKey] = None,
    key_id: str | None = None,
    settings: Settings | None = None,
    retired: list[str] | None = None,
    held_out: HeldOut | None = None,
    sign_with: Callable[[dict[str, Any]], dict[str, Any]] | None = None,
    embedder: Embedder | None = None,
) -> Built:
    """Build one pack. Held-out items are excluded from every build: by default the held-out hashes are read from the
    repository's `evals/held-out/` above the source directory. The search index (CUR-30) is written once the
    content-pack contract has its tables: `search_fts` always, `search_vectors` only with an `embedder`, whose model
    the manifest then names."""
    settings = settings or Settings()
    source = load_source(source_dir)
    problems, verifier = problems_of(source, settings, retired)
    problems += (held_out if held_out is not None else default_held_out(source_dir)).conflicts(source.tables)
    if problems:
        raise BuildError(problems)
    held_back = carry_library(source, source.passed)
    figures.carry(source.tables, source.passed.get("figures", {}))
    tables = dict(source.tables)
    passages = derive(tables)
    if "search_fts" in packs.TABLES:
        tables["search_fts"] = [p.record() for p in passages]
    if embedder is not None and "search_vectors" in packs.TABLES:
        tables["search_vectors"] = search_vectors_table(passages, embedder)
    listed = [t for t in packs.TABLES if t in tables]
    manifest = {
        **{k: v for k, v in source.pack.items() if k != "retired_ids"},
        "schema_version": SCHEMA_VERSION,
        "tables": [{"name": t, "sha256": table_sha256(t, tables[t]), "rows": len(tables[t])} for t in listed],
        "retired_ids": sorted({*source.pack.get("retired_ids", []), *(retired or [])}),
    }
    if "search_vectors" in tables and embedder is not None:
        manifest["embedding_model"] = embedder.model.manifest()
    signer = sign_with
    if signer is None and key is not None and key_id:
        private, private_id = key, key_id
        signer = lambda unsigned: sign(unsigned, private, private_id)  # noqa: E731
    if signer is not None:  # the signing service's content key (D34, AUT-33)
        signed = signer(manifest)
        document = to_document(signed, tables)
        validate(CONTRACT, document)
    else:  # unsigned: every other part of the pack is still checked against the contract
        signed = manifest
        document = to_document(signed, tables)
        validate(CONTRACT, {**document, "manifest": {**document["manifest"], "signature": _PLACEHOLDER}})
    out_dir.mkdir(parents=True, exist_ok=True)
    stem = f"{signed['pack_id']}-{signed['version']}"
    pack_path = out_dir / f"{stem}.pack"
    write_pack(pack_path, signed, tables)
    json_path = out_dir / f"{stem}.json"
    json_path.write_text(json.dumps(document, indent=1, sort_keys=True, ensure_ascii=False) + "\n", encoding="utf-8")
    credit_tables = {
        table: [
            {**record, "provenance": source.authoring[record["id"]]["import_provenance"]}
            if record.get("id") in source.authoring
            and "provenance" not in record
            and source.authoring[record["id"]].get("import_provenance")
            else record
            for record in records
        ]
        for table, records in tables.items()
    }
    entries = licence.credits(credit_tables, row_id)
    credits_path = out_dir / f"{stem}.credits.html"
    credits_path.write_text(licence.credits_html(entries, signed["title"]), encoding="utf-8")
    report_path = out_dir / f"{stem}.report.json"
    report = {"pack_id": signed["pack_id"], "version": signed["version"], "credits": entries,
              "checks": [verifier.results[c].record() for c in sorted(verifier.results)],
              "held_back": held_back}  # fmt: skip
    report_path.write_text(json.dumps(report, indent=1, sort_keys=True) + "\n", encoding="utf-8")
    return Built(pack_path, json_path, credits_path, report_path, signed)
