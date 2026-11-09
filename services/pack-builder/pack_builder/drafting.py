"""AI drafting (AUT-17 to AUT-20): one task per request, started by a person, kept on trust nowhere.

A request carries only allow-listed inputs: curriculum records of the pack (stripped of review and authoring data),
its conventions and the author's instruction. Before it leaves the machine it is checked against the held-out hashes
and the safety-set texts (AUT-20), and blocked on a match. Drafting runs on the cloud strong tier only when online
drafting is enabled and a provider is decided (D7); otherwise on a local model on the author's machine; otherwise the
task opens the record template and makes no model call at all.

Every field a model wrote is recorded in the record's `authoring.fields` with its provenance (AUT-19) and shows as not
yet accepted. The draft is verified at once with the build's tools (AUT-18): a number, key or step the tools do not
reproduce marks the field failed. A record cannot be submitted while any AI field is unaccepted or failed; the person
who accepts AI content becomes an author of that version.
"""

from __future__ import annotations

import copy
import hashlib
import json
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import yaml

from pack_builder.accounts import AuditLog, now_iso
from pack_builder.heldout import HeldOut, text_hashes

PROMPTS = Path(__file__).resolve().parents[3] / "content" / "agent" / "drafting"
TASKS: dict[str, dict[str, Any]] = {
    "item_variants": {"table": "items", "inputs": ("kcs", "misconceptions", "conventions"), "verify": "item"},
    "hint_ladder": {"table": "items", "inputs": ("items",), "verify": "item"},
    "distractors": {"table": "items", "inputs": ("items", "misconceptions"), "verify": "item"},
    "worked_example": {"table": "worked_examples", "inputs": ("kcs", "conventions"), "verify": "worked_example"},
    "lesson_text": {"table": "lessons", "inputs": ("kcs", "misconceptions"), "verify": "review"},
    "skill_skeleton": {"table": None, "inputs": ("kcs",), "verify": "lint"},
    "alt_text": {"table": "diagrams", "inputs": ("diagrams",), "verify": "review"},
}
S4_TASKS = frozenset({"translation_draft"})
STRIPPED = ("review", "authoring")  # reviewer identities and pipeline data never leave the machine
TEMPLATES: dict[str, dict[str, Any]] = {
    "items": {
        "stem": "",
        "parts": [
            {
                "id": "a",
                "prompt": "",
                "marks": 1,
                "answer": {"kind": "numeric", "value": "", "unit": "", "check_id": ""},
                "hints": [],
            }
        ],
        "solution": {"steps": []},
        "checks": {},
    },
    "worked_examples": {"problem": "", "steps": [], "checks": {}},
    "lessons": {"idea": "", "common_mistakes": "", "summary": ""},
    "diagrams": {"alt_text": ""},
}

Generate = Callable[[str, str], str]  # (system prompt, user message) -> model text


class DraftBlocked(PermissionError):
    """A drafting request that would send data outside its allow-list (AUT-20)."""


class DraftUnavailable(RuntimeError):
    """A drafting route that is not available in this environment."""


@dataclass(frozen=True)
class Prompt:
    prompt_id: str
    version: str
    sha256: str
    body: str


def prompt(task: str, root: Path = PROMPTS) -> Prompt:
    text = (root / f"{task}.md").read_text(encoding="utf-8")
    _, head, body = text.split("---", 2)
    meta = yaml.safe_load(head)
    return Prompt(str(meta["id"]), str(meta["version"]), hashlib.sha256(text.encode()).hexdigest(), body.strip())


@dataclass(frozen=True)
class Route:
    kind: str  # cloud | local | template
    model: str | None = None
    provider: str | None = None  # the provider name, or "local"
    tier: str | None = None
    generate: Generate | None = None


def choose_route(*, online_drafting: bool, cloud: Route | None, local: Route | None) -> Route:
    """AUT-17: the cloud strong tier when online drafting is on and a provider is decided, else a local model, else
    the record template."""
    if online_drafting and cloud is not None:
        return cloud
    if local is not None:
        return local
    return Route("template")


def _clean(record: Mapping[str, Any]) -> dict[str, Any]:
    return {k: copy.deepcopy(v) for k, v in record.items() if k not in STRIPPED}


def forbidden_hashes(repo_root: Path) -> HeldOut:
    """Held-out items plus the safety set: neither may appear in a drafting request (AUT-20)."""
    held = HeldOut.load(repo_root / "evals" / "held-out")
    safety: set[str] = set()
    for path in sorted((repo_root / "evals" / "safety").glob("*.yaml")):
        safety |= text_hashes(yaml.safe_load(path.read_text()))
    return HeldOut(held.hashes | frozenset(safety))


