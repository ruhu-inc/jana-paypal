"""The learner's map (SPEC §21, W10): the layers on the concept map, the overview of every installed subject, a
skill's links, "since last time" and the map as it stood on a date. Device-only and code-only: nothing here calls a
model or the network, and nothing here writes a learning event (JM-D39, JM-D45).

- **States at now.** A skill with evidence takes its TABLE LRN-09a state as `kc_views` evaluates it at now (FSRS
  retrievability, the stuck deferral and the retry lock move with the clock), so `state` and `refresh_due` agree. A
  row with no attempts keeps its stored state (the projection writes `not_started` for one, so the two only differ
  for a row nobody folded).
- **Freshness** (JM-D36, review B14): the newest learning touch of a skill, the later of `kc_mastery.last_seen_at`
  (attempts, hints and checks, through closed opportunities) and the newest `study_activity` event whose `kc_tags`
  hold it (a scan of the events within 26 weeks, JSON tags, no index), bucketed against the profile's local day:
  `today`, `week` (the 6 days before), `month` (within 30 days), `older`, `never`. A skill with no mastery row is
  included. A bucket by code, never a probability.
- **Study** (JM-D38, review 13): the materials tagged to a skill (the aids its Study activity names through its
  `ref`, and aids and sources whose title `study_topics.kc_tags` tags), the lecture moments (the timed transcript
  chunks of a recording, a video file or a video link, tagged the same way at read time) and the due flashcards
  reviewed at least once (a card is tied to a skill only through its `card_review` activity).
- **Notes**: the tutor's notes (active or awaiting review) whose cited events name the skill.
- **Topics your tutor is tracking** (review 12): the companion summary's topics, at most six.
- **Since last time** (JM-D45, review B12): the bands at the last open are kept in the device database's
  `app_settings` under `map_opened_at:<profile_id>`, never as a `Settings` key (that would append a synced learning
  event on every open and pollute the log the scrubber replays). The first live read in a core's life takes the
  stored bands as its baseline and every live read writes the current ones, so an overview read and the subject map
  opened from it compare against the same visit. `up`: the band rose; `new`: no evidence then, evidence now; "down"
  is dropped (review 11). The first open marks nothing.
- **As of a date** (JM-D37, review B15): `snapshot_as_of` replays the log into memory up to the end of that local
  day; the map is read from the replay with the clock at that moment. Dates are weekly from the first learning
  event, at most 26 weeks back; a later or earlier date is refused.
"""

from __future__ import annotations

import json
import sqlite3
from collections.abc import Callable, Iterable, Mapping
from dataclasses import dataclass, field
from datetime import date, timedelta
from typing import Any

from agent_core.learner import model
from agent_core.learner.mastery import DAY_MS, MasteryParams
from agent_core.learner.projections import Snapshots, local_date, note_lifecycle, snapshot_as_of
from agent_core.store.events import ms_from_iso

STATES = ("not_started", "learning", "ready_to_check", "mastered", "needs_review", "moved_on")
FRESHNESS = ("today", "week", "month", "older", "never")
WEEKS_BACK = 26
STUDY_SCAN_MS = WEEKS_BACK * 7 * DAY_MS
MATERIALS_MAX = 12
MOMENTS_MAX = 12
NOTES_MAX = 6
OPENED_KEY = "map_opened_at:{profile}"
# Sources whose transcript chunks carry time locators: recordings, and W9's video files and video links.
TIMED_KINDS = ("recording", "video", "video_link")
BAND_ORDER = {"not_started": 0, "learning": 1, "secure": 2}


class AsOfRefused(ValueError):
    """A scrubber date that is not a date, is in the future, or is more than 26 weeks back."""


# --- states, freshness ------------------------------------------------------------------------------------------------


