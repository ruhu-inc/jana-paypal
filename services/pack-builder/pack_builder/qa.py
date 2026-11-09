"""Automated content QA (CUR-25, TABLE CUR-25a): the checks a record passes before `in_eval_bank -> published`.

Schema and references (`validate`, with `structure.reference_problems`) and tool verification (`verify`) run in their
own build stages; the sensitivity lint is the content scan (`pack_builder.scan`, PRV-17). This module holds the rest:

- Reading level: stems (item stems and part prompts, worked-example problems) and lesson text blocks at or below the
  level's readability grade (Flesch-Kincaid grade, with maths, numbers, symbols and the terms in the glossary of the
  record's KCs left out).
- Maths rendering: every LaTeX string renders in the client's KaTeX (UIA-15), with the client's options.
- Units and notation: a numeric key's unit is one the unit checker reads; text keeps a space between a number and its
  unit and never writes a decimal comma (LOC-10).
- Near-duplicates: two items whose wording is at least the configured similarity apart are flagged. With no embedding
  model shipped yet (CUR-30), wording is compared as hashed word and word-pair vectors; an `Embedder` replaces them.
- MCQ balance: per subject-level, the key's position among the options is spread within the tolerance of uniform
  (for each option count with enough items to judge).
- Accessibility: every library diagram and preset has alt text, and no text points at a drawing by colour alone.
- Local context: names, places and currencies of word problems come from the `pack.ref` lists
  (`content/curriculum/ref/common/context.yaml`).
"""

from __future__ import annotations

import hashlib
import itertools
import json
import math
import re
import shutil
import subprocess
import sys
from collections import Counter, defaultdict
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass
from functools import cache
from pathlib import Path
from typing import Any

import yaml
from agent_core.config import Settings
from agent_core.engine.unit_format import decimal_unit_comma, format_unit_spacing
from agent_core.retrieval.embedding import Embedder

from pack_builder.scan import CONTEXT_FILE, common_ref

REPO = (
    Path(getattr(sys, "_MEIPASS", "")) / "authoring-data"
    if getattr(sys, "frozen", False)
    else Path(__file__).resolve().parents[3]
)
KATEX_SCRIPT = Path(__file__).with_name("katex_check.mjs")
KATEX_FROM = (
    REPO / "package.json" if getattr(sys, "frozen", False) else REPO / "packages/ui/package.json"
)  # the client package whose KaTeX renders the maths (UIA-15)
LESSON_TEXT = ("idea", "common_mistakes", "summary")

# --- reading level --------------------------------------------------------------------------------------------------

_WORD = re.compile("[A-Za-z][A-Za-z'\u2019]*(?:-[A-Za-z]+)*")
_SENTENCE = re.compile(r"[.!?;:]+(?=\s|$)")
_LATEX = re.compile(r"\\[A-Za-z]+|[{}^_$\\]")


def syllables(word: str) -> int:
    """An English syllable estimate: vowel groups, less a silent final e (the usual readability heuristic)."""
    w = word.lower().strip("'\u2019-")
    if len(w) <= 3:
        return 1
    w = re.sub(r"(?:[^laeiouy]es|ed|[^laeiouy]e)$", "", w)
    w = re.sub(r"^y", "", w)
    return max(1, len(re.findall(r"[aeiouy]{1,2}", w)))


def _without(text: str, glossary: Iterable[str]) -> str:
    for term in sorted({g for g in glossary if g.strip()}, key=len, reverse=True):
        text = re.sub(rf"(?<!\w){re.escape(term)}(?:s|es)?(?!\w)", " ", text, flags=re.IGNORECASE)
    return _LATEX.sub(" ", text)


_SHORT = frozenset(
    {
        "a",
        "an",
        "as",
        "at",
        "be",
        "by",
        "do",
        "go",
        "he",
        "if",
        "in",
        "is",
        "it",
        "me",
        "my",
        "no",
        "of",
        "on",
        "or",
        "so",
        "to",
        "up",
        "us",
        "we",
    }
)


def _counted(token: str) -> bool:
    """Words of prose; symbols, units, element symbols and formulas ("N", "Na", "NaCl", "pH") are maths, not
    reading."""
    if any(c.isupper() for c in token[1:]):
        return False
    return not (len(token) <= 2 and not token.islower() and token.lower() not in _SHORT)


