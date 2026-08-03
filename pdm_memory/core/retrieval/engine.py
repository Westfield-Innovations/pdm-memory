# © 2026 Westfield Innovations LLC. Patent Pending.
# U.S. App. No. 19/739,419 | 63/953,563 | 63/953,842
# MODIFICATION PROHIBITED. USE AS SHIPPED.

"""
PDM Retrieval Engine — pure TAS (Threshold-Adjustment Search) without Django.

Three-phase search:
  Phase 1 — Threshold Lowering:    θ_eff = θ_base × (1 - α × search_cost)
  Phase 2 — Impedance Matching:    coupling_score = weighted tag/domain/regime/pressure
  Phase 3 — Reinforcement delta:   Δp = REINF_BASE × log(1 + retrieval_count) × coupling
"""

from __future__ import annotations

import math
from datetime import datetime, timezone

from pdm_memory.core.math import (
    P_MAX,
    calculate_decay_factor,
    calculate_intent_weight,
    calculate_p_effective,
    calculate_temporal_geometry,
    calculate_v,
    infer_domain,
    infer_regime,
    resolve_half_life,
)
from pdm_memory.core.retrieval.constants import (
    ALPHA_DEFAULT,
    AUTO_FIRE_THRESHOLD,
    COUPLING_MIN_DEFAULT,
    DEFAULT_DIVERSITY_BIAS,
    EVENT_WINDOW_RANK_BOOST,
    REINF_BASE,
    TEMPORAL_GATE_BOOST,
    TEMPORAL_RANK_BOOST,
    THETA_BASE_DEFAULT,
    THETA_FLOOR,
    W_DOMAIN,
    W_PRESSURE,
    W_REGIME,
    W_TAGS,
)
from pdm_memory.core.retrieval.diversity import DiversityMixin
from pdm_memory.core.retrieval.event_window import EventWindowMixin
from pdm_memory.core.retrieval.tokenize import TokenizeMixin
from pdm_memory.core.retrieval.torsion import TorsionMixin
from pdm_memory.core.retrieval.types import NodeCoupling
from pdm_memory.core.signature import MemoryHit, SignatureRecord
from pdm_memory.models import AlignmentReport