def states_now(conn: sqlite3.Connection, params: MasteryParams | None, now: int) -> dict[str, str]:
    """Each KC with a mastery row and its TABLE LRN-09a state at `now`; without parameters the stored state."""
    try:
        rows = conn.execute("SELECT kc_id, status, attempts FROM kc_mastery").fetchall()
    except sqlite3.OperationalError:
        return {}
    stored = {str(k): str(s) for k, s, _ in rows if s in STATES}
    if params is None:
        return stored
    views = model.kc_views(conn, params, now)
    return {
        kc: views[kc].state if attempts and kc in views else state
        for kc, state, attempts in ((str(k), stored.get(str(k)), a) for k, _, a in rows)
        if state
    }


def _kc_ids(raw: Any) -> list[str]:
    out = []
    for item in raw if isinstance(raw, list) else []:
        kc = item.get("kc_id") if isinstance(item, dict) else item
        if isinstance(kc, str) and kc.startswith("kc."):
            out.append(kc)
    return out


def _study_rows(conn: sqlite3.Connection, now: int) -> list[tuple[dict[str, Any], int]]:
    """The Study activity within 26 weeks of `now`, oldest first: payload and when."""
    try:
        rows = conn.execute(
            "SELECT payload, occurred_at FROM events WHERE type = 'study_activity' AND erased_at IS NULL"
            " AND occurred_at > ? AND occurred_at <= ? ORDER BY occurred_at, seq",
            (now - STUDY_SCAN_MS, now),
        ).fetchall()
    except sqlite3.OperationalError:
        return []
    out = []
    for raw, at in rows:
        try:
            payload = json.loads(raw)
        except json.JSONDecodeError:
            continue
        if isinstance(payload, dict):
            out.append((payload, int(at)))
    return out


def touches(conn: sqlite3.Connection, now: int) -> dict[str, int]:
    """Each KC's newest learning touch at or before `now`: `last_seen_at` or a tagged Study activity (review B14)."""
    out: dict[str, int] = {}
    try:
        for kc, seen in conn.execute("SELECT kc_id, last_seen_at FROM kc_mastery WHERE last_seen_at IS NOT NULL"):
            if int(seen) <= now:
                out[str(kc)] = int(seen)
    except sqlite3.OperationalError:
        pass
    for payload, at in _study_rows(conn, now):
        for kc in _kc_ids(payload.get("kc_tags")):
            out[kc] = max(out.get(kc, 0), at)
    return out


