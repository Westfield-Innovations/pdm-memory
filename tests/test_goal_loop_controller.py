"""The Goal Loop controller: states, hysteresis, guard, proof, budget and recovery."""

from __future__ import annotations

import sqlite3
import threading
from dataclasses import replace
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from types import SimpleNamespace

import pytest

from pdm_memory.goal_loop import (
    ActionResult,
    Autonomy,
    Budget,
    CallableChecker,
    ContractHistory,
    DefaultGuard,
    EndConditions,
    EscalationKind,
    GoalContract,
    GoalLoopController,
    GuardDecision,
    InMemoryJobStore,
    InvalidTransition,
    JobState,
    NotArmableError,
    NotExecuted,
    Observation,
    OwnerApproval,
    PromiseChangeError,
    Proof,
    ProofReceipt,
    ProofVerdict,
    SqliteJobStore,
    StepTemplate,
    Trigger,
    TriggerState,
)
from pdm_memory.goal_loop.controller import ApprovalError, _Tx
from pdm_memory.goal_loop.states import TRANSITIONS, check_transition
from pdm_memory.guarded_effects import LedgerIntegrityError

S = JobState
T0 = datetime(2026, 9, 1, 12, 0, tzinfo=timezone.utc)
OWNER = OwnerApproval("eric", "ok")
SOURCE = "sms_delivery_receipt"


class Clock:
    def __init__(self):
        self.now = T0

    def __call__(self):
        return self.now

    def advance(self, **kwargs):
        self.now += timedelta(**kwargs)
        return self.now


class Crash(BaseException):
    """Stands for the process dying: not caught by the controller."""


class Sms:
    """Fake executor. ``plan`` lists outcomes in order; ``ok`` when it runs out."""

    def __init__(self, plan=(), cost="0"):
        self.plan = list(plan)
        self.cost = Decimal(cost)
        self.calls = []

    def __call__(self, step, *, idempotency_key):
        self.calls.append((idempotency_key, step))
        outcome = self.plan.pop(0) if self.plan else "ok"
        if outcome == "not_executed":
            raise NotExecuted("carrier refused")
        if outcome == "boom":
            raise RuntimeError("connection reset")
        if isinstance(outcome, BaseException):
            raise outcome
        if outcome == "garbage":
            return "sent"
        if callable(outcome):
            return outcome(step)
        return ActionResult(reference=f"msg-{len(self.calls)}", cost=self.cost)


class Proofs:
    """Fake checker. ``plan`` lists verdicts in order; ``PROVEN`` when it runs out."""

    def __init__(self, plan=(), source=SOURCE):
        self.source = source
        self.plan = list(plan)
        self.calls = []

    def check(self, contract, step, result, evidence, now):
        self.calls.append((step, result, evidence))
        what = self.plan.pop(0) if self.plan else "PROVEN"
        receipt = ProofReceipt(self.source, "dlr-1", now, evidence)
        if what == "PROVEN":
            return ProofVerdict("PROVEN", "delivered", receipt)
        if what == "PROVEN_NO_PROOF":
            return ProofVerdict("PROVEN", "looks done to me")
        if what == "PROVEN_WRONG_SOURCE":
            return ProofVerdict("PROVEN", "trust me", ProofReceipt("other_source", "x", now))
        if what == "RAISE":
            raise RuntimeError("provider down")
        return ProofVerdict(what, what.lower())


def contract(**kwargs):
    base = dict(
        contract_id="watch-mu",
        owner="eric",
        outcome="Eric gets a text when MU crosses $40 from below",
        proof=Proof(SOURCE),
        never=("Never trade MU shares",),
        trigger=Trigger(
            kind="threshold",
            metric="MU.price",
            direction="rising",
            fire_at="40.00",
            reset_at="39.50",
        ),
        autonomy=Autonomy(allowed=("sms.send",)),
        budget=Budget(deadline=T0 + timedelta(days=30)),
        end=EndConditions(on_success="complete"),
        created_at=T0,
    )
    base.update(kwargs)
    return GoalContract(**base)


def build(store=None, *, sms=None, proofs=None, clock=None, operation="sms.send", **ctl):
    clock = clock or Clock()
    sms = sms or Sms()
    proofs = proofs or Proofs()
    ctl.setdefault("clock", clock)
    controller = GoalLoopController(
        checkers={proofs.source: proofs}, executors={operation: sms}, store=store, **ctl
    )
    return SimpleNamespace(ctl=controller, clock=clock, sms=sms, proofs=proofs)


def rig(
    *,
    store=None,
    sms=None,
    proofs=None,
    operation="sms.send",
    summary="Send SMS to owner: {metric} crossed {fire_at}",
    armed=True,
    contract_kwargs=None,
    **ctl,
):
    r = build(store, sms=sms, proofs=proofs, operation=operation, **ctl)
    history = ContractHistory(contract(**(contract_kwargs or {})))
    r.jid = r.ctl.create(history, step=StepTemplate(operation, "+15550100", summary))
    if armed:
        r.ctl.approve(r.jid, OWNER)
        r.ctl.arm(r.jid)
    r.count = 0
    return r


