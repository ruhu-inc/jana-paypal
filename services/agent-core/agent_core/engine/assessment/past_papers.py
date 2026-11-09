"""Imported past papers: read as written, sat timed like a mock, marked honestly (ASM-27, D119).

A learner adds a past paper (and, if they have it, its mark scheme) to the study area as a file or pasted text; the
study area reads it (online for files, `study_materials`, D121). `import_past_paper` then keeps every question as
written, with its own numbering, marks and the page of any diagram it refers to, and links each question to the
knowledge components of an installed syllabus where its words name a syllabus topic (`matching_topics`, never a model
guess at a pack id).

Marking follows the ASM rules, and every mark says how it was decided (`method`):

- `checked`: a numeric or expression answer compared by tools (`math_check_equivalence`, `units_convert`, and exact
  decimal rounding to the key's own precision) against the key, which is the mark scheme's answer, never a model's;
- `key`: a choice compared with the mark scheme's key;
- `judged`: everything else, by the local judge against the scheme's points (method marks included) or, with no
  scheme, against the question; labelled "Judged, not checked" (STU-18);
- `not_marked`: nothing could decide it, so it counts for no marks either way.

A sitting is a mock sitting (`exams` row, active time, pauses, interruptions, the tutor lock); its report has the
mock's shape, so marks lost by topic and "Practise this" work as for a mock, and its weak KCs feed the plan as a mock
does. It never changes mastery: no attempt, verdict or exposure event is written for an imported question.

The parser reads the conventions WAEC, NECO and Cambridge papers share: a question starts at its number on a new line
(1, 2, 3 ... in order), parts are `(a)`, `(b)` and sub-parts `(i)`, `(ii)`; marks are `[n]` or `(n marks)` at the end
of a part; choices are lines `A ...` to `D ...` (or `A. ... B. ...` on one line). A mark scheme line starts with the
question reference (`2(a)(i)`, `2a`, `2`) and gives the answer, then any mark points `M1 ...`, `A1 ...`, `B1 ...`;
an objective key may list several `1 C 2 A 3 D` on one line.
"""

from __future__ import annotations

import copy
import json
import re
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from decimal import Decimal, InvalidOperation
from typing import Any

from agent_core.contracts import errors
from agent_core.engine.assessment.errors import Refused
from agent_core.engine.assessment.items import present, validate_answer
from agent_core.engine.learn import SUBJECT_CODES
from agent_core.engine.study_topics import matching_topics
from agent_core.store.assessment import dump
from agent_core.store.ids import new_ulid
from agent_core.store.sqlite import transaction

ROMAN = r"(?:viii|vii|vi|iv|ix|v|iii|ii|i|x)"
Q_START = re.compile(r"^\s*(\d{1,3})\s*[.)]?(?:\s+(.*\S))?\s*$")
PART = re.compile(r"^\s*\(([a-h])\)\s*(.*)$")
SUB = re.compile(rf"^\s*\(({ROMAN})\)\s*(.*)$")
MARKS = re.compile(r"\s*(?:\[\s*(\d{1,2})\s*(?:marks?)?\s*\]|\(\s*(\d{1,2})\s*marks?\s*\))\s*$", re.I)
TOTAL = re.compile(r"\[\s*total\s*:?\s*\d{1,3}\s*\]", re.I)
OPTION = re.compile(r"^\s*\(?([A-E])\s*[.):]?\s+(\S.*)$")
INLINE_OPTION = re.compile(r"(?:^|\s)\(?([A-E])[.)]\s+")
FIGURE = re.compile(r"\b(Fig(?:ure)?\.?\s*\d+(?:\.\d+)?|(?:the |this )?(?:diagram|graph))\b", re.I)
CALCULATOR_YES = re.compile(r"calculators?\s+(?:may|can|are allowed to)\s+be\s+used|may use (?:an? )?(?:electronic |"
                            r"scientific )?calculator", re.I)  # fmt: skip
