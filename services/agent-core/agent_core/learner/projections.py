"""Projections: learner tables folded from the event log (DAT-05, LRN-02).

Each projection owns tables and folds events in the merged order (hlc, device_id, origin_seq). The projector records
in `projection_state` the projection's code version, the hash of the mastery parameters and pinned content, the last
local `seq` folded and the full merge cursor. An event that sorts before the cursor (a late event from another
device), or one that changes how earlier events read (a re-mark or its evidence, ASM-23, CUR-36), makes the
projection replay instead of folding, so incremental and rebuilt tables are always equal. A rebuild runs in one
transaction: readers on other connections keep the previous tables until it commits.

The projections are `attempts`, `kc_mastery` (with `subject_ability` and the FSRS `review_cards`, LRN-13), `notes`,
`learner_misconceptions`, `preferences`, `study_plans` (LRN-28, LRN-31) and `engagement` (`engagement_state` and
`engagement_daily`: streaks with freeze, frustration and confidence bias; LRN-16, LRN-33). Where a fold reads a table
another projection owns (the engagement projection reads the day's checked attempts and completed plan blocks), it
reads only rows stamped at or before the event, so folding it alone or with the others gives the same rows.
"""

from __future__ import annotations

import hashlib
import json
import sqlite3
import statistics
import time
from collections.abc import Callable, Iterable, Mapping, Sequence
from dataclasses import dataclass, field, replace
from datetime import date, timedelta
from typing import Any, Protocol

from agent_core.learner.fsrs import Card, Reviewed, review_rating, scheduler_of
from agent_core.learner.kc_migrations import NO_MIGRATIONS, KcMigrations, migrations_of
from agent_core.learner.mastery import (
    DAY_MS,
    Ability,
    KcEvidence,
    MasteryParams,
    Opportunity,
    StateInputs,
    kc_probability,
    kc_state,
    normalised,
    observations,
    observe,
    subject_of,
)
from agent_core.store.events import COLUMNS, MERGE_ORDER, StoredEvent, iso_ms, ms_from_iso
from agent_core.store.sqlite import transaction

# The readers' rule for re-marks (ASM-23, CUR-36) lives in the store layer, below every reader of verdicts.
from agent_core.store.supersession import Supersession as Supersession
from agent_core.store.supersession import follows_a_verdict, read_log


class ContentIndex(Protocol):
    """What the projections need from the pinned content packs."""

    fingerprint: str

    def item_difficulty(self, item_id: str | None) -> float: ...
    def reference_difficulty(self, kc_id: str) -> float: ...
    def prerequisites(self) -> Mapping[str, Sequence[str]]: ...
    def dependents(self) -> Mapping[str, Sequence[str]]: ...
    def item_median_ms(self, item_id: str | None) -> int | None: ...
    def item_options(self, item_id: str | None) -> int | None: ...


@dataclass
class NoContent:
    """No pack installed: every difficulty is 0 and there is no prerequisite graph (LRN-10 cold start)."""

    fingerprint: str = ""
    prereqs: dict[str, list[str]] = field(default_factory=dict)

    def item_difficulty(self, item_id: str | None) -> float:
        return 0.0

    def reference_difficulty(self, kc_id: str) -> float:
        return 0.0

    def prerequisites(self) -> Mapping[str, Sequence[str]]:
        return self.prereqs

    def dependents(self) -> Mapping[str, Sequence[str]]:
        out: dict[str, list[str]] = {}
        for kc, needs in self.prereqs.items():
            for need in needs:
                out.setdefault(need, []).append(kc)
        return out

    def item_median_ms(self, item_id: str | None) -> int | None:
        return None

    def item_options(self, item_id: str | None) -> int | None:
        return None


@dataclass(frozen=True)
class Context:
    conn: sqlite3.Connection
    params: MasteryParams
    content: ContentIndex
    # During a replay, each session's events so far in merge order (erased ones left out), so an opportunity is built
    # from memory instead of re-reading its session for every verdict.
    sessions: dict[str, list[StoredEvent]] | None = None
    # KC migrations of the pinned packs (CUR-10): every event is read through them, folded and replayed alike.
    migrations: KcMigrations = NO_MIGRATIONS
    # The log's re-marks during a replay (ASM-23, CUR-36): a note resting only on replaced markings is withdrawn.
    remarks: Supersession = field(default_factory=Supersession)
    # Replay-only, cleared before each migrated event is folded: projections share one reconstruction of the
    # current opportunity, never a cached result from an earlier verdict, session, correction or rebuild.
    opportunities: dict[str, Closed | None] | None = None


class Projection(Protocol):
    name: str
    code_version: str
    tables: tuple[str, ...]

    def fold(self, ctx: Context, event: StoredEvent) -> None: ...


def local_date(day: int) -> str:
    """A local day number as the contracts' date."""
    return (date(1970, 1, 1) + timedelta(days=day)).isoformat()


def day_number(text: str) -> int:
    return (date.fromisoformat(text) - date(1970, 1, 1)).days


def _dumps(value: Any) -> str:
    return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False)


def _session_events(conn: sqlite3.Connection, session_id: str, upto: tuple[str, str, int]) -> list[StoredEvent]:
    """A session's events up to and including the cursor, in merge order, with re-marked verdicts read as their
    replacements (ASM-23): what an opportunity is built from."""
    rows = conn.execute(
        f"SELECT {COLUMNS} FROM events WHERE session_id = ? AND (hlc, device_id, origin_seq) <= (?, ?, ?)"  # noqa: S608
        f" ORDER BY {MERGE_ORDER}",
        (session_id, *upto),
    )
    return read_log(map(StoredEvent.from_row, rows))


@dataclass(frozen=True)
class Closed:
    """An opportunity closed by a verdict, with the attempts and hints it is built from. `not_learned` marks a
    diagnostic item answered "I haven't learned this yet" (ASM-10), which LRN-06 scores before its verdict."""

    attempt: dict[str, Any]
    verdict: dict[str, Any]
    outcomes: tuple[str, ...]
    max_rung: int
    hints: int
    not_learned: bool = False


def session_events(ctx: Context, event: StoredEvent) -> list[StoredEvent]:
    """The event's session so far, in merge order, read through the KC migrations."""
    if event.session_id is None:
        return []
    if ctx.sessions is not None:
        return ctx.sessions.get(event.session_id, [])
    stored = _session_events(ctx.conn, event.session_id, event.order)
    return [read for raw in stored for read in ctx.migrations.event(raw)]


def opportunity(ctx: Context, event: StoredEvent) -> Closed | None:
    if ctx.opportunities is None:
        return _read_opportunity(ctx, event)
    if event.event_id not in ctx.opportunities:
        ctx.opportunities[event.event_id] = _read_opportunity(ctx, event)
    return ctx.opportunities[event.event_id]


def _read_opportunity(ctx: Context, event: StoredEvent) -> Closed | None:
    """The opportunity a verdict belongs to: attempts at the same problem step in the session, their latest
    (non-superseded) verdicts in order, and the hints shown for that step."""
    verdict = event.payload
    if event.session_id is None:
        return None
    events = session_events(ctx, event)
    attempts = {e.payload["attempt_id"]: e.payload for e in events if e.type == "attempt"}
    attempt = attempts.get(verdict["attempt_id"])
    if attempt is None:
        return None
    step = (attempt["problem_ref"], attempt.get("step_index"))
    same = {a for a, p in attempts.items() if (p["problem_ref"], p.get("step_index")) == step}
    superseded = {e.payload.get("supersedes") for e in events if e.type == "verdict"}
    outcomes = tuple(
        e.payload["outcome"]
        for e in events
        if e.type == "verdict"
        and e.payload["attempt_id"] in same
        and e.payload["verdict_id"] not in superseded
        and e.payload["outcome"] != "unverifiable"
    )
    if verdict["outcome"] == "unverifiable":
        outcomes = (*outcomes, "unverifiable")
    rungs = [
        e.payload["rung"]
        for e in events
        if e.type == "hint" and (e.payload["problem_ref"], e.payload.get("step_index")) == step
    ]
    not_learned = any(
        e.type == "diagnostic_item"
        and e.payload.get("response") == "not_learned"
        and e.payload.get("attempt_id") in same
        for e in events
    )
    return Closed(attempt, verdict, outcomes, max(rungs, default=0), len(rungs), not_learned)


# Learning events that are never mastery, review or misconception evidence: a plan is the planner's own output, and
# Study activity is topic-tagged practice against a model-written key (SPEC §19.3.2, JM-D22, JM-D26). The mastery
# (with its review cards) and misconception projections return on them before anything else.
NON_EVIDENCE_TYPES = frozenset({"plan_generated", "plan_decision", "study_activity"})


