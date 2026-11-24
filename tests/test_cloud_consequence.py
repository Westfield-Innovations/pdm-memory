"""CloudConsequenceLedger: the server's clock stamps time and the record matches the local one."""

from __future__ import annotations

from datetime import datetime, timezone

import pytest

from pdm_memory import ConsequenceLedger
from pdm_memory.cloud_ledgers import CloudConsequenceLedger
from pdm_memory.cloud_ledgers.testing import in_process, strip_time
from pdm_memory.guarded_effects import (
    CheckResult,
    ConfigurationError,
    GuardedEffects,
    Opening,
)
from tests.consequence_fixtures import COMMIT, POLICY, TASK, TEMP, VALVE, _request
from tests.test_consequence_episode import POSITION_MOVED


def test_the_cloud_class_overrides_every_public_method():
    public = {
        n
        for n, v in vars(ConsequenceLedger).items()
        if callable(v) and not n.startswith("_")
    }
    assert public <= set(vars(CloudConsequenceLedger))


def test_a_cloud_target_builds_the_cloud_class(tmp_path):
    led = ConsequenceLedger(in_process(tmp_path / "c.db"), owner="o", policy=POLICY)
    assert type(led) is CloudConsequenceLedger
    local = ConsequenceLedger(tmp_path / "l.db", owner="o", policy=POLICY)
    assert type(local) is ConsequenceLedger
    local.close()


def test_a_custom_clock_is_refused_by_a_server_that_keeps_its_own(tmp_path):
    cloud = in_process(tmp_path / "c.db", trust_client_clock=False)
    with pytest.raises(ConfigurationError):
        ConsequenceLedger(cloud, owner="o", policy=POLICY, clock=lambda: COMMIT)
    ConsequenceLedger(cloud, owner="o", policy=POLICY)


def test_the_server_clock_stamps_commit_time_when_no_clock_is_given(tmp_path):
    before = datetime.now(timezone.utc)
    led = ConsequenceLedger(
        in_process(tmp_path / "c.db", trust_client_clock=False),
        owner="o",
        policy=POLICY,
    )
    led.prepare(VALVE)
    _, committed = led.expectation(VALVE.expectation_id)
    assert before <= committed <= datetime.now(timezone.utc)
    stamp = led.history()[0]["payload"]["recorded_at"]
    assert datetime.fromisoformat(stamp) >= before


def test_a_custom_clock_stamps_every_call(tmp_path):
    led = ConsequenceLedger(
        in_process(tmp_path / "c.db"), owner="o", policy=POLICY, clock=lambda: COMMIT
    )
    led.prepare(VALVE)
    assert led.expectation(VALVE.expectation_id)[1] == COMMIT
    assert led.verify_history() is True


def _episode(ledger, guard):
    guard.stage(_request())
    ledger.prepare(VALVE)
    ledger.attach_action("exp-valve-1", guard.release("act-valve-1"))
    ledger.observe(POSITION_MOVED)
    ledger.evaluate("exp-valve-1", task=TASK)
    ledger.suggest_observation("exp-valve-1")
    ledger.observe(TEMP)
    ledger.evaluate("exp-valve-1", task=TASK)


def _guard(target, check=None):
    g = GuardedEffects(
        target,
        owner="owner-1",
        program_version="worker-1",
        policy_version="policy-1",
        openings=[
            Opening(
                "open",
                "valve-3",
                lambda r: {"accepted": True},
                lambda r: CheckResult.SATISFIED,
                check,
            )
        ],
    )
    g.start_branch("b1")
    return g


def test_the_valve_episode_matches_the_local_ledger_record_for_record(tmp_path):
    local = ConsequenceLedger(
        tmp_path / "l.db", owner="owner-1", policy=POLICY, clock=lambda: COMMIT
    )
    cloud = ConsequenceLedger(
        in_process(tmp_path / "c.db"),
        owner="owner-1",
        policy=POLICY,
        clock=lambda: COMMIT,
    )
    _episode(local, _guard(tmp_path / "lg.db"))
    _episode(cloud, _guard(in_process(tmp_path / "cg.db")))
    assert strip_time(cloud.history(limit=1000)) == strip_time(
        local.history(limit=1000)
    )
    assert cloud.verify_history() is True
    local.close()


def test_prepared_check_works_as_a_cloud_guard_check(tmp_path):
    led = ConsequenceLedger(
        in_process(tmp_path / "c.db"), owner="owner-1", policy=POLICY
    )
    guard = _guard(in_process(tmp_path / "g.db"), check=led.prepared_check)
    guard.stage(_request())
    led.prepare(VALVE)
    assert guard.release("act-valve-1").status == "RETURNED"


def test_a_client_time_for_suggest_observation_is_refused_by_a_server_that_keeps_its_own(
    tmp_path,
):
    led = ConsequenceLedger(
        in_process(tmp_path / "c.db", trust_client_clock=False),
        owner="owner-1",
        policy=POLICY,
    )
    led.prepare(VALVE)
    with pytest.raises(ConfigurationError):
        led.suggest_observation("exp-valve-1", now=COMMIT)
