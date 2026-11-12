"""A horizon that waits for an event instead of a fixed window."""

from __future__ import annotations

import math
from dataclasses import replace

import pytest

from pdm_memory import ConsequenceLedger
from pdm_memory.consequence import (
    Comparison,
    EventCondition,
    EvidenceKind,
    Expectation,
    Ordering,
    Response,
)
from tests.consequence_fixtures import (
    COMMIT,
    END,
    HORIZON,
    MINUTE,
    POLICY,
    POSITION_MOVED,
    START,
    TASK,
    TEMP,
    VALVE,
)

MOVED_EVENT = EventCondition("valve_position", "state", ("categorical", "1"), ["moved"], grade_for_s=1800)
WAIT = replace(HORIZON, event=MOVED_EVENT)
EXP = replace(VALVE, horizon=WAIT)


def _temp(id_, at, value=21.0, **changes):
    return replace(TEMP, observation_id=id_, event_time=at, arrival_time=at, value=value, **changes)


def _pos(id_, at, value="moved", **changes):
    return replace(POSITION_MOVED, observation_id=id_, event_time=at, arrival_time=at, value=value,
                   **changes)


@pytest.fixture
def ledger(tmp_path):
    with ConsequenceLedger(tmp_path / "c.db", owner="owner-1", policy=POLICY,
                           clock=lambda: COMMIT) as led:
        led.prepare(EXP)
        yield led


def _ev(ledger):
    return ledger.evaluate("exp-valve-1", task=TASK)


def test_nothing_is_graded_before_the_event(ledger):
    ledger.observe(_temp("t-early", START + MINUTE))
    ev = _ev(ledger)
    for f in ev.findings:
        assert (f.comparison, f.missing) == (Comparison.UNRESOLVED, "event not observed")
    assert ev.response is Response.OBSERVE
    assert [p.criterion for p in ledger.suggest_observation("exp-valve-1")] == ["moved", "room_temp"]


def test_readings_count_from_the_event_until_grade_for_s(ledger):
    event_at = START + 10 * MINUTE
    ledger.observe(_temp("t-before", event_at - MINUTE, value=5.0))
    ledger.observe(_pos("p-1", event_at))
    ledger.observe(_temp("t-in", event_at + 29 * MINUTE))
    ledger.observe(_temp("t-edge", event_at + 30 * MINUTE))
    ledger.observe(_temp("t-after", event_at + 31 * MINUTE, value=5.0))
    ev = _ev(ledger)
    temp = ev.finding("room_temp")
    assert temp.comparison is Comparison.CONFIRMED_MATCH
    assert {i for i, _ in temp.used} == {"t-in", "t-edge"}
    assert temp.excluded == ()  # outside the span they are not evidence at all
    assert ev.finding("moved").comparison is Comparison.CONFIRMED_MATCH  # the event itself grades
    assert ev.response is Response.CONTINUE


def test_without_grade_for_s_the_window_end_closes_it(ledger, tmp_path):
    open_ended = replace(EXP, expectation_id="exp-2", action_id="act-2",
                         horizon=replace(WAIT, event=replace(MOVED_EVENT, grade_for_s=None)))
    ledger.prepare(open_ended)
    ledger.observe(_pos("p-1", START + MINUTE))
    ledger.observe(_temp("t-late", END))
    ledger.observe(_temp("t-past", END + MINUTE, value=5.0))
    temp = ledger.evaluate("exp-2", task=TASK).finding("room_temp")
    assert ({i for i, _ in temp.used}, temp.comparison) == ({"t-late"}, Comparison.CONFIRMED_MATCH)


def test_the_earliest_meeting_reading_opens_it(ledger):
    ledger.observe(_pos("p-stuck", START + MINUTE, value="stuck"))
    ledger.observe(_pos("p-late", START + 40 * MINUTE))
    ledger.observe(_pos("p-early", START + 5 * MINUTE))
    ledger.observe(_temp("t-1", START + 20 * MINUTE))  # graded only if the 5-minute reading opened it
    temp = _ev(ledger).finding("room_temp")
    assert {i for i, _ in temp.used} == {"t-1"}


