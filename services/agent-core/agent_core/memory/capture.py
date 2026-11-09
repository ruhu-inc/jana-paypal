"""The do-not-capture filter (MEM-10, PRV-04): every candidate note, for every profile, adults included (G98).

A lexicon with an allowlist catches the clear cases; a small naive-Bayes classifier scores the rest against
`capture_classifier_threshold`. The lexicon, the allowlist, the known near misses, the English vocabulary and the
classifier's labelled examples live in `content/agent/memory/capture.en.yaml` (own wording); the classifier's word
counts are taken from the examples when the file is loaded, so the shipped file is the whole model and two loads
always agree. A blocked candidate is counted by category and never stored.

Every rule reads the note in one shaped form (`shape`): compatibility forms folded (full-width letters, ligatures),
letters of other scripts that pass for Latin ones mapped to the Latin letter, accents and other combining marks
removed, case ignored, and hyphens, dashes, slashes and a possessive 's read as word breaks. So a spelling meant to
slip past the list ("mum's", "HIV-positive", full-width letters, an accented or Cyrillic "a") reads as the listed
word.

The checks run in a fixed order and the first match names the category: quotes and distress first (a quote or a
distress signal outranks everything else it might also be), then the rest of the list in order; then near misses (a
word of `capture_fuzzy_min_letters` or more one edit away from a listed word, unless it is a known English near miss
such as "rather" for "father"); then the script rule (a letter of a script other than Latin in a word, except Greek
letters used as symbols; a Greek look-alike inside a Latin word); then the language rule (fewer than
`capture_english_min_share` of the words read as English: the lexicon is English only, so such a note is blocked
until its language has a lexicon, MEM-25); then the classifier. Three rules are shared with the write policy rather
than restated: quotation marks (quotes), instruction shape and secret-like text (instructions and secrets).
Mid-sentence capitalised words that are not curriculum names are read as names of people or places. The filter
never logs or returns the text it checked.
"""

from __future__ import annotations

import functools
import math
import re
import unicodedata
from collections import Counter
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass, field
from itertools import pairwise
from pathlib import Path
from typing import Any, Literal, cast

import yaml

from agent_core.config import Settings
from agent_core.memory import policy

# The categories of the never-captured list, in its order.
CATEGORIES = (
    "home_and_family",
    "health",
    "beliefs_and_identity",
    "location_and_contacts",
    "quotes_and_distress",
    "guesses_and_moods",
    "other_students",
    "instructions_and_secrets",
)
Category = Literal[
    "home_and_family",
    "health",
    "beliefs_and_identity",
    "location_and_contacts",
    "quotes_and_distress",
    "guesses_and_moods",
    "other_students",
    "instructions_and_secrets",
]
# The order the lexicon is tried in: the first category that matches names the block.
CHECK_ORDER: tuple[Category, ...] = (
    "quotes_and_distress",
    "home_and_family",
    "health",
    "beliefs_and_identity",
    "location_and_contacts",
    "guesses_and_moods",
    "other_students",
    "instructions_and_secrets",
)
BENIGN = "benign"
# The capture file's path below the agent bundle root (`AppRuntime`'s content root).
CAPTURE_FILE = Path("memory") / "capture.en.yaml"

# Words too common to carry a category on their own; the classifier ignores them.
_STOP = frozenset(
    "a an the and or but of to in on at for with by from as is are was were be been being it its this that these "
    "those they them their he she his her him who which what when where how than then so too very".split()
)
_SENTENCE_END = re.compile(r"[.!?;:]\s+$")
_CAPITALISED = re.compile(r"(?<![\w'-])([A-Z][a-z]{2,})(?![\w-])")
_BOUNDARY_BEFORE, _BOUNDARY_AFTER = r"(?<![\w'])", r"(?!\w)"

