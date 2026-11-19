"""Separate processes racing on one ledger, on SQLite and on Postgres.

Threads share one connection and its lock; these tests take the database's own
single-writer lock, which is what several hosts or workers actually rely on.
"""

from __future__ import annotations

import multiprocessing as mp
import uuid
from dataclasses import replace

import pytest

from pdm_memory import ConsequenceLedger
from pdm_memory.consequence import (
    Admission,
    Application,
    Cause,
    CauseHypothesis,
    EvidenceKind,
    Revoked,
    Route,
)
from pdm_memory.goal_loop import (
    JobState,
    PostgresJobStore,
    SqliteJobStore,
    StaleJobError,
)
from pdm_memory.goal_loop.testing import _next, _start
from pdm_memory.guarded_effects import CheckResult, GuardedEffects, Opening
from tests.consequence_fixtures import (
    COMMIT,
    LIMITS,
    POLICY,
    POSITION_MOVED,
    TASK,
    TEMP,
    VALVE,
    _request,
)
from tests.ledger_target import PG_URL, postgres_target

WORKERS = 4
needs_pg = pytest.mark.skipif(not PG_URL, reason="PDM_TEST_POSTGRES_URL is not set")
ROOMY = replace(POLICY, limits=replace(LIMITS, max_revisions=1000))
SUPPLY = replace(
    TEMP,
    observation_id="obs-supply",
    source="supply-gauge",
    observable="supply_bar",
    unit="bar",
    value=0.4,
)


@pytest.fixture(params=["sqlite", pytest.param("postgres", marks=needs_pg)])
def target(request, tmp_path):
    if request.param == "sqlite":
        return lambda name: tmp_path / name
    key = uuid.uuid4().hex
    return lambda name: postgres_target(f"{key}/{name}")


@pytest.fixture(scope="module")
def manager():
    with mp.get_context("spawn").Manager() as m:
        yield m


def _race(manager, fn, args_list):
    # Every worker has opened and read before any of them writes.
    go = manager.Barrier(len(args_list))
    with mp.get_context("spawn").Pool(len(args_list)) as pool:
        pending = [pool.apply_async(fn, (go, *args)) for args in args_list]
        return [p.get(120) for p in pending]


def _ledger(target):
    return ConsequenceLedger(
        target, owner="owner-1", policy=ROOMY, clock=lambda: COMMIT
    )


# --- goal loop ---------------------------------------------------------------


def _job_store(target):
    return (
        PostgresJobStore(target) if isinstance(target, str) else SqliteJobStore(target)
    )


def _commit_job(go, target, state):
    store = _job_store(target)
    job = store.load("job-1")
    first = store.receipts("job-1")[0]
    moved, receipt = _next(job, first, JobState(state))
    go.wait()
    try:
        store.commit(moved, [receipt], expected_revision=job.revision)
        return "won"
    except StaleJobError:
        return "stale"
    finally:
        store.close()