def reading_grade(text: str, glossary: Iterable[str] = ()) -> tuple[float, int]:
    """(Flesch-Kincaid grade, words counted) of prose, glossary terms and maths left out; (0, 0) with no prose."""
    prose = _without(text, glossary)
    sentences = [s for s in _SENTENCE.split(prose) if _WORD.search(s)]
    words = [w for w in _WORD.findall(prose) if _counted(w)]
    if not words or not sentences:
        return 0.0, 0
    grade = 0.39 * len(words) / len(sentences) + 11.8 * sum(syllables(w) for w in words) / len(words) - 15.59
    return round(grade, 1), len(words)


def reading_units(tables: Mapping[str, list[dict[str, Any]]]) -> Iterable[tuple[str, str, str, list[str]]]:
    """(record id, where, text, KC ids) for each stem and lesson text block."""
    for item in tables.get("items", []):
        text = " ".join([str(item["stem"]), *(str(p["prompt"]) for p in item["parts"])])
        yield str(item["id"]), "stem", text, list(item["kc_ids"])
    for example in tables.get("worked_examples", []):
        yield str(example["id"]), "problem", str(example["problem"]), list(example["kc_ids"])
    for lesson in tables.get("lessons", []):
        for index, block in enumerate(lesson["blocks"]):
            if block["type"] in LESSON_TEXT and "text" in block:
                yield str(lesson["id"]), f"blocks[{index}] {block['type']}", str(block["text"]["en"]), list(
                    lesson["kc_ids"])  # fmt: skip
            if block["type"] == "step":  # a step's lead-in, why and named idea (TABLE CUR-11b)
                for field in ("text", "why", "names"):
                    if field in block:
                        yield str(lesson["id"]), f"blocks[{index}] step {field}", str(block[field]["en"]), list(
                            lesson["kc_ids"])  # fmt: skip


def reading_problems(tables: Mapping[str, list[dict[str, Any]]], level: str, settings: Settings) -> list[str]:
    limit = settings.qa_reading_grade_max_a if level == "a" else settings.qa_reading_grade_max_o
    glossary = {kc["id"]: list(kc.get("glossary", [])) for kc in tables.get("kcs", [])}
    problems = []
    for rid, where, text, kc_ids in reading_units(tables):
        terms = [t for k in kc_ids for t in glossary.get(k, [])]
        grade, _ = reading_grade(text, terms)
        if grade > limit:
            problems.append(f"{rid}: {where} reads at grade {grade}, above {limit} (CUR-25 reading level)")
    return problems


# --- maths rendering ------------------------------------------------------------------------------------------------


def latex_strings(tables: Mapping[str, list[dict[str, Any]]]) -> list[tuple[str, str, str]]:
    """(record id, where, LaTeX) for every LaTeX field: board steps, final answers and free-response maths keys."""
    found: list[tuple[str, str, str]] = []

    def steps(rid: str, where: str, board: Iterable[Mapping[str, Any]]) -> None:
        for step in board:
            found.append((rid, f"{where} step {step['n']}", str(step["latex"])))

    for example in tables.get("worked_examples", []):
        steps(example["id"], "worked example", example["steps"])
        found.append((example["id"], "final answer", str(example["final_answer"]["latex"])))
    for item in tables.get("items", []):
        steps(item["id"], "solution", item["solution"].get("steps", []))
        for part in item["parts"]:
            answer = part["answer"]
            if answer["kind"] == "algebraic":
                found.append((item["id"], f"part {part['id']} key", str(answer["expression"])))
            elif answer["kind"] == "equation_solutions":
                found += [(item["id"], f"part {part['id']} key", str(v)) for v in answer["values"]]
    return found


@dataclass(frozen=True)
class Rendered:
    errors: dict[int, str]  # index -> KaTeX's message
    unavailable: str | None = None  # why the check could not run


def katex_render(latex: Sequence[str], settings: Settings) -> Rendered:
    """Render each string with the client's KaTeX in Node; strings KaTeX refuses come back with its message."""
    node = (
        str(REPO / "node" / ("node.exe" if sys.platform == "win32" else "node"))
        if getattr(sys, "frozen", False)
        else shutil.which("node")
    )
    if node is None:
        return Rendered({}, "node is not on PATH")
    if not KATEX_FROM.is_file():
        return Rendered({}, f"{KATEX_FROM} is missing")
    payload = json.dumps({"from": str(KATEX_FROM), "maxExpand": settings.ui_katex_max_expand,
                          "maxSize": settings.ui_katex_max_size_em, "tex": list(latex)})  # fmt: skip
    try:
        done = subprocess.run([node, str(KATEX_SCRIPT)], input=payload, capture_output=True, text=True, check=False,
                              timeout=300)  # fmt: skip
    except (OSError, subprocess.TimeoutExpired) as error:
        return Rendered({}, f"node could not run ({type(error).__name__})")
    if done.returncode != 0:
        return Rendered({}, (done.stderr.strip().splitlines() or ["KaTeX could not be loaded"])[-1][:200])
    return Rendered({int(k): str(v) for k, v in json.loads(done.stdout)["errors"].items()})


