"""Evaluation: admissible evidence, ordering, applicability and comparison."""

from __future__ import annotations

from dataclasses import replace

import pytest

from pdm_memory import ConsequenceLedger
from pdm_memory.consequence import (
    Applicability,
    Cause,
    CauseHypothesis,
    Comparison,
    Criterion,
    EvidenceKind,
    Ordering,
    Response,
    Stage,
)
from tests.consequence_fixtures import (
    COMMIT,
    END,
    HORIZON,
    HOUR,
    MINUTE,
    POLICY,
    POSITION_MOVED,
    SECOND,
    START,
    TASK,
    TEMP,
    VALVE,
    _guard,
    _request,
)

# A window that opened before the commitment, so a reading can land in it
# either side of the commit.
EARLY = replace(VALVE, horizon=replace(HORIZON, window_start=COMMIT - HOUR))


@pytest.fixture
def ledger_at(tmp_path):
    made = []

    def make(now=COMMIT, policy=POLICY):
        led = ConsequenceLedger(tmp_path / f"c{len(made)}.db", owner="owner-1", policy=policy,
                                clock=lambda: now)
        made.append(led)
        return led

    yield make
    for led in made:
        led.close()


@pytest.fixture
def ledger(ledger_at):
    return ledger_at()


def _with(ledger, *observations, expectation=VALVE):
    ledger.prepare(expectation)
    for o in observations:
        ledger.observe(o)


def _finding(ledger, criterion, **kw):
    return ledger.evaluate("exp-valve-1", task=TASK, **kw).finding(criterion)


def test_a_returned_handler_is_not_a_task_effect(ledger, tmp_path):
    ledger.prepare(VALVE)
    with _guard(tmp_path) as guard:
        guard.stage(_request())
        ledger.attach_action("exp-valve-1", guard.release("act-valve-1"))
    ev = ledger.evaluate("exp-valve-1", task=TASK)
    assert ev.finding("room_temp").comparison is Comparison.UNRESOLVED
    assert ev.finding("moved").comparison is Comparison.UNRESOLVED
    assert ev.response is Response.OBSERVE


def test_movement_matches_while_the_effect_stays_unresolved(ledger):
    _with(ledger, POSITION_MOVED)
    ev = ledger.evaluate("exp-valve-1", task=TASK)
    assert ev.finding("moved").comparison is Comparison.CONFIRMED_MATCH
    assert ev.finding("moved").ordering is Ordering.PROSPECTIVE
    assert ev.finding("room_temp").comparison is Comparison.UNRESOLVED
    assert ev.finding("room_temp").missing == "no admissible observation"
    assert ev.response is Response.OBSERVE


def test_a_measured_band_confirms_and_continues(ledger):
    _with(ledger, POSITION_MOVED, replace(TEMP, value=21.0))
    ev = ledger.evaluate("exp-valve-1", task=TASK)
    assert ev.finding("room_temp").comparison is Comparison.CONFIRMED_MATCH
    assert ev.response is Response.CONTINUE


def test_a_full_coverage_silence_is_a_miss_but_a_gap_is_not(ledger):
    _with(ledger, replace(POSITION_MOVED, value=None, coverage=(START, END)))
    assert _finding(ledger, "moved").comparison is Comparison.CONFIRMED_MISS
    ledger.observe(
        replace(POSITION_MOVED, revision=2, supersedes=1, value=None, coverage=(START, END - HOUR / 2))
    )
    f = _finding(ledger, "moved")
    assert f.comparison is Comparison.UNRESOLVED
    assert ("obs-pos", "coverage") in f.excluded


def test_disagreeing_admissible_evidence_is_a_conflict(ledger):
    probe = replace(TEMP, observation_id="obs-temp-b", source="wall-probe", value=25.0)
    _with(ledger, replace(TEMP, value=21.0), probe)
    ev = ledger.evaluate("exp-valve-1", task=TASK)
    assert ev.finding("room_temp").comparison is Comparison.CONFLICT
    assert set(ev.finding("room_temp").used) == {("obs-temp", 1), ("obs-temp-b", 1)}
    assert ev.response is Response.RESOLVE_CONFLICT


def test_a_miss_opens_a_review(ledger):
    _with(ledger, POSITION_MOVED, replace(TEMP, value=25.0))
    ev = ledger.evaluate("exp-valve-1", task=TASK)
    assert ev.finding("room_temp").comparison is Comparison.CONFIRMED_MISS
    assert ev.response is Response.OPEN_REVIEW


def test_an_event_before_commitment_is_retrospective(ledger):
    late_arrival = replace(TEMP, event_time=COMMIT - MINUTE, arrival_time=COMMIT + HOUR, value=21.0)
    _with(ledger, late_arrival, expectation=EARLY)
    f = _finding(ledger, "room_temp")
    assert (f.ordering, f.comparison) == (Ordering.RETROSPECTIVE, Comparison.CONFIRMED_MATCH)


def test_a_clock_overlap_is_uncertain_and_does_not_grade(ledger):
    _with(ledger, replace(TEMP, event_time=COMMIT + SECOND, clock_uncertainty_s=5.0, value=21.0),
          expectation=EARLY)
    f = _finding(ledger, "room_temp")
    assert f.comparison is Comparison.UNRESOLVED
    assert ("obs-temp", "ordering") in f.excluded


