# © 2026 Westfield Innovations LLC. Patent Pending.
# U.S. App. No. 19/739,419 | 63/953,563 | 63/953,842
# MODIFICATION PROHIBITED. USE AS SHIPPED.

"""
Phase 3 — Controlled Semantic Perturbation.

Where the creativity happens, under strict rules:

  Rule A — Class Preservation: substitute only within the same noun class.
  Rule B — Functional Substitution: substitute on function, not synonym.
           If the structure survives → invariant pattern found.
           If it breaks → boundary condition revealed.
  Rule C — Absurdity Trigger: a verb that belongs to a *different* class is
           not discarded — it flags the assumption that made it absurd (the
           noun is borrowing a foreign-class job: a possible smush). Absurd
           structures are NOT kept here — Phase 4 decides.

Survive/break is decided deterministically via class-verb affinity: a verb in
the noun's own class affinity generalises across the class (invariant); a verb
owned by another class is foreign (C-absurd); a verb in no affinity is
idiosyncratic to the word (B-boundary).

Requires a locked Boundary (Law 0).
"""

from __future__ import annotations

from pdm_memory.noun_probe.boundary import BoundaryError
from pdm_memory.noun_probe.contracts import (
    Boundary,
    NounClass,
    Perturbation,
    VerbCouplingResult,
)
from pdm_memory.noun_probe.lexicon import CLASS_LEXICON, CLASS_VERB_AFFINITY


def perturb(
    coupling_result: VerbCouplingResult,
    boundary: Boundary,
    *,
    substitutes_per_noun: int = 1,
) -> tuple[Perturbation, ...]:
    if not isinstance(boundary, Boundary):
        raise BoundaryError("Phase 3 requires a locked Boundary (Law 0)")
    if substitutes_per_noun <= 0:
        raise ValueError("substitutes_per_noun must be positive")

    out: list[Perturbation] = []
    for coupling in coupling_result.couplings:
        verb = coupling.primary_verb or coupling.passive_verb or coupling.implied_verb
        for substitute in _substitutes(
            coupling.noun_class, coupling.noun, substitutes_per_noun
        ):
            out.append(_evaluate(coupling.noun, coupling.noun_class, verb, substitute))
    return tuple(out)


def _substitutes(noun_class: NounClass, source: str, count: int) -> list[str]:
    """Same-class functional stand-ins (Rule A), deterministic order."""
    pool = [w for w in sorted(CLASS_LEXICON[noun_class]) if w != source]
    return pool[:count]


def _evaluate(
    noun: str,
    noun_class: NounClass,
    verb: str,
    substitute: str,
) -> Perturbation:
    if verb in CLASS_VERB_AFFINITY[noun_class]:
        return Perturbation(
            source_noun=noun,
            noun_class=noun_class,
            verb=verb,
            substitute=substitute,
            survived=True,
            rule="B-invariant",
            note=(
                f"{noun_class.value} nouns share '{verb}' — pattern is "
                f"class-level; '{substitute}' {verb}s too"
            ),
        )

    foreign = _foreign_owner(verb, noun_class)
    if foreign is not None:
        return Perturbation(
            source_noun=noun,
            noun_class=noun_class,
            verb=verb,
            substitute=substitute,
            survived=False,
            rule="C-absurd",
            note=(
                f"'{verb}' is a {foreign.value} verb used on a "
                f"{noun_class.value} noun — '{noun}' borrows a foreign-class "
                f"job (possible smush)"
            ),
        )

    return Perturbation(
        source_noun=noun,
        noun_class=noun_class,
        verb=verb,
        substitute=substitute,
        survived=False,
        rule="B-boundary",
        note=(
            f"'{verb}' is idiosyncratic to '{noun}', not general to "
            f"{noun_class.value} (boundary condition)"
        ),
    )


def _foreign_owner(verb: str, own_class: NounClass) -> NounClass | None:
    for noun_class, affinity in CLASS_VERB_AFFINITY.items():
        if noun_class is not own_class and verb in affinity:
            return noun_class
    return None
