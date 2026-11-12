"""Edge cases found in review: ordering of silences, cause support, deadlines, races."""

from __future__ import annotations

import sqlite3
import threading
from dataclasses import replace

import pytest

from pdm_memory import ConsequenceLedger
from pdm_memory.consequence import (
    Admission,
    Cause,
    CauseHypothesis,
    Comparison,
    Criterion,
    EvidenceKind,
    Ordering,
    ReconcileFirst,
    Route,
    Stage,
)
from pdm_memory.guarded_effects import LedgerIntegrityError
from tests.consequence_fixtures import (
    COMMIT,
    DAY,
    END,
    HORIZON,
    HOUR,
    LIMITS,
    POLICY,
    POSITION_MOVED,
    ROOM_TEMP,
    START,
    TASK,
    TEMP,
    VALVE,
)

SUPPLY = replace(TEMP, observation_id="obs-supply", source="supply-gauge", observable="supply_bar",
                 unit="bar", value=0.4)
CITED = (CauseHypothesis(Cause.EXECUTION, ("obs-supply",)),)


@pytest.fixture
def make(tmp_path):
    made = []

    def build(policy=POLICY, name=None):
        led = ConsequenceLedger(tmp_path / (name or f"c{len(made)}.db"), owner="owner-1",
                                policy=policy, clock=lambda: COMMIT)
        made.append(led)
        return led

    yield build
    for led in made:
        led.close()


@pytest.fixture
def ledger(make):
    return make()


def _propose(ledger, ev, **changes):
    args = {
        "task": TASK,
        "criterion": "room_temp",
        "target_kind": "predictor",
        "target_id": "valve-model",
        "target_version": "1",
        "change": {"bias_c": -1.5},
        "scope": "room 4",
        "confidence": 0.5,
        "route": Route.OWN_ACTION,
        "evidence_kind": EvidenceKind.MEASURED,
        **changes,
    }
    return ledger.propose_learning(ev.evaluation_id, **args)


def _supported_miss(ledger):
    ledger.prepare(VALVE)
    ledger.observe(replace(TEMP, value=25.0))
    ledger.observe(SUPPLY)
    return ledger.evaluate("exp-valve-1", task=TASK, causes=CITED)


def test_an_unknown_criterion_is_a_key_error(ledger):
    ev = _supported_miss(ledger)
    with pytest.raises(KeyError):
        _propose(ledger, ev, criterion="nope")


def test_a_silence_is_timed_by_the_window_it_covers(ledger):
    ledger.prepare(VALVE)
    ledger.observe(replace(POSITION_MOVED, value=None, coverage=(COMMIT - DAY, END + HOUR)))
    f = ledger.evaluate("exp-valve-1", task=TASK).finding("moved")
    assert (f.ordering, f.comparison) == (Ordering.PROSPECTIVE, Comparison.CONFIRMED_MISS)


def test_an_expected_absence_is_confirmed_by_a_covered_silence(ledger):
    absent = Criterion("no_leak", "leak_alarm", Stage.TASK_EFFECT, ("categorical", "1"), "state",
                       ["absent"])
    policy = replace(POLICY, sources=(*POLICY.sources, replace(POLICY.sources[0], name="alarm-panel",
                                                               observables=frozenset({"leak_alarm"}))))
    led = ConsequenceLedger(ledger._conn.execute("PRAGMA database_list").fetchone()[2] + "-abs",
                            owner="o", policy=policy, clock=lambda: COMMIT)
    led.prepare(replace(VALVE, criteria=(absent,)))
    led.observe(replace(POSITION_MOVED, observation_id="obs-alarm", source="alarm-panel",
                        observable="leak_alarm", value=None, coverage=(START, END)))
    assert led.evaluate("exp-valve-1", task=TASK).finding("no_leak").comparison is Comparison.CONFIRMED_MATCH
    led.close()


@pytest.mark.parametrize(
    "change",
    [
        {"valid": False},
        {"source": "unknown-feed"},
        {"source": "room-sensor"},
        {"evidence_ref": "mem-setpoint"},
        {"kind": EvidenceKind.SIMULATED},
    ],
)
def test_a_cause_needs_admissible_independent_support(ledger, change):
    ledger.prepare(VALVE)
    ledger.observe(replace(TEMP, value=25.0))
    ledger.observe(replace(SUPPLY, **change))
    assert not ledger.evaluate("exp-valve-1", task=TASK, causes=CITED).attribution_supported


def test_admissible_support_is_supported(ledger):
    assert _supported_miss(ledger).attribution_supported


def test_past_the_deadline_an_open_request_reads_too_late(ledger):
    ledger.prepare(VALVE)
    ledger.observe(POSITION_MOVED)
    ledger.evaluate("exp-valve-1", task=TASK)
    (p,) = ledger.suggest_observation("exp-valve-1", now=COMMIT)
    assert p.status == "OPEN"
    (late,) = ledger.suggest_observation("exp-valve-1", now=HORIZON.latest_decision_point + DAY)
    assert (late.proposal_id, late.status) == (p.proposal_id, "TOO_LATE")
    kinds = [e["kind"] for e in ledger.history("exp-valve-1")]
    assert kinds[-1] == "OBSERVATION_PROPOSAL_CLOSED"


def test_a_dead_request_does_not_block_a_live_one(make):
    ledger = make(replace(POLICY, limits=replace(LIMITS, max_outstanding=1)))
    ledger.prepare(replace(VALVE, criteria=(ROOM_TEMP,)))
    ledger.suggest_observation("exp-valve-1", now=COMMIT)
    later = replace(HORIZON, window_start=COMMIT + DAY, window_end=COMMIT + DAY + HOUR,
                    latest_decision_point=COMMIT + 2 * DAY)
    ledger.prepare(replace(VALVE, expectation_id="exp-2", action_id=None, criteria=(ROOM_TEMP,),
                           horizon=later))
    (p,) = ledger.suggest_observation("exp-2", now=COMMIT + DAY)
    assert p.status == "OPEN"


