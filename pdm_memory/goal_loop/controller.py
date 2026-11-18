"""The Goal Loop controller: moves one job through its states and never loses track.

It is not clever. It does not think, plan or schedule. The host (AZUS, a cron
job, a webhook) hands it readings with ``observe()`` and calls ``tick()`` now
and then; between those calls it costs nothing. Every move is checked against
the state table, written together with its receipt in one compare-and-swap
commit, and chained by hash.

What it enforces:

* One trigger firing becomes one action. A reading above the line fires once;
  the trigger re-arms only after a reset (hysteresis, ``triggers.py``). Each
  firing has a fixed ``action_id`` that is also the executor's idempotency key.
* Every step passes the guard first, including retries and steps an owner
  approved. The guard asks whether the step is allowed.
* An executor's return is not proof. The job reaches ``complete`` only when the
  checker names a proof receipt from the contract's proof source.
* The budget (actions, tries, cost) ends a job in ``out_of_budget`` with the reason.
* The deadline ends the watch period: no new firing after it, but a step already
  fired still gets its retry and its proof. The job then ends ``complete`` only
  if every action in the period was proved, ``stopped`` otherwise.
* A step that may have run is never run again on its own. It escalates; a person
  (or a delivery receipt) settles it. A question nobody answers within
  ``confirm_expiry`` stops the job as unconfirmed.
* A guard that fails is a failed check: the step does not run and is checked
  again on the next try. A person is asked only when the tries are spent.

Not covered: the controller is cooperative. It cannot stop an executor or a
caller that bypasses it, and "owner approval" means the approval names the
contract's owner; whether a person really gave it is the host's responsibility.
Guards, checkers and executors run on the caller's thread, so keep them bounded.
"""

from __future__ import annotations

import threading
from collections.abc import Callable, Mapping
from dataclasses import replace
from datetime import datetime, timedelta
from typing import Any, Literal

from pdm_memory.goal_loop import _json
from pdm_memory.goal_loop.checker import Checker, ProofReceipt, ProofVerdict
from pdm_memory.goal_loop.contract import (
    ContractHistory,
    GoalContract,
    NotArmableError,
    OwnerApproval,
    changed_fields,
    ensure_armable,
)
from pdm_memory.goal_loop.executor import ActionResult, Executor, NotExecuted
from pdm_memory.goal_loop.guard import DefaultGuard, Guard
from pdm_memory.goal_loop.job import MAX_SEEN, Job, Observation, ProposedStep, StepTemplate
from pdm_memory.goal_loop.receipts import StepReceipt, verify_chain
from pdm_memory.goal_loop.states import (
    TERMINAL,
    EscalationKind,
    InvalidTransition,
    JobState,
    check_transition,
)
from pdm_memory.goal_loop.store import InMemoryJobStore, JobStore, StaleJobError
from pdm_memory.goal_loop.triggers import TriggerState, step_threshold

__all__ = ["ApprovalError", "GoalLoopController"]

S = JobState
Decision = Literal["proceed", "skip", "retry"]

# A contract can be revised only while no step is in flight.
REVISABLE = frozenset({S.DRAFTED, S.APPROVED, S.WAITING, S.ESCALATED})
# States that read observations.
LISTENING = frozenset(
    {S.WAITING, S.TRIGGERED, S.GUARD_CHECKED, S.ACTING, S.RETRYING, S.VERIFYING, S.ESCALATED}
)
# A job can be paused only while no step is in flight.
PAUSABLE = frozenset({S.WAITING, S.RETRYING, S.ESCALATED})
# Escalations that ask whether a step happened; they expire after ``confirm_expiry``.
QUESTIONS = frozenset({EscalationKind.OUTCOME_UNCERTAIN, EscalationKind.PROOF_TIMEOUT})


class ApprovalError(ValueError):
    """An approval that does not come from the contract's owner."""


def _clear_step() -> dict[str, Any]:
    return {
        "step": None,
        "action_id": None,
        "result": None,
        "approved_action_id": None,
        "attempts": {},
        "check_failures": {},
    }


class _Tx:
    """Builds one commit: the new job state plus its receipts, in order."""

    def __init__(
        self, ctl: GoalLoopController, job: Job, contract: GoalContract, now: datetime
    ) -> None:
        self.ctl = ctl
        self.base = job
        self.job = job
        self.contract = contract
        self.now = now
        self.receipts: list[StepReceipt] = []
        self.dispatch: ProposedStep | None = None
        self.history_dict: dict[str, Any] | None = None

    def touch(self, **fields: Any) -> None:
        """Change the job without a receipt (for example the last reading seen)."""
        self.job = replace(self.job, **fields)

    def _record(
        self,
        kind: str,
        reason: str,
        from_state: JobState | None,
        to_state: JobState | None,
        guard: dict[str, Any] | None,
        proof: ProofReceipt | None,
        detail: dict[str, Any] | None,
    ) -> None:
        receipt = StepReceipt.build(
            job_id=self.job.job_id,
            seq=self.job.receipt_seq + 1,
            at=self.now,
            kind=kind,
            from_state=from_state,
            to_state=to_state,
            contract_version=self.job.contract_version,
            action_id=self.job.action_id,
            reason=reason,
            guard=guard,
            proof=proof,
            detail=detail or {},
            prev_hash=self.job.receipt_head,
        )
        self.receipts.append(receipt)
        self.job = replace(self.job, receipt_seq=receipt.seq, receipt_head=receipt.receipt_hash)

    def note(
        self,
        kind: str,
        reason: str,
        *,
        guard: dict[str, Any] | None = None,
        proof: ProofReceipt | None = None,
        detail: dict[str, Any] | None = None,
        **fields: Any,
    ) -> None:
        """Record something that does not move the job."""
        self.job = replace(self.job, **fields)
        self._record(kind, reason, self.job.state, None, guard, proof, detail)

    def move(
        self,
        to: JobState,
        reason: str,
        *,
        kind: str = "transition",
        guard: dict[str, Any] | None = None,
        proof: ProofReceipt | None = None,
        detail: dict[str, Any] | None = None,
        **fields: Any,
    ) -> None:
        old = self.job.state
        check_transition(old, to)
        if to is S.COMPLETE and proof is None:
            raise InvalidTransition("a job cannot complete without a proof receipt")
        if to is not S.ACTING:
            fields.setdefault("dispatch_reserved", None)
            fields.setdefault("lease_expires_at", None)
        if to is not S.VERIFYING:
            fields.setdefault("proof_deadline", None)
        if to is not S.PAUSED:  # a pause keeps what it needs to resume
            if to is not S.RETRYING:
                fields.setdefault("retry_at", None)
            if to is not S.ESCALATED:
                fields.setdefault("escalation_kind", None)
                fields.setdefault("escalation_reason", "")
                fields.setdefault("confirm_by", None)
            elif fields.get("escalation_kind") in QUESTIONS and old is not S.PAUSED:
                fields.setdefault("confirm_by", self.now + self.ctl._confirm_expiry)
        self.job = replace(self.job, state=to, **fields)
        self._record(kind, reason, old, to, guard, proof, detail)

    def finish(self) -> tuple[Job, list[StepReceipt]]:
        job = replace(self.job, revision=self.base.revision + 1, updated_at=self.now)
        job = replace(job, next_due_at=self.ctl._next_due(job, self.contract))
        return job, self.receipts