def feed(r, *values):
    """Send readings one minute apart; return the job after the last one."""
    job = None
    for value in values:
        r.count += 1
        r.clock.advance(minutes=1)
        job = r.ctl.observe(
            r.jid,
            Observation(
                observation_id=f"o{r.count}",
                observed_at=r.clock.now,
                metric="MU.price",
                value=value,
            ),
        )
    return job


def fire(r):
    """Cross the line once; return the job."""
    return feed(r, "39.0", "40.1")


def kinds(r):
    return [(x.kind, x.to_state.value if x.to_state else None) for x in r.ctl.receipts(r.jid)]


# ------------------------------------------------------------ the acceptance criteria


def test_six_readings_above_the_line_produce_one_action():
    r = rig()
    feed(r, "39.0", "40.1", "40.3", "40.2", "40.5", "40.4", "40.6")
    assert len(r.sms.calls) == 1
    job = r.ctl.job(r.jid)
    assert job.state is S.COMPLETE
    assert job.actions_dispatched == 1 and job.fire_seq == 1


def test_complete_needs_a_proof_receipt():
    r = rig()
    job = fire(r)
    assert job.state is S.COMPLETE
    last = r.ctl.receipts(r.jid)[-1]
    assert last.to_state is S.COMPLETE
    assert last.proof is not None and last.proof.source == SOURCE


@pytest.mark.parametrize(
    "verdict, kind",
    [("PROVEN_NO_PROOF", "proof_missing"), ("PROVEN_WRONG_SOURCE", "proof_wrong_source")],
)
def test_looks_done_is_not_proof(verdict, kind):
    r = rig(proofs=Proofs([verdict]))
    job = fire(r)
    assert job.state is S.VERIFYING
    assert [x.kind for x in r.ctl.receipts(r.jid)][-1] == kind
    assert all(x.to_state is not S.COMPLETE for x in r.ctl.receipts(r.jid))


def test_the_state_table_has_one_way_into_complete():
    entrances = [old for old, new in TRANSITIONS.items() if S.COMPLETE in new]
    assert entrances == [S.VERIFYING]
    with pytest.raises(InvalidTransition):
        check_transition(S.WAITING, S.COMPLETE)
    r = rig()
    verifying = replace(r.ctl.job(r.jid), state=S.VERIFYING)
    tx = _Tx(r.ctl, verifying, r.ctl.contract(r.jid).current, r.clock.now)
    with pytest.raises(InvalidTransition, match="proof receipt"):
        tx.move(S.COMPLETE, "looks done to me")


def test_exhausted_action_budget_ends_in_out_of_budget_with_the_reason():
    r = rig(
        contract_kwargs=dict(budget=Budget(max_actions=1), end=EndConditions(on_success="rearm"))
    )
    job = fire(r)
    assert job.state is S.OUT_OF_BUDGET
    assert job.end_reason == "max_actions=1 reached (1 dispatched)"
    last = r.ctl.receipts(r.jid)[-1]
    assert last.to_state is S.OUT_OF_BUDGET
    assert last.reason == job.end_reason
    assert last.proof is not None, "the proven action is still on the record"


def test_deadline_ends_a_waiting_job_on_tick():
    r = rig(contract_kwargs=dict(budget=Budget(deadline=T0 + timedelta(days=1))))
    assert r.ctl.tick(r.clock.advance(hours=23)) == []
    (job,) = r.ctl.tick(r.clock.advance(hours=2))
    assert job.state is S.OUT_OF_BUDGET
    assert job.end_reason == "deadline 2026-09-02T12:00:00+00:00 reached"


# ------------------------------------------------------------ arming


def test_draft_approve_arm_wait():
    r = rig(armed=False)
    assert r.ctl.job(r.jid).state is S.DRAFTED
    with pytest.raises(InvalidTransition):
        r.ctl.arm(r.jid)
    with pytest.raises(ApprovalError):
        r.ctl.approve(r.jid, OwnerApproval("mallory"))
    assert r.ctl.approve(r.jid, OWNER).state is S.APPROVED
    assert r.ctl.arm(r.jid).state is S.WAITING
    assert [k for k, _ in kinds(r)] == ["transition"] * 4


def test_a_job_with_no_proof_source_cannot_be_armed():
    r = rig(armed=False, contract_kwargs=dict(proof=None))
    r.ctl.approve(r.jid, OWNER)
    with pytest.raises(NotArmableError, match="proof source"):
        r.ctl.arm(r.jid)
    assert r.ctl.job(r.jid).state is S.APPROVED