# Letters of other scripts that a reader takes for Latin ones (after compatibility folding). Mapped to the Latin
# letter before any rule runs, so a look-alike spelling of a listed word still matches it.
_LOOKALIKES = {
    # Cyrillic
    "\u0430": "a", "\u0432": "b", "\u0435": "e", "\u0451": "e", "\u043a": "k", "\u043c": "m", "\u043d": "h",
    "\u043e": "o", "\u0440": "p", "\u0441": "c", "\u0442": "t", "\u0443": "y", "\u0445": "x", "\u0455": "s",
    "\u0456": "i", "\u0457": "i", "\u0458": "j", "\u0501": "d", "\u051b": "q", "\u051d": "w", "\u04cf": "l",
    "\u04bb": "h", "\u0410": "A", "\u0412": "B", "\u0415": "E", "\u041a": "K", "\u041c": "M", "\u041d": "H",
    "\u041e": "O", "\u0420": "P", "\u0421": "C", "\u0422": "T", "\u0423": "Y", "\u0425": "X", "\u0405": "S",
    "\u0406": "I", "\u0408": "J", "\u0500": "D",
    # Greek
    "\u03b1": "a", "\u03bf": "o", "\u03bd": "v", "\u03c1": "p", "\u03c4": "t", "\u03b9": "i", "\u03ba": "k",
    "\u03c5": "u", "\u03f2": "c", "\u0391": "A", "\u0392": "B", "\u0395": "E", "\u0396": "Z", "\u0397": "H",
    "\u0399": "I", "\u039a": "K", "\u039c": "M", "\u039d": "N", "\u039f": "O", "\u03a1": "P", "\u03a4": "T",
    "\u03a5": "Y", "\u03a7": "X",
    # Latin letters with a stroke or hook, which have no decomposition
    "\u0261": "g", "\u0127": "h", "\u0142": "l", "\u00f8": "o", "\u0111": "d", "\u0180": "b", "\u0268": "i",
    "\u0289": "u", "\u0131": "i", "\u0237": "j",
}  # fmt: skip
_TO_LATIN = str.maketrans(_LOOKALIKES)
_GREEK_LOOKALIKES = frozenset(c for c in _LOOKALIKES if "GREEK" in unicodedata.name(c, ""))
_APOSTROPHES = str.maketrans({"\u2019": "'", "\u2018": "'", "\u02bc": "'", "\uff07": "'", "`": "'"})
_POSSESSIVE = re.compile(r"(?<=\w)'s\b", re.IGNORECASE)
_BREAKS = re.compile(r"[-\u2010-\u2015\u2212\u2043_/\\]+")
_LETTERS = re.compile(r"[^\W\d_]+", re.UNICODE)
_ENDINGS = ("ies", "ied", "es", "s", "ed", "ing", "ly", "er", "ers", "est", "d")
# Word endings that mark a long word as English (learning notes name many technical terms no word list holds).
_ENGLISH_SUFFIXES = (
    "tion", "tions", "sion", "sions", "ment", "ments", "ness", "ity", "ities", "ance", "ence", "ances", "ences",
    "ical", "ically", "ally", "ise", "ize", "ised", "ized", "ises", "izes", "ising", "izing", "isation",
    "ization", "ative", "able", "ible", "ous", "ful", "less", "ship", "ology", "ometry", "graph", "graphs", "ism",
    "ist", "ists", "ive", "ives", "ic", "ics", "al", "ant", "ants", "ent", "ents", "ure", "ures", "ide", "ides",
    "ate", "ates", "ated", "ating", "ine", "ines", "ene", "enes", "yl", "ol", "ase", "ose", "osis", "ing", "ings",
    "ed", "er", "ers", "ly", "est", "ies", "ied", "age", "ary", "ory", "ance", "ency", "ancy", "ward", "wards",
    "wise", "like", "hood", "ish", "ify", "ified", "ifies", "ometer", "ometers", "scope", "meter", "meters",
)  # fmt: skip


@dataclass(frozen=True)
class CaptureVerdict:
    blocked: bool
    category: Category | None
    via: Literal["lexicon", "near_miss", "script", "language", "classifier", "allowlist", "none"]
    score: float = 0.0


def _fold(text: str) -> str:
    """Compatibility forms folded, invisible format characters dropped, look-alike letters mapped to Latin: the text
    the shared write-policy rules (quotes, instruction shape, secrets) read."""
    folded = unicodedata.normalize("NFKC", text)
    return "".join(c for c in folded if unicodedata.category(c) != "Cf").translate(_TO_LATIN)


def shape(text: str) -> str:
    """The form every rule reads (case kept, for the names rule): compatibility forms folded, look-alike letters of
    other scripts mapped to Latin, combining marks removed, curly apostrophes made plain, and hyphens, dashes,
    slashes and a possessive 's turned into word breaks."""
    folded = _fold(text)
    bare = "".join(c for c in unicodedata.normalize("NFD", folded) if not unicodedata.combining(c))
    bare = unicodedata.normalize("NFC", bare).translate(_APOSTROPHES)
    bare = _POSSESSIVE.sub("", bare)
    return " ".join(_BREAKS.sub(" ", bare).split())


