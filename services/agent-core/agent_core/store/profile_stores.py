"""Every store that can hold a profile's data, in one registry that profile deletion and the retention job both walk
(DAT-08, DAT-09, DAT-14, MEM-22, UIA-06, UIA-08).

Each `ProfileStore` names what it holds, where it lives, the eraser that removes it when the profile is deleted, and
its row of TABLE DAT-08a (`retention.RULES`, by row class) or, for a store no retention row governs, how long it
lives. The retention job applies the rows in the registry's order (`retention_order`); profile deletion runs each
eraser once, in `ERASERS` order (`Erasers.erase`): what the process holds in memory first, the files and rows on disk
last, so nothing re-creates what was already erased.

Erasers are registered by the component that owns the data (`Erasers.register`); a name that is not an eraser of
the registry is refused, so a new store has to be listed here before anything can erase it.
`tests/test_a1_erasure.py` fails when a retention row, a file holding a profile's text or an eraser is not accounted
for here."""

from __future__ import annotations

import inspect
from collections.abc import Callable
from dataclasses import dataclass
from typing import Any

# The erasers, in the order profile deletion runs them.
ERASERS = (
    "core",  # CoreService.forget_profile(drop=True): sessions, rings, checkpoints, idempotency, jobs, close hooks
    "model_caches",  # CoreService.clear_caches: the model runtime's KV and prompt caches
    "lesson_positions",  # AppRuntime.drop_lesson_positions
    "online",  # AppRuntime.forget_profile_online: first-use egress decisions
    "traces",  # TraceStore.delete_profile
    "profile_directory",  # ProfileDirectory.delete: the profile folder, its key and its device rows
)
NONE = ""  # a store that holds no profile's data past its own short life


@dataclass(frozen=True)
class ProfileStore:
    name: str
    holds: str
    where: str  # a path under the data folder ({profile} is the profile's folder name), or memory, keystore, runtime
    erased_by: str  # one of ERASERS, or NONE with `lifetime` saying why nothing has to
    rule: str | None = None  # the TABLE DAT-08a row class (`retention.RULES`) that governs it
    lifetime: str = ""  # for a store with no rule: how long it lives


LEARNER = "profiles/{profile}/learner.db"
DEVICE = "device.db"

