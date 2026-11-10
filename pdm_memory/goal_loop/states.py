"""Job states and the only moves between them.

    drafted -> approved -> armed -> waiting -> triggered -> guard_checked
            -> acting -> verifying -> complete

with side paths: retrying, escalated, out_of_budget, conflict, cancelled and
impossible. ``complete`` has exactly one way in: from ``verifying``, with a
proof receipt (the controller refuses the move without one).
"""

from __future__ import annotations

from enum import Enum

__all__ = [
    "TERMINAL",
    "TRANSITIONS",
    "EscalationKind",
    "InvalidTransition",
    "JobState",
    "check_transition",
]


class JobState(str, Enum):
    DRAFTED = "drafted"
    APPROVED = "approved"
    ARMED = "armed"
    WAITING = "waiting"
    TRIGGERED = "triggered"
    GUARD_CHECKED = "guard_checked"
    ACTING = "acting"
    VERIFYING = "verifying"
    COMPLETE = "complete"
    RETRYING = "retrying"
    ESCALATED = "escalated"
    OUT_OF_BUDGET = "out_of_budget"
    CONFLICT = "conflict"
    CANCELLED = "cancelled"
    IMPOSSIBLE = "impossible"


class EscalationKind(str, Enum):
    """Why a job is waiting for a person. It decides which answers are valid."""

    APPROVAL_REQUIRED = "approval_required"  # the step has not run; the owner decides
    OUTCOME_UNCERTAIN = "outcome_uncertain"  # the step may have run; nobody knows
    PROOF_TIMEOUT = "proof_timeout"  # the step ran; no proof arrived in time


class InvalidTransition(ValueError):
    """A move the state machine does not allow, or an answer that does not fit."""


S = JobState

TERMINAL: frozenset[JobState] = frozenset(
    {S.COMPLETE, S.OUT_OF_BUDGET, S.CONFLICT, S.CANCELLED, S.IMPOSSIBLE}
)

TRANSITIONS: dict[JobState, frozenset[JobState]] = {
    S.DRAFTED: frozenset({S.APPROVED, S.CANCELLED}),
    S.APPROVED: frozenset({S.ARMED, S.CANCELLED}),
    S.ARMED: frozenset({S.WAITING, S.CANCELLED}),
    S.WAITING: frozenset({S.TRIGGERED, S.CANCELLED, S.OUT_OF_BUDGET}),
    S.TRIGGERED: frozenset(
        {S.GUARD_CHECKED, S.ESCALATED, S.CONFLICT, S.CANCELLED, S.OUT_OF_BUDGET}
    ),
    S.GUARD_CHECKED: frozenset({S.ACTING, S.CANCELLED, S.OUT_OF_BUDGET}),
    S.ACTING: frozenset({S.VERIFYING, S.RETRYING, S.OUT_OF_BUDGET, S.ESCALATED, S.CANCELLED}),
    S.VERIFYING: frozenset(
        {
            S.COMPLETE,
            S.WAITING,
            S.OUT_OF_BUDGET,
            S.RETRYING,
            S.IMPOSSIBLE,
            S.ESCALATED,
            S.CANCELLED,
        }
    ),
    S.RETRYING: frozenset({S.TRIGGERED, S.CANCELLED, S.OUT_OF_BUDGET}),
    S.ESCALATED: frozenset(
        {
            S.GUARD_CHECKED,
            S.CONFLICT,
            S.WAITING,
            S.VERIFYING,
            S.TRIGGERED,
            S.OUT_OF_BUDGET,
            S.CANCELLED,
        }
    ),
}


def check_transition(old: JobState, new: JobState) -> None:
    if new not in TRANSITIONS.get(old, frozenset()):
        raise InvalidTransition(f"a job cannot move from {old.value} to {new.value}")
