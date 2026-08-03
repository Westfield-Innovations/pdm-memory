# © 2026 Westfield Innovations LLC. Patent Pending.
# U.S. App. No. 19/739,419 | 63/953,563 | 63/953,842
# MODIFICATION PROHIBITED. USE AS SHIPPED.

"""Drawer diversity selection for top-k recall."""

from __future__ import annotations

import math

from pdm_memory.core.retrieval.types import NodeCoupling
from pdm_memory.core.signature import MemoryHit


def select_with_diversity(
    ranked: list[tuple[NodeCoupling, MemoryHit]],
    *,
    k: int,
    diversity_bias: float | None,
) -> list[MemoryHit]:
    """
    Cap how many top-k slots one drawer may occupy.

    When ``diversity_bias`` is set and other drawers still have candidates,
    a single drawer cannot take more than ``floor(k * bias)`` slots
    (minimum 1). Remaining slots prefer other drawers by score order.
    If not enough diverse candidates exist, overflow fills by pure score
    so we never return fewer hits than available.
    """
    if k <= 0 or not ranked:
        return []
    if diversity_bias is None:
        return [hit for _, hit in ranked[:k]]

    bias = float(diversity_bias)
    if not math.isfinite(bias):
        raise ValueError("diversity_bias must be a finite float or None")
    bias = max(0.0, min(1.0, bias))
    max_per_drawer = k if bias >= 1.0 else max(1, math.floor(k * bias))

    drawers_in_pool = {(hit.drawer or "general") for _, hit in ranked}
    if len(drawers_in_pool) <= 1:
        return [hit for _, hit in ranked[:k]]

    selected: list[MemoryHit] = []
    counts: dict[str, int] = {}
    overflow: list[MemoryHit] = []

    for _, hit in ranked:
        if len(selected) >= k:
            break
        drawer = hit.drawer or "general"
        used = counts.get(drawer, 0)
        if used < max_per_drawer:
            selected.append(hit)
            counts[drawer] = used + 1
        else:
            overflow.append(hit)

    if len(selected) < k:
        for hit in overflow:
            if len(selected) >= k:
                break
            selected.append(hit)

    return selected


class DiversityMixin:
    _select_with_diversity = staticmethod(select_with_diversity)