def test_arming_needs_a_checker_and_an_executor():
    r = rig(armed=False, contract_kwargs=dict(proof=Proof("tests_pass")))
    r.ctl.approve(r.jid, OWNER)
    with pytest.raises(NotArmableError, match="no checker"):
        r.ctl.arm(r.jid)
    r.ctl.revise_contract(r.jid, approval=OWNER, proof=Proof(SOURCE))
    other = build(r.ctl._store, operation="email.send")
    with pytest.raises(NotArmableError, match="no executor"):
        other.ctl.arm(r.jid)


def test_arming_after_the_deadline_is_refused():
    r = rig(armed=False, contract_kwargs=dict(budget=Budget(deadline=T0 + timedelta(hours=1))))
    r.ctl.approve(r.jid, OWNER)
    r.clock.advance(hours=2)
    with pytest.raises(NotArmableError, match="deadline"):
        r.ctl.arm(r.jid)


def test_naive_clock_is_refused():
    r = build(clock=lambda: datetime(2026, 9, 1))
    with pytest.raises(ValueError, match="timezone-aware"):
        r.ctl.create(ContractHistory(contract()), step=StepTemplate("sms.send", "x", "y"))


# ------------------------------------------------------------ readings


def test_first_reading_above_the_line_does_not_fire():
    r = rig()
    feed(r, "41.0", "42.0")
    assert r.sms.calls == []
    assert r.ctl.job(r.jid).trigger_state is TriggerState.LATCHED


def test_duplicate_and_stale_readings_are_ignored():
    r = rig()
    feed(r, "39.0")
    before = r.ctl.job(r.jid)
    same = Observation(observation_id="o1", observed_at=r.clock.now, metric="MU.price", value="45")
    assert r.ctl.observe(r.jid, same).revision == before.revision
    old = Observation(
        observation_id="older",
        observed_at=r.clock.now - timedelta(hours=1),
        metric="MU.price",
        value="45",
    )
    assert r.ctl.observe(r.jid, old).revision == before.revision
    assert r.sms.calls == []


def test_readings_for_other_metrics_are_ignored():
    r = rig()
    feed(r, "39.0")
    job = r.ctl.observe(
        r.jid,
        Observation(observation_id="x", observed_at=r.clock.now, metric="NVDA.price", value="99"),
    )
    assert job.trigger_state is TriggerState.ARMED and r.sms.calls == []


def test_reading_that_changes_nothing_leaves_no_receipt():
    r = rig()
    feed(r, "39.0")
    n = len(r.ctl.receipts(r.jid))
    feed(r, "39.1", "39.2")
    assert len(r.ctl.receipts(r.jid)) == n


def test_rearm_fires_again_only_after_a_reset():
    r = rig(contract_kwargs=dict(end=EndConditions(on_success="rearm")))
    feed(r, "39.0", "40.1", "40.3", "39.55", "40.2")
    assert len(r.sms.calls) == 1, "39.55 is not below the reset line"
    feed(r, "39.4", "40.2")
    assert len(r.sms.calls) == 2
    assert r.ctl.job(r.jid).state is S.WAITING


def test_crossing_that_happens_while_busy_is_kept_for_later():
    r = rig(proofs=Proofs(["NOT_YET"]), contract_kwargs=dict(end=EndConditions(on_success="rearm")))
    job = fire(r)
    assert job.state is S.VERIFYING
    feed(r, "39.4", "40.2")
    assert len(r.sms.calls) == 1
    assert r.ctl.job(r.jid).trigger_state is TriggerState.ARMED
    assert "fire_suppressed_busy" in [k for k, _ in kinds(r)]
    r.ctl.submit_evidence(r.jid, r.ctl.job(r.jid).action_id, {"delivered": True})
    assert r.ctl.job(r.jid).state is S.WAITING
    feed(r, "40.3")
    assert len(r.sms.calls) == 2


def test_event_trigger_fires_once_per_event_and_not_while_busy():
    trigger = Trigger(kind="event", event="work_order.received")
    r = rig(
        proofs=Proofs(["NOT_YET"]),
        summary="Notify Eric about {event}",
        contract_kwargs=dict(trigger=trigger, never=()),
    )

    def event(i):
        return Observation(
            observation_id=f"e{i}",
            observed_at=r.clock.advance(minutes=1),
            event="work_order.received",
        )

    assert r.ctl.observe(r.jid, event(1)).state is S.VERIFYING
    assert r.ctl.observe(r.jid, event(1)).revision == r.ctl.observe(r.jid, event(1)).revision
    r.ctl.observe(r.jid, event(2))
    assert len(r.sms.calls) == 1
    assert "fire_suppressed_busy" in [k for k, _ in kinds(r)]
    assert r.sms.calls[0][1].summary == "Notify Eric about work_order.received"


