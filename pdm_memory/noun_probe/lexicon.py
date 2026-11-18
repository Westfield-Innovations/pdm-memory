# © 2026 Westfield Innovations LLC. Patent Pending.
# U.S. App. No. 19/739,419 | 63/953,563 | 63/953,842
# MODIFICATION PROHIBITED. USE AS SHIPPED.

"""
Shared lexicon + tokenizers for the Noun Probe.

Deterministic and dependency-light — no spaCy / NLTK. Classification is a
lexicon-first, suffix-fallback heuristic; this is honest about being a
heuristic, not a parser. Reused by Phase 1 (extraction) and Phase 2 (verb
coupling).
"""

from __future__ import annotations

import re
from collections.abc import Sequence

from pdm_memory.noun_probe.contracts import NounClass

# ---------------------------------------------------------------------------
# Tokenizers
# ---------------------------------------------------------------------------

_WORD_RE = re.compile(r"[A-Za-z][A-Za-z\-]+")
_SENTENCE_SPLIT_RE = re.compile(r"[.;:!?\n]+")
_SECTION_SPLIT_RE = re.compile(r"\n\s*\n+")


def split_sections(text: str) -> list[str]:
    """Split on blank lines — paragraphs are the spread unit."""
    return [s.strip() for s in _SECTION_SPLIT_RE.split(text or "") if s.strip()]


def split_sentences(text: str) -> list[str]:
    return [s.strip() for s in _SENTENCE_SPLIT_RE.split(text or "") if s.strip()]


def tokenize(text: str) -> list[str]:
    """Original-case word tokens (proper-noun detection needs the casing)."""
    return _WORD_RE.findall(text or "")


# ---------------------------------------------------------------------------
# Stopwords / auxiliaries
# ---------------------------------------------------------------------------

STOPWORDS: frozenset[str] = frozenset(
    {
        "the",
        "a",
        "an",
        "and",
        "or",
        "but",
        "if",
        "then",
        "else",
        "for",
        "of",
        "to",
        "in",
        "on",
        "at",
        "by",
        "as",
        "is",
        "it",
        "its",
        "be",
        "this",
        "that",
        "these",
        "those",
        "with",
        "from",
        "into",
        "out",
        "up",
        "down",
        "over",
        "under",
        "than",
        "so",
        "not",
        "no",
        "yes",
        "we",
        "you",
        "they",
        "he",
        "she",
        "i",
        "me",
        "my",
        "our",
        "your",
        "their",
        "them",
        "us",
        "who",
        "whom",
        "which",
        "what",
        "when",
        "where",
        "why",
        "how",
        "all",
        "any",
        "each",
        "one",
        "two",
        "three",
        "more",
        "most",
        "some",
        "such",
        "only",
        "own",
        "same",
        "other",
        "very",
        "can",
        "will",
        "just",
        "also",
        "about",
        "across",
        "after",
        "before",
        "between",
        "through",
        "while",
        "once",
        "here",
        "there",
    }
)

# Only determiners are transparent between a verb and its object. Prepositions
# (over, into, through, ...) sever the direct-object relation, so they are NOT
# here — the verb scans break on them, avoiding false "VERB <prep> noun" couplings.
DETERMINERS: frozenset[str] = frozenset(
    {
        "the",
        "a",
        "an",
        "this",
        "that",
        "these",
        "those",
        "its",
        "their",
        "our",
        "your",
        "his",
        "her",
        "my",
    }
)

AUX_VERBS: frozenset[str] = frozenset(
    {
        "is",
        "are",
        "was",
        "were",
        "be",
        "been",
        "being",
        "am",
        "has",
        "have",
        "had",
        "do",
        "does",
        "did",
        "will",
        "would",
        "can",
        "could",
        "may",
        "might",
        "must",
        "shall",
        "should",
    }
)

# ---------------------------------------------------------------------------
# Noun class lexicons (function-first)
# ---------------------------------------------------------------------------

