"""Item analytics, item calibration and the internal review queue (CUR-26, ASM-21, AUT-37), in the authoring home.

The only inputs are evaluation runs and separately consented, de-identified research exports (D51, EVL-22). The intake
refuses anything else: a research export made without the `research_use` scope and the student's assent, a payload
carrying the student's words or an identifier the research projection drops, and a school-sync or telemetry payload
(no school analytics feed exists, D52). An accepted payload is reduced at once to per-item sums; its respondents exist
only while it is reduced, so no student row, conversation or identifier is stored (`analytics/items.json` keeps the
sums and the sha256 of each accepted payload, which is refused a second time).

From the sums, per record: facility, discrimination (point-biserial against the respondent's rest score), distractor
pick rates and the median time, each with its sample size and a 95% interval, and nothing but the sample size below
`author_analytics_min_students` students (AUT-37). From `item_analytics_min_responses` responses, an item crossing a
threshold of CUR-26 or ASM-21 enters the review queue (`analytics/queue.json`); below it, none does. A 1PL fit with a
normal prior at the author's rating (`difficulty.prior`, a logit) gives the calibrated difficulty, which replaces the
prior only from `calibration_min_responses` responses (ASM-21).

Nothing here edits a record. The queue and the calibration are read by authors; a confirmed wrong key or unsafe record
follows the withdrawal and correction path of CUR-36, and a calibrated difficulty reaches a pack only as an author's
registered edit (`apply_calibration`), so devices that never connect keep the difficulties they shipped with.

Pilot response volume does not exist yet: the tests seed synthetic responses.
"""

from __future__ import annotations

import bisect
import hashlib
import json
import math
import re
from collections.abc import Iterable, Mapping
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from agent_core.config import Settings
from agent_core.trace.research import item_responses, projected

from pack_builder.accounts import home, now_iso
from pack_builder.studio_io import atomic_write, locked

Z95 = 1.959963984540054
SOURCES = ("evaluation", "research_export")
RESEARCH_KEYS = frozenset({"kind", "consent", "events", "kc_mastery", "item_responses", "skill_sessions"})
EVALUATION_KEYS = frozenset({"kind", "run_id", "item_responses"})
RUN_ID = re.compile(r"[A-Za-z0-9_.:-]{8,80}")
RESPONDENT = re.compile(r"(sim|eval)[a-z0-9_.:-]{0,60}")  # a simulated student or eval case, never a person
SCORE = {"correct": 1.0, "partial": 0.5, "incorrect": 0.0}
# Response-time buckets: geometric edges from 1 s to about 2 h; the median is read from them (ASM-21's time rule).
TIME_EDGES = tuple(round(1000 * 1.25**k) for k in range(40))


class IntakeRefused(ValueError):
    """A payload that is not an evaluation run or a consented research export, or carries more than one may."""


# --- intake ----------------------------------------------------------------------------------------------------------


def _respondents(payload: Any) -> list[list[dict[str, Any]]]:
    """The payload's responses grouped by respondent, after every refusal of AUT-37."""
    if not isinstance(payload, dict):
        raise IntakeRefused("an analytics payload is a JSON object")
    kind = payload.get("kind")
    if kind not in SOURCES:
        raise IntakeRefused(f"{kind!r} is not an analytics source: evaluation runs and consented research exports "
                            "only (D51); there is no school analytics feed (D52)")  # fmt: skip
    if kind == "research_export":
        consent = payload.get("consent")
        if (
            not isinstance(consent, dict)
            or consent.get("scope") != "research_use"
            or consent.get("student_assent") is not True
            or not str(consent.get("protocol_ref") or "").strip()
        ):
            raise IntakeRefused("a research export needs the research_use scope, the student's assent and a protocol")
        if set(payload) - RESEARCH_KEYS:
            raise IntakeRefused("the research export carries fields the research projection does not")
        rows = {k: payload.get(k, []) for k in ("events", "kc_mastery", "item_responses", "skill_sessions")}
        if not projected(rows):  # the device's own allow-list: anything it would drop is student text or an id
            raise IntakeRefused("the research export carries student text or identifiers")
        return [list(rows["item_responses"])]
    if set(payload) - EVALUATION_KEYS or not RUN_ID.fullmatch(str(payload.get("run_id", ""))):
        raise IntakeRefused("an evaluation payload is {kind, run_id, item_responses}")
    grouped: dict[str, list[dict[str, Any]]] = {}
    for row in payload.get("item_responses") or []:
        if not isinstance(row, dict) or not RESPONDENT.fullmatch(str(row.get("respondent", ""))):
            raise IntakeRefused("each evaluation response names its simulated respondent")
        rest = {k: v for k, v in row.items() if k != "respondent"}
        if item_responses({"item_responses": [rest]}) != [rest]:
            raise IntakeRefused("an evaluation response carries text or fields outside the item response")
        grouped.setdefault(row["respondent"], []).append(rest)
    return list(grouped.values())


