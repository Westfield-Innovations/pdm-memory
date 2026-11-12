"""Owner revocations: a withdrawn expectation and a withdrawn applied update."""

from __future__ import annotations

from dataclasses import replace
from decimal import Decimal

import pytest

from pdm_memory import ConsequenceLedger
from pdm_memory.consequence import (
    Account,
    Admission,
    Application,
    Cause,
    CauseHypothesis,
    ConsequenceEntry,
    EvidenceKind,
    QuantityStatus,
    ReconcileFirst,
    Revoked,
    Route,
    UpdateState,
)
from pdm_memory.guarded_effects import IdReuseError
from tests.consequence_fixtures import (
    COMMIT,
    END,
    POLICY,
    POSITION_MOVED,
    TASK,
    TEMP,
    VALVE,
    _guard,
    _request,
)

SUPPLY = replace(TEMP, observation_id="obs-supply", source="supply-gauge", observable="supply_bar",
                 unit="bar", value=0.4)
CAUSES = (CauseHypothesis(Cause.EXECUTION, ("obs-supply",)),)


def _open(path, owner="owner-1"):
    return ConsequenceLedger(path, owner=owner, policy=POLICY, clock=lambda: COMMIT)


@pytest.fixture
def ledger(tmp_path):
    with _open(tmp_path / "c.db") as led:
        led.prepare(VALVE)
        yield led


def _miss(ledger):
    ledger.observe(POSITION_MOVED)
    ledger.observe(SUPPLY)
    ledger.observe(replace(TEMP, value=18.0))
    return ledger.evaluate("exp-valve-1", task=TASK, causes=CAUSES)


def _propose(ledger, evaluation_id, version="1"):
    return ledger.propose_learning(
        evaluation_id, task=TASK, criterion="room_temp", target_kind="belief",
        target_id="supply-pressure-ok", target_version=version, change={"reopen": True},
        scope="boiler loop B", confidence=0.7, route=Route.OWN_ACTION,
        evidence_kind=EvidenceKind.MEASURED,
    )


def _revoke(ledger, reason="decision cancelled"):
    ledger.revoke_expectation("exp-valve-1", controller_ref="ctl-7", reason=reason)


def _kinds(ledger):
    return [e["kind"] for e in ledger.history(limit=1000)]


# --- expectations -----------------------------------------------------------


def test_a_revoked_expectation_takes_no_further_evaluation(ledger):
    first = ledger.evaluate("exp-valve-1", task=TASK)
    _revoke(ledger)
    with pytest.raises(Revoked):
        ledger.evaluate("exp-valve-1", task=TASK)
    ledger.observe(POSITION_MOVED)  # facts still append
    with pytest.raises(Revoked):
        ledger.evaluate("exp-valve-1", task=TASK)
    assert ledger.current_evaluation("exp-valve-1") == first


def test_revoking_closes_open_requests_and_stops_new_ones(ledger):
    ledger.evaluate("exp-valve-1", task=TASK)
    assert {p.status for p in ledger.suggest_observation("exp-valve-1")} == {"OPEN"}
    _revoke(ledger)
    assert ledger.suggest_observation("exp-valve-1") == ()
    closed = [e["payload"] for e in ledger.history(limit=1000)
              if e["kind"] == "OBSERVATION_PROPOSAL_CLOSED"]
    assert [p["by"] for p in closed] == ["revoked", "revoked"]
    assert ledger._conn.execute("SELECT COUNT(*) FROM cl_proposals WHERE status='OPEN'").fetchone()[0] == 0


def test_a_revoked_expectation_takes_no_new_learning_but_replays_old(ledger):
    ev = _miss(ledger)
    proposal = _propose(ledger, ev.evaluation_id)
    _revoke(ledger)
    assert _propose(ledger, ev.evaluation_id) == proposal  # a replay, not a new proposal
    with pytest.raises(Revoked):
        _propose(ledger, ev.evaluation_id, version="2")


