"""M42 local source workspace. Revisions and jobs are durable. What the learner does here is recorded as
`study_activity` learning events (SPEC §19.3.2, W8) through `self.activity` (`engine.study_session.StudyVisits`),
each after Study's own transaction has committed: activity for the Reflector and the knows view, never mastery,
review or misconception evidence (JM-D26)."""

from __future__ import annotations

import asyncio
import base64
import contextlib
import datetime as dt
import hashlib
import itertools
import json
import re
import secrets
import sqlite3
import unicodedata
from collections.abc import Callable, Iterator
from dataclasses import replace
from pathlib import Path
from typing import Any, cast

from agent_core.contracts import errors
from agent_core.engine import pedagogy, study_concepts, study_outline
from agent_core.engine.events import new_id
from agent_core.engine.safety import LexiconClassifier, OutputCritic, redact_personal
from agent_core.engine.study_links import TranscriptTooLong, parse_transcript, parse_youtube, youtube_url
from agent_core.engine.study_topics import material_topics
from agent_core.learner.fsrs import Card, scheduler_of
from agent_core.learner.mastery import MasteryParams
from agent_core.prompts.untrusted import screened
from agent_core.router import RouterError
from agent_core.store.sqlite import transaction
from agent_core.tools.study_document import DocumentError, confident_text, extract, raster

PREFIX = "study/workspace#/$defs/"
TERMINAL = ("completed", "failed", "cancelled")
# Plain text a university course hands out: LaTeX, data and code. Read as text like .txt (study_worker goes by bytes).
TEXT_SUFFIXES = (".tex", ".bib", ".rst", ".csv", ".tsv", ".py", ".r", ".m", ".jl", ".java", ".c", ".h", ".cpp", ".cs",
                 ".js",
                 ".ts", ".go", ".rs", ".sql", ".ipynb", ".json", ".yaml", ".yml", ".html")  # fmt: skip
LONG_SOURCE_BATCHES = 24  # sections one aid reads of a long source with no focus: spread evenly across it
ONLINE_MARKED = ("fill_blank", "free_response", "numeric", "expression")
SAMPLED_KINDS = ("summary", "notes", "flashcards", "quiz", "concept_map", "audio_overview")
RETRIED_KINDS = (
    "summary",
    "notes",
    "flashcards",
    "quiz",
    "concept_map",
    "teaching_outline",
    "chapters",
    "audio_overview",
)
MAX_CHUNK = 1024 * 1024
ONLINE_RETRY_MS = 30_000
SCHEDULER = "fsrs-6-source-v1"
# STU-21: the Generate panel settings each tool takes; anything else is refused rather than silently ignored.
SETTINGS = {
    "quiz": {"question_types", "difficulty", "topics", "focus", "syllabus"},
    "flashcards": {"topics", "focus"},
    "notes": {"topics", "focus"},
    "summary": {"topics", "focus"},
    "audio_overview": {"podcast_length", "voices"},
    "lesson_plan": {"minutes_available", "ready_by", "topics", "focus"},
}
# A short podcast is a few spoken points; a standard one is still bounded (STU-21).
PODCAST_CLAIMS = {"short": 6, "standard": 20}
CHOICE_LIKE = ("choice", "multi_select")
BLANK = re.compile(r"_{3,}")  # a fill-in-the-blank stem's one blank


def media_type(data: bytes) -> str:
    """A recording's media type from its own leading bytes, so the player never trusts a file name."""
    head = data[:16]
    if head[:4] == b"RIFF" and head[8:12] == b"WAVE":
        return "audio/wav"
    if head[:3] == b"ID3" or (len(head) > 1 and head[0] == 0xFF and head[1] & 0xE0 == 0xE0):
        return "audio/mpeg"
    if head[4:8] == b"ftyp":
        return "video/mp4"
    if head[:4] == b"OggS":
        return "audio/ogg"
    if head[:4] == b"fLaC":
        return "audio/flac"
    if head[:4] == b"\x1aE\xdf\xa3":
        return "video/webm"
    return "application/octet-stream"


def chaptered(output: Any) -> None:
    """A chapters draft's claims are its chapter summaries, so the support check reads every sentence (STU-22)."""
    if isinstance(output, dict) and output.get("kind") == "chapters" and isinstance(output.get("chapters"), list):
        output["claims"] = [
            s for c in output["chapters"] if isinstance(c, dict) for s in c.get("summary", []) if isinstance(s, dict)
        ]


def outlined(output: Any) -> None:
    """A teaching outline's claims are its items in order (SPEC §20.3): objectives, examples, concepts, assumes, so
    the support check and the aid's claims map to them one to one."""
    if (
        isinstance(output, dict)
        and output.get("kind") == "teaching_outline"
        and isinstance(output.get("outline"), dict)
    ):
        output["claims"] = study_outline.flatten(output["outline"])