def test_schedule_trigger_reads_schedule_events():
    trigger = Trigger(kind="schedule", schedule="0 9 * * 1-5")
    r = rig(contract_kwargs=dict(trigger=trigger, never=()))
    r.ctl.observe(
        r.jid,
        Observation(observation_id="t1", observed_at=r.clock.now, event="work_order.received"),
    )
    assert r.sms.calls == []
    r.ctl.observe(
        r.jid, Observation(observation_id="t2", observed_at=r.clock.now, event="schedule")
    )
    assert len(r.sms.calls) == 1


def test_idempotency_key_is_the_action_id_and_names_the_contract_version():
    r = rig()
    fire(r)
    ((key, step),) = r.sms.calls
    assert key == step.action_id == "watch-mu:v1:f1"
    assert step.summary == "Send SMS to owner: MU.price crossed 40.00"
    assert step.context["value"] == "40.1"


# ------------------------------------------------------------ the guard


def test_operation_outside_the_autonomy_line_asks_the_owner():
    trade = Sms()
    r = rig(sms=trade, operation="trade.sell", summary="Sell MU position")
    job = fire(r)
    assert job.state is S.ESCALATED
    assert job.escalation_kind is EscalationKind.APPROVAL_REQUIRED
    assert trade.calls == []
    receipt = r.ctl.receipts(r.jid)[-1]
    assert "autonomy line" in receipt.reason
    assert receipt.guard["report"] is None, "the allowlist stops it before verify() runs"


def test_owner_can_approve_an_ask_first_step_and_the_guard_still_runs():
    r = rig(
        operation="email.send",
        summary="Email Eric that MU crossed",
        contract_kwargs=dict(autonomy=Autonomy(ask_first=("email.send",))),
    )
    job = fire(r)
    assert job.escalation_kind is EscalationKind.APPROVAL_REQUIRED
    with pytest.raises(ApprovalError):
        r.ctl.resolve(r.jid, OwnerApproval("mallory"), decision="proceed")
    with pytest.raises(InvalidTransition, match="retry"):
        r.ctl.resolve(r.jid, OWNER, decision="retry")
    job = r.ctl.resolve(r.jid, OWNER, decision="proceed")
    assert job.state is S.COMPLETE
    assert len(r.sms.calls) == 1
    guarded = [x for x in r.ctl.receipts(r.jid) if x.to_state is S.GUARD_CHECKED]
    assert guarded and guarded[-1].guard["report"] is not None, "verify() ran on the approved step"


def test_owner_approval_does_not_override_a_never_rule():
    r = rig(
        operation="email.send",
        summary="buy 100 shares of MU",
        contract_kwargs=dict(autonomy=Autonomy(ask_first=("email.send",))),
    )
    assert fire(r).state is S.ESCALATED
    job = r.ctl.resolve(r.jid, OWNER, decision="proceed")
    assert job.state is S.CONFLICT
    assert r.sms.calls == []


def test_step_that_contradicts_a_never_rule_ends_in_conflict():
    r = rig(
        operation="broker.order",
        summary="buy 100 shares of MU",
        contract_kwargs=dict(autonomy=Autonomy(allowed=("broker.order",))),
    )
    job = fire(r)
    assert job.state is S.CONFLICT
    assert job.end_reason.startswith("conflict:")
    assert r.sms.calls == []


def test_torsion_policy_escalate_asks_a_person_instead():
    r = rig(
        operation="broker.order",
        summary="buy 100 shares of MU",
        guard=DefaultGuard(torsion_policy="escalate"),
        contract_kwargs=dict(autonomy=Autonomy(allowed=("broker.order",))),
    )
    job = fire(r)
    assert job.state is S.ESCALATED
    assert job.escalation_kind is EscalationKind.APPROVAL_REQUIRED


def test_a_guard_that_raises_fails_closed():
    class Broken:
        def check(self, contract, step, *, owner_approved=False):
            raise RuntimeError("model down")

    r = rig(guard=Broken())
    job = fire(r)
    assert job.state is S.ESCALATED
    assert "guard failed" in job.escalation_reason
    assert r.sms.calls == []


def test_custom_guard_decides():
    class Deny:
        def check(self, contract, step, *, owner_approved=False):
            return GuardDecision("conflict", "not today")

    r = rig(guard=Deny())
    assert fire(r).state is S.CONFLICT


# ------------------------------------------------------------ retries and budget


def test_retry_does_not_spend_the_action_budget():
    r = rig(
        sms=Sms(["not_executed", "not_executed"]),
        contract_kwargs=dict(budget=Budget(max_actions=1, max_attempts=3)),
    )
    job = fire(r)
    assert job.state is S.RETRYING
    for _ in range(2):
        r.ctl.tick(job.retry_at)
        job = r.ctl.job(r.jid)
    assert job.state is S.COMPLETE
    assert job.actions_dispatched == 1
    assert job.attempts == {"watch-mu:v1:f1": 3}
    assert {key for key, _ in r.sms.calls} == {"watch-mu:v1:f1"}


