"""The spec's worked episode: one valve, a guarded request, readings that arrive apart."""

from __future__ import annotations

from dataclasses import replace

from pdm_memory import ConsequenceLedger
from pdm_memory.consequence import (
    Admission,
    Application,
    Cause,
    CauseHypothesis,
    Comparison,
    EvidenceKind,
    Response,
    Route,
    UpdateState,
)
from tests.consequence_fixtures import (
    COMMIT,
    POLICY,
    POSITION_MOVED,
    TASK,
    TEMP,
    VALVE,
    _guard,
    _request,
)

SUPPLY = replace(TEMP, observation_id="obs-supply", observable="supply_bar", value=0.4)


def _open(path):
    return ConsequenceLedger(path, owner="owner-1", policy=POLICY, clock=lambda: COMMIT)


def _steps_1_to_3(ledger, guard):
    # 1. The expectation is retained before the request is released.
    guard.stage(_request())
    ledger.prepare(VALVE)
    # 2. The guard admits and returns; a return says nothing about warmth.
    receipt = guard.release("act-valve-1")
    ledger.attach_action("exp-valve-1", receipt)
    # 3. Position confirms movement; temperature has not arrived.
    ledger.observe(POSITION_MOVED)
    ev = ledger.evaluate("exp-valve-1", task=TASK)
    return receipt, ev, ledger.suggest_observation("exp-valve-1")


def test_the_valve_episode_confirms_and_continues(tmp_path):
    calls = []
    with _open(tmp_path / "c.db") as ledger, _guard(
        tmp_path, check=ledger.prepared_check, handler=calls.append
    ) as guard:
        receipt, ev, proposals = _steps_1_to_3(ledger, guard)
        assert receipt.status == "RETURNED" and receipt.executed is None
        assert ev.finding("moved").comparison is Comparison.CONFIRMED_MATCH
        assert ev.finding("room_temp").comparison is Comparison.UNRESOLVED
        assert [p.criterion for p in proposals] == ["room_temp"]
        assert guard.dispatch_count("open", "valve-3") == 1  # no second movement requested
        # 4a. A temperature in band confirms; continuing needs no adjustment.
        ledger.observe(replace(TEMP, value=21.0))
        done = ledger.evaluate("exp-valve-1", task=TASK)
        assert done.response is Response.CONTINUE
        assert ledger.suggest_observation("exp-valve-1") == ()
        assert len(calls) == 1


def test_the_valve_episode_learns_from_a_supported_miss_once(tmp_path):
    with _open(tmp_path / "c.db") as ledger, _guard(tmp_path, check=ledger.prepared_check) as guard:
        _steps_1_to_3(ledger, guard)
        # 4b. A valid reading misses; a cause backed by its own reading is supported.
        ledger.observe(SUPPLY)
        ledger.observe(replace(TEMP, value=18.0))
        causes = (CauseHypothesis(Cause.EXECUTION, ("obs-supply",)),)
        ev = ledger.evaluate("exp-valve-1", task=TASK, causes=causes)
        assert (ev.response, ev.attribution_supported) == (Response.OPEN_REVIEW, True)
        proposal = ledger.propose_learning(
            ev.evaluation_id,
            criterion="room_temp",
            target_kind="belief",
            target_id="supply-pressure-ok",
            target_version="1",
            change={"reopen": True},
            scope="boiler loop B",
            confidence=0.7,
            route=Route.OWN_ACTION,
            evidence_kind=EvidenceKind.MEASURED,
        )
        # 5. The owner's controller admits and applies it.
        state = ledger.record_update_receipt(
            proposal.update_id,
            admission=Admission.ACCEPTED,
            application=Application.APPLIED,
            controller_ref="ctl-7",
        )
        assert state is UpdateState.APPLIED
        events = len(ledger.history(limit=1000))
        # Replaying the whole episode adds no action and no contribution.
        _steps_1_to_3(ledger, guard)
        ledger.observe(SUPPLY)
        ledger.observe(replace(TEMP, value=18.0))
        again = ledger.evaluate("exp-valve-1", task=TASK, causes=causes)
        assert again == ev
        assert ledger.propose_learning(
            again.evaluation_id,
            criterion="room_temp",
            target_kind="belief",
            target_id="supply-pressure-ok",
            target_version="1",
            change={"reopen": True},
            scope="boiler loop B",
            confidence=0.7,
            route=Route.OWN_ACTION,
            evidence_kind=EvidenceKind.MEASURED,
        ) == proposal
        assert len(ledger.history(limit=1000)) == events
        assert guard.dispatch_count("open", "valve-3") == 1


def test_worker_recovery_mid_episode(tmp_path):
    with _open(tmp_path / "c.db") as ledger, _guard(tmp_path, check=ledger.prepared_check) as guard:
        _, ev, proposals = _steps_1_to_3(ledger, guard)
        events = ledger.history(limit=1000)
    with _open(tmp_path / "c.db") as ledger, _guard(tmp_path, check=ledger.prepared_check) as guard:
        _, again, replayed = _steps_1_to_3(ledger, guard)
        assert (again, replayed) == (ev, proposals)
        assert ledger.history(limit=1000) == events
        assert ledger.verify_history()