CALCULATOR_NO = re.compile(r"calculators?\s+(?:may|must|are)\s+not|not\s+(?:use|allowed)\s+(?:an?\s+)?calculator", re.I)
NOISE = re.compile(r"turn over|©|ucles|blank page|^\s*page\s+\d+|^\s*\[?\s*total", re.I)
CALC_VERBS = re.compile(r"\b(calculate|work out|evaluate|find the value|determine the value|how many|how much)\b", re.I)
REF = re.compile(rf"^\s*(\d{{1,3}})\s*(?:\(([a-h])\)|([a-h])(?![a-z]))?\s*(?:\(({ROMAN})\)|({ROMAN})(?![a-z]))?"
                 r"\s*[.:)\-]?\s+(\S.*)$")  # fmt: skip
KEY_PAIR = re.compile(r"(\d{1,3})\s*[.)]?\s*([A-E])\b")
POINT = re.compile(r"\b([MAB])(\d)\b")
NUMBER = re.compile(
    r"^(?:[a-zA-Z]\s*=\s*)?([-+\u2212]?\d[\d,]*(?:\.\d+)?)(?:\s*(?:[x\u00d7]\s*10\s*\^?\s*([-+\u2212]?\d+)|[eE]([-+]?\d+)))?"
    r"\s*([A-Za-z\u00b5\u03a9\u00b0%/][A-Za-z0-9\u00b5\u03a9\u00b0%/\u00b7^\-\s]{0,15})?$"
)
EXPRESSION = re.compile(r"^[A-Za-z0-9\s+\-*/^().=\u2212\u00d7\\{}_]+$")


@dataclass
class Leaf:
    """One answerable part of a question as printed: its label, its words and its marks."""

    label: str
    lines: list[str] = field(default_factory=list)
    marks: int | None = None
    options: list[tuple[str, str]] = field(default_factory=list)


@dataclass
class Question:
    number: int
    pages: list[int] = field(default_factory=list)
    stem: list[str] = field(default_factory=list)
    leaves: list[Leaf] = field(default_factory=list)


@dataclass
class SchemeEntry:
    answer: str
    points: list[dict[str, Any]]
    marks: int | None


def _lines(chunks: Sequence[Mapping[str, Any]]) -> list[tuple[int | None, str]]:
    out: list[tuple[int | None, str]] = []
    for chunk in chunks:
        locator = chunk.get("locator") or {}
        page = locator.get("page") if locator.get("kind") == "page" else None
        out.extend((page, line.rstrip()) for line in str(chunk.get("text", "")).splitlines())
    return out


def _split_options(leaf: Leaf) -> None:
    """Choices printed as the leaf's last lines `A ...` to `D ...`, or on one line `A. ... B. ... C. ...`."""
    run: list[tuple[str, str]] = []
    cut = len(leaf.lines)
    for index in range(len(leaf.lines) - 1, -1, -1):
        match = OPTION.match(leaf.lines[index])
        if not match:
            break
        run.insert(0, (match[1], match[2].strip()))
        cut = index
    letters = [letter for letter, _ in run]
    if len(run) >= 3 and letters == list("ABCDE"[: len(run)]):
        leaf.options, leaf.lines = run, leaf.lines[:cut]
        return
    for index, line in enumerate(leaf.lines):
        marks = list(INLINE_OPTION.finditer(line))
        if len(marks) >= 3 and [m[1] for m in marks] == list("ABCDE"[: len(marks)]):
            texts = [line[m.end() : (marks[i + 1].start() if i + 1 < len(marks) else len(line))].strip()
                     for i, m in enumerate(marks)]  # fmt: skip
            if all(texts):
                leaf.options = [(m[1], t) for m, t in zip(marks, texts, strict=True)]
                head = line[: marks[0].start()].strip()
                leaf.lines = [*leaf.lines[:index], *([head] if head else []), *leaf.lines[index + 1 :]]
                return


def _take_marks(leaf: Leaf) -> None:
    for index in range(len(leaf.lines) - 1, -1, -1):
        line = TOTAL.sub("", leaf.lines[index]).rstrip()
        match = MARKS.search(line)
        if match:
            leaf.marks = int(match[1] or match[2])
            leaf.lines[index] = line[: match.start()].rstrip()
            return
        if line.strip():
            return


