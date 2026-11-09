"""The diagram library editor: safe SVG, part tagging, production previews and canonical saves."""

from __future__ import annotations

import copy
import re
import xml.etree.ElementTree as ET
from pathlib import Path
from typing import TYPE_CHECKING, Any

from agent_core.assets.visuals import VisualContext, VisualError, validate
from agent_core.config import Settings
from agent_core.store import svg

from pack_builder import diagrams, licence, source, versioning
from pack_builder.accounts import authorise
from pack_builder.studio_io import atomic_changes, revision

if TYPE_CHECKING:
    from pack_builder.studio import Workspace

ET.register_namespace("", svg.SVG_NS)


def locate(workspace: Workspace, pack: str, identity: str) -> source.Located:
    _, found = workspace.records(pack)
    item = next((i for i in found["diagrams"] if i.record["id"] == identity), None)
    if item is None:
        raise ValueError("Choose a diagram from the open pack")
    return item


def drawing_path(workspace: Workspace, pack: str, entry: dict[str, Any]) -> Path:
    directory = workspace.pack(pack) / "diagrams"
    path: Path = workspace.pack(pack) / str(entry["svg_path"])
    if not path.resolve().is_relative_to(directory) or path.suffix != ".svg" or path.is_symlink():
        raise ValueError("The SVG must be a regular file inside this pack diagram directory")
    return path


def sanitise(text: str) -> dict[str, Any]:
    if len(text.encode()) > Settings().visual_library_svg_max_kb * 1024:
        raise ValueError("The SVG exceeds the library size limit")
    root = svg.parse(text)
    removed = []
    for parent in list(root.iter()):
        for child in list(parent):
            if svg._local(str(child.tag)) not in svg.ALLOWED_ELEMENTS or not str(child.tag).startswith(
                "{" + svg.SVG_NS + "}"
            ):
                removed.append("element " + str(child.tag))
                parent.remove(child)
        for key, value in list(parent.attrib.items()):
            if (
                key not in svg.ALLOWED_ATTRIBUTES
                or key.lower().startswith("on")
                or (
                    key in ("href", "{" + svg.XLINK_NS + "}href")
                    and (svg._local(str(parent.tag)) != "use" or not value.startswith("#"))
                )
                or ("url(" in value and not svg.INTERNAL_URL.match(value.strip()))
                or "javascript:" in value.lower()
                or "data:" in value.lower()
            ):
                removed.append("attribute " + key)
                del parent.attrib[key]
    clean = svg.normalise(ET.tostring(root, encoding="unicode"))
    problems = svg.sanitise_problems(clean, Settings().visual_library_svg_max_kb * 1024)
    return {"svg": clean, "removed": removed, "ok": not problems, "problems": problems}


def check(workspace: Workspace, pack: str, entry: dict[str, Any], drawing: str) -> dict[str, Any]:
    drawing_path(workspace, pack, entry)
    problems = diagrams.diagram_problems(entry, drawing, Settings())
    problems += licence.gate_problems("diagrams", entry, str(entry["id"]))
    problems += licence.attribution_problems("diagrams", entry, str(entry["id"]))
    preview = None
    if not svg.sanitise_problems(drawing, Settings().visual_library_svg_max_kb * 1024):
        if not problems:
            ctx = VisualContext(
                diagrams.SUBJECTS[entry["subject"]],
                entry["levels"][0],
                "S2",
                diagrams={entry["id"]: {**entry, "svg": drawing}},
            )
            try:
                preview, _ = validate(
                    {"spec_version": 1, "type": "library_diagram", "ref": entry["id"], "params": {}}, ctx
                )
            except VisualError as error:
                problems.append(str(error))
    from pack_builder.form_schema import form_schema

    schema = form_schema("visuals/diagram-entry")

    return {
        "ok": not problems,
        "problems": problems,
        "schema": schema,
        "preview": preview,
        "licence": entry.get("licence"),
        "source": entry.get("sources", []),
        "reverification": entry.get("review", {}).get("approved_at"),
    }


