"""Learning proposals and the owner's update receipts."""

from __future__ import annotations

from dataclasses import replace

import pytest

from pdm_memory import ConsequenceLedger, Memory
from pdm_memory.consequence import (
    PROTECTED_TARGETS,
    Admission,
    Application,
    Cause,
    CauseHypothesis,
    EvidenceKind,
    NotSupported,
    ReconcileFirst,
    Route,
    UnauthorizedChange,
    UpdateState,
)
from tests.consequence_fixtures import (
    COMMIT,
    HORIZON,
    POLICY,
    POSITION_MOVED,
    TASK,
    TEMP,
    VALVE,
)

SUPPLY = replace(TEMP, observation_id="obs-supply", observable="supply_bar", value=0.4)
SUPPORTED = (CauseHypothesis(Cause.EXECUTION, ("obs-supply",)),)


@pytest.fixture
def ledger(tmp_path):
    with ConsequenceLedger(tmp_path / "c.db", owner="owner-1", policy=POLICY,
                           clock=lambda: COMMIT) as led:
        yield led


def _miss(ledger, *, causes=SUPPORTED, expectation=VALVE, temp=25.0, conditions=None):
    ledger.prepare(expectation)
    for o in (POSITION_MOVED, SUPPLY, replace(TEMP, value=temp, conditions=conditions or {})):
        ledger.observe(o)
    return ledger.evaluate("exp-valve-1", task=TASK, causes=causes)


def _correct(ledger, value, revision=2, causes=SUPPORTED):
    ledger.observe(replace(TEMP, revision=revision, supersedes=revision - 1, value=value))
    return ledger.evaluate("exp-valve-1", task=TASK, causes=causes)


def _propose(ledger, ev, **changes):
    args = {
        "criterion": "room_temp",
        "target_kind": "predictor",
        "target_id": "valve-model",
        "target_version": "1",
        "change": {"bias_c": -1.5},
        "scope": "room 4, heating season",
        "confidence": 0.6,
        "route": Route.OWN_ACTION,
        "evidence_kind": EvidenceKind.MEASURED,
        **changes,
    }
    return ledger.propose_learning(ev.evaluation_id, **args)


def _kinds(ledger):
    return [e["kind"] for e in ledger.history("exp-valve-1")]


@pytest.mark.parametrize("target", sorted(PROTECTED_TARGETS | {"budget"}))
def test_no_authority_over_protected_or_unadmitted_targets(ledger, target):
    ev = _miss(ledger)
    with pytest.raises(UnauthorizedChange):
        _propose(ledger, ev, target_kind=target)
    assert "LEARNING_PROPOSED" not in _kinds(ledger)


def test_a_supported_miss_gets_one_proposal(ledger):
    ev = _miss(ledger)
    p = _propose(ledger, ev)
    assert (p.target_kind, p.reconciles, ledger.update_state(p.update_id)) == (
        "predictor",
        None,
        UpdateState.PROPOSED,
    )


def test_a_miss_with_an_unresolved_cause_defers_learning(ledger):
    ev = _miss(ledger, causes=(CauseHypothesis(Cause.MODEL_LIMITATION),))
    with pytest.raises(NotSupported, match="attribution"):
        _propose(ledger, ev)


def test_an_out_of_scope_finding_never_becomes_a_penalty(ledger):
    guarded = replace(
        VALVE, conditions={"door": "closed"}, horizon=replace(HORIZON, invalidated_by=("door",))
    )
    ev = _miss(ledger, expectation=guarded, conditions={"door": "open"})
    with pytest.raises(NotSupported, match="OUT_OF_SCOPE"):
        _propose(ledger, ev)


def test_an_unresolved_finding_supports_nothing(ledger):
    ledger.prepare(VALVE)
    ev = ledger.evaluate("exp-valve-1", task=TASK)
    with pytest.raises(NotSupported):
        _propose(ledger, ev)


def test_a_superseded_evaluation_supports_nothing(ledger):
    old = _miss(ledger)
    _correct(ledger, 26.0)
    with pytest.raises(NotSupported, match="superseded"):
        _propose(ledger, old)


