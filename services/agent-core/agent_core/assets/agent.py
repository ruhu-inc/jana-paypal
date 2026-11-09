"""The agent bundle's versioned prompt files: soul, identity, role manifests, guidance blocks and family overlays
(PRM-01, PRM-04, PRM-05, PRM-07, PRM-08).

Files are read once and validated against their contracts; everything downstream (the prompt builder, delegation,
tool grants) works from these parsed records, never from the files again."""

from __future__ import annotations

import hashlib
import re
from dataclasses import dataclass, field
from pathlib import Path
from types import MappingProxyType
from typing import Any

import yaml

from agent_core.assets.glossary import Glossary
from agent_core.assets.skills import SkillSet, split_front_matter
from agent_core.contracts import errors as contract_errors

ROLE_SECTIONS = ("Role", "Method", "Untrusted input", "Non-goals", "Output")
SOUL_SECTIONS = ("Values", "Voice", "Teaching stance", "Boundaries")
ROLE_BODY_MAX = 3000
SOUL_MAX = 2000
SOUL_LINES = (15, 30)  # PRM-01: the soul is 15 to 30 lines
# PRM-03: the identity file holds these keys and nothing else; only `tutor_name` enters a prompt (as {{TUTOR_NAME}}).
IDENTITY_KEYS = ("tutor_name", "avatar_id", "locale", "reading_level")
# DSN-26, D103: the six tutor characters of TABLE DSN-26a, Jana first (the default, D47).
TUTOR_CHARACTERS = ("avatar.jana", "avatar.orbit", "avatar.prism", "avatar.kite", "avatar.pebble", "avatar.ray")
PLACEHOLDER = re.compile(r"\{\{([^{}\n]{0,64})\}\}")
FAMILY_OVERLAY_MAX = 600  # PRM-08


class AssetError(ValueError):
    pass


def _sha(text: str) -> str:
    return hashlib.sha256(text.encode()).hexdigest()


@dataclass(frozen=True)
class Soul:
    """The soul as loaded (PRM-01, PRM-02): read once from the bundle and never written; its sections are read-only."""

    sections: MappingProxyType[str, str]  # section title -> its lines (placeholders filled)
    text: str
    sha256: str
    preamble: str = ""  # the lines before the first section (who the tutor is); only the full exposure carries them
    template: str = ""  # the soul as written, placeholders unfilled: a profile's chosen tutor name fills it (PRM-03)
    tutor_name: str = ""  # the identity file's name, which filled this soul

    def named(self, tutor_name: str | None) -> Soul:
        """This soul with the profile's chosen tutor name in the placeholder instead of the identity file's (PRM-03,
        D99); the core checked the name against PRV-03 when the student chose it. The same soul when there is none.
        Braces and angle brackets are left out of the name here, so a chosen name can never form a placeholder (which
        would stop every prompt being built, PRM-02) or a context tag; nothing usable left keeps the default."""
        tutor_name = " ".join(re.sub(r"[{}<>]", "", tutor_name or "").split())
        if not tutor_name or tutor_name == self.tutor_name or not self.template:
            return self
        try:
            return _soul_from(self.template, {"TUTOR_NAME": tutor_name})
        except AssetError:
            return self  # a name long enough to push the soul past its size keeps the identity file's name

    def exposure(self, level: str) -> str:
        """Tier-1 text for a role's soul exposure (PRM-04): full, values, values_boundaries or none."""
        wanted = {
            "full": SOUL_SECTIONS,
            "values": ("Values",),
            "values_boundaries": ("Values", "Boundaries"),
            "none": (),
        }[level]
        parts = [self.preamble] if level == "full" and self.preamble else []
        return "\n\n".join(parts + [f"## {t}\n{self.sections[t]}" for t in wanted])


