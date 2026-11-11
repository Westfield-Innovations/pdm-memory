# © 2026 Westfield Innovations LLC. Patent Pending.
# U.S. App. No. 19/739,419 | 63/953,563 | 63/953,842
# MODIFICATION PROHIBITED. USE AS SHIPPED.

"""
PDM Noun Probe
==============

Controlled semantic-perturbation engine.

Pins each surviving, load-bearing noun to exactly ONE job by disturbing it
under strict constraint, watching where structure breaks, and re-locking the
boundary. Runs behind ``PDM_NOUN_PROBE_ENABLED``. The audit and vision
entry points are in ``pdm_memory.noun_probe.pipeline``.

Public API:
    from pdm_memory.noun_probe import (
        Boundary, BoundaryError, lock_boundary,
        NounClass, StructuralNoun, extract_structural_nouns,
        VerbCoupling, StructuralVerb, VerbCouplingResult, couple_verbs,
        Perturbation, perturb,
        EvidenceTag, RelockedInsight, relock,
        InvarianceSignal, detect_invariance,
        VetoRecord, DriftVetoResult, drift_veto,
        DetectorDecision, should_probe,
        Finding, ProbeReport, run_noun_probe,
        NounProbeConfig, noun_probe_config_from_settings,
        gated_probe, log_noun_probe_run,
    )
"""

from pdm_memory.noun_probe.boundary import BoundaryError, lock_boundary
from pdm_memory.noun_probe.config import (
    NounProbeConfig,
    noun_probe_config_from_settings,
)
from pdm_memory.noun_probe.contracts import (
    Boundary,
    DetectorDecision,
    DriftVetoResult,
    EvidenceTag,
    Finding,
    InvarianceSignal,
    NounClass,
    Perturbation,
    ProbeReport,
    RelockedInsight,
    StructuralNoun,
    StructuralVerb,
    VerbCoupling,
    VerbCouplingResult,
    VetoRecord,
)
from pdm_memory.noun_probe.detector import should_probe
from pdm_memory.noun_probe.drift_veto import drift_veto
from pdm_memory.noun_probe.extraction import extract_structural_nouns
from pdm_memory.noun_probe.invariance import detect_invariance
from pdm_memory.noun_probe.perturbation import perturb
from pdm_memory.noun_probe.relock import relock
from pdm_memory.noun_probe.report import run_noun_probe
from pdm_memory.noun_probe.service import gated_probe, log_noun_probe_run
from pdm_memory.noun_probe.verb_coupling import couple_verbs

__all__ = [
    "Boundary",
    "BoundaryError",
    "DetectorDecision",
    "DriftVetoResult",
    "EvidenceTag",
    "Finding",
    "InvarianceSignal",
    "NounClass",
    "NounProbeConfig",
    "Perturbation",
    "ProbeReport",
    "RelockedInsight",
    "StructuralNoun",
    "StructuralVerb",
    "VerbCoupling",
    "VerbCouplingResult",
    "VetoRecord",
    "couple_verbs",
    "detect_invariance",
    "drift_veto",
    "extract_structural_nouns",
    "gated_probe",
    "lock_boundary",
    "log_noun_probe_run",
    "noun_probe_config_from_settings",
    "perturb",
    "relock",
    "run_noun_probe",
    "should_probe",
]
