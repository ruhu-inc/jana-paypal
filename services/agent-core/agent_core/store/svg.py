"""Library SVG: normalise, sanitise and lint (VSL-22, VSL-23, VSL-34, DSN-18, CUR-29).

One implementation serves the pack build (the diagram branch of the authoring pipeline) and pack load, where every
diagram's SVG is checked again before the pack is accepted (SEC-13). The client applies the same allow-list when it
turns the markup into elements (packages/renderers/src/library_svg.ts), so a pack that slipped past both would still
draw nothing it should not.

Sanitising is an allow-list: the elements and attributes below and nothing else; no script, foreignObject, event
handler, style element or attribute, embedded font or image, and no reference that leaves the document (`href` and
`url()` only point at `#id`). Colours are design-token variables (`var(--color-…)`), or a content colour swatch
(`var(--content-…)`) on an element that names its colour, so a diagram follows the theme and a colour that is the
content is never its only carrier of meaning.
"""

from __future__ import annotations

import re
import xml.etree.ElementTree as ET
from collections.abc import Iterable
from dataclasses import dataclass
from typing import Any

SVG_NS = "http://www.w3.org/2000/svg"
XLINK_NS = "http://www.w3.org/1999/xlink"
ALLOWED_ELEMENTS = frozenset(
    {
        "svg",
        "g",
        "path",
        "rect",
        "circle",
        "ellipse",
        "line",
        "polyline",
        "polygon",
        "text",
        "tspan",
        "defs",
        "use",
        "marker",
        "title",
        "desc",
        "clipPath",
        "linearGradient",
        "stop",
    }
)
GEOMETRY = frozenset(
    {
        "x",
        "y",
        "x1",
        "y1",
        "x2",
        "y2",
        "cx",
        "cy",
        "r",
        "rx",
        "ry",
        "width",
        "height",
        "d",
        "points",
        "transform",
        "viewBox",
        "dx",
        "dy",
        "offset",
        "markerWidth",
        "markerHeight",
        "refX",
        "refY",
        "orient",
        "markerUnits",
        "gradientUnits",
        "clipPathUnits",
        "preserveAspectRatio",
        "version",
        "xmlns",
    }
)
PRESENTATION = frozenset(
    {
        "fill",
        "stroke",
        "stroke-width",
        "stroke-dasharray",
        "stroke-linecap",
        "stroke-linejoin",
        "fill-opacity",
        "stroke-opacity",
        "opacity",
        "font-size",
        "font-weight",
        "font-style",
        "text-anchor",
        "dominant-baseline",
        "marker-start",
        "marker-end",
        "marker-mid",
        "clip-path",
        "stop-color",
        "stop-opacity",
        "fill-rule",
        "vector-effect",
    }
)
DATA = frozenset(
    {
        "id",
        "data-part",
        "data-arrow",
        "data-label-for",
        "data-content-colour",
        "data-region",
        "href",
        f"{{{XLINK_NS}}}href",
        "role",
        "aria-hidden",
    }
)
ALLOWED_ATTRIBUTES = GEOMETRY | PRESENTATION | DATA
COLOUR_ATTRIBUTES = ("fill", "stroke", "stop-color")
TOKEN_COLOUR = re.compile(r"^var\(--color-([a-z0-9-]+)\)$")
CONTENT_COLOUR = re.compile(r"^var\(--content-([a-z-]+)\)$")
INTERNAL_URL = re.compile(r"^url\(#[A-Za-z][A-Za-z0-9_.-]*\)$")
# Role tokens a library drawing may use (DSN-18): strokes and ink, the label greys, surfaces, the four series and the
# highlight pair; status colours (danger, warn) never appear in content.
DRAWING_TOKENS = frozenset(
    {
        "diagram-stroke",
        "diagram-highlight",
        "diagram-highlight-ring",
        "ink",
        "muted",
        "faint",
        "line",
        "line-strong",
        "paper",
        "canvas",
        "panel",
        "series-1",
        "series-2",
        "series-3",
        "series-4",
    }
)
TEXT_TOKENS = frozenset({"ink", "muted", "diagram-stroke"})
STROKE_WIDTHS = frozenset({"1", "1.5", "3"})  # hairline leaders, the 1.5 px drawing stroke, the 3 px highlight ring
MIN_TEXT_SIZE = 12.0
TEXT_ADVANCE = 0.6  # a label's width is estimated at 0.6 em per character for the overlap check


class SvgRefused(ValueError):
    """The markup is not a drawing the app will show."""


