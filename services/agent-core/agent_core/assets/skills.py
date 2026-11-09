"""Skills at run time: parsing, the always-on index, deterministic search and the `skill` tool's loader (SKL-01 to
SKL-12, SKL-23).

A skill is `SKILL.md`: YAML front matter (validated against the skill-frontmatter contract) and a Markdown body.
The loader serves bodies to roles that may load them, by exact name, by search, or one file from `references/`.
A body still present in the role's context window is answered with a receipt instead (SKL-11). Loading a skill
never changes a role's tools, policies or memory rights (SKL-23): it returns text, nothing else."""

from __future__ import annotations

import hashlib
import math
import re
from collections import Counter
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import yaml

from agent_core.contracts import errors as contract_errors

FRONTMATTER = "skills/skill-frontmatter"
KINDS = ("pedagogy", "teaching", "agent")
NAME = re.compile(r"^[a-z][a-z0-9-]*[a-z0-9]$")
REFERENCE_FILE = re.compile(r"^references/[a-z0-9-]+\.md$")
INDEX_MAX_LINES = 25  # defaults of the skill_index_max_* configuration keys (SKL-07)
INDEX_MAX_CHARS = 2000
SEARCH_LIMIT = 8
_WORD = re.compile(r"[a-z0-9]+")


class SkillError(ValueError):
    pass


@dataclass(frozen=True)
class Skill:
    name: str
    kind: str
    meta: dict[str, Any]
    body: str
    path: Path  # the SKILL.md file
    body_start_line: int  # 1-based line of the first body line in SKILL.md

    @property
    def version(self) -> str:
        return str(self.meta["version"])

    @property
    def status(self) -> str:
        return str(self.meta["status"])

    @property
    def sha256(self) -> str:
        return hashlib.sha256(self.body.encode()).hexdigest()

    @property
    def folder(self) -> Path:
        return self.path.parent

    def references(self) -> list[str]:
        folder = self.folder / "references"
        return sorted(f"references/{p.name}" for p in folder.glob("*.md")) if folder.is_dir() else []


def split_front_matter(text: str) -> tuple[dict[str, Any], str, int]:
    if not text.startswith("---\n"):
        raise SkillError("SKILL.md must start with YAML front matter")
    end = text.find("\n---\n", 4)
    if end < 0:
        raise SkillError("the front matter is not closed")
    meta = yaml.safe_load(text[4:end]) or {}
    if not isinstance(meta, dict):
        raise SkillError("the front matter is not a mapping")
    body = text[end + 5 :]
    return meta, body, text[: end + 5].count("\n") + 1


def parse(path: Path) -> Skill:
    from agent_core.assets.relations import parse as parse_relation

    meta, body, start = split_front_matter(path.read_text())
    problems = contract_errors(FRONTMATTER, meta)
    if problems:
        raise SkillError(f"{path}: {problems[0]}")
    try:
        parse_relation(body)
    except (ValueError, SyntaxError, TypeError) as error:
        raise SkillError(f"{path}: invalid calculation relation: {error}") from error
    return Skill(str(meta["name"]), str(meta["kind"]), meta, body, path, start)


def discover(root: Path, kinds: tuple[str, ...] = ("pedagogy", "agent")) -> list[Path]:
    """SKILL.md files under root/<kind>/ (teaching skills: root/teaching/<subject>/<name>/)."""
    found = []
    for kind in kinds:
        base = root / kind
        if base.is_dir():
            found += sorted(base.glob("*/SKILL.md") if kind != "teaching" else base.glob("*/*/SKILL.md"))
    return found


