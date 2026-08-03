# © 2026 Westfield Innovations LLC. Patent Pending.
# U.S. App. No. 19/739,419 | 63/953,563 | 63/953,842
# MODIFICATION PROHIBITED. USE AS SHIPPED.

"""Live P_effective decay purge."""

from __future__ import annotations

import logging
from collections.abc import Callable
from datetime import datetime, timezone

from pdm_memory.core.math import (
    DECAY_DELETE_THRESHOLD,
    calculate_decay_factor,
    calculate_p_effective,
    calculate_v,
    infer_domain,
    resolve_half_life,
)
from pdm_memory.storage.base import BaseStorage

logger = logging.getLogger(__name__)


def run_decay(
    storage: BaseStorage,
    *,
    user: str,
    days_since: Callable[[datetime | None, datetime], float],
    dry_run: bool = False,
    limit: int = 10_000,
) -> dict[str, int]:
    """
    Purge memories whose live ``P_effective`` is below the delete threshold.

    Uses the SAME half-life law as ``recall()`` / ``explain()``.
    """
    records = storage.list(user=user, limit=limit)
    now = datetime.now(tz=timezone.utc)
    counts = {"decayed": 0, "deleted": 0, "skipped": 0}

    for rec in records:
        days_since_touch = days_since(rec.last_retrieved or rec.created_at, now)
        days_since_created = days_since(rec.created_at, now)
        domain = rec.domain or infer_domain(rec.intent_tags)
        half_life = resolve_half_life(domain)
        decay = calculate_decay_factor(
            days_since_touch,
            half_life,
            days_since_created=days_since_created,
            t_persistence=rec.t_persistence,
        )
        v = calculate_v(
            rec.validation_prediction_correct,
            rec.validation_prediction_total,
        )
        p_eff = calculate_p_effective(
            rec.p_magnitude, v, decay, intent_weight=1.0, quality=0.80
        )

        if p_eff < DECAY_DELETE_THRESHOLD:
            if not dry_run:
                hard_delete = getattr(storage, "hard_delete", None)
                if callable(hard_delete):
                    hard_delete(rec.id, user=user)
                else:
                    storage.delete(rec.id, user=user)
            counts["deleted"] += 1
        else:
            counts["skipped"] += 1

    logger.info("[PDM] decay() %s | %s", "(dry_run)" if dry_run else "", counts)
    return counts