@dataclass
class Tally:
    """One item's sufficient statistics: sums only, never a response."""

    kc_id: str | None = None
    responses: int = 0
    students: int = 0
    score: float = 0.0  # correct 1, partial 0.5
    choices: dict[str, int] = field(default_factory=dict)
    times: list[int] = field(default_factory=lambda: [0] * (len(TIME_EDGES) + 1))
    # point-biserial against the respondent's rest score t: n, sum x, sum t, sum t^2, sum x t, sum x^2
    pb: list[float] = field(default_factory=lambda: [0.0] * 6)
    # the respondents' ability (a logit of their smoothed share correct): sum and sum of squares, for the 1PL fit
    theta: list[float] = field(default_factory=lambda: [0.0, 0.0])

    @staticmethod
    def of(data: Mapping[str, Any]) -> Tally:
        return Tally(data.get("kc_id"), int(data["responses"]), int(data["students"]), float(data["score"]),
                     dict(data["choices"]), list(data["times"]), list(data["pb"]), list(data["theta"]))  # fmt: skip


def pick(row: Mapping[str, Any]) -> str | None:
    """What a response says about the option chosen: the option itself when the attempt recorded it, else the
    misconception the marker matched (`mc:<id>`, which names a diagnostic item's distractor, ASM-07), else `key` for a
    right answer. `options` resolves these against the item."""
    if row.get("choice_id"):
        return str(row["choice_id"])
    if row.get("misconception_id"):
        return f"mc:{row['misconception_id']}"
    return "key" if row["outcome"] == "correct" else None


def options(record: Mapping[str, Any] | None, tally: Tally) -> dict[str, int]:
    """The picks per option id of the item; a pick no option of the item answers to is left out of every share."""
    if record is None:
        return {k: v for k, v in tally.choices.items() if k != "key" and not k.startswith("mc:")}
    correct, wrong = _options(record)
    by_misconception: dict[str, list[str]] = {}
    for part in record.get("parts") or []:
        for option in (part.get("answer") or {}).get("options") or []:
            if option.get("misconception_id"):
                by_misconception.setdefault(str(option["misconception_id"]), []).append(str(option["id"]))
    counts = {option: 0 for option in sorted(correct | wrong)}
    for chosen, n in tally.choices.items():
        if chosen == "key":
            found = sorted(correct) if len(correct) == 1 else []
        elif chosen.startswith("mc:"):
            found = by_misconception.get(chosen[3:], [])
        else:
            found = [chosen]
        if len(found) == 1 and found[0] in counts:
            counts[found[0]] += n
    return counts


