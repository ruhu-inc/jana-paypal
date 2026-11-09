"""Mock papers in a pack (ASM-15, CUR-31): what the build checks beyond the `MockPaper` contract.

A mock paper is assembled from the pack's own items by a board-neutral blueprint. The build checks that it belongs to
the pack's subject and level; that every question names an item of the pack, once per paper, of a type its section
allows, with the item's KCs and the item's marks; that section marks and the paper total add up; that the blueprint's
topic weights are topics of the map summing to one; that the paper's marks per topic match the blueprint within one
item (ASM-15); and that a published paper uses published items only.

A predicted band is shown only in the exam board's own grade letters, set per pack (ASM-19, D27): a paper's
`grade_config` names one more letter than it has boundaries, its boundaries fall strictly, its letters are distinct,
and every paper of a pack carries the same scale or none does. A pack without one shows no band.
"""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any


def _topic_of(node_id: str | None, nodes: Mapping[str, Mapping[str, Any]]) -> str | None:
    seen: set[str] = set()
    while node_id is not None and node_id not in seen:
        node = nodes.get(node_id)
        if node is None:
            return None
        if node["kind"] == "topic":
            return node_id
        seen.add(node_id)
        node_id = node.get("parent_id")
    return None


def item_marks(item: Mapping[str, Any]) -> int:
    return sum(int(p["marks"]) for p in item["parts"])


def section_problems(pid: str, section: Mapping[str, Any]) -> list[str]:
    where = f"{pid} section {section['section_id']}"
    marks = [int(q["marks"]) for q in section["questions"]]
    choose = section.get("choose")
    if choose is None:
        return [] if sum(marks) == section["marks"] else [
            f"{where}: marks {section['marks']} are not the sum of its questions ({sum(marks)})"]  # fmt: skip
    if choose > len(marks):
        return [f"{where}: choose {choose} of {len(marks)} questions"]
    if len(set(marks)) != 1 or marks[0] * choose != section["marks"]:
        return [f"{where}: with choose {choose}, every question carries {section['marks']} / {choose} marks"]
    return []


def grade_problems(papers: list[dict[str, Any]]) -> list[str]:
    """The board's grade letters (D27): one consistent scale for the whole pack, or none."""
    problems: list[str] = []
    scales = []
    for paper in papers:
        config = paper.get("grade_config")
        scales.append(config)
        if not config:
            continue
        pid, boundaries, labels = paper["id"], list(config["boundaries"]), list(config["labels"])
        if len(labels) != len(boundaries) + 1:
            problems.append(f"{pid}: {len(labels)} grade letters for {len(boundaries)} boundaries (one more needed)")
        if any(b <= a for a, b in zip(boundaries[1:], boundaries, strict=False)):
            problems.append(f"{pid}: grade boundaries must fall strictly from the top letter down")
        if any(not str(label).strip() for label in labels) or len(set(labels)) != len(labels):
            problems.append(f"{pid}: grade letters must be distinct and not blank")
    if any(scales) and any(scale != scales[0] for scale in scales):
        problems.append("the pack's mock papers carry different grade scales or only some carry one: set the board's "
                        "letters once for the whole pack (D27)")  # fmt: skip
    return problems


def paper_problems(
    papers: list[dict[str, Any]],
    pack: Mapping[str, Any],
    items: list[dict[str, Any]],
    nodes: list[dict[str, Any]],
    kcs: list[dict[str, Any]],
    weight_tolerance: float,
) -> list[str]:
    by_item = {i["id"]: i for i in items}
    by_node = {n["id"]: n for n in nodes}
    topics = {n["id"] for n in nodes if n["kind"] == "topic" and n.get("status", "active") == "active"}
    kc_topic = {kc["id"]: _topic_of(kc["node_id"], by_node) for kc in kcs}
    problems: list[str] = []
    for paper in papers:
        pid = paper["id"]
        if (paper["subject"], paper["level"]) != (pack.get("subject"), pack.get("level")):
            problems.append(f"{pid}: a {paper['subject']}.{paper['level']} paper in pack {pack['pack_id']}")
        seen_items: set[str] = set()
        seen_questions: set[str] = set()
        section_ids = [s["section_id"] for s in paper["sections"]]
        if len(set(section_ids)) != len(section_ids):
            problems.append(f"{pid}: section ids repeat")
        on_topic: dict[str, int] = {}
        largest = 0
        free_choice = False
        for section in paper["sections"]:
            problems += section_problems(pid, section)
            free_choice = free_choice or section.get("choose") is not None
            for question in section["questions"]:
                where = f"{pid} question {question['question_id']}"
                if question["question_id"] in seen_questions:
                    problems.append(f"{where}: question id repeats")
                seen_questions.add(question["question_id"])
                item = by_item.get(question["item_id"])
                if item is None:
                    problems.append(f"{where}: item {question['item_id']} is not in the pack")
                    continue
                if item["id"] in seen_items:
                    problems.append(f"{where}: item {item['id']} is on the paper twice")
                seen_items.add(item["id"])
                if item["type"] not in section["item_types"]:
                    allowed = ", ".join(section["item_types"])
                    problems.append(f"{where}: a {item['type']} item in a section of {allowed}")
                if question["marks"] != item_marks(item):
                    problems.append(f"{where}: {question['marks']} marks, the item carries {item_marks(item)}")
                stray = sorted(set(question["kc_ids"]) - set(item["kc_ids"]))
                if stray:
                    problems.append(f"{where}: KCs {', '.join(stray)} are not the item's")
                if paper["review"]["status"] == "published" and item["review"]["status"] != "published":
                    problems.append(f"{where}: a published paper uses item {item['id']}, which is not published")
                largest = max(largest, int(question["marks"]))
                question_topics = sorted({t for k in question["kc_ids"] if (t := kc_topic.get(k))})
                for topic in question_topics:  # a question across topics shares its marks between them
                    on_topic[topic] = on_topic.get(topic, 0) + int(question["marks"]) // len(question_topics)
        total = sum(int(s["marks"]) for s in paper["sections"])
        if total != paper["total_marks"]:
            problems.append(f"{pid}: total marks {paper['total_marks']} are not the sum of its sections ({total})")
        weights = paper["blueprint"]["topic_weights"]
        unknown = sorted(set(weights) - topics)
        if unknown:
            problems.append(f"{pid}: blueprint topics {', '.join(unknown)} are not topics of the map")
        if abs(sum(weights.values()) - 1) > weight_tolerance:
            problems.append(f"{pid}: blueprint topic weights sum to {sum(weights.values()):.3f}, not 1")
        if free_choice:  # the marks a student sits depend on the choice made: no fixed share per topic
            continue
        for topic in sorted(set(weights) | set(on_topic)):
            planned = float(weights.get(topic, 0)) * paper["total_marks"]
            if abs(on_topic.get(topic, 0) - planned) > largest:
                problems.append(f"{pid}: {on_topic.get(topic, 0)} marks on {topic}, the blueprint plans "
                                f"{planned:.1f} (within one item, ASM-15)")  # fmt: skip
    return problems + grade_problems(papers)
