# © 2026 Westfield Innovations LLC. Patent Pending.
# U.S. App. No. 19/739,419 | 63/953,563 | 63/953,842
# MODIFICATION PROHIBITED. USE AS SHIPPED.

"""
Phase 4 — Constraint Re-Lock.

Reapply discipline to every Phase 3 perturbation and assign an evidence tag.
A perturbation passes re-lock only if the boundary stays intact and the
structure is class-consistent; anything else is labeled a creative artifact,
not a structural truth.

Substitution stayed within class (Rule A), so the boundary is intact by
construction — the discriminating signal is the perturbation outcome:

  B-invariant (survived)   → KNOWN       (kept: structural truth)
  B-boundary  (idiosyncratic) → HYPOTHESIS (kept: plausible boundary condition)
  C-absurd    (foreign job) → METAPHOR    (dropped: artifact unless evidenced)

Physical-plausibility / economic-feasibility gates are domain-specific and
left as honest pass-through hooks (boundary.domain) — they do not fabricate
verdicts they cannot justify.

Requires a locked Boundary (Law 0).
"""

from __future__ import annotations

from collections.abc import Sequence

from pdm_memory.noun_probe.boundary import BoundaryError
from pdm_memory.noun_probe.contracts import (
    Boundary,
    EvidenceTag,
    Perturbation,
    RelockedInsight,
)


def relock(
    perturbations: Sequence[Perturbation],
    boundary: Boundary,
) -> tuple[RelockedInsight, ...]:
    if not isinstance(boundary, Boundary):
        raise BoundaryError("Phase 4 requires a locked Boundary (Law 0)")
    return tuple(_relock_one(p) for p in perturbations)


def _relock_one(perturbation: Perturbation) -> RelockedInsight:
    if perturbation.survived and perturbation.rule == "B-invariant":
        return RelockedInsight(
            perturbation=perturbation,
            evidence=EvidenceTag.KNOWN,
            kept=True,
            reason="boundary intact + class-consistent → structural truth",
        )
    if perturbation.rule == "B-boundary":
        return RelockedInsight(
            perturbation=perturbation,
            evidence=EvidenceTag.HYPOTHESIS,
            kept=True,
            reason="boundary condition is plausible but word-specific → needs evidence",
        )
    return RelockedInsight(
        perturbation=perturbation,
        evidence=EvidenceTag.METAPHOR,
        kept=False,
        reason="foreign-class job → creative artifact, dropped unless evidenced",
    )