def reduce(groups: Iterable[list[dict[str, Any]]], tallies: dict[str, Tally]) -> int:
    """Add each respondent's first response per item to the items' sums; returns the respondents counted."""
    counted = 0
    for rows in groups:
        first: dict[str, dict[str, Any]] = {}
        for row in rows:
            first.setdefault(row["item_id"], row)
        if not first:
            continue
        counted += 1
        scores = {item: SCORE[row["outcome"]] for item, row in first.items()}
        total, n = sum(scores.values()), len(scores)
        theta = math.log((total + 0.5) / (n - total + 0.5))
        for item, row in first.items():
            tally = tallies.setdefault(item, Tally())
            x = scores[item]
            tally.kc_id = tally.kc_id or row.get("kc_id")
            tally.responses += 1
            tally.students += 1
            tally.score += x
            token = pick(row)
            if token is not None:
                tally.choices[token] = tally.choices.get(token, 0) + 1
            tally.times[bisect.bisect_left(TIME_EDGES, row["time_ms"])] += 1
            tally.theta[0] += theta
            tally.theta[1] += theta * theta
            if n > 1:
                t = (total - x) / (n - 1)
                for i, value in enumerate((1.0, x, t, t * t, x * t, x * x)):
                    tally.pb[i] += value
    return counted


class Analytics:
    """The authoring home's analytics store: per-item sums, the accepted payloads' hashes and the review queue."""

    def __init__(self, root: Path | None = None) -> None:
        self.directory = (root or home()) / "analytics"
        self.items_path = self.directory / "items.json"
        self.queue_path = self.directory / "queue.json"

    def _read(self, path: Path, empty: dict[str, Any]) -> dict[str, Any]:
        return json.loads(path.read_text()) if path.is_file() else empty

    def tallies(self) -> dict[str, Tally]:
        data = self._read(self.items_path, {"items": {}})
        return {k: Tally.of(v) for k, v in data["items"].items()}

    def accepted(self) -> list[dict[str, Any]]:
        return list(self._read(self.items_path, {"accepted": []}).get("accepted", []))

    def queue(self) -> dict[str, dict[str, Any]]:
        return dict(self._read(self.queue_path, {"entries": {}})["entries"])

    def intake(self, payload: Any) -> dict[str, Any]:
        """Accept one evaluation run or consented research export (AUT-37), refusing it whole otherwise."""
        groups = _respondents(payload)
        digest = hashlib.sha256(json.dumps(payload, sort_keys=True).encode()).hexdigest()
        with locked(self.directory):
            accepted = self.accepted()
            if any(entry["sha256"] == digest for entry in accepted):
                raise IntakeRefused("this payload was already taken in")
            tallies = self.tallies()
            counted = reduce(groups, tallies)
            accepted.append({"sha256": digest, "kind": payload["kind"], "respondents": counted, "at": now_iso()})
            data = {"items": {k: vars(v) for k, v in sorted(tallies.items())}, "accepted": accepted}
            atomic_write(self.items_path, json.dumps(data, indent=1, sort_keys=True) + "\n")
        return {"kind": payload["kind"], "respondents": counted, "items": len({r["item_id"] for g in groups
                                                                                 for r in g})}  # fmt: skip

    def save_queue(self, entries: Mapping[str, Any]) -> None:
        atomic_write(self.queue_path, json.dumps({"entries": dict(sorted(entries.items()))}, indent=1,
                                                 sort_keys=True) + "\n")  # fmt: skip


# --- statistics (AUT-37, CUR-26) ------------------------------------------------------------------------------------


def wilson(successes: float, total: int, z: float = Z95) -> list[float]:
    if total == 0:
        return [0.0, 0.0]
    p = successes / total
    denominator = 1 + z * z / total
    centre = (p + z * z / (2 * total)) / denominator
    half = z * math.sqrt(max(p * (1 - p), 0.0) / total + z * z / (4 * total * total)) / denominator
    return [round(max(0.0, centre - half), 6), round(min(1.0, centre + half), 6)]


def point_biserial(tally: Tally) -> float | None:
    n, sx, st, stt, sxt, sxx = tally.pb
    if n < 3:
        return None
    vx, vt = sxx / n - (sx / n) ** 2, stt / n - (st / n) ** 2
    if vx <= 0 or vt <= 0:
        return None
    return (sxt / n - (sx / n) * (st / n)) / math.sqrt(vx * vt)