CLASS_LEXICON: dict[NounClass, frozenset[str]] = {
    NounClass.CONSTRAINT: frozenset(
        {
            "constraint",
            "limit",
            "boundary",
            "bound",
            "threshold",
            "rule",
            "force",
            "law",
            "cap",
            "ceiling",
            "floor",
            "gate",
            "guardrail",
            "discipline",
            "requirement",
            "claim",
            "invariant",
            "pressure",
            "deadline",
            "scope",
            "policy",
        }
    ),
    NounClass.RESOURCE: frozenset(
        {
            "time",
            "money",
            "energy",
            "memory",
            "budget",
            "capital",
            "bandwidth",
            "fuel",
            "charge",
            "power",
            "cost",
            "resource",
            "token",
            "compute",
            "storage",
            "capacity",
            "debt",
            "entropy",
        }
    ),
    NounClass.ACTOR: frozenset(
        {
            "agent",
            "system",
            "user",
            "examiner",
            "developer",
            "dev",
            "reader",
            "author",
            "operator",
            "engine",
            "sensor",
            "detector",
            "resolver",
            "probe",
            "finder",
            "estimator",
            "model",
            "machine",
            "worker",
            "governor",
            "minister",
            "client",
            "server",
        }
    ),
    NounClass.STATE: frozenset(
        {
            "phase",
            "stability",
            "fidelity",
            "state",
            "mode",
            "status",
            "signature",
            "capability",
            "shape",
            "structure",
            "pattern",
            "alignment",
            "condition",
            "equilibrium",
            "dimension",
            "meaning",
            "command",
            "feedback",
        }
    ),
    NounClass.FAILURE: frozenset(
        {
            "loss",
            "decay",
            "instability",
            "failure",
            "error",
            "drift",
            "collapse",
            "corruption",
            "fault",
            "breakage",
            "misread",
            "hallucination",
            "leak",
            "race",
            "deadlock",
            "regression",
            "bug",
        }
    ),
}

# Words the "-er" / "-or" suffix rules would take for actors: comparatives and
# function words. Never nouns, so never candidates.
NON_NOUN_ER: frozenset[str] = frozenset(
    {
        "after",
        "another",
        "better",
        "bigger",
        "cheaper",
        "closer",
        "deeper",
        "earlier",
        "easier",
        "either",
        "ever",
        "faster",
        "fewer",
        "further",
        "greater",
        "harder",
        "higher",
        "however",
        "larger",
        "later",
        "longer",
        "lower",
        "neither",
        "never",
        "newer",
        "older",
        "other",
        "over",
        "poor",
        "rather",
        "safer",
        "shorter",
        "slower",
        "smaller",
        "sooner",
        "stronger",
        "together",
        "under",
        "weaker",
        "whether",
        "wider",
    }
)

# Longest suffixes first so "tion" wins over "on".
SUFFIX_RULES: tuple[tuple[str, NounClass], ...] = (
    ("tion", NounClass.STATE),
    ("sion", NounClass.STATE),
    ("ment", NounClass.STATE),
    ("ance", NounClass.STATE),
    ("ence", NounClass.STATE),
    ("ness", NounClass.STATE),
    ("ship", NounClass.STATE),
    ("ity", NounClass.STATE),
    ("ism", NounClass.STATE),
    ("ure", NounClass.STATE),
    ("ist", NounClass.ACTOR),
    ("age", NounClass.RESOURCE),
    ("er", NounClass.ACTOR),
    ("or", NounClass.ACTOR),
)

# ---------------------------------------------------------------------------
# Verb lexicon (recognition + lemma grouping)
# ---------------------------------------------------------------------------

VERB_LEXICON: frozenset[str] = frozenset(
    {
        "accumulate",
        "degrade",
        "shift",
        "relocate",
        "partition",
        "deposit",
        "carry",
        "hide",
        "compress",
        "break",
        "reveal",
        "surface",
        "split",
        "broaden",
        "lock",
        "perturb",
        "observe",
        "watch",
        "repeat",
        "nest",
        "pin",
        "smush",
        "act",
        "serve",
        "satisfy",
        "resemble",
        "fail",
        "decay",
        "drift",
        "leak",
        "validate",
        "measure",
        "fire",
        "run",
        "kill",
        "define",
        "declare",
        "extract",
        "classify",
        "map",
        "substitute",
        "survive",
        "reapply",
        "veto",
        "introduce",
        "invent",
        "keep",
        "disturb",
        "deploy",
        "flag",
        "abort",
        "limit",
        "force",
        "bound",
        "deplete",
        "persist",
        "propagate",
        "emerge",
        "couple",
        "damp",
        "do",
        "make",
        "cause",
        "produce",
        "hold",
        "block",
        "pass",
    }
)

# Canonical system-dynamics verbs (Phase 2 highlight set).
STRUCTURAL_VERBS: frozenset[str] = frozenset(
    {"accumulate", "degrade", "shift", "relocate", "partition"}
)

