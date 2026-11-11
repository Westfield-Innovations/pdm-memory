# © 2026 Westfield Innovations LLC. Patent Pending.
# U.S. App. No. 19/739,419 | 63/953,563 | 63/953,842
# MODIFICATION PROHIBITED. USE AS SHIPPED.

"""
Detector gate (detector-then-resolver).

Cheap, deterministic gate that runs on every input. The expensive six-phase
engine fires only on GO: committed documents and high-stakes inputs
(patent / spec / governance / financial): a cheap detector on every input,
the expensive resolver only when it matters.

No engine invocation here — just a keyword + length scan.
"""

from __future__ import annotations

from pdm_memory.noun_probe.contracts import DetectorDecision
from pdm_memory.noun_probe.lexicon import split_sections, tokenize

# High-stakes category markers (lowercased). Kept small and precise — this is
# a gate, not a classifier.
_CATEGORY_KEYWORDS: dict[str, frozenset[str]] = {
    "patent": frozenset(
        {
            "patent",
            "claim",
            "claims",
            "embodiment",
            "invention",
            "examiner",
            "filing",
            "uspto",
            "prior",
        }
    ),
    "spec": frozenset(
        {
            "specification",
            "spec",
            "requirement",
            "requirements",
            "shall",
            "must",
            "interface",
            "protocol",
            "schema",
        }
    ),
    "governance": frozenset(
        {
            "governance",
            "policy",
            "compliance",
            "regulation",
            "regulatory",
            "audit",
            "mandate",
            "charter",
            "bylaw",
            "sla",
        }
    ),
    "financial": frozenset(
        {
            "invoice",
            "payment",
            "liability",
            "contract",
            "financial",
            "revenue",
            "settlement",
            "escrow",
        }
    ),
}

_DEFAULT_MIN_DOC_WORDS = 40
_DEFAULT_MIN_HITS = 2


def should_probe(
    text: str,
    *,
    force: bool = False,
    min_doc_words: int = _DEFAULT_MIN_DOC_WORDS,
    min_hits: int = _DEFAULT_MIN_HITS,
) -> DetectorDecision:
    if min_doc_words <= 0 or min_hits <= 0:
        raise ValueError("min_doc_words and min_hits must be positive")

    if force:
        return DetectorDecision(
            should_run=True, score=1.0, categories=(), reasons=("forced",)
        )

    tokens = [t.lower() for t in tokenize(text)]
    word_count = len(tokens)
    token_set = set(tokens)

    categories, total_hits = _match_categories(token_set)
    committed = word_count >= min_doc_words or len(split_sections(text)) >= 2
    length_factor = min(word_count / min_doc_words, 1.0)
    score = round(total_hits + length_factor, 2)

    should_run = bool(categories) and (committed or total_hits >= min_hits)
    return DetectorDecision(
        should_run=should_run,
        score=score,
        categories=categories,
        reasons=_reasons(categories, committed, total_hits, min_hits),
    )


def _match_categories(token_set: set[str]) -> tuple[tuple[str, ...], int]:
    matched: list[str] = []
    total_hits = 0
    for category, keywords in _CATEGORY_KEYWORDS.items():
        hits = len(token_set & keywords)
        if hits:
            matched.append(category)
            total_hits += hits
    return tuple(matched), total_hits


def _reasons(
    categories: tuple[str, ...],
    committed: bool,
    total_hits: int,
    min_hits: int,
) -> tuple[str, ...]:
    if not categories:
        return ("no high-stakes category",)
    reasons = [f"high-stakes: {', '.join(categories)}"]
    if committed:
        reasons.append("committed document")
    elif total_hits >= min_hits:
        reasons.append(f"keyword density ({total_hits})")
    else:
        reasons.append("below committed/density threshold → skip")
    return tuple(reasons)