def test_backoff_doubles_up_to_the_cap():
    r = rig(
        sms=Sms(["not_executed"] * 6),
        retry_cap=timedelta(minutes=5),
        contract_kwargs=dict(budget=Budget(max_attempts=7)),
    )
    job = fire(r)
    delays = []
    for _ in range(6):
        delays.append((job.retry_at - r.clock.now).total_seconds())
        r.clock.now = job.retry_at
        r.ctl.tick()
        job = r.ctl.job(r.jid)
    assert delays == [30, 60, 120, 240, 300, 300]
    assert job.state is S.COMPLETE


def test_retry_waits_for_its_time():
    r = rig(sms=Sms(["not_executed"]))
    job = fire(r)
    assert r.ctl.tick(r.clock.advance(seconds=10)) == []
    assert len(r.sms.calls) == 1
    assert r.ctl.tick(job.retry_at)
    assert len(r.sms.calls) == 2


def test_last_failed_try_ends_the_job_at_once():
    r = rig(
        sms=Sms(["not_executed", "not_executed"]),
        contract_kwargs=dict(budget=Budget(max_attempts=2)),
    )
    job = fire(r)
    r.ctl.tick(job.retry_at)
    job = r.ctl.job(r.jid)
    assert job.state is S.OUT_OF_BUDGET
    assert job.end_reason == "max_attempts=2 reached for watch-mu:v1:f1"
    assert job.retry_at is None
    assert r.ctl.receipts(r.jid)[-1].from_state is S.ACTING


def test_failed_proof_on_the_last_try_ends_the_job_at_once():
    r = rig(
        proofs=Proofs(["FAILED", "FAILED"]), contract_kwargs=dict(budget=Budget(max_attempts=2))
    )
    job = fire(r)
    assert job.state is S.RETRYING
    r.ctl.tick(job.retry_at)
    job = r.ctl.job(r.jid)
    assert job.state is S.OUT_OF_BUDGET
    assert r.ctl.receipts(r.jid)[-1].from_state is S.VERIFYING
    assert len(r.sms.calls) == 2


def test_attempts_are_bounded_even_when_the_contract_names_no_limit():
    r = rig(sms=Sms(["not_executed"] * 20), default_max_attempts=3)
    job = fire(r)
    while job.state is S.RETRYING:
        r.ctl.tick(job.retry_at)
        job = r.ctl.job(r.jid)
    assert job.state is S.OUT_OF_BUDGET and len(r.sms.calls) == 3


def test_money_budget_stops_the_next_action():
    r = rig(
        sms=Sms(cost="0.75"),
        contract_kwargs=dict(
            budget=Budget(max_cost="1.00", currency="USD"), end=EndConditions(on_success="rearm")
        ),
    )
    feed(r, "39.0", "40.1", "39.0", "40.1")
    job = r.ctl.job(r.jid)
    assert len(r.sms.calls) == 2 and job.cost_spent == Decimal("1.50")
    feed(r, "39.0", "40.1")
    job = r.ctl.job(r.jid)
    assert job.state is S.OUT_OF_BUDGET
    assert job.end_reason == "max_cost=1.00 reached (1.50 spent)"
    assert len(r.sms.calls) == 2


def test_impossible_ends_the_job():
    r = rig(proofs=Proofs(["IMPOSSIBLE"]))
    job = fire(r)
    assert job.state is S.IMPOSSIBLE
    assert job.end_reason == "impossible: impossible"


# ------------------------------------------------------------ uncertain outcomes


def test_unknown_executor_error_escalates_and_never_repeats_on_its_own():
    r = rig(sms=Sms(["boom"]))
    job = fire(r)
    assert job.state is S.ESCALATED
    assert job.escalation_kind is EscalationKind.OUTCOME_UNCERTAIN
    r.ctl.tick(r.clock.advance(days=1))
    feed(r, "39.0", "40.5")
    assert len(r.sms.calls) == 1


def test_executor_returning_junk_is_treated_as_uncertain():
    r = rig(sms=Sms(["garbage"]))
    job = fire(r)
    assert job.escalation_kind is EscalationKind.OUTCOME_UNCERTAIN
    assert "ActionResult" in job.escalation_reason


def test_owner_closes_an_uncertain_step_with_evidence_without_resending():
    r = rig(sms=Sms(["boom"]))
    job = fire(r)
    with pytest.raises(InvalidTransition, match="proceed"):
        r.ctl.resolve(r.jid, OWNER, decision="proceed")
    job = r.ctl.submit_evidence(r.jid, job.action_id, {"dlr": "delivered, seen on the phone"})
    assert job.state is S.COMPLETE
    assert len(r.sms.calls) == 1
    step, result, evidence = r.proofs.calls[-1]
    assert result is None and evidence == {"dlr": "delivered, seen on the phone"}


def test_owner_can_retry_an_uncertain_step_after_checking():
    r = rig(sms=Sms(["boom"]))
    job = fire(r)
    job = r.ctl.resolve(r.jid, OWNER, decision="retry")
    assert job.state is S.COMPLETE
    assert [key for key, _ in r.sms.calls] == ["watch-mu:v1:f1", "watch-mu:v1:f1"]