def bucket(params: MasteryParams | None, now: int, touched: int | None) -> str:
    """A touch as a freshness word against the profile's local day."""
    if touched is None:
        return "never"
    day = (lambda t: params.local_day(t)) if params is not None else (lambda t: t // DAY_MS)
    back = day(now) - day(touched)
    return "today" if back <= 0 else "week" if back < 7 else "month" if back < 30 else "older"


def freshest(words: Iterable[str]) -> str:
    return min(words, key=FRESHNESS.index, default="never")


# --- Study, notes, tracked --------------------------------------------------------------------------------------------


_TAGS: dict[tuple[str, str], tuple[str, ...]] = {}
TAGS_HELD = 4096


def tags(content: Any, text: str) -> tuple[str, ...]:
    """`study_topics.kc_tags` of a text (a title, a transcript chunk), cached by the pack set's fingerprint."""
    from agent_core.engine.study_topics import kc_tags

    if content is None or not text.strip():
        return ()
    fingerprint = str(getattr(content, "fingerprint", "") or "")
    key = (fingerprint, text)
    found = _TAGS.get(key) if fingerprint else None
    if found is None:
        found = tuple(t["kc_id"] for t in kc_tags(content, text))
        if fingerprint:
            if len(_TAGS) >= TAGS_HELD:
                _TAGS.clear()
            _TAGS[key] = found
    return found


def _rows(conn: sqlite3.Connection, sql: str, args: tuple[Any, ...] = ()) -> list[tuple[Any, ...]]:
    try:
        return conn.execute(sql, args).fetchall()
    except sqlite3.OperationalError:
        return []


def _body(raw: Any) -> dict[str, Any]:
    try:
        found = json.loads(raw)
    except (TypeError, json.JSONDecodeError):
        return {}
    return found if isinstance(found, dict) else {}


@dataclass
class StudyIndex:
    """The Study layer per KC at a moment: materials (newest first), moments and the due cards reviewed once."""

    materials: dict[str, list[dict[str, Any]]] = field(default_factory=dict)
    moments: dict[str, list[dict[str, Any]]] = field(default_factory=dict)
    cards_due: dict[str, set[str]] = field(default_factory=dict)

    @classmethod
    def read(cls, conn: sqlite3.Connection, study: sqlite3.Connection, content: Any, now: int) -> StudyIndex:
        """`conn` holds the events (the live database or a replay), `study` the Study tables (the live database)."""
        out = cls()
        aids = {
            str(a): (_body(b), int(c))
            for a, b, c in _rows(study, "SELECT aid_id, body, created_at FROM study_aids")
            if int(c) <= now
        }
        quiz_aid = {str(q): str(a) for q, a in _rows(study, "SELECT quiz_id, aid_id FROM study_quizzes")}
        attempt_quiz = {str(a): str(q) for a, q in _rows(study, "SELECT attempt_id, quiz_id FROM study_attempts")}
        card_aid = {
            str(c): str(a)
            for c, a in _rows(
                study, "SELECT f.card_id, d.aid_id FROM study_flashcards f JOIN study_decks d ON d.deck_id = f.deck_id"
            )
        }
        linked: dict[str, dict[str, int]] = {}  # kc -> aid -> newest touch

        def link(kc: str, aid_id: str | None, at: int) -> None:
            if aid_id is not None and aid_id in aids:
                held = linked.setdefault(kc, {})
                held[aid_id] = max(held.get(aid_id, 0), at)

        card_kcs: dict[str, set[str]] = {}
        for payload, at in _study_rows(conn, now):
            kind, _, rest = str(payload.get("ref") or "").partition(":")
            kcs = _kc_ids(payload.get("kc_tags"))
            aid_id = (
                rest
                if kind == "aid"
                else quiz_aid.get(attempt_quiz.get(rest.rsplit(":", 1)[0], ""))
                if kind == "att"
                else card_aid.get(rest)
                if kind == "card"
                else None
            )
            for kc in kcs:
                link(kc, aid_id, at)
                if kind == "card":
                    card_kcs.setdefault(rest, set()).add(kc)
        for aid_id, (body, created) in aids.items():
            for kc in tags(content, str(body.get("title") or "")):
                link(kc, aid_id, created)
        for kc, held in linked.items():
            out.materials[kc] = [
                {
                    "aid_id": a,
                    "kind": str(aids[a][0].get("kind") or "notes"),
                    "title": str(aids[a][0].get("title") or "")[:200] or "Study",
                }
                for a, _ in sorted(held.items(), key=lambda kv: (-kv[1], kv[0]))
            ]
        gone = {str(s) for (s,) in _rows(study, "SELECT source_id FROM study_tombstones")}
        sources = {
            str(s): (_body(b), int(r), int(c))
            for s, r, b, c in _rows(study, "SELECT source_id, current_revision, body, created_at FROM study_sources")
            if str(s) not in gone and int(c) <= now
        }
        for source_id, (body, _revision, _created) in sorted(sources.items(), key=lambda kv: (-kv[1][2], kv[0])):
            for kc in tags(content, str(body.get("title") or "")):
                out.materials.setdefault(kc, []).append(
                    {
                        "source_id": source_id,
                        "kind": str(body.get("kind") or "text"),
                        "title": str(body.get("title") or "")[:200] or "Study",
                    }
                )
        for source_id, (body, revision, _created) in sorted(sources.items()):
            if body.get("kind") not in TIMED_KINDS:
                continue
            title = str(body.get("title") or "")[:200] or "Recording"
            for (chunk,) in _rows(
                study,
                "SELECT body FROM study_chunks WHERE source_id = ? AND revision = ? ORDER BY ordinal",
                (source_id, revision),
            ):
                piece = _body(chunk)
                locator: dict[str, Any] = piece.get("locator") or {}
                if locator.get("kind") != "time":
                    continue
                for kc in tags(content, str(piece.get("text") or "")):
                    moments = out.moments.setdefault(kc, [])
                    at = int(locator["start_ms"])
                    if not any(m["source_id"] == source_id and m["at_ms"] == at for m in moments):
                        moments.append({"source_id": source_id, "at_ms": at, "title": title})
        latest: dict[str, tuple[int, int]] = {}  # card -> (reviewed_at, due_ms) as of now
        for card_id, raw, reviewed in _rows(study, "SELECT card_id, body, reviewed_at FROM study_reviews"):
            if int(reviewed) > now:
                continue
            due = _body(raw).get("due_at")
            if not isinstance(due, str):
                continue
            last = latest.get(str(card_id))
            if last is None or int(reviewed) >= last[0]:
                latest[str(card_id)] = (int(reviewed), ms_from_iso(due))
        for card_id, (_reviewed, due) in latest.items():
            if due <= now:
                for kc in card_kcs.get(card_id, ()):
                    out.cards_due.setdefault(kc, set()).add(card_id)
        return out

    def counts(self, kc: str) -> dict[str, int]:
        return {
            "materials": len(self.materials.get(kc, [])),
            "moments": len(self.moments.get(kc, [])),
            "cards_due": len(self.cards_due.get(kc, ())),
        }


def note_kcs(conn: sqlite3.Connection) -> dict[str, list[tuple[str, str, int]]]:
    """Each KC's notes (id, text, updated) through the KCs of the events the note cites, newest first."""
    notes = _rows(
        conn,
        "SELECT note_id, text, source_event_ids, updated_at FROM notes WHERE status IN ('active', 'pending_review')",
    )
    cited = {i for _, _, raw, _ in notes for i in json.loads(raw or "[]")}
    event_kcs: dict[str, list[str]] = {}
    ids = sorted(cited)
    for start in range(0, len(ids), 500):
        chunk = tuple(ids[start : start + 500])
        marks = ", ".join("?" for _ in chunk)
        for event_id, raw in _rows(
            conn,
            f"SELECT event_id, kc_ids FROM events WHERE erased_at IS NULL"  # noqa: S608
            f" AND event_id IN ({marks})",
            chunk,
        ):
            try:
                event_kcs[str(event_id)] = _kc_ids(json.loads(raw)) if raw else []
            except json.JSONDecodeError:
                continue
    out: dict[str, list[tuple[str, str, int]]] = {}
    for note_id, text, raw, updated in sorted(notes, key=lambda n: (-int(n[3]), str(n[0]))):
        kcs = dict.fromkeys(k for e in json.loads(raw or "[]") for k in event_kcs.get(str(e), []))
        for kc in kcs:
            out.setdefault(kc, []).append((str(note_id), str(text), int(updated)))
    return out


def tracked(conn: sqlite3.Connection, now: int) -> set[str]:
    """The companion summary's topics (at most six) that it held at `now` (a topic's `last_at` at or before it)."""
    rows = _rows(conn, "SELECT body_json FROM companion_summary ORDER BY thread_id LIMIT 1")
    if not rows:
        return set()
    row = rows[0]
    out = set()
    for topic in (_body(row[0]).get("topics") or [])[:6]:
        last = topic.get("last_at") if isinstance(topic, dict) else None
        if isinstance(topic, dict) and isinstance(topic.get("kc_id"), str) and (not last or ms_from_iso(last) <= now):
            out.add(topic["kc_id"])
    return out


def watched(conn: sqlite3.Connection) -> dict[str, int]:
    """Watched misconceptions per KC: any status but resolved (a disputed one is the learner's word, still shown)."""
    out: dict[str, int] = {}
    for (kc,) in _rows(conn, "SELECT kc_id FROM learner_misconceptions WHERE status NOT IN ('resolved', 'disputed')"):
        out[str(kc)] = out.get(str(kc), 0) + 1
    return out


# --- since last time (JM-D45) -------------------------------------------------------------------------------------


@dataclass
class MapState:
    """What one profile's map keeps for the life of the core: the bands of the visit's baseline and the snapshots."""

    baseline: dict[str, str] | None = None
    loaded: bool = False
    snapshots: Snapshots = field(default_factory=Snapshots)


def opened(device: sqlite3.Connection | None, profile_id: str) -> dict[str, str] | None:
    """The bands kept at the last open, or None when the map was never opened on this device."""
    if device is None:
        return None
    row = device.execute("SELECT value FROM app_settings WHERE key = ?", (OPENED_KEY.format(profile=profile_id),))
    found = row.fetchone()
    value = _body(found[0]) if found else {}
    bands = value.get("bands")
    return {str(k): str(v) for k, v in bands.items()} if isinstance(bands, dict) else None


def write_opened(device: sqlite3.Connection | None, profile_id: str, bands: Mapping[str, str], now: int) -> None:
    """Record this open in the device database (never the learner database, never a learning event)."""
    if device is None:
        return
    value = json.dumps({"at": now, "bands": dict(sorted(bands.items()))}, separators=(",", ":"))
    device.execute(
        "INSERT INTO app_settings (key, value, updated_at) VALUES (?, ?, ?) ON CONFLICT (key) DO UPDATE"
        " SET value = excluded.value, updated_at = excluded.updated_at",
        (OPENED_KEY.format(profile=profile_id), value, now),
    )


def changed(before: Mapping[str, str] | None, kc: str, band: str) -> str | None:
    if before is None or band == "not_started":
        return None
    was = before.get(kc, "not_started")
    if was == "not_started":
        return "new"
    return "up" if BAND_ORDER.get(band, 0) > BAND_ORDER.get(was, 0) else None


# --- the dated map (JM-D37) -------------------------------------------------------------------------------------------


def day_end(params: MasteryParams | None, day: str) -> int:
    """The last millisecond of a local date."""
    offset = params.learner_day_offset_minutes * 60_000 if params is not None else 0
    return ((date.fromisoformat(day) - date(1970, 1, 1)).days + 1) * DAY_MS - offset - 1


def check_as_of(params: MasteryParams | None, now: int, text: str) -> str:
    """A scrubber date as `YYYY-MM-DD`, or AsOfRefused (not a date, in the future, more than 26 weeks back)."""
    try:
        asked = date.fromisoformat(text)
    except (TypeError, ValueError):
        raise AsOfRefused("as_of is a date, YYYY-MM-DD") from None
    if len(text) != 10:
        raise AsOfRefused("as_of is a date, YYYY-MM-DD")
    today = date.fromisoformat(local_date(params.local_day(now) if params is not None else now // DAY_MS))
    if asked > today:
        raise AsOfRefused("as_of is not in the future")
    if asked < today - timedelta(weeks=WEEKS_BACK):
        raise AsOfRefused("as_of is at most 26 weeks back")
    return asked.isoformat()


def snapshot(
    state: MapState,
    profile_id: str,
    day: str,
    conn: sqlite3.Connection,
    params: MasteryParams,
    content: Any,
    projections: Any,
) -> tuple[sqlite3.Connection, int]:
    """The replayed learner model at the end of `day`, cached for the session, and that moment."""
    ts = day_end(params, day)
    mem = state.snapshots.get(profile_id, day, conn, lambda: snapshot_as_of(conn, params, content, ts, projections))
    return mem, ts


# --- decorating the concept map ---------------------------------------------------------------------------------------


@dataclass
class Layers:
    """Everything the layers need for one read, at one moment."""

    params: MasteryParams | None
    now: int
    touched: dict[str, int]
    study: StudyIndex
    notes: dict[str, list[tuple[str, str, int]]]
    tracked: set[str]
    before: dict[str, str] | None

    @classmethod
    def read(
        cls,
        conn: sqlite3.Connection,
        study: sqlite3.Connection,
        content: Any,
        params: MasteryParams | None,
        now: int,
        before: dict[str, str] | None,
    ) -> Layers:
        return cls(
            params,
            now,
            touches(conn, now),
            StudyIndex.read(conn, study, content, now),
            note_kcs(conn),
            tracked(conn, now),
            before,
        )

    def node(self, node: dict[str, Any]) -> dict[str, Any]:
        kc = str(node["kc_id"])
        node["freshness"] = bucket(self.params, self.now, self.touched.get(kc))
        node["refresh_due"] = node["state"] == "needs_review"
        node["changed_since"] = changed(self.before, kc, str(node["band"]))
        node["study"] = self.study.counts(kc)
        node["notes"] = len(self.notes.get(kc, []))
        node["tracked"] = kc in self.tracked
        return node


def overview_topic(
    tile: Mapping[str, Any],
    nodes: Mapping[str, Mapping[str, Any]],
    mistakes: Mapping[str, int],
    rule: Callable[[list[str]], str],
) -> dict[str, Any]:
    """A subject map tile as an overview tile: its skills' bands, TABLE UXS-22a's rule and the layers' summaries."""
    kcs = list(tile["kcs"])
    bands = {"not_started": 0, "learning": 0, "secure": 0}
    for kc in kcs:
        bands[kc["band"]] += 1
    drawn = [nodes[k["kc_id"]] for k in kcs if k["kc_id"] in nodes]
    return {
        "topic_id": str(tile["node_id"]),
        "title": str(tile["title"])[:120],
        "kcs": len(kcs),
        "bands": bands,
        "rule": rule([str(k["state"]) for k in kcs]),
        "freshness": freshest(n["freshness"] for n in drawn),
        "refresh_due": any(n["refresh_due"] for n in drawn),
        "mistakes": sum(mistakes.get(k["kc_id"], 0) for k in kcs),
        "changed": sum(1 for n in drawn if n["changed_since"] is not None),
    }


def topic_rule(states: list[str]) -> str:
    """TABLE UXS-22a: a topic's label from its KCs' states, the first matching row wins (`learn.ts` `topicLabel`)."""
    if "needs_review" in states:
        return "needs_review"
    if "moved_on" in states:
        return "moved_on"
    if all(s == "not_started" for s in states):
        return "not_started"
    if all(s == "mastered" for s in states):
        return "mastered"
    if all(s in ("ready_to_check", "mastered") for s in states):
        return "ready_to_check"
    return "learning"


def links(
    conn: sqlite3.Connection,
    study: sqlite3.Connection,
    content: Any,
    kc_id: str,
    now: int,
    misconceptions: list[dict[str, Any]],
) -> dict[str, Any]:
    """The `KcLinks` reply: what is linked to one skill on this device, bounded."""
    index = StudyIndex.read(conn, study, content, now)
    notes = note_kcs(conn).get(kc_id, [])[:NOTES_MAX]
    lifecycle = note_lifecycle(conn, [n for n, _, _ in notes], now)
    return {
        "kc_id": kc_id,
        "materials": index.materials.get(kc_id, [])[:MATERIALS_MAX],
        "moments": sorted(index.moments.get(kc_id, []), key=lambda m: (m["source_id"], m["at_ms"]))[:MOMENTS_MAX],
        "notes": [
            {
                "note_id": n if n.startswith("mem_") else f"mem_{n}",
                "text": text[:400],
                "confidence": lifecycle[n].confidence if n in lifecycle else "tentative",
            }
            for n, text, _ in notes
        ],
        "tracked": kc_id in tracked(conn, now),
        "misconceptions": [m for m in misconceptions if m["kc_id"] == kc_id][:20],
    }


def first_day(conn: sqlite3.Connection, params: MasteryParams | None) -> str | None:
    """The local date of the profile's first learning event (the scrubber starts there), or None."""
    first = (_rows(conn, "SELECT min(occurred_at) FROM events WHERE erased_at IS NULL") or [(None,)])[0][0]
    if first is None:
        return None
    return local_date(params.local_day(int(first)) if params is not None else int(first) // DAY_MS)