@pytest.mark.parametrize(
    "changes",
    [
        {"valid": False},
        {"kind": EvidenceKind.SIMULATED},
        {"source": "room-sensor"},  # admitted, but not for valve_position
        {"source": "unknown-feed"},
        {"unit": "percent"},
        {"evidence_ref": "mem-setpoint"},  # the expectation's own input
    ],
)
def test_an_inadmissible_reading_does_not_open_it(ledger, changes):
    ledger.observe(_pos("p-1", START + MINUTE, **changes))
    ledger.observe(_temp("t-1", START + 2 * MINUTE))
    assert _ev(ledger).finding("room_temp").missing == "event not observed"


def test_a_reading_outside_the_wait_or_off_condition_does_not_open_it(ledger):
    ledger.observe(_pos("p-before", START - MINUTE))
    ledger.observe(_pos("p-after", END + MINUTE))
    ledger.observe(_pos("p-closed", START + MINUTE, value="closed"))
    ledger.observe(_pos("p-number", START + MINUTE, value=3))
    ledger.observe(_temp("t-1", START + 2 * MINUTE))
    assert _ev(ledger).finding("room_temp").missing == "event not observed"


def test_a_silence_cannot_be_the_event(ledger):
    ledger.observe(_pos("p-silent", START + MINUTE, value=None, coverage=(START, END)))
    assert _ev(ledger).finding("room_temp").missing == "event not observed"


def test_correcting_the_event_reading_revises_the_evaluation(ledger):
    ledger.observe(_pos("p-1", START + MINUTE))
    ledger.observe(_temp("t-1", START + 2 * MINUTE))
    first = _ev(ledger)
    assert first.finding("room_temp").comparison is Comparison.CONFIRMED_MATCH
    ledger.observe(_pos("p-1", START + MINUTE, revision=2, supersedes=1, valid=False))
    second = _ev(ledger)
    assert second.evaluation_id != first.evaluation_id and second.revision == 2
    assert second.finding("room_temp").missing == "event not observed"
    assert ledger.evaluation(first.evaluation_id).superseded_by == second.evaluation_id


def test_a_correction_to_an_event_on_another_observable_revises_the_evaluation(ledger):
    pressure = EventCondition("supply_bar", "bar", ("range", "1"), {"low": 0.3, "high": 1.0})
    ledger.prepare(replace(EXP, expectation_id="exp-2", action_id="act-2",
                           horizon=replace(WAIT, event=pressure)))
    supply = replace(TEMP, observation_id="s-1", source="supply-gauge", observable="supply_bar",
                     unit="bar", value=0.5, event_time=START + MINUTE, arrival_time=START + MINUTE)
    ledger.observe(supply)
    ledger.observe(_temp("t-1", START + 2 * MINUTE))
    first = ledger.evaluate("exp-2", task=TASK)
    assert first.finding("room_temp").comparison is Comparison.CONFIRMED_MATCH
    ledger.observe(replace(supply, revision=2, supersedes=1, value=0.1))
    second = ledger.evaluate("exp-2", task=TASK)
    assert second.evaluation_id != first.evaluation_id
    assert second.finding("room_temp").missing == "event not observed"


def test_moving_the_event_regrades_a_silence_with_the_same_inputs(ledger):
    pressure = EventCondition("supply_bar", "bar", ("range", "1"), {"low": 0.3, "high": 1.0},
                              grade_for_s=1800)
    ledger.prepare(replace(EXP, expectation_id="exp-2", action_id="act-2",
                           horizon=replace(WAIT, event=pressure)))
    at = START + 10 * MINUTE
    supply = replace(TEMP, observation_id="s-1", source="supply-gauge", observable="supply_bar",
                     unit="bar", value=0.5, event_time=at, arrival_time=at)
    ledger.observe(supply)
    ledger.observe(_temp("t-silent", at, value=None, coverage=(at - 5 * MINUTE, at + 35 * MINUTE)))
    first = ledger.evaluate("exp-2", task=TASK).finding("room_temp")
    assert first.comparison is Comparison.CONFIRMED_MISS
    # The corrected event time leaves the silence in the span but no longer covering it.
    moved = at - 8 * MINUTE
    ledger.observe(replace(supply, revision=2, supersedes=1, event_time=moved, arrival_time=moved))
    second = ledger.evaluate("exp-2", task=TASK).finding("room_temp")
    assert second.excluded == (("t-silent", "coverage"),)