@cache
def _rendered(key: str, latex: tuple[str, ...], expand: int, size: float) -> Rendered:
    settings = Settings().model_copy(update={"ui_katex_max_expand": expand, "ui_katex_max_size_em": size})
    return katex_render(latex, settings)


def rendering_problems(tables: Mapping[str, list[dict[str, Any]]], settings: Settings) -> list[str]:
    strings = latex_strings(tables)
    unique = tuple(sorted({s for _, _, s in strings}))
    key = hashlib.sha256("\n".join(unique).encode()).hexdigest()
    rendered = _rendered(key, unique, settings.ui_katex_max_expand, settings.ui_katex_max_size_em)
    if rendered.unavailable is not None:
        return [f"maths rendering could not be checked: {rendered.unavailable} (CUR-25 maths rendering)"]
    failed = {unique[i]: message for i, message in rendered.errors.items()}
    return [f"{rid}: {where} does not render in KaTeX ({failed[tex][:120]}) (CUR-25 maths rendering)"
            for rid, where, tex in strings if tex in failed]  # fmt: skip


# --- units and notation ---------------------------------------------------------------------------------------------

# A number written against a unit symbol ("50V", "12kg"); single letters that double as variables (m, s, g, C) are
# left out, since "2g" or "3C" is usually algebra or a formula.
_UNIT_JOINED = re.compile(
    r"(?<![\w.^_{/\\])\d+(?:\.\d+)?(?:kg|km|cm|mm|nm|ms|kJ|MJ|kW|MW|kPa|kHz|MHz|GHz|mA|kV|mV|mol|Pa|Hz|N|J|W|V|A)"
    r"(?![\w/^])"
)
_DECIMAL_COMMA = re.compile(r"(?<![\d,])\d+,\d{1,2}(?![\d,])(?=\s?[a-zA-Z°%])")
_LATEX_FIELDS = ("latex", "expression", "equation", "values")
# Fields that hold codes and metadata, not wording a student reads (an ISO 4217 code, a chart of accounts id).
METADATA = ("id", "check_id", "provenance", "review", "currency", "chart_of_accounts", "account_id")


def _plain_texts(record: Any, path: str = "") -> Iterable[tuple[str, str]]:
    if isinstance(record, str):
        yield path, record
    elif isinstance(record, Mapping):
        for key, value in record.items():
            if key not in _LATEX_FIELDS and key not in METADATA:
                yield from _plain_texts(value, f"{path}.{key}" if path else str(key))
    elif isinstance(record, list):
        for index, value in enumerate(record):
            yield from _plain_texts(value, f"{path}[{index}]")


def unit_readable(unit: str) -> bool:
    """Whether the worker's unit checker reads a key's unit (the marker converts student units against it)."""
    from tool_worker.functions import FUNCTIONS
    from tool_worker.protocol import WorkerError
    from tool_worker.registry import CallContext

    context = CallContext("qa_units", "chk_qa_units", {}, {}, lambda: False, settings={})
    try:
        FUNCTIONS["units_convert"].implementation({"quantity": f"1 {unit}", "to": unit}, context)
    except (WorkerError, KeyError, ValueError, TypeError):
        return False
    return True


