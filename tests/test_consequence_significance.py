"""Review priority and significance: inspectable, owner-declared, never an actuator."""

from __future__ import annotations

from dataclasses import replace
from decimal import Decimal

import pytest

from pdm_memory import ConsequenceLedger
from pdm_memory.consequence import (
    Account,
    ConsequenceEntry,
    InfluencePath,
    QuantityStatus,
    Response,
    Significance,
)
from pdm_memory.guarded_effects import ConfigurationError
from tests.consequence_fixtures import (
    COMMIT,
    END,
    LIMITS,
    MINUTE,
    POLICY,
    POSITION_MOVED,
    START,
    TASK,
    TEMP,
    VALVE,
    _policy,
)

IN_BAND = replace(TEMP, value=21.0)


def _open(tmp_path, name="c.db", **policy_changes):
    led = ConsequenceLedger(tmp_path / name, owner="owner-1", policy=_policy(**policy_changes),
                            clock=lambda: COMMIT)
    led.prepare(VALVE)
    return led


def _ev(ledger):
    return ledger.evaluate("exp-valve-1", task=TASK)


def _entry(n, account, path=None, unit="s"):
    return ConsequenceEntry(f"e-{n}", "exp-valve-1", account, "room 4", Decimal(1), unit, END,
                            "controller-log", QuantityStatus.MEASURED, path)


def test_before_any_evaluation(tmp_path):
    with _open(tmp_path) as led:
        assert led.significance("exp-valve-1") == Significance("exp-valve-1", 0, None, 0, (), False)
        with pytest.raises(KeyError):
            led.significance("exp-unknown")


def test_priority_comes_from_the_owner_by_task(tmp_path):
    with _open(tmp_path, priorities={TASK.task_id: 7, "other-task": 1}) as led:
        assert led.significance("exp-valve-1").priority == 7
    with _open(tmp_path, "d.db", priorities={"other-task": 9}) as led:
        assert led.significance("exp-valve-1").priority == 0


def test_persistence_counts_the_latest_revisions_that_did_not_continue(tmp_path):
    with _open(tmp_path) as led:
        assert _ev(led).response is Response.OBSERVE
        led.observe(POSITION_MOVED)
        _ev(led)
        assert led.significance("exp-valve-1").persistence == 2
        _ev(led)  # unchanged material is not another revision
        assert led.significance("exp-valve-1").persistence == 2
        led.observe(IN_BAND)
        assert _ev(led).response is Response.CONTINUE
        assert led.significance("exp-valve-1").persistence == 0
        led.observe(replace(IN_BAND, revision=2, supersedes=1, value=18.0))
        assert _ev(led).response is Response.OPEN_REVIEW
        sig = led.significance("exp-valve-1")
        assert (sig.persistence, sig.response) == (1, Response.OPEN_REVIEW)


def test_reach_lists_the_recorded_influence_paths(tmp_path):
    with _open(tmp_path) as led:
        led.record_consequence(_entry(1, Account.AGENT_OPERATION))
        for n, scope in enumerate(["zone B", "boiler loop", "zone B"], start=2):
            path = InfluencePath("observed", scope, (f"obs-{n}",))
            led.record_consequence(_entry(n, Account.COUPLED_EFFECT, path))
        led.record_consequence(
            _entry(9, Account.TASK_RESULT, InfluencePath("declared", "tenant", ("contract",)))
        )
        assert led.significance("exp-valve-1").reach == ("boiler loop", "tenant", "zone B")


@pytest.mark.parametrize(
    ("priority", "threshold", "settle", "expected"),
    [
        (5, None, False, False),  # no owner permission at all
        (5, 5, False, True),
        (4, 5, False, False),
        (9, 5, True, False),  # continuing needs no interruption
        (0, 0, False, True),
    ],
)
def test_interruption_needs_the_owners_threshold_and_an_open_result(
    tmp_path, priority, threshold, settle, expected
):
    with _open(tmp_path, priorities={TASK.task_id: priority}, interruption_priority=threshold) as led:
        if settle:
            led.observe(POSITION_MOVED)
            led.observe(IN_BAND)
        _ev(led)
        assert led.significance("exp-valve-1").interruption is expected


def test_no_interruption_before_any_evaluation(tmp_path):
    with _open(tmp_path, priorities={TASK.task_id: 9}, interruption_priority=0) as led:
        assert led.significance("exp-valve-1").interruption is False