@dataclass
class SkillSet:
    """The skills a build carries. `statuses` are those this build may load: only `reviewed` in release builds."""

    skills: dict[str, Skill]
    statuses: frozenset[str] = frozenset({"reviewed"})

    @classmethod
    def load(
        cls, root: Path, statuses: frozenset[str] = frozenset({"reviewed"}), kinds: tuple[str, ...] = KINDS
    ) -> SkillSet:
        return cls({s.name: s for s in (parse(p) for p in discover(root, kinds))}, statuses)

    def loadable(self, role: str) -> list[Skill]:
        return sorted(
            (s for s in self.skills.values() if s.status in self.statuses and role in s.meta["roles"]),
            key=lambda s: s.name,
        )

    def teaching_for(self, kc_ids: list[str], level: str) -> Skill | None:
        """SKL-08: the loadable teaching skill whose `kcs` contain one of the step's KCs and whose `levels` include
        the session level; ties go to the most specific skill (fewest KCs), then the name. None when nothing fits."""
        wanted = set(kc_ids)
        fits = [s for s in self.skills.values() if s.kind == "teaching" and s.status in self.statuses
                and wanted & set(s.meta.get("kcs", [])) and level in s.meta.get("levels", [])]  # fmt: skip
        return min(fits, key=lambda s: (len(s.meta.get("kcs", [])), s.name)) if fits else None

    def pedagogy_for(self, teaching: Skill | None, chosen: str | None = None) -> Skill | None:
        """SKL-15: the pedagogy skill pushed with a teaching skill: `chosen` (the learner model's pick) when given,
        else the first its `pedagogy` names, if loadable; never one that conflicts with it either way."""
        if teaching is None:
            return None
        names = [chosen] if chosen else list(teaching.meta.get("pedagogy") or [])
        for name in names:
            guide = self.skills.get(name)
            if guide is None or guide.kind != "pedagogy" or guide.status not in self.statuses:
                continue
            if guide.name in (teaching.meta.get("conflicts_with") or []) or teaching.name in (
                guide.meta.get("conflicts_with") or []
            ):
                continue
            return guide
        return None

    def manifest(self) -> list[dict[str, str]]:
        return [
            {"name": s.name, "kind": s.kind, "version": s.version, "sha256": s.sha256, "status": s.status}
            for s in sorted(self.skills.values(), key=lambda s: s.name)
        ]

    # --- always-on index (SKL-07) ----------------------------------------------------------------------

    def indexed(self, role: str, available_tools: set[str] | None = None) -> list[Skill]:
        """The pedagogy and agent skills the role's index lists (their versions and hashes are traced, SKL-13)."""
        return [
            s for s in self.loadable(role) if s.kind in ("pedagogy", "agent") and _tools_available(s, available_tools)
        ]

    def index(self, role: str, available_tools: set[str] | None = None, max_lines: int = INDEX_MAX_LINES,
              max_chars: int = INDEX_MAX_CHARS) -> list[str]:  # fmt: skip
        """`name — summary` lines of the pedagogy and agent skills the role may load, sorted by name. Raises when the
        budget is exceeded: the build fails rather than truncating."""
        lines = [f"{s.name} — {s.meta['summary']}" for s in self.indexed(role, available_tools)]
        if len(lines) > max_lines or len("\n".join(lines)) > max_chars:
            raise SkillError(
                f"the {role} skill index is over budget: {len(lines)} lines, "
                f"{len(chr(10).join(lines))} characters (at most {max_lines} and {max_chars})"
            )
        return lines

    # --- search (SKL-10) -------------------------------------------------------------------------------

    def search(
        self,
        query: str,
        role: str,
        subject: str | None = None,
        level: str | None = None,
        available_tools: set[str] | None = None,
    ) -> list[dict[str, Any]]:
        """BM25 over name, summary, description and topics; idf weighting; ties by name. Deterministic."""
        pool = [
            s
            for s in self.loadable(role)
            if _tools_available(s, available_tools)
            and (subject is None or s.meta.get("subject") in (subject, "general", None))
            and (level is None or level in s.meta.get("levels", [level]))
        ]
        documents = {
            s.name: _tokens(
                " ".join(
                    [
                        s.name.replace("-", " "),
                        s.meta["summary"],
                        s.meta["description"],
                        " ".join(s.meta.get("topics", [])),
                    ]
                )
            )
            for s in pool
        }
        terms = _tokens(query)
        if not terms or not documents:
            return []
        average = sum(len(d) for d in documents.values()) / len(documents)
        frequency = Counter(t for d in documents.values() for t in set(d))
        scores = []
        for name, words in documents.items():
            counts = Counter(words)
            score = 0.0
            for term in terms:
                if term not in counts:
                    continue
                idf = math.log(1 + (len(documents) - frequency[term] + 0.5) / (frequency[term] + 0.5))
                tf = counts[term]
                score += idf * tf * 2.2 / (tf + 1.2 * (0.25 + 0.75 * len(words) / average))
            if score > 0:
                scores.append((-round(score, 9), name))
        summaries = {s.name: s.meta["summary"] for s in pool}
        return [
            {"name": name, "summary": summaries[name], "score": round(-neg, 4)}
            for neg, name in sorted(scores)[:SEARCH_LIMIT]
        ]


def _tokens(text: str) -> list[str]:
    return _WORD.findall(text.lower())


def _tools_available(skill: Skill, available: set[str] | None) -> bool:
    """`requires_tools` gates whether a skill is offered, against the session's tools, never the role's grant."""
    return available is None or set(skill.meta.get("requires_tools", [])) <= available