def dispatch(workspace: Workspace, request: dict[str, Any]) -> dict[str, Any]:
    op, pack = request["op"], request["pack"]
    if op == "diagram_sanitise":
        workspace.pack(pack)
        return sanitise(request["svg"])
    if op == "diagram_tag":
        workspace.pack(pack)
        root = svg.parse(request["svg"])
        nodes = list(root.iter())
        index = request["element"]
        if (
            not isinstance(index, int)
            or not 0 <= index < len(nodes)
            or not re.fullmatch(r"[a-z][a-z0-9_]{0,39}", request["part"])
        ):
            raise ValueError("Choose a drawing element and a valid part id")
        nodes[index].set("data-part", request["part"])
        return {"svg": ET.tostring(root, encoding="unicode")}
    if op == "diagram_check":
        return check(workspace, pack, request["entry"], request["svg"])
    item = locate(workspace, pack, request["id"])
    path = drawing_path(workspace, pack, item.record)
    if op == "diagram_open":
        return {
            "entry": item.record,
            "svg": path.read_text(),
            "revision": revision(item.path),
            "svg_revision": revision(path),
            "report": check(workspace, pack, item.record, path.read_text()),
        }
    ctx = workspace.ctx(request.get("author"))
    account = ctx.need()
    authorise(account, "edit_draft", ctx.audit, subject=request["id"])
    entry, drawing = copy.deepcopy(request["entry"]), str(request["svg"])
    if len(drawing.encode()) > 512 * 1024:
        raise ValueError("SVG source is too large")
    if op == "diagram_duplicate":
        identity = request["new_id"]
        if not re.fullmatch(r"dgm\." + re.escape(item.record["subject"]) + r"\.[a-z0-9_]+\.v[1-9][0-9]*", identity):
            raise ValueError("Use a new diagram id of this subject, such as dgm.phy.new_diagram.v1")
        entry["id"] = identity
        entry["version"] = "1.0.0"
        entry["review"] = {**entry["review"], "status": "in_review"}
        entry["svg_path"] = f"diagrams/{identity}.svg"
        target = workspace.pack(pack) / f"diagrams/{identity}.yaml"
        path = drawing_path(workspace, pack, entry)
        manifest = source._load(workspace.pack(pack) / "pack.yaml")
        if target.exists() or path.exists() or identity in manifest.get("retired_ids", []):
            raise ValueError("That diagram identity is already used or retired")
        expected, svg_expected = "missing", "missing"
    elif op == "diagram_save":
        if entry["id"] != request["id"] or entry["svg_path"] != item.record["svg_path"]:
            raise ValueError("Diagram identity and drawing path are immutable; duplicate to a new identity")
        meanings = {p["id"]: p.get("function") for p in item.record["parts"]}
        if any(p["id"] in meanings and p.get("function") != meanings[p["id"]] for p in entry["parts"]):
            raise ValueError("Changing a part meaning requires a new diagram identity")
        target = item.path
        expected, svg_expected = request["revision"], request["svg_revision"]
        entry["review"] = {**entry["review"], "status": "in_review"}
        old = item.record["version"]
        if item.record.get("review", {}).get("status") == "approved" and (
            drawing != path.read_text() or entry["parts"] != item.record["parts"]
        ):
            a, b, c = versioning.version_tuple(old)
            entry["version"] = f"{a}.{b}.{c + 1}"
    else:
        raise ValueError("Unknown diagram command")
    # Invalid drafts may be saved; submitting/building still uses all library gates.
    atomic_changes(
        workspace.root,
        [
            {
                "path": str(target.relative_to(workspace.root)),
                "text": source.canonical(entry, "diagrams"),
                "expected": expected,
            },
            {"path": str(path.relative_to(workspace.root)), "text": drawing, "expected": svg_expected},
        ],
    )
    ctx.audit.append(account.account_id, "studio_diagram_save", "ok", entry["id"], revision(target))
    return {
        "entry": entry,
        "svg": drawing,
        "revision": revision(target),
        "svg_revision": revision(path),
        "report": check(workspace, pack, entry, drawing),
    }