def test_priority_never_changes_a_response_or_the_review_limits(tmp_path):
    limits = replace(LIMITS, max_repeats=1)
    runs = []
    for name, rank in (("low.db", 0), ("high.db", 99)):
        with _open(tmp_path, name, limits=limits, priorities={TASK.task_id: rank},
                   interruption_priority=1) as led:
            first = _ev(led)
            statuses = [p.status for p in led.suggest_observation("exp-valve-1")]
            led.observe(POSITION_MOVED)
            second = _ev(led)
            statuses += [p.status for p in led.suggest_observation("exp-valve-1")]
            runs.append((first, second, statuses))
    assert runs[0] == runs[1]
    assert runs[0][2] == ["OPEN", "OPEN", "EXHAUSTED"]


def test_accounts_follow_the_owners_priority_ordering(tmp_path):
    order = (Account.PRINCIPAL_BURDEN, Account.TASK_RESULT)
    with _open(tmp_path, priority_ordering=order) as led:
        led.record_consequence(_entry(1, Account.AGENT_OPERATION))
        led.record_consequence(_entry(2, Account.TASK_RESULT, unit="degC"))
        led.record_consequence(_entry(3, Account.PRINCIPAL_BURDEN, unit="interruption"))
        s = led.accounts("exp-valve-1")
    assert [a for a, _, _ in s.totals] == [
        Account.PRINCIPAL_BURDEN,
        Account.TASK_RESULT,
        Account.AGENT_OPERATION,
    ]
    assert s.priority_ordering == order and s.combined is None


def test_without_an_ordering_accounts_keep_recording_order(tmp_path):
    with _open(tmp_path) as led:
        led.record_consequence(_entry(1, Account.AGENT_OPERATION))
        led.record_consequence(_entry(2, Account.PRINCIPAL_BURDEN, unit="interruption"))
        s = led.accounts("exp-valve-1")
    assert [a for a, _, _ in s.totals] == [Account.AGENT_OPERATION, Account.PRINCIPAL_BURDEN]
    assert s.priority_ordering == ()


def test_an_ordering_does_not_replace_an_exchange_rule(tmp_path):
    with _open(tmp_path, priority_ordering=(Account.AGENT_OPERATION,),
               exchange_rules={"s": Decimal(2)}) as led:
        led.record_consequence(_entry(1, Account.AGENT_OPERATION))
        assert led.accounts("exp-valve-1").combined == Decimal(2)


@pytest.mark.parametrize(
    "changes",
    [
        {"priorities": {"t": -1}},
        {"priorities": {"t": True}},
        {"priorities": {"t": 1.5}},
        {"priorities": {"": 1}},
        {"interruption_priority": -1},
        {"interruption_priority": True},
        {"priority_ordering": (Account.TASK_RESULT, Account.TASK_RESULT)},
    ],
)
def test_bad_priority_declarations_are_refused(changes):
    with pytest.raises((ConfigurationError, ValueError, TypeError)):
        _policy(**changes)


def test_an_unknown_account_in_the_ordering_is_refused():
    with pytest.raises(ValueError):
        _policy(priority_ordering=("MORALE",))


def test_the_owners_priorities_cannot_be_edited_in_place():
    source = {TASK.task_id: 1}
    policy = _policy(priorities=source)
    source[TASK.task_id] = 99
    assert policy.priority(TASK.task_id) == 1
    with pytest.raises(TypeError):
        policy.priorities[TASK.task_id] = 50
    assert _policy(priority_ordering=["TASK_RESULT"]).priority_ordering == (Account.TASK_RESULT,)


def test_significance_reads_only_its_own_expectation(tmp_path):
    with _open(tmp_path) as led:
        other = replace(VALVE, expectation_id="exp-2", action_id="act-2")
        led.prepare(other)
        led.evaluate("exp-2", task=TASK)
        path = InfluencePath("observed", "zone B", ("obs-1",))
        led.record_consequence(replace(_entry(1, Account.COUPLED_EFFECT, path), expectation_id="exp-2"))
        sig = led.significance("exp-valve-1")
    assert (sig.response, sig.persistence, sig.reach) == (None, 0, ())


def test_significance_is_the_same_after_a_reopen(tmp_path):
    with _open(tmp_path, priorities={TASK.task_id: 3}) as led:
        led.observe(replace(POSITION_MOVED, event_time=START + MINUTE))
        _ev(led)
        before = led.significance("exp-valve-1")
    with _open(tmp_path, priorities={TASK.task_id: 3}) as led:
        assert led.significance("exp-valve-1") == before
        assert POLICY.priority(TASK.task_id) == 0
