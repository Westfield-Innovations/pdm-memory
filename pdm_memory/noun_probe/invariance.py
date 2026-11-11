# © 2026 Westfield Innovations LLC. Patent Pending.
# U.S. App. No. 19/739,419 | 63/953,563 | 63/953,842
# MODIFICATION PROHIBITED. USE AS SHIPPED.

"""
Phase 5 — Invariance Detection.

Where the real signal lives. Aggregates Phase 2 structural verbs and Phase 4
re-locked insights into four finding kinds:

  - invariant               — noun-verb structure repeating across nouns
  - boundary_inconsistency  — word-specific boundary condition (HYPOTHESIS)
  - constraint_relocation   — a verb sitting on the wrong class (relocate it)
  - misframed               — a noun framed as one role but used as another

Requires a locked Boundary (Law 0).
"""

from __future__ import annotations

from collections.abc import Sequence

from pdm_memory.noun_probe.boundary import BoundaryError
from pdm_memory.noun_probe.contracts import (
    Boundary,
    EvidenceTag,
    InvarianceSignal,
    NounClass,
    RelockedInsight,
    VerbCouplingResult,
)
from pdm_memory.noun_probe.lexicon import in_class_lexicon


def detect_invariance(
    coupling_result: VerbCouplingResult,
    relocked: Sequence[RelockedInsight],
    boundary: Boundary,
) -> tuple[InvarianceSignal, ...]:
    if not isinstance(boundary, Boundary):
        raise BoundaryError("Phase 5 requires a locked Boundary (Law 0)")

    signals: list[InvarianceSignal] = []
    signals.extend(_invariants(coupling_result))
    signals.extend(_from_relocked(relocked))
    signals.extend(_misframed(coupling_result))
    return tuple(signals)


def _invariants(coupling_result: VerbCouplingResult) -> list[InvarianceSignal]:
    return [
        InvarianceSignal(
            kind="invariant",
            subject=sv.verb,
            detail=(
                f"'{sv.verb}' repeats across {', '.join(sv.coupled_nouns)} "
                f"— system-dynamics verb"
            ),
        )
        for sv in coupling_result.structural_verbs
    ]


def _from_relocked(relocked: Sequence[RelockedInsight]) -> list[InvarianceSignal]:
    out: list[InvarianceSignal] = []
    for insight in sorted(relocked, key=lambda r: r.perturbation.source_noun):
        pert = insight.perturbation
        if insight.evidence is EvidenceTag.HYPOTHESIS:
            out.append(
                InvarianceSignal(
                    kind="boundary_inconsistency",
                    subject=pert.source_noun,
                    detail=pert.note,
                )
            )
        elif insight.evidence is EvidenceTag.METAPHOR:
            out.append(
                InvarianceSignal(
                    kind="constraint_relocation",
                    subject=pert.source_noun,
                    detail=(
                        f"relocate '{pert.verb}' to its owning class — "
                        f"'{pert.source_noun}' ({pert.noun_class.value}) "
                        f"should not carry it"
                    ),
                )
            )
    return out


def _misframed(coupling_result: VerbCouplingResult) -> list[InvarianceSignal]:
    # Only a lexicon actor can be misframed: a suffix guess ("customer",
    # "vendor") is too weak a frame to be used against.
    out: list[InvarianceSignal] = []
    for coupling in sorted(coupling_result.couplings, key=lambda c: c.noun):
        if (
            coupling.noun_class is NounClass.ACTOR
            and in_class_lexicon(coupling.noun)
            and coupling.primary_verb is None
            and coupling.passive_verb is not None
        ):
            out.append(
                InvarianceSignal(
                    kind="misframed",
                    subject=coupling.noun,
                    detail=(
                        f"'{coupling.noun}' is framed as an ACTOR but only "
                        f"appears acted upon ('{coupling.passive_verb}') — "
                        f"misframed problem"
                    ),
                )
            )
    return out