def notation_problems(tables: Mapping[str, list[dict[str, Any]]]) -> list[str]:
    problems = []
    units: dict[str, bool] = {}
    for item in tables.get("items", []):
        for part in item["parts"]:
            answer = part["answer"]
            unit = answer.get("unit") if answer["kind"] == "numeric" and answer["unit_policy"] != "none" else None
            if unit:
                if unit not in units:
                    units[unit] = unit_readable(unit)
                if not units[unit]:
                    problems.append(f"{item['id']} part {part['id']}: the unit {unit!r} is not one the unit checker "
                                    "reads (CUR-25 units and notation)")  # fmt: skip
    for table in ("items", "worked_examples", "lessons", "misconceptions"):
        for record in tables.get(table, []):
            for where, text in _plain_texts(record):
                joined = _UNIT_JOINED.search(text)
                if joined is not None:
                    problems.append(f"{record['id']}: {where} writes {joined.group(0)!r} with no space before the "
                                    "unit (CUR-25 units and notation)")  # fmt: skip
                comma = _DECIMAL_COMMA.search(text)
                if comma is not None:
                    problems.append(f"{record['id']}: {where} writes {comma.group(0)!r} with a decimal comma "
                                    "(CUR-25 units and notation)")  # fmt: skip
    for rid, where, latex in latex_strings(tables):
        if format_unit_spacing(latex, rid, latex=True) != latex:
            problems.append(f"{rid}: {where} has no space before an explicit unit (LOC-10)")
        if decimal_unit_comma(latex):
            problems.append(f"{rid}: {where} uses a decimal comma in a quantity (LOC-10)")
    return problems


# --- near-duplicates ------------------------------------------------------------------------------------------------

_DIMENSIONS = 1 << 13


def _wording(item: Mapping[str, Any]) -> str:
    parts = [str(item["stem"])]
    for part in item["parts"]:
        parts.append(str(part["prompt"]))
        for option in part["answer"].get("options", []):
            parts.append(str(option["text"]))
    return " ".join(parts)


def lexical_vectors(texts: Sequence[str]) -> list[dict[int, float]]:
    """Unit vectors of hashed words and word pairs (numbers kept: a variant with other numbers is another item)."""
    vectors = []
    for text in texts:
        words = re.findall(r"[a-z]+|\d+(?:\.\d+)?", text.lower())
        grams = Counter(words + [f"{a} {b}" for a, b in itertools.pairwise(words)])
        counts: dict[int, float] = defaultdict(float)
        for gram, n in grams.items():
            counts[int.from_bytes(hashlib.blake2b(gram.encode(), digest_size=4).digest(), "big") % _DIMENSIONS] += n
        norm = math.sqrt(sum(v * v for v in counts.values())) or 1.0
        vectors.append({k: v / norm for k, v in counts.items()})
    return vectors


def near_duplicates(texts: Sequence[str], threshold: float, embedder: Embedder | None = None
                    ) -> list[tuple[int, int, float]]:  # fmt: skip
    """Index pairs whose wording is at least `threshold` similar (cosine), found exactly by prefix filtering: features
    are ordered rarest first, and a vector's prefix is indexed up to where its remaining weight could no longer reach
    the threshold on its own, so any pair that does reach it shares a feature with an indexed prefix."""
    if embedder is not None:
        vectors: list[dict[int, float]] = []
        for embedded in embedder.embed(list(texts)):
            norm = math.sqrt(sum(v * v for v in embedded)) or 1.0
            vectors.append({i: v / norm for i, v in enumerate(embedded) if v})
    else:
        vectors = lexical_vectors(texts)
    frequency: Counter[int] = Counter(f for v in vectors for f in v)
    index: dict[int, list[int]] = defaultdict(list)
    for row, vector in enumerate(vectors):
        remaining = sum(w * w for w in vector.values())
        for feature in sorted(vector, key=lambda f: (frequency[f], f)):
            if math.sqrt(max(remaining, 0.0)) < threshold:
                break
            index[feature].append(row)
            remaining -= vector[feature] ** 2
    pairs = []
    for row, vector in enumerate(vectors):
        candidates = {other for f in vector for other in index.get(f, ()) if other > row}
        for other in sorted(candidates):
            theirs = vectors[other]
            score = sum(w * theirs.get(f, 0.0) for f, w in vector.items())
            if score >= threshold:
                pairs.append((row, other, score))
    return pairs


def key_of(item: Mapping[str, Any]) -> str:
    """What the item asks the student to arrive at, part by part: the key values, the correct options' wording, the
    order's wording. Two items worded alike with different keys are isomorphic variants (other numbers, other
    species), as parametric templates make them (CUR-17), not duplicates."""
    keys: list[Any] = []
    for part in item["parts"]:
        answer = dict(part["answer"])
        if answer["kind"] in ("mcq", "multi_select"):
            keys.append(sorted(str(o["text"]) for o in answer["options"] if o["correct"]))
        elif answer["kind"] == "ordering":
            text = {e["id"]: str(e["text"]) for e in answer["elements"]}
            keys.append([text.get(i, i) for i in answer["correct_order"]])
        else:
            keys.append({k: v for k, v in answer.items() if k not in ("check_id", "tolerance", "unit_policy")})
    return json.dumps(keys, sort_keys=True, ensure_ascii=False)