def load_identity(folder: Path) -> dict[str, str]:
    """`IDENTITY.yaml` (PRM-03): exactly the four keys, and a locale that is a language parameter (D20)."""
    identity = yaml.safe_load((folder / "IDENTITY.yaml").read_text()) or {}
    if not isinstance(identity, dict) or sorted(identity) != sorted(IDENTITY_KEYS):
        found = sorted(identity) if isinstance(identity, dict) else identity
        raise AssetError(f"IDENTITY.yaml holds {found}, not exactly {list(IDENTITY_KEYS)}")
    if not re.fullmatch(r"(en|fr|pt|ar)(-[A-Z]{2})?", str(identity["locale"])):
        raise AssetError(f"IDENTITY.yaml locale {identity['locale']} is not a language parameter")
    found = {k: str(v) for k, v in identity.items()}
    if found["avatar_id"] not in TUTOR_CHARACTERS:  # an id from before the six characters reads as Jana (D103, G119)
        found["avatar_id"] = TUTOR_CHARACTERS[0]
    return found


def load_soul(folder: Path) -> Soul:
    """The soul with its placeholders filled from the identity file and only from there (PRM-01, PRM-03). A
    placeholder the identity does not fill stays in the text, so `build_prompt` refuses to build (PRM-02)."""
    identity = load_identity(folder)
    raw = re.sub(r"<!--.*?-->\n?", "", (folder / "SOUL.md").read_text(), flags=re.S)  # author comments only
    return _soul_from(raw, {"TUTOR_NAME": identity["tutor_name"]})


def _soul_from(raw: str, values: dict[str, str]) -> Soul:
    text = PLACEHOLDER.sub(lambda m: values.get(m.group(1), m.group(0)), raw)
    if len(text) > SOUL_MAX:
        raise AssetError(f"the soul is {len(text)} characters (at most {SOUL_MAX})")
    sections: dict[str, str] = {}
    for match in re.finditer(r"^## (.+)\n((?:(?!^## ).*\n?)*)", text, re.M):
        sections[match.group(1).strip()] = match.group(2).strip()
    if tuple(sections) != SOUL_SECTIONS:
        raise AssetError(f"the soul's sections are {list(sections)}, not {list(SOUL_SECTIONS)}")
    first = re.search(r"^## ", text, re.M)
    preamble = text[: first.start()].strip() if first else ""
    return Soul(MappingProxyType(sections), text, _sha(text), preamble, raw, values.get("TUTOR_NAME", ""))


def soul_problems(soul: Soul, tool_names: set[str]) -> list[str]:
    """PRM-01: at most 2,000 characters, 15 to 30 lines, exactly the four sections, no tool names and no
    procedures (tool rules live in role manifests and skills)."""
    problems = []
    if len(soul.text) > SOUL_MAX:
        problems.append(f"{len(soul.text)} characters (at most {SOUL_MAX})")
    lines = len([ln for ln in soul.text.splitlines() if ln.strip()])
    if not SOUL_LINES[0] <= lines <= SOUL_LINES[1]:
        problems.append(f"{lines} lines (15 to 30)")
    if tuple(soul.sections) != SOUL_SECTIONS:
        problems.append(f"sections {list(soul.sections)}")
    words = set(re.findall(r"[a-z][a-z0-9_]{2,39}", soul.text.lower()))
    named = sorted(t for t in tool_names if "_" in t and t in words) + re.findall(r"`[^`]+`", soul.text)
    if named:
        problems.append(f"names tools or code: {named[:3]}")
    return problems


@dataclass(frozen=True)
class RoleManifest:
    meta: dict[str, Any]
    body: str
    path: Path

    @property
    def id(self) -> str:
        return str(self.meta["id"])

    @property
    def sha256(self) -> str:
        return _sha(self.path.read_text())

    @property
    def tools(self) -> list[str]:
        return list(self.meta["tools"])