def parse_paper(chunks: Sequence[Mapping[str, Any]]) -> tuple[list[Question], bool]:
    """The paper's questions in order, and whether it allows a calculator."""
    lines = _lines(chunks)
    text = "\n".join(line for _, line in lines)
    calculator = bool(CALCULATOR_YES.search(text)) and not CALCULATOR_NO.search(text)
    questions: list[Question] = []
    current: Question | None = None
    letter: str | None = None
    expected = 1

    def leaf(label: str, first: str) -> None:
        assert current is not None
        current.leaves.append(Leaf(label, [first] if first else []))

    for index, (page, line) in enumerate(lines):
        if not line.strip() or (NOISE.search(line) and not PART.match(line)):
            continue
        start = Q_START.match(line)
        if start and int(start[1]) == expected:
            rest = (start[2] or "").strip()
            following = next((t for _, t in lines[index + 1 :] if t.strip()), "")
            if rest or PART.match(following):
                current = Question(expected, [page] if page else [])
                questions.append(current)
                expected += 1
                letter = None
                part = PART.match(rest)
                if part:
                    letter = part[1]
                    sub = SUB.match(part[2])
                    if sub:
                        leaf(f"{current.number}({letter})({sub[1]})", sub[2].strip())
                    else:
                        leaf(f"{current.number}({letter})", part[2].strip())
                elif rest:
                    current.stem.append(rest)
                continue
        if current is None:
            continue
        if page and page not in current.pages:
            current.pages.append(page)
        part = PART.match(line)
        sub = SUB.match(line)
        if part:
            letter = part[1]
            inner = SUB.match(part[2])
            if inner:
                leaf(f"{current.number}({letter})({inner[1]})", inner[2].strip())
            else:
                leaf(f"{current.number}({letter})", part[2].strip())
        elif sub and letter is not None:
            parent = f"{current.number}({letter})"
            if current.leaves and current.leaves[-1].label == parent and current.leaves[-1].marks is None:
                # The part's own words lead into its sub-parts: they move to the first sub-part.
                lead = current.leaves.pop()
                current.leaves.append(Leaf(f"{parent}({sub[1]})", [*lead.lines, sub[2].strip()]))
            else:
                leaf(f"{parent}({sub[1]})", sub[2].strip())
        elif current.leaves:
            current.leaves[-1].lines.append(line.strip())
        else:
            current.stem.append(line.strip())
    for question in questions:
        if not question.leaves:
            question.leaves.append(Leaf(str(question.number), question.stem))
            question.stem = []
        for item in question.leaves:
            _take_marks(item)
            _split_options(item)
        question.leaves = [x for x in question.leaves if x.lines or x.options]
    return [q for q in questions if q.leaves], calculator


def _ref(number: str, letter: str | None, roman: str | None) -> str:
    return number + (f"({letter})" if letter else "") + (f"({roman})" if roman else "")


def parse_scheme(chunks: Sequence[Mapping[str, Any]]) -> dict[str, SchemeEntry]:
    """Mark-scheme entries by question reference (`2(a)(i)`)."""
    out: dict[str, SchemeEntry] = {}
    last: str | None = None
    for _, line in _lines(chunks):
        if not line.strip():
            continue
        pairs = KEY_PAIR.findall(line)
        if len(pairs) >= 2 and not KEY_PAIR.sub("", line).strip(" ;,.|\t"):
            for number, choice in pairs:
                out[number] = SchemeEntry(choice, [], 1)
            last = None
            continue
        match = REF.match(line)
        if match:
            letter = match[2] or match[3]
            roman = match[4] or (match[5] if letter else None)
            if match[5] and not letter:
                rest = (match[5] + " " + match[6]).strip()
            else:
                rest = match[6]
            last = _ref(match[1], letter, roman)
            out[last] = _entry(rest)
        elif last is not None and POINT.search(line):
            extra = _entry(line)
            entry = out[last]
            entry.points.extend(extra.points)
            if entry.points:
                entry.marks = sum(p["marks"] for p in entry.points)
    return out


