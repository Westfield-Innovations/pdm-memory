"""Consequence records: what was expected, what was seen, and the owner's policy.

Every record is frozen. ``to_dict()`` gives the canonical JSON form the ledger
stores and hashes; ``from_dict()`` reads it back unchanged.
"""

from __future__ import annotations

import math
from collections.abc import Mapping
from dataclasses import dataclass, field, fields
from datetime import datetime
from decimal import Decimal
from enum import Enum
from typing import Any

from pdm_memory.consequence.compare import COMPARATORS
from pdm_memory.goal_loop._json import parse_datetime, require_aware, to_decimal
from pdm_memory.guarded_effects import ConfigurationError, _name

__all__ = [
    "PROTECTED_TARGETS",
    "Account",
    "Admission",
    "Applicability",
    "Application",
    "Cause",
    "CauseHypothesis",
    "Comparison",
    "Criterion",
    "Evaluation",
    "EvidenceKind",
    "Expectation",
    "Finding",
    "ForecastRow",
    "Horizon",
    "LearningProposal",
    "NotSupported",
    "Observation",
    "Ordering",
    "OwnerPolicy",
    "QuantityStatus",
    "ReconcileFirst",
    "Response",
    "ReviewLimits",
    "Route",
    "SourceDecl",
    "Stage",
    "TaskRef",
    "UnauthorizedChange",
    "UpdateState",
]

# Outside any learning proposal's editable scope (spec: "Learning under
# retained owner control").
PROTECTED_TARGETS: frozenset[str] = frozenset(
    {"goal", "permission", "evaluator", "hard_limit", "admission_policy"}
)


class Comparison(str, Enum):
    CONFIRMED_MATCH = "CONFIRMED_MATCH"
    CONFIRMED_MISS = "CONFIRMED_MISS"
    UNRESOLVED = "UNRESOLVED"
    CONFLICT = "CONFLICT"


class Applicability(str, Enum):
    APPLICABLE = "APPLICABLE"
    OUT_OF_SCOPE = "OUT_OF_SCOPE"
    UNRESOLVED = "UNRESOLVED"
    CONFLICTING = "CONFLICTING"


class Ordering(str, Enum):
    PROSPECTIVE = "PROSPECTIVE"
    RETROSPECTIVE = "RETROSPECTIVE"
    UNCERTAIN = "UNCERTAIN"


class Stage(str, Enum):
    DISPATCH = "DISPATCH"
    HANDLER_RETURN = "HANDLER_RETURN"
    ACTUATOR_CONFIRMATION = "ACTUATOR_CONFIRMATION"
    TASK_EFFECT = "TASK_EFFECT"


class Route(str, Enum):
    OWN_ACTION = "OWN_ACTION"
    OTHER_ACTOR = "OTHER_ACTOR"
    PASSIVE_OBSERVATION = "PASSIVE_OBSERVATION"
    SIMULATION = "SIMULATION"


class EvidenceKind(str, Enum):
    MEASURED = "MEASURED"
    REPORTED = "REPORTED"
    SIMULATED = "SIMULATED"


class Response(str, Enum):
    CONTINUE = "CONTINUE"
    OPEN_REVIEW = "OPEN_REVIEW"
    OBSERVE = "OBSERVE"
    RESOLVE_CONFLICT = "RESOLVE_CONFLICT"


class Cause(str, Enum):
    EXECUTION = "EXECUTION"
    OBSERVATION = "OBSERVATION"
    CHANGED_CONDITIONS = "CHANGED_CONDITIONS"
    MODEL_LIMITATION = "MODEL_LIMITATION"
    ANTICIPATED_VARIATION = "ANTICIPATED_VARIATION"
    UNREPRESENTED = "UNREPRESENTED"


class Account(str, Enum):
    TASK_RESULT = "TASK_RESULT"
    AGENT_OPERATION = "AGENT_OPERATION"
    PRINCIPAL_BURDEN = "PRINCIPAL_BURDEN"
    COUPLED_EFFECT = "COUPLED_EFFECT"