def _term_pattern(term: str) -> str:
    """A lexicon term as a whole-word pattern: spaces match any spacing, a trailing * any word ending."""
    wildcard = term.endswith("*")
    words = shape(term.rstrip("*")).lower().split()
    body = r"\s+".join(re.escape(w) for w in words)
    return body + (r"[\w']*" if wildcard else "")


def _compile(terms: Iterable[str]) -> re.Pattern[str] | None:
    patterns = sorted({_term_pattern(t.strip()) for t in terms if t and t.strip()}, key=len, reverse=True)
    patterns = [p for p in patterns if p]
    if not patterns:
        return None
    return re.compile(_BOUNDARY_BEFORE + "(?:" + "|".join(patterns) + ")" + _BOUNDARY_AFTER, re.IGNORECASE)


def features(text: str) -> list[str]:
    """Unigrams and bigrams of the normalised words, stop words left out of the unigrams."""
    tokens = policy.words(shape(text))
    grams = [t for t in tokens if t not in _STOP]
    grams += [f"{a} {b}" for a, b in pairwise(tokens)]
    return grams


def _one_edit(a: str, b: str) -> bool:
    """Whether two words are at most one insertion, deletion or substitution apart."""
    if a == b:
        return True
    if abs(len(a) - len(b)) > 1:
        return False
    if len(a) > len(b):
        a, b = b, a
    i = 0
    while i < len(a) and a[i] == b[i]:
        i += 1
    return a[i:] == b[i + 1 :] or (len(a) == len(b) and a[i + 1 :] == b[i + 1 :])


def _deletions(word: str) -> set[str]:
    return {word, *(word[:i] + word[i + 1 :] for i in range(len(word)))}


@dataclass(frozen=True)
class NearMisses:
    """The lexicon's whole single words of at least `min_letters` letters (not wildcard stems: the file lists the full
    forms of those it wants near misses of, such as "dyslexia" beside "dyslexi*"), indexed by their one-letter
    deletions, so a word one edit away from one of them is found without comparing it with every term. A near miss
    is always one edit from a whole word, never from the beginning of a longer one."""

    min_letters: int
    words: Mapping[str, tuple[tuple[Category, str], ...]]
    known: frozenset[str]  # English words one edit from a term that mean something else ("rather", "faster")

    @classmethod
    def build(cls, terms: Mapping[Category, Sequence[str]], known: Iterable[str], min_letters: int) -> NearMisses:
        words: dict[str, list[tuple[Category, str]]] = {}
        for category in CHECK_ORDER:
            for raw in terms.get(category, ()):
                term = shape(str(raw)).lower()
                if len(term) < min_letters or not term.isalpha():
                    continue  # a phrase, a wildcard stem or a short word: no near misses
                for key in _deletions(term):
                    words.setdefault(key, []).append((category, term))
        return cls(min_letters, {k: tuple(v) for k, v in words.items()}, frozenset(shape(k).lower() for k in known))

    def match(self, word: str) -> Category | None:
        """The category of the listed word `word` is one edit away from, or None (a known near miss is not)."""
        if len(word) < self.min_letters - 1 or word in self.known:
            return None
        for key in sorted(_deletions(word)):
            for category, form in self.words.get(key, ()):
                if _one_edit(word, form):
                    return category
        return None


@dataclass(frozen=True)
class NaiveBayes:
    """Multinomial naive Bayes with add-one smoothing and equal class priors. Features the examples never showed are
    ignored, so an unfamiliar word cannot tip a note into a small class by smoothing alone."""

    classes: tuple[str, ...]
    log_likelihood: Mapping[str, Mapping[str, float]]
    vocabulary: frozenset[str]

    @classmethod
    def fit(cls, examples: Mapping[str, Sequence[str]]) -> NaiveBayes:
        counts = {label: Counter(g for text in texts for g in features(text)) for label, texts in examples.items()}
        vocabulary = frozenset(g for c in counts.values() for g in c)
        size = len(vocabulary)
        table = {
            label: {g: math.log((c[g] + 1) / (sum(c.values()) + size)) for g in vocabulary}
            for label, c in counts.items()
        }
        return cls(tuple(sorted(examples)), table, vocabulary)

    def posteriors(self, text: str) -> dict[str, float]:
        grams = [g for g in features(text) if g in self.vocabulary]
        if not grams:
            return {label: 1 / len(self.classes) for label in self.classes}
        scores = {label: sum(self.log_likelihood[label][g] for g in grams) for label in self.classes}
        top = max(scores.values())
        exp = {label: math.exp(s - top) for label, s in scores.items()}
        total = sum(exp.values())
        return {label: exp[label] / total for label in self.classes}