def _entry(rest: str) -> SchemeEntry:
    rest = TOTAL.sub("", rest).strip()
    marks_match = MARKS.search(rest)
    marks = int(marks_match[1] or marks_match[2]) if marks_match else None
    if marks_match:
        rest = rest[: marks_match.start()].strip()
    found = list(POINT.finditer(rest))
    points: list[dict[str, Any]] = []
    for index, m in enumerate(found):
        end = found[index + 1].start() if index + 1 < len(found) else len(rest)
        points.append({"type": m[1], "marks": int(m[2]), "text": rest[m.end() : end].strip(" ;,:")})
    answer = (rest[: found[0].start()] if found else rest).strip(" ;,:")
    if not answer and points:
        final = next((p for p in reversed(points) if p["type"] in ("A", "B") and p["text"]), None)
        answer = final["text"] if final else ""
    if points:
        marks = sum(p["marks"] for p in points)
    return SchemeEntry(answer, points, marks)


def _decimal(text: str, exponent: str | None = None) -> Decimal | None:
    try:
        value = Decimal(text.replace(",", "").replace("\u2212", "-"))
    except InvalidOperation:
        return None
    if exponent:
        value = value.scaleb(int(exponent.replace("\u2212", "-")))
    return value


def numeric_key(answer: str) -> dict[str, Any] | None:
    match = NUMBER.match(answer.strip())
    if not match:
        return None
    value = _decimal(match[1], match[2] or match[3])
    if value is None:
        return None
    unit = (match[4] or "").strip() or None
    if unit and (len(unit) > 12 or len(unit.split()) > 2):
        return None
    shown = match[1].replace(",", "").replace("\u2212", "-")
    if match[2] or match[3]:
        shown = format(value, "f")
    return {"value": shown, "unit": unit}


def expression_key(answer: str) -> dict[str, Any] | None:
    text = answer.strip()
    if not text or not EXPRESSION.match(text) or not re.search(r"[A-Za-z]", text):
        return None
    if any(len(word) > 3 for word in re.findall(r"[A-Za-z]+", text) if word not in ("sqrt", "frac", "sin", "cos")):
        return None
    variables = sorted({v for v in re.findall(r"(?<![A-Za-z\\])([A-Za-z])(?![A-Za-z])", text)})
    if not variables or not re.search(r"[+\-*/^=]|\d[A-Za-z]", text):
        return None
    if "=" in text:
        left, _, right = text.partition("=")
        if re.fullmatch(r"\s*[A-Za-z]\s*", left):
            text = right.strip()
    return {"expression": text, "variables": variables[:8]}


def matches_precision(student: Decimal, key: str) -> bool:
    """Whether the student's value rounds to the key at the key's own precision (a scheme's answer is given to the
    places it expects, so 12.54 earns a key of 12.5; 12.6 does not)."""
    target = _decimal(key)
    if target is None:
        return False
    exponent = target.as_tuple().exponent
    places = -exponent if isinstance(exponent, int) else 0
    step = Decimal(1).scaleb(-places)
    return abs(student - target) <= step / 2


