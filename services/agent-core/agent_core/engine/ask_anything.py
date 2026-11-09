"""Ask anything: a general question gets a grounded, labelled answer (STU-20, D117).

The online feature `ask_anything`. What it guarantees, whatever the cloud replies:

- Homework never gets an answer here: the question is routed first (`ask_route`) and a question in an installed
  subject is handed to the guided tutor without any answer (PED-01, D87).
- The search is shown before the answer: the learner's own material and session notes first, then trusted sources
  (`steps`), and every supported statement carries its citations.
- For a profile under 18 only the allow-listed sources (`config/ask_sources.json`, owned by the safety lead) count,
  and "Teach me" is the mode: a short answer that ends with one check question, the explanation on request. A
  learner of 18 or over may choose "Just explain" and the wider web, less its excluded domains. The policy is decided
  here from the profile, never from the request's reply: a fetched page that asks for another mode or another source
  changes nothing.
- A source the policy does not allow is dropped, and a statement left without a citation is shown with the
  general-knowledge label ("General knowledge, not checked against your syllabus or your material"). Nothing in this
  mode is labelled checked: no tool checks a general answer, so `checked` is always false.
- Fetched text is data, never instructions: snippets pass the untrusted-input scanner (PRM-18) and lose matched spans,
  and the output passes the output critic's safety classification (STU-11, PRV-18 layer 2).

Shapes (`AskRequest` from the device to the gateway, `AskReply` back, `AskAnswer` to the app) are validated here; they
are engine-local until the specification adopts them as contracts (D72 proposal, see the M45.a report)."""

from __future__ import annotations

import json
import re
import sqlite3
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass
from functools import cache
from pathlib import Path
from typing import Any, Literal
from urllib.parse import urlsplit

from agent_core.engine.safety import OutputCritic
from agent_core.prompts.untrusted import screened

Audience = Literal["under_18", "adult"]
Mode = Literal["teach", "explain"]
Reach = Literal["allow_list", "wider_web"]

SOURCES_FILE = Path(__file__).resolve().parents[1] / "config" / "ask_sources.json"
QUESTION_MAX = 2000
OWN_MAX = 6  # excerpts of the learner's own material and notes searched first
OWN_CHARS = 600
TEACH_SHORT = 3  # statements in a Teach me answer before the check question; the rest is the explanation
_WORD = re.compile(r"[^\W_]{3,}")


class AskError(ValueError):
    def __init__(self, reason: str):
        super().__init__(reason)
        self.reason = reason


@dataclass(frozen=True)
class SourceRule:
    domain: str
    kind: str
    path_prefix: str = ""


@dataclass(frozen=True)
class SourceConfig:
    version: str
    allow_list: tuple[SourceRule, ...]
    wider_web_excluded: tuple[str, ...]

    def domains(self) -> list[str]:
        return sorted({r.domain for r in self.allow_list})


@cache
def source_config(path: Path = SOURCES_FILE) -> SourceConfig:
    data = json.loads(path.read_text())
    rules = tuple(
        SourceRule(str(r["domain"]).lower(), str(r["kind"]), str(r.get("path_prefix", ""))) for r in data["allow_list"]
    )
    return SourceConfig(str(data["version"]), rules, tuple(str(d).lower() for d in data["wider_web_excluded"]))


def _under(host: str, domain: str) -> bool:
    return host == domain or host.endswith("." + domain)


@dataclass(frozen=True)
class AskPolicy:
    audience: Audience
    mode: Mode
    reach: Reach

    def allows(self, url: str, config: SourceConfig) -> bool:
        """A fetched source counts only if the policy allows it: https, a host, and the allow-list for under 18."""
        try:
            parts = urlsplit(url)
        except ValueError:
            return False
        host = (parts.hostname or "").lower().rstrip(".")
        if parts.scheme != "https" or not host or parts.username or parts.password:
            return False
        if self.reach == "wider_web":
            return not any(_under(host, d) for d in config.wider_web_excluded)
        path = parts.path or "/"
        return any(_under(host, r.domain) and path.startswith(r.path_prefix or "/") for r in config.allow_list)


