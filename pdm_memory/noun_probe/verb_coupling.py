# © 2026 Westfield Innovations LLC. Patent Pending.
# U.S. App. No. 19/739,419 | 63/953,563 | 63/953,842
# MODIFICATION PROHIBITED. USE AS SHIPPED.

"""
Phase 2 — Verb Coupling.

For each structural noun, derive its verb signature:
  - primary verb  — what it DOES   (active / subject: "noun VERBs")
  - passive verb  — what is done TO it ("noun is VERBed" / "VERB the noun")
  - implied verb  — the hidden assumption (class-level structural verb)

Then surface the structural verbs that repeat across >=2 nouns — those are
the system-dynamics verbs (accumulate, degrade, shift, relocate, partition
and any emergent ones).

Requires a locked Boundary (Law 0).
"""

from __future__ import annotations

from collections.abc import Sequence

from pdm_memory.noun_probe.boundary import BoundaryError
from pdm_memory.noun_probe.contracts import (
    Boundary,
    StructuralNoun,
    StructuralVerb,
    VerbCoupling,
    VerbCouplingResult,
)
from pdm_memory.noun_probe.lexicon import (
    AUX_VERBS,
    CLASS_IMPLIED_VERB,
    DETERMINERS,
    STRUCTURAL_VERBS,
    is_verb,
    normalize_verb,
    split_sections,
    split_sentences,
    tokenize,
)

_AHEAD_WINDOW = 4
_BEHIND_WINDOW = 3


def couple_verbs(
    nouns: Sequence[StructuralNoun],
    text: str,
    boundary: Boundary,
) -> VerbCouplingResult:
    if not isinstance(boundary, Boundary):
        raise BoundaryError("Phase 2 requires a locked Boundary (Law 0)")

    noun_texts = {n.text for n in nouns}
    if not noun_texts:
        return VerbCouplingResult(couplings=(), structural_verbs=())

    primary: dict[str, str] = {}
    passive: dict[str, str] = {}
    verb_to_nouns: dict[str, set[str]] = {}

    for section in split_sections(text):
        for sentence in split_sentences(section):
            tokens = [t.lower() for t in tokenize(sentence)]
            for i, token in enumerate(tokens):
                if token not in noun_texts:
                    continue
                active, taken = _couple_at(tokens, i)
                if active and token not in primary:
                    primary[token] = active
                if taken and token not in passive:
                    passive[token] = taken
                for verb in (active, taken):
                    if verb:
                        verb_to_nouns.setdefault(verb, set()).add(token)

    couplings = tuple(
        VerbCoupling(
            noun=n.text,
            noun_class=n.noun_class,
            primary_verb=primary.get(n.text),
            passive_verb=passive.get(n.text),
            implied_verb=CLASS_IMPLIED_VERB[n.noun_class],
        )
        for n in nouns
    )
    return VerbCouplingResult(
        couplings=couplings,
        structural_verbs=_structural_verbs(verb_to_nouns),
    )


def _couple_at(tokens: Sequence[str], index: int) -> tuple[str | None, str | None]:
    """Return (active_verb, passive_verb) lemmas for the noun at `index`."""
    role, lemma = _scan_ahead(tokens, index)
    active = lemma if role == "active" else None
    passive = lemma if role == "passive" else None
    if passive is None:
        passive = _scan_behind(tokens, index)
    return active, passive


def _scan_ahead(tokens: Sequence[str], index: int) -> tuple[str | None, str | None]:
    """'noun VERBs' → active; 'noun is VERBed' → passive."""
    passive_marker = False
    for j in range(index + 1, min(index + 1 + _AHEAD_WINDOW, len(tokens))):
        token = tokens[j]
        if token in AUX_VERBS:
            passive_marker = True
            continue
        if is_verb(token):
            lemma = normalize_verb(token)
            return ("passive" if passive_marker else "active"), lemma
        if token in DETERMINERS:
            continue
        break
    return None, None


def _scan_behind(tokens: Sequence[str], index: int) -> str | None:
    """'VERB the noun' → noun is the object (passive)."""
    for j in range(index - 1, max(index - 1 - _BEHIND_WINDOW, -1), -1):
        token = tokens[j]
        if token in AUX_VERBS:
            continue
        if is_verb(token):
            return normalize_verb(token)
        if token in DETERMINERS:
            continue
        break
    return None


def _structural_verbs(verb_to_nouns: dict[str, set[str]]) -> tuple[StructuralVerb, ...]:
    structural = [
        StructuralVerb(
            verb=verb,
            coupled_nouns=tuple(sorted(bound_nouns)),
            is_canonical=verb in STRUCTURAL_VERBS,
        )
        for verb, bound_nouns in verb_to_nouns.items()
        if len(bound_nouns) >= 2
    ]
    structural.sort(key=lambda sv: (-len(sv.coupled_nouns), sv.verb))
    return tuple(structural)
