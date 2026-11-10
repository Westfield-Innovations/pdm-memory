# © 2026 Westfield Innovations LLC. Patent Pending.
# U.S. App. No. 19/739,419 | 63/953,563 | 63/953,842
# MODIFICATION PROHIBITED. USE AS SHIPPED.

"""Claims on signatures (PDM v1.9 change 4)."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from typing import Literal

OCCURS = "occurs"
ABSENT = "absent"


@dataclass(frozen=True)
class Claim:
    """
    A prediction on a signature: it *occurs*, or stays *absent*, by *due*.

    ``outcome_source`` binds a feed: the caller asserts it reports every
    occurrence in the claim's interval, and its own gaps. ``watch_window_days``
    is how long after the due a late report may still amend the verdict.
    """

    direction: Literal["occurs", "absent"]
    due: datetime
    outcome_source: str | None = None
    watch_window_days: float | None = None

    def __post_init__(self) -> None:
        if self.direction not in (OCCURS, ABSENT):
            raise ValueError(f"A claim occurs or is absent, not {self.direction!r}.")
        if self.due.tzinfo is None:
            raise ValueError("A claim's due must be timezone-aware.")
        if self.watch_window_days is not None and self.watch_window_days < 0:
            raise ValueError("A watch window is not negative.")