class QuantityStatus(str, Enum):
    ESTIMATED = "ESTIMATED"
    MEASURED = "MEASURED"
    UNKNOWN = "UNKNOWN"


class Admission(str, Enum):
    ACCEPTED = "ACCEPTED"
    REJECTED = "REJECTED"
    DEFERRED = "DEFERRED"


class Application(str, Enum):
    APPLIED = "APPLIED"
    NOT_APPLIED = "NOT_APPLIED"
    UNCERTAIN = "UNCERTAIN"


class UpdateState(str, Enum):
    PROPOSED = "PROPOSED"
    DEFERRED = "DEFERRED"
    REJECTED = "REJECTED"
    UNCERTAIN = "UNCERTAIN"
    APPLIED = "APPLIED"
    NOT_APPLIED = "NOT_APPLIED"


class UnauthorizedChange(PermissionError):
    """A proposal reached for a target outside the owner-admitted scope."""


class NotSupported(ValueError):
    """The evaluation does not support the proposed learning."""


class ReconcileFirst(ValueError):
    """An earlier update's application is uncertain; reconcile it first."""


def _plain(value: Any) -> Any:
    if isinstance(value, Enum):
        return value.value
    if isinstance(value, datetime):
        return require_aware(value, "datetime").isoformat()
    if isinstance(value, Decimal):
        return str(value)
    if hasattr(value, "__dataclass_fields__"):
        return {f.name: _plain(getattr(value, f.name)) for f in fields(value)}
    if isinstance(value, Mapping):
        return {k: _plain(v) for k, v in value.items()}
    if isinstance(value, (tuple, list)):
        return [_plain(v) for v in value]
    if isinstance(value, (set, frozenset)):
        return sorted(_plain(v) for v in value)
    return value


def _opt_dt(value: Any) -> datetime | None:
    return None if value is None else parse_datetime(value, "datetime")


def _texts(value: Any, label: str) -> tuple[str, ...]:
    out = tuple(value)
    for v in out:
        _name(v, label)
    return out


@dataclass(frozen=True)
class TaskRef:
    task_id: str
    principal: str
    contract_id: str | None = None
    contract_version: int | None = None

    def __post_init__(self) -> None:
        _name(self.task_id, "task_id")
        _name(self.principal, "principal")
        if self.contract_id is not None:
            _name(self.contract_id, "contract_id")
        if self.contract_version is not None and (
            type(self.contract_version) is not int or self.contract_version < 1
        ):
            raise ValueError("contract_version must be a positive integer")


@dataclass(frozen=True)
class Criterion:
    name: str
    observable: str
    stage: Stage
    comparator: tuple[str, str]
    unit: str
    expected: Any

    def __post_init__(self) -> None:
        _name(self.name, "criterion name")
        _name(self.observable, "observable")
        object.__setattr__(self, "stage", Stage(self.stage))
        comparator = tuple(self.comparator)
        if len(comparator) != 2:
            raise ValueError("comparator must be a (name, version) pair")
        object.__setattr__(self, "comparator", _texts(comparator, "comparator"))
        _name(self.unit, "unit")

    @classmethod
    def from_dict(cls, data: Mapping[str, Any]) -> Criterion:
        return cls(**{**data, "comparator": tuple(data["comparator"])})


@dataclass(frozen=True)
class Horizon:
    window_start: datetime
    window_end: datetime
    latest_decision_point: datetime
    environment_versions: dict[str, str] = field(default_factory=dict)
    invalidated_by: tuple[str, ...] = ()

    def __post_init__(self) -> None:
        for label in ("window_start", "window_end", "latest_decision_point"):
            require_aware(getattr(self, label), label)
        if self.window_end <= self.window_start:
            raise ValueError("the observation window must close after it opens")
        object.__setattr__(self, "environment_versions", dict(self.environment_versions))
        object.__setattr__(self, "invalidated_by", _texts(self.invalidated_by, "condition"))

    @classmethod
    def from_dict(cls, data: Mapping[str, Any]) -> Horizon:
        return cls(
            window_start=parse_datetime(data["window_start"], "window_start"),
            window_end=parse_datetime(data["window_end"], "window_end"),
            latest_decision_point=parse_datetime(
                data["latest_decision_point"], "latest_decision_point"
            ),
            environment_versions=data["environment_versions"],
            invalidated_by=tuple(data["invalidated_by"]),
        )