class PastPaperCommands:
    """Import, list and sit imported past papers; mixed into `AssessmentDesk`."""

    # --- import ------------------------------------------------------------------------------------------------------

    def _links(self: Any, text: str, syllabus: str) -> list[str]:
        """KCs of the installed syllabus whose topic titles the question's own words name, the most specific first."""
        found = [m for m in matching_topics(self.content, text)["items"]
                 if str(m["node_id"]).startswith(syllabus + ".")]  # fmt: skip
        found.sort(key=lambda m: (-len(str(m["title"])), str(m["node_id"])))
        kcs: list[str] = []
        for match in found:
            for kc in match["kc_ids"]:
                if kc in self.content.kcs and kc not in kcs:
                    kcs.append(kc)
        return kcs[: self.settings.past_paper_question_kcs_max]

    def import_past_paper(
        self: Any,
        body: Mapping[str, Any],
        paper_chunks: Sequence[Mapping[str, Any]],
        scheme_chunks: Sequence[Mapping[str, Any]] | None,
        level: str = "o",
    ) -> dict[str, Any]:
        self.guard_tutor()
        subject = body["subject"]
        if subject not in SUBJECT_CODES:
            raise Refused("invalid_subject")
        count = self.learner.conn.execute("SELECT count(*) FROM past_papers").fetchone()[0]
        if count >= self.settings.past_papers_max:
            raise Refused("too_many_past_papers")
        questions, calculator = parse_paper(paper_chunks)
        if not questions:
            raise Refused("no_questions_found")
        scheme = parse_scheme(scheme_chunks) if scheme_chunks else {}
        syllabus = body.get("syllabus") or f"cur.{SUBJECT_CODES[subject]}.{level.lower()}"
        if syllabus.split(".")[1] != SUBJECT_CODES[subject]:
            raise Refused("syllabus_subject_mismatch")
        installed = any(k.startswith("kc." + ".".join(syllabus.split(".")[1:3]) + ".") for k in self.content.kcs)
        paper_id = "ppr_" + new_ulid(self.clock_ms())
        source = dict(body["paper"])
        items: dict[str, dict[str, Any]] = {}
        keys: dict[str, dict[str, Any]] = {}
        listed: list[dict[str, Any]] = []
        leaves_total = 0
        for question in questions:
            qid = f"q{question.number}"
            words = " ".join([*question.stem, *(line for x in question.leaves for line in x.lines)])
            kc_ids = self._links(words, syllabus) if installed else []
            parts: list[dict[str, Any]] = []
            part_keys: dict[str, dict[str, Any]] = {}
            for index, printed in enumerate(question.leaves, 1):
                leaves_total += 1
                if leaves_total > self.settings.past_paper_questions_max:
                    raise Refused("paper_too_long")
                part_id = f"p{index}"
                entry = scheme.get(printed.label) or (
                    scheme.get(str(question.number)) if len(question.leaves) == 1 else None
                )
                marks = printed.marks or (entry.marks if entry and entry.marks else None) or 1
                words_of = "\n".join(printed.lines).strip()
                own = printed.label[len(str(question.number)) :]  # "(a)(i)": the part's number as printed
                prompt = f"{own} {words_of}".strip() if own else (words_of or printed.label)
                answer: dict[str, Any]
                key: dict[str, Any] = {"label": printed.label, "points": entry.points if entry else [],
                                       "scheme": entry.answer if entry else None}  # fmt: skip
                if printed.options:
                    answer = {"kind": "mcq", "options": [{"id": o, "text": t[:500]} for o, t in printed.options]}
                    choice = (entry.answer.strip().split() or [""])[0].strip(".()") if entry else ""
                    if choice in {o for o, _ in printed.options}:
                        key.update(method="key", option=choice)
                    else:
                        key["method"] = "judged"
                elif entry and (number := numeric_key(entry.answer)):
                    answer = {"kind": "numeric", "unit": number["unit"],
                              "unit_policy": "exact" if number["unit"] else "none"}  # fmt: skip
                    key.update(method="checked", value=number["value"], unit=number["unit"])
                elif entry and (expression := expression_key(entry.answer)):
                    answer = {"kind": "algebraic", "variables": expression["variables"]}
                    key.update(method="checked", expression=expression["expression"])
                elif not entry and CALC_VERBS.search(prompt) and not printed.options:
                    answer = {"kind": "numeric", "unit": None, "unit_policy": "none"}
                    key["method"] = "judged"
                else:
                    answer = {"kind": "short_text"}
                    key["method"] = "judged"
                parts.append({"id": part_id, "prompt": prompt[:8000], "marks": int(marks), "answer": answer,
                              "number_label": printed.label})  # fmt: skip
                part_keys[part_id] = key
                listed.append({"question_id": qid, "number_label": printed.label[:24], "marks": int(marks),
                               "marking": key["method"],
                               "topic_ids": sorted({self._topic_of(k) for k in kc_ids})[:10]})  # fmt: skip
            kind = parts[0]["answer"]["kind"] if len(parts) == 1 else "structured"
            figures = []
            if FIGURE.search(words):
                captions = sorted({m[1].strip() for m in FIGURE.finditer(words)})
                figures = [{"source_id": source["source_id"], "revision": source["revision"],
                            "page": question.pages[0] if question.pages else None,
                            "caption": ", ".join(captions)[:200]}]  # fmt: skip
            item = {
                "id": f"{paper_id}.{qid}",
                "type": kind,
                "stem": "\n".join(question.stem)[:8000],
                "kc_ids": kc_ids,
                "parts": parts,
                "number_label": str(question.number),
                "figures": figures,
                "imported": True,
            }
            shown = present(item, self.content, self.settings)
            if errors("api/commands#/$defs/PracticeItem", shown):
                raise Refused("unreadable_question")
            items[qid] = item
            keys[qid] = part_keys
        record = {
            "paper_id": paper_id,
            "title": body["title"],
            "subject": subject,
            "duration_minutes": int(body["duration_minutes"]),
            "calculator": calculator,
            "has_mark_scheme": bool(scheme_chunks),
            "syllabus": syllabus if installed else None,
            "source": source,
            "mark_scheme": dict(body["mark_scheme"]) if body.get("mark_scheme") else None,
            "questions": [{"question_id": qid, "marks": sum(p["marks"] for p in items[qid]["parts"])} for qid in items],
            "items": items,
            "keys": keys,
            "listed": listed,
            "imported_ts": self.clock_ms(),
        }
        with transaction(self.learner.conn):
            self.learner.conn.execute(
                "INSERT INTO past_papers (paper_id,subject,title,body,created_at) VALUES (?,?,?,?,?)",
                (paper_id, subject, body["title"], dump(record), record["imported_ts"]),
            )
        return dict(self.past_paper(paper_id))

    # --- reading -----------------------------------------------------------------------------------------------------

    def _past_record(self: Any, paper_id: str) -> dict[str, Any]:
        row = self.learner.conn.execute("SELECT body FROM past_papers WHERE paper_id=?", (paper_id,)).fetchone()
        if row is None:
            raise Refused("not_found")
        return dict(json.loads(row[0]))

    @staticmethod
    def _summary(record: Mapping[str, Any]) -> dict[str, Any]:
        methods = [q["marking"] for q in record["listed"]]
        return {
            "paper_id": record["paper_id"],
            "title": record["title"],
            "subject": record["subject"],
            "duration_minutes": record["duration_minutes"],
            "total_marks": max(1, sum(q["marks"] for q in record["questions"])),
            "questions": len(record["questions"]),
            "has_mark_scheme": record["has_mark_scheme"],
            "syllabus": record["syllabus"],
            "linked": any(item["kc_ids"] for item in record["items"].values()),
            "calculator": record["calculator"],
            "imported_ts": record["imported_ts"],
            "marking": {k: methods.count(k) for k in ("checked", "key", "judged")},
        }

    def past_paper(self: Any, paper_id: str) -> dict[str, Any]:
        record = self._past_record(paper_id)
        return {**self._summary(record), "questions_found": record["listed"][:200]}

    def list_past_papers(self: Any) -> dict[str, Any]:
        rows = self.learner.conn.execute("SELECT body FROM past_papers ORDER BY created_at DESC, paper_id DESC")
        return {"papers": [self._summary(json.loads(r[0])) for r in rows][:200]}

    def delete_past_paper(self: Any, paper_id: str) -> dict[str, Any]:
        self._past_record(paper_id)
        if self.mock_active():
            raise Refused("tutor_locked")
        with transaction(self.learner.conn):
            self.learner.conn.execute("DELETE FROM past_papers WHERE paper_id=?", (paper_id,))
        return {"paper_id": paper_id, "deleted": True}

    # --- sitting -----------------------------------------------------------------------------------------------------

    def create_past_paper_sitting(self: Any, paper_id: str) -> dict[str, Any]:
        """A timed sitting of an imported paper: a mock sitting in every way but its marking (ASM-27)."""
        self.guard_tutor()
        record = self._past_record(paper_id)
        paper = {
            "id": paper_id,
            "subject": SUBJECT_CODES[record["subject"]],
            "title": record["title"],
            "duration_minutes": record["duration_minutes"],
            "total_marks": max(1, sum(q["marks"] for q in record["questions"])),
            "calculator": record["calculator"],
            "imported": True,
            "sections": [
                {
                    "section_id": "s1",
                    "title": record["title"],
                    "marks": sum(q["marks"] for q in record["questions"]),
                    "questions": [
                        {"question_id": q["question_id"], "item_id": record["items"][q["question_id"]]["id"],
                         "marks": q["marks"], "kc_ids": list(record["items"][q["question_id"]]["kc_ids"])}
                        for q in record["questions"]
                    ],
                }
            ],
        }  # fmt: skip
        state = self._new(
            "mock",
            record["subject"],
            paper=paper,
            items=copy.deepcopy(record["items"]),
            keys=copy.deepcopy(record["keys"]),
            report=None,
            signals=[],
            section_ms={},
            pause_started=None,
            active_section="s1",
        )
        duration = int(record["duration_minutes"] * 60_000)
        extra = self._extra_time()

        def insert() -> None:
            self.learner.conn.execute(
                "INSERT INTO exams (exam_id,paper_ref,status,duration_ms,extra_time_percent) VALUES (?,?,'ready',?,?)",
                (state["id"], paper_id, duration, extra),
            )

        state["state"] = "ready"
        self._commit(state, extra=insert)
        return dict(self.mock("exm_" + state["id"]))

    def figure(self: Any, identity: str, question_id: str) -> dict[str, Any]:
        """The page holding a question's diagram, for a sitting of an imported paper (ASM-27)."""
        state = self._get(identity, "mock")
        item = state["items"].get(question_id)
        figures = (item or {}).get("figures") or []
        if not state["paper"].get("imported") or not figures or figures[0].get("page") is None:
            raise Refused("not_found")
        return dict(figures[0])

    # --- marking -----------------------------------------------------------------------------------------------------

    async def _numeric_check(self: Any, value: str, unit: str | None, key: Mapping[str, Any]
                             ) -> tuple[bool | None, list[str]]:  # fmt: skip
        """A numeric answer against the scheme's value, by tools: the unit converted to the key's, then
        `math_check_equivalence`, then exact rounding to the key's precision."""
        ids: list[str] = []
        target, want = str(key["value"]), key.get("unit")
        if want and unit and unit.strip() and unit.strip() != want:
            converted = await self._convert(f"{value} {unit.strip()}", want)
            if converted is None:
                return None, ids
            value = format(converted.value, "f")
            ids.append(converted.check_id)
        student = _decimal(value.strip())
        equal, check_id = await self._method_equivalent(value.strip(), target)
        if check_id:
            ids.append(check_id)
        if equal is True:
            return True, ids
        if student is None:
            return (False if equal is False else None), ids
        return matches_precision(student, target), ids

    async def _judge(self: Any, rule: Mapping[str, Any], lines: Sequence[str]) -> bool | None:
        if self.judge_method is None or not [line for line in lines if line.strip()]:
            return None
        decision: bool | None = await self.judge_method(dict(rule), list(lines))
        return decision

    async def _mark_imported_part(
        self: Any, item: Mapping[str, Any], part: Mapping[str, Any], key: Mapping[str, Any], given: Any,
        working: list[str],
    ) -> tuple[list[dict[str, Any]], str, list[str]]:  # fmt: skip
        """One part's criterion rows, its method and the tool checks behind it."""
        marks = int(part["marks"])
        kind = part["answer"]["kind"]
        method = str(key.get("method", "judged"))
        scheme = (key.get("scheme") or part["prompt"] or "")[:300]
        points = list(key.get("points") or [])

        def row(cid: str, mark_type: str, awarded: int, available: int, how: str, text: str,
                resolved: bool = True) -> dict[str, Any]:  # fmt: skip
            return {"criterion_id": cid, "mark_type": mark_type, "awarded": awarded, "available": available,
                    "resolved": resolved, "scheme": text[:300], "method": how,
                    "notice_key": None if resolved else "assessment.not_marked_automatically"}  # fmt: skip

        def unmarked() -> list[dict[str, Any]]:
            return [row(part["id"], "B", 0, 0, "not_marked", scheme, resolved=False)]

        if given is None:  # unanswered: no marks, however it would have been marked
            return [row(part["id"], "B", 0, marks, method, scheme)], method, []
        text = (
            given.get("text") if kind == "short_text"
            else given.get("latex") if kind == "algebraic"
            else (str(given.get("value", "")) + (" " + given["unit"] if given.get("unit") else "")).strip()
            if kind == "numeric"
            else next((t["text"] for t in part["answer"].get("options", []) if t["id"] == given.get("option_id")), "")
        )  # fmt: skip
        rule = {"criterion_id": part["id"], "problem": item.get("stem", ""), "prompt": part["prompt"]}
        if method == "key":
            right = given.get("option_id") == key.get("option")
            return [row(part["id"], "B", marks if right else 0, marks, "key", scheme)], "key", []
        if method == "checked":
            if kind == "numeric":
                right, ids = await self._numeric_check(str(given.get("value", "")), given.get("unit"), key)
            else:
                variables = list(part["answer"].get("variables") or [])
                right, check_id = await self._method_equivalent(str(given.get("latex", "")), key["expression"],
                                                                variables)  # fmt: skip
                ids = [check_id] if check_id else []
            if right is None:
                return unmarked(), "not_marked", ids
            if right or not points:
                return [row(part["id"], "A", marks if right else 0, marks, "checked", scheme)], "checked", ids
            # A wrong final answer loses its answer marks; method marks are judged on the working shown.
            rows, judged = [], False
            for index, point in enumerate(points):
                cid = f"{part['id']}.{index + 1}"
                if point["type"] != "M":
                    rows.append(row(cid, point["type"], 0, point["marks"], "checked", point["text"] or scheme))
                    continue
                decision = await self._judge({**rule, "criterion_id": cid, "pass_if": point["text"]}, working)
                judged = True
                if decision is None:
                    rows.append(row(cid, "M", 0, 0, "not_marked", point["text"], resolved=False))
                else:
                    rows.append(row(cid, "M", point["marks"] if decision else 0, point["marks"], "judged",
                                    point["text"]))  # fmt: skip
            return rows, "judged" if judged else "checked", ids
        # Judged: against the scheme's points, else its answer, else the question itself.
        lines = [text, *working] if text else working
        if points:
            rows = []
            for index, point in enumerate(points):
                cid = f"{part['id']}.{index + 1}"
                decision = await self._judge({**rule, "criterion_id": cid, "pass_if": point["text"]}, lines)
                if decision is None:
                    rows.append(row(cid, point["type"], 0, 0, "not_marked", point["text"], resolved=False))
                else:
                    rows.append(row(cid, point["type"], point["marks"] if decision else 0, point["marks"], "judged",
                                    point["text"]))  # fmt: skip
            methods = {r["method"] for r in rows}
            return rows, "judged" if "judged" in methods else "not_marked", []
        criterion = (f"Agrees with the mark scheme's answer: {key['scheme']}" if key.get("scheme")
                     else "Correctly and fully answers the question")  # fmt: skip
        decision = await self._judge({**rule, "pass_if": criterion}, lines)
        if decision is None:
            return unmarked(), "not_marked", []
        return [row(part["id"], "B", marks if decision else 0, marks, "judged", scheme)], "judged", []

    async def _score_imported(
        self: Any, state: Mapping[str, Any], qid: str, answer: Any
    ) -> tuple[int, int, list[dict[str, Any]], list[dict[str, Any]]]:
        item = state["items"][qid]
        keys = state["keys"][qid]
        if answer is not None:
            try:
                answer = validate_answer(item, answer)
            except ValueError as error:
                raise Refused(str(error)) from None
        given = {a["part_id"]: a for a in (answer or {}).get("parts", [])}
        rows: list[dict[str, Any]] = []
        marking: list[dict[str, Any]] = []
        for part in item["parts"]:
            working = [w["latex"] for w in (answer or {}).get("working", []) if w["part_id"] == part["id"]]
            part_rows, method, ids = await self._mark_imported_part(
                item, part, keys[part["id"]], given.get(part["id"]) if answer is not None else None, working
            )
            rows.extend(part_rows)
            marking.append({"question_id": qid, "part_id": part["id"], "method": method,
                            "check_ids": [i for i in ids if i][:20]})  # fmt: skip
        return sum(r["awarded"] for r in rows), sum(r["available"] for r in rows), rows, marking