def closes(ctx: Context, event: StoredEvent) -> bool:
    """Whether a verdict closes an opportunity. Projections read the log through `store.supersession` (D98): a
    re-marked verdict is read at its own place carrying its correction, and the correcting verdict is skipped where it
    stands, so a correction never adds a second opportunity (LRN-06, ASM-23) and a re-mark replays every projection."""
    del ctx  # the rule is the event's own once the log is read through the re-marks
    return event.type == "verdict" and bool(event.payload.get("closes_opportunity"))


def open_steps(events: Sequence[StoredEvent]) -> list[Closed]:
    """The steps of a session with an attempt but no closing verdict: what a session end abandons (LRN-06). Each is
    returned with its latest attempt, its graded outcomes so far and its hints."""
    attempts: dict[str, dict[str, Any]] = {}
    steps: dict[tuple[Any, Any], list[str]] = {}
    for e in events:
        if e.type == "attempt":
            step = (e.payload["problem_ref"], e.payload.get("step_index"))
            attempts[e.payload["attempt_id"]] = e.payload
            steps.setdefault(step, []).append(e.payload["attempt_id"])
    verdicts = [e.payload for e in events if e.type == "verdict"]
    superseded = {v.get("supersedes") for v in verdicts}
    closed = {v["attempt_id"] for v in verdicts if v["closes_opportunity"]}
    out = []
    for step, ids in steps.items():
        if closed & set(ids):
            continue
        outcomes = tuple(v["outcome"] for v in verdicts if v["attempt_id"] in ids and v["verdict_id"] not in superseded
                         and v["outcome"] != "unverifiable")  # fmt: skip
        rungs = [e.payload["rung"] for e in events
                 if e.type == "hint" and (e.payload["problem_ref"], e.payload.get("step_index")) == step]  # fmt: skip
        out.append(Closed(attempts[ids[-1]], {}, outcomes, max(rungs, default=0), len(rungs)))
    return out


class AttemptsProjection:
    """One row per attempt with its latest verdict and the help used on that step."""

    name = "attempts"
    code_version = "attempts.2"  # .2: re-marked verdicts read in place (ASM-23)
    tables: tuple[str, ...] = ("attempts",)

    def fold(self, ctx: Context, event: StoredEvent) -> None:
        if event.type != "verdict":
            return
        closed = opportunity(ctx, event)
        if closed is None:
            return
        a, v = closed.attempt, closed.verdict
        ctx.conn.execute(
            "INSERT OR REPLACE INTO attempts (attempt_id, session_id, item_ref, step_id, kc_ids, mode, outcome,"
            " hints_used, max_rung, time_ms, error_category, misconception_id, occurred_at)"
            " VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (
                a["attempt_id"],
                a["session_id"],
                a.get("item_id") or a["problem_ref"],
                str(a["step_index"]) if a.get("step_index") is not None else None,
                _dumps(a["kc_ids"]),
                a["mode"],
                v["outcome"],
                closed.hints,
                closed.max_rung,
                a["time_ms"],
                v.get("error_category"),
                v.get("misconception_id"),
                event.occurred_at,
            ),
        )


