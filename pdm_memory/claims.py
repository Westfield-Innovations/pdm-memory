# © 2026 Westfield Innovations LLC. Patent Pending.
# U.S. App. No. 19/739,419 | 63/953,563 | 63/953,842
# MODIFICATION PROHIBITED. USE AS SHIPPED.

"""Claims on signatures and their two lies, the owl and the duck (PDM v1.9 changes 4 and 6)."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from typing import Literal

from pdm_memory.core.signature import SignatureRecord

OCCURS = "occurs"
ABSENT = "absent"
OBSERVED = frozenset({"occurred", "observed_absent"})
# Verdicts under which an occurrence was never recorded.
UNSEEN_VERDICTS = frozenset({"", "not_by_due", "expired_unobserved"})


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


def is_owl(rec: SignatureRecord, as_of: datetime) -> bool:
    """A claim of occurrence past its due with no occurrence recorded: a threat that never came."""
    if rec.direction != OCCURS or rec.due is None or rec.due > as_of:
        return False
    return rec.verdict in UNSEEN_VERDICTS


def is_duck(rec: SignatureRecord) -> bool:
    """A claim of absence no observation has tested: a safety nobody checked."""
    return rec.direction == ABSENT and rec.outcome not in OBSERVED