@dataclass(frozen=True)
class Label:
    text: str
    x: float
    y: float
    size: float
    anchor: str

    def box(self) -> tuple[float, float, float, float]:
        width = len(self.text) * self.size * TEXT_ADVANCE
        left = self.x - width / 2 if self.anchor == "middle" else self.x - width if self.anchor == "end" else self.x
        return left, self.y - self.size, left + width, self.y


def _local(tag: str) -> str:
    return tag.split("}", 1)[1] if tag.startswith("{") else tag


def parse(text: str) -> ET.Element:
    if re.search(r"<!(DOCTYPE|ENTITY)", text, re.I):
        raise SvgRefused("a doctype or entity declaration")
    try:
        root = ET.fromstring(text)  # noqa: S314 - no doctype, no entities (refused above), local file only
    except ET.ParseError as error:
        raise SvgRefused(f"not well-formed ({error})") from None
    if root.tag != f"{{{SVG_NS}}}svg":
        raise SvgRefused("the root is not an SVG element")
    return root


def sanitise_problems(text: str, max_bytes: int) -> list[str]:
    """Everything VSL-23 refuses in a library SVG; [] when the markup may be shipped and drawn."""
    if len(text.encode("utf-8")) > max_bytes:
        return [f"the SVG is {len(text.encode('utf-8'))} bytes (at most {max_bytes})"]
    try:
        root = parse(text)
    except SvgRefused as error:
        return [str(error)]
    problems = []
    for element in root.iter():
        name = _local(str(element.tag))
        if str(element.tag).startswith("{") and not str(element.tag).startswith(f"{{{SVG_NS}}}"):
            problems.append(f"<{name}> is outside the SVG namespace")
            continue
        if name not in ALLOWED_ELEMENTS:
            problems.append(f"<{name}> is not allowed")
        for attribute, value in element.attrib.items():
            local = attribute
            if local.lower().startswith("on"):
                problems.append(f"<{name}> has an event handler ({local})")
            elif local not in ALLOWED_ATTRIBUTES:
                problems.append(f"<{name}> has a {local} attribute")
            elif local in ("href", f"{{{XLINK_NS}}}href") and (name != "use" or not value.startswith("#")):
                problems.append(f"<{name}> refers outside the document ({value[:40]})")
            elif "url(" in value and not INTERNAL_URL.match(value.strip()):
                problems.append(f"<{name}> {local} uses an external url()")
            elif "javascript:" in value.lower() or "data:" in value.lower():
                problems.append(f"<{name}> {local} carries a script or data URL")
    return problems


def normalise(text: str) -> str:
    """The build's normalising step: comments, processing instructions and metadata out, coordinates rounded to two
    decimals, whitespace between elements removed; data-part ids are kept as written."""
    text = re.sub(r"<\?xml[^>]*\?>", "", text)
    text = re.sub(r"<!--.*?-->", "", text, flags=re.S)
    text = re.sub(r"<metadata\b.*?</metadata>", "", text, flags=re.S)
    text = re.sub(r"(\d+\.\d{3,})", lambda m: f"{float(m.group(1)):.2f}".rstrip("0").rstrip("."), text)
    text = re.sub(r">\s+<", "><", text.strip())
    return text + "\n"


def part_ids(text: str) -> list[str]:
    return [str(e.get("data-part")) for e in parse(text).iter() if e.get("data-part") is not None]


def arrow_ids(text: str) -> list[str]:
    return [str(e.get("data-arrow")) for e in parse(text).iter() if e.get("data-arrow") is not None]


def _labels(root: ET.Element) -> Iterable[tuple[ET.Element, Label]]:
    for element in root.iter(f"{{{SVG_NS}}}text"):
        content = "".join(element.itertext()).strip()
        try:
            label = Label(
                content,
                float(element.get("x", "0")),
                float(element.get("y", "0")),
                float(element.get("font-size", "0") or 0),
                element.get("text-anchor", "start"),
            )
        except ValueError:
            label = Label(content, 0, 0, 0, "start")
        yield element, label


def _overlap(a: tuple[float, float, float, float], b: tuple[float, float, float, float]) -> bool:
    return a[0] < b[2] and b[0] < a[2] and a[1] < b[3] and b[1] < a[3]