class MasteryProjection:
    """KC state per knowledge component, ability per subject and one FSRS review card per KC that has entered
    `learning`, from closed opportunities, abandoned steps, "not learned" diagnostic answers and mastery checks.

    On each closed opportunity: the LRN-06 observations update ability and evidence (LRN-07, LRN-08, LRN-10); the
    first reviewable opportunity of the local day on each tagged KC rates the KC's card by TABLE LRN-14a and schedules
    it (LRN-13); `fading_stage` moves on worked-example blanks (LRN-17); and the stuck cap counts the opportunities and
    sessions that pass without the KC reaching the bar, moving the KC on with a revisit date when either cap is reached
    (LRN-26). The state stored is the state as of the KC's last event; retrievability falls with time alone, so the
    state shown is re-evaluated when read (`agent_core.learner.model`)."""

    name = "kc_mastery"
    code_version = "pfa_elo.m15.2"  # .2: re-marked verdicts read in place (ASM-23)
    tables: tuple[str, ...] = ("kc_mastery", "subject_ability", "review_cards")

    def fold(self, ctx: Context, event: StoredEvent) -> None:
        if event.type in NON_EVIDENCE_TYPES:
            return
        if closes(ctx, event):
            self._closed(ctx, event)
        elif event.type == "mastery_check":
            self._check(ctx, event)
        elif event.type == "session_ended":
            self._abandoned(ctx, event)

    # Rows are read and written whole: the in-memory evidence is exactly what the columns hold.
    def _load(self, ctx: Context, kc_ids: Iterable[str]) -> dict[str, dict[str, Any]]:
        rows: dict[str, dict[str, Any]] = {}
        for kc in kc_ids:
            row = ctx.conn.execute(
                "SELECT status, succ_w, fail_w, implied_w, attempts, correct, hints_used, bottom_outs,"
                " mastery_check_state, mastery_check, last_seen_at, fading_stage, stuck_count, moved_on_at"
                " FROM kc_mastery WHERE kc_id = ?",
                (kc,),
            ).fetchone()
            if row is None:
                rows[kc] = {"status": "not_started", "evidence": KcEvidence(), "attempts": 0, "correct": 0,
                            "hints_used": 0, "bottom_outs": 0, "check_state": "none", "check": None, "fading": None,
                            "stuck": 0, "moved_on_at": None}  # fmt: skip
            else:
                rows[kc] = {"status": row[0], "evidence": KcEvidence(row[1], row[2], row[3] or 0.0, row[10]),
                            "attempts": row[4], "correct": row[5], "hints_used": row[6], "bottom_outs": row[7],
                            "check_state": row[8], "check": json.loads(row[9]) if row[9] else None,
                            "fading": row[11], "stuck": row[12], "moved_on_at": row[13]}  # fmt: skip
        return rows

    def _ability(self, ctx: Context, subject: str) -> Ability:
        row = ctx.conn.execute("SELECT theta, n FROM subject_ability WHERE subject = ?", (subject,)).fetchone()
        return Ability(row[0], row[1]) if row else Ability()

    def _probability(self, ctx: Context, theta: float, kc: str, ev: KcEvidence) -> float:
        return kc_probability(theta, ev, ctx.params, ctx.content.reference_difficulty(kc), kc)

    def _card(self, ctx: Context, kc: str) -> Card | None:
        row = ctx.conn.execute(
            "SELECT state, stability, difficulty, due_at, reps, lapses, last_review_at FROM review_cards"
            " WHERE kc_id = ?",
            (kc,),
        ).fetchone()
        return Card(*row) if row else None

    def _save(
        self,
        ctx: Context,
        rows: Mapping[str, dict[str, Any]],
        ability: Ability,
        subject: str,
        event: StoredEvent,
        observed: bool = True,
    ) -> None:
        t = event.stamp.physical
        p = ctx.params
        if observed:  # ability changes only with an observation
            ctx.conn.execute(
                "INSERT OR REPLACE INTO subject_ability (subject, theta, n, last_event_seq) VALUES (?, ?, ?, ?)",
                (subject, ability.theta, ability.n, event.seq),
            )
        scheduler = scheduler_of(p)
        for kc, row in rows.items():
            ev: KcEvidence = row["evidence"]
            p_k = self._probability(ctx, ability.theta, kc, ev)
            check = row["check"]
            retry = check.get("retry_after") if check else None
            card = self._card(ctx, kc)
            moved = row["moved_on_at"]
            row["status"] = kc_state(
                StateInputs(
                    direct_observations=row["attempts"],
                    p_mastery=p_k,
                    n_eff=ev.n_eff,
                    confirmed=row["check_state"] == "passed",
                    failed_since_confirmed=bool(check and check.get("review_failed")),
                    low_retrievability=card is not None
                    and scheduler.retrievability(card, t) < p.mastery_review_retrievability,
                    deferral_active=moved is not None and t < moved + int(p.stuck_revisit_days * DAY_MS),
                    retry_lock_active=retry is not None and t < ms_from_iso(retry),
                ),
                p,
            )
            ctx.conn.execute(
                "INSERT OR REPLACE INTO kc_mastery (kc_id, status, p_mastery, n_eff, succ_w, fail_w, implied_w,"
                " attempts, correct, hints_used, bottom_outs, fading_stage, stuck_count, mastery_check_state,"
                " mastery_check, moved_on_at, last_seen_at, last_event_seq)"
                " VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (kc, row["status"], p_k, ev.n_eff, ev.succ_w, ev.fail_w, ev.implied_w, row["attempts"],
                 row["correct"], row["hints_used"], row["bottom_outs"], row["fading"], row["stuck"],
                 row["check_state"], _dumps(check) if check else None, moved, ev.decayed_at, event.seq),
            )  # fmt: skip

    def _closed(self, ctx: Context, event: StoredEvent) -> None:
        closed = opportunity(ctx, event)
        if closed is None:
            return
        self._opportunity(ctx, event, closed, "not_learned" if closed.not_learned else "verdict")

    def _abandoned(self, ctx: Context, event: StoredEvent) -> None:
        """A session end abandons every step with an attempt but no closing verdict (LRN-06): one opportunity each."""
        for step in open_steps(session_events(ctx, event)):
            self._opportunity(ctx, event, step, "abandoned")

    def _opportunity(self, ctx: Context, event: StoredEvent, closed: Closed, close: str) -> None:
        a, v = closed.attempt, closed.verdict
        p, content = ctx.params, ctx.content
        weights = normalised([(k["kc_id"], k["weight"]) for k in a["kc_ids"]])
        subject = subject_of(next(iter(weights)))
        item_id = a.get("item_id")
        timed = a.get("time_ms") or None  # 0: the client did not measure it, so no time modifier applies
        median = content.item_median_ms(item_id)
        op = Opportunity(a["mode"], closed.outcomes, closed.max_rung, a["kc_source"], a.get("kc_confidence"),
                         close, timed, median, content.item_options(item_id))  # fmt: skip
        obs = [] if str(a["problem_ref"]).startswith(PROBE_PREFIX) else observations(op, p)
        prereqs, dependents = content.prerequisites(), content.dependents()
        # Every KC implied evidence can reach (LRN-10: up to the configured depth along either edge) is read and saved
        # with the opportunity, so a second-level prerequisite keeps its implied weight.
        touched, frontier = set(weights), set(weights)
        for _ in range(p.mastery_implied_depth):
            frontier = {o for kc in frontier for o in (*prereqs.get(kc, ()), *dependents.get(kc, ()))} - touched
            touched |= frontier
        rows = self._load(ctx, sorted(touched))
        ability = self._ability(ctx, subject)
        evidence = {kc: row["evidence"] for kc, row in rows.items()}
        t = event.stamp.physical
        before = {kc: self._probability(ctx, ability.theta, kc, evidence[kc]) for kc in weights}
        for y, w in obs:
            observe(y=y, w=w, t=t, weights=weights, first_error_kc=v.get("first_error_kc"), ability=ability,
                    evidence=evidence, p=p, b_item=content.item_difficulty(item_id),
                    b_ref={kc: content.reference_difficulty(kc) for kc in touched}, prerequisites=prereqs,
                    dependents=dependents)  # fmt: skip
        if obs:
            failed = any(y == 0 for y, _ in obs)
            final = closed.outcomes[-1] if closed.outcomes else None
            for kc in weights:
                row = rows[kc]
                row["attempts"] += 1
                row["correct"] += final == "correct" and close == "verdict"
                row["hints_used"] += closed.hints
                row["bottom_outs"] += closed.max_rung >= 5
                if row["check_state"] == "passed":  # a failure after mastery needs review; a passed review clears it
                    check = dict(row["check"] or {"state": "passed"})
                    if failed:
                        check["review_failed"] = True
                    else:
                        check.pop("review_failed", None)
                    row["check"] = check
                self._fading(row, op, str(a["problem_ref"]), p)
                self._stuck(ctx, row, kc, ability.theta, event)
                self._review(ctx, kc, event, Reviewed(closed.outcomes, closed.max_rung, close, timed, median,
                                                      before[kc]))  # fmt: skip
        # Only KCs with evidence get rows: an untouched neighbour stays absent (not_started).
        self._save(ctx, {kc: r for kc, r in rows.items() if r["evidence"].decayed_at is not None}, ability, subject,
                   event, observed=bool(obs))  # fmt: skip

    @staticmethod
    def _fading(row: dict[str, Any], op: Opportunity, problem_ref: str, p: MasteryParams) -> None:
        """LRN-17: a worked-example blank completed correctly without a hint fades one more step; a bottom-out on the
        KC brings one back."""
        stage = row["fading"] or 0
        if op.max_rung >= 5:
            row["fading"] = max(0, stage - 1)
        elif problem_ref.startswith(WORKED_EXAMPLE_PREFIX):
            unaided = op.close == "verdict" and op.graded[:1] == ("correct",) and op.max_rung == 0
            row["fading"] = min(stage + 1, p.fading_max_stage) if unaided else stage

    def _stuck(self, ctx: Context, row: dict[str, Any], kc: str, theta: float, event: StoredEvent) -> None:
        """LRN-26: opportunities and sessions on the KC since it last reached the bar; at either cap the KC moves on
        with a revisit date (the deferral the state reads), and the count starts again. The session count rides in
        the row's check record, which the learner-model snapshot never shows."""
        p = ctx.params
        t = event.stamp.physical
        moved = row["moved_on_at"]
        if moved is not None and t < moved + int(p.stuck_revisit_days * DAY_MS):
            return  # parked: work chosen on it during the deferral does not count again
        check = dict(row["check"] or {"state": row["check_state"]})
        if self._probability(ctx, theta, kc, row["evidence"]) >= p.stuck_cap_mastery:
            row["stuck"] = 0
            check.pop("stuck_sessions", None)
            check.pop("stuck_session", None)
        else:
            row["stuck"] += 1
            if check.get("stuck_session") != event.session_id:
                check["stuck_sessions"] = int(check.get("stuck_sessions", 0)) + 1
                check["stuck_session"] = event.session_id
            if row["stuck"] >= p.stuck_cap_opportunities or check["stuck_sessions"] >= p.stuck_cap_sessions:
                row["moved_on_at"] = t
                row["stuck"] = 0
                check.pop("stuck_sessions", None)
                check.pop("stuck_session", None)
        keep = row["check"] is not None or set(check) - {"state"}
        row["check"] = check if keep else None

    def _review(self, ctx: Context, kc: str, event: StoredEvent, reviewed: Reviewed) -> None:
        """LRN-13, LRN-14: the day's first reviewable opportunity on the KC rates and schedules its card; later ones
        that day leave it unchanged."""
        p = ctx.params
        t = event.stamp.physical
        card = self._card(ctx, kc)
        if card is not None and card.last_review_ms is not None and p.local_day(card.last_review_ms) == p.local_day(t):
            return
        rating = review_rating(reviewed, p.rating_easy_time_share, p.rating_easy_min_mastery,
                               p.rating_hard_time_multiple)  # fmt: skip
        after = scheduler_of(p).review(card, rating, t)
        ctx.conn.execute(
            "INSERT OR REPLACE INTO review_cards (kc_id, state, stability, difficulty, due_at, reps, lapses,"
            " last_review_at, scheduler, last_event_seq) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (kc, after.state, after.stability, after.difficulty, after.due_ms, after.reps, after.lapses,
             after.last_review_ms, SCHEDULER_NAME, event.seq),
        )  # fmt: skip

    def _check(self, ctx: Context, event: StoredEvent) -> None:
        payload = event.payload
        kc = payload["kc_id"]
        rows = self._load(ctx, [kc])
        row = rows[kc]
        check = dict(row["check"] or {"state": "none"})
        state = payload["check_state"]
        check["items_done"] = payload["item_index"] if state == "in_progress" else check.get("items_done", 0)
        if payload["item_index"] == 1 and "first_item_at" not in check:
            check["first_item_at"] = iso_ms(event.stamp.physical)
        if state == "failed":
            retry = event.stamp.physical + int(ctx.params.mastery_check_retry_days * DAY_MS)
            check["retry_after"] = iso_ms(retry)
        if state == "passed":
            check.pop("retry_after", None)
            check.pop("review_failed", None)
            row["status"] = "mastered"  # a pass restores confirmation and clears the review trigger
        check["state"] = state
        row["check"] = check
        row["check_state"] = state
        evidence = row["evidence"]
        if evidence.decayed_at is None:  # a check is not an observation: counts decay only at updates (LRN-07)
            evidence.decayed_at = event.stamp.physical
        self._save(ctx, rows, self._ability(ctx, subject_of(kc)), subject_of(kc), event, observed=False)