def correlation_interval(r: float, n: float) -> list[float]:
    """The Fisher z 95% interval of a correlation from n pairs."""
    if n <= 3:
        return [-1.0, 1.0]
    z = math.atanh(max(min(r, 0.999999), -0.999999))
    half = Z95 / math.sqrt(n - 3)
    return [round(math.tanh(z - half), 6), round(math.tanh(z + half), 6)]


def median_ms(counts: list[int]) -> int | None:
    total = sum(counts)
    if total == 0:
        return None
    seen = 0
    for index, count in enumerate(counts):
        seen += count
        if 2 * seen >= total:
            low = TIME_EDGES[index - 1] if index > 0 else 0
            high = TIME_EDGES[index] if index < len(TIME_EDGES) else TIME_EDGES[-1] * 1.25
            return round(math.sqrt(max(low, 1) * high))  # the bucket's geometric centre
    return None


def statistics(record_id: str, tally: Tally, settings: Settings | None = None,
               record: Mapping[str, Any] | None = None) -> dict[str, Any]:  # fmt: skip
    """What authors see for one record (AUT-37): sample size always; figures with 95% intervals only from
    `author_analytics_min_students` students."""
    settings = settings or Settings()
    suppressed = tally.students < settings.author_analytics_min_students
    out: dict[str, Any] = {"record_id": record_id, "responses": tally.responses, "students": tally.students,
                           "suppressed": suppressed}  # fmt: skip
    if suppressed:
        return out | {"facility": None, "discrimination": None, "distractors": None, "median_time_ms": None}
    r = point_biserial(tally)
    picks = options(record, tally)
    picked = sum(picks.values())
    out["facility"] = {
        "value": round(tally.score / tally.responses, 6),
        "interval": wilson(tally.score, tally.responses),
    }
    out["discrimination"] = (
        None if r is None else {"value": round(r, 6), "interval": correlation_interval(r, tally.pb[0])}
    )
    out["distractors"] = {k: {"share": round(v / picked, 6), "interval": wilson(v, picked)}
                          for k, v in sorted(picks.items())} if picked else {}  # fmt: skip
    out["median_time_ms"] = median_ms(tally.times)
    return out


def _options(record: Mapping[str, Any]) -> tuple[set[str], set[str]]:
    """An item's (correct, distractor) option ids over its choice parts."""
    correct: set[str] = set()
    wrong: set[str] = set()
    for part in record.get("parts") or []:
        for option in (part.get("answer") or {}).get("options") or []:
            (correct if option.get("correct") else wrong).add(str(option["id"]))
    return correct, wrong


def kc_medians(tallies: Mapping[str, Tally]) -> dict[str, int | None]:
    merged: dict[str, list[int]] = {}
    for tally in tallies.values():
        if tally.kc_id:
            into = merged.setdefault(tally.kc_id, [0] * (len(TIME_EDGES) + 1))
            for i, count in enumerate(tally.times):
                into[i] += count
    return {kc: median_ms(counts) for kc, counts in merged.items()}


def flags(
    record: Mapping[str, Any], tally: Tally, kc_median: int | None, settings: Settings | None = None
) -> list[str]:
    """The thresholds of CUR-26 and ASM-21 an item crosses; none below `item_analytics_min_responses` responses."""
    settings = settings or Settings()
    if tally.responses < settings.item_analytics_min_responses:
        return []
    found = []
    facility = tally.score / tally.responses
    if facility < settings.item_facility_low:
        found.append("facility_low")
    if facility > settings.item_facility_high:
        found.append("facility_high")
    r = point_biserial(tally)
    if r is not None and r < settings.item_discrimination_min:
        found.append("discrimination_low")
    _, distractors = _options(record)
    picks = options(record, tally)
    picked = sum(picks.values())
    for option in sorted(distractors) if picked else []:
        share = picks.get(option, 0) / picked
        if share == 0:
            found.append(f"distractor_unused:{option}")
        elif share < settings.item_distractor_min_share:
            found.append(f"distractor_rare:{option}")
    median = median_ms(tally.times)
    if median is not None and kc_median and median > settings.item_time_kc_multiple * kc_median:
        found.append("slow_median_time")
    return found


