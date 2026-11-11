# © 2026 Westfield Innovations LLC. Patent Pending.
# U.S. App. No. 19/739,419 | 63/953,563 | 63/953,842
# MODIFICATION PROHIBITED. USE AS SHIPPED.

"""
Phase 1 — Structural Noun Extraction.

Extract the top nouns by FUNCTION, not frequency, and force each into one of
the five structural classes (no metaphor class). Function-first means raw
count is log-damped while structural role (subject/object adjacency to a
verb) and section spread are amplified.

Requires a locked Boundary (Law 0) — re-asserted here so the phase cannot be
called out of order.
"""

from __future__ import annotations

import math
from dataclasses import dataclass, field

from pdm_memory.noun_probe.boundary import BoundaryError
from pdm_memory.noun_probe.contracts import Boundary, StructuralNoun
from pdm_memory.noun_probe.lexicon import (
    classify_noun,
    is_candidate_token,
    neighbor_is_verb,
    split_sections,
    split_sentences,
    tokenize,
)

_ROLE_CAP = 4  # diminishing returns on role evidence


@dataclass(slots=True)
class _Candidate:
    text: str
    occurrences: int = 0
    role_hits: int = 0
    is_proper: bool = False
    sections: set[int] = field(default_factory=set)


def extract_structural_nouns(
    text: str,
    boundary: Boundary,
    *,
    top_n: int = 12,
) -> tuple[StructuralNoun, ...]:
    if not isinstance(boundary, Boundary):
        raise BoundaryError("Phase 1 requires a locked Boundary (Law 0)")
    if top_n <= 0:
        raise ValueError("top_n must be positive")

    sections = split_sections(text)
    if not sections:
        return ()

    candidates = _collect_candidates(sections)
    total_sections = len(sections)
    nouns = [
        _build_noun(cand, total_sections) for cand in candidates.values() if _keep(cand)
    ]
    nouns.sort(key=lambda n: (-n.salience, n.text))
    return tuple(nouns[:top_n])


def _collect_candidates(sections: list[str]) -> dict[str, _Candidate]:
    candidates: dict[str, _Candidate] = {}
    for section_idx, section in enumerate(sections):
        for sentence in split_sentences(section):
            raw_tokens = tokenize(sentence)
            lowered = [t.lower() for t in raw_tokens]
            for i, raw in enumerate(raw_tokens):
                token = lowered[i]
                if not is_candidate_token(token):
                    continue
                cand = candidates.setdefault(token, _Candidate(text=token))
                cand.occurrences += 1
                cand.sections.add(section_idx)
                if i > 0 and raw[:1].isupper():
                    cand.is_proper = True
                if neighbor_is_verb(lowered, i, ahead=True) or neighbor_is_verb(
                    lowered, i, ahead=False
                ):
                    cand.role_hits += 1
    return candidates


def _keep(cand: _Candidate) -> bool:
    _, _, matched = classify_noun(cand.text)
    return matched or cand.is_proper or cand.role_hits > 0


def _build_noun(cand: _Candidate, total_sections: int) -> StructuralNoun:
    noun_class, confidence, matched = classify_noun(cand.text)
    if not matched:
        confidence = 0.4 if cand.is_proper else 0.3

    freq_factor = 1.0 + math.log(cand.occurrences)
    spread_factor = 0.5 + 0.5 * (len(cand.sections) / total_sections)
    role_factor = 1.0 + 0.5 * min(cand.role_hits, _ROLE_CAP)
    salience = round(confidence * freq_factor * spread_factor * role_factor, 4)

    return StructuralNoun(
        text=cand.text,
        noun_class=noun_class,
        salience=salience,
        occurrences=cand.occurrences,
        sections=tuple(sorted(cand.sections)),
        confidence=confidence,
    )
