"""Packaged curriculum vocabulary, independent of speech provider and learner identity."""

from importlib import resources

SUBJECTS = ("mathematics", "physics", "chemistry", "accounting")


def glossary_sections() -> dict[str, list[str]]:
    """The packaged keywords by subject (`voice_keywords.txt`: a `[subject]` line, then that subject's glossary
    terms, in glossary order)."""
    text = resources.files("cloud_gateway").joinpath("voice_keywords.txt").read_text(encoding="utf-8")
    sections: dict[str, list[str]] = {}
    current: list[str] | None = None
    for line in text.splitlines():
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        if line.startswith("[") and line.endswith("]"):
            current = sections.setdefault(line[1:-1], [])
        elif current is not None:
            current.append(line)
    return sections


def glossary_keywords(subject: str | None = None) -> list[str]:
    """The selected subject first, then remaining subjects; terms are unique without case sensitivity."""
    sections = glossary_sections()
    order = [subject] if subject in sections else []
    order += [s for s in SUBJECTS if s not in order]
    order += [s for s in sections if s not in order]
    words: dict[str, str] = {}
    for section in order:
        for word in sections.get(section, []):
            words.setdefault(word.casefold(), word)
    return list(words.values())