def chat_evidence(
    batches: list[list[dict[str, Any]]],
    outputs: list[dict[str, Any]],
    limit: int,
    *,
    opening_lesson: bool = False,
    prompt: str = "",
) -> tuple[list[dict[str, Any]], dict[str, int]]:
    """Bound the final answer's context with original text, never generated prose as evidence.

    Keep the opening section intact for introductory explanations and worked examples, then
    take supporting excerpts round-robin across verified sections. Offsets are rebased only
    in this transient request; saved citations always point into the original frozen chunks.
    """
    ordered = [c for batch in batches for c in batch]
    # Front matter can fill an entire batch. Keep the actual opening teaching section
    # whole, including examples beyond its first citation-sized paragraph. A contents
    # entry with dot leaders/page numbers is not a standalone chapter heading.
    opening = (
        next(
            (
                i
                for i, c in enumerate(ordered)
                if re.search(
                    r"(?im)^\s*(?:chapter|unit|lesson)\s+(?:1|one|i)(?:\s*[:.\-\u2013\u2014]\s*[^.\n]+)?\s*$", c["text"]
                )
            ),
            0,
        )
        if opening_lesson
        else 0
    )
    originals = {c["chunk_id"]: c for c in ordered[opening:]}
    selected: dict[str, tuple[int, int]] = {}
    used = 0
    # A targeted question needs the surrounding definitions and qualifications, not just
    # one citation-sized slice. Otherwise front matter consumes the full-page budget and
    # a later equation or its explanation can be cut off halfway through a sentence.
    terms = chat_terms(prompt)
    if terms and not opening_lesson:
        ranked = sorted(
            originals.values(),
            key=lambda c: len(terms.intersection(re.findall(r"\w{3,}", c["text"].casefold()))),
            reverse=True,
        )
        for chunk in ranked:
            if not terms.intersection(re.findall(r"\w{3,}", chunk["text"].casefold())):
                break
            if used + len(chunk["text"]) <= limit // 2 and len(selected) < 64:
                selected[chunk["chunk_id"]] = (0, len(chunk["text"]))
                used += len(chunk["text"])
    for chunk in originals.values():
        if chunk["chunk_id"] in selected:
            continue
        if selected and (used + len(chunk["text"]) > limit // 2 or len(selected) == 64):
            break
        selected[chunk["chunk_id"]] = (0, len(chunk["text"]))
        used += len(chunk["text"])
    remaining = limit - sum(end - start for start, end in selected.values())
    candidates = []
    for output in outputs:
        ranges: dict[str, tuple[int, int]] = {}
        for claim in output["claims"]:
            for citation in claim["citations"]:
                identity = citation["chunk_id"]
                start, end = ranges.get(identity, (citation["start"], citation["end"]))
                ranges[identity] = (min(start, citation["start"]), max(end, citation["end"]))
        candidates.append(list(ranges.items()))
    for row in itertools.zip_longest(*candidates):
        for candidate in row:
            if candidate is None:
                continue
            identity, (start, end) = candidate
            if identity in originals and identity not in selected and end - start <= remaining and len(selected) < 64:
                selected[identity] = (start, end)
                remaining -= end - start
    excerpts, offsets = [], {}
    for identity, original in originals.items():
        if identity in selected:
            start, end = selected[identity]
            text = original["text"][start:end]
            excerpts.append({**original, "text": text, "sha256": digest(text.encode())})
            offsets[identity] = start
    return excerpts, offsets


def chat_terms(prompt: str) -> set[str]:
    return set(re.findall(r"\w{3,}", prompt.casefold())) - {
        "the",
        "and",
        "for",
        "from",
        "this",
        "that",
        "with",
        "what",
        "which",
        "how",
        "why",
        "does",
        "can",
        "could",
        "would",
        "should",
        "about",
        "your",
        "these",
        "those",
        "using",
        "versus",
        "compare",
        "show",
        "give",
        "cite",
        "relevant",
        "pages",
        "teach",
        "beginning",
        "explain",
        "first",
        "example",
        "material",
        "notes",
        "references",
        "language",
        "friendly",
        "start",
        "explanations",
        "rather",
        "include",
        "source",
        "question",
    }


def chat_source_notes(identity: str, request: dict[str, Any], chunks: list[dict[str, Any]]) -> dict[str, Any]:
    """Select literal evidence, not a model-written lesson, from every source chunk.

    The final answer has its own independent support check. There is no reason to
    generate and validate display-ready equations for intermediate retrieval notes.
    """
    terms = chat_terms(request.get("prompt", ""))
    claims = []
    for chunk in chunks:
        text = chunk["text"]
        spans = []
        start = 0
        while start < len(text):
            end = min(len(text), start + 500)
            if end < len(text):
                boundary = text.rfind(" ", start + 250, end)
                if boundary > start:
                    end = boundary
            score = len(terms.intersection(re.findall(r"\w{3,}", text[start:end].casefold())))
            spans.append((score, start, end))
            start = end
        _, start, end = max(spans, key=lambda span: (span[0], -span[1]))
        claims.append(
            {
                "text": "Source excerpt.",
                "citations": [
                    {
                        "source_id": chunk["source_id"],
                        "revision": chunk["revision"],
                        "chunk_id": chunk["chunk_id"],
                        "start": start,
                        "end": end,
                        "quote": text[start:end],
                    }
                ],
            }
        )
    return {"request_id": identity, "kind": "notes", "claims": claims}


def item_text(item: dict[str, Any], key: dict[str, Any]) -> str:
    """A quiz item's words for its topic tags (SPEC §19.3.2): the stem, the key (an option's text for a choice) and
    the key's explanation. Used locally to choose tags; never stored or sent."""
    options = {o["option_id"]: o["text"] for o in item.get("options", [])}
    answer = str(key.get("answer", ""))
    keyed = [options.get(a, a) for a in answer_ids(answer)] if options else [answer]
    stem = item.get("stem", {}).get("text", "")
    explanation = key.get("explanation", {}).get("text", "") if isinstance(key.get("explanation"), dict) else ""
    return "\n".join([str(stem), *map(str, keyed), str(explanation)])


def answer_ids(answer: str) -> list[str]:
    """A multi-select answer: comma-separated option ids, order-free."""
    return sorted({part.strip() for part in answer.split(",") if part.strip()})


def blank_form(text: str) -> str:
    """A fill-in-the-blank answer compared literally: case, spacing and edge punctuation aside."""
    return " ".join(unicodedata.normalize("NFKC", text).casefold().strip(" .,;:!?\"'").split())


def valid_blank(item: dict[str, Any], key: dict[str, Any]) -> bool:
    """A usable blank has one gap and never prints its own grading answer in the stem."""
    answer = blank_form(key["answer"])
    return (
        len(BLANK.findall(item["stem"]["text"])) == 1
        and bool(answer)
        and len(key["answer"]) <= 120
        and re.search(r"\b" + re.escape(answer) + r"\b", blank_form(item["stem"]["text"])) is None
    )


class StudyError(ValueError):
    def __init__(self, reason: str, conflict: bool = False):
        super().__init__(reason)
        self.reason, self.conflict = reason, conflict


def checked[T](name: str, body: T) -> T:
    if errors(name if "#" in name else PREFIX + name, body):  # a def of study/workspace, or a full reference
        raise StudyError("invalid_study_request")
    return body


def digest(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def stamp(ms: int) -> str:
    return dt.datetime.fromtimestamp(ms / 1000, dt.UTC).isoformat(timespec="milliseconds").replace("+00:00", "Z")


def epoch(value: str) -> int:
    return int(dt.datetime.fromisoformat(value.replace("Z", "+00:00")).timestamp() * 1000)


def normalise(text: str) -> str:
    clean = redact_personal(
        unicodedata.normalize("NFC", text.replace("\r\n", "\n").replace("\r", "\n").replace("\x00", "")).strip()
    )[0]
    # Sources are uploaded data, even when pasted or imported from a note. Freeze the screened text BEFORE
    # chunk ids, hashes and citation offsets are minted; never modify a cited revision later.
    return screened(clean, "upload")[0]


@contextlib.contextmanager
def atomic(conn: sqlite3.Connection) -> Iterator[None]:
    """Allow an API command and its exactly-once response to commit with the store's own writes."""
    if not conn.in_transaction:
        with transaction(conn):
            yield
        return
    conn.execute("SAVEPOINT study_write")
    try:
        yield
        conn.execute("RELEASE study_write")
    except BaseException:
        conn.execute("ROLLBACK TO study_write")
        conn.execute("RELEASE study_write")
        raise


def located(output: Any, chunks: list[dict[str, Any]]) -> None:
    """Citation offsets from the quote: where a model's start/end miss its exact quote, the quote's first place in
    its chunk is used. The quote must still be the chunk's text word for word (`StudyDesk.citation` checks it); only
    the code-point arithmetic is the engine's, which a local model often gets wrong (STU-20)."""
    texts = {c["chunk_id"]: c["text"] for c in chunks}
    if isinstance(output, list):
        for value in output:
            located(value, chunks)
    elif isinstance(output, dict):
        text = texts.get(output.get("chunk_id")) if isinstance(output.get("quote"), str) else None
        if text is not None and isinstance(output.get("start"), int) and isinstance(output.get("end"), int):
            quote = output["quote"]
            if text[output["start"] : output["end"]] != quote and quote and (at := text.find(quote)) >= 0:
                output["start"], output["end"] = at, at + len(quote)
        for value in output.values():
            located(value, chunks)


class StudyDesk:
    def __init__(self, conn: sqlite3.Connection, clock: Callable[[], int], settings: Any):
        self.conn, self.clock, self.settings = conn, clock, settings
        # The open profile's Study session and activity recorder (`StudyVisits`), when the core attached one.
        self.activity: Any = None
        file = next((r[2] for r in conn.execute("PRAGMA database_list") if r[1] == "main"), "")
        if not file:
            raise StudyError("storage_full")
        self.root = Path(file).parent / "study"

    def recover_files(self) -> None:
        """Collect orphan files and expired incomplete staging, preserving recent resumable uploads."""
        terminal_cutoff = self.clock() - 30 * 86400000
        self.conn.execute(
            "DELETE FROM study_jobs WHERE state IN ('completed','failed','cancelled') AND updated_at<?",
            (terminal_cutoff,),
        )
        staging = self.root / "staging"
        if staging.exists():
            for file in staging.iterdir():
                if not file.is_file() or not re.fullmatch(r"src_[0-9A-HJKMNP-TV-Z]{26}-[1-9][0-9]*", file.name):
                    continue
                if self.clock() - int(file.stat().st_mtime * 1000) <= self.settings.study_upload_staging_ttl_s * 1000:
                    continue
                sid, revision = file.name.rsplit("-", 1)
                with atomic(self.conn):
                    pending = self.conn.execute(
                        "SELECT revision FROM study_pending_revisions WHERE source_id=?", (sid,)
                    ).fetchone()
                    if pending and pending[0] == int(revision):
                        self.conn.execute("DELETE FROM study_pending_revisions WHERE source_id=?", (sid,))
                        self.conn.execute(
                            "DELETE FROM study_revisions WHERE source_id=? AND revision=?", (sid, int(revision))
                        )
                    else:
                        with contextlib.suppress(StudyError):
                            source = self.source(sid)
                            if source["revision"] == int(revision) and source["state"] == "uploading":
                                source["state"] = "failed"
                                self.save_source(source)
                file.unlink(missing_ok=True)
        blobs = {
            r[0] for r in self.conn.execute("SELECT DISTINCT blob_ref FROM study_revisions WHERE blob_ref IS NOT NULL")
        }
        folder = self.root / "blobs"
        if folder.exists():
            for file in folder.iterdir():
                if re.fullmatch(r"[0-9a-f]{64}", file.name) and file.name not in blobs and file.is_file():
                    file.unlink(missing_ok=True)
        uploads = {
            f"{sid}-{revision}"
            for sid, revision in self.conn.execute(
                "SELECT r.source_id,r.revision FROM study_revisions r JOIN study_sources s USING(source_id) "
                "WHERE r.blob_ref IS NULL AND ((r.revision=s.current_revision "
                "AND json_extract(s.body,'$.state')='uploading') OR r.revision IN "
                "(SELECT revision FROM study_pending_revisions p WHERE p.source_id=s.source_id))"
            )
        }
        folder = self.root / "staging"
        if folder.exists():
            for file in folder.iterdir():
                if (
                    re.fullmatch(r"src_[0-9A-HJKMNP-TV-Z]{26}-[1-9][0-9]*", file.name)
                    and file.name not in uploads
                    and file.is_file()
                ):
                    file.unlink(missing_ok=True)

    def one(self, sql: str, args: tuple[Any, ...]) -> dict[str, Any]:
        row = self.conn.execute(sql, args).fetchone()
        if row is None:
            raise StudyError("not_found")
        return dict(json.loads(row[0]))

    def pending_source(self, sid: str) -> dict[str, Any]:
        row = self.conn.execute("SELECT body FROM study_pending_revisions WHERE source_id=?", (sid,)).fetchone()
        return {**json.loads(row[0]), "private": self.source(sid)["private"]} if row else self.source(sid)

    def source(self, sid: str) -> dict[str, Any]:
        return self.one("SELECT body FROM study_sources WHERE source_id=?", (sid,))

    def library(self) -> dict[str, list[str]]:
        return {
            "archived_source_ids": [
                r[0] for r in self.conn.execute("SELECT source_id FROM study_library_archive ORDER BY source_id")
            ]
        }

    def archive_source(self, sid: str, archived: bool) -> dict[str, list[str]]:
        self.source(sid)  # Only a source belonging to this learner can be organised.
        if archived:
            self.conn.execute("INSERT OR IGNORE INTO study_library_archive VALUES (?)", (sid,))
        else:
            self.conn.execute("DELETE FROM study_library_archive WHERE source_id=?", (sid,))
        return self.library()

    def public(self, source: dict[str, Any]) -> dict[str, Any]:
        return (
            {k: source[k] for k in ("source_id", "revision", "kind", "private", "created_at", "updated_at")}
            if source["private"]
            else source
        )

    def sources(self, cursor: str | None = None, limit: int = 100) -> dict[str, Any]:
        if not 1 <= limit <= 100:
            raise StudyError("invalid_study_request")
        rows = self.conn.execute(
            "SELECT source_id,body FROM study_sources WHERE source_id>? ORDER BY source_id LIMIT ?",
            (cursor or "", limit + 1),
        ).fetchall()
        return {
            "items": [self.public(json.loads(r[1])) for r in rows[:limit]],
            "next_cursor": rows[limit - 1][0] if len(rows) > limit else None,
        }

    def save_source(self, source: dict[str, Any]) -> None:
        checked("Source", source)
        self.conn.execute(
            "UPDATE study_sources SET current_revision=?,private=?,body=?,updated_at=? WHERE source_id=?",
            (source["revision"], int(source["private"]), json.dumps(source), self.clock(), source["source_id"]),
        )

    def create(self, body: dict[str, Any]) -> dict[str, Any]:
        checked("SourceCreate", body)
        sid, now = new_id("src"), stamp(self.clock())
        kind = body["kind"]
        private = False
        extra: dict[str, Any] = {}
        transcript = None
        if kind in ("session_note", "dictated_note", "board_note"):
            if kind == "session_note":
                row = self.conn.execute(
                    "SELECT title,statements,private FROM session_notes WHERE note_id=? AND state='ready'",
                    (body["note_id"],),
                ).fetchone()
                if row is None:
                    raise StudyError("not_found")
                title, statements, private = row
                text = "\n\n".join(s["text"] for s in json.loads(statements))
            else:
                row = self.conn.execute(
                    "SELECT text,private FROM study_notes WHERE note_id=?", (body["note_id"],)
                ).fetchone()
                if row is None:
                    raise StudyError("not_found")
                text, private = row
                title = text[:80]
            if private and body.get("confirm_private") is not True:
                raise StudyError("confirm_needed")
            private = bool(private)
        elif kind == "text":
            title, text = body["title"], body["text"]
        elif kind == "video_link":
            # SPEC §20.4: a YouTube link and the transcript the learner pasted (JM-D30); ready at once, no extraction.
            video = parse_youtube(body["url"])
            if video is None:
                raise StudyError("unsupported_link")
            try:
                transcript = parse_transcript(body["transcript"])
            except TranscriptTooLong:
                raise StudyError("transcript_too_long") from None
            title = body.get("title") or f"YouTube video {video}"
            text = (
                transcript.text
                if transcript.kind == "plain"
                else "\n\n".join(str(c["text"]) for c in transcript.chunks)
            )
            extra["link"] = {"provider": "youtube", "video_id": video, "url": youtube_url(video)}
            extra["transcript_kind"] = transcript.kind
            if transcript.chapters:
                extra["chapters"] = [
                    {k: c[k] for k in ("start_ms", "end_ms", "title")} for c in transcript.chapters[:60]
                ]
        else:
            title, text = body["file_name"], None
            self.accept_file(kind, title)
            if body["byte_length"] > self.settings.study_source_max_mb * 1048576:
                raise StudyError("source_too_large")
            if kind == "video":
                extra["frames_choice"] = body.get("frames", "auto")
        title = (self.file_title(title) if text is None else redact_personal(title)[0]).strip()[:200]
        if not title:
            raise StudyError("invalid_study_request")
        data = normalise(text).encode() if text is not None else None
        if data is not None and not data:
            raise StudyError("unreadable_source")
        sha = digest(data) if data is not None else body["sha256"]
        source = {
            "source_id": sid,
            "revision": 1,
            "kind": kind,
            "title": title,
            "state": "uploading" if data is None else "ready",
            "created_at": now,
            "updated_at": now,
            "private": private,
            "readable_units": 0,
            "unreadable_units": [],
            "sha256": sha,
            **extra,
        }
        checked("Source", source)
        with atomic(self.conn):
            self.conn.execute(
                "INSERT INTO study_sources VALUES (?,?,?,?,?,?)",
                (sid, 1, int(private), json.dumps(source), self.clock(), self.clock()),
            )
            self.conn.execute(
                "INSERT INTO study_revisions VALUES (?,?,?,?,?,?)",
                (sid, 1, sha, None, len(data) if data is not None else body["byte_length"], None),
            )
            if data is not None:
                self.commit_units(source, self.transcript_units(transcript, data.decode(), body))
        # SPEC §19.3.2, §20.3: adding a source is a Study activity (its ref the source, its tags the title's topics).
        if self.activity is not None:
            with contextlib.suppress(Exception):
                self.activity.source_added(sid, f"{title}\n{text or ''}")
        return self.source(sid)

    @staticmethod
    def transcript_units(transcript: Any, text: str, body: dict[str, Any]) -> list[dict[str, Any]]:
        """The units a pasted source commits: a timed transcript's chunks with their time locators (each line keeps
        its `[m:ss]` moment in the text), else one text or note unit."""
        if transcript is not None and transcript.kind == "timed":
            return [
                {"text": str(c["text"]), "locator": {"kind": "time", "start_ms": int(c["start_ms"]),
                                                      "end_ms": int(c["end_ms"])}}
                for c in transcript.chunks
            ]  # fmt: skip
        return [
            {
                "text": text,
                "locator": {"kind": "note", "note_id": body["note_id"], "paragraph": 1}
                if "note_id" in body
                else {"kind": "text", "paragraph": 1},
            }
        ]

    @staticmethod
    def file_title(name: str) -> str:
        title = redact_personal(name)[0]
        suffix = Path(name).suffix.lower()
        return title if title.lower().endswith(suffix) else title + suffix

    @staticmethod
    def accept_file(kind: str, name: str) -> None:
        allowed = {
            "pdf": (".pdf",),
            "document": (".txt", ".md", ".docx", ".odt", *TEXT_SUFFIXES),
            "slides": (".pptx", ".odp"),
            "photo": (".png", ".jpg", ".jpeg", ".webp"),
            "recording": (".wav", ".mp3", ".m4a", ".mp4", ".webm", ".ogg", ".flac", ".aac", ".mov", ".mkv", ".avi"),
            # SPEC §20.4: a video file is kind `video` by suffix at create (an older client still sends `recording`).
            "video": (".mp4", ".webm", ".mov", ".mkv", ".avi"),
        }
        if kind not in allowed or Path(name).suffix.lower() not in allowed[kind]:
            raise StudyError("unsupported_format")

    def staging(self, sid: str, revision: int) -> Path:
        checked("SourceId", sid)
        path = self.root / "staging" / f"{sid}-{revision}"
        path.parent.mkdir(parents=True, exist_ok=True)
        return path

    def upload(self, sid: str, index: int, body: dict[str, Any]) -> dict[str, Any]:
        checked("UploadChunk", body)
        source = self.pending_source(sid)
        if index < 0 or body["offset"] != index * MAX_CHUNK:
            raise StudyError("source_stale", True)
        try:
            data = base64.b64decode(body["data_base64"], validate=True)
        except ValueError:
            raise StudyError("corrupt_source") from None
        length = self.conn.execute(
            "SELECT byte_length FROM study_revisions WHERE source_id=? AND revision=?", (sid, source["revision"])
        ).fetchone()[0]
        if not data or len(data) > MAX_CHUNK or digest(data) != body["sha256"] or body["offset"] + len(data) > length:
            raise StudyError("corrupt_source")
        if body["offset"] + len(data) < length and len(data) != MAX_CHUNK:
            raise StudyError("corrupt_source")
        if source["state"] != "uploading":
            row = self.conn.execute(
                "SELECT blob_ref FROM study_revisions WHERE source_id=? AND revision=?", (sid, source["revision"])
            ).fetchone()
            blob = self.root / "blobs" / row[0] if row and row[0] else None
            if blob is None or not blob.exists():
                raise StudyError("source_stale", True)
            with blob.open("rb") as stored:
                stored.seek(body["offset"])
                if stored.read(len(data)) != data:
                    raise StudyError("upload_conflict", True)
            return {"received_bytes": length, "complete": True}
        path = self.staging(sid, source["revision"])
        offset = body["offset"]
        size = path.stat().st_size if path.exists() else 0
        if offset > size:
            raise StudyError("upload_out_of_order", True)
        with path.open("r+b" if path.exists() else "w+b") as file:
            file.seek(offset)
            prefix = file.read(len(data)) if size > offset else b""
            if not data.startswith(prefix):
                raise StudyError("upload_conflict", True)
            # A power cut can leave part of this chunk on disk. Reuse its verified prefix and finish the chunk.
            file.seek(offset + len(prefix))
            file.write(data[len(prefix) :])
            file.flush()
            import os

            os.fsync(file.fileno())
        received = path.stat().st_size
        if received == length:
            if digest(path.read_bytes()) != source["sha256"]:
                path.unlink()
                raise StudyError("corrupt_source")
            blob = self.root / "blobs" / source["sha256"]
            blob.parent.mkdir(parents=True, exist_ok=True)
            path.replace(blob)
            with atomic(self.conn):
                self.conn.execute(
                    "UPDATE study_revisions SET blob_ref=? WHERE source_id=? AND revision=?",
                    (source["sha256"], sid, source["revision"]),
                )
                source["state"], source["updated_at"] = "queued", stamp(self.clock())
                if self.source(sid)["revision"] != source["revision"]:
                    self.conn.execute(
                        "UPDATE study_pending_revisions SET body=? WHERE source_id=?", (json.dumps(source), sid)
                    )
                else:
                    self.save_source(source)
        return checked("UploadReceipt", {"received_bytes": received, "complete": received == length})

    def commit_units(self, source: dict[str, Any], units: list[dict[str, Any]],
                     frames: list[dict[str, Any]] | None = None) -> None:  # fmt: skip
        """Freeze a revision's units as chunks. `frames` (SPEC §20.3, a video file) are the stills read as text: one
        `study_frames` row each, a screened one counted with no text; a read frame's text is the unit whose frame
        locator names it."""
        sid, revision = source["source_id"], source["revision"]
        current = self.source(sid)
        replacing = revision == current["revision"] + 1 and self.pending_source(sid)["revision"] == revision
        if current["revision"] != revision and not replacing:
            raise StudyError("source_stale")
        unreadable, readable, ordinal = [], 0, 0
        needs_confirmation = any(u.get("ocr") for u in units)
        if source["kind"] in ("recording", "video", "video_link"):
            timed = [u["locator"]["end_ms"] for u in units if u["locator"]["kind"] == "time"]
            if timed:
                source["duration_ms"] = max(timed)
        if frames is not None:
            texts = {u["locator"]["frame_id"]: normalise(u["text"]) for u in units if u["locator"]["kind"] == "frame"}
            self.conn.execute("DELETE FROM study_frames WHERE source_id=? AND revision=?", (sid, revision))
            for frame in frames:
                checked("Frame", {k: frame[k] for k in ("frame_id", "at_ms", "reason", "read_state")})
                self.conn.execute(
                    "INSERT INTO study_frames VALUES (?,?,?,?,?,?,?)",
                    (frame["frame_id"], sid, revision, frame["at_ms"], frame["reason"], frame["read_state"],
                     texts.get(frame["frame_id"], "") if frame["read_state"] == "read" else ""),
                )  # fmt: skip
            source["frames"] = len(frames)
            source["screened_frames"] = sum(1 for f in frames if f["read_state"] == "screened")
        for index, unit in enumerate(units):
            text = normalise(unit["text"])
            if not text:
                unreadable.append(index + 1)
                continue
            readable += 1
            for start in range(0, len(text), 16000):
                part = text[start : start + 16000]
                chunk = checked(
                    "Chunk",
                    {
                        "chunk_id": new_id("sck"),
                        "source_id": sid,
                        "revision": revision,
                        "ordinal": ordinal,
                        "locator": unit["locator"],
                        "text": part,
                        "sha256": digest(part.encode()),
                    },
                )
                self.conn.execute(
                    "INSERT INTO study_chunks VALUES (?,?,?,?,?)",
                    (chunk["chunk_id"], sid, revision, ordinal, json.dumps(chunk)),
                )
                ordinal += 1
        source["private"] = current["private"]
        source.update(
            state="partial"
            if readable and (unreadable or needs_confirmation)
            else "ready"
            if readable
            else "unreadable",
            readable_units=readable,
            unreadable_units=unreadable,
            updated_at=stamp(self.clock()),
        )
        if replacing and readable == 0:
            # A failed replacement never makes the last readable revision inaccessible.
            raise StudyError("unreadable_source")
        self.save_source(source)
        if replacing:
            self.conn.execute("DELETE FROM study_pending_revisions WHERE source_id=?", (sid,))
            self.stale(sid)
        self.conn.execute(
            "UPDATE study_revisions SET extracted_at=? WHERE source_id=? AND revision=?", (self.clock(), sid, revision)
        )

    async def page_raster(self, sid: str, revision: int, page: int) -> bytes:
        source = self.source(sid)
        row = self.conn.execute(
            "SELECT blob_ref FROM study_revisions WHERE source_id=? AND revision=?", (sid, revision)
        ).fetchone()
        if row is None or row[0] is None:
            raise StudyError("not_found")
        data = await asyncio.to_thread((self.root / "blobs" / row[0]).read_bytes)
        try:
            return await raster(data, source["kind"], page, self.settings)
        except DocumentError as error:
            raise StudyError(str(error)) from None

    def chunks(self, sid: str, revision: int, cursor: str | None = None, limit: int = 100) -> dict[str, Any]:
        self.source(sid)
        if not 1 <= limit <= 100 or revision < 1:
            raise StudyError("invalid_study_request")
        try:
            offset = int(cursor or "0")
        except ValueError:
            raise StudyError("invalid_study_request") from None
        if offset < 0:
            raise StudyError("invalid_study_request")
        rows = self.conn.execute(
            "SELECT body FROM study_chunks WHERE source_id=? AND revision=? ORDER BY ordinal LIMIT ? OFFSET ?",
            (sid, revision, limit + 1, offset),
        ).fetchall()
        return {
            "items": [json.loads(r[0]) for r in rows[:limit]],
            "next_cursor": str(offset + limit) if len(rows) > limit else None,
        }

    def revise(self, sid: str, body: dict[str, Any]) -> dict[str, Any]:
        checked("SourceRevision", body)
        source = self.source(sid)
        if body["expected_revision"] != source["revision"]:
            raise StudyError("source_stale", True)
        if self.pending_source(sid)["revision"] != source["revision"]:
            raise StudyError("source_stale", True)
        if "text" not in body:
            self.accept_file(source["kind"], body["file_name"])
            if body["byte_length"] > self.settings.study_source_max_mb * 1048576:
                raise StudyError("source_too_large")
            pending = {
                **source,
                "revision": source["revision"] + 1,
                "state": "uploading",
                "title": self.file_title(body["file_name"]),
                "sha256": body["sha256"],
                "readable_units": 0,
                "unreadable_units": [],
                "updated_at": stamp(self.clock()),
            }
            checked("Source", pending)
            with atomic(self.conn):
                self.conn.execute(
                    "INSERT INTO study_revisions VALUES (?,?,?,?,?,?)",
                    (sid, pending["revision"], pending["sha256"], None, body["byte_length"], None),
                )
                self.conn.execute(
                    "INSERT INTO study_pending_revisions VALUES (?,?,?)",
                    (sid, pending["revision"], json.dumps(pending)),
                )
            return pending
        data = normalise(body["text"]).encode() if "text" in body else None
        sha = digest(data) if data is not None else body["sha256"]
        revision = source["revision"] + 1
        with atomic(self.conn):
            source.update(
                revision=revision,
                state="uploading" if data is None else "ready",
                sha256=sha,
                readable_units=0,
                unreadable_units=[],
                updated_at=stamp(self.clock()),
            )
            if data is None:
                source["title"] = self.file_title(body["file_name"])
            self.save_source(source)
            self.conn.execute(
                "INSERT INTO study_revisions VALUES (?,?,?,?,?,?)",
                (sid, revision, sha, None, len(data) if data is not None else body["byte_length"], None),
            )
            if data is not None:
                units = [{"text": data.decode(), "locator": {"kind": "text", "paragraph": 1}}]
                if source["kind"] == "video_link":  # a corrected transcript is parsed again: moments and chapters
                    try:
                        parsed = parse_transcript(body["text"])
                    except TranscriptTooLong:
                        raise StudyError("transcript_too_long") from None
                    source["transcript_kind"] = parsed.kind
                    source.pop("chapters", None)
                    if parsed.chapters:
                        source["chapters"] = [
                            {k: c[k] for k in ("start_ms", "end_ms", "title")} for c in parsed.chapters[:60]
                        ]
                    units = self.transcript_units(parsed, data.decode(), body)
                self.commit_units(source, units)
            self.stale(sid)
        return self.source(sid)

    def stale(self, sid: str) -> None:
        self.conn.execute(
            "UPDATE study_aids SET body=json_set(body,'$.status','stale') "
            "WHERE aid_id IN (SELECT aid_id FROM study_aid_sources WHERE source_id=?)",
            (sid,),
        )
        self.conn.execute(
            "UPDATE study_flashcards SET body=json_set(body,'$.suspended',json('true')) "
            "WHERE deck_id IN (SELECT deck_id FROM study_decks WHERE source_id=?)",
            (sid,),
        )

    def patch(self, sid: str, body: dict[str, Any]) -> dict[str, Any]:
        checked("SourcePatch", body)
        source = self.source(sid)
        source.update(body, updated_at=stamp(self.clock()))
        source["title"] = redact_personal(source["title"])[0]
        self.save_source(source)
        return source

    def delete(self, sid: str) -> dict[str, Any]:
        if self.conn.execute("SELECT 1 FROM study_tombstones WHERE source_id=?", (sid,)).fetchone():
            pending = self.conn.execute("SELECT 1 FROM study_cloud_cleanup WHERE source_id=?", (sid,)).fetchone()
            return {"source_id": sid, "state": "deleted", "cloud_cleanup": "pending" if pending else "not_sent"}
        self.source(sid)
        blobs = [
            r[0]
            for r in self.conn.execute(
                "SELECT blob_ref FROM study_revisions WHERE source_id=? AND blob_ref IS NOT NULL", (sid,)
            )
        ]
        with atomic(self.conn):
            self.conn.execute("DELETE FROM study_sources WHERE source_id=?", (sid,))
            self.conn.execute("DELETE FROM idempotency_responses WHERE idempotency_key LIKE 'study-pending:%'")
            self.conn.execute(
                "UPDATE idempotency_responses SET response=json_set(response,'$.body',NULL) WHERE response LIKE ?",
                (f"%{sid}%",),
            )
        for blob in blobs:
            if not self.conn.execute("SELECT 1 FROM study_revisions WHERE blob_ref=?", (blob,)).fetchone():
                (self.root / "blobs" / blob).unlink(missing_ok=True)
        if (self.root / "staging").exists():
            for path in (self.root / "staging").glob(f"{sid}-*"):
                path.unlink(missing_ok=True)
        pending = self.conn.execute("SELECT 1 FROM study_cloud_cleanup WHERE source_id=?", (sid,)).fetchone()
        return {"source_id": sid, "state": "deleted", "cloud_cleanup": "pending" if pending else "not_sent"}

    def curriculum_text(self, selection: dict[str, Any]) -> str:
        """Local title suggestions may use readable text awaiting review; generation still needs confirmation."""
        source = self.source(selection["source_id"])
        if source["revision"] != selection["revision"]:
            raise StudyError("source_stale", True)
        if source["state"] not in ("ready", "partial") or not source.get("readable_units"):
            raise StudyError("unreadable_source")
        return "\n".join(chunk["text"] for chunk in self.selected([selection], ready=False))

    def selected(self, selections: list[dict[str, Any]], *, ready: bool = True) -> list[dict[str, Any]]:
        if len({s["source_id"] for s in selections}) != len(selections):
            raise StudyError("invalid_study_request")
        chunks = []
        for s in selections:
            source = (
                self.source(s["source_id"])
                if ready
                else next(
                    (
                        v
                        for v in (self.source(s["source_id"]), self.pending_source(s["source_id"]))
                        if v["revision"] == s["revision"]
                    ),
                    self.source(s["source_id"]),
                )
            )
            if source["revision"] != s["revision"]:
                raise StudyError("source_stale", True)
            if ready and source["state"] != "ready":
                raise StudyError("confirm_needed" if source["state"] == "partial" else "unreadable_source")
            rows = self.conn.execute(
                "SELECT body FROM study_chunks WHERE source_id=? AND revision=? ORDER BY ordinal",
                (s["source_id"], s["revision"]),
            ).fetchall()
            chunks += [json.loads(r[0]) for r in rows]
        return chunks

    def create_job(self, body: dict[str, Any]) -> dict[str, Any]:
        checked("JobCreate", body)
        if body.get("learning_intent") and body["kind"] != "chat":
            raise StudyError("invalid_study_request")
        if body.get("context_aid_id"):
            self.follow_up_aid(body)
        settings = body.get("settings", {})
        # Only the learner's actual request determines intent; imported homework remains source data.
        # A Generate focus or topic reaches the model like a prompt, so it takes the same screen (STU-11, STU-21).
        for prompt in [body.get("prompt", ""), settings.get("focus", ""), *settings.get("topics", [])]:
            category = pedagogy.classify_input(prompt)
            if category == "cheating_request":
                raise StudyError("graded_work")
            if category:
                raise StudyError("safety_blocked")
            category, confidence = LexiconClassifier(
                categories=("harmful", "sexual_or_violent", "medical_legal_advice")
            )(prompt)
            if confidence >= self.settings.guard_classifier_threshold and category != "in_scope":
                raise StudyError("safety_blocked")
        self.selected(body["sources"], ready=body["kind"] != "extract" and not body.get("confirm_readable_only"))
        if body["kind"] != "extract" and any(
            self.source(s["source_id"])["state"] not in ("ready", "partial") for s in body["sources"]
        ):
            raise StudyError("unreadable_source")
        self.check_settings(body)
        if (
            self.conn.execute(
                "SELECT count(*) FROM study_jobs WHERE state NOT IN ('completed','failed','cancelled')"
            ).fetchone()[0]
            >= self.settings.study_job_queue_limit
        ):
            raise StudyError("queue_full")
        now, jid = stamp(self.clock()), new_id("stj")
        sha = digest(json.dumps(body, sort_keys=True).encode())
        job = checked(
            "Job",
            {
                "job_id": jid,
                "kind": body["kind"],
                "sources": body["sources"],
                "state": "queued",
                "completed_units": 0,
                "total_units": None,
                "result_ids": [],
                "error": None,
                "created_at": now,
                "updated_at": now,
            },
        )
        checkpoint: dict[str, Any] = checked(
            "Checkpoint",
            {
                "completed_chunk_ids": [],
                "extractor_version": "local-document-1",
                "model_version": "local",
                "prompt_digest": sha,
                "reservation_id": None,
                "attempt": 1,
                "request": body,
            },
        )
        with atomic(self.conn):
            self.conn.execute(
                "INSERT INTO study_jobs VALUES (?,?,?,?,?,?,?)",
                (jid, "queued", 1, sha, json.dumps(job), json.dumps(checkpoint), self.clock()),
            )
            # A syllabus file shapes the job, so erasing it cancels the job like erasing the material (STU-19).
            for s in [*body["sources"], *settings.get("syllabus", [])]:
                self.conn.execute("INSERT INTO study_job_sources VALUES (?,?,?)", (jid, s["source_id"], s["revision"]))
        return job

    def create_quiz_help_job(self, qid: str, item_id: str, body: dict[str, Any]) -> dict[str, Any]:
        quiz = self.quiz(qid)["view"]
        aid = self.aid(quiz["aid_id"])
        if quiz["status"] != "ready":
            raise StudyError("source_stale", True)
        if not any(item["item_id"] == item_id for item in quiz["items"]):
            raise StudyError("not_found")
        if body["kind"] != "chat" or body["sources"] != aid["sources"] or body.get("settings"):
            raise StudyError("invalid_study_request")
        job = self.create_job(body)
        self.conn.execute("INSERT INTO study_quiz_help_jobs VALUES (?,?,?)", (job["job_id"], qid, item_id))
        return job

    def quiz_help_scope(self, jid: str, chunks: list[dict[str, Any]]) -> list[dict[str, Any]]:
        binding = self.conn.execute(
            "SELECT quiz_id,item_id FROM study_quiz_help_jobs WHERE job_id=?", (jid,)
        ).fetchone()
        if binding is None:
            return chunks
        quiz = self.quiz(binding[0])
        if quiz["view"]["status"] != "ready":
            raise StudyError("source_stale", True)
        item = next((i for i in quiz["view"]["items"] if i["item_id"] == binding[1]), None)
        key = next((k for k in quiz["keys"] if k["item_id"] == binding[1]), None)
        if item is None or key is None:
            raise StudyError("not_found")
        # Select frozen evidence IDs, never the answer or key text. Keep neighbouring source chunks for
        # qualifications across page boundaries; ordinary whole-document study jobs are unaffected.
        refs = [*item["stem"]["citations"], *key["explanation"]["citations"]]
        cited = {(c["source_id"], c["revision"], c["chunk_id"]) for c in refs}
        chosen = set()
        for i, chunk in enumerate(chunks):
            if (chunk["source_id"], chunk["revision"], chunk["chunk_id"]) in cited:
                chosen.add(i)
                for neighbour in (i - 1, i + 1):
                    if 0 <= neighbour < len(chunks) and (
                        chunks[neighbour]["source_id"],
                        chunks[neighbour]["revision"],
                    ) == (chunk["source_id"], chunk["revision"]):
                        chosen.add(neighbour)
        kept = [chunk for i, chunk in enumerate(chunks) if i in chosen]
        # Prerequisite definitions can be at the start of the preceding page, several chunks away
        # from the answer. Prefer complete adjacent PDF pages when they fit the same bounded context.
        pages = {
            (c["source_id"], c["revision"], c["locator"]["page"] + offset)
            for c in chunks
            if (c["source_id"], c["revision"], c["chunk_id"]) in cited and c["locator"]["kind"] == "page"
            for offset in (-1, 0, 1)
        }
        expanded = [
            c
            for i, c in enumerate(chunks)
            if i in chosen
            or (c["locator"]["kind"] == "page" and (c["source_id"], c["revision"], c["locator"]["page"]) in pages)
        ]
        if expanded and sum(len(c["text"]) for c in expanded) <= self.settings.study_batch_char_limit:
            kept = expanded
        if not kept or sum(len(c["text"]) for c in kept) > self.settings.study_batch_char_limit:
            raise StudyError("source_too_large" if kept else "insufficient_evidence")
        return kept

    def quiz_help_brief(self, jid: str, brief: dict[str, Any]) -> dict[str, Any]:
        binding = self.conn.execute(
            "SELECT quiz_id,item_id FROM study_quiz_help_jobs WHERE job_id=?", (jid,)
        ).fetchone()
        if binding is None:
            return brief
        quiz = self.quiz(binding[0])
        item = next(i for i in quiz["view"]["items"] if i["item_id"] == binding[1])
        key = next(k for k in quiz["keys"] if k["item_id"] == binding[1])
        if item["type"] == "choice":
            options = item["options"]
            index = next(i for i, o in enumerate(options) if o["option_id"] == key["answer"])
            correct = options[index]["text"]
            # Exact option wording and its distinguishing terms are private inference constraints.
            # Do not block words shared with the question/distractors, which can be useful prerequisites.
            other_words = set(re.findall(r"\w+", blank_form(item["stem"]["text"])))
            for i, option in enumerate(options):
                if i != index:
                    other_words.update(re.findall(r"\w+", blank_form(option["text"])))
            distinctive = list(
                dict.fromkeys(
                    w for w in re.findall(r"\w+", blank_form(correct)) if len(w) >= 4 and w not in other_words
                )
            )
            number = index + 1
            words = [correct] if len(correct) <= 150 else []
            words += [f"option {number}", f"choice {number}", f"option {chr(97 + index)}", f"choice {chr(97 + index)}"]
            directional: list[str] = []
            tokens = set(re.findall(r"\w+", blank_form(correct)))
            group: tuple[str, ...]
            for group in (
                (
                    "reduce",
                    "reduces",
                    "reduced",
                    "reducing",
                    "reduction",
                    "lower",
                    "lowers",
                    "lowered",
                    "lowering",
                    "decrease",
                    "decreases",
                    "decreased",
                    "decreasing",
                    "diminish",
                    "less",
                    "smaller",
                ),
                ("increase", "increases", "increased", "increasing", "higher", "greater", "grow", "grows"),
            ):
                if tokens.intersection(group):
                    directional.extend(group)
            # A formula from the grading explanation or a long identifying phrase can give away
            # the option even if its defining adjective has been replaced with a synonym.
            aliases = re.findall(r"P\([^()\n]{1,100}\)", key["explanation"]["text"])
            correct_words = re.findall(r"\w+", blank_form(correct))
            phrases = [" ".join(correct_words[i : i + 6]) for i in range(max(0, len(correct_words) - 5))]
            for word in [*aliases, *distinctive, *directional, *phrases]:
                if word in words:
                    continue
                candidate = [*words, word]
                if len(candidate) <= 40 and len("Coaching withheld wording: " + json.dumps(candidate)) <= 500:
                    words = candidate
            focus = "Coaching withheld wording: " + json.dumps(words)
            return {**brief, "settings": {**brief.get("settings", {}), "focus": focus}}
        if item["type"] != "fill_blank":
            return brief
        answer = blank_form(key["answer"])
        if not answer or len(answer) > 150:
            return brief
        words = [answer]
        # Common directional equivalents must not leak a blank's answer through an obvious synonym.
        for group in (
            (
                "diminish",
                "diminishes",
                "diminished",
                "decrease",
                "decreases",
                "decreased",
                "reduce",
                "reduces",
                "reduced",
                "shrink",
                "shrinks",
                "lessen",
                "lessens",
            ),
            ("increase", "increases", "increased", "grow", "grows", "enlarge", "enlarges"),
        ):
            if answer in group:
                words = list(group)
        # This bounded inference-only constraint is never added to the stored learner prompt, aid or quiz view.
        focus = "Coaching withheld wording: " + json.dumps(words)
        return {**brief, "settings": {**brief.get("settings", {}), "focus": focus}}

    def check_settings(self, body: dict[str, Any]) -> None:
        """STU-21: the Generate settings fit their tool, name the material's own topics and a ready syllabus."""
        kind, settings = body["kind"], body.get("settings", {})
        if set(settings) - SETTINGS.get(kind, set()):
            raise StudyError("invalid_study_request")
        if "topics" in settings:
            titles = {t["title"] for t in self.topics(body["sources"])}
            if not set(settings["topics"]) <= titles:
                raise StudyError("invalid_study_request")
        syllabus = settings.get("syllabus", [])
        if {s["source_id"] for s in syllabus} & {s["source_id"] for s in body["sources"]}:
            raise StudyError("invalid_study_request")
        if syllabus:
            self.selected(syllabus)
        if kind == "chapters":
            chunks = self.selected(body["sources"], ready=not body.get("confirm_readable_only"))
            if not chunks or any(c["locator"]["kind"] not in ("time", "frame") for c in chunks):
                raise StudyError("invalid_study_request")
        if kind == "teaching_outline":  # SPEC §20.4: over a transcript's timed chunks
            chunks = self.selected(body["sources"], ready=not body.get("confirm_readable_only"))
            if not any(c["locator"]["kind"] == "time" for c in chunks):
                raise StudyError("invalid_study_request")

    def topics(self, selections: list[dict[str, Any]]) -> list[dict[str, Any]]:
        """The material's own topics for the Generate settings, over every selected source (STU-21)."""
        items: list[dict[str, Any]] = []
        for s in selections:
            for topic in material_topics(self.selected([s], ready=False))["items"]:
                same = next((t for t in items if t["title"].casefold() == topic["title"].casefold()), None)
                if same is None:
                    items.append(topic)
                else:
                    same["chunk_ids"] += [c for c in topic["chunk_ids"] if c not in same["chunk_ids"]]
        return items[:20]

    def follow_up_aid(self, request: dict[str, Any]) -> dict[str, Any]:
        """A follow-up cannot change profiles, source revisions, or turn a quiz key into context."""
        if request["kind"] not in ("chat", "flashcards", "quiz"):
            raise StudyError("invalid_study_request")
        parent = self.aid(request["context_aid_id"])
        if parent["kind"] != "chat" and not (parent["kind"] == "flashcards" and request["kind"] == "quiz"):
            raise StudyError("invalid_study_request")
        if parent["status"] != "ready":
            raise StudyError("source_stale", True)

        def selections(sources: list[dict[str, Any]]) -> set[tuple[Any, Any]]:
            return {(s["source_id"], s["revision"]) for s in sources}

        if selections(parent["sources"]) != selections(request["sources"]):
            raise StudyError("invalid_study_request")
        return parent

    def follow_up_claims(self, parent: dict[str, Any]) -> list[dict[str, Any]]:
        if parent["kind"] == "flashcards":
            return [
                claim
                for card in self.deck(parent["deck_id"])["cards"]
                if not card["suspended"]
                for claim in (card["front"], card["back"])
            ]
        return cast(list[dict[str, Any]], parent["claims"])

    def scoped(self, request: dict[str, Any], chunks: list[dict[str, Any]]) -> list[dict[str, Any]]:
        """Only the chunks of the topics the learner kept; all topics kept means the whole material (STU-21)."""
        # An explicit PDF page scope is applied before batching. Otherwise a focused quiz
        # asks unrelated chapters for evidence and one irrelevant batch can fail the job.
        # Keep whole frozen chunks: page selection must not alter citation text or offsets.
        focus = request.get("settings", {}).get("focus", "")
        page_scope = re.search(r"(?:^|\n)PDF pages?\s+(\d+)(?:\s*[-\u2013\u2014]\s*(\d+))?\s*:", focus, re.I)
        if page_scope and request["kind"] in ("quiz", "flashcards", "summary", "notes", "lesson_plan"):
            first, last = int(page_scope[1]), int(page_scope[2] or page_scope[1])
            if not 1 <= first <= last <= 1000:
                raise StudyError("insufficient_evidence")
            chunks = [c for c in chunks if c["locator"]["kind"] == "page" and first <= c["locator"]["page"] <= last]
            if not chunks:
                raise StudyError("insufficient_evidence")
        if request.get("context_aid_id"):
            parent = self.follow_up_aid(request)
            ids = {c["chunk_id"] for claim in self.follow_up_claims(parent) for c in claim["citations"]}
            chunks = [c for c in chunks if c["chunk_id"] in ids]
            if not chunks:
                raise StudyError("insufficient_evidence")
        if request["kind"] == "teaching_outline":  # transcript chunks only (JM-D33): never a frame's text
            chunks = [c for c in chunks if c["locator"]["kind"] == "time"]
        chosen = request.get("settings", {}).get("topics")
        if not chosen:
            return chunks
        topics = self.topics(request["sources"])
        if {t["title"] for t in topics} <= set(chosen):
            return chunks
        ids = {c for t in topics if t["title"] in chosen for c in t["chunk_ids"]}
        kept = [c for c in chunks if c["chunk_id"] in ids]
        if not kept:
            raise StudyError("insufficient_evidence")
        return kept

    def brief(self, request: dict[str, Any]) -> dict[str, Any]:
        """The generation request a model sees: the settings, with a syllabus file as bounded scope text only."""
        settings = dict(request.get("settings", {}))
        chosen = settings.get("topics")
        if chosen and {t["title"] for t in self.topics(request["sources"])} <= set(chosen):
            # Keeping every detected heading means the whole document. The
            # bounded heading preview must not become a model-side restriction
            # that omits later chapters or asks it to summarise author metadata.
            settings.pop("topics")
        syllabus = settings.pop("syllabus", [])
        brief = {k: v for k, v in request.items() if k not in ("settings", "context_aid_id", "learning_intent")}
        if request.get("context_aid_id"):
            parent = self.follow_up_aid(request)
            lines: list[str] = []
            for i, claim in enumerate(self.follow_up_claims(parent)):
                line = f"{i + 1}. {claim['text']}"
                if sum(len(s) + 1 for s in lines) + len(line) > 8000:
                    break
                lines.append(line)
            brief["previous_response"] = "\n".join(lines)
        if settings:
            brief["settings"] = settings
        # SPEC §19.3.1: who is asking, as MDL-25's data (level, subjects, bands, watched misconceptions), when the
        # profile's learner database is open. The app never sends it; the engine renders it from the snapshot.
        # The outline request carries transcript chunks and nothing else (SPEC §20.3, JM-D33, review B7).
        learner = (self.activity.learner_block() if self.activity is not None and request["kind"] != "teaching_outline"
                   else None)  # fmt: skip
        if learner is not None:
            brief["learner"] = learner
        if syllabus:
            text = "\n".join(c["text"] for c in self.selected(syllabus))
            brief["syllabus_outline"] = text[:8000]
        return brief

    def local_possible(self, body: dict[str, Any]) -> bool:
        if not self.settings.study_offline_enabled:
            return False  # D133: study tools are made online only
        if body["kind"] == "teaching_outline":
            return False  # drafted and verified by Muse Spark only (JM-D33)
        pages = 0
        for selected in body["sources"]:
            source = self.source(selected["source_id"])
            if source["kind"] in ("recording", "video") and body["kind"] != "chapters":
                return False
            if source["kind"] in ("session_note", "dictated_note", "board_note"):
                continue
            pages += (
                source["readable_units"]
                if source["kind"] in ("pdf", "slides", "photo")
                else (
                    self.conn.execute(
                        "SELECT coalesce(sum(length(json_extract(body,'$.text'))),0) FROM study_chunks "
                        "WHERE source_id=? AND revision=?",
                        (source["source_id"], source["revision"]),
                    ).fetchone()[0]
                    + 3999
                )
                // 4000
            )
        return bool(pages <= self.settings.study_offline_max_pages)

    def job(self, jid: str) -> dict[str, Any]:
        return self.one("SELECT body FROM study_jobs WHERE job_id=?", (jid,))

    def save_job(self, job: dict[str, Any]) -> None:
        job["updated_at"] = stamp(self.clock())
        checked("Job", job)
        self.conn.execute(
            "UPDATE study_jobs SET state=?,body=?,updated_at=? WHERE job_id=?",
            (job["state"], json.dumps(job), self.clock(), job["job_id"]),
        )

    def action(self, jid: str, body: dict[str, Any]) -> dict[str, Any]:
        checked("JobAction", body)
        job = self.job(jid)
        action = body["action"]
        allowed = {
            "cancel": ("queued", "running", "paused", "waiting_online", "failed"),
            "resume": ("paused", "waiting_online"),
            "retry": ("failed",),
        }
        if job["state"] not in allowed[action]:
            raise StudyError("job_terminal", True)
        self.selected(job["sources"], ready=False)
        if action == "retry":
            self.conn.execute(
                "UPDATE study_jobs SET attempt=attempt+1,checkpoint=json_set(checkpoint,'$.attempt',attempt+1) "
                "WHERE job_id=?",
                (jid,),
            )
        if action == "cancel":
            self.conn.execute(
                "UPDATE study_jobs SET checkpoint=json_remove(checkpoint,'$.batches') WHERE job_id=?", (jid,)
            )
        job.update(state="cancelled" if action == "cancel" else "queued", error=None)
        self.save_job(job)
        return job

    def aid(self, aid: str) -> dict[str, Any]:
        return self.one("SELECT body FROM study_aids WHERE aid_id=?", (aid,))

    def citation(self, claim: dict[str, Any], sources: list[dict[str, Any]]) -> None:
        checked("Claim", claim)
        if redact_personal(claim["text"])[1]:
            raise StudyError("insufficient_evidence")
        allowed = {(s["source_id"], s["revision"]) for s in sources}
        for c in claim["citations"]:
            if (c["source_id"], c["revision"]) not in allowed:
                raise StudyError("insufficient_evidence")
            chunk = self.one(
                "SELECT body FROM study_chunks WHERE chunk_id=? AND source_id=? AND revision=?",
                (c["chunk_id"], c["source_id"], c["revision"]),
            )
            if (
                not (0 <= c["start"] < c["end"] <= len(chunk["text"]))
                or chunk["text"][c["start"] : c["end"]] != c["quote"]
            ):
                raise StudyError("insufficient_evidence")

    @staticmethod
    def check_chapters(chapters: list[dict[str, Any]], chunks: list[dict[str, Any]]) -> None:
        """STU-22: chapters run in time order over the transcript and never overlap; each summary sentence cites a
        span inside its own chapter. Gaps are closed so the chapters cover the recording from its start to its end."""
        timed = {c["chunk_id"]: c["locator"] for c in chunks if c["locator"]["kind"] == "time"}
        if not chapters or not timed:
            raise StudyError("insufficient_evidence")
        first = min(loc["start_ms"] for loc in timed.values())
        last = max(loc["end_ms"] for loc in timed.values())
        for a, b in itertools.pairwise(chapters):
            if b["start_ms"] < a["end_ms"]:
                raise StudyError("insufficient_evidence")
        if any(c["start_ms"] >= c["end_ms"] or c["start_ms"] >= last for c in chapters):
            raise StudyError("insufficient_evidence")
        # A chapter starts at a transcript line, so selecting it and seeking to a line agree.
        starts = sorted({loc["start_ms"] for loc in timed.values()})
        for chapter in chapters:
            chapter["start_ms"] = min(starts, key=lambda at: (abs(at - chapter["start_ms"]), at))
        chapters[0]["start_ms"] = first
        for a, b in itertools.pairwise(chapters):
            a["end_ms"] = b["start_ms"]
        chapters[-1]["end_ms"] = last
        for chapter in chapters:
            if chapter["start_ms"] >= chapter["end_ms"]:
                raise StudyError("insufficient_evidence")
            for claim in chapter["summary"]:
                for cite in claim["citations"]:
                    loc = timed.get(cite["chunk_id"])
                    if loc is None or not chapter["start_ms"] <= loc["start_ms"] < chapter["end_ms"]:
                        raise StudyError("insufficient_evidence")

    def commit_aid(self, job: dict[str, Any], output: dict[str, Any], supported: list[bool],
                   content: Any = None) -> dict[str, Any]:  # fmt: skip
        checked("ProcessingResponse", output)
        if output["request_id"] != job["job_id"] or output["kind"] != job["kind"]:
            raise StudyError("insufficient_evidence")
        self.selected(job["sources"], ready=False)
        claims = output["claims"]
        cards = output.get("flashcards", [])
        quiz = output.get("quiz", {"items": [], "keys": []})
        chapters = output.get("chapters", [])
        drafted = output.get("concept_map")
        outline = output.get("outline")
        all_claims = [
            *claims,
            *(v[k] for v in cards for k in ("front", "back")),
            *(i["stem"] for i in quiz["items"]),
            *(k["explanation"] for k in quiz["keys"]),
            *(s for c in chapters for s in c["summary"]),
            *(study_concepts.draft_claims(drafted) if drafted else []),
            *(study_outline.flatten(outline) if outline else []),
        ]
        for claim in all_claims:
            if OutputCritic().check(claim["text"], "low") is not None:
                raise StudyError("safety_blocked")
            self.citation(claim, job["sources"])
        for chapter in chapters:
            if OutputCritic().check(chapter["title"], "low") is not None or redact_personal(chapter["title"])[1]:
                raise StudyError("insufficient_evidence")
        for concept in (drafted or {}).get("concepts", []):
            if OutputCritic().check(concept["name"], "low") is not None or redact_personal(concept["name"])[1]:
                raise StudyError("insufficient_evidence")
        for concept in (outline or {}).get("concepts", []):
            if OutputCritic().check(concept["name"], "low") is not None or redact_personal(concept["name"])[1]:
                raise StudyError("insufficient_evidence")
        request = self.one("SELECT checkpoint FROM study_jobs WHERE job_id=?", (job["job_id"],))["request"]
        settings = request.get("settings", {})
        if job["kind"] == "chapters":
            self.check_chapters(chapters, self.selected(job["sources"], ready=False))
        wanted = settings.get("question_types")
        if wanted and any(i["type"] not in wanted for i in quiz["items"]):
            raise StudyError("insufficient_evidence")
        groups = len(claims) + len(cards) + len(quiz["items"])
        # Support checking is a separate model call; valid span coordinates alone do not establish support.
        if len(supported) != groups or not all(type(b) is bool for b in supported):
            raise StudyError("insufficient_evidence")
        filtered = [c for i, c in enumerate(claims) if supported[i]]
        if not filtered or not all(supported[len(claims) :]):
            raise StudyError("insufficient_evidence")
        aid = {
            "aid_id": new_id("aid"),
            "job_id": job["job_id"],
            "kind": job["kind"],
            "sources": job["sources"],
            "title": self.source(job["sources"][0]["source_id"])["title"],
            "claims": filtered,
            "status": "ready",
            "created_at": stamp(self.clock()),
            # Where it was made and by which model, shown beside the aid; the client never infers it.
            "made_by": {"runtime": "online" if request["execution"] == "prefer_cloud" else "device"},
        }
        if isinstance(output.get("model"), str) and output["model"]:
            aid["made_by"]["model"] = output["model"][:80]
        if job["kind"] == "chat":
            aid["prompt"] = request["prompt"]
        if settings:
            aid["settings"] = settings
        if chapters:
            aid["chapters"] = chapters
        if drafted:
            # Anchored to the curated backbone by the pack's own records; the wording stays the document's.
            aid["concept_map"] = study_concepts.document_map(drafted, content)
        if outline:
            # SPEC §20.4: moments, skills, skips and the learner's bands by code on the device, never by the model.
            aid["outline"] = study_outline.fill(outline, self.selected(job["sources"], ready=False), content,
                                                self.band_reader())  # fmt: skip
        if cards:
            aid["deck_id"] = new_id("dek")
        if quiz["items"]:
            aid["quiz_id"] = new_id("sqz")
        checked("Aid", aid)
        fronts = [" ".join(c["front"]["text"].split()).casefold() for c in cards]
        stems = [" ".join(i["stem"]["text"].split()).casefold() for i in quiz["items"]]
        if len(fronts) != len(set(fronts)) or len(stems) != len(set(stems)):
            raise StudyError("insufficient_evidence")
        requested_count = request.get("count", 10)
        if len(cards) > requested_count or len(quiz["items"]) > requested_count:
            raise StudyError("insufficient_evidence")
        with atomic(self.conn):
            if self.job(job["job_id"])["state"] != "running":
                raise StudyError("source_deleted")
            self.conn.execute("INSERT INTO study_aids VALUES (?,?,?)", (aid["aid_id"], json.dumps(aid), self.clock()))
            for s in [*job["sources"], *settings.get("syllabus", [])]:
                self.conn.execute(
                    "INSERT INTO study_aid_sources VALUES (?,?,?)", (aid["aid_id"], s["source_id"], s["revision"])
                )
            if cards:
                s = job["sources"][0]
                self.conn.execute(
                    "INSERT INTO study_decks VALUES (?,?,?,?,?)",
                    (aid["deck_id"], aid["aid_id"], s["source_id"], s["revision"], SCHEDULER),
                )
                for c in cards:
                    card = {"card_id": new_id("fcd"), **c, "revision": 1, "suspended": False}
                    self.conn.execute(
                        "INSERT INTO study_flashcards VALUES (?,?,?,?,?,?)",
                        (
                            card["card_id"],
                            aid["deck_id"],
                            1,
                            json.dumps(checked("Flashcard", card)),
                            json.dumps(self.empty_schedule()),
                            self.clock(),
                        ),
                    )
            if quiz["items"]:
                keys = {k["item_id"]: k for k in quiz["keys"]}
                if len(keys) != len(quiz["items"]) or len(keys) != len(quiz["keys"]):
                    raise StudyError("insufficient_evidence")
                seen = set()
                for item in quiz["items"]:
                    identity = item["item_id"]
                    if identity in seen or identity not in keys or keys[identity]["marking"] != "judged":
                        raise StudyError("insufficient_evidence")
                    seen.add(identity)
                    if item["type"] in ("numeric", "expression") and not keys[identity].get("calculation"):
                        raise StudyError("insufficient_evidence")
                    if item["type"] in CHOICE_LIKE:
                        options = item["options"]
                        if any(redact_personal(option["text"])[1] for option in options):
                            raise StudyError("insufficient_evidence")
                        ids = {o["option_id"] for o in options}
                        answer = keys[identity]["answer"]
                        chosen = answer_ids(answer) if item["type"] == "multi_select" else [answer]
                        if (
                            len(ids) != len(options)
                            or len({o["text"].strip().casefold() for o in options}) != len(options)
                            or not chosen
                            or not set(chosen) <= ids
                            or any("," in i for i in ids)
                        ):
                            raise StudyError("insufficient_evidence")
                    if item["type"] == "true_false" and keys[identity]["answer"] not in ("true", "false"):
                        raise StudyError("insufficient_evidence")
                    if item["type"] == "fill_blank" and not valid_blank(item, keys[identity]):
                        raise StudyError("insufficient_evidence")
                stored = checked(
                    "QuizStored",
                    {
                        "view": {
                            "quiz_id": aid["quiz_id"],
                            "aid_id": aid["aid_id"],
                            "revision": 1,
                            "status": "ready",
                            "items": quiz["items"],
                        },
                        "keys": quiz["keys"],
                    },
                )
                self.conn.execute(
                    "INSERT INTO study_quizzes VALUES (?,?,?,?)", (aid["quiz_id"], aid["aid_id"], 1, json.dumps(stored))
                )
            from agent_core.engine.study_journey import StudyJourney

            StudyJourney(self).register(aid, request)
            job.update(state="completed", result_ids=[aid["aid_id"]], completed_units=job["total_units"] or 1)
            self.save_job(job)
        return aid

    def band_reader(self) -> Callable[[str], str | None] | None:
        """A skill's band from the open learner database (`kc_views` at now), for the outline's `for_you`; None
        when no learner database is open (every band then reads not started)."""
        learner = self.activity.learner() if self.activity is not None else None
        if learner is None:
            return None
        from agent_core.learner.model import kc_views

        try:
            views = kc_views(learner.conn, learner.projector.ctx.params, self.clock())
        except Exception:
            return None
        return lambda kc: views[kc].band if kc in views else None

    def identity_check(self, kind: str, identity: str, body: dict[str, Any]) -> None:
        row = self.conn.execute(
            "SELECT request_sha256 FROM idempotency_responses WHERE idempotency_key=?", (f"study-{kind}:{identity}",)
        ).fetchone()
        if row and row[0] != digest(json.dumps(body, sort_keys=True).encode()):
            raise StudyError(f"{kind}_conflict", True)

    def identity_save(self, kind: str, identity: str, body: dict[str, Any], sources: list[dict[str, Any]]) -> None:
        self.conn.execute(
            "INSERT INTO idempotency_responses VALUES (?,?,?,?,?)",
            (
                f"study-{kind}:{identity}",
                digest(json.dumps(body, sort_keys=True).encode()),
                200,
                json.dumps({"sources": sources, "body": None}),
                self.clock(),
            ),
        )

    def empty_schedule(self) -> dict[str, Any]:
        return {
            "state": "new",
            "stability": 0,
            "difficulty": 0,
            "reps": 0,
            "lapses": 0,
            "due_at": stamp(self.clock()),
            "last_review_at": None,
        }

    def deck(self, did: str) -> dict[str, Any]:
        row = self.conn.execute(
            "SELECT aid_id,source_id,revision,scheduler FROM study_decks WHERE deck_id=?", (did,)
        ).fetchone()
        if row is None:
            raise StudyError("not_found")
        now = self.clock()
        cards = []
        for body, raw_schedule in self.conn.execute(
            """SELECT body,schedule FROM study_flashcards WHERE deck_id=?
               ORDER BY CASE WHEN due_at<=? AND json_extract(schedule,'$.state') IN ('learning','relearning')
                             THEN 0 ELSE 1 END, due_at,card_id""",
            (did, now),
        ):
            card, schedule = json.loads(body), json.loads(raw_schedule)
            card["review_intervals_ms"] = {
                rating: self.scheduled_review(schedule, rating, now).due_ms - now
                for rating in ("again", "hard", "good", "easy")
            }
            cards.append(card)
        return {
            "deck_id": did,
            "aid_id": row[0],
            "source_id": row[1],
            "revision": row[2],
            "scheduler": row[3],
            "cards": cards,
        }

    @staticmethod
    def scheduled_review(schedule: dict[str, Any], rating: str, now: int) -> Card:
        """Pure source-card scheduling, shared by previews and committed reviews."""
        state = (
            None
            if schedule["state"] == "new"
            else Card(
                schedule["state"],
                schedule["stability"],
                schedule["difficulty"],
                epoch(schedule["due_at"]),
                schedule["reps"],
                schedule["lapses"],
                epoch(schedule["last_review_at"]) if schedule["last_review_at"] else None,
            )
        )
        new = scheduler_of(MasteryParams()).review(state, rating, now)
        # Source relearning is separate from the shared O-level scheduler. Retain FSRS memory/lapse history.
        return (
            replace(new, state="relearning" if state else "learning", due_ms=now + 2 * 60_000)
            if rating == "again"
            else new
        )

    def review(self, did: str, body: dict[str, Any]) -> dict[str, Any]:
        checked("ReviewSubmit", body)
        deck = self.deck(did)
        self.identity_check("review", body["client_review_id"], body)
        # Generation already required extraction review and verified the saved cards.
        # Reviewing them needs the same source revision, not a second extraction approval.
        self.selected([{"source_id": deck["source_id"], "revision": deck["revision"]}], ready=False)
        row = self.conn.execute(
            "SELECT body,schedule FROM study_flashcards WHERE deck_id=? AND card_id=?", (did, body["card_id"])
        ).fetchone()
        if row is None:
            raise StudyError("not_found")
        card, schedule = map(json.loads, row)
        if card["revision"] != body["card_revision"] or card["suspended"]:
            raise StudyError("source_stale", True)
        old = self.conn.execute(
            "SELECT card_id,body FROM study_reviews WHERE review_id=?", (body["client_review_id"],)
        ).fetchone()
        if old:
            if old[0] != body["card_id"]:
                raise StudyError("review_conflict", True)
            return dict(json.loads(old[1]))
        now = self.clock()
        new = self.scheduled_review(schedule, body["rating"], now)
        schedule = {
            "state": new.state,
            "stability": new.stability,
            "difficulty": new.difficulty,
            "reps": new.reps,
            "lapses": new.lapses,
            "due_at": stamp(new.due_ms),
            "last_review_at": stamp(now),
        }
        result = checked(
            "ReviewResult",
            {
                "client_review_id": body["client_review_id"],
                "due_at": schedule["due_at"],
                "scheduler": SCHEDULER,
                **{k: schedule[k] for k in ("stability", "difficulty", "reps", "lapses")},
            },
        )
        with atomic(self.conn):
            self.conn.execute(
                "UPDATE study_flashcards SET schedule=?,due_at=? WHERE card_id=?",
                (json.dumps(checked("CardSchedule", schedule)), new.due_ms, body["card_id"]),
            )
            self.identity_save(
                "review",
                body["client_review_id"],
                body,
                [{"source_id": deck["source_id"], "revision": deck["revision"]}],
            )
            self.conn.execute(
                "INSERT INTO study_reviews VALUES (?,?,?,?)",
                (body["client_review_id"], body["card_id"], json.dumps(result), self.clock()),
            )
        # The activity's seam: after the review committed (or, inside a command's transaction, at its flush).
        if self.activity is not None:
            self.activity.card(body["card_id"], f"{card['front']['text']}\n{card['back']['text']}", body["rating"])
        return result

    def quiz(self, qid: str) -> dict[str, Any]:
        stored = self.one("SELECT body FROM study_quizzes WHERE quiz_id=?", (qid,))
        if self.aid(stored["view"]["aid_id"])["status"] == "stale":
            stored["view"]["status"] = "stale"
        return stored

    def submit(self, qid: str, body: dict[str, Any], graded: dict[str, dict[str, Any]] | None = None) -> dict[str, Any]:
        checked("QuizSubmit", body)
        quiz = self.quiz(qid)
        self.identity_check("attempt", body["attempt_id"], body)
        if body["quiz_revision"] != quiz["view"]["revision"] or quiz["view"]["status"] != "ready":
            raise StudyError("source_stale", True)
        answers = {a["item_id"]: a["answer"] for a in body["answers"]}
        items = {i["item_id"]: i for i in quiz["view"]["items"]}
        if len(answers) != len(body["answers"]) or set(answers) - items.keys():
            raise StudyError("invalid_study_request")
        old = self.conn.execute(
            "SELECT quiz_id,body FROM study_attempts WHERE attempt_id=?", (body["attempt_id"],)
        ).fetchone()
        if old:
            if old[0] != qid:
                raise StudyError("attempt_conflict", True)
            return dict(json.loads(old[1]))
        results = []
        for key in quiz["keys"]:
            identity = key["item_id"]
            if identity not in answers:
                results.append({"item_id": identity, "verdict": "skipped"})
                continue
            answer = answers[identity].strip()
            kind = items[identity]["type"]
            options = {o["option_id"] for o in items[identity].get("options", [])}
            if kind == "choice" and answer not in options:
                raise StudyError("invalid_study_request")
            if kind == "multi_select" and (not answer_ids(answer) or not set(answer_ids(answer)) <= options):
                raise StudyError("invalid_study_request")
            if kind == "true_false" and answer not in ("true", "false"):
                raise StudyError("invalid_study_request")
            if graded and identity in graded:
                results.append(graded[identity])
                continue
            # Free response and algebra need a supported independent marker; never guess from lexical similarity.
            # A selection is compared with its model-written key, so it stays judged, not checked (STU-05, STU-18).
            # A fill-in-the-blank word that matches the key literally is correct; any other word is not guessed.
            if kind in ("choice", "true_false"):
                verdict = "correct" if answer.casefold() == key["answer"].strip().casefold() else "incorrect"
            elif kind == "multi_select":
                verdict = "correct" if answer_ids(answer) == answer_ids(key["answer"]) else "incorrect"
            elif kind == "fill_blank" and blank_form(answer) == blank_form(key["answer"]):
                verdict = "correct"
            else:
                verdict = "unverifiable"
            results.append(
                {
                    "item_id": identity,
                    "verdict": verdict,
                    "marking": "judged",
                    "label": "Judged, not checked",
                    "explanation": key["explanation"],
                }
            )
        result = checked("QuizResult", {"attempt_id": body["attempt_id"], "results": results})
        with atomic(self.conn):
            self.identity_save("attempt", body["attempt_id"], body, self.aid(quiz["view"]["aid_id"])["sources"])
            self.conn.execute(
                "INSERT INTO study_attempts VALUES (?,?,?,?)",
                (body["attempt_id"], qid, json.dumps(result), self.clock()),
            )
            self.conn.execute(
                "INSERT INTO study_quiz_submissions VALUES (?,?)",
                (body["attempt_id"], body.get("submission_kind", "submit")),
            )
            # A submission is where a draft ends: the set now shows what the learner answered (STU-21).
            if body.get("submission_kind") != "check":
                self.conn.execute(
                    "UPDATE study_quizzes SET body=json_set(json_remove(body,'$.view.draft'),"
                    "'$.submission',json(?)) WHERE quiz_id=?",
                    (json.dumps({"attempt_id": body["attempt_id"], "answers": body["answers"]}), qid),
                )
        # The activity's one seam (SPEC §19.4): after the submission committed, one quiz_item per item; a preliminary
        # check records nothing. A repeat returned the stored result above and never reaches here.
        if self.activity is not None and body.get("submission_kind") != "check":
            order = {item_id: i for i, item_id in enumerate(items)}
            keys = {k["item_id"]: k for k in quiz["keys"]}
            self.activity.quiz(body["attempt_id"], [
                (order.get(r["item_id"], i), item_text(items.get(r["item_id"], {}), keys.get(r["item_id"], {})),
                 r["verdict"])
                for i, r in enumerate(result["results"])
            ], material=self.quiz_material(quiz["view"]["aid_id"]))  # fmt: skip
        return result

    def quiz_material(self, aid_id: str) -> str:
        """The text of a quiz's sources, bounded, which names the topics its questions belong to. Used for tags only."""
        with contextlib.suppress(StudyError, sqlite3.Error, KeyError):
            return "\n".join(c["text"] for c in self.selected(self.aid(aid_id)["sources"], ready=False))[:200_000]
        return ""

    def save_draft(self, qid: str, body: dict[str, Any]) -> dict[str, Any]:
        """STU-21: keep a quiz's unsubmitted answers and place so the set opens where it was left. Never marked."""
        checked("QuizDraft", body)
        stored = self.quiz(qid)
        view = stored["view"]
        if body["quiz_revision"] != view["revision"] or view["status"] != "ready":
            raise StudyError("source_stale", True)
        ids = [a["item_id"] for a in body["answers"]]
        if len(set(ids)) != len(ids) or set(ids) - {i["item_id"] for i in view["items"]}:
            raise StudyError("invalid_study_request")
        if body["position"] >= len(view["items"]):
            raise StudyError("invalid_study_request")
        self.conn.execute(
            "UPDATE study_quizzes SET body=json_set(body,'$.view.draft',json(?)) WHERE quiz_id=?",
            (json.dumps(body), qid),
        )
        return body

    def progress(self, aid: dict[str, Any]) -> dict[str, Any] | None:
        """Live set progress (STU-21): questions not yet answered, or cards due by the end of today (UTC)."""
        if aid.get("quiz_id"):
            stored = self.quiz(aid["quiz_id"])
            total = len(stored["view"]["items"])
            done = {a["item_id"] for a in stored["view"].get("draft", {}).get("answers", [])}
            for row in self.conn.execute("SELECT body FROM study_attempts WHERE quiz_id=?", (aid["quiz_id"],)):
                done |= {r["item_id"] for r in json.loads(row[0])["results"] if r["verdict"] != "skipped"}
            return {"questions_total": total, "questions_left": max(0, total - len(done))}
        if aid.get("deck_id"):
            day = dt.datetime.fromtimestamp(self.clock() / 1000, dt.UTC).date() + dt.timedelta(days=1)
            end = int(dt.datetime.combine(day, dt.time(), dt.UTC).timestamp() * 1000)
            cards = [
                (json.loads(body), due)
                for body, due in self.conn.execute(
                    "SELECT body,due_at FROM study_flashcards WHERE deck_id=?", (aid["deck_id"],)
                )
            ]
            live = [due for card, due in cards if not card["suspended"]]
            return {"cards_total": len(cards), "cards_due_today": sum(1 for due in live if due < end)}
        return None

    def sets(self, granted: Callable[[list[dict[str, Any]]], bool], cursor: str | None = None) -> dict[str, Any]:
        """My Sets (STU-21): every generated aid but chat replies, newest first, with its progress. A set from a
        private source shows neither title nor progress until that source is opened with its grant (STU-19)."""
        try:
            offset = int(cursor or "0")
        except ValueError:
            raise StudyError("invalid_study_request") from None
        if offset < 0:
            raise StudyError("invalid_study_request")
        rows = self.conn.execute(
            "SELECT body FROM study_aids WHERE json_extract(body,'$.kind')!='chat' "
            "ORDER BY created_at DESC,aid_id LIMIT 101 OFFSET ?",
            (offset,),
        ).fetchall()
        items = []
        for (raw,) in rows[:100]:
            aid = json.loads(raw)
            private = any(self.source(s["source_id"])["private"] for s in aid["sources"])
            entry = {k: aid[k] for k in ("aid_id", "kind", "sources", "status", "created_at")} | {"private": private}
            if not private or granted(aid["sources"]):
                entry["title"] = aid["title"]
                progress = self.progress(aid)
                if progress is not None:
                    entry["progress"] = progress
            items.append(entry)
        return checked("StudySetList", {"items": items, "next_cursor": str(offset + 100) if len(rows) > 100 else None})

    def media(self, sid: str, revision: int) -> tuple[bytes, str]:
        """STU-22: an owned recording's own bytes for the player; it never leaves the device."""
        source = self.source(sid)
        if source["kind"] not in ("recording", "video"):
            raise StudyError("not_found")
        row = self.conn.execute(
            "SELECT blob_ref FROM study_revisions WHERE source_id=? AND revision=?", (sid, revision)
        ).fetchone()
        if row is None or row[0] is None:
            raise StudyError("not_found")
        data = (self.root / "blobs" / row[0]).read_bytes()
        return data, media_type(data)


class StudyQueue:
    def __init__(
        self,
        connection: Callable[[], Any],
        model: Callable[[], Any],
        busy: Callable[[], bool],
        clock: Callable[[], int],
        settings: Any,
    ):
        self.connection, self.model, self.busy, self.clock, self.settings = connection, model, busy, clock, settings
        self.cloud: Any = None
        self.checker: Callable[..., Any] | None = None
        self.content: Callable[[], Any] | None = None
        self.task: asyncio.Task[Any] | None = None
        self.next_online_retry = 0
        self.owner: Any = None
        self.grants: dict[str, tuple[Any, str, int, int]] = {}
        self.jid: str | None = None
        self.recovered_owner: Any = None
        self.next_recovery = 0
        self.next_cleanup = 0
        self.cleanup_task: asyncio.Task[Any] | None = None
        from agent_core.tools.perception import PerceptionClient

        self.perception: Callable[..., Any] = PerceptionClient(settings=settings).run
        # The open profile's Study session and activity recorder (`engine.study_session.StudyVisits`), once attached.
        self.activity: Any = None

    def desk(self) -> StudyDesk:
        conn = self.connection()
        if conn is None:
            raise StudyError("not_found")
        desk = StudyDesk(conn, self.clock, self.settings)
        desk.activity = self.activity
        return desk

    def aid_made(self, desk: StudyDesk, jid: str) -> None:
        """SPEC §19.4: an aid is recorded where it completes, once its job committed (never at `create_job`), in the
        open Study session; with none open, the next session's open records it at the time it was made."""
        if self.activity is None:
            return
        with contextlib.suppress(StudyError, sqlite3.Error, KeyError):
            job = desk.job(jid)
            if job["state"] != "completed":
                return
            from agent_core.engine.study_session import aid_count, aid_text

            for aid_id in job.get("result_ids", []):
                aid = desk.aid(aid_id)
                self.activity.aid(aid_id, aid["kind"], aid_count(desk.conn, aid), aid_text(aid))

    async def mark_online(
        self, desk: StudyDesk, aid_id: str, item: dict[str, Any], key: dict[str, Any], answer: str
    ) -> str | None:
        """A written answer no local marker could settle, marked through the gateway when this profile may use it: only
        the passages the question cites go, and any failure leaves it unmarked, which the learner is told."""
        if self.cloud is None or not hasattr(self.cloud, "mark_answer"):
            return None
        cited = {c["chunk_id"] for claim in (item["stem"], key["explanation"]) for c in claim["citations"]}
        with contextlib.suppress(StudyError, ValueError, TimeoutError, OSError):
            aid = desk.aid(aid_id)
            passages = [c["text"] for c in desk.selected(aid["sources"], ready=False) if c["chunk_id"] in cited]
            if not passages or not answer:
                return None
            said = await self.cloud.mark_answer(desk.conn, new_id("req"), {
                "item_type": item["type"],
                "question": item["stem"]["text"][:3000],
                "answer": answer[:4000],
                "reference": f"{key.get('answer', '')}\n{key['explanation']['text']}"[:6000],
                "passages": passages[:12],
            })  # fmt: skip
            return said if said in ("correct", "incorrect", "partial") else None
        return None

    async def submit_quiz(self, qid: str, body: dict[str, Any]) -> dict[str, Any]:
        if self.busy():
            raise StudyError("queue_full")
        desk = self.desk()
        checked("QuizSubmit", body)
        stored = desk.quiz(qid)
        desk.identity_check("attempt", body["attempt_id"], body)
        if desk.conn.execute("SELECT 1 FROM study_attempts WHERE attempt_id=?", (body["attempt_id"],)).fetchone():
            return desk.submit(qid, body)
        if body["quiz_revision"] != stored["view"]["revision"] or stored["view"]["status"] != "ready":
            raise StudyError("source_stale", True)
        items = {i["item_id"]: i for i in stored["view"]["items"]}
        keys = {k["item_id"]: k for k in stored["keys"]}
        identities = [a["item_id"] for a in body["answers"]]
        if len(set(identities)) != len(identities) or set(identities) - items.keys():
            raise StudyError("invalid_study_request")
        graded = {}

        async def check(a: str, b: str) -> dict[str, Any] | None:
            return await self.checker("math_check_equivalence", {"a": a, "b": b}) if self.checker else None

        for response in body["answers"]:
            identity, answer = response["item_id"], response["answer"].strip()
            if identity not in items:
                raise StudyError("invalid_study_request")
            item, key = items[identity], keys[identity]
            if item["type"] in ("choice", "multi_select", "true_false") or (
                item["type"] == "fill_blank" and blank_form(answer) == blank_form(key["answer"])
            ):
                # These types use the exact, validated key comparison in StudyDesk.submit.
                # The independent-marker fallback must not override their verdicts.
                continue
            value: dict[str, Any] = {
                "item_id": identity,
                "verdict": "unverifiable",
                "marking": "judged",
                "label": "Judged, not checked",
                "explanation": key["explanation"],
            }
            if item["type"] in ("numeric", "expression") and key.get("calculation"):
                expected, submitted = key["answer"], answer
                unit = item.get("unit_prompt")
                converted = None
                if unit and self.checker:
                    converted = await self.checker("units_convert", {"quantity": answer, "to": unit})
                    match = re.fullmatch(r"\s*([-+0-9.eE]+)\s*" + re.escape(unit) + r"\s*", expected)
                    if converted and match:
                        expected = match[1]
                        submitted = str(converted["value"]["value"]["decimal"])
                    else:
                        graded[identity] = value
                        continue
                key_check = await check(key["calculation"], expected)
                if (
                    key_check
                    and key_check["value"].get("verdict") == "equivalent"
                    and key_check["check"].get("confidence") == "proved"
                ):
                    marked = await check(key["calculation"], submitted)
                    if (
                        marked
                        and marked["check"].get("confidence") == "proved"
                        and marked["value"].get("verdict") in ("equivalent", "not_equivalent")
                    ):
                        ids = [key_check["check"]["check_id"], marked["check"]["check_id"]]
                        if converted:
                            ids.append(converted["check"]["check_id"])
                        value.update(
                            verdict="correct" if marked["value"]["verdict"] == "equivalent" else "incorrect",
                            marking="checked",
                            label="Checked",
                            check_ids=ids,
                        )
            elif item["type"] == "fill_blank":
                # A saved quiz has already passed extraction review. Only its cited passages are needed;
                # sending the whole PDF into a short-answer marker wastes context and can hide the evidence.
                aid = desk.aid(stored["view"]["aid_id"])
                cited = {c["chunk_id"] for claim in (item["stem"], key["explanation"]) for c in claim["citations"]}
                chunks = [c for c in desk.selected(aid["sources"], ready=False) if c["chunk_id"] in cited]
                model = self.model()
                if chunks and model is not None and hasattr(model, "study_grade_support"):
                    try:
                        if await model.local_ready():
                            judgement, local = await model.study_grade(item, key, answer, chunks)
                            if self.connection() is not desk.conn:
                                raise StudyError("source_deleted")
                            if local and not errors(PREFIX + "Grading", judgement):
                                desk.citation(judgement["reason"], aid["sources"])
                                supported, local = await model.study_grade_support(item, key, answer, judgement, chunks)
                                if local and supported is True and judgement["verdict"] in ("correct", "incorrect"):
                                    value.update(verdict=judgement["verdict"], explanation=judgement["reason"])
                    except (ValueError, RouterError, TimeoutError):
                        pass  # Keep a safe unverifiable result if a local marker is unavailable or invalid.
            elif item["type"] == "free_response":
                model = self.model()
                if model is not None and await model.local_ready() and hasattr(model, "study_grade"):
                    aid = desk.aid(stored["view"]["aid_id"])
                    chunks = desk.selected(aid["sources"])
                    judgement, local = await model.study_grade(item, key, answer, chunks)
                    if local and judgement.get("verdict") in ("correct", "incorrect", "partial", "unverifiable"):
                        reason = judgement["reason"]
                        desk.citation(reason, aid["sources"])
                        support, local = await model.study_support({}, chunks, {"claims": [reason]})
                        if local and support == [True]:
                            value.update(verdict=judgement["verdict"], explanation=reason)
            if value["verdict"] == "unverifiable" and item["type"] in ONLINE_MARKED:
                said = await self.mark_online(desk, stored["view"]["aid_id"], item, key, answer)
                if said is not None:
                    value["verdict"] = said
            graded[identity] = value
            if self.connection() is not desk.conn:
                raise StudyError("source_deleted")
        if self.busy():
            raise StudyError("queue_full")
        return desk.submit(qid, body, graded)

    def authorise(self, sid: str, headers: str = "", revision: int | None = None) -> dict[str, Any]:
        desk = self.desk()
        source = desk.source(sid)
        if source["private"]:
            grants = headers.split(",")
            if len(grants) > 8 or not any(
                self.grants.get(t.strip()) == (desk.conn, sid, revision or source["revision"], expiry)
                and expiry > self.clock()
                for t in grants
                for expiry in [self.grants.get(t.strip(), (None, None, None, 0))[3]]
            ):
                raise StudyError("grant_expired")
        return source

    def open(self, sid: str, revision: int | None = None) -> dict[str, Any]:
        desk = self.desk()
        source = desk.source(sid)
        if revision is not None and revision != source["revision"]:
            row = desk.conn.execute(
                "SELECT digest FROM study_revisions WHERE source_id=? AND revision=?", (sid, revision)
            ).fetchone()
            if row is None:
                raise StudyError("not_found")
            count = desk.conn.execute(
                "SELECT count(*) FROM study_chunks WHERE source_id=? AND revision=?", (sid, revision)
            ).fetchone()[0]
            source = {
                **source,
                "revision": revision,
                "sha256": row[0],
                "state": "ready" if count else "unreadable",
                "readable_units": count,
                "unreadable_units": [],
            }
        token, expiry = secrets.token_urlsafe(32), self.clock() + 300_000
        self.grants = {t: v for t, v in self.grants.items() if v[3] > self.clock()}
        if len(self.grants) >= 512:
            self.grants.pop(next(iter(self.grants)))
        self.grants[token] = (self.connection(), sid, source["revision"], expiry)
        return {
            "source": source,
            "open_token": token,
            "expires_at": stamp(expiry),
            "available_revisions": [
                r[0]
                for r in desk.conn.execute(
                    "SELECT revision FROM study_revisions WHERE source_id=? "
                    "AND extracted_at IS NOT NULL ORDER BY revision",
                    (sid,),
                )
            ]
            or [source["revision"]],
        }

    def pause(self, *, clear: bool = False) -> None:
        if self.task is not None and not self.task.done():
            self.task.cancel()
        if self.owner is not None and self.jid is not None:
            with contextlib.suppress(sqlite3.Error, StudyError):
                desk = StudyDesk(self.owner, self.clock, self.settings)
                job = desk.job(self.jid)
                if job["state"] == "running":
                    checkpoint = desk.one("SELECT checkpoint FROM study_jobs WHERE job_id=?", (self.jid,))
                    cloud = checkpoint["request"]["execution"] == "prefer_cloud"
                    job.update(state="cancelled" if clear and cloud else "paused", error=None)
                    if clear and cloud:
                        desk.conn.execute(
                            "UPDATE study_jobs SET checkpoint=json_remove(checkpoint,'$.batches') WHERE job_id=?",
                            (self.jid,),
                        )
                    desk.save_job(job)
        if clear:
            connection = self.owner if self.owner is not None else self.connection()
            if connection is not None:
                with contextlib.suppress(sqlite3.Error):
                    # A profile close cancels every outstanding cloud send, including jobs that never started.
                    # Local work remains resumable. Keep opaque cleanup receipts until remote acknowledgement.
                    connection.execute(
                        "UPDATE study_jobs SET state='cancelled', "
                        "body=json_set(body,'$.state','cancelled'), "
                        "checkpoint=json_remove(checkpoint,'$.batches') "
                        "WHERE state IN ('queued','running','waiting_online','paused') "
                        "AND json_extract(checkpoint,'$.request.execution')='prefer_cloud'"
                    )
            self.grants.clear()
            self.recovered_owner = None
        self.owner, self.jid = None, None

    def outstanding(self) -> bool:
        """A job is running, or one is queued or running in the open learner database."""
        if self.task is not None and not self.task.done():
            return True
        conn = self.connection()
        if conn is None:
            return False
        with contextlib.suppress(sqlite3.Error):
            return bool(conn.execute("SELECT 1 FROM study_jobs WHERE state IN ('queued','running') LIMIT 1").fetchone())
        return False

    def cancel_cloud(self) -> None:
        """A consent/subscription/switch withdrawal cancels cloud work without stopping local generation."""
        conn = self.connection()
        if conn is None:
            return
        if self.owner is conn and self.jid is not None:
            with contextlib.suppress(sqlite3.Error, StudyError):
                request = self.desk().one("SELECT checkpoint FROM study_jobs WHERE job_id=?", (self.jid,))["request"]
                if request["execution"] == "prefer_cloud" and self.task is not None:
                    self.task.cancel()
        conn.execute(
            "UPDATE study_jobs SET state='cancelled',body=json_set(body,'$.state','cancelled'), "
            "checkpoint=json_remove(checkpoint,'$.batches') "
            "WHERE state IN ('queued','running','waiting_online','paused') "
            "AND json_extract(checkpoint,'$.request.execution')='prefer_cloud'"
        )

    async def close(self) -> None:
        self.pause(clear=True)
        tasks = [task for task in (self.task, self.cleanup_task) if task is not None]
        if self.cleanup_task:
            self.cleanup_task.cancel()
        if tasks:
            await asyncio.gather(*tasks, return_exceptions=True)

    def retry_waiting_online(self, desk: StudyDesk) -> None:
        """A job parked because the gateway could not be reached goes again by itself once in a while, so a brief outage
        never leaves a learner's material waiting until they press Resume. Offline, a retry is refused on this device
        before anything is sent, and the attempt is unchanged, so the gateway bills a retried request only once."""
        if self.cloud is None or self.clock() < self.next_online_retry:
            return
        self.next_online_retry = self.clock() + ONLINE_RETRY_MS
        rows = desk.conn.execute(
            "SELECT job_id FROM study_jobs WHERE state='waiting_online' "
            "AND json_extract(checkpoint,'$.request.execution')='prefer_cloud'"
        ).fetchall()
        for row in rows:
            job = desk.job(row[0])
            job.update(state="queued", error=None)
            desk.save_job(job)

    def poll(self) -> None:
        if self.cleanup_task is not None and not self.cleanup_task.done():
            return
        if self.task is not None and not self.task.done():
            if self.owner is not self.connection() or self.busy():
                self.pause()
            return
        if self.connection() is None or self.busy():
            return
        desk = self.desk()
        if self.recovered_owner is not desk.conn or self.clock() >= self.next_recovery:
            with contextlib.suppress(OSError):
                desk.recover_files()
            self.recovered_owner = desk.conn
            self.next_recovery = self.clock() + 3600000
        if (
            self.cloud is not None
            and self.clock() >= self.next_cleanup
            and (self.cleanup_task is None or self.cleanup_task.done())
        ):
            self.next_cleanup = self.clock() + 30000
            if desk.conn.execute("SELECT 1 FROM study_cloud_cleanup LIMIT 1").fetchone():

                async def cleanup() -> None:
                    with contextlib.suppress(Exception):
                        await self.cloud.cleanup(desk.conn)

                self.cleanup_task = asyncio.create_task(cleanup())
                return
        self.retry_waiting_online(desk)
        # Running is crash recovery only when there is no live task for it.
        row = desk.conn.execute(
            "SELECT job_id FROM study_jobs WHERE state IN ('queued','running') ORDER BY updated_at,job_id LIMIT 1"
        ).fetchone()
        if row:
            self.owner, self.jid = desk.conn, row[0]
            self.task = asyncio.create_task(self.run(desk, row[0]))

    async def generate_batches(
        self, desk: StudyDesk, job: dict[str, Any], request: dict[str, Any], chunks: list[dict[str, Any]]
    ) -> None:
        batches: list[list[dict[str, Any]]] = []
        for chunk in chunks:
            if (
                not batches
                or len(batches[-1]) >= 64
                or sum(len(c["text"]) for c in batches[-1]) + len(chunk["text"]) > self.settings.study_batch_char_limit
            ):
                batches.append([])
            batches[-1].append(chunk)
        if not batches or len(batches) > 128:
            raise StudyError("source_too_large")
        jid = job["job_id"]
        focused = bool(request.get("settings", {}).get("focus", "").strip())
        if len(batches) > LONG_SOURCE_BATCHES and not focused and request["kind"] in SAMPLED_KINDS:
            # A textbook's whole length would be 100 model calls for ten questions. Spread evenly and say so; a focus
            # (PDF pages 14-16, a chapter's name) reads exactly what was asked for.
            step = (len(batches) - 1) / (LONG_SOURCE_BATCHES - 1)
            batches = [batches[round(i * step)] for i in range(LONG_SOURCE_BATCHES)]
            job["notice"] = "sampled_long_source"
        if request["kind"] in ("concept_map", "teaching_outline") and request["execution"] != "prefer_cloud":
            raise StudyError("model_unavailable")  # drafted and verified by Muse Spark only
        checkpoint = desk.one("SELECT checkpoint FROM study_jobs WHERE job_id=?", (jid,))
        saved = checkpoint.get("batches", [])
        # Chat is one response to one learner question, not independent answers concatenated
        # for every chapter. Quiz coaching already has a narrowly scoped, protected context.
        synthesize = (
            request["kind"] == "chat"
            and len(batches) > 1
            and not request.get("settings", {}).get("focus", "").startswith("Coaching withheld wording: ")
        )
        job["total_units"] = len(chunks) + 2 * len(batches) + (2 if synthesize else 0)
        desk.save_job(job)
        outputs = []
        model = self.model()
        for index, batch in enumerate(batches):
            sha = digest(json.dumps([(c["chunk_id"], c["sha256"]) for c in batch]).encode())
            if index < len(saved):
                if saved[index]["input_digest"] != sha:
                    raise StudyError("source_stale")
                output = saved[index]["output"]
            else:
                batch_request = dict(request)
                if synthesize:
                    batch_request["kind"] = "notes"
                if request["kind"] in ("flashcards", "quiz"):
                    batch_request["count"] = max(1, (request.get("count", 10) + len(batches) - 1) // len(batches))
                if synthesize:
                    output = chat_source_notes(jid, request, batch)
                    support = [True] * len(output["claims"])
                elif request["execution"] == "prefer_cloud":
                    if self.cloud is None:
                        raise StudyError("consent_required")
                    output = await self.cloud.process(
                        desk.conn,
                        jid,
                        batch_request,
                        batch,
                        checkpoint["attempt"],
                        batch_index=index,
                    )
                    chaptered(output)
                    outlined(output)
                    support = [True] * (
                        len(output["claims"])
                        + len(output.get("flashcards", []))
                        + len(output.get("quiz", {}).get("items", []))
                    )
                else:
                    if model is None or not await model.local_ready():
                        raise StudyError("model_unavailable")
                    output, local = await model.study_generate(jid, batch_request, batch)
                    if not local:
                        raise StudyError("consent_required")
                    located(output, batch)
                    chaptered(output)
                    support, local = await model.study_support(batch_request, batch, output)
                    if not local:
                        raise StudyError("consent_required")
                if self.connection() is not desk.conn or desk.job(jid)["state"] != "running":
                    return
                checked("ProcessingResponse", output)
                groups = (
                    len(output["claims"])
                    + len(output.get("flashcards", []))
                    + len(output.get("quiz", {}).get("items", []))
                )
                if (
                    output["request_id"] != jid
                    or output["kind"] != batch_request["kind"]
                    or len(support) != groups
                    or not all(type(v) is bool for v in support)
                ):
                    raise StudyError("insufficient_evidence")
                if not all(support[len(output["claims"]) :]):
                    raise StudyError("insufficient_evidence")
                # A chapter's summary is shown whole or not at all: every sentence must be supported (STU-22).
                if request["kind"] == "chapters" and not all(support):
                    raise StudyError("insufficient_evidence")
                if request["kind"] == "teaching_outline":
                    kept = study_outline.rebuild(output["outline"], support[: len(output["claims"])])
                    if kept is None:
                        raise StudyError("insufficient_evidence")
                    output["outline"] = kept
                    outlined(output)
                elif request["kind"] != "chapters":
                    output["claims"] = [c for i, c in enumerate(output["claims"]) if support[i]][
                        : 200 if synthesize else max(1, 200 // len(batches))
                    ]
                if not output["claims"]:
                    raise StudyError("insufficient_evidence")
                for claim in [
                    *output["claims"],
                    *(v[k] for v in output.get("flashcards", []) for k in ("front", "back")),
                    *(v["stem"] for v in output.get("quiz", {}).get("items", [])),
                    *(v["explanation"] for v in output.get("quiz", {}).get("keys", [])),
                    *(study_concepts.draft_claims(output["concept_map"]) if "concept_map" in output else []),
                ]:
                    desk.citation(claim, job["sources"])
                saved.append({"input_digest": sha, "output": output})
                checkpoint.update(
                    batches=saved, completed_chunk_ids=[c["chunk_id"] for b in batches[: index + 1] for c in b]
                )
                checked("Checkpoint", checkpoint)
                with atomic(desk.conn):
                    desk.selected(job["sources"], ready=False)
                    desk.conn.execute(
                        "UPDATE study_jobs SET checkpoint=? WHERE job_id=?", (json.dumps(checkpoint), jid)
                    )
                    job["completed_units"] = len(checkpoint["completed_chunk_ids"]) + 2 * len(saved)
                    desk.save_job(job)
            outputs.append(output)
        named = [o["model"] for o in outputs if isinstance(o.get("model"), str) and o["model"]]
        combined: dict[str, Any] = {
            "request_id": jid,
            "kind": request["kind"],
            "claims": [c for output in outputs for c in output["claims"]],
            **({"model": named[0]} if named else {}),
        }
        if synthesize:
            prompt = request.get("prompt", "").casefold()
            opening_lesson = bool(
                re.search(r"\b(?:teach|learn|explain)\b", prompt) and re.search(r"\b(?:beginning|start)\b", prompt)
            )
            excerpts, offsets = chat_evidence(
                batches,
                outputs,
                self.settings.study_batch_char_limit * 2,
                opening_lesson=opening_lesson,
                prompt=request.get("prompt", ""),
            )
            if request["execution"] == "prefer_cloud":
                combined = await self.cloud.process(
                    desk.conn, jid, request, excerpts, checkpoint["attempt"], batch_index=256
                )
                support = [True] * len(combined["claims"])
            else:
                if model is None or not await model.local_ready():
                    raise StudyError("model_unavailable")
                combined, local = await model.study_generate(jid, request, excerpts)
                if not local:
                    raise StudyError("consent_required")
                located(combined, excerpts)
                support, local = await model.study_support(request, excerpts, combined)
                if not local:
                    raise StudyError("consent_required")
            if self.connection() is not desk.conn or desk.job(jid)["state"] != "running":
                return
            checked("ProcessingResponse", combined)
            if (
                combined["request_id"] != jid
                or combined["kind"] != "chat"
                or not combined["claims"]
                or len(support) != len(combined["claims"])
                or not all(v is True for v in support)
            ):
                raise StudyError("insufficient_evidence")
            by_id = {c["chunk_id"]: c for c in excerpts}
            for claim in combined["claims"]:
                for citation in claim["citations"]:
                    identity = citation["chunk_id"]
                    if (
                        identity not in by_id
                        or not 0 <= citation["start"] < citation["end"] <= len(by_id[identity]["text"])
                        or by_id[identity]["text"][citation["start"] : citation["end"]] != citation["quote"]
                    ):
                        raise StudyError("insufficient_evidence")
                    citation["start"] += offsets[identity]
                    citation["end"] += offsets[identity]
                desk.citation(claim, job["sources"])
        count = request.get("count", 10)

        def sample(groups: list[list[Any]]) -> list[Any]:
            # Give each verified section a turn before taking a second item from any section.
            # A verbose early section must not crowd out later chapters. Keep document order in the result.
            nonempty = [g for g in groups if g]
            if not nonempty:
                return []
            if len(nonempty) > count:
                indices = (
                    [len(nonempty) // 2]
                    if count == 1
                    else [i * (len(nonempty) - 1) // (count - 1) for i in range(count)]
                )
                nonempty = [nonempty[i] for i in indices]
            chosen: list[tuple[int, int, Any]] = []
            for offset in range(max(len(group) for group in nonempty)):
                for index, group in enumerate(nonempty):
                    if offset < len(group):
                        chosen.append((index, offset, group[offset]))
                        if len(chosen) == count:
                            return [item for _, _, item in sorted(chosen, key=lambda x: (x[0], x[1]))]
            return [item for _, _, item in sorted(chosen, key=lambda x: (x[0], x[1]))]

        if request["kind"] == "concept_map":
            # One map from every verified section: concepts by name, relations re-pointed (study_concepts).
            combined["concept_map"] = study_concepts.merge_drafts([o["concept_map"] for o in outputs])
            combined["claims"] = study_concepts.draft_claims(combined["concept_map"])[:200]
        if request["kind"] == "teaching_outline":
            combined["outline"] = study_outline.merge([o["outline"] for o in outputs])
            combined["claims"] = study_outline.flatten(combined["outline"])[:200]
        if request["kind"] == "chapters":
            combined["chapters"] = [c for output in outputs for c in output["chapters"]]
            if len(combined["chapters"]) > 60:
                raise StudyError("insufficient_evidence")
            combined["claims"] = [s for c in combined["chapters"] for s in c["summary"]]
        settings = request.get("settings", {})
        if request["kind"] == "audio_overview":
            limit = PODCAST_CLAIMS[settings.get("podcast_length", "standard")]
            spoken = combined["claims"]
            combined["claims"] = (
                spoken
                if len(spoken) <= limit
                else [spoken[min(len(spoken) - 1, i * len(spoken) // limit)] for i in range(limit)]
            )
        if request["kind"] == "flashcards":
            seen_fronts: set[str] = set()
            card_groups = []
            for output in outputs:
                group = []
                for card in output["flashcards"]:
                    front = " ".join(card["front"]["text"].split()).casefold()
                    if front not in seen_fronts:
                        seen_fronts.add(front)
                        group.append(card)
                card_groups.append(group)
            combined["flashcards"] = sample(card_groups)
        if request["kind"] == "quiz":
            pair_groups = []
            seen = set()
            for output in outputs:
                pairs = []
                keys = {k["item_id"]: k for k in output["quiz"]["keys"]}
                if len(keys) != len(output["quiz"]["keys"]) or len(keys) != len(output["quiz"]["items"]):
                    raise StudyError("insufficient_evidence")
                for item in output["quiz"]["items"]:
                    # Only the question types the learner chose reach the quiz (STU-21).
                    if settings.get("question_types") and item["type"] not in settings["question_types"]:
                        continue
                    text = " ".join(item["stem"]["text"].split()).casefold()
                    if text in seen:
                        continue
                    seen.add(text)
                    if item["item_id"] not in keys:
                        raise StudyError("insufficient_evidence")
                    if item["type"] == "fill_blank" and not valid_blank(item, keys[item["item_id"]]):
                        continue
                    pairs.append((item, keys[item["item_id"]]))
                pair_groups.append(pairs)
            if not any(pair_groups):
                raise StudyError("insufficient_evidence")
            items, final_keys = [], []
            for index, (item, key) in enumerate(sample(pair_groups)):
                identity = "q" + str(index + 1)
                items.append({**item, "item_id": identity})
                final_keys.append({**key, "item_id": identity})
            combined["quiz"] = {"items": items, "keys": final_keys}
        groups = (
            len(combined["claims"])
            + len(combined.get("flashcards", []))
            + len(combined.get("quiz", {}).get("items", []))
        )
        desk.commit_aid(job, combined, [True] * groups, self.content() if self.content is not None else None)
        desk.conn.execute("UPDATE study_jobs SET checkpoint=json_remove(checkpoint,'$.batches') WHERE job_id=?", (jid,))

    def retry_refused(self, desk: StudyDesk, jid: str) -> bool:
        """A generated aid the verifier refused, or a model reply that could not be read, goes again once by itself,
        from its verified sections, so one weak draft is not the learner's failure (the 2 November rehearsal). The
        attempt number is the gateway's billing identity: each press of Try again earns one more automatic try, never
        a loop. A learner's question that the material cannot answer is not a weak draft: chat and quiz help stay
        refused."""
        row = desk.one("SELECT checkpoint FROM study_jobs WHERE job_id=?", (jid,))
        request = row["request"]
        if request.get("execution") != "prefer_cloud" or request.get("kind") not in RETRIED_KINDS:
            return False
        if int(row.get("attempt", 1)) % 2 == 0:
            return False  # this press has had its automatic try
        desk.conn.execute(
            "UPDATE study_jobs SET attempt=attempt+1,checkpoint=json_set(checkpoint,'$.attempt',attempt+1) "
            "WHERE job_id=?",
            (jid,),
        )
        job = desk.job(jid)
        job.update(state="queued", error=None)
        desk.save_job(job)
        return True

    async def run(self, desk: StudyDesk, jid: str) -> None:
        try:
            job = desk.job(jid)
            desk.selected(job["sources"], ready=False)
            request = desk.one("SELECT checkpoint FROM study_jobs WHERE job_id=?", (jid,))["request"]
            job.update(state="running", error=None)
            desk.save_job(job)
            if request["kind"] == "extract":
                s = desk.pending_source(request["sources"][0]["source_id"])
                row = desk.conn.execute(
                    "SELECT blob_ref FROM study_revisions WHERE source_id=? AND revision=?",
                    (s["source_id"], s["revision"]),
                ).fetchone()
                if s["state"] != "queued" or not row or not row[0]:
                    raise StudyError("unreadable_source")
                data = await asyncio.to_thread((desk.root / "blobs" / row[0]).read_bytes)
                if self.connection() is not desk.conn or desk.job(jid)["state"] != "running":
                    return
                cloud = request["execution"] == "prefer_cloud"
                if not cloud and not self.settings.study_offline_enabled:
                    # Recover uploads queued by older clients using the local extractor.
                    # StudyCloud.extract still checks this profile's consent before egress.
                    cloud = True
                    request = {**request, "execution": "prefer_cloud"}
                    desk.conn.execute(
                        "UPDATE study_jobs SET checkpoint=json_set(checkpoint,'$.request.execution','prefer_cloud') "
                        "WHERE job_id=?",
                        (jid,),
                    )
                if s["kind"] in ("recording", "video") and not cloud:
                    job.update(state="waiting_online", error=None)
                    desk.save_job(job)
                    return
                frames: list[dict[str, Any]] | None = None
                if cloud:
                    if self.cloud is None:
                        raise StudyError("consent_required")
                    units = await self.cloud.extract(desk.conn, jid, request, s, data)
                    # SPEC §20.4: a video file's frames (read as text in the worker) and the downgrade when the
                    # container had no video stream, both the gateway's to say.
                    if s["kind"] == "video":
                        frames = list(getattr(units, "frames", []) or [])
                        if getattr(units, "has_video", None) is False:
                            s["kind"], frames = "recording", None
                else:
                    units = await extract(data, s["kind"], s["title"], self.settings)
                for unit in units if not cloud else []:
                    if unit.get("ocr"):
                        png = await raster(data, s["kind"], unit["locator"]["page"], self.settings)
                        result = await self.perception(png)
                        if result.get("refusal"):
                            raise StudyError("safety_blocked")
                        if result.get("ocr_model_version") is None:
                            raise StudyError("model_unavailable")
                        # A low-confidence line cannot become evidence. The learner may correct a new revision.
                        unit["text"] = confident_text(result.get("lines", []))
                    if self.connection() is not desk.conn or desk.job(jid)["state"] != "running":
                        return
                if self.connection() is not desk.conn or desk.job(jid)["state"] != "running":
                    return
                with transaction(desk.conn):
                    desk.commit_units(s, units, frames=frames)
                    unreadable = desk.source(s["source_id"])["state"] == "unreadable"
                    job.update(
                        state="failed" if unreadable else "completed",
                        error="unreadable_source" if unreadable else None,
                        completed_units=len(units),
                        total_units=max(1, len(units)),
                    )
                    desk.save_job(job)
                return
            if request["execution"] == "local" and not desk.local_possible(request):
                job.update(state="waiting_online", error=None)
                desk.save_job(job)
                return
            chunks = desk.scoped(
                request, desk.selected(request["sources"], ready=not request.get("confirm_readable_only", False))
            )
            chunks = desk.quiz_help_scope(jid, chunks)
            brief = desk.quiz_help_brief(jid, desk.brief(request))
            try:
                await self.generate_batches(desk, job, brief, chunks)
            except ValueError as error:
                if getattr(error, "reason", None) != "allowance_exhausted" or not desk.local_possible(request):
                    raise
                job["notice"] = "allowance_exhausted"
                desk.save_job(job)
                await self.generate_batches(desk, job, {**brief, "execution": "local"}, chunks)
            if self.connection() is desk.conn:
                self.aid_made(desk, jid)
        except asyncio.CancelledError:
            raise
        except Exception as error:
            if self.connection() is desk.conn:
                with contextlib.suppress(sqlite3.Error, StudyError):
                    job = desk.job(jid)
                    if job["state"] == "running":
                        reason = (
                            error.reason
                            if isinstance(error, StudyError) or hasattr(error, "reason")
                            else str(error)
                            if isinstance(error, DocumentError)
                            else "processing_failed"
                        )
                        if reason == "waiting_online":
                            job.update(state="waiting_online", error=None)
                            desk.save_job(job)
                            return
                        if errors(PREFIX + "StudyError", reason):
                            reason = "processing_failed"
                        if reason in ("insufficient_evidence", "model_unavailable") and self.retry_refused(desk, jid):
                            return
                        job.update(state="failed", error=reason)
                        desk.save_job(job)
        finally:
            if self.cloud is not None:
                with contextlib.suppress(Exception):
                    await self.cloud.cleanup(desk.conn)
