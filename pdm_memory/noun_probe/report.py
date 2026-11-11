# © 2026 Westfield Innovations LLC. Patent Pending.
# U.S. App. No. 19/739,419 | 63/953,563 | 63/953,842
# MODIFICATION PROHIBITED. USE AS SHIPPED.

"""
Output: run the full Noun Probe and assemble the findings list.

Orchestrates phases 1–6 over a locked boundary and produces a short,
de-duplicated list of headline findings — terms to split, terms to broaden,
invariants found, misframed problems — each carrying an evidence tag.

Finding sources:
  broaden    ← KNOWN B-invariant insights (term generalises to its class)
  split      ← constraint_relocation signals (noun carries a foreign-class job)
  invariant  ← structural verbs repeating across nouns
  misframed  ← misframed signals (role used against its class)

The cheap detector gate (`should_probe`) is the caller's responsibility — this
is the expensive resolver and assumes a GO decision.

Requires a locked Boundary (Law 0).
"""

from __future__ import annotations

from pdm_memory.noun_probe.boundary import BoundaryError
from pdm_memory.noun_probe.contracts import (
    Boundary,
    DriftVetoResult,
    EvidenceTag,
    Finding,
    InvarianceSignal,
    ProbeReport,
)
from pdm_memory.noun_probe.drift_veto import drift_veto
from pdm_memory.noun_probe.extraction import extract_structural_nouns
from pdm_memory.noun_probe.invariance import detect_invariance
from pdm_memory.noun_probe.perturbation import perturb
from pdm_memory.noun_probe.relock import relock
from pdm_memory.noun_probe.verb_coupling import couple_verbs

_KIND_ORDER = {"split": 0, "broaden": 1, "invariant": 2, "misframed": 3}


def run_noun_probe(
    text: str,
    boundary: Boundary,
    *,
    top_n: int = 12,
    substitutes_per_noun: int = 1,
) -> ProbeReport:
    if not isinstance(boundary, Boundary):
        raise BoundaryError("Noun Probe requires a locked Boundary (Law 0)")

    nouns = extract_structural_nouns(text, boundary, top_n=top_n)
    coupling = couple_verbs(nouns, text, boundary)
    perturbations = perturb(
        coupling, boundary, substitutes_per_noun=substitutes_per_noun
    )
    relocked = relock(perturbations, boundary)
    veto = drift_veto(relocked, boundary)
    invariance = detect_invariance(coupling, relocked, boundary)

    findings = _assemble_findings(veto, invariance)
    return ProbeReport(
        boundary=boundary,
        findings=findings,
        vetoed=veto.vetoed,
        noun_count=len(nouns),
    )


def _assemble_findings(
    veto: DriftVetoResult,
    invariance: tuple[InvarianceSignal, ...],
) -> tuple[Finding, ...]:
    raw: list[Finding] = []
    raw.extend(_broaden(veto))
    raw.extend(_from_signals(invariance))

    seen: set[tuple[str, str]] = set()
    unique: list[Finding] = []
    for finding in raw:
        key = (finding.kind, finding.term)
        if key not in seen:
            seen.add(key)
            unique.append(finding)
    unique.sort(key=lambda f: (_KIND_ORDER.get(f.kind, 99), f.term))
    return tuple(unique)


def _broaden(veto: DriftVetoResult) -> list[Finding]:
    out: list[Finding] = []
    for insight in veto.kept:
        pert = insight.perturbation
        if insight.evidence is EvidenceTag.KNOWN and pert.rule == "B-invariant":
            out.append(
                Finding(
                    kind="broaden",
                    term=pert.source_noun,
                    detail=(
                        f"broaden '{pert.source_noun}' to the "
                        f"{pert.noun_class.value} class — within-class "
                        f"substitution survived ('{pert.substitute}' fits too)"
                    ),
                    evidence=EvidenceTag.KNOWN,
                )
            )
    return out


def _from_signals(invariance: tuple[InvarianceSignal, ...]) -> list[Finding]:
    mapping = {
        "constraint_relocation": ("split", EvidenceTag.HYPOTHESIS),
        "invariant": ("invariant", EvidenceTag.KNOWN),
        "misframed": ("misframed", EvidenceTag.HYPOTHESIS),
    }
    out: list[Finding] = []
    for signal in invariance:
        mapped = mapping.get(signal.kind)
        if mapped is None:
            continue
        kind, evidence = mapped
        out.append(
            Finding(
                kind=kind,
                term=signal.subject,
                detail=signal.detail,
                evidence=evidence,
            )
        )
    return out
