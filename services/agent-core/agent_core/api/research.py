"""The research agent on the device (SPEC §18): the short conversation, then a study pack made from the web.

    POST /v1/study/research/chat              one turn of the conversation (the app keeps the turns so far)
    POST /v1/study/research                   start research on the approved brief (202, a Record)
    GET  /v1/study/research[/{research_id}]   the packs, each brought up to date when read
    POST /v1/study/research/{research_id}/cancel

The gateway's job searches and fetches the pages; when they arrive they become one Study text source ("Web research:
…", one section per page with its URL) and each part the brief asked for is an ordinary Study job on it, so every
claim is checked against its cited passages as any Study aid is (the gateway's support verifier). A photographed
diagram's pack also keeps its capture for the diagram drill. The conversation's text never leaves the device again:
the job receives the brief's fields and the photo's confirmed part names only. A pack is brought up to date when it
is read (the app polls while it shows one), so a restart or an offline spell only delays it.
"""

from __future__ import annotations

import contextlib
import json
import re
from collections.abc import Awaitable, Callable
from typing import Any

from fastapi import FastAPI, Request, Response

from agent_core.api.core import CURRENT_PROFILE, CoreService
from agent_core.api.errors import ApiError, cloud_refusal
from agent_core.contracts import errors as contract_errors
from agent_core.engine.events import new_id
from agent_core.engine.research_chat import ChatError, Start, job_trace, learner_profile, next_turn, trace_record
from agent_core.engine.research_links import checked_links
from agent_core.engine.safety import redact_personal
from agent_core.engine.study import StudyDesk, StudyError, checked
from agent_core.engine.study_concepts import RELATIONS_MAX, with_outline

RESEARCH = "study/research#/$defs/"
SOURCE_CHARS = 20_000  # what Study processes per part: a few batches, not dozens (see `relevant`)
STOPWORDS = frozenset(
    {
        "what",
        "which",
        "when",
        "where",
        "does",
        "with",
        "from",
        "that",
        "this",
        "their",
        "they",
        "into",
        "each",
        "your",
        "about",
        "level",
        "there",
        "these",
        "those",
        "have",
        "been",
    }
)
PART_KINDS = ("concept_map", "notes", "flashcards", "quiz", "audio_overview")
# SPEC §18.1: only types code marks, as Study's own quiz defaults. With multi-select and numeric added, the first live
# pack's quiz failed (a topic with no quantities); Study's defaults made the same material's quiz at once.
CODE_MARKED = ["choice", "fill_blank", "true_false"]
LIVE = ("sending", "waiting_online", "researching", "making")


def _stem(word: str) -> str:
    """Enough of a stem to match "valves" with "valve" and "stops" with "stop"."""
    if word.endswith("ies") and len(word) > 5:
        return word[:-3] + "y"
    if word.endswith("ing") and len(word) > 6:
        return word[:-3]
    if word.endswith("ed") and len(word) > 5:
        return word[:-2]
    if word.endswith("s") and not word.endswith("ss"):
        return word[:-1]
    return word


def _words(text: str) -> set[str]:
    return {_stem(w) for w in re.findall(r"[a-z][a-z-]{3,}", text.lower()) if w not in STOPWORDS}


def relevant(text: str, question: str, budget: int) -> str:
    """The page's paragraphs that share most words with the question it was found for, in page order, within
    `budget` characters: Study processes every character it is given, so the pack's material is what bears on the
    plan, not whole pages (measured 2026-10-27: 17 whole pages were 195,000 characters, 39 batches and ~$0.40 for one
    concept map)."""
    wanted = _words(question)
    paragraphs = [p.strip() for p in re.split(r"\n\s*\n", text) if len(p.strip()) >= 60]
    scored = sorted(range(len(paragraphs)),
                    key=lambda i: (-len(wanted & _words(paragraphs[i])), i))  # fmt: skip
    kept: list[int] = []
    used = 0
    for i in scored:
        if not wanted & _words(paragraphs[i]):
            break
        if used + len(paragraphs[i]) > budget:
            continue
        kept.append(i)
        used += len(paragraphs[i]) + 2
    return "\n\n".join(paragraphs[i] for i in sorted(kept))