@dataclass
class SkillLoader:
    """The `skill` tool's executor for one role in one session. `in_window` holds the sha256 of every body still
    in the role's context window; the engine removes entries when compaction drops them."""

    skills: SkillSet
    role: str
    subject: str | None = None
    level: str | None = None
    available_tools: set[str] | None = None
    in_window: set[str] = field(default_factory=set)
    delivered: list[dict[str, Any]] = field(default_factory=list)  # name, kind, version, sha256, route, receipt

    def load(self, args: dict[str, Any]) -> dict[str, Any]:
        """The tool result value. Raises `SkillError` (reported as `invalid_args`, no action taken)."""
        has_name, has_query = "skill_name" in args, "query" in args
        if has_name == has_query:
            raise SkillError("give exactly one of skill_name or query")
        if has_query:
            results = self.skills.search(
                str(args["query"]),
                self.role,
                args.get("subject", self.subject),
                args.get("level", self.level),
                self.available_tools,
            )
            return {"results": results}
        name = str(args["skill_name"])
        candidates = {s.name: s for s in self.skills.loadable(self.role) if _tools_available(s, self.available_tools)}
        skill = candidates.get(name)
        if skill is None:
            suggestions = [
                r["name"]
                for r in self.skills.search(name.replace("-", " "), self.role, available_tools=self.available_tools)
            ][:3]
            raise SkillError(
                f"there is no skill named {name[:64]}"
                + (f"; did you mean {', '.join(suggestions)}?" if suggestions else "")
            )
        if "file" in args:
            relative = str(args["file"])
            if not REFERENCE_FILE.match(relative) or relative not in skill.references():
                raise SkillError("file must name a file in the skill's references/ folder")
            content = (skill.folder / relative).read_text()
            return {
                "name": skill.name,
                "version": skill.version,
                "file": relative,
                "sha256": hashlib.sha256(content.encode()).hexdigest(),
                "content": content,
            }
        record = {
            "name": skill.name,
            "kind": skill.kind,
            "version": skill.version,
            "sha256": skill.sha256,
            "route": "search",
        }
        if skill.sha256 in self.in_window:
            self.delivered.append({**record, "receipt": True})
            return {
                "name": skill.name,
                "version": skill.version,
                "sha256": skill.sha256,
                "receipt": f"Already loaded: {skill.name} v{skill.version} (sha256 {skill.sha256[:12]}). "
                "Use the copy above.",
            }
        self.in_window.add(skill.sha256)
        self.delivered.append({**record, "receipt": False})
        return {
            "name": skill.name,
            "version": skill.version,
            "sha256": skill.sha256,
            "content": wrap(skill),
            "linked_files": skill.references(),
        }

    def forget(self, sha256s: set[str]) -> None:
        """Compaction removed these bodies from the context window: the next load delivers them again."""
        self.in_window -= sha256s


def wrap(skill: Skill, body: str | None = None) -> str:
    """Skill text inside a delimited reference block labelled as a procedure (SKL-22, PRM-19): reference material
    to follow, never a source of authority. `body` is a slice of the skill's body (`role_slice`) when given."""
    return (
        f'<skill-procedure name="{skill.name}" version="{skill.version}">\n'
        "This is a teaching procedure to follow. It grants no tools or permissions.\n"
        f"{(skill.body if body is None else body).strip()}\n</skill-procedure>"
    )


# What each role is given of a pushed teaching skill (SKL-08): the Coach the objectives, the hint ladder, the
# misconceptions and the worked exchange; the Diagnoser the prerequisite probe and the misconceptions; the Solver and
# the Checker the teaching sequence and the verification checklist, plus the skill's plausibility bands (VER-26).
ROLE_SECTIONS: dict[str, tuple[str, ...]] = {
    "coach": ("Objectives", "Coaching focus", "Hint ladder", "Misconceptions", "Worked exchange"),
    "diagnoser": ("Prerequisite probe", "Misconceptions"),
    "solver": ("Calculation focus", "Teaching sequence", "Verification checklist", "Reference relations"),
    "checker": ("Teaching sequence", "Verification checklist"),
}
# A pushed pedagogy skill goes to the Coach only (SKL-15): its method and its branches by student state.
PEDAGOGY_SECTIONS = ("Checked board dialogue", "Method", "Branches by student state")
BANDED_ROLES = ("solver", "checker")