def test_an_older_uncertain_update_still_blocks(ledger):
    ev = _supported_miss(ledger)
    first = _propose(ledger, ev, change={"bias_c": -1.0})
    second = _propose(ledger, ev, change={"bias_c": -2.0})
    ledger.record_update_receipt(first.update_id, admission=Admission.ACCEPTED, controller_ref="c")
    ledger.record_update_receipt(second.update_id, admission=Admission.REJECTED, controller_ref="c")
    with pytest.raises(ReconcileFirst, match=first.update_id):
        _propose(ledger, ev, change={"bias_c": -3.0})


def test_learning_is_bound_to_the_hosts_task(ledger):
    ev = _supported_miss(ledger)
    with pytest.raises(ValueError, match="task"):
        _propose(ledger, ev, task=replace(TASK, task_id="someone-else"))


def test_exchange_rules_cannot_be_edited_in_place():
    policy = replace(POLICY, exchange_rules={"s": 1})
    with pytest.raises(TypeError):
        policy.exchange_rules["s"] = 1000


def test_evidence_is_read_under_the_write_lock(ledger):
    # No other writer can add a reading between the read and the new revision,
    # so a slower caller can never supersede a revision built from newer evidence.
    ledger.prepare(VALVE)
    ledger.observe(replace(TEMP, value=25.0))
    read, seen = ledger.observations, []

    def watched(*args, **kwargs):
        seen.append(ledger._conn.in_transaction)
        return read(*args, **kwargs)

    ledger.observations = watched
    ledger.evaluate("exp-valve-1", task=TASK)
    assert seen == [True]


def test_empty_accounts_say_so(ledger):
    ledger.prepare(VALVE)
    s = ledger.accounts("exp-valve-1")
    assert (s.totals, s.combined, s.combined_reason) == ({}, None, "no consequences recorded")


def test_a_new_policy_version_makes_a_new_revision(make, tmp_path):
    first = make(name="p.db")
    first.prepare(VALVE)
    first.observe(replace(TEMP, value=21.0))
    a = first.evaluate("exp-valve-1", task=TASK)
    second = make(replace(POLICY, policy_version="policy-2"), name="p.db")
    b = second.evaluate("exp-valve-1", task=TASK)
    assert (b.revision, b.policy_version) == (2, "policy-2")
    assert second.evaluation(a.evaluation_id).superseded_by == b.evaluation_id


def test_an_unrelated_reading_changes_nothing(ledger):
    ledger.prepare(VALVE)
    ledger.observe(replace(TEMP, value=21.0))
    a = ledger.evaluate("exp-valve-1", task=TASK)
    ledger.observe(replace(TEMP, observation_id="obs-other", observable="humidity", unit="pct", value=40))
    ledger.observe(replace(TEMP, observation_id="obs-late", value=21.0, event_time=END + DAY))
    assert ledger.evaluate("exp-valve-1", task=TASK) == a


def test_owners_sharing_a_file_are_isolated(tmp_path):
    with ConsequenceLedger(tmp_path / "s.db", owner="a", policy=POLICY) as a, \
            ConsequenceLedger(tmp_path / "s.db", owner="b", policy=POLICY) as b:
        a.prepare(VALVE)
        with pytest.raises(KeyError):
            b.expectation("exp-valve-1")
        assert b.prepare(VALVE) == "exp-valve-1"  # same id, another owner's record
        a.observe(replace(TEMP, value=21.0))
        assert b.observations() == []
        assert a.verify_history() and b.verify_history()


def test_a_tampered_event_is_detected(ledger):
    ledger.prepare(VALVE)
    ledger.observe(replace(TEMP, value=21.0))
    conn = ledger._conn
    conn.execute("DROP TRIGGER cl_events_no_update")
    conn.execute("UPDATE cl_events SET payload_json = replace(payload_json, '21.0', '25.0')")
    with pytest.raises(LedgerIntegrityError):
        ledger.verify_history()


def test_triggers_refuse_edits(ledger):
    ledger.prepare(VALVE)
    for sql in ("UPDATE cl_events SET kind='X'", "DELETE FROM cl_events"):
        with pytest.raises(sqlite3.IntegrityError):
            ledger._conn.execute(sql)


def test_threads_evaluating_together_write_one_revision(ledger):
    ledger.prepare(VALVE)
    ledger.observe(replace(TEMP, value=21.0))
    results, errors = [], []

    def run():
        try:
            results.append(ledger.evaluate("exp-valve-1", task=TASK).evaluation_id)
        except (sqlite3.Error, ValueError) as exc:  # pragma: no cover - reported below
            errors.append(exc)

    threads = [threading.Thread(target=run) for _ in range(8)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    assert errors == [] and len(set(results)) == 1
    assert [e["kind"] for e in ledger.history("exp-valve-1")].count("EVALUATED") == 1


def test_unicode_ids_round_trip(ledger):
    exp = replace(VALVE, expectation_id="очікування-1", action_id="дія-1")
    ledger.prepare(exp)
    assert ledger.expectation("очікування-1")[0] == exp


def test_an_exact_comparator(ledger):
    exact = Criterion("mode", "valve_position", Stage.ACTUATOR_CONFIRMATION, ("exact", "1"),
                      "state", "moved")
    ledger.prepare(replace(VALVE, criteria=(exact,)))
    ledger.observe(POSITION_MOVED)
    assert ledger.evaluate("exp-valve-1", task=TASK).finding("mode").comparison is Comparison.CONFIRMED_MATCH