def foreign_letters(text: str) -> bool:
    """A letter of a script other than Latin in the note (Greek letters standing as symbols excepted, such as λ, Δ
    or μ), or a Greek look-alike letter inside a word with Latin letters, or a word of Greek look-alikes only: the
    shapes used to hide a word from the lexicon (MEM-10)."""
    for word in _LETTERS.findall(unicodedata.normalize("NFKC", text)):
        scripts = {unicodedata.name(c, "UNKNOWN").split()[0] for c in word}
        if scripts - {"LATIN", "GREEK"}:
            return True
        if "GREEK" in scripts and "LATIN" in scripts and any(c in _GREEK_LOOKALIKES for c in word):
            return True
        if scripts == {"GREEK"} and len(word) > 1 and all(c in _GREEK_LOOKALIKES for c in word):
            return True
    return False


@dataclass(frozen=True)
class CaptureFilter:
    """The loaded lexicon, allowlist, near misses, English vocabulary and classifier."""

    path: Path
    threshold: float
    lexicon: Mapping[Category, re.Pattern[str]] = field(default_factory=dict, repr=False)
    allowlist: re.Pattern[str] | None = field(default=None, repr=False)
    names_allowed: frozenset[str] = field(default_factory=frozenset, repr=False)
    classifier: NaiveBayes | None = field(default=None, repr=False)
    near_misses: NearMisses | None = field(default=None, repr=False)
    english: frozenset[str] = field(default_factory=frozenset, repr=False)
    english_min_share: float = 0.0

    @classmethod
    def load(cls, path: Path, settings: Settings) -> CaptureFilter:
        data: dict[str, Any] = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
        terms: Mapping[str, list[str]] = data.get("categories") or {}
        unknown = set(terms) - set(CATEGORIES)
        if unknown:
            raise ValueError(f"{path.name}: unknown categories {sorted(unknown)}")
        lexicon: dict[Category, re.Pattern[str]] = {}
        for category in CATEGORIES:
            pattern = _compile(str(t) for t in terms.get(category, []))
            if pattern is not None:
                lexicon[cast(Category, category)] = pattern
        examples: Mapping[str, list[str]] = data.get("examples") or {}
        if set(examples) - {BENIGN, *CATEGORIES} or BENIGN not in examples:
            raise ValueError(f"{path.name}: examples need a benign class and categories of the list only")
        names = frozenset(str(n) for n in data.get("names_allowed") or [])
        allowed = [str(t) for t in data.get("allowlist") or []]
        typed = {cast(Category, c): [str(t) for t in v] for c, v in terms.items()}
        for category, extra in (data.get("near_miss_forms") or {}).items():
            if category not in CATEGORIES:
                raise ValueError(f"{path.name}: unknown category {category!r} in near_miss_forms")
            typed[cast(Category, category)] = [*typed.get(cast(Category, category), []), *_flat(extra)]
        # Every word the file itself uses in a learning sense reads as English too.
        sources = [*_flat(data.get("english")), *allowed, *examples.get(BENIGN, [])]
        english = {w for t in sources for w in _words(str(t))}
        english |= {n.lower() for n in names}
        return cls(
            path=path,
            threshold=settings.capture_classifier_threshold,
            lexicon=lexicon,
            allowlist=_compile(allowed),
            names_allowed=names,
            classifier=NaiveBayes.fit({k: [str(t) for t in v] for k, v in examples.items()}),
            near_misses=NearMisses.build(typed, _flat(data.get("near_misses")), settings.capture_fuzzy_min_letters),
            english=frozenset(english),
            english_min_share=settings.capture_english_min_share,
        )

    def _names_other_people(self, text: str) -> bool:
        """A capitalised word in the middle of a sentence that is not a curriculum name reads as a person or a
        place. The first word of each sentence is skipped."""
        for match in _CAPITALISED.finditer(text):
            before = text[: match.start()]
            if not before.strip() or _SENTENCE_END.search(before):
                continue
            word = match.group(1)
            if word not in self.names_allowed and word.rstrip("s") not in self.names_allowed:
                return True
        return False

    def _rules(self, category: Category, folded: str, shaped: str, cleared: str, instructions: bool) -> bool:
        pattern = self.lexicon.get(category)
        if pattern is not None and pattern.search(cleared):
            return True
        if category == "quotes_and_distress":
            return policy.quoted(folded)
        if category == "location_and_contacts":
            return self._names_other_people(cleared)
        if category == "instructions_and_secrets":
            shaped_like = instructions and (policy.instruction_shaped(folded) or policy.instruction_shaped(shaped))
            return shaped_like or policy.secret_like(folded)
        return False

    def _reads_as_english(self, cleared: str) -> bool:
        """At least `english_min_share` of the words of three letters or more read as English: known words (or a
        known word with an English ending), long words with an English word ending, curriculum names. Short words
        (units, symbols) and capitalised abbreviations do not count either way."""
        counted = known = 0
        for word in _LETTERS.findall(cleared):
            if len(word) < 3 or (word.isupper() and len(word) <= 5):
                continue
            lower = word.lower()
            counted += 1
            if (
                lower in self.english
                or any(lower.endswith(e) and lower[: -len(e)] in self.english for e in _ENDINGS)
                or (len(lower) >= 6 and lower.endswith(_ENGLISH_SUFFIXES))
            ):
                known += 1
        return counted == 0 or known >= self.english_min_share * counted

    def check(self, text: str, *, instructions: bool = True) -> CaptureVerdict:
        """Blocked when the lexicon matches a category (and the allowlist does not clear it), a word is a near miss
        of a listed word, the note hides a word in another script, the note is not mostly English, or the
        classifier's probability for a category reaches the threshold. `instructions=False` leaves out the
        instruction-shape rule, for a session summary, which speaks to the student."""
        folded, shaped = _fold(text), shape(text)
        cleared = self.allowlist.sub(" ", shaped) if self.allowlist is not None else shaped
        allowed = cleared != shaped
        for category in CHECK_ORDER:
            if self._rules(category, folded, shaped, cleared, instructions):
                return CaptureVerdict(True, category, "lexicon", 1.0)
        if self.near_misses is not None:
            for word in _LETTERS.findall(cleared.lower()):
                near = self.near_misses.match(word)
                if near is not None:
                    return CaptureVerdict(True, near, "near_miss", 1.0)
        if foreign_letters(text):
            return CaptureVerdict(True, None, "script", 1.0)
        if self.english and not self._reads_as_english(cleared):
            return CaptureVerdict(True, None, "language", 1.0)
        score, top = 0.0, None
        if self.classifier is not None:
            posteriors = self.classifier.posteriors(cleared)
            top, score = max(((c, p) for c, p in posteriors.items() if c != BENIGN), key=lambda cp: (cp[1], cp[0]))
        if top is not None and score >= self.threshold:
            return CaptureVerdict(True, cast(Category, top), "classifier", score)
        return CaptureVerdict(False, None, "allowlist" if allowed else "none", score)