def test_the_guard_holds_an_action_whose_expectation_was_revoked(ledger, tmp_path):
    calls = []
    (tmp_path / "g").mkdir()
    with _guard(tmp_path / "g", check=ledger.prepared_check, handler=calls.append) as guard:
        _revoke(ledger)
        guard.stage(_request())
        assert guard.release("act-valve-1").status == "HELD"
        verdict = ledger.prepared_check(_request())
        assert verdict.reason == "the expectation was revoked"
        assert calls == []


def test_receipts_and_consequences_still_append(ledger, tmp_path):
    (tmp_path / "g").mkdir()
    with _guard(tmp_path / "g") as guard:
        _revoke(ledger)
        ledger.attach_action("exp-valve-1", guard.stage(_request()))
    entry = ConsequenceEntry("e-1", "exp-valve-1", Account.AGENT_OPERATION, "room 4", Decimal(1), "s",
                             END, "log", QuantityStatus.MEASURED)
    ledger.record_consequence(entry)
    assert ledger.accounts("exp-valve-1").totals


def test_significance_shows_the_revocation_and_permits_no_interruption(tmp_path):
    policy = replace(POLICY, priorities={TASK.task_id: 9}, interruption_priority=1)
    with ConsequenceLedger(tmp_path / "c.db", owner="o", policy=policy, clock=lambda: COMMIT) as led:
        led.prepare(VALVE)
        led.evaluate("exp-valve-1", task=TASK)
        assert led.significance("exp-valve-1").interruption is True
        led.revoke_expectation("exp-valve-1", controller_ref="ctl-7", reason="cancelled")
        sig = led.significance("exp-valve-1")
    assert (sig.revoked, sig.interruption) == (True, False)


def test_a_repeated_revocation_is_recorded_once(ledger):
    _revoke(ledger)
    _revoke(ledger)
    assert _kinds(ledger).count("EXPECTATION_REVOKED") == 1
    with pytest.raises(IdReuseError):
        _revoke(ledger, reason="a different reason")
    assert ledger.revocations() == [
        {"kind": "expectation", "record_id": "exp-valve-1", "expectation_id": "exp-valve-1",
         "controller_ref": "ctl-7", "reason": "decision cancelled"}
    ]
    assert ledger.verify_history()


@pytest.mark.parametrize(
    ("kwargs", "error"),
    [
        ({"controller_ref": "", "reason": "x"}, ValueError),
        ({"controller_ref": "ctl", "reason": "  "}, ValueError),
        ({"controller_ref": "ctl", "reason": None}, ValueError),
    ],
)
def test_a_revocation_names_who_and_why(ledger, kwargs, error):
    with pytest.raises(error):
        ledger.revoke_expectation("exp-valve-1", **kwargs)
    assert ledger.revocations() == []


def test_an_unknown_expectation_cannot_be_revoked(ledger):
    with pytest.raises(KeyError):
        ledger.revoke_expectation("exp-unknown", controller_ref="ctl", reason="x")


def test_another_owners_revocation_does_not_reach_this_one(tmp_path):
    with _open(tmp_path / "c.db", "owner-a") as a, _open(tmp_path / "c.db", "owner-b") as b:
        a.prepare(VALVE)
        b.prepare(VALVE)
        a.revoke_expectation("exp-valve-1", controller_ref="ctl", reason="x")
        assert b.evaluate("exp-valve-1", task=TASK).revision == 1
        assert b.revocations() == []


def test_revocations_survive_a_reopen_and_an_older_file_gains_the_table(tmp_path):
    with _open(tmp_path / "c.db") as led:
        led.prepare(VALVE)
        led._conn.execute("DROP TABLE cl_revocations")  # a file from before revocations
    with _open(tmp_path / "c.db") as led:
        _revoke(led)
    with _open(tmp_path / "c.db") as led:
        with pytest.raises(Revoked):
            led.evaluate("exp-valve-1", task=TASK)
        assert len(led.revocations("exp-valve-1")) == 1 and led.revocations("exp-other") == []
        assert led.verify_history()


