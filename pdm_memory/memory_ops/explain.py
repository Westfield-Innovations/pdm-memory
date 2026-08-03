# © 2026 Westfield Innovations LLC. Patent Pending.
# U.S. App. No. 19/739,419 | 63/953,563 | 63/953,842
# MODIFICATION PROHIBITED. USE AS SHIPPED.

"""Explain and record→hit conversion."""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Any, Protocol

from pdm_memory.core.math import (
    calculate_decay_factor,
    calculate_intent_weight,
    calculate_p_effective,
    calculate_v,
    infer_domain,
    resolve_half_life,
)
from pdm_memory.core.signature import ExplainReport, MemoryHit, SignatureRecord


class _ExplainableMemory(Protocol):
    _storage: Any
    _user: str
    _engine: Any

    @staticmethod
    def _days_since(dt: datetime | None, now: datetime) -> float: ...


def record_to_hit(mem: _ExplainableMemory, rec: SignatureRecord) -> MemoryHit:
    """Build a MemoryHit with live decay / P_effective (no query coupling)."""
    now = datetime.now(tz=timezone.utc)
    days_since = mem._days_since(rec.last_retrieved or rec.created_at, now)
    days_since_created = mem._days_since(rec.created_at, now)
    domain = rec.domain or infer_domain(rec.intent_tags)
    half_life = resolve_half_life(domain)
    decay = calculate_decay_factor(
        days_since,
        half_life,
        days_since_created=days_since_created,
        t_persistence=rec.t_persistence,
    )
    v = calculate_v(rec.validation_prediction_correct, rec.validation_prediction_total)
    p_eff = calculate_p_effective(rec.p_magnitude, v, decay, 1.0, 0.80)
    e_temporal, is_urgent = mem._engine._temporal_energy(rec, now)
    return MemoryHit.from_record(
        rec,
        p_eff,
        decay,
        1.0,
        v,
        e_temporal=e_temporal,
        is_urgent=is_urgent,
    )


def explain_memory(
    mem: _ExplainableMemory,
    memory_id: str,
    query: str | None = None,
) -> ExplainReport:
    rec = mem._storage.get(memory_id, user=mem._user)
    if rec is None:
        raise KeyError(f"Memory '{memory_id}' not found for user '{mem._user}'.")

    now = datetime.now(tz=timezone.utc)
    days_since = mem._days_since(rec.last_retrieved or rec.created_at, now)
    days_since_created = mem._days_since(rec.created_at, now)

    domain = rec.domain or infer_domain(rec.intent_tags)
    half_life = resolve_half_life(domain)
    decay = calculate_decay_factor(
        days_since,
        half_life,
        days_since_created=days_since_created,
        t_persistence=rec.t_persistence,
    )
    v = calculate_v(rec.validation_prediction_correct, rec.validation_prediction_total)
    i_weight = calculate_intent_weight(rec.intent_tags, query) if query else None
    p_eff = calculate_p_effective(
        rec.p_magnitude,
        v,
        decay,
        i_weight if i_weight is not None else 1.0,
        0.80,
    )

    coupling_score = tag_overlap = domain_match = regime_match = press_prox = None
    if query:
        hits = mem._engine.recall(records=[rec], query=query, k=1, search_cost=1.0)
        if hits:
            h = hits[0]
            coupling_score = h.coupling_score
            tag_overlap = h.tag_overlap
            domain_match = h.domain_match
            regime_match = h.regime_match
            press_prox = h.pressure_proximity

    return ExplainReport(
        memory_id=memory_id,
        compressed_fact=rec.compressed_fact,
        drawer=rec.drawer_domain,
        source=rec.source,
        p_magnitude=rec.p_magnitude,
        t_persistence=rec.t_persistence,
        effective_spike=rec.effective_spike or 0.0,
        created_at=rec.created_at,
        last_retrieved=rec.last_retrieved,
        retrieval_count=rec.retrieval_count,
        days_since_retrieved=round(days_since, 2),
        half_life_days=half_life,
        decay_factor=round(decay, 4),
        v_coefficient=round(v, 4),
        intent_weight=round(i_weight, 4) if i_weight is not None else None,
        quality=0.80,
        p_effective=round(p_eff, 2),
        coupling_score=coupling_score,
        tag_overlap=tag_overlap,
        domain_match=domain_match,
        regime_match=regime_match,
        pressure_proximity=press_prox,
        intent_tags=rec.intent_tags,
        domain=domain,
    )
