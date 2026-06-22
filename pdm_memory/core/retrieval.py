"""
PDM Retrieval Engine — pure TAS (Threshold-Adjustment Search) without Django.

Ported from companion_api/pdm/threshold_search/engine.py.
All Django ORM calls removed. Accepts List[SignatureRecord] directly.

Three-phase search:
  Phase 1 — Threshold Lowering:    θ_eff = θ_base × (1 - α × search_cost)
  Phase 2 — Impedance Matching:    coupling_score = weighted tag/domain/regime/pressure
  Phase 3 — Reinforcement delta:   Δp = REINF_BASE × log(1 + retrieval_count) × coupling
"""

from __future__ import annotations

import re
import math
import logging
from dataclasses import dataclass, field, replace
from datetime import datetime, timezone
from typing import List, Optional

from pdm_memory.core.math import (
    DOMAIN_HALF_LIVES,
    DEFAULT_HALF_LIFE,
    P_MAX,
    calculate_decay_factor,
    calculate_intent_weight,
    calculate_p_effective,
    calculate_v,
    infer_domain,
    infer_regime,
    calculate_incremental_decay,
)
from pdm_memory.core.signature import MemoryHit, SignatureRecord

logger = logging.getLogger(__name__)

WORD_PATTERN = re.compile(r"\b[a-zA-Z]{3,}\b")

# ---------------------------------------------------------------------------
# TAS constants (mirrors threshold_search/engine.py)
# ---------------------------------------------------------------------------

ALPHA_DEFAULT: float = 0.7          # Threshold-lowering aggressiveness
THETA_FLOOR: float = 5.0            # Absolute minimum effective threshold
THETA_BASE_DEFAULT: float = 30.0    # Default base threshold

COUPLING_MIN_DEFAULT: float = 0.3   # Minimum coupling score to count as "coupled"

W_TAGS: float = 0.50
W_DOMAIN: float = 0.20
W_REGIME: float = 0.15
W_PRESSURE: float = 0.15

AUTO_FIRE_THRESHOLD: float = 85.0
REINF_BASE: float = 2.0


# ---------------------------------------------------------------------------
# Internal result types
# ---------------------------------------------------------------------------


@dataclass
class NodeCoupling:
    signature_id: str
    p_effective: float
    p_magnitude_raw: float
    coupling_score: float
    is_coupled: bool
    auto_fire_eligible: bool
    reinforcement_delta: float = 0.0
    tag_overlap: float = 0.0
    domain_match: float = 0.0
    regime_match: float = 0.0
    pressure_proximity: float = 0.0


@dataclass
class RetrievalResult:
    """Full result from the TAS retrieval engine."""
    found: bool
    threshold_used: float
    base_threshold: float
    search_cost: float
    coupled_nodes: List[NodeCoupling] = field(default_factory=list)
    damped_nodes: List[NodeCoupling] = field(default_factory=list)
    top_node: Optional[NodeCoupling] = None
    total_scanned: int = 0
    reinforced_count: int = 0


# ---------------------------------------------------------------------------
# RetrievalEngine
# ---------------------------------------------------------------------------