# The scheduler named on each card (TABLE LRN-03a `scheduler`): this package's FSRS-6, conformant with py-fsrs.
SCHEDULER_NAME = "fsrs-6 (py-fsrs conformant)"
# Worked-example ids (`wex.<subj>.<lvl>...`, the content-pack contract): an attempt on one is a fading blank (LRN-17).
WORKED_EXAMPLE_PREFIX = "wex."
# A misconception probe's attempt (`probe:<assessment id>`): its events are kept with their mode (ASM-01), but a probe
# feeds the misconception table and the Coach, never mastery (TABLE ASM-01a).
PROBE_PREFIX = "probe:"


class NotesProjection:
    """Reflector notes. A deleted note keeps only its id, status and deletion time (the same row whether folded or
    rebuilt). Erased events are never folded, and erasure replays the projections.

    An update writes the same note id again: the row takes the new text, evidence and status and keeps the time the
    note was first written, while `updated_at` moves. Accepting a review item (MEM-09: a `correction` of the pending
    note with action `override`) makes the pending note active. A note whose marking evidence came only from
    verdicts or matches a later re-mark replaced reads as deleted (`Supersession.note_withdrawn`, CUR-36)."""

    name = "notes"
    code_version = "notes.3"  # .3: a note resting only on re-marked markings is withdrawn (CUR-36)
    tables: tuple[str, ...] = ("notes",)

    @staticmethod
    def _deleted(ctx: Context, event: StoredEvent) -> None:
        ctx.conn.execute(
            "INSERT OR REPLACE INTO notes (note_id, subject, kc_ids, text, status, source_event_ids,"
            " source_session_id, created_at, updated_at, expires_at)"
            " VALUES (?, NULL, NULL, '', 'deleted', ?, NULL, ?, ?, NULL)",
            (event.payload["note_id"], _dumps([event.event_id]), event.occurred_at, event.occurred_at),
        )

    def fold(self, ctx: Context, event: StoredEvent) -> None:
        payload = event.payload
        if event.type == "note_written" and ctx.remarks.note_withdrawn(event):
            self._deleted(ctx, event)  # it rested only on markings a re-mark replaced (CUR-36)
        elif event.type == "note_written":
            ctx.conn.execute(
                "INSERT INTO notes (note_id, subject, kc_ids, text, status, source_event_ids, source_session_id,"
                " created_at, updated_at, expires_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)"
                " ON CONFLICT (note_id) DO UPDATE SET subject = excluded.subject, kc_ids = excluded.kc_ids,"
                " text = excluded.text, status = excluded.status, source_event_ids = excluded.source_event_ids,"
                " source_session_id = excluded.source_session_id, updated_at = excluded.updated_at,"
                " expires_at = excluded.expires_at,"
                " created_at = CASE WHEN notes.status = 'deleted' THEN excluded.created_at ELSE notes.created_at END",
                (
                    payload["note_id"],
                    payload.get("subject"),
                    _dumps(payload["kc_ids"]) if "kc_ids" in payload else None,
                    payload["text"],
                    payload["status"],
                    _dumps(payload["source_event_ids"]),
                    event.session_id,
                    event.occurred_at,
                    event.occurred_at,
                    ms_from_iso(payload["expires_at"]) if "expires_at" in payload else None,
                ),
            )
        elif event.type == "note_deleted":
            self._deleted(ctx, event)
        elif event.type == "correction" and payload.get("target") == "note" and payload.get("action") == "override":
            ctx.conn.execute(
                "UPDATE notes SET status = 'active', updated_at = ? WHERE note_id = ? AND status = 'pending_review'",
                (event.occurred_at, payload.get("target_id")),
            )


FADING_MS = 90 * 86_400_000  # SPEC §19.3.5: no update and no newer cited evidence for 90 days


@dataclass(frozen=True)
class NoteLifecycle:
    """A note's derived standing (SPEC §19.3.5, §19.5.6; JM-D28): never written by a model, never stored."""

    confidence: str  # "tentative" or "supported"
    fading: bool


def note_lifecycle(conn: sqlite3.Connection, note_ids: Iterable[str], now_ms: int) -> dict[str, NoteLifecycle]:
    """Derived from what the notes projection already folds: the evidence every write of the note cited (its
    `note_written` events, an update's included) is `supported` when it spans two or more distinct session ids, else
    `tentative`; `fading` when the note's last write and its newest cited evidence are both older than 90 days.
    Derived at read, because fading moves with the clock; a replay gives the same answer for the same moment."""
    out: dict[str, NoteLifecycle] = {}
    for note_id in dict.fromkeys(note_ids):
        row = conn.execute("SELECT source_event_ids, updated_at FROM notes WHERE note_id = ?", (note_id,)).fetchone()
        if row is None:
            continue
        cited = set(json.loads(row[0] or "[]"))
        for (payload,) in conn.execute(
            "SELECT payload FROM events WHERE type = 'note_written' AND erased_at IS NULL"
            " AND json_extract(payload, '$.note_id') = ?",
            (note_id,),
        ):
            cited.update(json.loads(payload).get("source_event_ids") or [])
        sessions: set[str] = set()
        newest = 0
        ids = sorted(cited)
        for start in range(0, len(ids), 500):
            chunk = ids[start : start + 500]
            marks = ", ".join("?" for _ in chunk)
            for session_id, occurred in conn.execute(
                f"SELECT session_id, occurred_at FROM events WHERE erased_at IS NULL AND event_id IN ({marks})",  # noqa: S608
                chunk,
            ):
                if session_id:
                    sessions.add(str(session_id))
                newest = max(newest, int(occurred))
        last = max(int(row[1] or 0), newest)
        out[note_id] = NoteLifecycle("supported" if len(sessions) >= 2 else "tentative", now_ms - last > FADING_MS)
    return out


