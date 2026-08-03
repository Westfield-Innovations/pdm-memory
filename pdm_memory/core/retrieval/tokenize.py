# © 2026 Westfield Innovations LLC. Patent Pending.
# U.S. App. No. 19/739,419 | 63/953,563 | 63/953,842
# MODIFICATION PROHIBITED. USE AS SHIPPED.

"""Query tokenization and semantic overlap helpers."""

from __future__ import annotations

import re
from collections.abc import Sequence

from pdm_memory.core.signature import SignatureRecord

WORD_PATTERN = re.compile(r"\b[a-zA-Z]{3,}\b")
NUMBER_PATTERN = re.compile(r"(?<![A-Za-z])-?\d+(?:\.\d+)?(?![A-Za-z])")

_STOPWORDS: frozenset[str] = frozenset(
    {
        "the", "and", "for", "how", "what", "that", "this", "with", "are", "was",
        "not", "can", "will", "from", "have", "been", "should", "would", "could",
        "which", "when", "where",
    }
)


def tokenize_query(query: str) -> list[str]:
    """Extract meaningful tokens from a query string for tag matching."""
    words = WORD_PATTERN.findall(query.lower())
    return [w for w in words if w not in _STOPWORDS]


def tags_overlap(cue: set[str], tags: set[str]) -> bool:
    if cue & tags:
        return True
    for q in cue:
        for t in tags:
            if len(q) >= 4 and (q in t or t in q):
                return True
    return False


def semantic_query_overlap(rec: SignatureRecord, query_tags: Sequence[str]) -> float:
    """
    Tag or fact-token overlap between query and signature.

    Blocks high-P memories from coupling when tags do not match and the
    fact text shares no meaningful tokens with the query.
    """
    if not query_tags:
        return 1.0
    cue = {t.lower() for t in query_tags if t}
    sig_tags = {t.lower() for t in (rec.intent_tags or []) if t}
    if tags_overlap(cue, sig_tags):
        return 1.0
    fact_tokens = set(tokenize_query(rec.compressed_fact or ""))
    if not fact_tokens:
        return 0.0
    if tags_overlap(cue, fact_tokens):
        return 1.0
    return len(fact_tokens & cue) / max(len(fact_tokens | cue), 1)


class TokenizeMixin:
    """Backward-compatible static methods on RetrievalEngine."""

    _tokenize_query = staticmethod(tokenize_query)
    _tags_overlap = staticmethod(tags_overlap)
    _semantic_query_overlap = staticmethod(semantic_query_overlap)
