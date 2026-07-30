# © 2026 Westfield Innovations LLC. Patent Pending.
# U.S. App. No. 19/739,419 | 63/953,563 | 63/953,842
# MODIFICATION PROHIBITED. USE AS SHIPPED.

"""CSV export helpers for PDM signatures — backup and spreadsheet analysis."""

from __future__ import annotations

import csv
from datetime import datetime, timezone
from pathlib import Path
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from pdm_memory.storage.base import BaseStorage


def _dt_to_iso(value: datetime | None) -> str:
    if value is None:
        return ""
    if value.tzinfo is None:
        value = value.replace(tzinfo=timezone.utc)
    return value.isoformat()


def _tags_to_cell(tags: list[str] | None) -> str:
    return ", ".join(tag for tag in (tags or []) if tag)


def export_signatures_csv(
    target: BaseStorage | Any,
    path: str | Path,
    *,
    user: str = "default",
    limit: int = 100_000,
) -> int:
    """
    Export all signatures for ``user`` to a CSV file.

    Accepts either a ``BaseStorage`` instance or a ``Memory`` object for convenience.

    Columns:
        id, text, p_magnitude, tags, drawer, created_at

    Returns:
        Number of signatures written.
    """
    if hasattr(target, "_storage"):
        storage = target._storage
        user = getattr(target, "_user", user)
    else:
        storage = target

    records = storage.list(user=user, limit=limit)
    out = Path(path)
    out.parent.mkdir(parents=True, exist_ok=True)

    with out.open("w", encoding="utf-8", newline="") as fh:
        writer = csv.DictWriter(
            fh,
            fieldnames=["id", "text", "p_magnitude", "tags", "drawer", "created_at"],
        )
        writer.writeheader()
        for rec in records:
            writer.writerow(
                {
                    "id": rec.id,
                    "text": rec.compressed_fact,
                    "p_magnitude": rec.p_magnitude,
                    "tags": _tags_to_cell(rec.intent_tags),
                    "drawer": rec.drawer_domain,
                    "created_at": _dt_to_iso(rec.created_at),
                }
            )

    return len(records)