class MisconceptionsProjection:
    """Misconceptions matched by the checker or a probe (PED-22), and the student's disputes (LRN-37).

    Each `misconception_evidence` event is one match. A probe match, or `active_evidence` matches, make the
    misconception active; fewer leave it suspected. While it is active, a correct closing verdict on its KC starts
    `resolving`; correct closing verdicts on two later days resolve it. A new match while resolving makes it active
    again; a match after it was resolved starts it afresh. "This isn't right" (a `correction` with action
    `dispute`) sets it disputed, and it returns to active only after `dispute_reactivate` new matches; withdrawing
    the dispute (`undispute`) re-derives the status from its evidence. Evidence a verdict gave follows that
    verdict's re-marks (`store.supersession`, CUR-36): a wrong key's match is withdrawn with its verdict, and the
    corrected marking's match counts once, where the verdict stands.

    `resolving_correct` holds the correct days counted while resolving and, while disputed, the matches since the
    dispute; the learner-model snapshot shows it only while resolving. Days are the HLC day of the event, so a replay
    gives the same rows."""

    name = "learner_misconceptions"
    tables: tuple[str, ...] = ("learner_misconceptions",)

    def __init__(self, active_evidence: int = 2, dispute_reactivate: int = 2) -> None:
        self.active_evidence = active_evidence
        self.dispute_reactivate = dispute_reactivate
        # The thresholds change the fold, so they are part of the version: a new value rebuilds the table.
        # .2: ASM-23 re-marks; .3: evidence follows its verdict's re-mark (CUR-36)
        self.code_version = f"misconceptions.3/{active_evidence}/{dispute_reactivate}"

    def _row(self, ctx: Context, mc_id: str) -> dict[str, Any] | None:
        row = ctx.conn.execute(
            "SELECT kc_id, status, evidence_count, fixes_tried, resolving_correct, first_seen_at, last_seen_at"
            " FROM learner_misconceptions WHERE misconception_id = ?",
            (mc_id,),
        ).fetchone()
        keys = ("kc_id", "status", "evidence", "fixes", "counted", "first", "last")
        return dict(zip(keys, row, strict=True)) if row is not None else None

    def _save(self, ctx: Context, mc_id: str, row: dict[str, Any], seq: int) -> None:
        ctx.conn.execute(
            "INSERT OR REPLACE INTO learner_misconceptions (misconception_id, kc_id, status, evidence_count,"
            " fixes_tried, resolving_correct, first_seen_at, last_seen_at, last_event_seq)"
            " VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (mc_id, row["kc_id"], row["status"], row["evidence"], row["fixes"], row["counted"], row["first"],
             row["last"], seq),
        )  # fmt: skip

    def _settled(self, evidence: int) -> str:
        return "active" if evidence >= self.active_evidence else "suspected"

    def fold(self, ctx: Context, event: StoredEvent) -> None:
        if event.type in NON_EVIDENCE_TYPES:
            return
        if event.type == "misconception_evidence":
            self._matched(ctx, event)
        elif event.type == "correction" and event.payload.get("target") == "misconception":
            self._corrected(ctx, event)
        elif event.type == "verdict" and event.payload.get("closes_opportunity"):
            self._verdict(ctx, event)

    def _matched(self, ctx: Context, event: StoredEvent) -> None:
        payload, t = event.payload, event.stamp.physical
        mc_id = payload["misconception_id"]
        row = self._row(ctx, mc_id) or {"kc_id": payload["kc_id"], "status": None, "evidence": 0, "fixes": 0,
                                         "counted": None, "first": t, "last": t}  # fmt: skip
        row["evidence"] += 1
        row["last"] = max(row["last"], t)
        status = row["status"]
        if status == "disputed":
            row["counted"] = (row["counted"] or 0) + 1
            if row["counted"] >= self.dispute_reactivate:
                row["status"], row["counted"] = "active", None
        elif status in ("active", "resolving"):
            row["status"], row["counted"] = "active", None
        else:
            if status == "resolved":
                row["evidence"] = 1  # a recurrence after resolution is counted afresh
            probe = payload["evidence_source"] == "probe"
            row["status"], row["counted"] = ("active" if probe else self._settled(row["evidence"])), None
        self._save(ctx, mc_id, row, event.seq)

    def _corrected(self, ctx: Context, event: StoredEvent) -> None:
        mc_id = event.payload.get("target_id")
        if not isinstance(mc_id, str):
            return
        row = self._row(ctx, mc_id)
        if row is None:
            return
        action = event.payload.get("action")
        if action == "dispute" and row["status"] != "disputed":
            row["status"], row["counted"] = "disputed", 0
        elif action == "undispute" and row["status"] == "disputed":
            row["status"], row["counted"] = self._settled(row["evidence"]), None
        else:
            return
        self._save(ctx, mc_id, row, event.seq)

    def _verdict(self, ctx: Context, event: StoredEvent) -> None:
        if event.payload.get("outcome") != "correct":
            return
        closed = opportunity(ctx, event)
        if closed is None or closed.outcomes[-1:] != ("correct",):
            return
        kc_ids = sorted({k["kc_id"] for k in closed.attempt["kc_ids"]})
        marks = ", ".join("?" for _ in kc_ids)
        query = (  # placeholders only
            "SELECT misconception_id FROM learner_misconceptions WHERE status IN ('active', 'resolving')"  # noqa: S608
            f" AND kc_id IN ({marks}) ORDER BY misconception_id"
        )
        found = ctx.conn.execute(query, kc_ids).fetchall()
        day = event.stamp.physical // DAY_MS
        for (mc_id,) in found:
            row = self._row(ctx, mc_id)
            if row is None:
                continue
            if row["status"] == "active":
                row["status"], row["counted"] = "resolving", 0
            elif self._last_day(ctx, mc_id) < day:
                row["counted"] = (row["counted"] or 0) + 1
                if row["counted"] >= 2:
                    row["status"], row["counted"] = "resolved", None
            else:
                continue  # a second correct answer on the same day does not count again
            self._save(ctx, mc_id, row, event.seq)

    def _last_day(self, ctx: Context, mc_id: str) -> int:
        """The HLC day of the verdict that last moved a resolving misconception (its row's last event)."""
        row = ctx.conn.execute(
            "SELECT e.hlc FROM learner_misconceptions m JOIN events e ON e.seq = m.last_event_seq"
            " WHERE m.misconception_id = ?",
            (mc_id,),
        ).fetchone()
        return int(row[0][:15]) // DAY_MS


# The student-editable preferences and the values each may take (the learner-model contract's preferences).
PREFERENCE_VALUES: dict[str, tuple[Any, ...]] = {
    "start_style": ("worked_example", "hint"),
    "representation": ("diagram", "simpler_numbers", "analogy", "worked_example"),
    "pace": ("short", "normal"),
    "unprompted_help": (True, False),
    "self_explanation_prompts": (True, False),
}


class PreferencesProjection:
    """How the student likes to learn. A student edit (a `correction` of a preference with action `override` and a
    value the preference may take) overrides any inference until `clear_override` removes it (LRN-37). A row's
    value is `{"override": value}` or `{"inferred": value}` with its confidence.

    No event yet carries the outcome of an explanation style, so nothing is inferred and only overrides are stored;
    the snapshot shows the prior for the rest."""

    name = "preferences"
    code_version = "preferences.1"
    tables: tuple[str, ...] = ("preferences",)

    def fold(self, ctx: Context, event: StoredEvent) -> None:
        payload = event.payload
        if event.type != "correction" or payload.get("target") != "preference":
            return
        key, action, value = payload.get("target_id"), payload.get("action"), payload.get("value")
        if key not in PREFERENCE_VALUES:
            return
        if action == "override" and any(value == v and type(value) is type(v) for v in PREFERENCE_VALUES[key]):
            ctx.conn.execute(
                "INSERT OR REPLACE INTO preferences (key, value, confidence, last_event_seq) VALUES (?, ?, 1.0, ?)",
                (key, _dumps({"override": value}), event.seq),
            )
        elif action == "clear_override":
            ctx.conn.execute("DELETE FROM preferences WHERE key = ?", (key,))


PLAN_STATUS = {"start": "started", "change": "changed", "not_today": "not_today"}
# Block kinds that complete by checked work on their KCs; reflect completes with the rest, mock and mastery_check by
# their own events.
WORK_BLOCKS = ("warmup", "main", "mixed")


def block_target(kind: str, kc_ids: Sequence[str], p: MasteryParams) -> int:
    """How many closed opportunities complete a plan block (LRN-31): each listed review for a warm-up, the configured
    item counts for a main or mixed block, one sitting or check for a mock or mastery check, none for a reflection."""
    if kind == "warmup":
        return max(1, len(kc_ids))
    if kind == "main":
        return p.planner_main_items
    if kind == "mixed":
        return p.planner_mixed_items
    return 0 if kind == "reflect" else 1


class StudyPlansProjection:
    """Every proposed plan (LRN-28) with its decision (LRN-31) and the progress of its blocks.

    A plan is `proposed` when generated; `start`, `change` and `not_today` decisions set `started`, `changed` and
    `not_today` (a change also generates the changed plan as a new proposal). Once started, each checked opportunity
    on a block's KCs counts towards the first unfinished block that lists one of them; a block is complete at its
    target, a reflection when every other block is, and the plan is `done` when all are. Completed work is never
    undone: stopping leaves the plan `started` with its finished blocks, and resuming continues at the first
    unfinished one."""

    name = "study_plans"
    # .2: a warm-up of Study cards (SPEC §19.4, W8) completes with reviews of its own cards, never with practice.
    code_version = "study_plans.2"
    tables: tuple[str, ...] = ("study_plans",)

    def fold(self, ctx: Context, event: StoredEvent) -> None:
        payload = event.payload
        t = event.stamp.physical
        if event.type == "plan_generated":
            blocks = [{**b, "target": len(b["study_cards"]) if b.get("study_cards") else
                       block_target(b["kind"], b.get("kc_ids", []), ctx.params), "done": 0,
                       "completed_at": None} for b in payload["blocks"]]  # fmt: skip
            items = {"phase": payload["phase"], "minutes": payload["minutes"], "blocks": blocks,
                     "deferred_reviews": payload.get("deferred_reviews", 0), "started_at": None}  # fmt: skip
            ctx.conn.execute(
                "INSERT OR REPLACE INTO study_plans (plan_id, day, items, status, created_at, updated_at)"
                " VALUES (?, ?, ?, 'proposed', ?, ?)",
                (payload["plan_id"], payload["date"], _dumps(items), t, t),
            )
        elif event.type == "plan_decision":
            row = ctx.conn.execute("SELECT items, status FROM study_plans WHERE plan_id = ?",
                                   (payload["plan_id"],)).fetchone()  # fmt: skip
            if row is None or row[1] != "proposed":
                return  # a decision is taken once per proposal
            items = json.loads(row[0])
            if payload["decision"] == "start":
                items["started_at"] = t
            ctx.conn.execute("UPDATE study_plans SET status = ?, items = ?, updated_at = ? WHERE plan_id = ?",
                             (PLAN_STATUS[payload["decision"]], _dumps(items), t, payload["plan_id"]))  # fmt: skip
        elif closes(ctx, event):
            closed = opportunity(ctx, event)
            graded = [o for o in (closed.outcomes if closed else ()) if o != "unverifiable"]
            if closed is None or not graded or closed.attempt["mode"] == "solution":
                return
            self._progress(ctx, t, {k["kc_id"] for k in closed.attempt["kc_ids"]}, WORK_BLOCKS)
        elif event.type == "mock_submitted":
            self._progress(ctx, t, None, ("mock",))
        elif event.type == "study_activity" and payload.get("activity") == "card_review":
            self._study_card(ctx, t, str(payload.get("ref", "")).removeprefix("card:"))
        elif event.type == "mastery_check" and payload["check_state"] in ("passed", "failed"):
            self._progress(ctx, t, {payload["kc_id"]}, ("mastery_check",))

    def _progress(self, ctx: Context, t: int, kcs: set[str] | None, kinds: Sequence[str]) -> None:
        row = ctx.conn.execute(
            "SELECT plan_id, items FROM study_plans WHERE status = 'started' ORDER BY created_at DESC, plan_id DESC"
            " LIMIT 1"
        ).fetchone()
        if row is None:
            return
        items = json.loads(row[1])
        block = next((b for b in items["blocks"] if b["kind"] in kinds and b["completed_at"] is None
                      and not b.get("study_cards")
                      and (kcs is None or not b.get("kc_ids") or kcs & set(b["kc_ids"]))), None)  # fmt: skip
        if block is None:
            return
        self._advance(ctx, t, row[0], items, block)

    def _study_card(self, ctx: Context, t: int, card_id: str) -> None:
        """A Study card reviewed (a `study_activity` card review): the started plan's Study warm-up advances once per
        listed card, whoever's scheduler moved it (Study's own; the learner model's cards are untouched, JM-D26)."""
        row = ctx.conn.execute(
            "SELECT plan_id, items FROM study_plans WHERE status = 'started' ORDER BY created_at DESC, plan_id DESC"
            " LIMIT 1"
        ).fetchone()
        if row is None:
            return
        items = json.loads(row[1])
        block = next((b for b in items["blocks"] if b["completed_at"] is None and card_id in
                      {c["card_id"] for c in b.get("study_cards", [])}
                      and card_id not in b.get("reviewed", [])), None)  # fmt: skip
        if block is None:
            return
        block["reviewed"] = [*block.get("reviewed", []), card_id]
        self._advance(ctx, t, row[0], items, block)

    def _advance(self, ctx: Context, t: int, plan_id: str, items: dict[str, Any], block: dict[str, Any]) -> None:
        block["done"] += 1
        if block["done"] >= block["target"]:
            block["completed_at"] = t
        rest = [b for b in items["blocks"] if b["kind"] != "reflect"]
        if all(b["completed_at"] is not None for b in rest):
            for b in items["blocks"]:
                if b["completed_at"] is None:
                    b["completed_at"] = t
        status = "done" if all(b["completed_at"] is not None for b in items["blocks"]) else "started"
        ctx.conn.execute("UPDATE study_plans SET items = ?, status = ?, updated_at = ? WHERE plan_id = ?",
                         (_dumps(items), status, t, plan_id))  # fmt: skip


