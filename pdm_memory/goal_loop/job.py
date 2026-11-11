"""The job record the controller keeps for one contract, and what feeds it."""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass, field
from datetime import datetime
from decimal import Decimal
from typing import Any

from pdm_memory.goal_loop import _json
from pdm_memory.goal_loop.checker import ProofReceipt
from pdm_memory.goal_loop.executor import ActionResult
from pdm_memory.goal_loop.states import EscalationKind, JobState
from pdm_memory.goal_loop.triggers import TriggerState

__all__ = ["Job", "Observation", "ProposedStep", "StepTemplate"]

PARAM_LIMIT = 65_536
MAX_SEEN = 256


def _text(value: Any, label: str, *, allow_empty: bool = False) -> str:
    if type(value) is not str:
        raise TypeError(f"{label} must be a string")
    if not allow_empty and not value.strip():
        raise ValueError(f"{label} must not be blank")
    return value


def _json_object(value: Any, label: str) -> dict[str, Any]:
    if not isinstance(value, Mapping):
        raise TypeError(f"{label} must be a mapping")
    plain = dict(value)
    _json.dumps(plain, limit=PARAM_LIMIT)
    return plain


class _SafeFields(dict):
    def __missing__(self, key: str) -> str:
        return "{" + key + "}"


@dataclass(frozen=True)
class StepTemplate:
    """The one action a v1 job takes when its trigger fires.

    ``summary`` is plain language and is what the guard reads. It may use
    ``{metric} {value} {fire_at} {reset_at} {event}``.
    """

    operation: str
    target: str
    summary: str
    parameters: Mapping[str, Any] = field(default_factory=dict)

    def __post_init__(self) -> None:
        _text(self.operation, "step operation")
        _text(self.target, "step target")
        _text(self.summary, "step summary")
        object.__setattr__(self, "parameters", _json_object(self.parameters, "step parameters"))
        try:
            self.render({})
        except (ValueError, IndexError, KeyError, AttributeError) as exc:
            raise ValueError(f"step summary is not a valid template: {exc}") from None

    def render(self, fields: Mapping[str, str]) -> str:
        return self.summary.format_map(_SafeFields(fields))

    def to_dict(self) -> dict[str, Any]:
        return {
            "operation": self.operation,
            "target": self.target,
            "summary": self.summary,
            "parameters": dict(self.parameters),
        }

    @classmethod
    def from_dict(cls, data: Any) -> StepTemplate:
        return cls(
            operation=data["operation"],
            target=data["target"],
            summary=data["summary"],
            parameters=data.get("parameters", {}),
        )


@dataclass(frozen=True)
class ProposedStep:
    """One concrete action, bound to one trigger firing by its ``action_id``."""

    action_id: str
    contract_version: int
    operation: str
    target: str
    summary: str
    parameters: Mapping[str, Any] = field(default_factory=dict)
    context: Mapping[str, Any] = field(default_factory=dict)

    def __post_init__(self) -> None:
        _text(self.action_id, "action_id")
        _text(self.operation, "operation")
        _text(self.target, "target")
        _text(self.summary, "summary")
        object.__setattr__(self, "parameters", _json_object(self.parameters, "parameters"))
        object.__setattr__(self, "context", _json_object(self.context, "context"))

    def to_dict(self) -> dict[str, Any]:
        return {
            "action_id": self.action_id,
            "contract_version": self.contract_version,
            "operation": self.operation,
            "target": self.target,
            "summary": self.summary,
            "parameters": dict(self.parameters),
            "context": dict(self.context),
        }

    @classmethod
    def from_dict(cls, data: Any) -> ProposedStep:
        return cls(
            action_id=data["action_id"],
            contract_version=data["contract_version"],
            operation=data["operation"],
            target=data["target"],
            summary=data["summary"],
            parameters=data.get("parameters", {}),
            context=data.get("context", {}),
        )