def test_a_silence_covering_the_span_is_timed_from_the_event(ledger):
    event_at = START + 10 * MINUTE  # after commit, so prospective
    ledger.observe(_pos("p-1", event_at))
    ledger.observe(_temp("t-silent", event_at, value=None,
                         coverage=(event_at - MINUTE, event_at + 31 * MINUTE)))
    temp = _ev(ledger).finding("room_temp")
    assert (temp.comparison, temp.ordering) == (Comparison.CONFIRMED_MISS, Ordering.PROSPECTIVE)


def test_a_silence_that_misses_part_of_the_span_is_excluded(ledger):
    event_at = START + 10 * MINUTE
    ledger.observe(_pos("p-1", event_at))
    ledger.observe(_temp("t-silent", event_at, value=None, coverage=(event_at, event_at + 10 * MINUTE)))
    assert _ev(ledger).finding("room_temp").excluded == (("t-silent", "coverage"),)


def test_an_event_before_commit_grades_retrospectively(tmp_path):
    early = replace(EXP, horizon=replace(WAIT, window_start=COMMIT - 10 * MINUTE))
    with ConsequenceLedger(tmp_path / "c.db", owner="o", policy=POLICY, clock=lambda: COMMIT) as led:
        led.prepare(early)
        led.observe(_pos("p-1", COMMIT - 5 * MINUTE))
        led.observe(_temp("t-1", COMMIT - 2 * MINUTE))
        temp = led.evaluate("exp-valve-1", task=TASK).finding("room_temp")
    assert (temp.comparison, temp.ordering) == (Comparison.CONFIRMED_MATCH, Ordering.RETROSPECTIVE)


def test_prepare_checks_the_event_rule(tmp_path):
    with ConsequenceLedger(tmp_path / "c.db", owner="o", policy=POLICY) as led:
        fuzzy = replace(MOVED_EVENT, comparator=("fuzzy", "1"))
        with pytest.raises(ValueError, match="comparator"):
            led.prepare(replace(EXP, horizon=replace(WAIT, event=fuzzy)))
        bad_band = EventCondition("room_temp_c", "degC", ("range", "1"), {"low": 1})
        with pytest.raises(ValueError, match="band"):
            led.prepare(replace(EXP, horizon=replace(WAIT, event=bad_band)))
        assert led.history() == []


@pytest.mark.parametrize("grade_for_s", [0, -1, math.inf, math.nan, True, "60"])
def test_grade_for_s_must_be_a_positive_finite_number(grade_for_s):
    with pytest.raises(ValueError, match="grade_for_s"):
        replace(MOVED_EVENT, grade_for_s=grade_for_s)


def test_an_event_must_be_an_event_condition():
    with pytest.raises(TypeError):
        replace(HORIZON, event={"observable": "x"})
    with pytest.raises(ValueError, match="comparator"):
        EventCondition("x", "u", ("range",), {"low": 0, "high": 1})


def test_an_event_horizon_round_trips_and_a_plain_one_keeps_its_old_form(tmp_path):
    assert "event" not in VALVE.to_dict()["horizon"]
    assert Expectation.from_dict(EXP.to_dict()) == EXP
    with ConsequenceLedger(tmp_path / "c.db", owner="o", policy=POLICY) as a:
        a.prepare(EXP)
    with ConsequenceLedger(tmp_path / "c.db", owner="o", policy=POLICY) as b:
        assert b.expectation("exp-valve-1")[0] == EXP
        assert b.prepare(EXP) == "exp-valve-1"


def test_the_forecast_record_uses_the_event_span(ledger):
    ledger.observe(_pos("p-1", START + MINUTE))
    _ev(ledger)
    assert {r.horizon_s for r in ledger.forecast_record("valve-model")} == {1800.0}