class RetrievalEngine(
    TokenizeMixin,
    EventWindowMixin,
    DiversityMixin,
    TorsionMixin,
):
    """
    Pure PDM Threshold-Adjustment Search (TAS) engine.

    Standalone — no DB access. Accepts List[SignatureRecord], returns
    List[MemoryHit] ranked by (coupling_score × p_effective) descending.
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

    def recall(
        self,
        records: list[SignatureRecord],
        query: str | None = None,
        k: int = 5,
        search_cost: float = 0.5,
        base_threshold: float = THETA_BASE_DEFAULT,
        target_pressure: float = 50.0,
        domain: str | None = None,
        regime: str | None = None,
        diversity_bias: float | None = DEFAULT_DIVERSITY_BIAS,
    ) -> list[MemoryHit]:
        if not records:
            return []

        now = datetime.now(tz=timezone.utc)
        event_window = self._parse_relative_event_window(query, now)
        theta_eff = self._compute_threshold(base_threshold, search_cost)

        coupled: list[tuple[NodeCoupling, MemoryHit]] = []
        damped: list[NodeCoupling] = []

        query_tags = self._tokenize_query(query) if query else []
        effective_domain = domain or (infer_domain(query_tags) if query_tags else None)
        effective_regime = regime or (infer_regime(query_tags) if query_tags else None)

        for rec in records:
            domain_key = rec.domain or infer_domain(rec.intent_tags)
            half_life = resolve_half_life(domain_key)
            days_since_touch = self._days_since(rec.last_retrieved or rec.created_at, now)
            days_since_created = self._days_since(rec.created_at, now)
            decay = calculate_decay_factor(
                days_since_touch,
                half_life,
                days_since_created=days_since_created,
                t_persistence=rec.t_persistence,
            )
            v = calculate_v(rec.validation_prediction_correct, rec.validation_prediction_total)
            i_weight = calculate_intent_weight(rec.intent_tags, query)
            p_eff = calculate_p_effective(rec.p_magnitude, v, decay, i_weight, quality=0.80)
            e_temporal, is_urgent = self._temporal_energy(rec, now)
            p_gate = p_eff * (1.0 + TEMPORAL_GATE_BOOST * e_temporal)

            if p_gate < theta_eff:
                coupling = self._compute_coupling(
                    rec,
                    query_tags,
                    p_eff,
                    rec.p_magnitude,
                    effective_domain,
                    effective_regime,
                    target_pressure,
                )
                damped.append(coupling)
                continue

            coupling = self._compute_coupling(
                rec,
                query_tags,
                p_eff,
                rec.p_magnitude,
                effective_domain,
                effective_regime,
                target_pressure,
            )

            if coupling.is_coupled:
                if query_tags and self._semantic_query_overlap(rec, query_tags) < 0.12:
                    damped.append(coupling)
                    continue
                hit = MemoryHit.from_record(
                    rec,
                    p_eff,
                    decay,
                    i_weight,
                    v,
                    coupling_score=coupling.coupling_score,
                    tag_overlap=coupling.tag_overlap,
                    domain_match=coupling.domain_match,
                    regime_match=coupling.regime_match,
                    pressure_proximity=coupling.pressure_proximity,
                    e_temporal=e_temporal,
                    is_urgent=is_urgent,
                )
                coupled.append((coupling, hit))
            else:
                damped.append(coupling)

        def _rank_key(item: tuple[NodeCoupling, MemoryHit]) -> tuple[int, float]:
            coupling, hit = item
            in_window = 1 if self._hit_in_event_window(hit, event_window) else 0
            window_boost = EVENT_WINDOW_RANK_BOOST if in_window else 0.0
            score = (
                coupling.coupling_score
                * hit.p_effective
                * (1.0 + TEMPORAL_RANK_BOOST * (hit.e_temporal or 0.0) + window_boost)
            )
            return (in_window, score)

        coupled.sort(key=_rank_key, reverse=True)

        if event_window is not None:
            in_w = [(c, h) for c, h in coupled if self._hit_in_event_window(h, event_window)]
            out_w = [(c, h) for c, h in coupled if not self._hit_in_event_window(h, event_window)]
            if in_w:
                coupled = in_w + out_w

        return self._select_with_diversity(coupled, k=k, diversity_bias=diversity_bias)

    @staticmethod
    def _temporal_energy(
        rec: SignatureRecord,
        now: datetime,
    ) -> tuple[float, bool]:
        deadline = rec.t_deadline
        if deadline is None:
            return 0.0, False
        if deadline.tzinfo is None:
            deadline = deadline.replace(tzinfo=timezone.utc)
        t_remaining = (deadline - now).total_seconds() / 86400.0
        geo = calculate_temporal_geometry(
            c_base=1.0,
            s_base=1.0,
            p_base=max(0.0, float(rec.p_magnitude or 0.0) / 100.0),
            urgency_rate=max(1.0, float(rec.urgency_rate or 2.0)),
            t_remaining_days=t_remaining,
            persist_days=max(1.0, float(rec.t_persistence or 30.0)),
        )
        return float(geo["e_temporal"]), bool(geo["is_urgent"])

    def compute_reinforcement_delta(
        self,
        p_magnitude: float,
        retrieval_count: int,
        coupling_score: float,
    ) -> float:
        retrieval_count = max(0, int(retrieval_count or 0))
        log_factor = 1.0 + math.log(1.0 + retrieval_count)
        delta = self.reinforcement_base * log_factor * coupling_score
        headroom = max(0.0, P_MAX - float(p_magnitude))
        return min(delta, headroom)

    def _compute_threshold(self, theta_base: float, search_cost: float) -> float:
        cost = max(0.0, min(1.0, search_cost))
        theta_eff = theta_base * (1.0 - self.alpha * cost)
        return max(self.theta_floor, min(theta_base, theta_eff))

    def _compute_coupling(
        self,
        rec: SignatureRecord,
        query_tags: list[str],
        p_eff: float,
        p_raw: float,
        effective_domain: str | None,
        effective_regime: str | None,
        target_pressure: float,
    ) -> NodeCoupling:
        sig_tags = [t.lower() for t in rec.intent_tags]
        cue_tags = [t.lower() for t in query_tags]

        if cue_tags and sig_tags:
            intersection = sum(1 for t in cue_tags if t in sig_tags)
            tag_overlap = intersection / max(len(cue_tags), len(sig_tags))
        elif not cue_tags:
            tag_overlap = 1.0
        else:
            tag_overlap = 0.0

        if effective_domain is None or (rec.domain or "").lower() == effective_domain.lower():
            domain_match = 1.0
        else:
            domain_match = 0.0

        sig_regime = infer_regime(sig_tags)
        if effective_regime is None or sig_regime == effective_regime:
            regime_match = 1.0
        elif sig_regime == "neutral" or effective_regime == "neutral":
            regime_match = 0.5
        else:
            regime_match = 0.0

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
    def _days_since(dt: datetime | None, now: datetime) -> float:
        if dt is None:
            return 0.0
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        return max(0.0, (now - dt).total_seconds() / 86400.0)

    def verify_alignment(
        self,
        records: list[SignatureRecord],
        intent_text: str,
        *,
        min_pressure: float = 60.0,
        k_goals: int = 8,
        torsion_threshold: float = 0.70,
        conflict_threshold: float = 0.40,
    ) -> AlignmentReport:
        from pdm_memory.core.alignment import verify_alignment as run_gaa

        return run_gaa(
            self,
            records,
            intent_text,
            min_pressure=min_pressure,
            k_goals=k_goals,
            torsion_threshold=torsion_threshold,
            conflict_threshold=conflict_threshold,
        )