Handler = Callable[[Job, ContractHistory, datetime], "_Tx | None"]


class GoalLoopController:
    """Owns each job's state from ``drafted`` to a final state.

    Args:
        store: where jobs live. Defaults to a process-local ``InMemoryJobStore``;
            pass a ``SqliteJobStore`` (or your own ``JobStore``) for jobs that
            must outlive the process.
        guard: asks whether a step is allowed. Defaults to ``DefaultGuard()``.
        checkers: proof source name to the ``Checker`` that can verify it.
        executors: operation name to the ``Executor`` that performs it.
        clock: returns the current timezone-aware time.
        retry_base, retry_cap: backoff between tries of one action:
            ``min(retry_base * 2 ** (attempt - 1), retry_cap)``, no jitter.
        proof_timeout: how long to wait for a proof before asking the owner.
        action_timeout: how long a dispatch is presumed in flight. After that a
            job still in ``acting`` is escalated as outcome uncertain.
        confirm_expiry: how long a question to the owner ("did it happen?")
            stays open. Unanswered, the job stops as unconfirmed, never complete.
        default_max_attempts: tries per action when the contract's budget names
            no ``max_attempts``: the first try and one retry, as in AZUS goals.
            A failed guard check counts as a try.
    """

    def __init__(
        self,
        *,
        checkers: Mapping[str, Checker],
        executors: Mapping[str, Executor],
        store: JobStore | None = None,
        guard: Guard | None = None,
        clock: Callable[[], datetime] = _json.utc_now,
        retry_base: timedelta = timedelta(seconds=30),
        retry_cap: timedelta = timedelta(minutes=30),
        proof_timeout: timedelta = timedelta(minutes=10),
        action_timeout: timedelta = timedelta(minutes=2),
        confirm_expiry: timedelta = timedelta(days=7),
        default_max_attempts: int = 2,
        max_cas_retries: int = 8,
    ) -> None:
        for label, value in [
            ("retry_base", retry_base),
            ("retry_cap", retry_cap),
            ("proof_timeout", proof_timeout),
            ("action_timeout", action_timeout),
            ("confirm_expiry", confirm_expiry),
        ]:
            if not isinstance(value, timedelta) or value <= timedelta(0):
                raise ValueError(f"{label} must be a positive timedelta")
        if type(default_max_attempts) is not int or default_max_attempts < 1:
            raise ValueError("default_max_attempts must be a positive integer")
        self._store = InMemoryJobStore() if store is None else store
        self._guard: Guard = DefaultGuard() if guard is None else guard
        self._checkers = dict(checkers)
        self._executors = dict(executors)
        self._clock = clock
        self._retry_base, self._retry_cap = retry_base, retry_cap
        self._proof_timeout, self._action_timeout = proof_timeout, action_timeout
        self._confirm_expiry = confirm_expiry
        self._default_max_attempts = default_max_attempts
        self._max_cas_retries = max_cas_retries
        self._lock = threading.RLock()
        self._in_flight: set[str] = set()

    # ------------------------------------------------------------ helpers

    def _now(self) -> datetime:
        return _json.require_aware(self._clock(), "clock()")

    def _mutate(
        self, job_id: str, handler: Handler, *, now: datetime | None = None
    ) -> tuple[Job, ProposedStep | None]:
        """Load, apply ``handler``, commit; reload and repeat if another writer won.

        The handler must be pure: it can run more than once.
        """
        for _ in range(self._max_cas_retries):
            job = self._store.load(job_id)
            history = ContractHistory.from_dict(self._store.contract_history(job_id))
            at = self._now() if now is None else now
            tx = handler(job, history, at)
            if tx is None:
                return job, None
            final, receipts = tx.finish()
            try:
                self._store.commit(
                    final,
                    receipts,
                    expected_revision=job.revision,
                    contract_history=tx.history_dict,
                )
            except StaleJobError:
                continue
            return final, tx.dispatch
        raise StaleJobError(
            f"job {job_id} kept changing; gave up after {self._max_cas_retries} tries"
        )

    def _next_due(self, job: Job, contract: GoalContract) -> datetime | None:
        if job.state in TERMINAL or job.state in (S.DRAFTED, S.APPROVED, S.ARMED):
            return None
        if job.state is S.ACTING:
            return job.lease_expires_at
        if job.state is S.PAUSED:
            return None
        moments: list[datetime] = []
        if contract.budget.deadline is not None and self._closable(job):
            moments.append(contract.budget.deadline)
        if job.state is S.ESCALATED and job.confirm_by is not None:
            moments.append(job.confirm_by)
        if job.state in (S.TRIGGERED, S.GUARD_CHECKED):
            moments.append(job.updated_at)
        if job.state is S.RETRYING and job.retry_at is not None:
            moments.append(job.retry_at)
        if job.state is S.VERIFYING and job.proof_deadline is not None:
            moments.append(job.proof_deadline)
        return min(moments) if moments else None

    def _max_attempts(self, contract: GoalContract) -> int:
        return contract.budget.max_attempts or self._default_max_attempts

    @staticmethod
    def _tries(job: Job) -> int:
        """Tries spent on the current action: dispatches plus failed guard checks."""
        assert job.action_id is not None
        return job.attempts.get(job.action_id, 0) + job.check_failures.get(job.action_id, 0)

    def _retry_delay(self, attempt: int) -> timedelta:
        return min(self._retry_base * (2 ** min(max(attempt - 1, 0), 30)), self._retry_cap)

    @staticmethod
    def _deadline_reason(contract: GoalContract, now: datetime) -> str | None:
        deadline = contract.budget.deadline
        if deadline is not None and now >= deadline:
            return f"deadline {_json.format_datetime(deadline)} reached"
        return None

    @staticmethod
    def _budget_reason(job: Job, contract: GoalContract, *, new_action: bool) -> str | None:
        budget = contract.budget
        if new_action and budget.max_actions is not None:
            if job.actions_dispatched >= budget.max_actions:
                return (
                    f"max_actions={budget.max_actions} reached "
                    f"({job.actions_dispatched} dispatched)"
                )
        if budget.max_cost is not None and job.cost_spent >= budget.max_cost:
            return f"max_cost={budget.max_cost} reached ({job.cost_spent} spent)"
        return None

    def _attempts_reason(self, contract: GoalContract, action_id: str) -> str:
        return f"max_attempts={self._max_attempts(contract)} reached for {action_id}"

    @staticmethod
    def _check_owner(approval: OwnerApproval, contract: GoalContract) -> None:
        if not isinstance(approval, OwnerApproval):
            raise TypeError("approval must be an OwnerApproval")
        if approval.owner != contract.owner:
            raise ApprovalError(
                f"approval is from {approval.owner!r}, not the contract owner {contract.owner!r}"
            )

    def _out_of_budget(self, tx: _Tx, reason: str, **fields: Any) -> None:
        tx.move(S.OUT_OF_BUDGET, reason, end_reason=reason, **fields)

    @staticmethod
    def _closable(job: Job) -> bool:
        """Nothing of the period is still open: no step fired and unsettled.

        A step waiting for the owner's approval has not run, so it does not hold
        the period open.
        """
        return job.state is S.WAITING or (
            job.state is S.ESCALATED and job.escalation_kind is EscalationKind.APPROVAL_REQUIRED
        )

    def _check_deadline(self, tx: _Tx) -> bool:
        """Close the watch period if the deadline passed and nothing is open. True if closed.

        After the deadline nothing new fires, but a step that already fired keeps
        its retry and its proof; the period closes once that step is settled.
        """
        if not self._closable(tx.job):
            return False
        reason = self._deadline_reason(tx.contract, tx.now)
        if reason is None:
            return False
        self._close_period(tx, reason)
        return True

    def _close_period(self, tx: _Tx, reason: str) -> None:
        job = tx.job
        end = f"end_date: {reason}"
        if job.state is S.ESCALATED:
            tx.move(
                S.STOPPED,
                f"{end}; the step waiting for approval never ran",
                end_reason=end,
            )
        elif job.last_proof is not None and job.proven_actions == job.fire_seq:
            tx.move(
                S.COMPLETE,
                f"{end}; every action in the period was proved ({job.proven_actions})",
                proof=job.last_proof,
                end_reason=end,
            )
        elif job.fire_seq == 0:
            # No firing is not proof that nothing crossed: the controller does not
            # know the host's observation coverage.
            tx.move(S.STOPPED, f"{end}; nothing fired in the period", end_reason=end)
        else:
            unproved = job.fire_seq - job.proven_actions
            tx.move(
                S.STOPPED,
                f"{end}; {unproved} of {job.fire_seq} actions were not proved",
                end_reason=end,
            )

    # ------------------------------------------------------------ lifecycle

    def create(self, history: ContractHistory, *, step: StepTemplate) -> str:
        """Open a job for a contract. It starts ``drafted``; nothing runs yet."""
        if not isinstance(history, ContractHistory):
            raise TypeError("history must be a ContractHistory")
        if not isinstance(step, StepTemplate):
            raise TypeError("step must be a StepTemplate")
        now = self._now()
        job = Job.new(
            history.contract_id,
            contract_version=history.current.version,
            step_template=step,
            now=now,
        )
        first = StepReceipt.build(
            job_id=job.job_id,
            seq=1,
            at=now,
            kind="transition",
            from_state=None,
            to_state=S.DRAFTED,
            contract_version=job.contract_version,
            action_id=None,
            reason="job drafted from contract",
            prev_hash="",
        )
        job = replace(job, receipt_seq=1, receipt_head=first.receipt_hash)
        self._store.create(job, history.to_dict(), [first])
        return job.job_id

    def approve(self, job_id: str, approval: OwnerApproval) -> Job:
        """The owner approves the contract as shown. ``drafted`` to ``approved``."""

        def handler(job: Job, history: ContractHistory, now: datetime) -> _Tx | None:
            if job.state is not S.DRAFTED:
                raise InvalidTransition(
                    f"only a drafted job can be approved, not {job.state.value}"
                )
            self._check_owner(approval, history.current)
            tx = _Tx(self, job, history.current, now)
            tx.move(
                S.APPROVED,
                f"approved by {approval.owner}",
                detail={"approved_by": approval.owner, "reason": approval.reason},
            )
            return tx

        return self._mutate(job_id, handler)[0]

    def arm(self, job_id: str) -> Job:
        """Start watching. Needs a named proof source with a checker, and an executor."""

        def handler(job: Job, history: ContractHistory, now: datetime) -> _Tx | None:
            if job.state is not S.APPROVED:
                raise InvalidTransition(f"only an approved job can be armed, not {job.state.value}")
            contract = history.current
            self._ensure_runnable(contract, job.step_template)
            reason = self._deadline_reason(contract, now)
            if reason:
                raise NotArmableError(f"the job cannot be armed: {reason}")
            tx = _Tx(self, job, contract, now)
            tx.move(S.ARMED, "armed", trigger_state=TriggerState.UNKNOWN)
            tx.move(S.WAITING, "waiting for a trigger")
            return tx

        return self._mutate(job_id, handler)[0]

    def _ensure_runnable(self, contract: GoalContract, template: StepTemplate) -> None:
        ensure_armable(contract)
        assert contract.proof is not None
        if contract.proof.source not in self._checkers:
            raise NotArmableError(
                f"no checker is registered for proof source {contract.proof.source!r}"
            )
        if template.operation not in self._executors:
            raise NotArmableError(f"no executor is registered for operation {template.operation!r}")

    # ------------------------------------------------------------ readings

    def observe(self, job_id: str, observation: Observation) -> Job:
        """Feed one reading or event. This is the main way a job wakes up."""
        if not isinstance(observation, Observation):
            raise TypeError("observation must be an Observation")

        def handler(job: Job, history: ContractHistory, now: datetime) -> _Tx | None:
            return self._on_observation(job, history.current, now, observation)

        job, dispatch = self._mutate(job_id, handler)
        if dispatch is not None:
            self._dispatch(job_id, dispatch)
            return self._store.load(job_id)
        return job

    def _on_observation(
        self, job: Job, contract: GoalContract, now: datetime, obs: Observation
    ) -> _Tx | None:
        if job.state not in LISTENING:
            return None
        tx = _Tx(self, job, contract, now)
        if self._check_deadline(tx):
            return tx
        trigger = contract.trigger
        if trigger.kind == "threshold":
            if obs.metric != trigger.metric:
                return None
        elif obs.event != (trigger.event if trigger.kind == "event" else "schedule"):
            return None
        if obs.observation_id in job.seen_observations:
            return None
        if job.last_observed_at is not None and obs.observed_at < job.last_observed_at:
            return None
        fields: dict[str, Any] = {
            "last_observed_at": obs.observed_at,
            "seen_observations": (job.seen_observations + (obs.observation_id,))[-MAX_SEEN:],
        }
        idle = job.state is S.WAITING
        if trigger.kind == "threshold":
            assert obs.value is not None
            moved = step_threshold(trigger, job.trigger_state, obs.value, can_fire=idle)
            fields["trigger_state"] = moved.state
            if moved.fired:
                self._fire(tx, obs, fields)
            elif moved.suppressed:
                tx.note(
                    "fire_suppressed_busy",
                    f"{obs.metric} {obs.value} crossed {trigger.fire_at} while the job was "
                    f"{job.state.value}; it fires on the first reading above the line once free",
                    detail={"observation_id": obs.observation_id, "value": str(obs.value)},
                    **fields,
                )
            elif moved.changed:
                tx.note(
                    "trigger_state",
                    f"trigger is now {moved.state.value} at {obs.value}",
                    detail={"observation_id": obs.observation_id, "value": str(obs.value)},
                    **fields,
                )
            else:
                tx.touch(**fields)
        elif idle:
            self._fire(tx, obs, fields)
        else:
            tx.note(
                "fire_suppressed_busy",
                f"event {obs.event} arrived while the job was {job.state.value}; "
                "version 1 runs one action at a time, so the host may replay it",
                detail={"observation_id": obs.observation_id},
                **fields,
            )
        return tx

    def _fire(self, tx: _Tx, obs: Observation, fields: dict[str, Any]) -> None:
        job, contract = tx.job, tx.contract
        reason = self._budget_reason(job, contract, new_action=True)
        if reason:
            self._out_of_budget(tx, reason, **fields)
            return
        fire_seq = job.fire_seq + 1
        action_id = f"{job.job_id}:v{contract.version}:f{fire_seq}"
        step = self._build_step(job, contract, action_id, obs)
        if contract.trigger.kind == "threshold":
            why = f"trigger fired: {obs.metric} {obs.value} crossed {contract.trigger.fire_at}"
        else:
            why = f"trigger fired: event {obs.event}"
        tx.move(
            S.TRIGGERED,
            why,
            detail={
                "observation_id": obs.observation_id,
                "value": None if obs.value is None else str(obs.value),
            },
            fire_seq=fire_seq,
            action_id=action_id,
            step=step,
            result=None,
            approved_action_id=None,
            **fields,
        )
        self._guard_and_reserve(tx)

    @staticmethod
    def _build_step(
        job: Job, contract: GoalContract, action_id: str, obs: Observation
    ) -> ProposedStep:
        trigger = contract.trigger
        summary = job.step_template.render(
            {
                "metric": trigger.metric or "",
                "fire_at": "" if trigger.fire_at is None else str(trigger.fire_at),
                "reset_at": "" if trigger.reset_at is None else str(trigger.reset_at),
                "value": "" if obs.value is None else str(obs.value),
                "event": obs.event or "",
            }
        )
        return ProposedStep(
            action_id=action_id,
            contract_version=contract.version,
            operation=job.step_template.operation,
            target=job.step_template.target,
            summary=summary,
            parameters=dict(job.step_template.parameters),
            context={
                "observation_id": obs.observation_id,
                "observed_at": _json.format_datetime(obs.observed_at),
                "metric": obs.metric,
                "value": None if obs.value is None else str(obs.value),
                "event": obs.event,
                "payload": dict(obs.payload),
            },
        )

    # ------------------------------------------------------------ guard and dispatch

    def _escalate(
        self,
        tx: _Tx,
        reason: str,
        *,
        kind: EscalationKind = EscalationKind.APPROVAL_REQUIRED,
        guard: dict[str, Any] | None = None,
    ) -> None:
        if tx.job.state is S.ESCALATED:
            tx.note("escalation_unchanged", reason, guard=guard)
            return
        tx.move(S.ESCALATED, reason, guard=guard, escalation_kind=kind, escalation_reason=reason)

    def _guard_and_reserve(self, tx: _Tx) -> None:
        """From ``triggered`` (or an escalated step the owner answered): guard, then reserve."""
        job, contract = tx.job, tx.contract
        step = job.step
        assert step is not None
        if step.operation not in self._executors:
            self._escalate(tx, f"no executor is registered for operation {step.operation!r}")
            return
        approved = job.approved_action_id == job.action_id
        try:
            decision = self._guard.check(contract, step, owner_approved=approved)
        except Exception as exc:  # a broken guard fails closed: the step does not run
            self._guard_failed(tx, f"guard failed: {type(exc).__name__}: {exc}")
            return
        report = decision.to_dict()
        if decision.verdict == "conflict":
            tx.move(
                S.CONFLICT,
                decision.reason,
                guard=report,
                end_reason=f"conflict: {decision.reason}",
            )
        elif decision.verdict == "escalate":
            self._escalate(tx, decision.reason, guard=report)
        else:
            tx.move(S.GUARD_CHECKED, decision.reason or "guard: proceed", guard=report)
            self._reserve(tx)

    def _guard_failed(self, tx: _Tx, reason: str) -> None:
        """A failed check, not a verdict: the step did not run; check again on the next try."""
        job = tx.job
        assert job.action_id is not None
        failures = dict(job.check_failures)
        failures[job.action_id] = failures.get(job.action_id, 0) + 1
        tries = job.attempts.get(job.action_id, 0) + failures[job.action_id]
        detail = {"error": reason}
        if tries < self._max_attempts(tx.contract):
            delay = self._retry_delay(tries)
            tx.move(
                S.RETRYING,
                f"{reason}; the step did not run; checked again in {int(delay.total_seconds())}s",
                kind="check_failed",
                detail=detail,
                check_failures=failures,
                retry_at=tx.now + delay,
            )
            return
        # Tries are spent: ask the owner. A "proceed" runs the guard again with
        # fresh checks; the step still never runs without a passing guard.
        failures[job.action_id] = 0
        tx.note("check_failed", reason, detail=detail, check_failures=failures)
        self._escalate(tx, f"{reason}; the guard failed on every try, so the step did not run")

    def _reserve(self, tx: _Tx) -> None:
        """From ``guard_checked``: write down that the step is about to run, then run it."""
        job, contract = tx.job, tx.contract
        step = job.step
        assert step is not None and job.action_id is not None
        tries = self._tries(job)
        dispatched = job.attempts.get(job.action_id, 0)
        if tries >= self._max_attempts(contract):
            self._out_of_budget(tx, self._attempts_reason(contract, job.action_id))
            return
        # The action budget was checked when the trigger fired; only money can run out here.
        reason = self._budget_reason(job, contract, new_action=False)
        if reason:
            self._out_of_budget(tx, reason)
            return
        attempts = dict(job.attempts)
        attempts[job.action_id] = dispatched + 1
        tx.move(
            S.ACTING,
            f"dispatch reserved (attempt {tries + 1})",
            attempts=attempts,
            actions_dispatched=job.actions_dispatched + (1 if dispatched == 0 else 0),
            dispatch_reserved=job.action_id,
            lease_expires_at=tx.now + self._action_timeout,
        )
        tx.dispatch = step

    def _dispatch(self, job_id: str, step: ProposedStep) -> None:
        """Run the executor after the reservation is stored, then store what came back."""
        executor = self._executors[step.operation]
        with self._lock:
            self._in_flight.add(step.action_id)
        try:
            outcome: tuple[str, ActionResult | None, str]
            try:
                result = executor(step, idempotency_key=step.action_id)
            except NotExecuted as exc:
                outcome = ("not_executed", None, str(exc))
            except Exception as exc:  # anything else: the step may have run
                outcome = ("uncertain", None, f"{type(exc).__name__}: {exc}")
            else:
                if isinstance(result, ActionResult):
                    outcome = ("ok", result, "")
                else:
                    outcome = (
                        "uncertain",
                        None,
                        f"executor returned {type(result).__name__}, not an ActionResult",
                    )

            def handler(job: Job, history: ContractHistory, now: datetime) -> _Tx | None:
                return self._on_result(job, history.current, now, step, outcome)

            job, _ = self._mutate(job_id, handler)
        finally:
            with self._lock:
                self._in_flight.discard(step.action_id)
        if outcome[0] == "ok" and job.state is S.VERIFYING and job.action_id == step.action_id:
            self._judge(job_id, step.action_id, None)

    def _on_result(
        self,
        job: Job,
        contract: GoalContract,
        now: datetime,
        step: ProposedStep,
        outcome: tuple[str, ActionResult | None, str],
    ) -> _Tx | None:
        kind, result, message = outcome
        running = job.state is S.ACTING and job.dispatch_reserved == step.action_id
        late = (
            job.state is S.ESCALATED
            and job.escalation_kind is EscalationKind.OUTCOME_UNCERTAIN
            and job.action_id == step.action_id
        )
        if not (running or (late and kind == "ok")):
            return None
        tx = _Tx(self, job, contract, now)
        if kind == "ok":
            assert result is not None
            spent = job.cost_spent + result.cost
            over = contract.budget.max_cost is not None and spent > contract.budget.max_cost
            detail = {
                "reference": result.reference,
                "cost": str(result.cost),
                "cost_over_max": over,
            }
            if running and job.cancel_requested:
                tx.move(
                    S.CANCELLED,
                    f"cancelled: {job.cancel_reason}; the step had already finished",
                    detail=detail,
                    cost_spent=spent,
                    result=result,
                    end_reason=f"cancelled: {job.cancel_reason}",
                )
            else:
                tx.move(
                    S.VERIFYING,
                    "executor returned; waiting for proof"
                    if running
                    else "late result for a step presumed lost; waiting for proof",
                    kind="transition" if running else "late_result",
                    detail=detail,
                    cost_spent=spent,
                    result=result,
                    proof_deadline=now + self._proof_timeout,
                )
            return tx
        if job.cancel_requested:
            tx.move(
                S.CANCELLED,
                f"cancelled: {job.cancel_reason}; the step ended with {kind}",
                detail={"error": message},
                end_reason=f"cancelled: {job.cancel_reason}",
            )
        elif kind == "not_executed":
            self._retry_or_stop(tx, f"executor reports the step did not run: {message}")
        else:
            tx.move(
                S.ESCALATED,
                f"the step may have run and the outcome is unknown: {message}",
                detail={"error": message},
                escalation_kind=EscalationKind.OUTCOME_UNCERTAIN,
                escalation_reason=message,
            )
        return tx

    def _retry_or_stop(self, tx: _Tx, reason: str) -> None:
        """After a failed try: schedule the next one, or end the job if tries are spent."""
        job, contract = tx.job, tx.contract
        assert job.action_id is not None
        tries = self._tries(job)
        if tries >= self._max_attempts(contract):
            self._out_of_budget(tx, self._attempts_reason(contract, job.action_id))
        else:
            delay = self._retry_delay(tries)
            tx.move(
                S.RETRYING,
                f"{reason}; retry {tries + 1} in {int(delay.total_seconds())}s",
                retry_at=tx.now + delay,
            )

    # ------------------------------------------------------------ proof

    def _ask_checker(
        self, contract: GoalContract, job: Job, evidence: Any, now: datetime
    ) -> ProofVerdict:
        source = contract.proof.source if contract.proof is not None else ""
        checker = self._checkers.get(source)
        if checker is None:
            return ProofVerdict("NOT_YET", f"no checker is registered for proof source {source!r}")
        try:
            verdict = checker.check(contract, job.step, job.result, evidence, now)
        except Exception as exc:
            return ProofVerdict("NOT_YET", f"checker failed: {type(exc).__name__}: {exc}")
        if not isinstance(verdict, ProofVerdict):
            return ProofVerdict("NOT_YET", f"checker returned {type(verdict).__name__}")
        return verdict

    def _judge(self, job_id: str, action_id: str, evidence: Any) -> Job:
        """Ask the checker about a verifying job and apply the answer."""
        job = self._store.load(job_id)
        if job.state is not S.VERIFYING or job.action_id != action_id:
            return job
        history = ContractHistory.from_dict(self._store.contract_history(job_id))
        verdict = self._ask_checker(history.current, job, evidence, self._now())

        def handler(job: Job, history: ContractHistory, now: datetime) -> _Tx | None:
            if job.state is not S.VERIFYING or job.action_id != action_id:
                return None
            tx = _Tx(self, job, history.current, now)
            self._apply_verdict(tx, verdict)
            return tx

        return self._mutate(job_id, handler)[0]

    def _apply_verdict(self, tx: _Tx, verdict: ProofVerdict) -> None:
        job, contract = tx.job, tx.contract
        assert contract.proof is not None and job.action_id is not None
        if verdict.status == "PROVEN":
            proof = verdict.proof
            if proof is None:
                tx.note("proof_missing", "the checker said PROVEN but gave no proof receipt")
                return
            if proof.source != contract.proof.source:
                tx.note(
                    "proof_wrong_source",
                    f"proof is from {proof.source!r}, the contract names {contract.proof.source!r}",
                )
                return
            self._accept_proof(tx, proof, verdict.reason)
        elif verdict.status == "NOT_YET":
            tx.note("proof_pending", verdict.reason or "no proof yet")
        elif verdict.status == "FAILED":
            self._retry_or_stop(tx, f"proof failed: {verdict.reason}")
        else:
            reason = f"impossible: {verdict.reason}"
            tx.move(S.IMPOSSIBLE, reason, end_reason=reason)

    def _accept_proof(self, tx: _Tx, proof: ProofReceipt, note: str) -> None:
        job, contract = tx.job, tx.contract
        label = f"proof accepted from {proof.source}" + (f": {note}" if note else "")
        proved = {"proven_actions": job.proven_actions + 1, "last_proof": proof}
        if contract.end.on_success == "complete":
            tx.move(
                S.COMPLETE, label, proof=proof, end_reason=f"proved by {proof.source}", **proved
            )
            return
        # A watch goes back to waiting, unless nothing is left to spend. A limit is
        # no evidence of completion, so a spent budget ends out_of_budget.
        reason = self._budget_reason(job, contract, new_action=True)
        if reason:
            self._out_of_budget(tx, reason, proof=proof, **proved)
            return
        tx.move(S.WAITING, f"{label}; watching again", proof=proof, **_clear_step(), **proved)
        self._check_deadline(tx)

    def submit_evidence(self, job_id: str, action_id: str, evidence: Any = None) -> Job:
        """Hand over proof from outside (a delivery webhook, a person's check).

        Works for a verifying job, and for an escalated job whose step may have
        run (outcome uncertain, proof timeout): that is how an owner who sees
        the text arrived closes the job without sending it again. ``evidence`` of
        ``None`` simply asks the checker to look again.

        A job already stopped as unconfirmed keeps its state, but a late proof
        is still recorded on it (``late_proof``).
        """
        _json.dumps(evidence, limit=65_536)
        stopped = self._store.load(job_id)
        if stopped.state is S.STOPPED and stopped.end_reason.startswith("unconfirmed"):
            return self._late_proof(job_id, action_id, evidence)

        def handler(job: Job, history: ContractHistory, now: datetime) -> _Tx | None:
            if job.action_id != action_id:
                raise InvalidTransition(f"job {job_id} has no action {action_id!r}")
            if job.state is S.VERIFYING:
                return None
            if job.state is S.ESCALATED and job.escalation_kind in (
                EscalationKind.OUTCOME_UNCERTAIN,
                EscalationKind.PROOF_TIMEOUT,
            ):
                tx = _Tx(self, job, history.current, now)
                tx.move(
                    S.VERIFYING,
                    "evidence submitted for an escalated step",
                    kind="evidence_submitted",
                    proof_deadline=now + self._proof_timeout,
                )
                return tx
            raise InvalidTransition(
                f"evidence cannot be accepted while the job is {job.state.value}"
            )

        self._mutate(job_id, handler)
        return self._judge(job_id, action_id, evidence)

    def _late_proof(self, job_id: str, action_id: str, evidence: Any) -> Job:
        job = self._store.load(job_id)
        if job.action_id != action_id:
            raise InvalidTransition(f"job {job_id} has no action {action_id!r}")
        history = ContractHistory.from_dict(self._store.contract_history(job_id))
        verdict = self._ask_checker(history.current, job, evidence, self._now())
        source = history.current.proof.source if history.current.proof else ""
        if verdict.status != "PROVEN" or verdict.proof is None or verdict.proof.source != source:
            return job

        def handler(job: Job, history: ContractHistory, now: datetime) -> _Tx | None:
            if job.state is not S.STOPPED or job.action_id != action_id:
                return None
            tx = _Tx(self, job, history.current, now)
            tx.note(
                "late_proof",
                "proof arrived after the job stopped as unconfirmed; the job stays stopped",
                proof=verdict.proof,
            )
            return tx

        return self._mutate(job_id, handler)[0]

    # ------------------------------------------------------------ time

    def tick(self, now: datetime | None = None) -> list[Job]:
        """Do what is due: retries, deadlines, stuck steps, proof timeouts.

        Call it from the host's scheduler. It returns the jobs it changed. A job
        left in ``acting`` after its lease ran out is escalated, never re-run.
        """
        at = self._now() if now is None else _json.require_aware(now, "now")
        changed: list[Job] = []
        for job_id in self._store.due(at):
            touched: list[bool] = []

            def handler(
                job: Job,
                history: ContractHistory,
                moment: datetime,
                touched: list[bool] = touched,
            ) -> _Tx | None:
                tx = self._on_tick(job, history.current, moment)
                touched[:] = [tx is not None]
                return tx

            try:
                job, dispatch = self._mutate(job_id, handler, now=at)
            except StaleJobError:
                continue
            if dispatch is not None:
                self._dispatch(job_id, dispatch)
                job = self._store.load(job_id)
            if touched and touched[0]:
                changed.append(job)
        return changed

    def _on_tick(self, job: Job, contract: GoalContract, now: datetime) -> _Tx | None:
        if job.state in TERMINAL:
            return None
        tx = _Tx(self, job, contract, now)
        if self._check_deadline(tx):
            return tx
        if job.state is S.ACTING:
            with self._lock:
                running = job.action_id in self._in_flight
            if running or job.lease_expires_at is None or now < job.lease_expires_at:
                return None
            reason = "the step was reserved but no result came back before its lease ended"
            tx.move(
                S.ESCALATED,
                reason,
                escalation_kind=EscalationKind.OUTCOME_UNCERTAIN,
                escalation_reason=reason,
            )
        elif job.state is S.TRIGGERED:
            self._guard_and_reserve(tx)
        elif job.state is S.GUARD_CHECKED:
            self._reserve(tx)
        elif job.state is S.RETRYING:
            if job.retry_at is not None and now < job.retry_at:
                return None
            tx.move(S.TRIGGERED, "retry is due")
            self._guard_and_reserve(tx)
        elif job.state is S.VERIFYING:
            if job.proof_deadline is not None and now < job.proof_deadline:
                return None
            reason = "no proof arrived before the proof timeout"
            tx.move(
                S.ESCALATED,
                reason,
                escalation_kind=EscalationKind.PROOF_TIMEOUT,
                escalation_reason=reason,
            )
        elif job.state is S.ESCALATED and job.confirm_by is not None:
            if now < job.confirm_by:
                return None
            reason = (
                f"unconfirmed: nobody confirmed whether {job.action_id} happened "
                f"by {_json.format_datetime(job.confirm_by)}"
            )
            tx.move(S.STOPPED, reason, end_reason=reason)
        else:
            return None
        return tx

    # ------------------------------------------------------------ people

    def resolve(self, job_id: str, approval: OwnerApproval, *, decision: Decision) -> Job:
        """The owner answers an escalated job. Valid answers depend on why it escalated.

        * ``approval_required``: ``proceed`` (run the step; the guard still checks
          it) or ``skip``.
        * ``outcome_uncertain`` / ``proof_timeout``: ``retry`` (run the same action
          again, after the owner has checked it did not happen) or ``skip``. To
          close such a job as done, use ``submit_evidence`` instead.
        """
        if decision not in ("proceed", "skip", "retry"):
            raise ValueError("decision must be 'proceed', 'skip' or 'retry'")

        def handler(job: Job, history: ContractHistory, now: datetime) -> _Tx | None:
            contract = history.current
            if job.state is not S.ESCALATED or job.escalation_kind is None:
                raise InvalidTransition(f"job {job_id} is {job.state.value}, not escalated")
            self._check_owner(approval, contract)
            kind = job.escalation_kind
            allowed = (
                ("proceed", "skip")
                if kind is EscalationKind.APPROVAL_REQUIRED
                else ("retry", "skip")
            )
            if decision not in allowed:
                raise InvalidTransition(
                    f"{decision!r} does not answer {kind.value}; use one of {', '.join(allowed)}"
                )
            tx = _Tx(self, job, contract, now)
            if self._check_deadline(tx):
                return tx
            who = f"owner {approval.owner}: {approval.reason}".rstrip(": ")
            if decision == "skip":
                self._skip(tx, who)
            elif decision == "proceed":
                tx.note("owner_decision", f"{who} (proceed)", approved_action_id=job.action_id)
                self._guard_and_reserve(tx)
            else:
                assert job.action_id is not None
                if self._tries(job) >= self._max_attempts(contract):
                    self._out_of_budget(tx, self._attempts_reason(contract, job.action_id))
                else:
                    tx.move(
                        S.TRIGGERED,
                        f"{who} (retry after checking the step did not happen)",
                        approved_action_id=job.action_id,
                        result=None,
                    )
                    self._guard_and_reserve(tx)
            return tx

        job, dispatch = self._mutate(job_id, handler)
        if dispatch is not None:
            self._dispatch(job_id, dispatch)
            return self._store.load(job_id)
        return job

    def _skip(self, tx: _Tx, who: str) -> None:
        kind = tx.job.escalation_kind
        if (
            kind is not EscalationKind.APPROVAL_REQUIRED
            and tx.contract.end.on_success == "complete"
        ):
            tx.move(
                S.CANCELLED,
                f"{who} (skip); the job ends without proof",
                end_reason="cancelled: owner skipped an unresolved step",
            )
        else:
            tx.move(S.WAITING, f"{who} (skip); watching again", **_clear_step())
            self._check_deadline(tx)

    def revise_contract(
        self,
        job_id: str,
        *,
        approval: OwnerApproval | None = None,
        reason: str = "",
        **changes: Any,
    ) -> Job:
        """Write a new contract version for a job that has no step in flight.

        The promise fields need the owner's approval (``PromiseChangeError``
        otherwise). Allowed while the job is drafted, approved, waiting or
        escalated for approval. A changed trigger resets the trigger state; a
        step that was waiting for approval is dropped, because it was built
        under the old version.
        """

        def handler(job: Job, history: ContractHistory, now: datetime) -> _Tx | None:
            if job.state not in REVISABLE or (
                job.state is S.ESCALATED
                and job.escalation_kind is not EscalationKind.APPROVAL_REQUIRED
            ):
                raise InvalidTransition(
                    f"the contract cannot change while the job is {job.state.value}: "
                    "a step is in flight"
                )
            before = history.current
            after = history.revise(approval=approval, reason=reason, now=now, **changes)
            if after is before:
                return None
            if job.state not in (S.DRAFTED, S.APPROVED):
                self._ensure_runnable(after, job.step_template)
            changed = changed_fields(before, after)
            fields: dict[str, Any] = {"contract_version": after.version}
            if "trigger" in changed:
                fields["trigger_state"] = TriggerState.UNKNOWN
            tx = _Tx(self, job, after, now)
            tx.history_dict = history.to_dict()
            tx.note(
                "contract_revised",
                f"contract version {before.version} to {after.version}",
                detail={
                    "from_version": before.version,
                    "to_version": after.version,
                    "fields": sorted(changed),
                    "approved_by": after.approved_by,
                },
                **fields,
            )
            if job.state is S.ESCALATED:
                tx.move(
                    S.WAITING,
                    "the step waiting for approval was built under the old version and is dropped",
                    kind="pending_step_dropped",
                    **_clear_step(),
                )
            return tx

        return self._mutate(job_id, handler)[0]

    def pause(self, job_id: str, reason: str) -> Job:
        """Suspend a job that has no step in flight. Nothing fires, retries or
        expires while it is paused; readings that arrive are ignored."""
        if type(reason) is not str or not reason.strip():
            raise ValueError("a pause needs a reason")

        def handler(job: Job, history: ContractHistory, now: datetime) -> _Tx | None:
            if job.state not in PAUSABLE:
                raise InvalidTransition(
                    f"job {job_id} is {job.state.value}; it can be paused only while "
                    "waiting, retrying or escalated"
                )
            tx = _Tx(self, job, history.current, now)
            tx.move(
                S.PAUSED,
                f"paused: {reason}",
                kind="paused",
                paused_from=job.state,
                pause_reason=reason,
                paused_at=now,
            )
            return tx

        return self._mutate(job_id, handler)[0]

    def resume(self, job_id: str, reason: str = "") -> Job:
        """Continue a paused job from where it stopped, after checking its grounds.

        The proof source still needs a checker and the operation an executor
        (``NotArmableError`` otherwise; the job stays paused). A trigger that was
        armed must see a reading below the line again, so a crossing during the
        pause never fires late. If the deadline passed during the pause, the
        watch period closes now.
        """

        def handler(job: Job, history: ContractHistory, now: datetime) -> _Tx | None:
            if job.state is not S.PAUSED or job.paused_from is None:
                raise InvalidTransition(f"job {job_id} is {job.state.value}, not paused")
            contract = history.current
            self._ensure_runnable(contract, job.step_template)
            back = job.paused_from
            fields: dict[str, Any] = {"paused_from": None, "pause_reason": "", "paused_at": None}
            if job.trigger_state is TriggerState.ARMED:
                fields["trigger_state"] = TriggerState.UNKNOWN
            tx = _Tx(self, job, contract, now)
            tx.move(back, "resumed" + (f": {reason}" if reason else ""), kind="resumed", **fields)
            self._check_deadline(tx)
            return tx

        return self._mutate(job_id, handler)[0]

    def cancel(self, job_id: str, reason: str) -> Job:
        """Stop the job. A step already running cannot be recalled: the job is
        cancelled as soon as that step returns."""
        if type(reason) is not str or not reason.strip():
            raise ValueError("a cancellation needs a reason")

        def handler(job: Job, history: ContractHistory, now: datetime) -> _Tx | None:
            if job.state in TERMINAL:
                raise InvalidTransition(f"job {job_id} already ended: {job.state.value}")
            tx = _Tx(self, job, history.current, now)
            if job.state is S.ACTING:
                if job.cancel_requested:
                    return None
                tx.note(
                    "cancel_requested",
                    f"cancel requested while a step is running: {reason}",
                    cancel_requested=True,
                    cancel_reason=reason,
                )
            else:
                tx.move(S.CANCELLED, f"cancelled: {reason}", end_reason=f"cancelled: {reason}")
            return tx

        return self._mutate(job_id, handler)[0]

    # ------------------------------------------------------------ reading back

    def job(self, job_id: str) -> Job:
        return self._store.load(job_id)

    def contract(self, job_id: str) -> ContractHistory:
        return ContractHistory.from_dict(self._store.contract_history(job_id))

    def receipts(self, job_id: str, *, after_seq: int = 0) -> list[StepReceipt]:
        return self._store.receipts(job_id, after_seq=after_seq)

    def verify_receipts(self, job_id: str) -> bool:
        """Recompute the receipt chain. Raises ``LedgerIntegrityError`` on any break."""
        job = self._store.load(job_id)
        return verify_chain(
            self._store.receipts(job_id),
            expected_seq=job.receipt_seq,
            expected_head=job.receipt_head,
        )