def test_owner_retry_still_counts_toward_max_attempts():
    r = rig(sms=Sms(["boom"]), contract_kwargs=dict(budget=Budget(max_attempts=1)))
    fire(r)
    job = r.ctl.resolve(r.jid, OWNER, decision="retry")
    assert job.state is S.OUT_OF_BUDGET
    assert len(r.sms.calls) == 1


def test_skipping_an_uncertain_step_cancels_a_one_shot_job_and_rewaits_a_watch():
    r = rig(sms=Sms(["boom"]))
    fire(r)
    assert r.ctl.resolve(r.jid, OWNER, decision="skip").state is S.CANCELLED
    watch = rig(sms=Sms(["boom"]), contract_kwargs=dict(end=EndConditions(on_success="rearm")))
    fire(watch)
    assert watch.ctl.resolve(watch.jid, OWNER, decision="skip").state is S.WAITING


def test_proof_timeout_escalates_and_late_evidence_closes_the_job():
    r = rig(proofs=Proofs(["NOT_YET", "NOT_YET"]))
    job = fire(r)
    assert job.state is S.VERIFYING
    assert r.ctl.tick(r.clock.advance(minutes=5)) == []
    (job,) = r.ctl.tick(r.clock.advance(minutes=6))
    assert job.state is S.ESCALATED
    assert job.escalation_kind is EscalationKind.PROOF_TIMEOUT
    with pytest.raises(InvalidTransition):
        r.ctl.resolve(r.jid, OWNER, decision="proceed")
    job = r.ctl.submit_evidence(r.jid, job.action_id, {"dlr": "ok"})
    assert job.state is S.VERIFYING, "still not proven: the second NOT_YET"
    job = r.ctl.submit_evidence(r.jid, job.action_id, {"dlr": "ok"})
    assert job.state is S.COMPLETE
    assert len(r.sms.calls) == 1


def test_checker_that_raises_never_completes_a_job():
    r = rig(proofs=Proofs(["RAISE"]))
    job = fire(r)
    assert job.state is S.VERIFYING
    assert "checker failed" in r.ctl.receipts(r.jid)[-1].reason


def test_evidence_for_another_action_is_refused():
    r = rig(proofs=Proofs(["NOT_YET"]))
    fire(r)
    with pytest.raises(InvalidTransition, match="no action"):
        r.ctl.submit_evidence(r.jid, "someone-else:v1:f9", {})
    with pytest.raises(TypeError):
        r.ctl.submit_evidence(r.jid, r.ctl.job(r.jid).action_id, object())


# ------------------------------------------------------------ crashes and recovery


def test_a_crash_after_the_reservation_escalates_and_never_runs_twice(tmp_path):
    path = tmp_path / "jobs.db"
    r = rig(store=SqliteJobStore(path), sms=Sms([Crash()]))
    with pytest.raises(Crash):
        fire(r)
    assert r.ctl.job(r.jid).state is S.ACTING

    again = build(SqliteJobStore(path), clock=r.clock)
    assert again.ctl.tick(r.clock.advance(seconds=30)) == []
    (job,) = again.ctl.tick(r.clock.advance(minutes=3))
    assert job.state is S.ESCALATED and job.escalation_kind is EscalationKind.OUTCOME_UNCERTAIN
    assert again.sms.calls == [] and len(r.sms.calls) == 1


def test_a_late_result_for_a_presumed_lost_step_is_still_proved(tmp_path):
    r = rig(store=SqliteJobStore(tmp_path / "jobs.db"), sms=Sms([Crash()]))
    with pytest.raises(Crash):
        fire(r)
    r.ctl.tick(r.clock.advance(minutes=3))
    job = r.ctl.job(r.jid)
    assert job.state is S.ESCALATED
    step = job.step

    def handler(j, h, now):
        return r.ctl._on_result(j, h.current, now, step, ("ok", ActionResult(reference="late"), ""))

    r.ctl._mutate(r.jid, handler)
    r.ctl._judge(r.jid, job.action_id, None)
    assert r.ctl.job(r.jid).state is S.COMPLETE
    assert "late_result" in [k for k, _ in kinds(r)]


def test_job_survives_the_process(tmp_path):
    path = tmp_path / "jobs.db"
    first = rig(store=SqliteJobStore(path))
    feed(first, "39.0")
    first.ctl._store.close()

    second = build(SqliteJobStore(path), clock=first.clock)
    second.count = 1
    second.jid = first.jid
    feed(second, "40.2")
    assert len(second.sms.calls) == 1 and second.ctl.job(first.jid).state is S.COMPLETE
    assert second.ctl.verify_receipts(first.jid)


