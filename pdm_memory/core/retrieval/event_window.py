# © 2026 Westfield Innovations LLC. Patent Pending.
# U.S. App. No. 19/739,419 | 63/953,563 | 63/953,842
# MODIFICATION PROHIBITED. USE AS SHIPPED.

"""Relative and absolute event-window parsing for temporal recall."""

from __future__ import annotations

import re
from datetime import datetime, timedelta, timezone

from pdm_memory.core.signature import MemoryHit


def parse_absolute_month_year_window(text: str) -> tuple[datetime, datetime] | None:
    """Parse ``January 2024`` / ``Dec 2026`` / ``2024-01`` into ``[start, end)``."""
    months = {
        "jan": 1, "january": 1, "feb": 2, "february": 2, "mar": 3, "march": 3,
        "apr": 4, "april": 4, "may": 5, "jun": 6, "june": 6, "jul": 7, "july": 7,
        "aug": 8, "august": 8, "sep": 9, "sept": 9, "september": 9,
        "oct": 10, "october": 10, "nov": 11, "november": 11, "dec": 12, "december": 12,
    }
    named = re.search(
        r"\b(" + "|".join(sorted(months, key=len, reverse=True)) + r")\s+(20\d{2})\b",
        text,
    )
    if named is not None:
        month = months[named.group(1)]
        year = int(named.group(2))
        start = datetime(year, month, 1, tzinfo=timezone.utc)
        end_month = month + 1
        end_year = year
        if end_month == 13:
            end_month = 1
            end_year += 1
        return start, datetime(end_year, end_month, 1, tzinfo=timezone.utc)

    iso = re.search(r"\b(20\d{2})-(0[1-9]|1[0-2])\b", text)
    if iso is not None:
        year = int(iso.group(1))
        month = int(iso.group(2))
        start = datetime(year, month, 1, tzinfo=timezone.utc)
        end_month = month + 1
        end_year = year
        if end_month == 13:
            end_month = 1
            end_year += 1
        return start, datetime(end_year, end_month, 1, tzinfo=timezone.utc)
    return None


def parse_relative_event_window(
    query: str | None,
    now: datetime,
) -> tuple[datetime, datetime] | None:
    """
    Map temporal phrases in *query* to a half-open UTC window
    ``[start, end)`` for ``t_event_at`` prioritization.
    """
    if not query or not query.strip():
        return None
    if now.tzinfo is None:
        now = now.replace(tzinfo=timezone.utc)

    text = query.lower()
    day0 = datetime(now.year, now.month, now.day, tzinfo=timezone.utc)

    absolute = parse_absolute_month_year_window(text)
    if absolute is not None:
        return absolute

    if re.search(r"\blast\s+year\b", text):
        start = day0 - timedelta(days=365)
        return start, day0 + timedelta(days=1)
    if re.search(r"\blast\s+month\b", text):
        start = day0 - timedelta(days=30)
        return start, day0 + timedelta(days=1)
    if re.search(r"\blast\s+week\b|\bpast\s+week\b", text):
        start = day0 - timedelta(days=7)
        return start, day0 + timedelta(days=1)
    if re.search(r"\bthis\s+week\b", text):
        start = day0 - timedelta(days=day0.weekday())
        return start, day0 + timedelta(days=1)
    if re.search(r"\byesterday\b", text):
        start = day0 - timedelta(days=1)
        return start, day0
    if re.search(r"\btomorrow\b", text):
        start = day0 + timedelta(days=1)
        return start, day0 + timedelta(days=2)
    if re.search(r"\btoday\b", text):
        return day0, day0 + timedelta(days=1)
    return None


def hit_in_event_window(
    hit: MemoryHit,
    window: tuple[datetime, datetime] | None,
) -> bool:
    if window is None:
        return False
    event_at = hit.t_event_at
    if event_at is None:
        return False
    if event_at.tzinfo is None:
        event_at = event_at.replace(tzinfo=timezone.utc)
    start, end = window
    return start <= event_at < end


class EventWindowMixin:
    _parse_absolute_month_year_window = staticmethod(parse_absolute_month_year_window)
    _parse_relative_event_window = staticmethod(parse_relative_event_window)
    _hit_in_event_window = staticmethod(hit_in_event_window)