def policy_for(age_band: str | None, mode: str | None = None, reach: str | None = None) -> AskPolicy:
    """The profile decides: under 18 (or unknown) is always Teach me on the allow-list (STU-20)."""
    if age_band != "18_plus":
        return AskPolicy("under_18", "teach", "allow_list")
    return AskPolicy(
        "adult", "explain" if mode == "explain" else "teach", "wider_web" if reach == "wider_web" else "allow_list"
    )


# --- the learner's own material first (STU-16) --------------------------------------------------------------------


def _terms(text: str) -> set[str]:
    return {w.lower() for w in _WORD.findall(text)}


def own_evidence(conn: sqlite3.Connection | None, question: str, limit: int = OWN_MAX) -> list[dict[str, Any]]:
    """The best-matching excerpts of the learner's own sources and session notes, private ones left out (STU-19).

    Lexical and local; each excerpt is kept verbatim with what it cites, so it can be shown as it is."""
    if conn is None:
        return []
    wanted = _terms(question)
    if not wanted:
        return []
    found: list[tuple[float, dict[str, Any]]] = []
    try:
        rows = conn.execute(
            "SELECT s.source_id, s.body, c.body FROM study_sources s JOIN study_chunks c "
            "ON c.source_id = s.source_id AND c.revision = s.current_revision WHERE s.private = 0"
        ).fetchall()
    except sqlite3.Error:
        rows = []
    for source_id, source_body, chunk_body in rows:
        source, chunk = json.loads(source_body), json.loads(chunk_body)
        if source.get("state") not in ("ready", "partial"):
            continue
        for part in re.split(r"(?<=[.!?])\s+", chunk.get("text", "")):
            score = len(wanted & _terms(part)) / len(wanted)
            if score >= 0.5:
                found.append((score, {
                    "kind": "own_source", "title": str(source.get("title", ""))[:120], "source_id": source_id,
                    "chunk_id": chunk["chunk_id"], "locator": chunk.get("locator"), "text": part[:OWN_CHARS],
                }))  # fmt: skip
    try:
        notes = conn.execute(
            "SELECT note_id, title, statements FROM session_notes WHERE state = 'ready' AND private = 0"
        ).fetchall()
    except sqlite3.Error:
        notes = []
    for note_id, title, statements in notes:
        for statement in json.loads(statements):
            text = statement.get("text") if isinstance(statement, dict) else None
            if not isinstance(text, str):
                continue
            score = len(wanted & _terms(text)) / len(wanted)
            if score >= 0.5:
                found.append((score, {
                    "kind": "session_note", "title": str(title)[:120], "note_id": note_id,
                    "reference": statement.get("reference"), "text": text[:OWN_CHARS],
                }))  # fmt: skip
    found.sort(key=lambda item: -item[0])
    out = []
    for n, (_, item) in enumerate(found[:limit], start=1):
        out.append({"ref": f"own{n}", **item})
    return out


# --- the request and the reply ------------------------------------------------------------------------------------


def request_body(request_id: str, question: str, policy: AskPolicy, own: Sequence[Mapping[str, Any]],
                 share_own: bool, config: SourceConfig) -> dict[str, Any]:  # fmt: skip
    """`AskRequest`: what goes to the gateway. The learner's excerpts go only when the profile may send study
    material (`share_own`); the allow-list goes with an under-18 request so the gateway searches only there."""
    body: dict[str, Any] = {
        "request_id": request_id,
        "question": question[:QUESTION_MAX],
        "mode": policy.mode,
        "audience": policy.audience,
        "reach": policy.reach,
        "output_locale": "en",
        "own": [{"ref": o["ref"], "title": o["title"], "text": o["text"]} for o in own] if share_own else [],
    }
    if policy.reach == "allow_list":
        body["allow_list"] = config.domains()
    else:
        body["excluded"] = list(config.wider_web_excluded)
    return body


def _text(value: Any, limit: int) -> str | None:
    return value[:limit] if isinstance(value, str) and value.strip() else None


