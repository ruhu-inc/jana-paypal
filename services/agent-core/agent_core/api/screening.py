"""The input guard on every free-text student route outside a tutor turn (PRV-13, PRV-14, STU-11, VOC-20).

Every route of the local API that takes something a student wrote is named in `ROUTES`, with how the input guard
reaches it:

- `Screened`: the guard runs here, in the API's one mutating wrapper, before the command touches anything. The
  categories the surface refuses end the command with their template (`validation_failed`, reason
  `safety_template`, the template id and whether the calm screen shows); nothing is stored or marked. Distress is
  handled wherever it is written (PRV-14): the lesson flow stops, `safety_flagged` records the category and the
  template only, and a session the input belongs to gets the calm-screen notice.
- `Turn`: the text becomes a tutor turn (or is carried into one), and the engine's guard decides it before any model
  call (`Engine._screen`, PRV-13, PRV-18 layer 1).
- `Exempt`: the body carries no student-written text (ids, enums, numbers, files, names of things), or the text
  reaches neither a model nor storage; the reason is stated.

Scope differs by surface (PRV-13 owns the categories): an answer box is not a chat, so an answer is refused only for
distress, and is otherwise marked as the student wrote it. Study notes and dictated notes follow the study area's
rule (STU-11): distress, harmful requests, sexual or violent content, medical and legal advice and cheating requests
get their templates; personal details are redacted (PRV-20) and the note is kept. A problem report is never refused,
since reporting unsafe output is its purpose (UXS-44); a disclosure in it still gets the calm screen.

`tests/test_a1_input_routes.py` builds the app and fails when a route that takes a body or a text query is missing
here, so a new route cannot skip the guard."""

from __future__ import annotations

from collections.abc import Callable, Mapping
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any

from agent_core.api.core import CURRENT_PROFILE
from agent_core.api.errors import ApiError
from agent_core.engine.events import new_id
from agent_core.engine.input import normalise_input
from agent_core.engine.safety import InputGuard, calm_screen_notice

if TYPE_CHECKING:
    from agent_core.api.core import CoreService

# Keys whose values are identifiers, enums or machine values, never something the student wrote.
_MACHINE_KEYS = frozenset({"kind", "type", "side", "action", "status", "category", "mode", "subject", "level",
                           "intent", "source", "purpose", "destination", "unit"})  # fmt: skip


def _machine(key: str) -> bool:
    return key in _MACHINE_KEYS or key.endswith(("_id", "_ids")) or key.startswith("client_")


def written(value: Any) -> list[str]:
    """Every string a student wrote somewhere inside a JSON value: identifiers and enums are left out."""
    if isinstance(value, str):
        return [value]
    if isinstance(value, Mapping):
        return [text for key, item in value.items() if not _machine(str(key)) for text in written(item)]
    if isinstance(value, list):
        return [text for item in value for text in written(item)]
    return []


def field_text(*keys: str) -> Callable[[Any], list[str]]:
    """The strings under these top-level keys of a body."""

    def read(body: Any) -> list[str]:
        if not isinstance(body, Mapping):
            return []
        return [text for key in keys for text in written(body.get(key))]

    return read


def _concept_edit(body: Any) -> list[str]:
    """The names the learner gives concepts on a concept map (rename, merge, add)."""
    ops = body.get("ops") if isinstance(body, Mapping) else None
    return [t for op in ops or [] if isinstance(op, Mapping) for t in written(op.get("name"))]


def _concept_topic(body: Any) -> list[str]:
    """A typed topic, sent to Muse Spark when no pack topic matches."""
    return written(body.get("topic")) if isinstance(body, Mapping) else []


def _study_request(body: Any) -> list[str]:
    """A study request's prompt, and the Generate focus and kept topics that travel with it (STU-21)."""
    if not isinstance(body, Mapping):
        return []
    settings = body.get("settings")
    chosen = settings if isinstance(settings, Mapping) else {}
    return [*written(body.get("prompt")), *written(chosen.get("focus")), *written(chosen.get("topics"))]


def _research_chat(body: Any) -> list[str]:
    """The research conversation's typed topic and the learner's newest reply (SPEC §18.1a)."""
    if not isinstance(body, Mapping):
        return []
    start = body.get("start")
    return [*written(start.get("topic") if isinstance(start, Mapping) else None), *written(body.get("answer"))]