PROFILE_STORES: tuple[ProfileStore, ...] = (
    # --- held in memory while the profile is open -------------------------------------------------------------------
    ProfileStore("open sessions", "sessions, their projections, event rings and replay frames, the idempotency cache",
                 "memory", "core", lifetime="until the profile closes (lock, switch, idle lock or deletion)"),
    ProfileStore("Reflector jobs", "queued and running Reflector jobs over the profile's sessions", "memory", "core",
                 lifetime="cancelled when the profile closes"),
    ProfileStore("capture images", "photos and their derivatives waiting for confirmation (perception input)",
                 "memory", "core", lifetime="until confirmed or the profile closes (CaptureDesk.forget)"),
    ProfileStore("voice drafts", "dictated questions and notes waiting for confirmation, read-aloud utterances",
                 "memory", "core", lifetime="10 minutes, or until the profile closes (VoiceActivities.forget)"),
    ProfileStore("voice session", "the voice binding, its audio buffers and turn jobs, the calm-screen hold",
                 "memory", "core", lifetime="until voice stops or the profile closes (VoiceRuntime, adapter)"),
    ProfileStore("model caches", "the local runtime's KV and prompt caches of the profile's prompts", "runtime",
                 "model_caches", lifetime="cleared when the profile closes, switches or is deleted (DAT-14)"),
    ProfileStore("lesson positions in memory", "where each lesson stands this run", "memory", "lesson_positions",
                 lifetime="the life of the process; dropped with the profile"),
    # --- the profile's learner database: TABLE DAT-08a, in the order the job applies it -----------------------------
    ProfileStore("learner record", "events, consents, profile, settings, sessions, lesson positions", LEARNER,
                 "profile_directory", "life of the profile"),
    ProfileStore("projections", "mastery, reviews, misconceptions, notes and their index, plans", LEARNER,
                 "profile_directory", "projections"),
    ProfileStore("exams", "mock sittings and their marks", LEARNER, "profile_directory", "exams"),
    ProfileStore("study notes", "typed and dictated study notes", LEARNER, "profile_directory", "study notes"),
    ProfileStore("companion episodes", "the Study companion's daily episodes: their session, day and opener facts "
                 "(SPEC §19.4)", LEARNER, "profile_directory", "companion episodes"),
    ProfileStore("companion turn provenance", "memory revision and egress policy for retained companion turns",
                 LEARNER, "profile_directory", "companion turn provenance"),
    ProfileStore("companion summary", "the companion thread's rolling summary, folded by code: ids, bands and counts, "
                 "no free text (SPEC §19.5.3)", LEARNER, "profile_directory", "companion summary"),
    ProfileStore("check-ins", "the tutor's check-ins: kind, subject, Jana's question, due window and state (SPEC "
                 "§19.3.4)", LEARNER, "profile_directory", "check-ins"),
    ProfileStore("note recalls", "which note the tutor recalled in which session and day (SPEC §19.3.5), ids only",
                 LEARNER, "profile_directory", "note recalls"),
    ProfileStore("source study workspace", "source revisions, chunks, aids, cards, reviews, quizzes and jobs", LEARNER,
                 "profile_directory", "source study workspace"),
    ProfileStore("study concept maps of typed topics", "concept maps of typed topics and the learner's edits to them",
                 LEARNER, "profile_directory", "study concept maps of typed topics"),
    ProfileStore("video frames", "the writing on a video file's frames read as text in the worker (SPEC §20.3), "
                 "never an image; a screened frame is a count", LEARNER, "profile_directory", "video frames"),
    ProfileStore("study packs from the research agent", "approved research briefs, progress and the parts made",
                 LEARNER, "profile_directory", "study packs from the research agent"),
    ProfileStore("source material files", "hashed original blobs and incomplete uploads", "profiles/{profile}/study",
                 "profile_directory", lifetime="until source/profile deletion; unreferenced files collected on open"),
    ProfileStore("study jobs and private grants", "in-flight source jobs and five-minute private-source grants",
                 "memory", "core", lifetime="paused and grants discarded on close, switch or deletion"),
    ProfileStore("private note marks", "the ids of the study notes the student marked private (D102), never text: "
                 "the side file of a core before API 1.10, moved into study_notes.private (D104) and removed",
                 "profiles/{profile}/study-notes-private.json", "profile_directory",
                 lifetime="until the profile's notes are next read, or erased with the profile's folder"),
    ProfileStore("support sharing confirmations", "the student's own support shares", LEARNER, "profile_directory",
                 "support sharing confirmations"),
    ProfileStore("sync cursors", "personal multi-device cursors", LEARNER, "profile_directory", "sync cursors"),
    ProfileStore("transcript", "turns, parts, board steps and checks", LEARNER, "profile_directory", "transcript"),
    ProfileStore("assessment runs", "diagnostics and mastery checks with their answers", LEARNER, "profile_directory",
                 "assessment runs"),
    ProfileStore("saved questions and problem reports", "saved questions and reports with their notes", LEARNER,
                 "profile_directory", "saved questions and problem reports"),
    ProfileStore("past papers", "imported past papers, their questions, mark schemes and figure pages (ASM-27)",
                 LEARNER, "profile_directory", "past papers"),
    ProfileStore("exam dates", "the student's exam dates and dismissed reminders (LRN-44)", LEARNER,
                 "profile_directory", "exam dates"),
    ProfileStore("archive authorizations", "the student's single-use archive grants", LEARNER, "profile_directory",
                 "archive authorizations"),
    ProfileStore("exam answers", "mock answers and working", LEARNER, "profile_directory", "exam answers"),
    ProfileStore("learner idempotency responses", "stored replies of the profile's commands", LEARNER,
                 "profile_directory", "idempotency responses (learner)"),
    ProfileStore("captures", "confirmed photo, typed and dictated captures (capture drafts in SQLite)", LEARNER,
                 "profile_directory", "captures"),
    ProfileStore("UI crash journals", "bounded transcript tail and in-flight envelopes",
                 "profiles/{profile}/ui-journals",
                 "profile_directory", lifetime="until profile close, switch, idle lock, or deletion; cache only"),
    ProfileStore("editor drafts", "unsent composer drafts", LEARNER, "profile_directory", "editor_drafts"),
    ProfileStore("attachments", "photo and handwriting files and their rows", "profiles/{profile}/attachments",
                 "profile_directory", "attachments"),
    ProfileStore("checkpoints", "session checkpoints for crash recovery", LEARNER, "profile_directory", "checkpoints"),
    ProfileStore("spill files", "large tool results of the profile's sessions", "profiles/{profile}/spill",
                 "profile_directory", "spill files"),
    ProfileStore("trace store", "turn traces of the profile's sessions (redacted at the writer)", "traces/traces.db",
                 "traces", "trace store"),
    ProfileStore("Reflector notes", "notes the Reflector wrote", LEARNER, "profile_directory", "Reflector notes"),
    ProfileStore("digests", "weekly parent digests", LEARNER, "profile_directory", "digests"),
    ProfileStore("alerts", "alerts and their dismissals", LEARNER, "profile_directory", "alerts"),
    ProfileStore("audit log", "the profile's audit rows and pending re-mark notices (ASM-23)", LEARNER,
                 "profile_directory", "audit log"),
    ProfileStore("exports", "records of the exports made", LEARNER, "profile_directory", "exports"),
    ProfileStore("profile key", "the learner database's key", "keystore", "profile_directory",
                 lifetime="the life of the profile"),
    # --- the device database: rows that name the profile ------------------------------------------------------------
    ProfileStore("installation rows", "the profile's app settings keys (spend, covered profiles, age bands) and "
                 "migration rows", DEVICE, "profile_directory", "life of the installation"),
    ProfileStore("profiles index", "the profile's row in the picker", DEVICE, "profile_directory", "profiles index"),
    ProfileStore("egress ledger", "what left the device for the profile", DEVICE, "profile_directory",
                 "egress ledger"),
    ProfileStore("first-use egress decisions", "the profile's answers to first-use notices", DEVICE, "online",
                 lifetime="the life of the profile"),
    ProfileStore("device idempotency responses", "stored replies that name the profile", DEVICE, "profile_directory",
                 "idempotency responses (device)"),
    ProfileStore("outbox", "undelivered items for the profile (deletion and withdrawal notices stay, DAT-09)", DEVICE,
                 "profile_directory", "outbox"),
    ProfileStore("device audit", "audit rows; only the deletion row keeps the profile id", DEVICE,
                 "profile_directory", "device audit"),
    ProfileStore("device consents", "telemetry and crash-report consents (device scope)", DEVICE, NONE,
                 "device consents", lifetime="device scope: holds no profile's data"),
    # --- short-lived working copies ---------------------------------------------------------------------------------
    ProfileStore("perception scratch", "a photo's working files while it is read", "system temporary folder", NONE,
                 lifetime="one photo reading; the folder is removed when the reading ends (tools/perception.py)"),
    ProfileStore("worker scratch", "a tool call's working files", "scratch", NONE,
                 lifetime="one worker process; removed when the worker is recycled or the app stops"),
)  # fmt: skip