def build_request(task: str, instruction: str, inputs: Mapping[str, list[Mapping[str, Any]]],
                  forbidden: HeldOut) -> tuple[str, str, list[str]]:  # fmt: skip
    """The system prompt, the user message and the input record ids; raises DraftBlocked outside the allow-list."""
    if task in S4_TASKS:
        raise DraftUnavailable(f"{task}: translation drafts arrive with translations (S4, AUT-40)")
    spec = TASKS.get(task)
    if spec is None:
        raise DraftUnavailable(f"{task}: not a drafting task (TABLE AUT-17a)")
    extra = sorted(set(inputs) - set(spec["inputs"]))
    if extra:
        raise DraftBlocked(f"{task}: inputs {', '.join(extra)} are outside the task's allow-list (AUT-20)")
    cleaned = {table: [_clean(r) for r in records] for table, records in inputs.items()}
    ids = sorted(str(r.get("id", r.get("key"))) for records in cleaned.values() for r in records)
    user = "Instruction from the author:\n" + instruction.strip() + "\n\nCurriculum records:\n" + yaml.safe_dump(
        cleaned, sort_keys=True, allow_unicode=True)  # fmt: skip
    if forbidden.blocks(user):
        raise DraftBlocked(f"{task}: the request contains a held-out or safety-set item; nothing was sent (AUT-20)")
    return prompt(task).body, user, ids


def authors(record: Mapping[str, Any]) -> list[str]:
    """The people who authored this version or accepted AI content in it (their role ids)."""
    authoring = record.get("authoring") or {}
    listed = list(authoring.get("authors") or [])
    if not listed and authoring.get("drafted_by"):
        listed = [authoring["drafted_by"]]
    return listed


def _ai_fields(record: Mapping[str, Any]) -> dict[str, dict[str, Any]]:
    return dict((record.get("authoring") or {}).get("fields") or {})


def apply_draft(record: dict[str, Any], drafted: Mapping[str, Any], route: Route, task: str, input_ids: list[str],
                at: str | None = None) -> dict[str, Any]:  # fmt: skip
    """Write the drafted fields into the record, each marked AI and not yet accepted (AUT-18, AUT-19)."""
    updated = copy.deepcopy(record)
    authoring = dict(updated.get("authoring") or {})
    fields = dict(authoring.get("fields") or {})
    p = prompt(task)
    for key, value in drafted.items():
        if key in ("id", "version", "review", "authoring", "provenance"):
            continue  # identity, review and provenance are never model-written
        updated[key] = value
        fields[key] = {"origin": "ai", "accepted": False, "status": "unverified", "model": route.model,
                       "provider": route.provider, "tier": route.tier, "prompt_id": p.prompt_id,
                       "prompt_sha256": p.sha256, "inputs": input_ids, "at": at or now_iso(),
                       "drafted_sha256": _value_sha(value)}  # fmt: skip
    authoring["fields"] = fields
    updated["authoring"] = authoring
    return updated


def _value_sha(value: Any) -> str:
    return hashlib.sha256(json.dumps(value, sort_keys=True, ensure_ascii=False).encode()).hexdigest()


def verify_fields(record: dict[str, Any], problems: list[str]) -> dict[str, Any]:
    """Mark AI fields passed or failed from the build verifier's problems for this record (AUT-18)."""
    updated = copy.deepcopy(record)
    fields = _ai_fields(updated)
    mine = [p for p in problems if p.startswith(str(record.get("id")))]
    for key, info in fields.items():
        if not info.get("origin", "").startswith("ai"):
            continue
        hit = [p for p in mine if key in p or key in ("parts", "solution", "steps", "checks", "answer")]
        info = dict(info)
        info["status"] = "failed" if hit else "passed"
        info["detail"] = "; ".join(hit)[:500] if hit else ""
        fields[key] = info
    updated.setdefault("authoring", {})["fields"] = fields
    return updated


def accept(record: dict[str, Any], key: str, author_role_id: str) -> dict[str, Any]:
    """The author accepts an AI field: as drafted (`ai_accepted`) or after editing it (`ai_edited`)."""
    updated = copy.deepcopy(record)
    fields = _ai_fields(updated)
    info = dict(fields.get(key) or {})
    if not info.get("origin", "").startswith("ai"):
        raise ValueError(f"{record['id']}: {key} is not an AI-drafted field")
    if info.get("status") == "failed":
        raise ValueError(f"{record['id']}: {key} failed verification; fix it and verify again first (AUT-18)")
    info["accepted"] = True
    info["origin"] = "ai_accepted" if info.get("drafted_sha256") == _value_sha(updated.get(key)) else "ai_edited"
    info["accepted_by"] = author_role_id
    fields[key] = info
    authoring = updated.setdefault("authoring", {})
    authoring["fields"] = fields
    authoring["authors"] = sorted(set(authoring.get("authors") or []) | {author_role_id})
    models = sorted({f["model"] for f in fields.values() if f.get("origin", "").startswith("ai") and f.get("model")})
    if models and isinstance(updated.get("provenance"), dict):
        updated["provenance"] = {**updated["provenance"], "drafted_with_ai": True, "ai_model": models[-1]}
    return updated


