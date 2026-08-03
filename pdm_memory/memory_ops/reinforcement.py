# © 2026 Westfield Innovations LLC. Patent Pending.
# U.S. App. No. 19/739,419 | 63/953,563 | 63/953,842
# MODIFICATION PROHIBITED. USE AS SHIPPED.

"""Recall reinforcement and torsion V-penalty storage writes."""

from __future__ import annotations

import logging
from datetime import datetime, timezone
from typing import Any, Protocol

from pdm_memory.core.math import calculate_effective_spike
from pdm_memory.core.signature import MemoryHit
from pdm_memory.models import TorsionReport
from pdm_memory.storage.base import BaseStorage

logger = logging.getLogger(__name__)


class _ReinforcementEngine(Protocol):
    def compute_reinforcement_delta(
        self,
        p_magnitude: float,
        retrieval_count: int,
        coupling_score: float,
    ) -> float: ...


def apply_torsion_v_penalty(
    storage: BaseStorage,
    reports: list[TorsionReport],
    *,
    user: str,
) -> None:
    """Record a validation miss on each signature involved in high torsion."""
    affected: set[str] = set()
    for report in reports:
        affected.add(report.signature_a_id)
        affected.add(report.signature_b_id)

    batch_updates: list[tuple[str, dict[str, Any]]] = []
    for sig_id in affected:
        rec = storage.get(sig_id, user=user)
        if rec is None:
            continue
        new_total = int(rec.validation_prediction_total or 0) + 1
        batch_updates.append(
            (
                sig_id,
                {"validation_prediction_total": new_total},
            )
        )
    if batch_updates:
        try:
            storage.update_batch(batch_updates, user=user)
        except Exception as e:
            logger.warning("[PDM] torsion V penalty batch failed: %s", e)


def apply_reinforcement(
    storage: BaseStorage,
    engine: _ReinforcementEngine,
    hits: list[MemoryHit],
    *,
    user: str,
) -> None:
    """Write retrieval reinforcement back to storage for all hits."""
    now = datetime.now(tz=timezone.utc)
    batch_updates: list[tuple[str, dict[str, Any]]] = []
    for hit in hits:
        try:
            rec = storage.get(hit.id, user=user)
            if rec is None:
                continue
            delta = engine.compute_reinforcement_delta(
                rec.p_magnitude, rec.retrieval_count, hit.coupling_score
            )
            new_p = min(100.0, rec.p_magnitude + delta)
            new_spike = calculate_effective_spike(new_p, rec.t_persistence, rec.phase_privilege)
            batch_updates.append(
                (
                    hit.id,
                    {
                        "p_magnitude": new_p,
                        "effective_spike": new_spike,
                        "retrieval_count": (rec.retrieval_count or 0) + 1,
                        "last_retrieved": now,
                    },
                )
            )
        except Exception as e:
            logger.warning("[PDM] reinforcement check failed for %s: %s", hit.id, e)

    if batch_updates:
        try:
            storage.update_batch(batch_updates, user=user)
        except Exception as e:
            logger.warning("[PDM] reinforcement batch update failed: %s", e)