BY_NAME = {store.name: store for store in PROFILE_STORES}


def retention_order() -> list[str]:
    """The TABLE DAT-08a row classes in the order the retention job applies them: the registry's order."""
    return [store.rule for store in PROFILE_STORES if store.rule is not None]


Eraser = Callable[[str], Any]


class UnknownStore(KeyError):
    """An eraser was registered under a name the registry does not have."""


class Erasers:
    """What profile deletion runs (DAT-09): each registered eraser once, in `ERASERS` order."""

    def __init__(self) -> None:
        self.hooks: dict[str, list[Eraser]] = {}

    def register(self, name: str, hook: Eraser) -> None:
        if name not in ERASERS:
            raise UnknownStore(f"{name!r} is not an eraser of store.profile_stores; list the store there first")
        self.hooks.setdefault(name, []).append(hook)

    def missing(self) -> list[str]:
        """Erasers the registry needs that nothing registered (an assembled app has none)."""
        wanted = {store.erased_by for store in PROFILE_STORES if store.erased_by}
        return [name for name in ERASERS if name in wanted and not self.hooks.get(name)]

    async def erase(self, profile_id: str) -> dict[str, Any]:
        """Run every eraser for `profile_id`, in order; returns each eraser's last result by name."""
        results: dict[str, Any] = {}
        for name in ERASERS:
            for hook in self.hooks.get(name, []):
                result = hook(profile_id)
                if inspect.isawaitable(result):
                    result = await result
                results[name] = result
        return results
