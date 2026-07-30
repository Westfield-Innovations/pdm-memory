"""CSV export helpers for PDM signatures."""

from __future__ import annotations

import csv
from datetime import datetime, timezone
from pathlib import Path
from typing import Any


def _dt_to_iso(value: datetime | None) -> str:
    if value is None:
        return ""
    if value.tzinfo is None:
        value = value.replace(tzinfo=timezone.utc)
    return value.isoformat()


def _tags_to_cell(tags: list[str] | None) -> str:
    return ", ".join(tag for tag in (tags or []) if tag)


def export_signatures_csv(mem: Any, path: str | Path) -> int:
    """
    Export all signatures for the current user to a CSV file.

    Columns:
        id, text, p_magnitude, tags, drawer, created_at
    """
    records = mem._storage.list(user=mem._user, limit=100_000)  # noqa: SLF001
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