def _research_brief(body: Any) -> list[str]:
    """The approved brief's own words: what the job sends to the gateway (the learner may have edited them)."""
    brief = body.get("brief") if isinstance(body, Mapping) else None
    if not isinstance(brief, Mapping):
        return []
    return [text for key in ("title", "known", "focus", "sub_questions") for text in written(brief.get(key))]


ANSWER = frozenset({"distress"})
# STU-11: the study area's inputs; `off_topic` and `beyond_level` are about requests to the tutor, and a note is not
# one, so a note in either keeps its text (listed for the spec round).
NOTE = frozenset({"distress", "harmful", "sexual_or_violent", "medical_legal_advice", "cheating_request"})


@dataclass(frozen=True)
class Screened:
    surface: str
    texts: Callable[[Any], list[str]]
    refuse: frozenset[str]
    # Where the calm-screen notice goes: a path parameter, or a body field, naming the input's session.
    session_param: str | None = None
    session_field: str | None = None


@dataclass(frozen=True)
class Turn:
    why: str


@dataclass(frozen=True)
class Exempt:
    why: str


Rule = Screened | Turn | Exempt

_TURN = Turn("becomes a tutor turn; the engine's input guard decides it before any model call")
_IDS = Exempt("ids, enums, numbers or flags only")
_NAMES = Exempt("names of things the student picks (display name, avatar, voice); never sent to a model")
_FILES = Exempt("a file path, archive or signed file chosen by the person at the laptop; no free text")
_SECRET = Exempt("a passphrase; it is a secret, never stored or shown, so it is never screened or logged")