@dataclass(frozen=True, kw_only=True)
class Observation:
    """One reading or event handed to the controller.

    A threshold reading carries ``metric`` and ``value``; an event carries
    ``event`` (and an optional ``payload``). ``observation_id`` must be unique
    per reading: a repeat is ignored, which is what stops six data points above
    the line from becoming six actions.
    """

    observation_id: str
    observed_at: datetime
    metric: str | None = None
    value: Decimal | None = None
    event: str | None = None
    payload: Mapping[str, Any] = field(default_factory=dict)

    def __post_init__(self) -> None:
        _text(self.observation_id, "observation_id")
        _json.require_aware(self.observed_at, "observed_at")
        if self.metric is not None:
            _text(self.metric, "metric")
        if self.event is not None:
            _text(self.event, "event")
        if (self.metric is None) == (self.event is None):
            raise ValueError("an observation carries a metric reading or an event, not both")
        if self.metric is not None:
            if self.value is None:
                raise ValueError("a metric reading needs a value")
            object.__setattr__(self, "value", _json.to_decimal(self.value, "value"))
        elif self.value is not None:
            raise ValueError("an event does not take a value")
        object.__setattr__(self, "payload", _json_object(self.payload, "payload"))


def _opt_iso(value: datetime | None) -> str | None:
    return None if value is None else _json.format_datetime(value)


def _opt_dt(data: Mapping[str, Any], key: str) -> datetime | None:
    value = data.get(key)
    return None if value is None else _json.parse_datetime(value, key)


