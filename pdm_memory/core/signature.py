# © 2026 Westfield Innovations LLC. Patent Pending.
# U.S. App. No. 19/739,419 | 63/953,563 | 63/953,842
# MODIFICATION PROHIBITED. USE AS SHIPPED.

"""
PDM Core Dataclasses — framework-agnostic signature and result types.
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any, Dict, List, Optional


# ---------------------------------------------------------------------------
# Storage records
# ---------------------------------------------------------------------------


@dataclass
class SignatureRecord:
    """
    One PDM memory — the unit stored in SQLite or the cloud.

    All fields mirror the Signature model in companion_api/pdm/models.py
    but carry no Django dependency.
    """

    # Identity
    id: str = field(default_factory=lambda: str(uuid.uuid4()))
    user: str = "default"

    # Content
    compressed_fact: str = ""          # The memory text (≤ 500 chars)
    source: str = "chat"               # Where it came from: chat, manual, csv, …

    # PDM pressure fields
    p_magnitude: float = 50.0         # Importance (0–100)
    t_persistence: float = 30.0       # Days it stays relevant
    phase_privilege: float = 1.0      # Nesting multiplier
    effective_spike: Optional[float] = None  # Computed: p × t/30 × phase

    # Classification
    intent_tags: List[str] = field(default_factory=list)
    question_regime: str = "neutral"
    domain: str = "insight"            # Inferred from tags

    # Retrieval tracking
    retrieval_count: int = 0
    last_retrieved: Optional[datetime] = None
    created_at: Optional[datetime] = None

    # Validation coefficient fields
    validation_prediction_total: int = 0
    validation_prediction_correct: int = 0

    # Decay config (schema compat — NOT used by pressure decay; half-life is canonical)
    decay_rate: float = 0.9            # Legacy field; ignored by P_effective / decay()

    # Optional: temporal deadline
    t_deadline: Optional[datetime] = None
    urgency_rate: float = 2.0

    # Optional: drawer (category) name
    drawer_domain: str = "general"

    # Extra metadata
    metadata: Dict[str, Any] = field(default_factory=dict)

    def __post_init__(self) -> None:
        if self.created_at is None:
            self.created_at = datetime.utcnow()
        if self.effective_spike is None:
            from pdm_memory.core.math import calculate_effective_spike
            self.effective_spike = calculate_effective_spike(
                self.p_magnitude, self.t_persistence, self.phase_privilege
            )


# ---------------------------------------------------------------------------
# Retrieval results
# ---------------------------------------------------------------------------


@dataclass
class MemoryHit:
    """
    A single memory returned by mem.recall().

    Carries both the raw record fields and the computed retrieval metrics
    so callers can rank, filter, or explain results.
    """

    # The record
    id: str
    text: str                   # alias for compressed_fact
    source: str
    drawer: str                 # drawer_domain

    # Live pressure metrics
    pressure: float             # p_effective at retrieval time
    p_raw: float                # raw stored p_magnitude
    p_effective: float          # same as pressure (explicit name)

    # Decay/retrieval info
    decay_factor: float
    intent_weight: float
    v_coefficient: float
    quality: float
    last_reinforced: Optional[datetime]
    retrieval_count: int

    # Classification
    intent_tags: List[str]
    domain: str

    # Resonance detail (TAS engine output)
    coupling_score: float = 0.0
    tag_overlap: float = 0.0
    domain_match: float = 0.0
    regime_match: float = 0.0
    pressure_proximity: float = 0.0

    # Temporal (if deadline exists)
    e_temporal: Optional[float] = None
    is_urgent: bool = False

    @classmethod
    def from_record(
        cls,
        record: SignatureRecord,
        p_effective: float,
        decay_factor: float,
        intent_weight: float,
        v_coefficient: float,
        coupling_score: float = 0.0,
        tag_overlap: float = 0.0,
        domain_match: float = 0.0,
        regime_match: float = 0.0,
        pressure_proximity: float = 0.0,
    ) -> "MemoryHit":
        return cls(
            id=record.id,
            text=record.compressed_fact,
            source=record.source,
            drawer=record.drawer_domain,
            pressure=p_effective,
            p_raw=record.p_magnitude,
            p_effective=p_effective,
            decay_factor=decay_factor,
            intent_weight=intent_weight,
            v_coefficient=v_coefficient,
            quality=0.80,
            last_reinforced=record.last_retrieved,
            retrieval_count=record.retrieval_count,
            intent_tags=record.intent_tags,
            domain=record.domain,
            coupling_score=coupling_score,
            tag_overlap=tag_overlap,
            domain_match=domain_match,
            regime_match=regime_match,
            pressure_proximity=pressure_proximity,
        )


# ---------------------------------------------------------------------------
# Drawer summary
# ---------------------------------------------------------------------------


@dataclass
class DrawerInfo:
    """Summary of a memory drawer (category)."""
    domain: str
    signature_count: int
    avg_pressure: float
    description: str = ""


# ---------------------------------------------------------------------------
# Explain report
# ---------------------------------------------------------------------------


@dataclass
class ExplainReport:
    """
    Returned by mem.explain(memory_id).
    Shows exactly why a memory has the pressure it has and how it resonated.
    """

    memory_id: str
    compressed_fact: str
    drawer: str
    source: str

    # Raw stored values
    p_magnitude: float
    t_persistence: float
    effective_spike: float
    created_at: Optional[datetime]
    last_retrieved: Optional[datetime]
    retrieval_count: int

    # Computed at explain time
    days_since_retrieved: float
    half_life_days: float
    decay_factor: float
    v_coefficient: float
    intent_weight: Optional[float]   # None if no query was given
    quality: float
    p_effective: float

    # Resonance breakdown
    coupling_score: Optional[float]
    tag_overlap: Optional[float]
    domain_match: Optional[float]
    regime_match: Optional[float]
    pressure_proximity: Optional[float]

    # Intent tags
    intent_tags: List[str]
    domain: str

    def render(self) -> str:
        """Return a human-readable text report."""
        lines = [
            "╔══════════════════════════════════════════════════════",
            "║  PDM Memory Explain Report",
            "╠══════════════════════════════════════════════════════",
            f"║  ID:              {self.memory_id}",
            f"║  Fact:            {self.compressed_fact[:80]}{'…' if len(self.compressed_fact) > 80 else ''}",
            f"║  Drawer:          {self.drawer}",
            f"║  Source:          {self.source}",
            f"║  Tags:            {', '.join(self.intent_tags)}",
            f"║  Domain:          {self.domain}",
            "╠──────────────────────────────────────────────────────",
            "║  Pressure Components:",
            f"║    p_magnitude:    {self.p_magnitude:.2f}",
            f"║    V coefficient:  {self.v_coefficient:.4f}  ({self.retrieval_count} retrievals)",
            f"║    Decay factor:   {self.decay_factor:.4f}  ({self.days_since_retrieved:.1f}d since retrieved, T½={self.half_life_days}d)",
            f"║    Intent weight:  {self.intent_weight if self.intent_weight is not None else 'n/a (no query)'}",
            f"║    Quality:        {self.quality:.2f}",
            "║    ─────────────────────────────",
            f"║    P_effective:    {self.p_effective:.2f}",
            f"║    Eff. spike:     {self.effective_spike:.2f}",
        ]
        if self.coupling_score is not None:
            lines += [
                "╠──────────────────────────────────────────────────────",
                "║  Resonance (TAS coupling):",
                f"║    coupling_score:     {self.coupling_score:.4f}",
                f"║    tag_overlap:        {self.tag_overlap:.4f}",
                f"║    domain_match:       {self.domain_match:.4f}",
                f"║    regime_match:       {self.regime_match:.4f}",
                f"║    pressure_proximity: {self.pressure_proximity:.4f}",
            ]
        lines.append("╚══════════════════════════════════════════════════════")
        return "\n".join(lines)
