"""Topic classification: retrieval proposes, the Diagnoser chooses (EVL-13, MEM-21).

The pack search index ranks the installed packs' topics for a question (`KnowledgeIndex.topic_scores`); the best
`classify_topic_candidates` become the numbered lists the Diagnoser is given: the topics, and under them the KCs it may
tag. The Diagnoser's `topic_ref` (or, when it gives none, the topic of its first `kc_ref`) names the topic. The gate
compares that topic with the bank's label, top-1 (TABLE EVL-17a).

The question is wrapped and escaped as untrusted material (PRM-19); the lists are pack content.
"""

from __future__ import annotations

from collections.abc import Collection, Iterable, Mapping, Sequence
from dataclasses import dataclass
from typing import Any

from agent_core.config import Settings
from agent_core.retrieval.index import SUBJECT_NAMES, KnowledgeIndex


@dataclass(frozen=True)
class TopicCandidate:
    ref: int  # the topic's number in the list the Diagnoser is given
    topic_id: str
    title: str
    subject: str  # the subject's name (`physics`)
    kcs: tuple[tuple[int, str, str], ...]  # (number in the KC list, KC id, statement)


def topic_candidates(index: KnowledgeIndex, question: str, settings: Settings,
                     subjects: Collection[str] | None = None) -> list[TopicCandidate]:  # fmt: skip
    """The question's best topics, numbered from 1, each with its KCs numbered across the whole list."""
    out = []
    number = 0
    for ref, (topic_id, _) in enumerate(
        index.topic_scores(question, subjects=subjects, limit=settings.classify_topic_candidates), start=1
    ):
        node = index.nodes.get(topic_id)
        kcs = []
        for kc_id in index.kcs_in_topic(topic_id):
            number += 1
            kcs.append((number, kc_id, index.kcs[kc_id].statement))
        subject = SUBJECT_NAMES.get(topic_id.split(".")[1], topic_id.split(".")[1])
        out.append(TopicCandidate(ref, topic_id, node.title if node else topic_id, subject, tuple(kcs)))
    return out


def _escape(text: str) -> str:
    return text.replace("<", "&lt;").replace(">", "&gt;")


def diagnoser_task(question: str, candidates: Sequence[TopicCandidate]) -> str:
    """The Diagnoser's task: the wrapped question, then the topic list and the KC list it chooses from by number."""
    topics = "\n".join(f"{c.ref}. {_escape(c.title)} ({c.subject})" for c in candidates)
    kcs = "\n".join(f"{n}. {_escape(statement)} (topic {c.ref})" for c in candidates for n, _, statement in c.kcs)
    return (f"<student-message>\n{_escape(question)}\n</student-message>\n\nTopics:\n{topics or '(none)'}\n\n"
            f"Knowledge components:\n{kcs or '(none)'}")  # fmt: skip


def topic_of(diagnosis: Mapping[str, Any], candidates: Sequence[TopicCandidate]) -> str | None:
    """The topic a diagnosis names: its `topic_ref`, else the topic of its first `kc_ref`; None when neither is a
    number in the lists."""
    ref = diagnosis.get("topic_ref")
    if type(ref) is int and 1 <= ref <= len(candidates):
        return candidates[ref - 1].topic_id
    for kc_ref in diagnosis.get("kc_refs") or []:
        for c in candidates:
            if any(n == kc_ref for n, _, _ in c.kcs):
                return c.topic_id
    return None


@dataclass(frozen=True)
class Case:
    id: str
    label: str  # the bank's topic id
    retrieved: tuple[str, ...]  # the candidate topics, best first
    chosen: str | None  # the Diagnoser's topic (None when it gave none, or no model ran)

    @property
    def correct(self) -> bool:
        return self.chosen == self.label


def summary(cases: Iterable[Case]) -> dict[str, Any]:
    """Top-1 topic accuracy (the gate), with retrieval's own top-1 and its recall within the candidate list."""
    found = list(cases)
    total = len(found)
    if not total:
        return {"total": 0, "top1": 0.0, "retrieval_top1": 0.0, "retrieval_recall": 0.0, "by_subject": {}}
    by_subject: dict[str, dict[str, int]] = {}
    for c in found:
        entry = by_subject.setdefault(c.label.split(".")[1], {"total": 0, "correct": 0})
        entry["total"] += 1
        entry["correct"] += int(c.correct)
    return {
        "total": total,
        "correct": sum(c.correct for c in found),
        "top1": sum(c.correct for c in found) / total,
        "retrieval_top1": sum(bool(c.retrieved) and c.retrieved[0] == c.label for c in found) / total,
        "retrieval_recall": sum(c.label in c.retrieved for c in found) / total,
        "by_subject": by_subject,
    }
