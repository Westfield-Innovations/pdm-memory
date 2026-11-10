"""Goal Loop: a job described once and carried to the end (preview).

Two parts ship together:

* the **Goal Contract** (``GoalContract``, ``ContractHistory``): the one record
  every part of the loop reads. Its promise changes only with the owner's say-so.
* the **controller** (``GoalLoopController``): moves one job through its states,
  checks every step with a guard, proves the result with a checker, and writes a
  hash-chained receipt for every move.

    from pdm_memory.goal_loop import (
        ContractHistory, GoalContract, GoalLoopController, Observation,
        OwnerApproval, StepTemplate, Trigger,
    )

No PDM store, account or network is needed; ``InMemoryJobStore`` is the default
and ``SqliteJobStore`` makes jobs survive the process.
"""

from pdm_memory.goal_loop.checker import CallableChecker, Checker, ProofReceipt, ProofVerdict
from pdm_memory.goal_loop.contract import (
    PROMISE_FIELDS,
    Autonomy,
    Budget,
    ContractHistory,
    EndConditions,
    GoalContract,
    NotArmableError,
    Notify,
    OwnerApproval,
    PromiseChangeError,
    Proof,
    Trigger,
    ensure_armable,
)
from pdm_memory.goal_loop.controller import ApprovalError, GoalLoopController
from pdm_memory.goal_loop.executor import ActionResult, Executor, NotExecuted
from pdm_memory.goal_loop.guard import DefaultGuard, Guard, GuardDecision
from pdm_memory.goal_loop.job import Job, Observation, ProposedStep, StepTemplate
from pdm_memory.goal_loop.receipts import StepReceipt
from pdm_memory.goal_loop.states import EscalationKind, InvalidTransition, JobState
from pdm_memory.goal_loop.store import (
    InMemoryJobStore,
    JobExistsError,
    JobNotFoundError,
    JobStore,
    SqliteJobStore,
    StaleJobError,
)
from pdm_memory.goal_loop.triggers import TriggerState

__all__ = [
    "PROMISE_FIELDS",
    "ActionResult",
    "ApprovalError",
    "Autonomy",
    "Budget",
    "CallableChecker",
    "Checker",
    "ContractHistory",
    "DefaultGuard",
    "EndConditions",
    "EscalationKind",
    "Executor",
    "GoalContract",
    "GoalLoopController",
    "Guard",
    "GuardDecision",
    "InMemoryJobStore",
    "InvalidTransition",
    "Job",
    "JobExistsError",
    "JobNotFoundError",
    "JobState",
    "JobStore",
    "NotArmableError",
    "NotExecuted",
    "Notify",
    "Observation",
    "OwnerApproval",
    "PromiseChangeError",
    "Proof",
    "ProofReceipt",
    "ProofVerdict",
    "ProposedStep",
    "SqliteJobStore",
    "StaleJobError",
    "StepReceipt",
    "StepTemplate",
    "Trigger",
    "TriggerState",
    "ensure_armable",
]