def lint_problems(text: str, entry: dict[str, Any]) -> list[str]:
    """VSL-34 lint over a sanitised drawing and its entry: ids and names, label anchors, the 1.5 px stroke style,
    token colours, named content colours, label size and overlapping labels."""
    root = parse(text)
    problems = []
    parts = {p["id"]: p for p in entry.get("parts", [])}
    for pid, part in parts.items():
        if not str(part.get("name", "")).strip():
            problems.append(f"part {pid} has no name")
        if "label_anchor" not in part:
            problems.append(f"part {pid} has no label anchor")
    drawn = [str(e.get("data-part")) for e in root.iter() if e.get("data-part") in parts]
    tabbed = sorted(drawn, key=lambda pid: int(parts[pid].get("tab_order", 0)))
    if drawn != tabbed:
        problems.append(f"parts are drawn in the order {', '.join(drawn)}, not their tab order (LOC-15)")
    for element in root.iter():
        name = _local(str(element.tag))
        for attribute in COLOUR_ATTRIBUTES:
            value = element.get(attribute)
            if value is None or value in ("none", "transparent", "currentColor") or INTERNAL_URL.match(value):
                continue
            token = TOKEN_COLOUR.match(value)
            content = CONTENT_COLOUR.match(value)
            if token and token.group(1) not in DRAWING_TOKENS:
                problems.append(f"<{name}> {attribute} uses {token.group(1)}, which is not a drawing token")
            elif content:
                if element.get("data-content-colour") != content.group(1):
                    problems.append(f"<{name}> uses content colour {content.group(1)} without naming it")
                if element.get("stroke") != "var(--color-line-strong)":
                    problems.append(f"<{name}> content colour {content.group(1)} has no line-strong outline")
            elif not token:
                problems.append(f"<{name}> {attribute}={value} is a fixed colour, not a token")
        width = element.get("stroke-width")
        if width is not None and width not in STROKE_WIDTHS:
            problems.append(f"<{name}> stroke-width {width} is outside the style guide (1, 1.5 or 3)")
        if name == "text":
            fill = element.get("fill", "var(--color-ink)")
            token = TOKEN_COLOUR.match(fill)
            if token and token.group(1) not in TEXT_TOKENS:
                problems.append(f"label fill {token.group(1)} is too faint for text")
    named = {str(e.get("data-content-colour")) for e in root.iter() if e.get("data-content-colour")}
    texts = " ".join("".join(t.itertext()).lower() for t in root.iter(f"{{{SVG_NS}}}text"))
    for colour in sorted(named):
        if colour.replace("-", " ") not in texts:
            problems.append(f"content colour {colour} is not named in a label")
    labels = list(_labels(root))
    for _, label in labels:
        if label.size < MIN_TEXT_SIZE:
            problems.append(f"label {label.text!r} is {label.size:g} px (at least {MIN_TEXT_SIZE:g})")
    for i, (_, a) in enumerate(labels):
        for _, b in labels[i + 1 :]:
            if a.text and b.text and _overlap(a.box(), b.box()):
                problems.append(f"labels {a.text!r} and {b.text!r} overlap")
    return problems


def tagging_problems(text: str) -> list[str]:
    problems = []

    def visit(node: ET.Element, covered: bool, decoration: bool) -> None:
        tag = _local(str(node.tag))
        covered = (
            covered or bool(node.get("data-part")) or bool(node.get("data-label-for")) or bool(node.get("data-arrow"))
        )
        decoration = (
            decoration or tag in ("defs", "marker", "clipPath", "linearGradient") or node.get("aria-hidden") == "true"
        )
        if (
            tag in ("path", "rect", "circle", "ellipse", "line", "polyline", "polygon", "text", "use")
            and not covered
            and not decoration
        ):
            problems.append(f"Untagged labellable element: {tag} {node.get('id', '')}")
        for child in node:
            visit(child, covered, decoration)

    visit(parse(text), False, False)
    return problems


def parts_problems(entry: dict[str, Any], text: str) -> list[str]:
    """VSL-22 and CUR-29: every declared part has an element in the SVG and every data-part is declared; every
    sequence step names declared parts and pre-drawn arrows."""
    problems = tagging_problems(text)
    declared = [p["id"] for p in entry.get("parts", [])]
    drawn = part_ids(text)
    arrows = set(arrow_ids(text))
    for pid in declared:
        if pid not in drawn:
            problems.append(f"part {pid} has no element in the SVG")
    for pid in sorted(set(drawn) - set(declared)):
        problems.append(f"the SVG has data-part {pid}, which the entry does not declare")
    for dup in sorted({d for d in drawn if drawn.count(d) > 1}):
        problems.append(f"data-part {dup} is on more than one element")
    for sequence in entry.get("sequences", []):
        for n, step in enumerate(sequence.get("steps", []), 1):
            for pid in step.get("highlight", []):
                if pid not in declared:
                    problems.append(f"sequence {sequence['id']} step {n} names unknown part {pid}")
            for aid in step.get("arrows", []):
                if aid not in arrows:
                    problems.append(f"sequence {sequence['id']} step {n} names unknown arrow {aid}")
    return problems