# --- updates ----------------------------------------------------------------


def _applied(ledger):
    proposal = _propose(ledger, _miss(ledger).evaluation_id)
    ledger.record_update_receipt(proposal.update_id, admission=Admission.ACCEPTED,
                                 application=Application.APPLIED, controller_ref="ctl-7")
    return proposal


def test_an_applied_update_can_be_revoked_once(ledger):
    proposal = _applied(ledger)
    revoke = {"controller_ref": "ctl-7", "reason": "rolled back"}
    assert ledger.revoke_update(proposal.update_id, **revoke) is UpdateState.REVOKED
    assert ledger.revoke_update(proposal.update_id, **revoke) is UpdateState.REVOKED
    assert ledger.update_state(proposal.update_id) is UpdateState.REVOKED
    assert _kinds(ledger).count("UPDATE_REVOKED") == 1
    with pytest.raises(IdReuseError):
        ledger.revoke_update(proposal.update_id, controller_ref="ctl-8", reason="rolled back")
    with pytest.raises(ValueError, match="cannot follow"):
        ledger.record_update_receipt(proposal.update_id, admission=Admission.ACCEPTED,
                                     application=Application.APPLIED, controller_ref="ctl-7")


def test_a_new_proposal_after_a_revocation_reconciles_nothing(ledger):
    proposal = _applied(ledger)
    ledger.revoke_update(proposal.update_id, controller_ref="ctl-7", reason="rolled back")
    again = _propose(ledger, proposal.evaluation_id, version="2")
    assert again.reconciles is None


def test_without_a_revocation_the_new_proposal_reconciles_the_applied_one(ledger):
    proposal = _applied(ledger)
    assert _propose(ledger, proposal.evaluation_id, version="2").reconciles == proposal.update_id


def test_an_uncertain_update_is_reconciled_before_revocation(ledger):
    proposal = _propose(ledger, _miss(ledger).evaluation_id)
    ledger.record_update_receipt(proposal.update_id, admission=Admission.ACCEPTED, controller_ref="c")
    with pytest.raises(ReconcileFirst):
        ledger.revoke_update(proposal.update_id, controller_ref="c", reason="x")
    assert ledger.update_state(proposal.update_id) is UpdateState.UNCERTAIN


@pytest.mark.parametrize(
    "receipt",
    [
        None,
        {"admission": Admission.REJECTED},
        {"admission": Admission.DEFERRED},
        {"admission": Admission.ACCEPTED, "application": Application.NOT_APPLIED},
    ],
)
def test_only_an_applied_update_can_be_revoked(ledger, receipt):
    proposal = _propose(ledger, _miss(ledger).evaluation_id)
    if receipt:
        ledger.record_update_receipt(proposal.update_id, controller_ref="c", **receipt)
    before = ledger.update_state(proposal.update_id)
    with pytest.raises(ValueError, match="only an applied update"):
        ledger.revoke_update(proposal.update_id, controller_ref="c", reason="x")
    assert ledger.update_state(proposal.update_id) is before
    assert ledger.revocations() == []


def test_an_unknown_update_cannot_be_revoked(ledger):
    with pytest.raises(KeyError):
        ledger.revoke_update("no-such-update", controller_ref="c", reason="x")


def test_revoking_an_update_leaves_its_expectation_live(ledger):
    proposal = _applied(ledger)
    ledger.revoke_update(proposal.update_id, controller_ref="ctl-7", reason="rolled back")
    ledger.observe(replace(TEMP, revision=2, supersedes=1, value=21.0))
    assert ledger.evaluate("exp-valve-1", task=TASK).revision == 2
    assert ledger.revocations("exp-valve-1")[0]["kind"] == "update"