def test_a_job_left_triggered_is_finished_by_tick():
    r = rig()
    real = r.ctl._guard_and_reserve
    r.ctl._guard_and_reserve = lambda tx: None
    assert fire(r).state is S.TRIGGERED
    r.ctl._guard_and_reserve = real
    (job,) = r.ctl.tick(r.clock.now)
    assert job.state is S.COMPLETE and len(r.sms.calls) == 1


def test_a_job_left_guard_checked_is_finished_by_tick():
    r = rig()
    real = r.ctl._reserve
    r.ctl._reserve = lambda tx: None
    assert fire(r).state is S.GUARD_CHECKED
    r.ctl._reserve = real
    (job,) = r.ctl.tick(r.clock.now)
    assert job.state is S.COMPLETE and len(r.sms.calls) == 1


def test_running_step_is_not_mistaken_for_a_lost_one():
    seen = {}

    def slow(step):
        r.clock.advance(minutes=10)
        seen["tick"] = r.ctl.tick()
        return ActionResult(reference="slow")

    r = rig(sms=Sms([slow]))
    fire(r)
    assert seen["tick"] == []
    assert r.ctl.job(r.jid).state is S.COMPLETE


# ------------------------------------------------------------ cancel and revise


def test_cancel_stops_a_waiting_job_and_a_finished_job_cannot_be_cancelled():
    r = rig()
    job = r.ctl.cancel(r.jid, "no longer needed")
    assert job.state is S.CANCELLED and job.end_reason == "cancelled: no longer needed"
    with pytest.raises(InvalidTransition):
        r.ctl.cancel(r.jid, "again")
    with pytest.raises(ValueError):
        rig().ctl.cancel("watch-mu", " ")


def test_cancel_during_a_running_step_takes_effect_when_it_returns():
    def cancel_now(step):
        mid = r.ctl.cancel(r.jid, "changed my mind")
        assert mid.state is S.ACTING and mid.cancel_requested
        return ActionResult(reference="sent anyway")

    r = rig(sms=Sms([cancel_now]))
    job = fire(r)
    assert job.state is S.CANCELLED
    last = r.ctl.receipts(r.jid)[-1]
    assert last.detail["reference"] == "sent anyway", "the receipt says the text did go out"


def test_revising_the_promise_needs_the_owner_and_is_recorded():
    r = rig()
    with pytest.raises(PromiseChangeError):
        r.ctl.revise_contract(r.jid, outcome="Eric gets a text at $45")
    with pytest.raises(PromiseChangeError):
        r.ctl.revise_contract(r.jid, approval=OwnerApproval("mallory"), outcome="x")
    job = r.ctl.revise_contract(
        r.jid, approval=OWNER, reason="new target", outcome="Eric gets a text at $45"
    )
    assert job.contract_version == 2
    history = r.ctl.contract(r.jid)
    assert [v.version for v in history.versions()] == [1, 2]
    receipt = r.ctl.receipts(r.jid)[-1]
    assert receipt.kind == "contract_revised"
    assert receipt.detail["fields"] == ["outcome"] and receipt.contract_version == 2
    assert r.ctl.verify_receipts(r.jid)


def test_new_actions_carry_the_new_contract_version():
    r = rig()
    r.ctl.revise_contract(r.jid, preferences=("short",))
    fire(r)
    assert r.sms.calls[0][0] == "watch-mu:v2:f1"


def test_changing_the_trigger_resets_its_state():
    r = rig()
    feed(r, "39.0")
    assert r.ctl.job(r.jid).trigger_state is TriggerState.ARMED
    new = Trigger(
        kind="threshold", metric="MU.price", direction="rising", fire_at="50", reset_at="49"
    )
    job = r.ctl.revise_contract(r.jid, trigger=new)
    assert job.trigger_state is TriggerState.UNKNOWN


def test_revision_that_leaves_the_job_unarmable_is_refused():
    r = rig()
    with pytest.raises(NotArmableError):
        r.ctl.revise_contract(r.jid, approval=OWNER, proof=None)
    assert r.ctl.job(r.jid).contract_version == 1
    assert len(r.ctl.contract(r.jid).versions()) == 1


def test_contract_cannot_change_while_a_step_is_in_flight():
    seen = {}

    def revise(step):
        try:
            r.ctl.revise_contract(r.jid, preferences=("x",))
        except InvalidTransition as exc:
            seen["error"] = str(exc)
        return ActionResult(reference="ok")

    r = rig(sms=Sms([revise]))
    fire(r)
    assert "in flight" in seen["error"]


def test_revising_drops_a_step_that_was_waiting_for_approval():
    r = rig(
        operation="email.send",
        summary="Email Eric",
        contract_kwargs=dict(autonomy=Autonomy(ask_first=("email.send",))),
    )
    assert fire(r).state is S.ESCALATED
    job = r.ctl.revise_contract(r.jid, preferences=("x",))
    assert job.state is S.WAITING and job.step is None and job.escalation_kind is None
    assert [x.kind for x in r.ctl.receipts(r.jid)][-2:] == [
        "contract_revised",
        "pending_step_dropped",
    ]