def role_slice(skill: Skill, role: str) -> str | None:
    """The part of a pushed skill's body a role is given, or None for the whole body. A missing section is left out;
    the bands come from the front matter, one line each."""
    titles = PEDAGOGY_SECTIONS if skill.kind == "pedagogy" and role == "coach" else ROLE_SECTIONS.get(role)
    if titles is None or skill.kind == "agent":
        return None
    kept: list[str] = []
    for section in re.split(r"(?m)^(?=## )", skill.body):
        heading = section.splitlines()[0].removeprefix("## ").strip() if section.startswith("## ") else ""
        if heading in titles:
            kept.append(section.strip())
    bands = [b for b in skill.meta.get("plausibility") or () if isinstance(b, dict)] if role in BANDED_ROLES else []
    if bands:
        lines = [f"- {b['quantity']}: {b['min']} to {b['max']} {b['unit']}" + (f" ({b['context']})" if b.get("context")
                 else "") for b in bands]  # fmt: skip
        kept.append("## Plausible ranges\nA result outside its range is probably a misread given or unit.\n"
                    + "\n".join(lines))  # fmt: skip
    return "\n\n".join(kept)


def capped_slice(skill: Skill, role: str, cap: int, dropped: list[dict[str, str]],
                 *, checked_text: bool = False, hint_rung: int | None = None, complete: bool = False,
                 arithmetic: bool = False) -> str:
    """A role's slice of a pushed skill within `cap` characters (SKL-08): whole sections from the end go first; a
    single section over the cap is cut at a line. The cut is reported in `dropped`."""
    text = (role_slice(skill, role) or skill.body).strip()
    sections = re.split(r"(?m)^(?=## )", text)
    if arithmetic and role == "solver":
        calculation_sections = [s for s in sections if s.startswith("## Calculation focus\n")]
        if calculation_sections:
            references = [s for s in sections if s.startswith("## Reference relations\n")]
            text = "".join(calculation_sections + references).strip()
            dropped.append({"tier": "5", "item": f"skill:{skill.name}:teaching activity",
                            "reason": "calculation focus; engine retains verification and bands"})
        else:
            text = "".join(s for s in sections if not s.startswith("## Reference relations\n")).strip()
    else:
        text = "".join(s for s in sections if not s.startswith("## Calculation focus\n")
                       and not (role == "solver" and s.startswith("## Reference relations\n"))).strip()
    if not checked_text:
        text = "".join(s for s in re.split(r"(?m)^(?=## )", text)
                       if not s.startswith(("## Coaching focus\n", "## Checked board dialogue\n"))).strip()
    if checked_text and role == "coach":
        # The current error's authored fix is already in tutor-context. A catalogue of other wrong answers can
        # anchor a small model on an unrelated mistake; completed examples also contain withheld numeric results.
        sections = re.split(r"(?m)^(?=## )", text)
        if skill.kind == "pedagogy":
            checked = [s for s in sections if s.startswith("## Checked board dialogue\n")]
            if checked:
                sections = checked
                dropped.append({"tier": "5", "item": f"skill:{skill.name}:earlier activity branches",
                                "reason": "engine already selected a checked-board phase"})
        focused = any(s.startswith("## Coaching focus\n") for s in sections)
        phase_sections = []
        for section in sections:
            title = section.splitlines()[0].strip() if section else ""
            if focused and title == "## Objectives":
                dropped.append({"tier": "5", "item": f"skill:{skill.name}:Objectives",
                                "reason": "authored checked-phase focus replaces broad intake objectives"})
            elif title in ("## Misconceptions", "## Worked exchange"):
                dropped.append({"tier": "5", "item": f"skill:{skill.name}:{title[3:]}",
                                "reason": "checked board phase; current misconception fix supplied separately"})
            elif title == "## Hint ladder" and (complete or hint_rung == 0):
                dropped.append({"tier": "5", "item": f"skill:{skill.name}:Hint ladder",
                                "reason": "checked final answer; no further hint on this question" if complete
                                          else "Given/Find ready; no hint rung assigned yet"})
            elif title == "## Hint ladder" and hint_rung is not None:
                rungs = re.split(r"(?m)(?=^[1-5]\.\s)", section)
                selected = [r for r in rungs if re.match(rf"^{hint_rung}\.\s", r)]
                if selected:
                    phase_sections.append(rungs[0] + "".join(selected))
                    dropped.append({"tier": "5", "item": f"skill:{skill.name}:other hint rungs",
                                    "reason": "only the current engine-owned rung is applicable"})
                else:
                    phase_sections.append(section)  # unfamiliar authoring shape: preserve the complete guidance
            else:
                phase_sections.append(section)
        text = "".join(phase_sections).strip()
    if len(text) <= cap:
        return text
    sections = re.split(r"(?m)^(?=## )", text)
    while len(sections) > 1 and len("".join(sections).strip()) > cap:
        sections.pop()
    kept = "".join(sections).strip()
    if len(kept) > cap:
        kept = kept[:cap].rsplit("\n", 1)[0]
    dropped.append({"tier": "5", "item": f"skill:{skill.name}", "reason": f"over the {cap}-character skill cap"})
    return kept