class RetrievalEngine:
    """
    Pure PDM Threshold-Adjustment Search (TAS) engine.

    Standalone — no DB access. Accepts List[SignatureRecord], returns
    List[MemoryHit] ranked by (coupling_score × p_effective) descending.

    Usage:
        engine = RetrievalEngine()
        hits = engine.recall(
            records=my_signatures,
            query="how should I format the answer?",
            k=5,
        )
    """

    def __init__(
        self,
        alpha: float = ALPHA_DEFAULT,
        theta_floor: float = THETA_FLOOR,
        coupling_min: float = COUPLING_MIN_DEFAULT,
        auto_fire_threshold: float = AUTO_FIRE_THRESHOLD,
        reinforcement_base: float = REINF_BASE,
    ) -> None:
        self.alpha = max(0.0, min(1.0, alpha))
        self.theta_floor = max(0.0, theta_floor)
        self.coupling_min = max(0.0, min(1.0, coupling_min))
        self.auto_fire_threshold = auto_fire_threshold
        self.reinforcement_base = reinforcement_base

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    def recall(
        self,
        records: List[SignatureRecord],
        query: Optional[str] = None,
        k: int = 5,
        search_cost: float = 0.5,   # 0=TIGHT, 1=LOOSE
        base_threshold: float = THETA_BASE_DEFAULT,
        target_pressure: float = 50.0,
        domain: Optional[str] = None,
        regime: Optional[str] = None,
    ) -> List[MemoryHit]:
        """
        Retrieve top-k memories from a list of SignatureRecords.

        Args:
            records:         All candidate SignatureRecords.
            query:           Natural-language recall query.
            k:               Maximum number of hits to return.
            search_cost:     0.0 = strict, 1.0 = very loose threshold.
            base_threshold:  Starting pressure threshold (default 30).
            target_pressure: Pressure proximity anchor for coupling.
            domain:          Optional domain filter (None = all).
            regime:          Optional regime filter (None = all).

        Returns:
            List[MemoryHit] sorted by p_effective descending, length ≤ k.
        """
        if not records:
            return []

        now = datetime.now(tz=timezone.utc)

        # Phase 1: threshold lowering
        theta_eff = self._compute_threshold(base_threshold, search_cost)

        coupled: List[tuple[NodeCoupling, MemoryHit]] = []
        damped: List[NodeCoupling] = []

        # Infer domain/regime from query if not specified
        query_tags = self._tokenize_query(query) if query else []
        effective_domain = domain or (infer_domain(query_tags) if query_tags else None)
        effective_regime = regime or (infer_regime(query_tags) if query_tags else None)

        for rec in records:
            # Apply incremental decay at recall time (Task 1.4)
            rec = self._apply_incremental_decay(rec, now)

            # Compute live pressure
            domain_key = rec.domain or infer_domain(rec.intent_tags)
            half_life = DOMAIN_HALF_LIVES.get(domain_key, DEFAULT_HALF_LIFE)
            days_since = self._days_since(rec.last_retrieved or rec.created_at, now)
            decay = calculate_decay_factor(days_since, half_life)
            v = calculate_v(rec.validation_prediction_correct, rec.validation_prediction_total)
            i_weight = calculate_intent_weight(rec.intent_tags, query)
            p_eff = calculate_p_effective(
                rec.p_magnitude, v, decay, i_weight, quality=0.80
            )

            # Phase 1 gate
            if p_eff < theta_eff:
                coupling = self._compute_coupling(
                    rec, query_tags, p_eff, rec.p_magnitude,
                    effective_domain, effective_regime, target_pressure,
                )
                damped.append(coupling)
                continue

            # Phase 2: impedance matching
            coupling = self._compute_coupling(
                rec, query_tags, p_eff, rec.p_magnitude,
                effective_domain, effective_regime, target_pressure,
            )

            if coupling.is_coupled:
                hit = MemoryHit.from_record(
                    rec, p_eff, decay, i_weight, v,
                    coupling_score=coupling.coupling_score,
                    tag_overlap=coupling.tag_overlap,
                    domain_match=coupling.domain_match,
                    regime_match=coupling.regime_match,
                    pressure_proximity=coupling.pressure_proximity,
                )
                coupled.append((coupling, hit))
            else:
                damped.append(coupling)

        # Sort by coupling_score × p_effective descending
        coupled.sort(key=lambda t: t[0].coupling_score * t[1].p_effective, reverse=True)

        return [hit for _, hit in coupled[:k]]

    def compute_reinforcement_delta(
        self,
        p_magnitude: float,
        retrieval_count: int,
        coupling_score: float,
    ) -> float:
        """
        Δp = REINF_BASE × (1 + log(1 + retrieval_count)) × coupling_score
        Capped at remaining headroom to P_MAX.
        """
        retrieval_count = max(0, int(retrieval_count or 0))
        log_factor = 1.0 + math.log(1.0 + retrieval_count)
        delta = self.reinforcement_base * log_factor * coupling_score
        headroom = max(0.0, P_MAX - float(p_magnitude))
        return min(delta, headroom)

    # ------------------------------------------------------------------
    # Private helpers
    # ------------------------------------------------------------------

    def _compute_threshold(self, theta_base: float, search_cost: float) -> float:
        cost = max(0.0, min(1.0, search_cost))
        theta_eff = theta_base * (1.0 - self.alpha * cost)
        return max(self.theta_floor, min(theta_base, theta_eff))

    def _compute_coupling(
        self,
        rec: SignatureRecord,
        query_tags: List[str],
        p_eff: float,
        p_raw: float,
        effective_domain: Optional[str],
        effective_regime: Optional[str],
        target_pressure: float,
    ) -> NodeCoupling:
        sig_tags = [t.lower() for t in rec.intent_tags]
        cue_tags = [t.lower() for t in query_tags]

        # Tag overlap
        if cue_tags and sig_tags:
            intersection = sum(1 for t in cue_tags if t in sig_tags)
            tag_overlap = intersection / max(len(cue_tags), len(sig_tags))
        elif not cue_tags:
            tag_overlap = 1.0
        else:
            tag_overlap = 0.0

        # Domain match
        if effective_domain is None:
            domain_match = 1.0
        elif (rec.domain or "").lower() == effective_domain.lower():
            domain_match = 1.0
        else:
            domain_match = 0.0

        # Regime match
        sig_regime = infer_regime(sig_tags)
        if effective_regime is None:
            regime_match = 1.0
        elif sig_regime == effective_regime:
            regime_match = 1.0
        elif sig_regime == "neutral" or effective_regime == "neutral":
            regime_match = 0.5
        else:
            regime_match = 0.0

        # Pressure proximity
        pressure_diff = abs(p_raw - target_pressure)
        pressure_proximity = max(0.0, 1.0 - pressure_diff / 100.0)

        coupling_score = (
            W_TAGS * tag_overlap
            + W_DOMAIN * domain_match
            + W_REGIME * regime_match
            + W_PRESSURE * pressure_proximity
        )
        coupling_score = max(0.0, min(1.0, coupling_score))

        return NodeCoupling(
            signature_id=rec.id,
            p_effective=round(p_eff, 3),
            p_magnitude_raw=round(p_raw, 3),
            coupling_score=round(coupling_score, 4),
            is_coupled=coupling_score >= self.coupling_min,
            auto_fire_eligible=p_raw >= self.auto_fire_threshold,
            tag_overlap=round(tag_overlap, 4),
            domain_match=round(domain_match, 4),
            regime_match=round(regime_match, 4),
            pressure_proximity=round(pressure_proximity, 4),
        )

    @staticmethod
    def _apply_incremental_decay(
        rec: SignatureRecord,
        now: datetime,
    ) -> SignatureRecord:
        """
        Apply Task 1.4 incremental decay on read.
        Mutates a copy of the record (does not touch storage).
        """
        if rec.created_at is None:
            return rec

        created = rec.created_at
        if created.tzinfo is None:
            created = created.replace(tzinfo=timezone.utc)

        days_elapsed = (now - created).total_seconds() / 86400.0
        new_p, new_spike = calculate_incremental_decay(
            rec.p_magnitude,
            days_elapsed,
            rec.t_persistence,
            rec.phase_privilege,
            rec.decay_rate,
        )
        # Return a shallow copy with updated pressure (don't write to DB here)
        return replace(
            rec,
            p_magnitude=new_p,
            effective_spike=new_spike,
        )

    @staticmethod
    def _days_since(dt: Optional[datetime], now: datetime) -> float:
        if dt is None:
            return 0.0
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        return max(0.0, (now - dt).total_seconds() / 86400.0)

    @staticmethod
    def _tokenize_query(query: str) -> List[str]:
        """Extract meaningful tokens from a query string for tag matching."""
        words = WORD_PATTERN.findall(query.lower())
        stopwords = {
            "the", "and", "for", "how", "what", "that", "this", "with",
            "are", "was", "not", "can", "will", "from", "have", "been",
            "should", "would", "could", "which", "when", "where",
        }
        return [w for w in words if w not in stopwords]