def page_sections(title: str, brief: dict[str, Any], pages: list[dict[str, Any]]) -> str:
    """The researched pages as one Study source: each page keeps its title, site and URL above the passages that bear
    on its question and the plan's focus, so every citation shows where it came from; a page with none is left out."""
    head = (f"Web research: {title}\nFound by the research agent for these questions:\n"
            + "\n".join(f"- {q}" for q in brief["sub_questions"]))  # fmt: skip
    budget = max(600, (SOURCE_CHARS - len(head)) // max(1, len(pages)))
    sections: list[str] = []
    for page in pages:
        index = page["sub_question"]
        question = brief["sub_questions"][index] if index < len(brief["sub_questions"]) else brief["title"]
        passages = relevant(page["text"], f"{question} {brief['focus']} {brief['title']}", budget)
        if passages:
            sections.append(f"## {len(sections) + 1}. {page['title']}\nSource URL: {page['url']}\n"
                            f"Site: {page['site']}\nQuestion: {index + 1}\n\n{passages}")  # fmt: skip
    return "\n\n".join([head, *sections])


QUESTION_LINE = re.compile(r"^Question: (\d+)$", re.M)


def branches_for(conn: Any, source_id: str, concepts: list[dict[str, Any]]) -> dict[str, int]:
    """Which of the brief's questions each idea was found for: the section its summary's first citation lies in
    (each page section of the pack's material names its question, `page_sections`)."""
    rows = [json.loads(r[0]) for r in conn.execute("SELECT body FROM study_chunks WHERE source_id=?", (source_id,))]
    rows.sort(key=lambda c: c["ordinal"])
    starts, at = {}, 0
    for chunk in rows:
        starts[chunk["chunk_id"]] = at
        at += len(chunk["text"])
    marks = [(m.start(), int(m[1]) - 1) for m in QUESTION_LINE.finditer("".join(c["text"] for c in rows))]
    out: dict[str, int] = {}
    for concept in concepts:
        citations = (concept.get("summary") or {}).get("citations") or []
        if not citations or citations[0]["chunk_id"] not in starts:
            continue
        position = starts[citations[0]["chunk_id"]] + citations[0]["start"]
        before = [index for start, index in marks if start <= position]
        if before:
            out[concept["concept_id"]] = before[-1]
    return out


def part_request(kind: str, source: dict[str, Any], brief: dict[str, Any]) -> dict[str, Any]:
    """The Study job for one part of the pack, scoped by the brief (its focus, its length)."""
    selection = {"source_id": source["source_id"], "revision": source["revision"]}
    body: dict[str, Any] = {"kind": kind, "sources": [selection], "execution": "prefer_cloud"}
    long = brief["length"] == "long"
    settings: dict[str, Any] = {}
    if brief["focus"] and kind in ("notes", "flashcards", "quiz"):  # the kinds whose settings take a focus
        settings["focus"] = brief["focus"][:500]
    if kind == "flashcards":
        body["count"] = 20 if long else 10
    if kind == "quiz":
        body["count"] = 10
        settings["question_types"] = CODE_MARKED
        settings["difficulty"] = "medium"
    if kind == "audio_overview":
        settings["podcast_length"] = "standard" if long else "short"
    if settings:
        body["settings"] = settings
    return body


def photo_parts(core: CoreService, capture_id: str) -> tuple[str | None, tuple[str, ...]]:
    """A confirmed photographed diagram's title and part names (the diagram drill's reading, SPEC §5.2)."""
    desk = getattr(core, "capture_desk", None)
    if desk is None:
        raise ApiError("not_found")
    record = desk.get(capture_id)
    if record["purpose"] != "diagram" or record["status"] != "confirmed":
        raise ApiError("validation_failed", details={"reason": "diagram_not_confirmed"})
    named = [r["text"].strip() for r in record["regions"] if r["kind"] == "diagram" and "[?]" not in r["text"]]
    names = tuple(dict.fromkeys(n for n in named if n))
    if not names:
        raise ApiError("validation_failed", details={"reason": "diagram_not_confirmed"})
    return None, names[:24]


def add_routes(app: FastAPI, core: CoreService, desk: Callable[[], StudyDesk],
               command: Callable[..., Awaitable[Response]], fail: Callable[[StudyError], ApiError],
               response: Callable[..., Response]) -> None:  # fmt: skip
    def cloud() -> Any:
        if core.study.cloud is None:
            raise StudyError("consent_required")
        return core.study.cloud

    def load(d: StudyDesk, research_id: str) -> tuple[dict[str, Any], str | None]:
        row = d.conn.execute("SELECT body,gateway_id FROM study_research WHERE research_id=?",
                             (research_id,)).fetchone()  # fmt: skip
        if row is None:
            raise StudyError("not_found")
        return json.loads(row[0]), row[1]

    def save(d: StudyDesk, record: dict[str, Any], gateway_id: str | None) -> dict[str, Any]:
        record["updated_at"] = core.clock_ms()
        if contract_errors(RESEARCH + "Record", record):
            raise StudyError("invalid_study_request")
        d.conn.execute("UPDATE study_research SET body=?,gateway_id=?,updated_at=? WHERE research_id=?",
                       (json.dumps(record), gateway_id, record["updated_at"], record["research_id"]))  # fmt: skip
        return record

    async def send(d: StudyDesk, record: dict[str, Any]) -> tuple[dict[str, Any], str | None]:
        """The brief to the gateway; offline it waits, refused it fails with the reason."""
        parts: tuple[str, ...] = ()
        if record["capture_id"]:
            _, parts = photo_parts(core, record["capture_id"])
        try:
            job = await cloud().research_start(d.conn, {"brief": record["brief"], "photo_parts": list(parts)})
        except ValueError as error:
            reason = cloud_refusal(error)
            if reason == "waiting_online":
                return {**record, "status": "waiting_online"}, None
            failure = reason if reason in ("consent_required", "allowance_exhausted") else "upstream"
            return {**record, "status": "failed", "failure": failure}, None
        return {**record, "status": "researching", "sub_questions": job["sub_questions"]}, job["research_id"]

    def make(d: StudyDesk, record: dict[str, Any], pages: list[dict[str, Any]]) -> dict[str, Any]:
        """The pages as one Study source, and a Study job for each part the brief asked for."""
        brief = record["brief"]
        text, _ = redact_personal(page_sections(brief["title"], brief, pages))
        source = d.create({"kind": "text", "title": f"Web research: {brief['title']}"[:200], "text": text})
        parts = []
        for kind in [k for k in PART_KINDS if k in brief["outputs"]]:
            try:
                job = d.create_job(part_request(kind, source, brief))
                parts.append({"kind": kind, "job_id": job["job_id"], "aid_id": None, "state": "making"})
            except StudyError:
                parts.append({"kind": kind, "job_id": None, "aid_id": None, "state": "failed"})
        core.study.poll()
        return {**record, "status": "making", "pages": len(pages), "source_id": source["source_id"], "parts": parts}

    def settle(d: StudyDesk, record: dict[str, Any]) -> dict[str, Any]:
        """Each part follows its Study job; the pack is ready once none is still being made."""
        parts = []
        for part in record["parts"]:
            if part["state"] == "making" and part["job_id"]:
                job = d.job(part["job_id"])
                if job["state"] == "completed" and job["result_ids"]:
                    part = {**part, "state": "ready", "aid_id": job["result_ids"][0]}
                elif job["state"] in ("failed", "cancelled"):
                    part = {**part, "state": "failed"}
            parts.append(part)
        done = all(p["state"] in ("ready", "failed") for p in parts)
        return {**record, "parts": parts, "status": "ready" if done else "making"}

    async def refresh(d: StudyDesk, research_id: str) -> dict[str, Any]:
        record, gateway_id = load(d, research_id)
        if record["status"] in ("sending", "waiting_online") and gateway_id is None:
            record, gateway_id = await send(d, record)
        elif record["status"] == "researching" and gateway_id is not None:
            try:
                job = await cloud().research_job(d.conn, gateway_id)
            except ValueError as error:
                if cloud_refusal(error) == "research_not_found":  # the gateway lost it (a 24-hour expiry, a reset)
                    record = {**record, "status": "failed", "failure": "interrupted"}
                return save(d, record, gateway_id)
            record = {**record, "sub_questions": job["sub_questions"]}
            if job["status"] == "ready" and job["pages"]:
                try:
                    record = make(d, record, job["pages"])
                except StudyError:
                    record = {**record, "status": "failed", "failure": "import_failed"}
                try:
                    await cloud().research_forget(d.conn, gateway_id)  # the pages are kept here now
                except ValueError as error:
                    cloud_refusal(error)  # the gateway deletes them itself within 24 hours
            elif job["status"] in ("failed", "cancelled"):
                record = {**record, "status": job["status"], "failure": job["failure"]}
        if record["status"] == "making":
            was = {p["kind"]: p["state"] for p in record["parts"]}
            record = save(d, settle(d, record), gateway_id)  # saved first: a second read never shapes the map again
            for part in record["parts"]:
                if part["kind"] == "concept_map" and part["state"] == "ready" and was.get("concept_map") != "ready":
                    await shape(d, record, part["aid_id"])
            return record
        return save(d, record, gateway_id)

    async def shape(d: StudyDesk, record: dict[str, Any], aid_id: str | None) -> None:
        """The pack's concept map as a tree: its outline (the topic, its questions, each idea under the question it was
        found for), then the links a whole-map pass proposes and its check confirms (`research_links`)."""
        if aid_id is None or record["source_id"] is None:
            return
        try:
            aid = d.aid(aid_id)
        except StudyError:
            return  # gone (the learner deleted it): nothing to shape
        current = aid.get("concept_map")
        if current is None or any(c["origin"] == "outline" for c in current["concepts"]):
            return
        brief = record["brief"]
        shaped = with_outline(current, brief["title"], brief["sub_questions"],
                              branches_for(d.conn, record["source_id"], current["concepts"]))  # fmt: skip
        router = core.research_router() if core.research_router is not None else None
        if router is not None:
            links = await checked_links(router, shaped)
            shaped = {**shaped, "relations": (shaped["relations"] + links)[:RELATIONS_MAX]}
        aid = d.aid(aid_id)  # read again: the learner may have edited it meanwhile
        if any(c["origin"] == "outline" for c in aid["concept_map"]["concepts"]):
            return
        aid["concept_map"] = shaped
        checked("Aid", aid)
        d.conn.execute("UPDATE study_aids SET body=? WHERE aid_id=?", (json.dumps(aid), aid_id))

    async def guarded(run: Callable[[StudyDesk], Awaitable[Any]]) -> Response:
        try:
            return response(await run(desk()))
        except StudyError as error:
            raise fail(error) from None

    def first_use_notice(answer: Any) -> None:
        """The online tutor's first-use notice (ARC-10) before the conversation's first cloud call. The conversation
        goes to the cloud tier, and a profile that starts in Study has never been asked it (the tutoring card asks it
        in a turn), so the router would drop the cloud candidate and nothing could ever ask. The learner's answer,
        when the turn carries one, is recorded first; a missing or declined answer is refused with its reason, for
        the app to show the notice and send the answer with the same turn."""
        online, profile = getattr(core, "online", None), CURRENT_PROFILE.get()
        if online is None or profile is None:
            return
        if answer in ("accepted", "declined"):
            online.record_notice(profile, "cloud_tier", answer == "accepted")
        given = online.notice_answer(profile, "cloud_tier")
        if given != "accepted":
            reason = "notice_declined" if given == "declined" else "notice_required"
            raise ApiError("feature_disabled", details={"reason": reason, "feature": "cloud_tier"})

    def _job_trace(core: Any, trace: Any) -> None:
        """The research conversation's job trace, written where the runtime's traces go (none without a store)."""
        sink, record = getattr(core, "job_trace_sink", None), trace_record(trace)
        if sink is not None and record is not None:
            with contextlib.suppress(Exception):
                sink(record)

    @app.post("/v1/study/research/chat")
    async def chat(request: Request) -> Response:
        async def run(d: StudyDesk, body: dict[str, Any]) -> dict[str, Any]:
            try:
                cloud().guard(d.conn)  # consent, subscription: the conversation is online as the research is
            except ValueError as error:
                raise StudyError(cloud_refusal(error)) from None
            start = body["start"]
            if "capture_id" in start:
                title, parts = photo_parts(core, start["capture_id"])
                subject = Start(photo_title=title, photo_parts=parts)
            else:
                subject = Start(topic=start["topic"])
            first_use_notice(body.get("cloud_notice"))
            router = core.research_router() if core.research_router is not None else None
            if router is None:
                raise ApiError("model_unavailable")
            # SPEC §19.4: the learner's level, subjects and bands go in as the profile section; an O profile's level
            # is known by code (JM-D29). A profile that cannot be read leaves the conversation as it was.
            try:
                content = core.study.content() if core.study.content is not None else None
                profile = learner_profile(d.conn, content, core.settings, core._today())
            except Exception:
                profile = None
            trace = job_trace(core.records_profile, core.settings)
            try:
                return await next_turn(router, subject, body["history"], body["answer"], body["skip"],
                                       profile=profile, trace=trace)  # fmt: skip
            except ChatError as error:
                # The reason by code (the router's and each provider refusal's), never prompt text: what failed is
                # visible in the reply and the job trace (§19 status, W8 fix).
                raise ApiError("model_unavailable", details={"reason": str(error), **error.details}) from None
            finally:
                _job_trace(core, trace)

        return await command(request, "study/research#/$defs/ChatRequest", run, atomic_command=False)

    @app.post("/v1/study/research")
    async def create(request: Request) -> Response:
        async def run(d: StudyDesk, body: dict[str, Any]) -> dict[str, Any]:
            brief = dict(body["brief"])
            if body["capture_id"]:
                photo_parts(core, body["capture_id"])  # a confirmed diagram, or refused before anything is sent
            else:
                brief["outputs"] = [o for o in brief["outputs"] if o != "diagram_drill"] or ["notes"]
            now = core.clock_ms()
            record = {"research_id": new_id("rsr"), "status": "sending", "brief": brief,
                      "capture_id": body["capture_id"], "failure": None, "source_id": None, "pages": 0, "parts": [],
                      "sub_questions": [{"text": q, "state": "waiting", "sources": 0} for q in brief["sub_questions"]],
                      "created_at": now, "updated_at": now}  # fmt: skip
            if contract_errors(RESEARCH + "Record", record):
                raise StudyError("invalid_study_request")
            d.conn.execute("INSERT INTO study_research VALUES (?,?,?,?,?)",
                           (record["research_id"], None, json.dumps(record), now, now))  # fmt: skip
            record, gateway_id = await send(d, record)
            return save(d, record, gateway_id)

        return await command(request, "study/research#/$defs/CreateRequest", run, status=202, atomic_command=False)

    @app.get("/v1/study/research")
    async def listed() -> Response:
        async def run(d: StudyDesk) -> dict[str, Any]:
            rows = d.conn.execute("SELECT body FROM study_research ORDER BY created_at DESC, research_id DESC "
                                  "LIMIT 200").fetchall()  # fmt: skip
            return {"items": [json.loads(r[0]) for r in rows]}

        return await guarded(run)

    @app.get("/v1/study/research/{research_id}")
    async def one(research_id: str) -> Response:
        async def run(d: StudyDesk) -> dict[str, Any]:
            return await refresh(d, research_id)

        return await guarded(run)

    @app.post("/v1/study/research/{research_id}/cancel")
    async def cancel(research_id: str, request: Request) -> Response:
        async def run(d: StudyDesk, _: Any) -> dict[str, Any]:
            record, gateway_id = load(d, research_id)
            if record["status"] not in ("sending", "waiting_online", "researching"):
                raise StudyError("invalid_study_request", True)  # making or done: the Study jobs are the parts now
            if gateway_id is not None:
                try:
                    await cloud().research_cancel(d.conn, gateway_id)
                except ValueError as error:
                    cloud_refusal(
                        error
                    )  # the gateway stops at its own limits; nothing more is fetched for this pack here
            return save(d, {**record, "status": "cancelled"}, gateway_id)

        return await command(request, None, run, atomic_command=False)