@dataclass(frozen=True)
class Expectation:
    """A forecast retained before the outcome it will grade."""

    expectation_id: str
    task: TaskRef
    predictor: str
    predictor_version: str
    criteria: tuple[Criterion, ...]
    horizon: Horizon
    conditions: dict[str, Any] = field(default_factory=dict)
    inputs: tuple[str, ...] = ()
    action_id: str | None = None
    interface_id: str | None = None
    decision_ref: str = ""
    projection_id: str | None = None
    source_sequences: dict[str, int] = field(default_factory=dict)

    def __post_init__(self) -> None:
        _name(self.expectation_id, "expectation_id")
        _name(self.predictor, "predictor")
        _name(self.predictor_version, "predictor_version")
        if not isinstance(self.task, TaskRef) or not isinstance(self.horizon, Horizon):
            raise TypeError("an expectation needs a TaskRef and a Horizon")
        criteria = tuple(self.criteria)
        if not criteria or not all(isinstance(c, Criterion) for c in criteria):
            raise ValueError("an expectation needs at least one Criterion")
        if len({c.name for c in criteria}) != len(criteria):
            raise ValueError("criterion names must be unique")
        object.__setattr__(self, "criteria", criteria)
        object.__setattr__(self, "inputs", _texts(self.inputs, "input"))
        object.__setattr__(self, "conditions", dict(self.conditions))
        for label in ("action_id", "interface_id", "projection_id"):
            if getattr(self, label) is not None:
                _name(getattr(self, label), label)
        sequences = dict(self.source_sequences)
        if any(type(v) is not int or v < 0 for v in sequences.values()):
            raise ValueError("source sequences must be nonnegative integers")
        object.__setattr__(self, "source_sequences", sequences)

    def criterion(self, name: str) -> Criterion:
        for c in self.criteria:
            if c.name == name:
                return c
        raise KeyError(name)

    def to_dict(self) -> dict[str, Any]:
        return _plain(self)

    @classmethod
    def from_dict(cls, data: Mapping[str, Any]) -> Expectation:
        return cls(
            **{
                **data,
                "task": TaskRef(**data["task"]),
                "criteria": tuple(Criterion.from_dict(c) for c in data["criteria"]),
                "horizon": Horizon.from_dict(data["horizon"]),
                "inputs": tuple(data["inputs"]),
            }
        )


@dataclass(frozen=True)
class Observation:
    """One revision of one reading; a correction is a new revision."""

    observation_id: str
    revision: int
    source: str
    observable: str
    event_time: datetime
    arrival_time: datetime
    value: Any
    unit: str
    evidence_ref: str
    kind: EvidenceKind
    actor: str = ""
    clock_uncertainty_s: float = 0.0
    source_sequence: int | None = None
    coverage: tuple[datetime, datetime] | None = None
    valid: bool = True
    configuration: dict[str, str] = field(default_factory=dict)
    conditions: dict[str, Any] = field(default_factory=dict)
    supersedes: int | None = None

    def __post_init__(self) -> None:
        for label in ("observation_id", "source", "observable", "unit", "evidence_ref"):
            _name(getattr(self, label), label)
        if type(self.revision) is not int or self.revision < 1:
            raise ValueError("revision must be a positive integer")
        if self.supersedes is not None and type(self.supersedes) is not int:
            raise ValueError("supersedes must name an earlier revision")
        require_aware(self.event_time, "event_time")
        require_aware(self.arrival_time, "arrival_time")
        object.__setattr__(self, "kind", EvidenceKind(self.kind))
        u = self.clock_uncertainty_s
        if type(u) not in (int, float) or not math.isfinite(u) or u < 0:
            raise ValueError("clock_uncertainty_s must be a finite nonnegative number")
        if self.source_sequence is not None and (
            type(self.source_sequence) is not int or self.source_sequence < 0
        ):
            raise ValueError("source_sequence must be a nonnegative integer")
        if self.coverage is not None:
            start, end = self.coverage
            require_aware(start, "coverage start")
            require_aware(end, "coverage end")
            if end <= start:
                raise ValueError("coverage must end after it starts")
            object.__setattr__(self, "coverage", (start, end))
        if type(self.valid) is not bool:
            raise TypeError("valid must be bool")
        object.__setattr__(self, "configuration", dict(self.configuration))
        object.__setattr__(self, "conditions", dict(self.conditions))

    def to_dict(self) -> dict[str, Any]:
        return _plain(self)

    @classmethod
    def from_dict(cls, data: Mapping[str, Any]) -> Observation:
        coverage = data["coverage"]
        return cls(
            **{
                **data,
                "event_time": parse_datetime(data["event_time"], "event_time"),
                "arrival_time": parse_datetime(data["arrival_time"], "arrival_time"),
                "coverage": None if coverage is None else tuple(_opt_dt(c) for c in coverage),
            }
        )