ROUTES: dict[str, Rule] = {
    "POST /v1/sessions/{session_id}/end": _IDS,
    "POST /v1/alerts/{alert_id}/dismiss": _IDS,
    "GET /v1/session-notes": Exempt("bounded local search; query is data, never stored or sent to a model"),
    "POST /v1/session-notes/{note_id}/open": _IDS,
    "POST /v1/session-notes/{note_id}/retry": _IDS,
    "PATCH /v1/session-notes/{note_id}": _IDS,
    "DELETE /v1/session-notes/{note_id}": _IDS,
    # M42: fixed study actions accept source data, never instruction prompts. The decoder is sandboxed;
    # generation and independent support checking delimit all material as untrusted records (D87, STU-16).
    "POST /v1/study/sources": Exempt(
        "source data for the M42 sandbox/grounding path; not a request to obey the document"
    ),
    "POST /v1/study/sources/{source_id}/revisions": Exempt("immutable replacement source data, same M42 guards"),
    "PUT /v1/study/sources/{source_id}/chunks/{chunk_index}": Exempt("bounded hashed file bytes; sandbox decoder only"),
    "PATCH /v1/study/sources/{source_id}": _NAMES,
    "POST /v1/study/sources/{source_id}/open": _IDS,
    "POST /v1/study/sources/{source_id}/archive": _IDS,
    "DELETE /v1/study/sources/{source_id}/archive": _IDS,
    "POST /v1/study/session": _IDS,  # the Study screen opens its session of the day (SPEC §19.3.2); an empty body
    "POST /v1/companion": _IDS,  # opens today's episode of the companion's thread (SPEC §19.3.3); an empty body
    "POST /v1/checkins/{commitment_id}/dismiss": _IDS,  # a check-in put away (SPEC §19.3.4); no body
    "POST /v1/checkins/{commitment_id}/open": _IDS,  # the thread opened with a check-in's question; no body
    "POST /v1/study/search": Exempt("read-only bounded local search, never persisted or sent to a model"),
    # A Generate focus and the kept topics reach the model with the request, so they are screened with it (STU-21).
    "POST /v1/study/quizzes/{quiz_id}/items/{item_id}/help": Screened(
        "study_request", _study_request, NOTE | {"off_topic", "personal_data"}
    ),
    "POST /v1/study/jobs": Screened("study_request", _study_request, NOTE | {"off_topic", "personal_data"}),
    # Concept maps (Study): a typed topic may reach Muse Spark; the learner's concept names stay on the map.
    "POST /v1/study/concept-maps": Screened("study_request", _concept_topic, NOTE | {"off_topic", "personal_data"}),
    # The research agent (SPEC §18): the conversation's words and the approved brief go online. Off topic is the
    # tutor's scope, not the pack's (Biology diagrams are in scope); the conversation refuses other subjects itself.
    "POST /v1/study/research/chat": Screened("study_request", _research_chat, NOTE | {"personal_data"}),
    "POST /v1/study/research": Screened("study_request", _research_brief, NOTE | {"personal_data"}),
    "POST /v1/study/research/{research_id}/cancel": _IDS,
    "PATCH /v1/study/concept-maps/{map_id}": Screened("study_request", _concept_edit, NOTE | {"personal_data"}),
    "PATCH /v1/study/aids/{aid_id}/concept-map": Screened("study_request", _concept_edit, NOTE | {"personal_data"}),
    "POST /v1/study/jobs/{job_id}/actions": _IDS,
    # A learning journey from a saved aid: the body is one action of a closed set ("start", "resume",
    # "lesson_reviewed"; StudyJourneyAction forbids any other field), so no learner text reaches it.
    "POST /v1/study/aids/{aid_id}/journey": _IDS,
    # The server prepares the saved passage; the body contains only its index,
    # offset, digest and explicit consent, never client-supplied narration text.
    "POST /v1/study/aids/{aid_id}/neural-speech": _IDS,
    "POST /v1/study/retries/{digest}": _IDS,
    "POST /v1/study/decks/{deck_id}/reviews": _IDS,
    "POST /v1/study/quizzes/{quiz_id}/attempts": Exempt("answers compared with private keys; never sent to a model"),
    "PUT /v1/study/quizzes/{quiz_id}/draft": Exempt("unsubmitted answers kept for the learner; never marked or sent"),
    "PATCH /v1/study/decks/{deck_id}/cards/{card_id}": Exempt(
        "citation validation and independent source/safety support check before publishing"
    ),
    # The one Home input (UXS-67) and Ask anything (STU-20): routing reads the words locally and keeps nothing; the
    # question to Ask anything is guarded here before it is routed, searched or sent (PRV-13, STU-11).
    "POST /v1/ask/route": Exempt("read-only local routing of the box's words; never stored or sent to a model"),
    "POST /v1/ask": Screened("ask_anything", field_text("question"), NOTE | {"personal_data"}),
    # tutor turns (AGT-04, AGT-17, AGT-18, API-14, VOC-32)
    "POST /v1/sessions/{session_id}/turns": _TURN,
    "POST /v1/turns/{turn_id}/steer": Turn(
        "queued steering is injected only when the guard admits it, else carried"
        " to start the next turn, where the guard answers it (AGT-17)"
    ),
    "POST /v1/sessions/{session_id}/side-questions": Turn("the side fork runs the full guard first (AGT-18)"),
    "POST /v1/cards/{request_id}/answer": Turn(
        "a typed card answer starts the next turn (API-14); the plan card takes an action and option ids only"
    ),
    "POST /v1/voice/llm/chat/completions": Turn("each utterance runs the full tutor turn of its session (VOC-32)"),
    "WS /v1/sessions/{session_id}/voice": Turn("audio becomes a transcript the adapter's turn guards (VOC-20)"),
    "POST /v1/captures": Turn("the capture's text is kept whole for the guard and becomes a turn when sent (INP-39)"),
    "POST /v1/captures/{capture_id}/confirm": Turn(
        "the confirmed question becomes a turn when sent; photo text is "
        "kept whole when the guard would answer it (INP-39, PRV-13)"
    ),
    "PUT /v1/sessions/{session_id}/journal": Exempt(
        "cache of already screened public transcript events; never fed to a model"
    ),
    "DELETE /v1/sessions/{session_id}/journal": Exempt("deletes a profile-owned cache"),
    "PUT /v1/sessions/{session_id}/drafts/{slot}": Turn("an unsent composer draft; it is guarded when sent as a turn"),
    # free text outside a turn, guarded here
    "POST /v1/sessions/{session_id}/study/notes": Screened(
        "study_note", field_text("text"), NOTE, session_param="session_id"
    ),
    "POST /v1/sessions/{session_id}/voice/dictations/{identity}/confirm": Screened(
        "dictation", field_text("text"), NOTE, session_param="session_id"
    ),
    "POST /v1/practice/sets/{identity}/answers": Screened("practice_answer", field_text("answer"), ANSWER),
    "POST /v1/assessments/{identity}/answers": Screened("assessment_answer", field_text("answer"), ANSWER),
    # Explain it back: the learner's own explanation of a KC (typed, or the transcript of what they said).
    "POST /v1/assessments/{identity}/explain": Screened("explain_back", field_text("text"), ANSWER),
    "PUT /v1/mocks/{identity}/answers/{question_id}": Screened("mock_answer", field_text("answer"), ANSWER),
    "POST /v1/lessons/{lesson_id}/checks": Screened("lesson_check", field_text("answer"), ANSWER),
    "PATCH /v1/sessions/{session_id}": Screened(
        "session_title", field_text("title"), ANSWER, session_param="session_id"
    ),
    "POST /v1/problem-reports": Screened("problem_report", field_text("note"), frozenset(), session_field="session_id"),
    # a value set on a manipulable visual becomes the student's board line, read by the Coach later (VSL-42)
    "POST /v1/visuals/{visual_id}/practice": _IDS,
    "POST /v1/visuals/{visual_id}/answer": Screened("manipulation_answer", field_text("values"), ANSWER),
    # VSL-47: a prediction or its explanation becomes a tutor turn, whose intake screens the words (Engine._intake).
    "POST /v1/sessions/{session_id}/visuals/{visual_id}/prediction": Turn("the engine's intake screens it as a turn"),
    # the student's own words for a goal (UXS-10 interview): kept in the profile like a note (STU-11)
    "POST /v1/interview/{question_id}/answer": Screened("interview_goal", field_text("answer"), NOTE),
    # no student-written text
    "POST /v1/sessions": _IDS,
    "POST /v1/sessions/{session_id}/archive": _IDS,
    "PUT /v1/sessions/{session_id}/selection": Exempt("a board term or visual part named by ids only (UXS-58)"),
    "POST /v1/turns/{turn_id}/cancel": _IDS,
    "POST /v1/probes/{identity}/answers": Exempt("a probe answer is one option id"),
    "POST /v1/assessments": _IDS,
    "POST /v1/assessments/{identity}/stop": _IDS,
    "POST /v1/assessments/{identity}/resume": _IDS,
    "POST /v1/practice/sets": _IDS,
    "POST /v1/learning-journey": _IDS,
    "POST /v1/learning-journey/step": _IDS,
    "POST /v1/practice/sets/{identity}/help": _IDS,
    "POST /v1/practice/sets/{identity}/reveal": _IDS,
    "POST /v1/mocks": _IDS,
    "POST /v1/past-papers": Exempt(
        "source ids, a subject, minutes and the paper's title; the paper's own words are "
        "source data the study area already holds, never instructions (ASM-27)"
    ),
    "DELETE /v1/past-papers/{paper_id}": _IDS,
    "POST /v1/exam-dates": _NAMES,
    "PATCH /v1/exam-dates/{exam_id}": _NAMES,
    "DELETE /v1/exam-dates/{exam_id}": _IDS,
    "POST /v1/mocks/{identity}/{action}": _IDS,
    "PUT /v1/lessons/{lesson_id}/position": _IDS,
    "POST /v1/plans": _IDS,
    "POST /v1/plans/{plan_id}/change": _IDS,
    "POST /v1/saved-questions": Exempt("session and turn ids; the snapshot is taken from the already guarded turn"),
    "PATCH /v1/saved-questions/{record_id}": _IDS,
    "POST /v1/profiles/{profile_id}/memory/reviews/{review_id}": _IDS,
    "POST /v1/profiles/{profile_id}/memory/{note_id}/dispute": _IDS,
    "POST /v1/profiles/{profile_id}/misconceptions/{misconception_id}/correction": _IDS,
    "PATCH /v1/profiles/{profile_id}/preferences/{key}": Exempt("one value of a closed set per preference key"),
    "POST /v1/profiles": _NAMES,
    "PATCH /v1/profiles/{profile_id}": _NAMES,
    "POST /v1/profiles/{profile_id}/open": _IDS,
    "POST /v1/profiles/{profile_id}/close": _IDS,
    "PATCH /v1/profiles/{profile_id}/settings": _NAMES,
    "PUT /v1/profiles/{profile_id}/consents/{scope}": _IDS,
    "PATCH /v1/connectivity": _IDS,
    "POST /v1/connectivity/send-now": _IDS,
    "POST /v1/subscription": _FILES,
    "POST /v1/profiles/{profile_id}/archive-authorizations": _IDS,
    "POST /v1/exports": _SECRET,
    "POST /v1/profiles/import": _SECRET,
    "POST /v1/sessions/{session_id}/support-share": _IDS,
    "POST /v1/crash-reports/{crash_id}/{action}": _IDS,
    "POST /v1/sessions/{session_id}/voice/utterances": Exempt("names text the device already holds (VOC-18)"),
    "POST /v1/sessions/{session_id}/voice/utterances/{identity}/playback": _IDS,
    "POST /v1/sessions/{session_id}/voice/dictations": _IDS,
    "PATCH /v1/captures/{capture_id}/crop": _IDS,
    "POST /v1/captures/{capture_id}/read": _IDS,
    "POST /v1/operations/catalogue": _FILES,
    "POST /v1/updates/install": _FILES,
    "POST /v1/updates/{action}": _IDS,
    "POST /v1/licence": _FILES,
    "PUT /v1/setup": _IDS,
    "POST /v1/packs/install": _FILES,
    "POST /v1/models/{model_id}/download": _IDS,
    "POST /v1/models/downloads/{download_id}/{action}": _IDS,
    "PUT /v1/models/assignment": _IDS,
    "PUT /v1/models/downloads/{download_id}/limits": _IDS,
    "POST /v1/models/benchmark": _IDS,
    "PATCH /v1/study/notes/{note_id}": Exempt("marks a note private or not: one flag (D102)"),
    "POST /v1/study/notes/{note_id}/open": Exempt("opens a private note after the confirm step: one flag (D102)"),
    "POST /v1/sessions/{session_id}/idle": Exempt("a step's idle time and window focus: a number and a flag (D104)"),
    # text queries: searched locally, never stored and never sent to a model
    "GET /v1/search/items": Exempt("a search query over installed items; not stored, not sent to a model"),
    "GET /v1/study/notes": Exempt("a search over the student's own notes; not stored, not sent to a model"),
}


