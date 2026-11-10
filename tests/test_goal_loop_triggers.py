"""Trigger hysteresis: fire once per crossing, re-arm only after a real reset."""

from __future__ import annotations

from decimal import Decimal

from pdm_memory.goal_loop import Trigger, TriggerState
from pdm_memory.goal_loop.triggers import in_fire_region, in_reset_region, step_threshold

RISING = Trigger(
    kind="threshold", metric="MU.price", direction="rising", fire_at="40.00", reset_at="39.50"
)
FALLING = Trigger(
    kind="threshold", metric="MU.price", direction="falling", fire_at="30.00", reset_at="30.50"
)


def run(trigger, values, *, state=TriggerState.UNKNOWN, can_fire=True):
    """Feed readings; return the readings that fired and the final state."""
    fired = []
    for value in values:
        step = step_threshold(trigger, state, Decimal(str(value)), can_fire=can_fire)
        if step.fired:
            fired.append(value)
        state = step.state
    return fired, state


def test_six_readings_above_the_line_fire_once():
    fired, state = run(RISING, [39.0, 40.1, 40.3, 40.2, 40.5, 40.4, 40.6])
    assert fired == [40.1]
    assert state is TriggerState.LATCHED


def test_reset_below_the_reset_line_arms_a_second_firing():
    fired, _ = run(RISING, [39.0, 40.1, 40.3, 39.4, 40.1])
    assert fired == [40.1, 40.1]


def test_dipping_only_to_the_reset_line_does_not_rearm():
    fired, state = run(RISING, [39.0, 40.1, 39.5, 39.9, 40.2])
    assert fired == [40.1]
    assert state is TriggerState.LATCHED


def test_first_reading_above_the_line_never_fires():
    fired, state = run(RISING, [41.0, 42.0])
    assert fired == []
    assert state is TriggerState.LATCHED


def test_first_reading_above_the_line_fires_after_a_reset():
    fired, _ = run(RISING, [41.0, 39.4, 40.0])
    assert fired == [40.0]


def test_the_fire_line_itself_counts_as_crossing():
    assert in_fire_region(RISING, Decimal("40.00"))
    assert not in_fire_region(RISING, Decimal("39.99"))
    assert in_reset_region(RISING, Decimal("39.49"))
    assert not in_reset_region(RISING, Decimal("39.50"))


def test_decimal_readings_do_not_lose_precision():
    fired, _ = run(RISING, ["39.9", "39.99", "40.00"])
    assert fired == ["40.00"]


def test_falling_trigger_mirrors_rising():
    fired, state = run(FALLING, [31.0, 29.9, 29.5, 29.0])
    assert fired == [29.9]
    assert state is TriggerState.LATCHED
    fired, _ = run(FALLING, [31.0, 29.9, 30.4, 29.9])
    assert fired == [29.9]
    fired, _ = run(FALLING, [31.0, 29.9, 30.6, 29.9])
    assert fired == [29.9, 29.9]


def test_busy_job_keeps_the_crossing_for_later():
    """A crossing during an action is not consumed: the trigger stays armed."""
    step = step_threshold(RISING, TriggerState.ARMED, Decimal("40.2"), can_fire=False)
    assert step.suppressed and not step.fired
    assert step.state is TriggerState.ARMED
    again = step_threshold(RISING, step.state, Decimal("40.3"), can_fire=True)
    assert again.fired


def test_busy_job_still_honours_a_reset():
    step = step_threshold(RISING, TriggerState.LATCHED, Decimal("39.4"), can_fire=False)
    assert step.state is TriggerState.ARMED
    assert step.changed and not step.fired


def test_reset_then_crossing_while_busy_is_not_lost():
    """Cross, reset to 39.40, rise to 40.20 while busy; the first free reading fires."""
    state = TriggerState.LATCHED
    for value in ("39.4", "40.2"):
        state = step_threshold(RISING, state, Decimal(value), can_fire=False).state
    assert state is TriggerState.ARMED
    assert step_threshold(RISING, state, Decimal("40.2"), can_fire=True).fired


def test_readings_between_the_lines_change_nothing_for_a_latched_trigger():
    step = step_threshold(RISING, TriggerState.LATCHED, Decimal("39.7"), can_fire=True)
    assert step.state is TriggerState.LATCHED
    assert not step.changed