def week_start(day: int) -> int:
    """The Monday on or before a local day number (day 0, 1970-01-01, was a Thursday)."""
    return day - (day + 3) % 7


class EngagementProjection:
    """Engagement (TABLE LRN-03a): per local day the active minutes, sessions, attempts, solutions viewed, frustration
    signals and gaming flags, the streak length and whether a freeze covered the day; and the current streak, freezes
    left, last active day, frustration and confidence bias.

    A day counts for the streak with one completed plan block or `streak_min_minutes` of checked work; opening the
    app alone does not (LRN-33). When a day counts, the days missed since the last one are covered by freezes when
    enough are left, else the streak starts again at 1 (shown as "New streak: day 1", never as a loss). A freeze is
    earned each week (`streak_weekly_freezes`, or `support_weekly_freezes` while attendance was below
    `support_min_days_per_week` in each of the last `support_weeks` weeks, LRN-27), up to `streak_max_freezes`.

    `frustration` is an exponentially weighted average of frustration signals over closed attempts and help events
    (half-life `frustration_half_life_events`, LRN-16): a wrong answer after a hint, a bottom-out and a gaming flag.
    `confidence_bias` is the mean of self-rated confidence (1-3 as 0, 0.5, 1) minus the outcome over the last
    `confidence_bias_window` rated attempts."""

    name = "engagement"
    code_version = "engagement.1"
    tables: tuple[str, ...] = ("engagement_state", "engagement_daily")

    def fold(self, ctx: Context, event: StoredEvent) -> None:
        payload, kind = event.payload, event.type
        day = ctx.params.local_day(event.stamp.physical)
        if kind == "session_started":
            self._bump(ctx, day, event, sessions=1)
        elif kind == "session_ended":
            self._bump(ctx, day, event, minutes_active=round(payload["active_ms"] / 60_000))
        elif kind == "attempt":
            self._bump(ctx, day, event, attempts=1)
        elif kind == "integrity_signal":
            signal = payload["kind"]
            if signal == "solution_view":
                self._bump(ctx, day, event, solutions_viewed=1)
            elif signal in ("hint_burst", "fast_guess_run", "bottom_out"):
                gaming = int(signal != "bottom_out")
                self._bump(ctx, day, event, frustration_signals=1, gaming_flags=gaming)
                self._frustration(ctx, event, 1.0)
        elif closes(ctx, event):
            closed = opportunity(ctx, event)
            if closed is None:
                return
            final = closed.outcomes[-1] if closed.outcomes else None
            signal = final == "incorrect" and closed.max_rung >= 1
            if signal:
                self._bump(ctx, day, event, frustration_signals=1)
            self._frustration(ctx, event, float(signal))
            if closed.attempt.get("confidence_rating") is not None:
                self._confidence(ctx, event)
            self._maybe_counts(ctx, day, event)
        elif kind in ("mock_submitted", "mastery_check"):
            self._maybe_counts(ctx, day, event)

    # --- rows ---------------------------------------------------------------------------------------------------------

    def _bump(self, ctx: Context, day: int, event: StoredEvent, **add: int) -> None:
        columns = ("minutes_active", "sessions", "attempts", "solutions_viewed", "frustration_signals", "gaming_flags")
        values = [add.get(c, 0) for c in columns]
        ctx.conn.execute(
            "INSERT INTO engagement_daily (day, minutes_active, sessions, attempts, solutions_viewed, streak_length,"
            " freeze_used, frustration_signals, gaming_flags) VALUES (?, ?, ?, ?, ?, 0, 0, ?, ?)"
            " ON CONFLICT (day) DO UPDATE SET minutes_active = minutes_active + excluded.minutes_active,"
            " sessions = sessions + excluded.sessions, attempts = attempts + excluded.attempts,"
            " solutions_viewed = solutions_viewed + excluded.solutions_viewed,"
            " frustration_signals = frustration_signals + excluded.frustration_signals,"
            " gaming_flags = gaming_flags + excluded.gaming_flags",
            (local_date(day), *values),
        )
        self._state(ctx, event)

    def _state(self, ctx: Context, event: StoredEvent) -> dict[str, Any]:
        row = ctx.conn.execute("SELECT streak_days, freezes_left, last_active_date, frustration, confidence_bias"
                               " FROM engagement_state WHERE id = 1").fetchone()  # fmt: skip
        if row is None:
            ctx.conn.execute("INSERT INTO engagement_state (id, last_event_seq) VALUES (1, ?)", (event.seq,))
            return {"streak_days": 0, "freezes_left": 0, "last_active_date": None, "frustration": 0.0,
                    "confidence_bias": None}  # fmt: skip
        keys = ("streak_days", "freezes_left", "last_active_date", "frustration", "confidence_bias")
        return dict(zip(keys, row, strict=True))

    def _put(self, ctx: Context, state: Mapping[str, Any], event: StoredEvent) -> None:
        ctx.conn.execute(
            "UPDATE engagement_state SET streak_days = ?, freezes_left = ?, last_active_date = ?, frustration = ?,"
            " confidence_bias = ?, last_event_seq = ? WHERE id = 1",
            (state["streak_days"], state["freezes_left"], state["last_active_date"], state["frustration"],
             state["confidence_bias"], event.seq),
        )  # fmt: skip

    # --- signals ----------------------------------------------------------------------------------------------

    def _frustration(self, ctx: Context, event: StoredEvent, signal: float) -> None:
        state = self._state(ctx, event)
        alpha = 1 - 2 ** (-1 / ctx.params.frustration_half_life_events)
        state["frustration"] = min(1.0, max(0.0, state["frustration"] + alpha * (signal - state["frustration"])))
        self._put(ctx, state, event)

    def _confidence(self, ctx: Context, event: StoredEvent) -> None:
        rated = ctx.conn.execute(
            "SELECT json_extract(e.payload, '$.confidence_rating'), a.outcome FROM events e"
            " JOIN attempts a ON a.attempt_id = json_extract(e.payload, '$.attempt_id')"
            " WHERE e.type = 'attempt' AND e.erased_at IS NULL"
            " AND json_extract(e.payload, '$.confidence_rating') IS NOT NULL"
            " AND (e.hlc, e.device_id, e.origin_seq) <= (?, ?, ?)"
            " ORDER BY e.hlc DESC, e.device_id DESC, e.origin_seq DESC LIMIT ?",
            (*event.order, ctx.params.confidence_bias_window),
        ).fetchall()
        if not rated:
            return
        gaps = [(int(rating) - 1) / 2 - (outcome == "correct") for rating, outcome in rated]
        state = self._state(ctx, event)
        state["confidence_bias"] = min(1.0, max(-1.0, sum(gaps) / len(gaps)))
        self._put(ctx, state, event)

    # --- streaks (LRN-33) --------------------------------------------------------------------------------------------

    def _maybe_counts(self, ctx: Context, day: int, event: StoredEvent) -> None:
        """Whether today now counts for the streak: checked work reached the minimum, or a plan block completed."""
        state = self._state(ctx, event)
        if state["last_active_date"] == local_date(day):
            return
        p = ctx.params
        start = day * DAY_MS - p.learner_day_offset_minutes * 60_000
        checked = ctx.conn.execute(
            "SELECT coalesce(sum(time_ms), 0) FROM attempts WHERE outcome != 'unverifiable' AND mode != 'solution'"
            " AND occurred_at >= ? AND occurred_at <= ?",
            (start, event.occurred_at),
        ).fetchone()[0]
        blocks = any(
            start <= (b.get("completed_at") or -1) <= event.stamp.physical and b["kind"] != "reflect"
            for (items,) in ctx.conn.execute("SELECT items FROM study_plans WHERE updated_at >= ?", (start,))
            for b in json.loads(items)["blocks"]
        )
        if checked >= p.streak_min_minutes * 60_000 or blocks:
            self._count_day(ctx, day, state, event)

    def _weekly_freezes(self, ctx: Context, day: int) -> int:
        """Freezes earned a week: the extra-support number while attendance was low in each of the last weeks."""
        p = ctx.params
        this_week = week_start(day)
        first = ctx.conn.execute("SELECT min(day) FROM engagement_daily WHERE streak_length > 0").fetchone()[0]
        earliest = this_week - 7 * p.support_weeks
        if first is None or day_number(first) > earliest:
            return p.streak_weekly_freezes
        for week in range(p.support_weeks):
            begin = this_week - 7 * (week + 1)
            active = ctx.conn.execute(
                "SELECT count(*) FROM engagement_daily WHERE streak_length > 0 AND freeze_used = 0"
                " AND day >= ? AND day < ?",
                (local_date(begin), local_date(begin + 7)),
            ).fetchone()[0]
            if active >= p.support_min_days_per_week:
                return p.streak_weekly_freezes
        return p.support_weekly_freezes

    def _count_day(self, ctx: Context, day: int, state: dict[str, Any], event: StoredEvent) -> None:
        p = ctx.params
        last = state["last_active_date"]
        if last is None:
            streak, freezes = 1, state["freezes_left"]
        else:
            previous = day_number(last)
            weekly = self._weekly_freezes(ctx, day)
            mondays = sum(1 for d in range(previous + 1, day + 1) if week_start(d) == d)
            freezes = min(p.streak_max_freezes, state["freezes_left"] + mondays * weekly)
            missed = list(range(previous + 1, day))
            if len(missed) <= freezes:
                freezes -= len(missed)
                for gap in missed:
                    ctx.conn.execute(
                        "INSERT INTO engagement_daily (day, minutes_active, sessions, attempts, solutions_viewed,"
                        " streak_length, freeze_used, frustration_signals, gaming_flags)"
                        " VALUES (?, 0, 0, 0, 0, ?, 1, 0, 0) ON CONFLICT (day) DO UPDATE SET freeze_used = 1,"
                        " streak_length = excluded.streak_length",
                        (local_date(gap), state["streak_days"]),
                    )
                streak = state["streak_days"] + 1
            else:
                streak = 1
        self._bump(ctx, day, event)  # the day's row exists before its streak length is set
        ctx.conn.execute("UPDATE engagement_daily SET streak_length = ? WHERE day = ?", (streak, local_date(day)))
        state.update(streak_days=streak, freezes_left=freezes, last_active_date=local_date(day))
        self._put(ctx, state, event)