@dataclass(frozen=True)
class SourceDecl:
    """An owner-admitted source, with what one more reading from it costs."""

    name: str
    observables: frozenset[str]
    latency_s: float
    cost: Decimal
    cost_unit: str
    burden: Decimal
    burden_unit: str

    def __post_init__(self) -> None:
        _name(self.name, "source name")
        object.__setattr__(self, "observables", frozenset(_texts(self.observables, "observable")))
        if type(self.latency_s) not in (int, float) or not math.isfinite(self.latency_s):
            raise ValueError("latency_s must be a finite number")
        object.__setattr__(self, "cost", to_decimal(self.cost, "cost"))
        object.__setattr__(self, "burden", to_decimal(self.burden, "burden"))
        _name(self.cost_unit, "cost_unit")
        _name(self.burden_unit, "burden_unit")


@dataclass(frozen=True)
class ReviewLimits:
    max_outstanding: int
    max_repeats: int
    max_payload_bytes: int
    max_revisions: int

    def __post_init__(self) -> None:
        for f in fields(self):
            value = getattr(self, f.name)
            if type(value) is not int or value < 1:
                raise ConfigurationError(f"{f.name} must be a positive integer")


@dataclass(frozen=True)
class OwnerPolicy:
    """What the owner admits: sources, comparators, learning targets and bounds.

    The worker never edits this; a new version is a new policy object.
    """

    policy_version: str
    sources: tuple[SourceDecl, ...]
    comparators: frozenset[tuple[str, str]]
    learning_targets: frozenset[str]
    limits: ReviewLimits
    escalation: str
    exchange_rules: Mapping[str, Decimal] | None = None

    def __post_init__(self) -> None:
        _name(self.policy_version, "policy_version")
        if not isinstance(self.limits, ReviewLimits):
            raise ConfigurationError("operating limits must be a complete ReviewLimits")
        if not isinstance(self.escalation, str) or not self.escalation.strip():
            raise ConfigurationError("an escalation route is required")
        sources = tuple(self.sources)
        if not all(isinstance(s, SourceDecl) for s in sources):
            raise ConfigurationError("sources must be SourceDecl records")
        if len({s.name for s in sources}) != len(sources):
            raise ConfigurationError("source names must be unique")
        object.__setattr__(self, "sources", sources)
        comparators = frozenset(tuple(c) for c in self.comparators)
        unknown = comparators - COMPARATORS.keys()
        if unknown:
            raise ConfigurationError(f"no implementation for comparators {sorted(unknown)}")
        object.__setattr__(self, "comparators", comparators)
        targets = frozenset(_texts(self.learning_targets, "learning target"))
        if targets & PROTECTED_TARGETS:
            raise ConfigurationError(
                f"protected targets cannot be admitted: {sorted(targets & PROTECTED_TARGETS)}"
            )
        object.__setattr__(self, "learning_targets", targets)
        if self.exchange_rules is not None:
            object.__setattr__(
                self,
                "exchange_rules",
                {_name(k, "unit"): to_decimal(v, "exchange rate") for k, v in self.exchange_rules.items()},
            )

    def source(self, name: str) -> SourceDecl | None:
        return next((s for s in self.sources if s.name == name), None)