def test_contract_is_not_revisable_while_an_outcome_is_unresolved():
    r = rig(sms=Sms(["boom"]))
    fire(r)
    with pytest.raises(InvalidTransition):
        r.ctl.revise_contract(r.jid, preferences=("x",))


# ------------------------------------------------------------ receipts


def test_every_move_leaves_exactly_one_chained_receipt():
    r = rig(contract_kwargs=dict(end=EndConditions(on_success="rearm")))
    fire(r)
    job = r.ctl.job(r.jid)
    receipts = r.ctl.receipts(r.jid)
    assert [x.seq for x in receipts] == list(range(1, len(receipts) + 1))
    assert job.receipt_seq == len(receipts) and job.receipt_head == receipts[-1].receipt_hash
    moves = [(x.from_state, x.to_state) for x in receipts if x.to_state is not None]
    states = [moves[0][1]] + [b for _, b in moves[1:]]
    assert [a for a, _ in moves[1:]] == states[:-1], "no move is missing between two receipts"
    assert states == [
        S.DRAFTED,
        S.APPROVED,
        S.ARMED,
        S.WAITING,
        S.TRIGGERED,
        S.GUARD_CHECKED,
        S.ACTING,
        S.VERIFYING,
        S.WAITING,
    ]
    assert r.ctl.verify_receipts(r.jid)


def test_the_chain_notices_tampering(tmp_path):
    path = tmp_path / "jobs.db"
    r = rig(store=SqliteJobStore(path))
    fire(r)
    assert r.ctl.verify_receipts(r.jid)
    with sqlite3.connect(path) as db:
        db.execute(
            "UPDATE gl_receipts SET receipt_json="
            "replace(receipt_json, 'waiting for a trigger', 'nothing to see') WHERE seq=4"
        )
    with pytest.raises(LedgerIntegrityError):
        r.ctl.verify_receipts(r.jid)


def test_the_chain_notices_a_missing_receipt(tmp_path):
    path = tmp_path / "jobs.db"
    r = rig(store=SqliteJobStore(path))
    fire(r)
    with sqlite3.connect(path) as db:
        db.execute("DELETE FROM gl_receipts WHERE seq=5")
    with pytest.raises(LedgerIntegrityError):
        r.ctl.verify_receipts(r.jid)


# ------------------------------------------------------------ concurrency


def test_concurrent_readings_still_produce_one_action(tmp_path):
    r = rig(store=SqliteJobStore(tmp_path / "jobs.db"), max_cas_retries=64)
    feed(r, "39.0")
    errors = []
    start = threading.Barrier(6)

    def worker(i):
        try:
            start.wait()
            r.ctl.observe(
                r.jid,
                Observation(
                    observation_id=f"c{i}",
                    observed_at=r.clock.now + timedelta(seconds=i),
                    metric="MU.price",
                    value="41",
                ),
            )
        except Exception as exc:  # pragma: no cover - reported below
            errors.append(exc)

    threads = [threading.Thread(target=worker, args=(i,)) for i in range(6)]
    [t.start() for t in threads]
    [t.join() for t in threads]
    assert errors == []
    assert len(r.sms.calls) == 1
    assert r.ctl.verify_receipts(r.jid)


def test_default_store_is_in_memory_and_needs_no_configuration():
    r = rig()
    assert isinstance(r.ctl._store, InMemoryJobStore)
    assert fire(r).state is S.COMPLETE


def test_checker_registered_by_name_not_by_its_own_attribute():
    seen = []
    checker = CallableChecker(
        SOURCE,
        lambda c, step, result, evidence, now: (
            seen.append(step.action_id)
            or ProofVerdict("PROVEN", "", ProofReceipt(SOURCE, "r", now))
        ),
    )
    r = build(proofs=checker)
    jid = r.ctl.create(
        ContractHistory(contract()), step=StepTemplate("sms.send", "+1", "Text Eric")
    )
    r.ctl.approve(jid, OWNER)
    r.ctl.arm(jid)
    r.ctl.observe(
        jid, Observation(observation_id="a", observed_at=r.clock.now, metric="MU.price", value="39")
    )
    r.ctl.observe(
        jid,
        Observation(
            observation_id="b",
            observed_at=r.clock.advance(seconds=1),
            metric="MU.price",
            value="41",
        ),
    )
    assert seen == ["watch-mu:v1:f1"] and r.ctl.job(jid).state is S.COMPLETE


def test_the_watch_price_example_sends_one_text_and_keeps_an_intact_chain(tmp_path):
    from pdm_memory.examples.goal_loop_watch_price import run_example

    result = run_example(tmp_path)
    assert result["texts_sent"] == 1
    assert result["state"] == "waiting"
    assert result["chain_intact"] and result["promise_change_refused"]
    assert result["proof"].source == "sms_delivery_receipt"
