# © 2026 Westfield Innovations LLC. Patent Pending.
# U.S. App. No. 19/739,419 | 63/953,563 | 63/953,842
# MODIFICATION PROHIBITED. USE AS SHIPPED.

"""
Noun Probe contracts — typed payloads for the controlled-perturbation engine.

Pure Python. No Django, no DB. Frozen, slotted dataclasses so the resolver
stage can run standalone and cheaply (engine fires only on GO).
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import Enum


class NounClass(str, Enum):
    """
    The five structural noun classes. There is deliberately NO metaphor
    class — every surviving noun is forced into exactly one functional role.
    """

    CONSTRAINT = "constraint"  # limits, forces, boundaries
    RESOURCE = "resource"  # time, money, energy, memory
    ACTOR = "actor"  # agents, systems
    STATE = "state"  # phase, stability, fidelity
    FAILURE = "failure"  # loss, decay, instability


class EvidenceTag(str, Enum):
    """Phase 4 evidence tag. Metaphor is a creative artifact, not truth."""

    KNOWN = "known"  # structural truth — survived re-lock
    HYPOTHESIS = "hypothesis"  # plausible boundary condition — needs evidence
    METAPHOR = "metaphor"  # creative artifact — dropped unless evidenced


@dataclass(frozen=True, slots=True)
class Boundary:
    """
    Phase 0 boundary declaration.

    Law 0: no perturbation may run without a fully declared boundary —
    perturbation without a boundary is just drift.

    Attributes:
        system:        What is under analysis (the object the probe disturbs).
        domain:        The field the system lives in (engineering, legal, ...).
        scale:         The granularity / size the analysis is locked to.
        not_analyzed:  Explicit out-of-scope set. An empty set means the
                       boundary was never really drawn — `lock_boundary`
                       rejects it.
    """

    system: str
    domain: str
    scale: str
    not_analyzed: tuple[str, ...]


@dataclass(frozen=True, slots=True)
class StructuralNoun:
    """
    A load-bearing noun extracted by FUNCTION (not raw frequency).

    Attributes:
        text:         The lowercased noun token.
        noun_class:   One of the five functional classes.
        salience:     Function-first score (role + spread + damped count
                      weighted by class confidence). Higher = more load-bearing.
        occurrences:  Raw count across the document (diagnostic only).
        sections:     Section indices the noun appears in (spread evidence).
        confidence:   Classification confidence [0,1]: lexicon hit (1.0),
                      suffix rule (0.6), proper noun (0.4), role-only default (0.3).
    """

    text: str
    noun_class: NounClass
    salience: float
    occurrences: int
    sections: tuple[int, ...]
    confidence: float


@dataclass(frozen=True, slots=True)
class VerbCoupling:
    """
    Phase 2 verb signature for a single noun.

    Attributes:
        noun:          The noun token.
        noun_class:    Its functional class.
        primary_verb:  What it DOES (active / subject role), lemma or None.
        passive_verb:  What is DONE to it (object / "is VERBed"), lemma or None.
        implied_verb:  The hidden assumption — the class-level structural verb
                       (never observed, always present). Surfaces the silent
                       job the noun is assumed to perform.
    """

    noun: str
    noun_class: NounClass
    primary_verb: str | None
    passive_verb: str | None
    implied_verb: str


@dataclass(frozen=True, slots=True)
class StructuralVerb:
    """
    A verb that couples to >=2 distinct nouns — the system-dynamics layer.

    Attributes:
        verb:          The verb lemma.
        coupled_nouns: Distinct nouns it binds (sorted).
        is_canonical:  True for the spec's named set
                       (accumulate, degrade, shift, relocate, partition).
    """

    verb: str
    coupled_nouns: tuple[str, ...]
    is_canonical: bool


@dataclass(frozen=True, slots=True)
class VerbCouplingResult:
    """Phase 2 output: per-noun couplings + the cross-noun structural verbs."""

    couplings: tuple[VerbCoupling, ...]
    structural_verbs: tuple[StructuralVerb, ...]


@dataclass(frozen=True, slots=True)
class Perturbation:
    """
    Phase 3 result of one within-class functional substitution.

    Attributes:
        source_noun:  The original noun.
        noun_class:   Its class (substitution stays inside it — Rule A).
        verb:         The verb frame tested.
        substitute:   The same-class functional stand-in (Rule B).
        survived:     True if the structure held → invariant pattern.
                      False → boundary condition / foreign-class job revealed.
        rule:         "B-invariant" | "B-boundary" | "C-absurd".
        note:         What the perturbation revealed.
    """

    source_noun: str
    noun_class: NounClass
    verb: str
    substitute: str
    survived: bool
    rule: str
    note: str


@dataclass(frozen=True, slots=True)
class RelockedInsight:
    """
    Phase 4 verdict on one perturbation after re-applying discipline.

    Attributes:
        perturbation:  The Phase 3 perturbation under review.
        evidence:      KNOWN / HYPOTHESIS / METAPHOR.
        kept:          True if it survives re-lock as a real signal.
                       False → creative artifact (not structural truth).
        reason:        Why this verdict was assigned.
    """

    perturbation: Perturbation
    evidence: EvidenceTag
    kept: bool
    reason: str


@dataclass(frozen=True, slots=True)
class InvarianceSignal:
    """
    Phase 5 finding — where the real signal lives.

    Attributes:
        kind:     "invariant" | "boundary_inconsistency"
                  | "constraint_relocation" | "misframed".
        subject:  The verb or noun the signal is about.
        detail:   Human-readable description.
    """

    kind: str
    subject: str
    detail: str


@dataclass(frozen=True, slots=True)
class VetoRecord:
    """Phase 6 record of one removed insight (kept separate from output)."""

    subject: str
    reason: str
    #: ``discipline`` for a breach inside the probe's own run; ``reference_drift``
    #: for an insight whose noun stands on a reference that has since drifted.
    kind: str = "discipline"
    #: For ``reference_drift``: how far the reference drifted, in teeth.
    displacement_teeth: float | None = None


@dataclass(frozen=True, slots=True)
class DriftVetoResult:
    """
    Phase 6 output: the surviving insights plus the veto log.

    Attributes:
        kept:    Insights that passed the final discipline check.
        vetoed:  What was removed and why (drift evidence, not silent loss).
    """

    kept: tuple[RelockedInsight, ...]
    vetoed: tuple[VetoRecord, ...]


@dataclass(frozen=True, slots=True)
class DetectorDecision:
    """
    Cheap gate verdict — runs on every input, fires the engine only on GO.

    Attributes:
        should_run:  True → run the (expensive) Noun Probe engine.
        score:       Cheap confidence signal (keyword hits + length factor).
        categories:  High-stakes categories matched
                     (patent / spec / governance / financial).
        reasons:     Why this verdict was reached.
    """

    should_run: bool
    score: float
    categories: tuple[str, ...]
    reasons: tuple[str, ...]


@dataclass(frozen=True, slots=True)
class Finding:
    """
    One headline recommendation in the probe output.

    Attributes:
        kind:      "split" | "broaden" | "invariant" | "misframed".
        term:      The noun or verb the finding is about.
        detail:    Human-readable recommendation.
        evidence:  KNOWN / HYPOTHESIS / METAPHOR.
    """

    kind: str
    term: str
    detail: str
    evidence: EvidenceTag

    def to_dict(self) -> dict[str, str]:
        return {
            "kind": self.kind,
            "term": self.term,
            "detail": self.detail,
            "evidence": self.evidence.value,
        }


@dataclass(frozen=True, slots=True)
class ProbeReport:
    """
    Full Noun Probe output: the short findings list + the veto log.

    Attributes:
        boundary:    The locked boundary the run operated under.
        findings:    Ordered, de-duplicated headline findings.
        vetoed:      What Phase 6 removed (drift evidence).
        noun_count:  How many structural nouns were probed.
    """

    boundary: Boundary
    findings: tuple[Finding, ...]
    vetoed: tuple[VetoRecord, ...]
    noun_count: int

    def to_dict(self) -> dict[str, object]:
        return {
            "boundary": {
                "system": self.boundary.system,
                "domain": self.boundary.domain,
                "scale": self.boundary.scale,
                "not_analyzed": list(self.boundary.not_analyzed),
            },
            "findings": [f.to_dict() for f in self.findings],
            "vetoed": [
                {
                    "subject": v.subject,
                    "reason": v.reason,
                    **(
                        {}
                        if v.kind == "discipline"
                        else {
                            "kind": v.kind,
                            "displacement_teeth": v.displacement_teeth,
                        }
                    ),
                }
                for v in self.vetoed
            ],
            "noun_count": self.noun_count,
        }