def duplicate_problems(tables: Mapping[str, list[dict[str, Any]]], settings: Settings,
                       embedder: Embedder | None = None) -> list[str]:  # fmt: skip
    """Items worded at or above the threshold alike that ask for the same thing (the same keys)."""
    items = tables.get("items", [])
    pairs = near_duplicates([_wording(i) for i in items], settings.qa_near_duplicate_similarity, embedder)
    return [f"{items[a]['id']}: worded {score:.2f} like {items[b]['id']}, with the same key, at or above "
            f"{settings.qa_near_duplicate_similarity} (CUR-25 near-duplicates)" for a, b, score in pairs
            if key_of(items[a]) == key_of(items[b])]  # fmt: skip


# --- MCQ balance ----------------------------------------------------------------------------------------------------


def balance_problems(tables: Mapping[str, list[dict[str, Any]]], pack: Mapping[str, Any], settings: Settings
                     ) -> list[str]:  # fmt: skip
    positions: dict[int, Counter[int]] = defaultdict(Counter)
    for item in tables.get("items", []):
        for part in item["parts"]:
            answer = part["answer"]
            if answer["kind"] == "mcq":
                key = next((i for i, o in enumerate(answer["options"]) if o["correct"]), None)
                if key is not None:
                    positions[len(answer["options"])][key] += 1
    problems = []
    tolerance = settings.qa_mcq_key_balance_tolerance
    for count, spread in sorted(positions.items()):
        total = sum(spread.values())
        if total < settings.qa_mcq_balance_min_items:
            continue
        uniform = 1 / count
        for position in range(count):
            share = spread[position] / total
            if abs(share - uniform) > tolerance * uniform:
                letter = "ABCDEFGH"[position]
                problems.append(f"pack.{pack.get('subject')}.{pack.get('level')}: {share:.1%} of the {total} "
                                f"{count}-option keys are option {letter}, outside {uniform:.1%} ± "
                                f"{tolerance:.0%} (CUR-25 MCQ balance)")  # fmt: skip
    return problems


# --- accessibility --------------------------------------------------------------------------------------------------

_COLOUR_ONLY = re.compile(
    r"\b(?:red|blue|green|yellow|orange|purple|pink|grey|gray|brown|black)\s+(?:line|curve|graph|bar|arrow|region|"
    r"area|wire|ray|dot|point|shape|box|bars|lines|curves|arrows|dots|points)\b(?![^.]*\b(?:labelled|marked|"
    r"named|called)\b)",
    re.IGNORECASE,
)


def accessibility_problems(tables: Mapping[str, list[dict[str, Any]]], library: Mapping[str, list[dict[str, Any]]],
                           settings: Settings) -> list[str]:  # fmt: skip
    problems = []
    for table in ("diagrams", "visual_presets"):
        for entry in library.get(table, []):
            alt = str(entry.get("alt_text") or "")
            if len(alt.split()) < settings.qa_alt_text_min_words:
                problems.append(f"{entry.get('id')}: alt text {alt!r} is missing or under "
                                f"{settings.qa_alt_text_min_words} words (CUR-25 accessibility)")  # fmt: skip
    for table in ("items", "worked_examples", "lessons", "misconceptions"):
        for record in tables.get(table, []):
            for where, text in _plain_texts(record):
                found = _COLOUR_ONLY.search(text)
                if found is not None:
                    problems.append(f"{record['id']}: {where} points at {found.group(0)!r} by colour alone "
                                    "(CUR-25 accessibility)")  # fmt: skip
    return problems


# --- local context --------------------------------------------------------------------------------------------------


@dataclass(frozen=True)
class Context:
    names: frozenset[str]
    places: frozenset[str]
    proper_terms: frozenset[str]
    business_words: frozenset[str]
    currencies: frozenset[str]
    markers: tuple[str, ...]


@cache
def _context(path: Path, stamp: int) -> Context:
    data = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    return Context(frozenset(data.get("names") or []), frozenset(data.get("places") or []),
                   frozenset(data.get("proper_terms") or []), frozenset(data.get("business_words") or []),
                   frozenset(str(c) for c in data.get("currencies") or []),
                   tuple(str(m) for m in data.get("currency_markers") or []))  # fmt: skip