def test_job_commit_race_has_one_winner(manager, target):
    t = target("jobs")
    store = _job_store(t)
    job, _ = _start(store)
    store.close()
    states = [JobState.APPROVED.value, JobState.CANCELLED.value] * (WORKERS // 2)
    outcomes = _race(manager, _commit_job, [(t, s) for s in states])
    assert sorted(outcomes) == ["stale"] * (WORKERS - 1) + ["won"]
    store = _job_store(t)
    assert store.load("job-1").revision == job.revision + 1
    assert len(store.receipts("job-1")) == 2
    store.close()


# --- guarded effects ---------------------------------------------------------


def _open_guard(target, handler, *, start=False):
    g = GuardedEffects(
        target,
        owner="owner-1",
        program_version="worker-1",
        policy_version="policy-1",
        openings=[Opening("open", "valve-3", handler, lambda r: CheckResult.SATISFIED)],
    )
    if start:
        g.start_branch("b1")
    return g


def _release(go, target):
    calls = []
    with _open_guard(target, calls.append) as guard:
        go.wait()
        return guard.release("act-valve-1").status, len(calls)


def test_guard_release_race_dispatches_once(manager, target):
    t = target("guard.db")
    with _open_guard(t, lambda r: None, start=True) as guard:
        guard.stage(_request())
    results = _race(manager, _release, [(t,)] * WORKERS)
    assert sum(n for _, n in results) == 1  # one handler call across all processes
    # A process that finds another's dispatch in flight cannot tell it from a
    # crash and says UNCERTAIN; the dispatching process still records RETURNED.
    assert "RETURNED" in {status for status, _ in results}
    assert {status for status, _ in results} <= {"RETURNED", "UNCERTAIN"}
    with _open_guard(t, lambda r: None) as guard:
        assert guard.dispatch_count("open", "valve-3") == 1
        assert guard.release("act-valve-1").status in ("RETURNED", "UNCERTAIN")
        assert guard.verify_history()


# --- consequence ledger --------------------------------------------------------


def _evaluate(go, target):
    with _ledger(target) as led:
        go.wait()
        return led.evaluate("exp-valve-1", task=TASK).evaluation_id


def test_consequence_evaluate_race_makes_one_revision(manager, target):
    t = target("c.db")
    with _ledger(t) as led:
        led.prepare(VALVE)
        led.observe(POSITION_MOVED)
    ids = _race(manager, _evaluate, [(t,)] * WORKERS)
    assert len(set(ids)) == 1
    with _ledger(t) as led:
        assert [e["kind"] for e in led.history(limit=1000)].count("EVALUATED") == 1
        assert led.verify_history()


def _propose(go, target, evaluation_id):
    with _ledger(target) as led:
        go.wait()
        return led.propose_learning(
            evaluation_id,
            task=TASK,
            criterion="room_temp",
            target_kind="belief",
            target_id="b",
            target_version="1",
            change={"x": 1},
            scope="s",
            confidence=0.5,
            route=Route.OWN_ACTION,
            evidence_kind=EvidenceKind.MEASURED,
        ).update_id


def _supported_miss(led):
    led.prepare(VALVE)
    led.observe(POSITION_MOVED)
    led.observe(SUPPLY)
    led.observe(replace(TEMP, value=18.0))
    return led.evaluate(
        "exp-valve-1",
        task=TASK,
        causes=(CauseHypothesis(Cause.EXECUTION, ("obs-supply",)),),
    )


def test_consequence_propose_race_makes_one_proposal(manager, target):
    t = target("c.db")
    with _ledger(t) as led:
        ev = _supported_miss(led)
    ids = _race(manager, _propose, [(t, ev.evaluation_id)] * WORKERS)
    assert len(set(ids)) == 1
    with _ledger(t) as led:
        assert [e["kind"] for e in led.history(limit=1000)].count(
            "LEARNING_PROPOSED"
        ) == 1


def _revoke_update(go, target, update_id):
    with _ledger(target) as led:
        go.wait()
        return led.revoke_update(
            update_id, controller_ref="ctl", reason="rolled back"
        ).value


def test_consequence_update_revocation_race_records_one(manager, target):
    t = target("c.db")
    with _ledger(t) as led:
        ev = _supported_miss(led)
    (update_id,) = set(_race(manager, _propose, [(t, ev.evaluation_id)]))
    with _ledger(t) as led:
        led.record_update_receipt(
            update_id,
            admission=Admission.ACCEPTED,
            application=Application.APPLIED,
            controller_ref="c",
        )
    states = _race(manager, _revoke_update, [(t, update_id)] * WORKERS)
    assert states == ["REVOKED"] * WORKERS
    with _ledger(t) as led:
        assert [e["kind"] for e in led.history(limit=1000)].count("UPDATE_REVOKED") == 1


def _evaluate_new_readings(go, target, worker):
    out = []
    with _ledger(target) as led:
        go.wait()
        for k in range(10):
            led.observe(replace(TEMP, observation_id=f"t-{worker}-{k}", value=21.0))
            try:
                led.evaluate("exp-valve-1", task=TASK)
                out.append("ok")
            except Revoked:
                out.append("revoked")
    return out


def _revoke_expectation(go, target, _worker):
    with _ledger(target) as led:
        go.wait()
        led.revoke_expectation("exp-valve-1", controller_ref="ctl", reason="cancelled")
    return ["revoker"]


def _either(go, target, worker):
    fn = _revoke_expectation if worker == 0 else _evaluate_new_readings
    return fn(go, target, worker)


def test_no_evaluation_lands_after_a_revocation(manager, target):
    t = target("c.db")
    with _ledger(t) as led:
        led.prepare(VALVE)
    _race(manager, _either, [(t, w) for w in range(WORKERS)])
    with _ledger(t) as led:
        history = led.history(limit=1000)
        (revoked,) = [
            e["sequence"] for e in history if e["kind"] == "EXPECTATION_REVOKED"
        ]
        assert all(e["sequence"] < revoked for e in history if e["kind"] == "EVALUATED")
        revisions = [
            r[0]
            for r in led._conn.execute(
                "SELECT revision FROM cl_evaluations ORDER BY revision"
            )
        ]
        assert revisions == list(range(1, len(revisions) + 1))
        assert led.verify_history()