@dataclass(frozen=True)
class Outcome:
    category: str
    refused: bool
    calm_screen: bool


def screen(core: CoreService, key: str, path_params: Mapping[str, str], body: Any) -> Outcome | None:
    """Run the input guard for one command, before it touches anything. Raises `validation_failed` with reason
    `safety_template` when the surface refuses the input; otherwise returns what the guard found (None when the route
    is not screened here or has no text)."""
    rule = ROUTES.get(key)
    if not isinstance(rule, Screened):
        return None
    texts = [text for text in rule.texts(body) if text.strip()]
    if not texts:
        return None
    result = InputGuard(core.settings).check(normalise_input("\n".join(texts)))
    if result.failed:  # the guard could not run: fail closed, nothing is stored or marked (PRV-01a item 9)
        raise ApiError("internal_error", "the safety check could not run", details={"reason": "safety_unavailable"})
    distress = result.category == "distress"
    refused = result.category in rule.refuse
    if (refused or distress) and result.template is not None:
        _flag(core, rule, path_params, body, result.category, result.template)
    if refused:
        raise ApiError("validation_failed", "this input is answered by a safety template",
                       details={"reason": "safety_template", "template_id": result.template,
                                "calm_screen": distress})  # fmt: skip
    return Outcome(result.category, refused, distress)


def _flag(core: CoreService, rule: Screened, path_params: Mapping[str, str], body: Any, category: str,
          template: str) -> None:  # fmt: skip
    """`safety_flagged` with the category and template only, never the text (PRV-14), and the calm screen on the
    input's session when it has one."""
    session = path_params.get(rule.session_param) if rule.session_param else None
    if session is None and rule.session_field and isinstance(body, Mapping):
        found = body.get(rule.session_field)
        session = found if isinstance(found, str) else None
    entry = core.sessions.get(session) if session else None
    if entry is not None and entry.profile_id != CURRENT_PROFILE.get():
        entry = None  # never another profile's session (API-04)
    own = core.activator is None or core.records_profile == CURRENT_PROFILE.get()
    if core.records is not None and own:  # only ever into the requesting profile's learner database (DAT-14)
        core.records.safety_flagged(category, template, entry.session_id if entry is not None else None)
    if category == "distress" and entry is not None:
        core._announce(entry.session_id, calm_screen_notice(new_id("ntc")))