def _flat(value: Any) -> list[str]:
    """A YAML list of words, which may be written as rows of flow lists, as one flat list."""
    if value is None:
        return []
    if isinstance(value, list):
        return [w for item in value for w in _flat(item)]
    return [str(value)]


def _words(text: str) -> list[str]:
    return [w.lower() for w in _LETTERS.findall(shape(text))]


# The source tree's agent bundle, for callers that were not handed the runtime's loaded filter.
SOURCE_BUNDLE = Path(__file__).resolve().parents[4] / "content" / "agent"


@functools.cache
def _load_cached(path: Path, threshold: float, fuzzy: int, share: float) -> CaptureFilter:
    return CaptureFilter.load(path, Settings(capture_classifier_threshold=threshold, capture_fuzzy_min_letters=fuzzy,
                                             capture_english_min_share=share))  # fmt: skip


def default_filter(settings: Settings, root: Path | None = None) -> CaptureFilter | None:
    """The filter of the agent bundle at `root` (default: the source tree's), loaded once per setting; None when
    the bundle has no capture file. Writers treat None as "do not write" (fail closed); readers fall back to the
    write policy's text rules, so a prompt is never held up by it."""
    path = (root or SOURCE_BUNDLE) / CAPTURE_FILE
    if not path.is_file():
        return None
    return _load_cached(path, settings.capture_classifier_threshold, settings.capture_fuzzy_min_letters,
                        settings.capture_english_min_share)  # fmt: skip