def test_a_source_sequence_beats_the_clock(ledger):
    sequenced = replace(EARLY, source_sequences={"room-sensor": 40})
    _with(ledger, replace(TEMP, event_time=COMMIT - MINUTE, source_sequence=41, value=21.0),
          expectation=sequenced)
    assert _finding(ledger, "room_temp").ordering is Ordering.PROSPECTIVE


@pytest.mark.parametrize(
    ("change", "reason"),
    [
        ({"unit": "degF"}, "unit"),
        ({"evidence_ref": "mem-setpoint"}, "own witness"),
        ({"kind": EvidenceKind.SIMULATED}, "simulated"),
        ({"source": "unknown-feed"}, "source"),
        ({"valid": False}, "invalid"),
        ({"event_time": START - HOUR}, "window"),
        ({"value": "warm"}, "value"),
    ],
)
def test_inadmissible_evidence_is_kept_out_with_its_reason(ledger, change, reason):
    _with(ledger, replace(TEMP, **{"value": 21.0, **change}))
    f = _finding(ledger, "room_temp")
    assert f.comparison is Comparison.UNRESOLVED
    assert ("obs-temp", reason) in f.excluded
    assert reason in f.missing


def test_a_changed_condition_is_out_of_scope(ledger):
    guarded = replace(
        VALVE, conditions={"door": "closed"}, horizon=replace(HORIZON, invalidated_by=("door",))
    )
    _with(ledger, replace(TEMP, value=25.0, conditions={"door": "open"}), expectation=guarded)
    f = _finding(ledger, "room_temp")
    assert (f.applicability, f.comparison) == (Applicability.OUT_OF_SCOPE, Comparison.CONFIRMED_MISS)


def test_an_unreported_condition_leaves_applicability_unresolved(ledger):
    guarded = replace(
        VALVE, conditions={"door": "closed"}, horizon=replace(HORIZON, invalidated_by=("door",))
    )
    _with(ledger, replace(TEMP, value=21.0), expectation=guarded)
    assert _finding(ledger, "room_temp").applicability is Applicability.UNRESOLVED


def test_another_environment_version_is_out_of_scope(ledger):
    versioned = replace(VALVE, horizon=replace(HORIZON, environment_versions={"firmware": "4.2"}))
    _with(ledger, replace(TEMP, value=21.0, configuration={"firmware": "5.0"}), expectation=versioned)
    assert _finding(ledger, "room_temp").applicability is Applicability.OUT_OF_SCOPE


def test_a_probabilistic_miss_can_be_allowed_by_the_model(ledger):
    banded = Criterion(
        "room_temp", "room_temp_c", Stage.TASK_EFFECT, ("probabilistic", "1"), "degC",
        {"low": 20.0, "high": 22.0, "probability": 0.9},
    )
    _with(ledger, replace(TEMP, value=23.0), expectation=replace(VALVE, criteria=(banded,)))
    f = _finding(ledger, "room_temp")
    assert (f.comparison, f.allowed_by_model) == (Comparison.CONFIRMED_MISS, True)


def test_a_malformed_expected_value_is_refused_at_prepare(ledger):
    bad = replace(VALVE.criteria[1], expected={"low": 22.0, "high": 20.0})
    with pytest.raises(ValueError, match="high"):
        ledger.prepare(replace(VALVE, criteria=(bad,)))


def test_an_unsupported_cause_leaves_attribution_unresolved(ledger):
    _with(ledger, POSITION_MOVED, replace(TEMP, value=25.0))
    ev = ledger.evaluate(
        "exp-valve-1", task=TASK, causes=(CauseHypothesis(Cause.CHANGED_CONDITIONS),)
    )
    assert ev.response is Response.OPEN_REVIEW
    assert not ev.attribution_supported


def test_a_cause_is_supported_only_by_recorded_independent_evidence(ledger):
    pressure = replace(TEMP, observation_id="obs-supply", observable="supply_bar", value=0.4)
    _with(ledger, POSITION_MOVED, replace(TEMP, value=25.0), pressure)
    unknown = (CauseHypothesis(Cause.EXECUTION, ("obs-nowhere",)),)
    assert not ledger.evaluate("exp-valve-1", task=TASK, causes=unknown).attribution_supported
    ledger.observe(replace(pressure, revision=2, supersedes=1, value=0.3))
    cited = (CauseHypothesis(Cause.EXECUTION, ("obs-supply",)),)
    ev = ledger.evaluate("exp-valve-1", task=TASK, causes=cited)
    assert ev.attribution_supported and ev.revision == 2


def test_evaluate_binds_to_the_expectations_task(ledger):
    ledger.prepare(VALVE)
    with pytest.raises(ValueError, match="task"):
        ledger.evaluate("exp-valve-1", task=replace(TASK, task_id="other"))


def test_an_evaluation_reads_back_unchanged(ledger):
    _with(ledger, POSITION_MOVED, replace(TEMP, value=25.0))
    ev = ledger.evaluate("exp-valve-1", task=TASK)
    assert ledger.evaluation(ev.evaluation_id) == ev


def test_a_policy_cannot_register_a_comparator_with_no_implementation():
    from pdm_memory.guarded_effects import ConfigurationError

    with pytest.raises(ConfigurationError, match="implementation"):
        replace(POLICY, comparators=POLICY.comparators | {("fuzzy", "1")})
