# © 2026 Westfield Innovations LLC. Patent Pending.
# U.S. App. No. 19/739,419 | 63/953,563 | 63/953,842
# MODIFICATION PROHIBITED. USE AS SHIPPED.

"""Claims on signatures, their settlement, and their two lies, the owl and the duck (PDM v1.9 changes 4 to 6).

One claim, one current verdict, amended in place. Grading, by direction:

==================  ===========  ===========
verdict             occurs       absent
==================  ===========  ===========
on_time             correct      incorrect
late                incorrect    correct
observed_absent     incorrect    correct
not_by_due          incorrect    correct
expired_unobserved  —            —
==================  ===========  ===========

A claim adds at most one count to V, from its current verdict; an amendment
moves that count, it never adds a second.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timedelta
from typing import Literal

from pdm_memory.core.signature import SignatureRecord

OCCURS = "occurs"
ABSENT = "absent"
# What was observed, as supplied to validate_claim. Unknown grades nothing.
OCCURRED = "occurred"
OBSERVED_ABSENT = "observed_absent"
UNKNOWN = "unknown"
OBSERVED = frozenset({OCCURRED, OBSERVED_ABSENT})
# The memory's verdict on a claim; "" while open.
ON_TIME = "on_time"
LATE = "late"
NOT_BY_DUE = "not_by_due"
EXPIRED_UNOBSERVED = "expired_unobserved"
# Verdicts under which an occurrence was never recorded.
UNSEEN_VERDICTS = frozenset({"", NOT_BY_DUE, EXPIRED_UNOBSERVED})


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


@dataclass(frozen=True)
class ClaimResult:
    """The verdict a claim now holds, how many signatures a correction re-opened, and whether anything changed."""

    verdict: str
    reopened: int = 0
    changed: bool = True


@dataclass
class SettlementReport:
    """What one settlement pass did: verdicts written by kind, signatures re-opened, claims that failed."""

    settled: dict[str, int] = field(default_factory=dict)
    reopened: int = 0
    failed: list[str] = field(default_factory=list)


def graded(direction: str, verdict: str) -> bool | None:
    """Whether *verdict* makes a claim in *direction* correct; None when it grades nothing."""
    if verdict in ("", EXPIRED_UNOBSERVED):
        return None
    if direction == OCCURS:
        return verdict == ON_TIME
    return verdict != ON_TIME


def counts_for(direction: str, verdict: str) -> tuple[int, int]:
    """(correct, total) the claim's current verdict adds to V: at most one."""
    grade = graded(direction, verdict)
    if grade is None:
        return 0, 0
    return (1 if grade else 0), 1


def watch_window_end(rec: SignatureRecord) -> datetime:
    """The due plus the claim's watch window. No drawer default exists here, so none means zero."""
    assert rec.due is not None
    return rec.due + timedelta(days=rec.watch_window or 0.0)


def gap_touches(gaps: list[tuple[datetime, datetime]], start: datetime, end: datetime) -> bool:
    """Whether any reported gap overlaps the interval from *start* to *end*."""
    return any(gap_from < end and gap_to > start for gap_from, gap_to in gaps)


def observed_verdict(
    rec: SignatureRecord, outcome: str, event_time: datetime, report_time: datetime
) -> str | None:
    """
    The verdict a supplied observation gives *rec*, or None when it changes nothing.

    ``occurred`` at or before the due is on time, inside the watch window late,
    and past the window outside the claim. ``observed_absent`` is absence
    through the due, so it cannot be observed before it. ``unknown`` grades
    nothing. A bound claim already settled is amended only while its watch
    window is open; an unbound one at any time.
    """
    if outcome == UNKNOWN:
        return None
    window_end = watch_window_end(rec)
    if rec.verdict and rec.outcome_source and report_time > window_end:
        return None  # the watch window records; it does not extend the due
    if outcome == OCCURRED:
        if event_time <= rec.due:
            return ON_TIME
        if event_time <= window_end:
            return LATE
        return None
    if outcome == OBSERVED_ABSENT:
        if event_time < rec.due:
            raise ValueError("Absence through the due is observed at or after it.")
        return OBSERVED_ABSENT
    raise ValueError(f"Unknown observation {outcome!r}.")