# --- calibration (ASM-21) ------------------------------------------------------------------------------------------


def calibrate(tally: Tally, prior: float, settings: Settings | None = None) -> dict[str, Any]:
    """The 1PL difficulty (logit; higher is harder) from the sums: the normal-approximation estimate from the
    respondents' mean ability and the item's failure log-odds (PROX), shrunk toward the author's prior by a normal
    prior of `calibration_prior_sd`. It replaces the prior only from `calibration_min_responses` responses."""
    settings = settings or Settings()
    n = tally.responses
    if n == 0:
        return {"prior": prior, "estimate": None, "difficulty": prior, "replaced": False, "responses": 0}
    right = tally.score
    mean = tally.theta[0] / n
    spread = max(tally.theta[1] / n - mean * mean, 0.0)
    expansion = math.sqrt(1 + spread / 2.89)
    fitted = mean + expansion * math.log((n - right + 0.5) / (right + 0.5))
    p = (right + 0.5) / (n + 1)
    precision = n * p * (1 - p) / (expansion * expansion)
    weight = 1 / settings.calibration_prior_sd**2
    estimate = max(-4.0, min(4.0, (prior * weight + fitted * precision) / (weight + precision)))
    replaced = n >= settings.calibration_min_responses
    return {"prior": prior, "estimate": round(estimate, 3), "difficulty": round(estimate, 3) if replaced else prior,
            "replaced": replaced, "responses": n}  # fmt: skip


# --- the review queue ------------------------------------------------------------------------------------------------


def refresh_queue(store: Analytics, records: Mapping[str, Mapping[str, Any]],
                  settings: Settings | None = None) -> dict[str, dict[str, Any]]:  # fmt: skip
    """Queue every item whose sums cross a threshold, or update its open entry; a resolved entry reopens only when a
    new reason appears. The records are read for their options and prior, never written."""
    settings = settings or Settings()
    tallies = store.tallies()
    medians = kc_medians(tallies)
    entries = store.queue()
    for record_id, tally in sorted(tallies.items()):
        record = records.get(record_id)
        if record is None:
            continue
        reasons = flags(record, tally, medians.get(tally.kc_id or ""), settings)
        if not reasons:
            continue
        prior = float(((record.get("difficulty") or {}).get("prior")) or 0.0)
        entry = entries.get(record_id)
        if entry is not None and entry["status"] != "open" and set(reasons) <= set(entry["reasons"]):
            continue
        entries[record_id] = {
            "record_id": record_id,
            "version": record.get("version"),
            "reasons": reasons,
            "status": "open",
            "queued_at": entry["queued_at"] if entry and entry["status"] == "open" else now_iso(),
            "statistics": statistics(record_id, tally, settings, record),
            "calibration": calibrate(tally, prior, settings),
        }
    store.save_queue(entries)
    return entries


OUTCOMES = {
    "dismissed": "No change: the statistics do not show a content problem.",
    "needs_edit": "Open a draft change on the record; it passes the pipeline gates like any other change.",
    "wrong_key": "Withdraw the item through the signed safety configuration, retire its id and publish a corrected "
    "item naming it in `corrects` in a pack patch (CUR-36).",
    "unsafe": "Withdraw the record through the signed safety configuration, retire its id and publish a correction "
    "in a pack patch (CUR-36).",
}


def resolve(store: Analytics, record_id: str, outcome: str, reviewer: str) -> dict[str, Any]:
    """Close a queue entry with what the review found. Nothing about the record changes here."""
    if outcome not in OUTCOMES:
        raise ValueError(f"choose {', '.join(OUTCOMES)}")
    with locked(store.directory):
        entries = store.queue()
        if record_id not in entries:
            raise KeyError("That record is not in the review queue.")
        entries[record_id] = {**entries[record_id], "status": outcome, "resolved_by": reviewer,
                              "resolved_at": now_iso()}  # fmt: skip
        store.save_queue(entries)
    return {**entries[record_id], "next": OUTCOMES[outcome]}