def default_projections(settings: Any = None) -> tuple[Projection, ...]:
    """Every projection, with the misconception thresholds from `settings` when given (`misconception_*` keys)."""
    misconceptions = (
        MisconceptionsProjection(settings.misconception_active_evidence, settings.misconception_dispute_reactivate)
        if settings is not None
        else MisconceptionsProjection()
    )
    return (AttemptsProjection(), MasteryProjection(), NotesProjection(), misconceptions, PreferencesProjection(),
            StudyPlansProjection(), EngagementProjection())  # fmt: skip


# --- the projector -------------------------------------------------------------------------------------------------

# Columns that are local bookkeeping, not learner state: excluded from checksums compared across devices.
LOCAL_COLUMNS = {"last_event_seq"}


def table_checksum(conn: sqlite3.Connection, table: str) -> str:
    info = conn.execute(f"PRAGMA table_info({table})").fetchall()
    columns = [row[1] for row in info if row[1] not in LOCAL_COLUMNS]
    keys = [row[1] for row in sorted(info, key=lambda r: r[5]) if row[5]] or columns
    digest = hashlib.sha256()
    query = f"SELECT {', '.join(columns)} FROM {table} ORDER BY {', '.join(keys)}"  # noqa: S608
    for row in conn.execute(query):
        digest.update(_dumps([round(v, 12) if isinstance(v, float) else v for v in row]).encode())
        digest.update(b"\n")
    return digest.hexdigest()


@dataclass(frozen=True)
class State:
    code_version: str
    params_hash: str
    last_seq: int
    cursor: tuple[str, str, int] | None