@dataclass(frozen=True)
class CauseHypothesis:
    """A proposed cause. It is supported only by recorded observations it cites."""

    cause: Cause
    support: tuple[str, ...] = ()

    def __post_init__(self) -> None:
        object.__setattr__(self, "cause", Cause(self.cause))
        object.__setattr__(self, "support", _texts(self.support, "support"))


@dataclass(frozen=True)
class Finding:
    """One criterion's result. Applicability and ordering are kept beside it."""

    criterion: str
    ordering: Ordering | None
    applicability: Applicability
    comparison: Comparison
    used: tuple[tuple[str, int], ...]
    excluded: tuple[tuple[str, str], ...]
    allowed_by_model: bool
    missing: str

    @classmethod
    def from_dict(cls, data: Mapping[str, Any]) -> Finding:
        return cls(
            criterion=data["criterion"],
            ordering=None if data["ordering"] is None else Ordering(data["ordering"]),
            applicability=Applicability(data["applicability"]),
            comparison=Comparison(data["comparison"]),
            used=tuple((i, r) for i, r in data["used"]),
            excluded=tuple((i, r) for i, r in data["excluded"]),
            allowed_by_model=data["allowed_by_model"],
            missing=data["missing"],
        )


@dataclass(frozen=True)
class Evaluation:
    """Comparison for exact input revisions. It never says the decision was wrong."""

    evaluation_id: str
    expectation_id: str
    revision: int
    findings: tuple[Finding, ...]
    policy_version: str
    causes: tuple[CauseHypothesis, ...]
    attribution_supported: bool
    response: Response
    superseded_by: str | None = None

    def finding(self, criterion: str) -> Finding:
        return next(f for f in self.findings if f.criterion == criterion)

    def to_dict(self) -> dict[str, Any]:
        record = _plain(self)
        del record["superseded_by"]
        return record

    @classmethod
    def from_dict(cls, data: Mapping[str, Any], superseded_by: str | None = None) -> Evaluation:
        return cls(
            evaluation_id=data["evaluation_id"],
            expectation_id=data["expectation_id"],
            revision=data["revision"],
            findings=tuple(Finding.from_dict(f) for f in data["findings"]),
            policy_version=data["policy_version"],
            causes=tuple(CauseHypothesis(c["cause"], tuple(c["support"])) for c in data["causes"]),
            attribution_supported=data["attribution_supported"],
            response=Response(data["response"]),
            superseded_by=superseded_by,
        )


@dataclass(frozen=True)
class ForecastRow:
    """How one predictor held for one comparable case, with its sample counts."""

    predictor: str
    predictor_version: str
    observable: str
    conditions_key: str
    evidence_kind: EvidenceKind | None
    horizon_s: float
    tolerance: str
    n: int
    match: int
    miss: int
    unresolved: int
    conflict: int
    retrospective: int
    mean_probability: float | None


@dataclass(frozen=True)
class LearningProposal:
    """A bounded change for the owner's update path. It grants nothing by itself."""

    update_id: str
    expectation_id: str
    evaluation_id: str
    criterion: str
    target_kind: str
    target_id: str
    target_version: str
    change: dict[str, Any]
    scope: str
    confidence: float
    route: Route
    evidence_kind: EvidenceKind
    interface_id: str | None = None
    reconciles: str | None = None

    def to_dict(self) -> dict[str, Any]:
        return _plain(self)

    @classmethod
    def from_dict(cls, data: Mapping[str, Any]) -> LearningProposal:
        return cls(
            **{
                **data,
                "route": Route(data["route"]),
                "evidence_kind": EvidenceKind(data["evidence_kind"]),
            }
        )
