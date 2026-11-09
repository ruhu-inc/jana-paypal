"""The adaptation policies wired to the learner model (LRN-15 to LRN-27): what the engine reads, which pure policy it
asks, and the decision it logs.

The policies themselves are pure (`agent_core.learner.policies`); this module only gathers their inputs from the open
learner database at a moment and hands back the `Decision`, which the caller logs as a `policy_decision` in the turn
trace (OBS-01) before acting on it. The model never sees a probability and never chooses: it words the result.
"""

from __future__ import annotations

import json
import sqlite3
from collections.abc import Callable, Mapping, Sequence
from typing import Any

from agent_core.config import Settings
from agent_core.learner import model, policies
from agent_core.learner.mastery import Ability, KcEvidence, MasteryParams, item_probability, kc_probability, subject_of


def start_rung(
    conn: sqlite3.Connection, params: MasteryParams, kc_ids: Sequence[str], settings: Settings, now: int
) -> policies.Decision:
    """LRN-18 for the KCs in play: the weakest one's P_k and its recent hint dependence (the highest rung of each of
    its last closed opportunities)."""
    views = model.kc_views(conn, params, now)
    known = [views[k] for k in kc_ids if k in views]
    weakest = min(known, key=lambda v: (v.p_mastery, v.kc_id)) if known else None
    p_k = weakest.p_mastery if weakest else 0.0  # nothing practised yet: the concept rung first
    rungs: list[int] = []
    if weakest is not None:
        rows = conn.execute(  # one row per opportunity (a session's step), not per attempt
            "SELECT max(coalesce(max_rung, 0)) FROM attempts, json_each(attempts.kc_ids)"
            " WHERE json_extract(value, '$.kc_id') = ? GROUP BY session_id, item_ref, step_id"
            " ORDER BY max(occurred_at) DESC LIMIT ?",
            (weakest.kc_id, settings.hint_dependence_window),
        ).fetchall()
        rungs = [int(r[0] or 0) for r in reversed(rows)]
    return policies.hint_start_rung(p_k, rungs, settings)


def practice_selection(
    conn: sqlite3.Connection,
    params: MasteryParams,
    candidates: Sequence[Mapping[str, Any]],
    size: int,
    settings: Settings,
    now: int,
    *,
    difficulty: Any = None,
    support_extra: bool = False,
) -> tuple[list[Mapping[str, Any]], list[policies.Decision]]:
    """A practice set from eligible items (ASM-22 already applied): items on a parked KC (the stuck cap's deferral,
    LRN-26) are not served, then each next item is the one whose predicted success is nearest the target (LRN-16).
    Returns the items and every decision, for the caller to log."""
    views = model.kc_views(conn, params, now)
    decisions: list[policies.Decision] = []
    pool = []
    for item in candidates:
        kcs = [str(k) for k in item.get("kc_ids", [])]
        parked = [k for k in kcs if k in views and views[k].state == "moved_on"]
        if parked:
            view = views[parked[0]]
            decisions.append(policies.stuck_cap(stuck_count=view.stuck_count, stuck_sessions=0, p_k=view.p_mastery,
                                                revisit_at=view.revisit_at, now=now, s=settings))  # fmt: skip
            continue
        pool.append(item)
    abilities = {r[0]: Ability(r[1], r[2]) for r in conn.execute("SELECT subject, theta, n FROM subject_ability")}
    evidence = {
        r[0]: KcEvidence(r[1], r[2], r[3] or 0.0)
        for r in conn.execute("SELECT kc_id, succ_w, fail_w, implied_w FROM kc_mastery")
    }
    row = conn.execute("SELECT frustration FROM engagement_state WHERE id = 1").fetchone()
    frustration = float(row[0]) if row else 0.0
    exposures = _exposures(conn)
    chosen: list[Mapping[str, Any]] = []
    order = {str(item["id"]): i for i, item in enumerate(pool)}
    while pool and len(chosen) < size:
        options = []
        for item in pool:
            kcs = [str(k) for k in item.get("kc_ids", [])] or ["kc.unknown.o.none.none"]
            weights = {k: 1 / len(kcs) for k in kcs}
            subject = _subject(kcs[0])
            b_item = difficulty(str(item["id"])) if difficulty is not None else 0.0
            p_hat = item_probability(abilities.get(subject, Ability()).theta, weights,
                                     {k: evidence.get(k, KcEvidence()) for k in kcs}, params, b_item)  # fmt: skip
            options.append(policies.Candidate(str(item["id"]), p_hat, exposures.get(str(item["id"]), 0),
                                              order[str(item["id"])]))  # fmt: skip
        decision = policies.difficulty_targeting(options, settings, support_extra=support_extra,
                                                 frustration=frustration)  # fmt: skip
        decisions.append(decision)
        picked = decision.output["item_id"]
        chosen.append(next(i for i in pool if str(i["id"]) == picked))
        pool = [i for i in pool if str(i["id"]) != picked]
    return chosen, decisions