def manifest_errors(path: Path) -> list[str]:
    """Contract, body length and sections of one role manifest (PRM-05)."""
    try:
        meta, body, _ = split_front_matter(path.read_text())
    except ValueError as error:
        return [f"{path.name}: {error}"]
    problems = [f"{path.name}: {p}" for p in contract_errors("agents/role-manifest", meta)]
    if meta.get("id") != path.stem:
        problems.append(f"{path.name}: id {meta.get('id')} does not equal the file name")
    if len(body) > ROLE_BODY_MAX:
        problems.append(f"{path.name}: body is {len(body)} characters (at most {ROLE_BODY_MAX})")
    titles = tuple(m.group(1).strip() for m in re.finditer(r"^## (.+)$", body, re.M))
    if titles != ROLE_SECTIONS:
        problems.append(f"{path.name}: sections are {list(titles)}, not {list(ROLE_SECTIONS)}")
    method = re.search(r"^## Method\n((?:(?!^## ).*\n?)*)", body, re.M)
    if not method or not re.search(r"^1\. ", method.group(1), re.M):
        problems.append(f"{path.name}: the Method section is not numbered")
    return problems


def load_manifest(path: Path) -> RoleManifest:
    problems = manifest_errors(path)
    if problems:
        raise AssetError(problems[0])
    meta, body, _ = split_front_matter(path.read_text())
    return RoleManifest(meta, body.strip(), path)


@dataclass(frozen=True)
class Block:
    """A guidance block or a family overlay (PRM-07, PRM-08)."""

    meta: dict[str, Any]
    body: str

    @property
    def id(self) -> str:
        return str(self.meta["id"])

    def applies(
        self, role: str, family: str, mode: str | None, prompt_mode: str, available: set[str], locale: str = "en"
    ) -> bool:
        m = self.meta
        return (
            (role in m["applies_to"] or "*" in m["applies_to"])
            and ("*" in m["models"] or family in m["models"])
            and set(m.get("requires_tools", [])) <= available
            and (mode is None or not m.get("modes") or mode in m["modes"])
            and (not m.get("prompt_modes") or prompt_mode in m["prompt_modes"])
            and (not m.get("locales") or locale in m["locales"] or locale.split("-")[0] in m["locales"])
            and (prompt_mode != "minimal" or m.get("essential", False) or m["kind"] == "family_overlay")
        )


def load_block(path: Path) -> Block:
    meta, body, _ = split_front_matter(path.read_text())
    problems = contract_errors("agents/guidance-block", meta)
    if problems:
        raise AssetError(f"{path.name}: {problems[0]}")
    if meta["id"] != path.stem:
        raise AssetError(f"{path.name}: id {meta['id']} does not equal the file name")
    if len(body.strip()) > meta["max_chars"]:
        raise AssetError(f"{path.name}: body is longer than its max_chars ({meta['max_chars']})")
    if path.parent.name == "families" and (meta["kind"] != "family_overlay" or len(body.strip()) > FAMILY_OVERLAY_MAX):
        raise AssetError(f"{path.name}: a family overlay is a family_overlay block of at most {FAMILY_OVERLAY_MAX} "
                         "characters (PRM-08)")  # fmt: skip
    return Block(meta, body.strip())


@dataclass(frozen=True)
class AgentAssets:
    soul: Soul
    roles: dict[str, RoleManifest]
    guidance: tuple[Block, ...]
    families: tuple[Block, ...]
    skills: SkillSet
    root: Path
    glossary: Glossary = field(default_factory=Glossary)  # the course definitions of tier 5 (PRM-09)

    @classmethod
    def load(
        cls, root: Path, skills_root: Path, skill_statuses: frozenset[str] = frozenset({"reviewed"})
    ) -> AgentAssets:
        return cls(
            soul=load_soul(root / "soul"),
            roles={p.stem: load_manifest(p) for p in sorted((root / "roles").glob("*.md"))},
            guidance=tuple(load_block(p) for p in sorted((root / "guidance").glob("*.md"))),
            families=tuple(load_block(p) for p in sorted((root / "families").glob("*.md"))),
            skills=SkillSet.load(skills_root, skill_statuses),
            root=root,
            glossary=Glossary.load(root / "glossary" / "en.yaml"),
        )

    def fingerprint(self) -> dict[str, str]:
        """Versions and hashes of the soul and every role manifest (OBS-03)."""
        return {"soul": self.soul.sha256, **{f"role.{k}": v.sha256 for k, v in sorted(self.roles.items())}}