def test_another_actors_evidence_does_not_calibrate_this_interface(ledger):
    ev = _miss(ledger, temp=21.0)
    with pytest.raises(NotSupported, match="interface"):
        _propose(ledger, ev, target_kind="interface_calibration", route=Route.OTHER_ACTOR,
                 interface_id="valve-3")
    with pytest.raises(NotSupported, match="interface"):
        _propose(ledger, ev, target_kind="interface_calibration", interface_id="valve-9")
    own = _propose(ledger, ev, target_kind="interface_calibration", interface_id="valve-3")
    assert own.interface_id == "valve-3"
    assert _propose(ledger, ev, route=Route.OTHER_ACTOR).target_kind == "predictor"


def test_a_replayed_proposal_is_one_contribution(ledger):
    ev = _miss(ledger)
    assert _propose(ledger, ev) == _propose(ledger, ev)
    assert _kinds(ledger).count("LEARNING_PROPOSED") == 1


def test_acceptance_without_an_application_receipt_is_uncertain(ledger):
    p = _propose(ledger, _miss(ledger))
    state = ledger.record_update_receipt(p.update_id, admission=Admission.ACCEPTED,
                                         controller_ref="ctl-1")
    assert state is UpdateState.UNCERTAIN


def test_an_uncertain_update_must_be_reconciled_first(ledger):
    p = _propose(ledger, _miss(ledger))
    ledger.record_update_receipt(p.update_id, admission=Admission.ACCEPTED, controller_ref="ctl-1")
    ev = _correct(ledger, 26.0)
    with pytest.raises(ReconcileFirst):
        _propose(ledger, ev, change={"bias_c": -2.0})
    ledger.record_update_receipt(p.update_id, admission=Admission.ACCEPTED,
                                 application=Application.NOT_APPLIED, controller_ref="ctl-1")
    assert _propose(ledger, ev, change={"bias_c": -2.0}).reconciles is None


def test_learning_already_applied_gets_a_reconciliation_request(ledger):
    first = _propose(ledger, _miss(ledger))
    ledger.record_update_receipt(first.update_id, admission=Admission.ACCEPTED,
                                 application=Application.APPLIED, controller_ref="ctl-1")
    ev = _correct(ledger, 26.0)
    assert _propose(ledger, ev) == first  # the same change is the same contribution
    second = _propose(ledger, ev, change={"bias_c": -2.0}, target_version="2")
    assert second.reconciles == first.update_id


def test_a_receipt_cannot_contradict_a_final_state(ledger):
    p = _propose(ledger, _miss(ledger))
    applied = {
        "admission": Admission.ACCEPTED,
        "application": Application.APPLIED,
        "controller_ref": "ctl-1",
    }
    assert ledger.record_update_receipt(p.update_id, **applied) is UpdateState.APPLIED
    events = len(ledger.history())
    assert ledger.record_update_receipt(p.update_id, **applied) is UpdateState.APPLIED
    assert len(ledger.history()) == events
    with pytest.raises(ValueError, match="cannot follow"):
        ledger.record_update_receipt(p.update_id, admission=Admission.ACCEPTED,
                                     application=Application.NOT_APPLIED, controller_ref="ctl-1")
    with pytest.raises(ValueError, match="accepted"):
        ledger.record_update_receipt(p.update_id, admission=Admission.REJECTED,
                                     application=Application.APPLIED, controller_ref="ctl-1")


def test_deferral_then_rejection(ledger):
    p = _propose(ledger, _miss(ledger))
    assert ledger.record_update_receipt(p.update_id, admission=Admission.DEFERRED,
                                        controller_ref="ctl-1") is UpdateState.DEFERRED
    assert ledger.record_update_receipt(p.update_id, admission=Admission.REJECTED,
                                        controller_ref="ctl-1") is UpdateState.REJECTED


def _boom(*args, **kwargs):
    raise AssertionError("the consequence ledger must not write to a memory")


def test_the_ledger_never_touches_memory(ledger, monkeypatch):
    for name in ("reinforce", "penalize", "apply_contrary_evidence", "save"):
        monkeypatch.setattr(Memory, name, _boom)
    p = _propose(ledger, _miss(ledger))
    ledger.record_update_receipt(p.update_id, admission=Admission.ACCEPTED,
                                 application=Application.APPLIED, controller_ref="ctl-1")
    assert ledger.update_state(p.update_id) is UpdateState.APPLIED
