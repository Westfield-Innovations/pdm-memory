"""Trigger hysteresis: fire once per crossing, re-arm only after a real reset.

For a ``rising`` trigger with ``fire_at=40`` and ``reset_at=39.50``:

    value >= 40.00           the fire region
    value <  39.50           the reset region
    39.50 <= value < 40.00   neither: a latched trigger stays latched

A ``falling`` trigger mirrors this (fire at or below ``fire_at``, reset above
``reset_at``). The state is ``ARMED`` (a crossing would fire), ``LATCHED``
(already fired, waiting for a reset) or ``UNKNOWN`` (no reading yet).

The first reading never fires: "crosses from below" needs a reading on the
other side first. A job armed while the price already sits above the line waits
for a reset and a new crossing.
"""

from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal
from enum import Enum

from pdm_memory.goal_loop.contract import Trigger

__all__ = ["TriggerState", "TriggerStep", "in_fire_region", "in_reset_region", "step_threshold"]


class TriggerState(str, Enum):
    UNKNOWN = "unknown"
    ARMED = "armed"
    LATCHED = "latched"


@dataclass(frozen=True)
class TriggerStep:
    state: TriggerState
    fired: bool = False
    suppressed: bool = False  # a crossing happened while the job could not act on it
    changed: bool = False  # the trigger state moved


def in_fire_region(trigger: Trigger, value: Decimal) -> bool:
    assert trigger.fire_at is not None
    if trigger.direction == "rising":
        return value >= trigger.fire_at
    return value <= trigger.fire_at


def in_reset_region(trigger: Trigger, value: Decimal) -> bool:
    assert trigger.reset_at is not None
    if trigger.direction == "rising":
        return value < trigger.reset_at
    return value > trigger.reset_at


def step_threshold(
    trigger: Trigger, state: TriggerState, value: Decimal, *, can_fire: bool
) -> TriggerStep:
    """Advance the trigger by one reading.

    ``can_fire`` is False while the job is busy. A reset is still honoured then,
    but a crossing is not consumed: the trigger stays ``ARMED``, so the first
    reading above the line after the job is free again fires it.
    """
    if state is TriggerState.UNKNOWN:
        new = TriggerState.LATCHED if in_fire_region(trigger, value) else TriggerState.ARMED
        return TriggerStep(new, changed=True)
    if state is TriggerState.ARMED:
        if not in_fire_region(trigger, value):
            return TriggerStep(TriggerState.ARMED)
        if can_fire:
            return TriggerStep(TriggerState.LATCHED, fired=True, changed=True)
        return TriggerStep(TriggerState.ARMED, suppressed=True)
    if in_reset_region(trigger, value):
        return TriggerStep(TriggerState.ARMED, changed=True)
    return TriggerStep(TriggerState.LATCHED)