def _subject(kc_id: str) -> str:
    from agent_core.learner.mastery import SUBJECTS

    return SUBJECTS.get(kc_id.split(".")[1], "") if kc_id.count(".") >= 2 else ""


# --- LRN-20, LRN-23: what the engine's own offers read ------------------------------------------------------------


def unprompted_help_on(conn: sqlite3.Connection) -> bool:
    """The student's `preferences.unprompted_help` (LRN-20); on until the student switches it off."""
    row = conn.execute("SELECT value FROM preferences WHERE key = 'unprompted_help'").fetchone()
    return True if row is None else json.loads(row[0]) is not False


class LearnerDetours:
    """What a prerequisite detour reads (LRN-23, `engine.offers.Detours`): each prerequisite's P_j from the mastery
    projection (a KC with no evidence yet at its prior from the subject's ability), the published single-part items on
    a KC, easiest first, and a KC's statement, from the installed packs."""

    def __init__(self, conn: sqlite3.Connection, params: MasteryParams, content: Any, now: Callable[[], int]) -> None:
        self.conn, self.params, self.content, self.now = conn, params, content, now

    def prerequisites(self, kc_ids: Sequence[str]) -> dict[str, dict[str, float]]:
        views = model.kc_views(self.conn, self.params, self.now())
        theta = {r[0]: float(r[1]) for r in self.conn.execute("SELECT subject, theta FROM subject_ability")}

        def p(kc: str) -> float:
            view = views.get(kc)
            if view is not None:
                return float(view.p_mastery)
            return kc_probability(theta.get(subject_of(kc), 0.0), KcEvidence(), self.params,
                                  self.content.reference_difficulty(kc), kc)  # fmt: skip

        out: dict[str, dict[str, float]] = {}
        for kc in kc_ids:
            record = self.content.kcs.get(kc)
            if record is not None:
                out[kc] = {j: p(j) for j in record.prerequisites if j in self.content.kcs}
        return out

    def items(self, kc_id: str, count: int) -> list[dict[str, Any]]:
        found = [i for i in self.content.table("items") if kc_id in (i.get("kc_ids") or [])
                 and (i.get("review") or {}).get("status") == "published" and len(i.get("parts") or []) == 1
                 and i.get("type") != "mcq_diagnostic"]  # fmt: skip
        found.sort(key=lambda i: (self.content.item_difficulty(str(i["id"])), str(i["id"])))
        return [dict(i) for i in found[:count]]

    def statement(self, kc_id: str) -> str | None:
        found = self.content.kc_statement(kc_id)
        return None if found is None else str(found)


def _exposures(conn: sqlite3.Connection) -> dict[str, int]:
    out: dict[str, int] = {}
    for (payload,) in conn.execute("SELECT payload FROM events WHERE type = 'item_exposed' AND erased_at IS NULL"):
        item_id = json.loads(payload).get("item_id")
        if isinstance(item_id, str):
            out[item_id] = out.get(item_id, 0) + 1
    return out