# Verbs that are NATURAL for each class (Phase 3 survive/break test).
# A noun's verb being in its own class affinity → within-class substitution
# survives (invariant). A verb that belongs to another class → foreign job
# (possible smush). A verb in no affinity → idiosyncratic (boundary condition).
CLASS_VERB_AFFINITY: dict[NounClass, frozenset[str]] = {
    NounClass.CONSTRAINT: frozenset(
        {"limit", "force", "bound", "partition", "block", "hold", "gate", "pin"}
    ),
    NounClass.RESOURCE: frozenset(
        {"accumulate", "deplete", "deposit", "decay", "carry", "compress"}
    ),
    NounClass.ACTOR: frozenset(
        {"act", "serve", "observe", "fire", "run", "deploy", "satisfy", "produce"}
    ),
    NounClass.STATE: frozenset(
        {"persist", "shift", "hold", "emerge", "couple", "damp", "surface"}
    ),
    NounClass.FAILURE: frozenset(
        {"propagate", "degrade", "decay", "leak", "collapse", "drift", "fail", "break"}
    ),
}

# Class-level hidden assumption (Phase 2 implied verb).
CLASS_IMPLIED_VERB: dict[NounClass, str] = {
    NounClass.CONSTRAINT: "limit",
    NounClass.RESOURCE: "deplete",
    NounClass.ACTOR: "act",
    NounClass.STATE: "persist",
    NounClass.FAILURE: "propagate",
}


# ---------------------------------------------------------------------------
# Verb helpers
# ---------------------------------------------------------------------------


def normalize_verb(token: str) -> str:
    """Crude lemmatizer: strip common inflections to a lexicon-comparable stem."""
    word = token.lower()
    if word.endswith("ies") and len(word) > 4:
        return word[:-3] + "y"
    for suffix in ("ing", "ed", "es", "s"):
        if word.endswith(suffix) and len(word) - len(suffix) >= 3:
            stem = word[: -len(suffix)]
            if stem in VERB_LEXICON:
                return stem
            if suffix == "ing" and (stem + "e") in VERB_LEXICON:
                return stem + "e"
            if suffix == "ed" and (stem + "e") in VERB_LEXICON:
                return stem + "e"
    return word


def is_verb(token: str) -> bool:
    word = token.lower()
    if word in VERB_LEXICON or word in AUX_VERBS:
        return True
    return normalize_verb(word) in VERB_LEXICON


# ---------------------------------------------------------------------------
# Noun classification
# ---------------------------------------------------------------------------


def classify_noun(word: str) -> tuple[NounClass, float, bool]:
    """
    Return (class, confidence, matched).

    `matched` is True when the class came from a lexicon hit (conf 1.0) or a
    suffix rule (conf 0.6). When False the default STATE/0.3 is returned and
    the caller decides whether to keep the token on other evidence.
    """
    token = word.lower()
    for noun_class, lexicon in CLASS_LEXICON.items():
        if token in lexicon:
            return noun_class, 1.0, True
    for suffix, noun_class in SUFFIX_RULES:
        if token.endswith(suffix) and len(token) - len(suffix) >= 2:
            return noun_class, 0.6, True
    return NounClass.STATE, 0.3, False


def in_class_lexicon(word: str) -> bool:
    token = word.lower()
    return any(token in lexicon for lexicon in CLASS_LEXICON.values())


def is_verb_form(token: str) -> bool:
    """A verb or a past participle ("deployed", "enforced") the lexicon does not name as a noun."""
    word = token.lower()
    if in_class_lexicon(word):
        return False
    return is_verb(word) or (word.endswith("ed") and not word.endswith("eed"))


def is_candidate_token(token: str) -> bool:
    word = token.lower()
    return (
        word.isalpha()
        and len(word) >= 3
        and word not in STOPWORDS
        and word not in AUX_VERBS
        and word not in NON_NOUN_ER
        and not is_verb_form(word)
    )


def neighbor_is_verb(tokens: Sequence[str], index: int, *, ahead: bool) -> bool:
    """Subject role = verb shortly after; object role = verb shortly before."""
    if ahead:
        window = range(index + 1, min(index + 3, len(tokens)))
    else:
        window = range(index - 1, max(index - 3, -1), -1)
    return any(is_verb(tokens[j]) for j in window)