def submit_blockers(record: Mapping[str, Any]) -> list[str]:
    """Why a record cannot be submitted for review because of its AI fields (AUT-18)."""
    out = []
    for key, info in sorted(_ai_fields(record).items()):
        if not info.get("origin", "").startswith("ai"):
            continue
        if info.get("status") == "failed":
            out.append(f"{record['id']}: {key} failed verification: {info.get('detail', '')}".rstrip(": "))
        elif not info.get("accepted"):
            out.append(f"{record['id']}: {key} is an AI draft not yet accepted")
    return out


def provenance_errors(record: Mapping[str, Any]) -> list[str]:
    """AUT-19: a record with an AI-origin field says so in its pack provenance, with a model id."""
    ai = [f for f in _ai_fields(record).values() if f.get("origin", "").startswith("ai")]
    provenance = record.get("provenance") or {}
    if ai and not (provenance.get("drafted_with_ai") and provenance.get("ai_model")):
        return [f"{record.get('id')}: has AI-drafted fields but its provenance does not say drafted_with_ai with a "
                "model id (AUT-19)"]  # fmt: skip
    return []


def run_draft(task: str, record: dict[str, Any], instruction: str, inputs: Mapping[str, list[Mapping[str, Any]]],
              route: Route, forbidden: HeldOut, audit: AuditLog, actor: str,
              egress_log: Path | None = None) -> tuple[dict[str, Any], str]:  # fmt: skip
    """Draft one task into a record. Returns the updated record and the route kind used."""
    if route.kind == "template":
        template = TEMPLATES.get(TASKS[task]["table"] or "", {})
        updated = copy.deepcopy(record)
        for key, value in template.items():
            updated.setdefault(key, copy.deepcopy(value))
        audit.append(actor, f"ai_draft:{task}", "template", str(record.get("id")), "no model available")
        return updated, "template"
    system, user, ids = build_request(task, instruction, inputs, forbidden)
    assert route.generate is not None
    if route.kind == "cloud" and egress_log is not None:
        egress_log.parent.mkdir(parents=True, exist_ok=True)
        with egress_log.open("a") as out:
            out.write(json.dumps({"at": now_iso(), "task": task, "model": route.model,
                                  "bytes": len(system.encode()) + len(user.encode())}) + "\n")  # fmt: skip
    text = route.generate(system, user)
    drafted = yaml.safe_load(_strip_fence(text))
    if not isinstance(drafted, dict):
        drafted = {_scalar_field(task): drafted}
    audit.append(actor, f"ai_draft:{task}", "ok", str(record.get("id")), f"{route.kind} {route.model}")
    return apply_draft(record, drafted, route, task, ids), route.kind


def _strip_fence(text: str) -> str:
    lines = text.strip().splitlines()
    if lines and lines[0].startswith("```"):
        lines = lines[1:]
        if lines and lines[-1].startswith("```"):
            lines = lines[:-1]
    return "\n".join(lines)


def _scalar_field(task: str) -> str:
    return {"hint_ladder": "hints", "distractors": "options", "alt_text": "alt_text"}.get(task, "text")


def local_route(url: str, model: str, timeout_s: float = 120.0) -> Route:
    """A local OpenAI-compatible endpoint on the author's machine (llama-server or Ollama)."""
    import urllib.request

    def generate(system: str, user: str) -> str:
        body = json.dumps(
            {
                "model": model,
                "temperature": 0.4,
                "messages": [{"role": "system", "content": system}, {"role": "user", "content": user}],
            }
        )
        headers = {"Content-Type": "application/json"}
        request = urllib.request.Request(url.rstrip("/") + "/chat/completions", body.encode(), headers)  # noqa: S310
        # The URL is a loopback endpoint the author configured (the CLI refuses any other host).
        with urllib.request.urlopen(request, timeout=timeout_s) as response:  # noqa: S310
            reply = json.loads(response.read())
        return str(reply["choices"][0]["message"]["content"])

    return Route("local", model, "local", "local", generate)
