# © 2026 Westfield Innovations LLC. Patent Pending.
# U.S. App. No. 19/739,419 | 63/953,563 | 63/953,842
# MODIFICATION PROHIBITED. USE AS SHIPPED.

"""
Phase 6 — Drift Veto.

The last gate before output. Defense-in-depth over the kept insight set: even
if an earlier phase let drift through, nothing reaches output that violated
discipline. Removed items are logged separately — drift is recorded, never
silently dropped.

Vetoes (any one removes the insight):
  - cross-class substitution   — substitute left its class (Rule A breach)
  - kept absurd-as-truth        — a METAPHOR / C-absurd insight marked kept
                                  (also the undeclared cross-domain-metaphor case)
  - invented quantitative claim — a number the engine fabricated in its own text
  - reference drift             — the insight's noun stands on a reference that
                                  has drifted a tooth or more since the evidence
                                  was read (``reference_drift``, given by the
                                  caller: ``{noun: displacement_in_teeth}``)

On an honest engine run this removes nothing; it exists to catch regressions.

Requires a locked Boundary (Law 0).
"""

from __future__ import annotations

import re
from collections.abc import Mapping, Sequence

from pdm_memory.noun_probe.boundary import BoundaryError
from pdm_memory.noun_probe.contracts import (
    Boundary,
    DriftVetoResult,
    EvidenceTag,
    RelockedInsight,
    VetoRecord,
)
from pdm_memory.noun_probe.lexicon import classify_noun

_NUMBER_RE = re.compile(r"\d")


#: How far a reference has to have drifted, in its own teeth, to veto an insight.
REFERENCE_DRIFT_TEETH = 1.0


def drift_veto(
    relocked: Sequence[RelockedInsight],
    boundary: Boundary,
    *,
    reference_drift: Mapping[str, float] | None = None,
) -> DriftVetoResult:
    if not isinstance(boundary, Boundary):
        raise BoundaryError("Phase 6 requires a locked Boundary (Law 0)")

    kept: list[RelockedInsight] = []
    vetoed: list[VetoRecord] = []
    for insight in relocked:
        if not insight.kept:
            continue  # already excluded from output — not a veto event
        reason = _veto_reason(insight)
        noun = insight.perturbation.source_noun
        moved = abs((reference_drift or {}).get(noun, 0.0))
        if reason is None and moved >= REFERENCE_DRIFT_TEETH:
            vetoed.append(
                VetoRecord(
                    subject=noun,
                    reason="reference_drift",
                    kind="reference_drift",
                    displacement_teeth=moved,
                )
            )
        elif reason is None:
            kept.append(insight)
        else:
            vetoed.append(VetoRecord(subject=noun, reason=reason))
    return DriftVetoResult(kept=tuple(kept), vetoed=tuple(vetoed))


def _veto_reason(insight: RelockedInsight) -> str | None:
    pert = insight.perturbation
    substitute_class, _, _ = classify_noun(pert.substitute)
    if substitute_class is not pert.noun_class:
        return "cross-class substitution"
    if insight.evidence is EvidenceTag.METAPHOR or pert.rule == "C-absurd":
        return "kept absurd-as-truth"
    if _NUMBER_RE.search(insight.reason) or _NUMBER_RE.search(pert.note):
        return "invented quantitative claim"
    return None