def context_for(pack_dir: Path | None) -> Context | None:
    path = common_ref(pack_dir) / CONTEXT_FILE
    return _context(path.resolve(), path.stat().st_mtime_ns) if path.is_file() else None


def _marker_pattern(marker: str) -> re.Pattern[str]:
    if re.fullmatch(r"[A-Za-z]+", marker):
        flags = 0 if marker.isupper() else re.IGNORECASE
        return re.compile(rf"(?<![A-Za-z]){re.escape(marker)}(?![A-Za-z])", flags)
    return re.compile(re.escape(marker))


_WORD_ENDING = re.compile(r"(?:ing|ed|ly|tion|ment|ness)$")  # "Following", "Repairing": words, not names


def _vocabulary(tables: Mapping[str, list[dict[str, Any]]]) -> frozenset[str]:
    """Words written in lower case anywhere in the pack or in the repository's curriculum sources: a capitalised word
    found here is an ordinary word that starts a sentence, not a name."""
    found = {w for records in tables.values() for r in records
             for w in re.findall(r"\b[a-z][a-z]+\b", json.dumps(r, ensure_ascii=False))}  # fmt: skip
    return frozenset(found) | _repository_words(REPO / "content" / "curriculum")


@cache
def _repository_words(root: Path) -> frozenset[str]:
    words: set[str] = set()
    for path in root.rglob("*.yaml"):
        words |= set(re.findall(r"\b[a-z][a-z]+\b", path.read_text(encoding="utf-8")))
    return frozenset(words)


def context_problems(tables: Mapping[str, list[dict[str, Any]]], pack: Mapping[str, Any], context: Context | None
                     ) -> list[str]:  # fmt: skip
    if context is None:
        return ["no local-context lists (common/context.yaml in the reference pack) (CUR-25 local context)"]
    currency = {str(c["value"]) for c in tables.get("conventions", []) if str(c["key"]).endswith(".currency")}
    allowed = {c.lower() for c in context.currencies | currency}
    markers = [(m, _marker_pattern(m)) for m in context.markers if m.lower() not in allowed]
    vocabulary = _vocabulary(tables)
    known = context.names | context.places | context.proper_terms | context.business_words
    problems = []
    for table in ("items", "worked_examples", "lessons"):
        for record in tables.get(table, []):
            texts = list(_plain_texts({k: v for k, v in record.items() if k not in ("kc_ids", "node_id")}))
            for where, text in texts:
                for marker, pattern in markers:
                    if pattern.search(text):
                        problems.append(f"{record['id']}: {where} uses the currency {marker!r}, not one of the "
                                        "pack.ref currencies (CUR-25 local context)")  # fmt: skip
            tags = set(record.get("context_tags", []))
            if not tags & {"local_names", "local_places"}:
                continue
            words = " ".join(text for _, text in texts)
            unknown = sorted(
                {
                    w
                    for w in re.findall(r"\b[A-Z][a-z]{2,}\b", words)
                    if w not in known and w.lower() not in vocabulary and not _WORD_ENDING.search(w)
                }
            )
            if unknown:
                problems.append(f"{record['id']}: names {', '.join(unknown)}, not in the pack.ref name and place "
                                "lists (CUR-25 local context)")  # fmt: skip
            if "local_names" in tags and not any(re.search(rf"\b{re.escape(n)}\b", words) for n in context.names):
                problems.append(f"{record['id']}: tagged local_names but names no one from the pack.ref list "
                                "(CUR-25 local context)")  # fmt: skip
    return problems


# --- every QA check -------------------------------------------------------------------------------------------------


def qa_problems(tables: Mapping[str, list[dict[str, Any]]], library: Mapping[str, list[dict[str, Any]]],
                pack: Mapping[str, Any], pack_dir: Path | None, settings: Settings,
                embedder: Embedder | None = None) -> list[str]:  # fmt: skip
    """The checks of TABLE CUR-25a this module owns, over every record of the pack."""
    return [
        *reading_problems(tables, str(pack.get("level")), settings),
        *rendering_problems(tables, settings),
        *notation_problems(tables),
        *duplicate_problems(tables, settings, embedder),
        *balance_problems(tables, pack, settings),
        *accessibility_problems(tables, library, settings),
        *context_problems(tables, pack, context_for(pack_dir)),
    ]