class Projector:
    def __init__(
        self,
        conn: sqlite3.Connection,
        params: MasteryParams,
        content: ContentIndex | None = None,
        projections: Sequence[Projection] | None = None,
        wall_ms: Callable[[], int] = lambda: int(time.time() * 1000),
    ) -> None:
        self.wall_ms = wall_ms
        self.ctx = Context(conn, params, content or NoContent(), migrations=migrations_of(content))
        self.projections = tuple(projections if projections is not None else default_projections())
        migrated = self.ctx.migrations.fingerprint
        pinned = self.ctx.content.fingerprint + (f"|kc_migrations:{migrated}" if migrated else "")
        self.params_hash = params.digest(pinned)
        self.rebuilding = False  # views show "Updating your progress…" while a background rebuild runs

    @property
    def conn(self) -> sqlite3.Connection:
        return self.ctx.conn

    def state(self, name: str) -> State | None:
        row = self.conn.execute(
            "SELECT code_version, params_hash, last_seq, last_hlc, last_device_id, last_origin_seq"
            " FROM projection_state WHERE name = ?",
            (name,),
        ).fetchone()
        if row is None:
            return None
        return State(row[0], row[1], row[2], (row[3], row[4], row[5]) if row[3] is not None else None)

    def _record(
        self, projection: Projection, last_seq: int, cursor: tuple[str, str, int] | None, rebuilt: bool
    ) -> None:
        # The checksum is taken after a rebuild only: hashing every table on every fold would make folding quadratic.
        checksum = self.checksum(projection.name) if rebuilt else None
        self.conn.execute(
            "INSERT INTO projection_state (name, code_version, params_hash, last_seq, last_hlc, last_device_id,"
            " last_origin_seq, rebuilt_at, checksum) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)"
            " ON CONFLICT (name) DO UPDATE SET code_version = excluded.code_version,"
            " params_hash = excluded.params_hash, last_seq = excluded.last_seq, last_hlc = excluded.last_hlc,"
            " last_device_id = excluded.last_device_id, last_origin_seq = excluded.last_origin_seq,"
            " rebuilt_at = coalesce(excluded.rebuilt_at, projection_state.rebuilt_at),"
            " checksum = excluded.checksum",
            (
                projection.name,
                projection.code_version,
                self.params_hash,
                last_seq,
                *(cursor or (None, None, None)),
                self.wall_ms() if rebuilt else None,
                checksum,
            ),
        )

    def apply(self, event: StoredEvent) -> None:
        """Fold one newly stored event into every projection (inside the transaction that stored it). A re-mark
        (a verdict that supersedes another, ASM-23) changes how an earlier verdict reads, and evidence on a re-marked
        attempt reads where its verdict stands (CUR-36), so either replays."""
        remark = self._follows_a_remark(event)
        for projection in self.projections:
            state = self.state(projection.name)
            if remark or (state is not None and state.cursor is not None and event.order < state.cursor):
                self.replay(projection)
                continue
            if not event.erased:
                for read in self.ctx.migrations.event(event):
                    projection.fold(self.ctx, read)
            last_seq = max(event.seq, state.last_seq if state else 0)
            cursor = max(event.order, state.cursor) if state and state.cursor else event.order
            self._record(projection, last_seq, cursor, rebuilt=False)

    def replay(self, projection: Projection) -> None:
        self.replay_all([projection])

    def replay_all(self, projections: Sequence[Projection]) -> None:
        """Clear and refold the projections in one pass over the log, in merge order (LRN-02)."""
        for projection in projections:
            for table in projection.tables:
                self.conn.execute(f"DELETE FROM {table}")  # noqa: S608 - table names are the projection's own
        sessions: dict[str, list[StoredEvent]] = {}
        remarks = self.supersession()
        opportunities: dict[str, Closed | None] = {}
        ctx = replace(self.ctx, sessions=sessions, remarks=remarks, opportunities=opportunities)
        last_seq, cursor = 0, None
        for row in self.conn.execute(f"SELECT {COLUMNS} FROM events ORDER BY {MERGE_ORDER}"):  # noqa: S608
            stored = StoredEvent.from_row(row)
            # An erased payload contributes nothing (DAT-09); a re-mark, and the evidence it recorded, are read in
            # place of the verdict they replace (ASM-23, CUR-36).
            for event in remarks.read_all(stored) if not stored.erased else ():
                for read in ctx.migrations.event(event):  # read through the KC migrations (CUR-10)
                    if read.session_id is not None:
                        sessions.setdefault(read.session_id, []).append(read)
                    opportunities.clear()
                    for projection in projections:
                        projection.fold(ctx, read)
            last_seq, cursor = max(last_seq, stored.seq), stored.order
        for projection in projections:
            self._record(projection, last_seq, cursor, rebuilt=True)

    def supersession(self) -> Supersession:
        """The log's re-marked verdicts and the evidence that follows them (ASM-23, CUR-36); empty, and read
        without loading verdicts, when nothing was re-marked."""
        if (
            self.conn.execute(
                "SELECT 1 FROM events WHERE type = 'verdict' AND erased_at IS NULL"
                " AND json_extract(payload, '$.supersedes') IS NOT NULL LIMIT 1"
            ).fetchone()
            is None
        ):
            return Supersession()
        rows = self.conn.execute(
            f"SELECT {COLUMNS} FROM events WHERE erased_at IS NULL AND (type = 'verdict' OR (type ="  # noqa: S608
            f" 'misconception_evidence' AND json_extract(payload, '$.attempt_id') IS NOT NULL)) ORDER BY {MERGE_ORDER}"
        )
        return Supersession.of(map(StoredEvent.from_row, rows))

    def _follows_a_remark(self, event: StoredEvent) -> bool:
        """Whether a new event changes how the log reads under re-marks (ASM-23, CUR-36): a re-mark itself, or
        evidence naming an attempt that was re-marked (it belongs to one marking of it)."""
        if event.erased or not follows_a_verdict(event):
            return False
        if event.type == "verdict":
            return bool(event.payload.get("supersedes"))
        return (
            self.conn.execute(
                "SELECT 1 FROM events WHERE type = 'verdict' AND erased_at IS NULL"
                " AND json_extract(payload, '$.attempt_id') = ? AND json_extract(payload, '$.supersedes') IS NOT NULL"
                " LIMIT 1",
                (event.payload["attempt_id"],),
            ).fetchone()
            is not None
        )

    def rebuild(self, names: Iterable[str] | None = None) -> list[str]:
        """Replay the named projections (default: all) from the first event, in one transaction."""
        wanted = set(names) if names is not None else {p.name for p in self.projections}
        chosen = [p for p in self.projections if p.name in wanted]
        self.rebuilding = True
        try:
            with transaction(self.conn):
                self.replay_all(chosen)
        finally:
            self.rebuilding = False
        return [p.name for p in chosen]

    def stale(self) -> list[str]:
        """Projections whose code version or parameters differ from what built them."""
        out = []
        for projection in self.projections:
            state = self.state(projection.name)
            if state is None or state.code_version != projection.code_version or state.params_hash != self.params_hash:
                out.append(projection.name)
        return out

    def ensure_current(self) -> list[str]:
        stale = self.stale()
        return self.rebuild(stale) if stale else []

    def checksum(self, name: str | None = None) -> str:
        tables = [t for p in self.projections if name in (None, p.name) for t in p.tables]
        return hashlib.sha256("".join(table_checksum(self.conn, t) for t in tables).encode()).hexdigest()

    def median_time(self, kc_id: str) -> int | None:
        times = [
            row[0]
            for row in self.conn.execute(
                "SELECT time_ms FROM attempts, json_each(attempts.kc_ids) WHERE json_extract(value, '$.kc_id') = ?",
                (kc_id,),
            )
        ]
        return int(statistics.median(times)) if times else None


# --- the learner model as it stood at a moment (SPEC §21, JM-D37) -----------------------------------------------------

# The non-projection tables a fold reads besides `events` (the projections' own tables are rebuilt from it).
SNAPSHOT_COPIES = ("profile",)


def snapshot_as_of(
    conn: sqlite3.Connection,
    params: MasteryParams,
    content: ContentIndex | None,
    ts: int,
    projections: Sequence[Projection] | None = None,
) -> sqlite3.Connection:
    """The learner model as it stood at `ts`, replayed into memory and never written back (JM-D37): the generated
    learner schema in a `:memory:` database, the `events` that occurred at or before `ts` and the `profile` row copied
    in, then `Projector(mem, params, content).rebuild()`, which folds `self.conn`, so the memory connection is the
    one rebuilt. The live connection is only read: its tables' checksums are the same before and after. Snapshotting
    at the newest event gives the live projection tables (incremental and rebuilt tables are always equal)."""
    from agent_core.store.generated.schema import LEARNER
    from agent_core.store.sqlite import statements

    mem = sqlite3.connect(":memory:", isolation_level=None, check_same_thread=False)
    for statement in statements(LEARNER):
        mem.execute(statement)
    columns = [row[1] for row in conn.execute("PRAGMA table_info(events)")]
    names = ", ".join(columns)
    marks = ", ".join("?" for _ in columns)
    with transaction(mem):
        mem.executemany(
            f"INSERT INTO events ({names}) VALUES ({marks})",  # noqa: S608 - the table's own column names
            conn.execute(f"SELECT {names} FROM events WHERE occurred_at <= ? ORDER BY seq", (ts,)),  # noqa: S608
        )
        for table in SNAPSHOT_COPIES:
            copied = [row[1] for row in conn.execute(f"PRAGMA table_info({table})")]
            held = {row[1] for row in mem.execute(f"PRAGMA table_info({table})")}
            shared = ", ".join(c for c in copied if c in held)
            rows = conn.execute(f"SELECT {shared} FROM {table}").fetchall()  # noqa: S608 - fixed table names
            if rows:
                places = ", ".join("?" for _ in rows[0])
                mem.executemany(f"INSERT INTO {table} ({shared}) VALUES ({places})", rows)  # noqa: S608
    Projector(mem, params, content, projections).rebuild()
    return mem


@dataclass
class Snapshots:
    """`snapshot_as_of` cached per (profile, date) for the life of the core (the scrubber's session): a date redrawn
    twice replays once. The key holds the log's newest `seq`, so an event that arrives later (sync, a late fold)
    replays the date again instead of serving a stale model."""

    held: dict[tuple[str, str, int], sqlite3.Connection] = field(default_factory=dict)
    limit: int = 8
    builds: int = 0

    def get(
        self,
        profile_id: str,
        day: str,
        conn: sqlite3.Connection,
        build: Callable[[], sqlite3.Connection],
    ) -> sqlite3.Connection:
        newest = int(conn.execute("SELECT coalesce(max(seq), 0) FROM events").fetchone()[0])
        key = (profile_id, day, newest)
        found = self.held.get(key)
        if found is None:
            for stale in [k for k in self.held if k[:2] == key[:2]]:
                self.held.pop(stale).close()
            while len(self.held) >= self.limit:
                self.held.pop(next(iter(self.held))).close()
            found = self.held[key] = build()
            self.builds += 1
        return found
