# © 2026 Westfield Innovations LLC. Patent Pending.
# U.S. App. No. 19/739,419 | 63/953,563 | 63/953,842
# MODIFICATION PROHIBITED. USE AS SHIPPED.

"""Keyset pagination for recall candidate loading."""

from __future__ import annotations

from pdm_memory.core.signature import SignatureRecord
from pdm_memory.storage.base import BaseStorage


def load_recall_candidates(
    storage: BaseStorage,
    *,
    user: str,
    min_pressure: float,
    drawer: str | None,
    candidate_limit: int,
    page_size: int,
) -> list[SignatureRecord]:
    """Load recall candidates via keyset pagination instead of one bulk query."""
    if candidate_limit <= 0:
        return []

    page_size = max(1, min(page_size, candidate_limit))
    records: list[SignatureRecord] = []
    cursor_id: str | None = None

    while len(records) < candidate_limit:
        batch_limit = min(page_size, candidate_limit - len(records))
        batch = storage.list(
            user=user,
            limit=batch_limit,
            min_pressure=min_pressure,
            drawer=drawer,
            cursor_id=cursor_id,
        )
        if not batch:
            break
        records.extend(batch)
        if len(batch) < batch_limit:
            break
        cursor_id = batch[-1].id

    return records
