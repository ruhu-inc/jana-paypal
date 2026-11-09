"""The "What your tutor knows" view (LRN-36, MEM-14, UXS-28): the learner model and the notes in plain words, for
the student (and the device adult). States are shown as bands and labels, never probabilities. Notes that fail the
render check are listed and marked so they can be deleted (MEM-27); pending review items carry the event ids that
pin their resolution (MEM-09).

The view carries codes, not sentences: the client turns states, bands and statuses into the string catalogue's
words. Note text is the stored text, unescaped: the page renders it as text, never as markup.
"""

from __future__ import annotations

import sqlite3
from dataclasses import dataclass
from typing import Literal, cast

from agent_core.config import Settings
from agent_core.memory.capture import CaptureFilter, default_filter
from agent_core.memory.policy import render_check
from agent_core.memory.queries import (
    ContentLabels,
    KcLine,
    MisconceptionLine,
    NoteRecord,
    Preference,
    ProfileSummary,
    ReviewItem,
    active_misconceptions,
    all_kcs,
    memory_saving,
    preferences,
    profile_summary,
    review_items,
    stored_notes,
)
from agent_core.memory.render import BANDS

Band = Literal["not_started", "learning", "secure"]
__all__ = ["About", "Band", "KnowsView", "ReviewItem", "ViewKc", "ViewNote", "about", "knows_view"]


@dataclass(frozen=True)
class ViewNote:
    note: NoteRecord
    flagged: bool  # fails the render check: visible here, never in a prompt


@dataclass(frozen=True)
class ViewKc:
    """A KC as the page shows it: its state (the label's key) and the bar's three-step band."""

    kc: KcLine
    band: Band


@dataclass(frozen=True)
class KnowsView:
    profile: ProfileSummary
    kcs: tuple[KcLine, ...]
    misconceptions: tuple[MisconceptionLine, ...]
    preferences: tuple[Preference, ...]
    notes: tuple[ViewNote, ...]
    reviews: tuple[ReviewItem, ...]
    memory_saving: bool
    bands: tuple[ViewKc, ...] = ()


@dataclass(frozen=True)
class About:
    """The page's "About you": level, subjects and the profile's exam date (the date itself, which the page shows;
    prompts see only the distance)."""

    level: str
    subjects: tuple[str, ...]
    exam_date: str | None


def about(conn: sqlite3.Connection) -> About | None:
    """The profile row as the page shows it, or None when the learner database has no profile row (S1)."""
    row = conn.execute("SELECT level, exam_date FROM profile").fetchone()
    if row is None:
        return None
    return About(str(row[0]), profile_summary(conn).subjects, row[1] if isinstance(row[1], str) else None)


def knows_view(
    conn: sqlite3.Connection,
    content: ContentLabels,
    settings: Settings,
    *,
    today: str | None = None,
    capture: CaptureFilter | None = None,
) -> KnowsView:
    """Everything the page shows, read through the fixed queries: the profile (exam distance from `today`), every KC
    with evidence and its band, active and resolving misconceptions, preferences, every active note (each marked if
    it fails the render check, with the do-not-capture filter given or else the source tree's), the open review items
    and whether memory saving is on."""
    capture = capture or default_filter(settings)
    kcs = all_kcs(conn, content, settings)
    notes = stored_notes(conn, ("active",))
    (watched,) = conn.execute("SELECT count(*) FROM learner_misconceptions").fetchone()
    return KnowsView(
        profile=profile_summary(conn, today=today, settings=settings),
        kcs=tuple(kcs),
        misconceptions=tuple(active_misconceptions(conn, content, None, int(watched))),
        preferences=tuple(preferences(conn)),
        notes=tuple(ViewNote(n, bool(render_check(n.text, settings, capture))) for n in notes),
        reviews=tuple(review_items(conn)),
        memory_saving=memory_saving(conn, settings),
        bands=tuple(ViewKc(k, cast(Band, BANDS[k.state])) for k in kcs),
    )