@dataclass(frozen=True, kw_only=True)
class Job:
    """Everything the controller needs to carry one contract between calls.

    Frozen; the controller replaces it, never edits it. ``revision`` is the
    compare-and-swap token for the store. ``receipt_seq`` and ``receipt_head``
    point at the end of the receipt chain.
    """

    job_id: str
    state: JobState
    revision: int
    contract_version: int
    step_template: StepTemplate
    created_at: datetime
    updated_at: datetime
    trigger_state: TriggerState = TriggerState.UNKNOWN
    last_observed_at: datetime | None = None
    seen_observations: tuple[str, ...] = ()
    fire_seq: int = 0
    action_id: str | None = None
    step: ProposedStep | None = None
    result: ActionResult | None = None
    attempts: Mapping[str, int] = field(default_factory=dict)
    check_failures: Mapping[str, int] = field(default_factory=dict)
    actions_dispatched: int = 0
    proven_actions: int = 0
    last_proof: ProofReceipt | None = None
    cost_spent: Decimal = Decimal(0)
    dispatch_reserved: str | None = None
    lease_expires_at: datetime | None = None
    retry_at: datetime | None = None
    proof_deadline: datetime | None = None
    escalation_kind: EscalationKind | None = None
    escalation_reason: str = ""
    confirm_by: datetime | None = None
    paused_from: JobState | None = None
    pause_reason: str = ""
    paused_at: datetime | None = None
    approved_action_id: str | None = None
    cancel_requested: bool = False
    cancel_reason: str = ""
    end_reason: str = ""
    next_due_at: datetime | None = None
    receipt_seq: int = 0
    receipt_head: str = ""

    @classmethod
    def new(
        cls, job_id: str, *, contract_version: int, step_template: StepTemplate, now: datetime
    ) -> Job:
        return cls(
            job_id=job_id,
            state=JobState.DRAFTED,
            revision=1,
            contract_version=contract_version,
            step_template=step_template,
            created_at=now,
            updated_at=now,
        )

    @property
    def is_terminal(self) -> bool:
        from pdm_memory.goal_loop.states import TERMINAL

        return self.state in TERMINAL

    def to_dict(self) -> dict[str, Any]:
        return {
            "job_id": self.job_id,
            "state": self.state.value,
            "revision": self.revision,
            "contract_version": self.contract_version,
            "step_template": self.step_template.to_dict(),
            "created_at": _opt_iso(self.created_at),
            "updated_at": _opt_iso(self.updated_at),
            "trigger_state": self.trigger_state.value,
            "last_observed_at": _opt_iso(self.last_observed_at),
            "seen_observations": list(self.seen_observations),
            "fire_seq": self.fire_seq,
            "action_id": self.action_id,
            "step": None if self.step is None else self.step.to_dict(),
            "result": None if self.result is None else self.result.to_dict(),
            "attempts": dict(self.attempts),
            "check_failures": dict(self.check_failures),
            "actions_dispatched": self.actions_dispatched,
            "proven_actions": self.proven_actions,
            "last_proof": None if self.last_proof is None else self.last_proof.to_dict(),
            "cost_spent": str(self.cost_spent),
            "dispatch_reserved": self.dispatch_reserved,
            "lease_expires_at": _opt_iso(self.lease_expires_at),
            "retry_at": _opt_iso(self.retry_at),
            "proof_deadline": _opt_iso(self.proof_deadline),
            "escalation_kind": None if self.escalation_kind is None else self.escalation_kind.value,
            "escalation_reason": self.escalation_reason,
            "confirm_by": _opt_iso(self.confirm_by),
            "paused_from": None if self.paused_from is None else self.paused_from.value,
            "pause_reason": self.pause_reason,
            "paused_at": _opt_iso(self.paused_at),
            "approved_action_id": self.approved_action_id,
            "cancel_requested": self.cancel_requested,
            "cancel_reason": self.cancel_reason,
            "end_reason": self.end_reason,
            "next_due_at": _opt_iso(self.next_due_at),
            "receipt_seq": self.receipt_seq,
            "receipt_head": self.receipt_head,
        }

    @classmethod
    def from_dict(cls, data: Any) -> Job:
        step = data.get("step")
        result = data.get("result")
        kind = data.get("escalation_kind")
        proof = data.get("last_proof")
        paused_from = data.get("paused_from")
        return cls(
            job_id=data["job_id"],
            state=JobState(data["state"]),
            revision=data["revision"],
            contract_version=data["contract_version"],
            step_template=StepTemplate.from_dict(data["step_template"]),
            created_at=_json.parse_datetime(data["created_at"], "created_at"),
            updated_at=_json.parse_datetime(data["updated_at"], "updated_at"),
            trigger_state=TriggerState(data.get("trigger_state", "unknown")),
            last_observed_at=_opt_dt(data, "last_observed_at"),
            seen_observations=tuple(data.get("seen_observations", ())),
            fire_seq=data.get("fire_seq", 0),
            action_id=data.get("action_id"),
            step=None if step is None else ProposedStep.from_dict(step),
            result=None if result is None else ActionResult.from_dict(result),
            attempts=dict(data.get("attempts", {})),
            check_failures=dict(data.get("check_failures", {})),
            actions_dispatched=data.get("actions_dispatched", 0),
            proven_actions=data.get("proven_actions", 0),
            last_proof=None if proof is None else ProofReceipt.from_dict(proof),
            cost_spent=_json.to_decimal(data.get("cost_spent", "0"), "cost_spent"),
            dispatch_reserved=data.get("dispatch_reserved"),
            lease_expires_at=_opt_dt(data, "lease_expires_at"),
            retry_at=_opt_dt(data, "retry_at"),
            proof_deadline=_opt_dt(data, "proof_deadline"),
            escalation_kind=None if kind is None else EscalationKind(kind),
            escalation_reason=data.get("escalation_reason", ""),
            confirm_by=_opt_dt(data, "confirm_by"),
            paused_from=None if paused_from is None else JobState(paused_from),
            pause_reason=data.get("pause_reason", ""),
            paused_at=_opt_dt(data, "paused_at"),
            approved_action_id=data.get("approved_action_id"),
            cancel_requested=data.get("cancel_requested", False),
            cancel_reason=data.get("cancel_reason", ""),
            end_reason=data.get("end_reason", ""),
            next_due_at=_opt_dt(data, "next_due_at"),
            receipt_seq=data.get("receipt_seq", 0),
            receipt_head=data.get("receipt_head", ""),
        )
