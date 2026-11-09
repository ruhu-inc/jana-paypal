"""Native field names identify checked phases; all generated content still passes the public reply gates."""

from typing import Any


def project(schema: dict[str, Any], kind: str | None) -> dict[str, Any]:
    if kind not in ("operation_question", "repair_question", "confirmation"):
        return schema
    props = dict(schema["properties"])
    say = props.pop("say")
    props[kind] = {**say, "minLength": max(2, say.get("minLength", 0))}
    if kind == "repair_question":
        props[kind]["pattern"] = r"\?\s*$"
        props[kind]["description"] = (
            "One question addressed directly to the learner about repairing the marked relation."
        )
    elif kind == "confirmation":
        props[kind]["description"] = (
            "Your own words to the learner acknowledging their checked answer is complete. "
            "A next-question offer is optional. This is the spoken reply, not instructions about writing a reply."
        )
    props.pop("expects", None)
    return {**schema, "properties": props, "required": [kind]}


def complete(value: dict[str, Any], kind: str | None) -> dict[str, Any]:
    if kind not in ("operation_question", "repair_question", "confirmation") or kind not in value or "say" in value:
        return value
    value = dict(value)
    value["say"] = value.pop(kind)
    value.setdefault("expects", "student_choice" if kind == "confirmation" else "student_answer")
    return value
