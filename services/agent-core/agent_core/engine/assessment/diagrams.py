"""Numbered assessment diagrams with no answer-bearing library metadata."""

from __future__ import annotations

import json
import re
import xml.etree.ElementTree as ET
from collections.abc import Mapping
from typing import Any

from agent_core.assets.visuals import VisualContext, render
from agent_core.store.pack_index import PackIndex
from agent_core.store.svg import SVG_NS, SvgRefused, parse, sanitise_problems


def public_ref(item: Mapping[str, Any]) -> str:
    return f"dgm.{str(item['id']).split('.')[1]}.assessment.v1"


def aliases(item: Mapping[str, Any]) -> dict[str, str]:
    targets = dict.fromkeys(
        t["part_id"] for p in item["parts"] if p["answer"]["kind"] == "diagram_label" for t in p["answer"]["targets"]
    )
    return {target: f"label_{n}" for n, target in enumerate(targets, 1)}


def diagram(item: Mapping[str, Any], content: PackIndex) -> dict[str, Any] | None:
    ref = public_ref(item)
    parts = [p for p in item["parts"] if p["answer"]["kind"] == "diagram_label"]
    if not parts:
        return None
    refs = {p["answer"]["diagram_id"] for p in parts}
    if len(refs) != 1:
        raise ValueError("assessment requires one diagram")
    code, level = str(item["id"]).split(".")[1:3]
    entry = content.visual_sources(code)["diagrams"].get(next(iter(refs)))
    if not entry or not entry.get("licence") or not entry.get("svg"):
        raise ValueError("assessment diagram unavailable")
    if entry.get("approved") is False or (entry.get("review") or {}).get("status") in ("draft", "retired"):
        raise ValueError("assessment diagram unavailable")
    numbered = aliases(item)
    known = {p["id"]: p for p in entry.get("parts", [])}
    if not numbered.keys() <= known.keys():
        raise ValueError("assessment diagram targets unavailable")
    # The authored alternative must describe geometry without naming the requested answers.
    # Never derive an alternative from the answer key or silently give it to a screen reader.
    forbidden = {label.casefold() for p in parts for t in p["answer"]["targets"] for label in t["accepted_labels"]}
    forbidden.update(str(known[k]["name"]).casefold() for k in numbered)
    alt = str(entry.get("alt_text", "")).strip()
    if not alt or any(re.search(r"(?<!\w)" + re.escape(label) + r"(?!\w)", alt.casefold()) for label in forbidden):
        raise ValueError("assessment diagram needs a non-revealing alternative")
    try:
        if sanitise_problems(entry["svg"], 256 * 1024):
            raise ValueError("assessment diagram refused")
        root = parse(entry["svg"])
    except SvgRefused as error:
        raise ValueError("assessment diagram refused") from error
    ids = {e.attrib["id"]: f"v{n}" for n, e in enumerate(root.iter()) if "id" in e.attrib}
    visible_labels: set[str] = set()
    for parent in list(root.iter()):
        for child in list(parent):
            tag = child.tag.rsplit("}", 1)[-1]
            label = child.get("data-label-for")
            if (
                tag in ("title", "desc")
                or child.get("data-arrow") is not None
                or (tag in ("text", "tspan") and label not in numbered)
            ):
                parent.remove(child)
    for element in root.iter():
        label = element.get("data-label-for")
        if label in numbered:
            visible_labels.add(label)
            for child in list(element):
                element.remove(child)
            element.text = numbered[label].removeprefix("label_")
        elif element.tag.rsplit("}", 1)[-1] not in ("text", "tspan"):
            element.text = None
        element.tail = None
        for key, value in list(element.attrib.items()):
            if key in ("data-part", "data-label-for"):
                if value in numbered:
                    element.set(key, numbered[value])
                else:
                    del element.attrib[key]
            elif key.startswith("aria-") or key in ("role", "tabindex", "data-arrow"):
                del element.attrib[key]
            elif key == "id":
                element.set(key, ids[value])
            else:
                for old, new in sorted(ids.items(), key=lambda pair: -len(pair[0])):
                    value = re.sub(r"#" + re.escape(old) + r"(?=[)\s]|$)", "#" + new, value)
                element.set(key, value)
    if visible_labels != set(numbered):
        raise ValueError("assessment diagram needs visible numbered targets")
    ET.register_namespace("", SVG_NS)
    public = {
        "id": ref,
        "version": "1.0.0",
        "title": "Numbered diagram",
        "approved": True,
        "licence": entry["licence"],
        "attribution": entry.get("attribution", ""),
        "alt_text": alt,
        "parts": [
            {"id": public_id, "name": f"Part {n}", "tab_order": n} for n, public_id in enumerate(numbered.values(), 1)
        ],
        "svg": ET.tostring(root, encoding="unicode"),
        "sequences": [],
        "regions": [],
    }
    if any(re.search(r"(?<!\w)" + re.escape(label) + r"(?!\w)", json.dumps(public).casefold()) for label in forbidden):
        raise ValueError("assessment diagram contains answer-bearing metadata")
    subject = {"mth": "mathematics", "phy": "physics", "chm": "chemistry", "bio": "biology", "acc": "accounting"}[code]
    result = render(
        {
            "spec_version": 1,
            "type": "library_diagram",
            "ref": ref,
            "params": {},
            "numbers_from": [],
            "steps": [],
            "labels": "quiz",
        },
        VisualContext(subject, level.upper(), stage="S2", diagrams={ref: public}),
    )
    if result.status not in ("ok", "repaired") or result.resolved is None:
        raise ValueError("assessment diagram could not be rendered")
    return result.resolved