def _statements(value: Any) -> list[dict[str, Any]]:
    if not isinstance(value, list):
        raise AskError("insufficient_evidence")
    out = []
    for item in value[:24]:
        text = _text(item.get("text") if isinstance(item, dict) else None, 1200)
        refs = item.get("refs") if isinstance(item, dict) else None
        if text is None or not isinstance(refs, list) or not all(isinstance(r, str) for r in refs):
            raise AskError("insufficient_evidence")
        out.append({"text": text, "refs": list(dict.fromkeys(refs))[:6]})
    return out


def compose(ask_id: str, question: str, policy: AskPolicy, own: Sequence[Mapping[str, Any]], reply: Any,
            config: SourceConfig, critic: OutputCritic | None = None) -> dict[str, Any]:  # fmt: skip
    """`AskAnswer` from the gateway's `AskReply`, under the device's own rules (see the module's docstring)."""
    if not isinstance(reply, dict) or not isinstance(reply.get("sources"), list):
        raise AskError("insufficient_evidence")
    web: list[dict[str, Any]] = []
    seen: set[str] = set()
    for item in reply["sources"][:20]:
        if not isinstance(item, dict):
            continue
        ref, url = item.get("ref"), item.get("url")
        if not isinstance(ref, str) or not isinstance(url, str) or ref in seen or ref.startswith("own"):
            continue
        if not policy.allows(url, config):
            continue  # not on the policy: as if never fetched; what cited it is left unsupported
        seen.add(ref)
        # A snippet carrying instruction-like text is not shown at all: the source still counts, its words do not.
        raw = _text(item.get("snippet"), 600) or ""
        snippet = "" if screened(raw, "upload")[1] else raw
        host = (urlsplit(url).hostname or "").lower()
        web.append({"ref": ref, "kind": "web", "url": url, "domain": host.removeprefix("www."),
                    "title": _text(item.get("title"), 160) or host, "snippet": snippet})  # fmt: skip
    own_refs = {o["ref"] for o in own}
    known = own_refs | seen
    statements = _statements(reply.get("statements"))
    explanation = _statements(reply.get("explanation", []))
    check = _text(reply.get("check_question"), 400)
    if policy.mode == "teach" and check is None:
        raise AskError("insufficient_evidence")
    critic = critic or OutputCritic()

    def label(items: Iterable[dict[str, Any]]) -> list[dict[str, Any]]:
        out = []
        for s in items:
            if critic.check(s["text"], "cloud") is not None:
                raise AskError("safety_blocked")
            refs = [r for r in s["refs"] if r in known]
            # A statement is supported only by a source shown here; no tool checks a general answer (STU-20).
            out.append({"text": s["text"], "refs": refs, "supported": bool(refs), "checked": False})
        return out

    answer, rest = label(statements), label(explanation)
    if check is not None and critic.check(check, "cloud") is not None:
        raise AskError("safety_blocked")
    if policy.mode == "teach" and len(answer) > TEACH_SHORT:
        answer, rest = answer[:TEACH_SHORT], answer[TEACH_SHORT:] + rest
    if not answer:
        raise AskError("insufficient_evidence")
    cited = {r for s in (*answer, *rest) for r in s["refs"]}
    sources = [
        {k: v for k, v in o.items() if k in ("ref", "kind", "title", "source_id", "note_id", "chunk_id", "locator")}
        for o in own
    ] + web
    return {
        "ask_id": ask_id,
        "question": question,
        "mode": policy.mode,
        "audience": policy.audience,
        "reach": policy.reach,
        # The search, shown before the answer: "Looked in your notes", then "Looked at 4 trusted sources".
        "steps": [
            {"kind": "own", "count": len(own)},
            {"kind": "trusted" if policy.reach == "allow_list" else "web", "count": len(web)},
        ],
        "sources": sources,
        "own_excerpts": [{"ref": o["ref"], "text": o["text"]} for o in own],
        "answer": answer,
        "check_question": check if policy.mode == "teach" else None,
        "explanation": rest,
        "general_knowledge": any(not s["supported"] for s in (*answer, *rest)),
        "cited": sorted(cited),
        "checked": False,
        "can_switch": policy.audience == "adult",
    }


def handoff(route: Mapping[str, Any]) -> dict[str, Any]:
    """The reply when the question belongs to the tutor: where to go, and no answer at all."""
    return {"route": "tutor", "reason": route["reason"], "subject": route.get("subject"), "answer": None}
