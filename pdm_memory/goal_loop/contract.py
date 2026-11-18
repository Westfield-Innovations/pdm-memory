"""Goal Contract: the one record that holds a job.

"The system may rewrite the plan. It may not rewrite the promise."

A ``GoalContract`` is frozen. Every change goes through
``ContractHistory.revise()``, which writes a new dated version and keeps the
earlier ones. The promise fields (outcome, proof, never, autonomy, budget and
sacrifice order) change only with an ``OwnerApproval`` from the contract's
owner. The other fields (preferences, trigger, notify, end) belong to the plan
and still get a new version, so every change stays on record.

Owner approval is cooperative, as in ``GuardedEffects``: this module checks
that the approval names the contract's owner. Whether a person really gave
it is the host's responsibility.

Nothing here needs a PDM store, an account or a network. ``to_dict()`` and
``to_json()`` hand the record to whatever storage the host uses.
"""

from __future__ import annotations

import dataclasses
from collections.abc import Iterable, Mapping
from dataclasses import dataclass, field
from datetime import datetime
from decimal import Decimal
from itertools import pairwise
from typing import Any, Literal

from pdm_memory.goal_loop import _json
from pdm_memory.goal_loop._json import require_aware, to_decimal, utc_now

__all__ = [
    "PROMISE_FIELDS",
    "Autonomy",
    "Budget",
    "ContractHistory",
    "EndConditions",
    "GoalContract",
    "NotArmableError",
    "Notify",
    "OwnerApproval",
    "PromiseChangeError",
    "Proof",
    "Trigger",
    "ensure_armable",
]

SCHEMA = "pdm.goal_contract/1"

PROMISE_FIELDS: frozenset[str] = frozenset(
    {"outcome", "proof", "never", "autonomy", "budget", "sacrifice_order"}
)
PLAN_FIELDS: frozenset[str] = frozenset({"preferences", "trigger", "notify", "end"})
REVISABLE_FIELDS: frozenset[str] = PROMISE_FIELDS | PLAN_FIELDS
RECORD_FIELDS: frozenset[str] = frozenset(
    {
        "contract_id",
        "owner",
        "version",
        "created_at",
        "previous_version",
        "approved_by",
        "approved_at",
        "change_reason",
    }
)

TRIGGER_KINDS = ("threshold", "event", "schedule")
DIRECTIONS = ("rising", "falling")
NOTIFY_CHANNELS = ("sms", "email", "record")
ON_SUCCESS = ("complete", "rearm")


class PromiseChangeError(ValueError):
    """A promise field was changed without the owner's approval."""

    def __init__(self, message: str, fields: Iterable[str] = ()) -> None:
        super().__init__(message)
        self.fields: tuple[str, ...] = tuple(sorted(fields))


class NotArmableError(ValueError):
    """The contract cannot be armed: it names no way to prove success."""


# ---------------------------------------------------------------- validators


def _text(value: Any, label: str, *, allow_empty: bool = False) -> str:
    if type(value) is not str:
        raise TypeError(f"{label} must be a string")
    if not allow_empty and not value.strip():
        raise ValueError(f"{label} must not be blank")
    return value


def _optional_text(value: Any, label: str) -> str | None:
    return None if value is None else _text(value, label)


def _texts(value: Any, label: str) -> tuple[str, ...]:
    if isinstance(value, str) or not isinstance(value, (list, tuple)):
        raise TypeError(f"{label} must be a list or tuple of strings")
    items = tuple(_text(v, f"{label} item") for v in value)
    if len(set(items)) != len(items):
        raise ValueError(f"{label} contains duplicates")
    return items


def _choice(value: Any, label: str, options: tuple[str, ...]) -> str:
    if value not in options:
        raise ValueError(f"{label} must be one of {', '.join(options)}; got {value!r}")
    return value


def _positive_int(value: Any, label: str) -> int | None:
    if value is None:
        return None
    if type(value) is not int or value < 1:
        raise ValueError(f"{label} must be a positive integer")
    return value


def _optional_decimal(value: Any, label: str) -> Decimal | None:
    return None if value is None else to_decimal(value, label)


def _optional_aware(value: Any, label: str) -> datetime | None:
    return None if value is None else require_aware(value, label)


def _set(obj: Any, name: str, value: Any) -> None:
    object.__setattr__(obj, name, value)


def _iso(value: datetime | None) -> str | None:
    return None if value is None else _json.format_datetime(value)


def _num(value: Decimal | None) -> str | None:
    return None if value is None else str(value)


def _date(data: Mapping[str, Any], key: str) -> datetime | None:
    value = data.get(key)
    return None if value is None else _json.parse_datetime(value, key)


def _exact_keys(data: Any, label: str, allowed: Iterable[str]) -> Mapping[str, Any]:
    if not isinstance(data, Mapping):
        raise TypeError(f"{label} must be a JSON object")
    unknown = set(data) - set(allowed)
    if unknown:
        raise ValueError(f"{label} has unknown keys: {', '.join(sorted(unknown))}")
    return data


# ---------------------------------------------------------------- parts


@dataclass(frozen=True)
class Proof:
    """The measurement that shows the job is done. ``source`` names the checker."""

    source: str
    description: str = ""

    def __post_init__(self) -> None:
        _text(self.source, "proof.source")
        _text(self.description, "proof.description", allow_empty=True)

    def to_dict(self) -> dict[str, Any]:
        return {"source": self.source, "description": self.description}

    @classmethod
    def from_dict(cls, data: Any) -> Proof:
        data = _exact_keys(data, "proof", ("source", "description"))
        return cls(source=data.get("source"), description=data.get("description", ""))


@dataclass(frozen=True, kw_only=True)
class Budget:
    """How much time, money and effort the job may spend.

    ``max_actions`` counts distinct actions over the whole job.
    ``max_attempts`` counts tries of one action, including the first.
    """

    deadline: datetime | None = None
    max_actions: int | None = None
    max_attempts: int | None = None
    max_cost: Decimal | None = None
    currency: str | None = None

    def __post_init__(self) -> None:
        _optional_aware(self.deadline, "budget.deadline")
        _positive_int(self.max_actions, "budget.max_actions")
        _positive_int(self.max_attempts, "budget.max_attempts")
        cost = _optional_decimal(self.max_cost, "budget.max_cost")
        if cost is not None and cost < 0:
            raise ValueError("budget.max_cost must not be negative")
        _set(self, "max_cost", cost)
        _optional_text(self.currency, "budget.currency")

    def to_dict(self) -> dict[str, Any]:
        return {
            "deadline": _iso(self.deadline),
            "max_actions": self.max_actions,
            "max_attempts": self.max_attempts,
            "max_cost": _num(self.max_cost),
            "currency": self.currency,
        }

    @classmethod
    def from_dict(cls, data: Any) -> Budget:
        data = _exact_keys(
            data, "budget", ("deadline", "max_actions", "max_attempts", "max_cost", "currency")
        )
        return cls(
            deadline=_date(data, "deadline"),
            max_actions=data.get("max_actions"),
            max_attempts=data.get("max_attempts"),
            max_cost=data.get("max_cost"),
            currency=data.get("currency"),
        )


@dataclass(frozen=True, kw_only=True)
class Trigger:
    """What wakes the job up.

    ``threshold`` watches one metric with hysteresis: a ``rising`` trigger
    fires when the value crosses ``fire_at`` from below and re-arms only after
    it drops below ``reset_at``. ``event`` fires on a named event. ``schedule``
    carries a host-interpreted schedule (for example a cron expression).
    """

    kind: Literal["threshold", "event", "schedule"]
    metric: str | None = None
    direction: Literal["rising", "falling"] | None = None
    fire_at: Decimal | None = None
    reset_at: Decimal | None = None
    event: str | None = None
    schedule: str | None = None

    def __post_init__(self) -> None:
        _choice(self.kind, "trigger.kind", TRIGGER_KINDS)
        if self.kind == "threshold":
            _text(self.metric, "trigger.metric")
            _choice(self.direction, "trigger.direction", DIRECTIONS)
            if self.fire_at is None or self.reset_at is None:
                raise ValueError("a threshold trigger needs fire_at and reset_at")
            fire = to_decimal(self.fire_at, "trigger.fire_at")
            reset = to_decimal(self.reset_at, "trigger.reset_at")
            if self.direction == "rising" and not reset < fire:
                raise ValueError("a rising trigger needs reset_at below fire_at")
            if self.direction == "falling" and not reset > fire:
                raise ValueError("a falling trigger needs reset_at above fire_at")
            _set(self, "fire_at", fire)
            _set(self, "reset_at", reset)
            self._forbid("event", "schedule")
        elif self.kind == "event":
            _text(self.event, "trigger.event")
            self._forbid("metric", "direction", "fire_at", "reset_at", "schedule")
        else:
            _text(self.schedule, "trigger.schedule")
            self._forbid("metric", "direction", "fire_at", "reset_at", "event")

    def _forbid(self, *names: str) -> None:
        present = [n for n in names if getattr(self, n) is not None]
        if present:
            raise ValueError(f"a {self.kind} trigger does not take {', '.join(present)}")

    def to_dict(self) -> dict[str, Any]:
        return {
            "kind": self.kind,
            "metric": self.metric,
            "direction": self.direction,
            "fire_at": _num(self.fire_at),
            "reset_at": _num(self.reset_at),
            "event": self.event,
            "schedule": self.schedule,
        }

    @classmethod
    def from_dict(cls, data: Any) -> Trigger:
        keys = ("kind", "metric", "direction", "fire_at", "reset_at", "event", "schedule")
        data = _exact_keys(data, "trigger", keys)
        return cls(**{k: data.get(k) for k in keys})


@dataclass(frozen=True)
class Autonomy:
    """The line between act-on-its-own and ask-first. Anything unlisted is denied."""

    allowed: tuple[str, ...] = ()
    ask_first: tuple[str, ...] = ()

    def __post_init__(self) -> None:
        allowed = _texts(self.allowed, "autonomy.allowed")
        ask_first = _texts(self.ask_first, "autonomy.ask_first")
        both = set(allowed) & set(ask_first)
        if both:
            raise ValueError(f"operations both allowed and ask-first: {', '.join(sorted(both))}")
        _set(self, "allowed", allowed)
        _set(self, "ask_first", ask_first)

    def to_dict(self) -> dict[str, Any]:
        return {"allowed": list(self.allowed), "ask_first": list(self.ask_first)}

    @classmethod
    def from_dict(cls, data: Any) -> Autonomy:
        data = _exact_keys(data, "autonomy", ("allowed", "ask_first"))
        return cls(allowed=data.get("allowed", ()), ask_first=data.get("ask_first", ()))


@dataclass(frozen=True)
class Notify:
    """Who gets told, and how."""

    channel: Literal["sms", "email", "record"]
    to: str

    def __post_init__(self) -> None:
        _choice(self.channel, "notify.channel", NOTIFY_CHANNELS)
        _text(self.to, "notify.to")

    def to_dict(self) -> dict[str, Any]:
        return {"channel": self.channel, "to": self.to}

    @classmethod
    def from_dict(cls, data: Any) -> Notify:
        data = _exact_keys(data, "notify", ("channel", "to"))
        return cls(channel=data.get("channel"), to=data.get("to"))


@dataclass(frozen=True)
class EndConditions:
    """How the job can end.

    ``on_success="complete"`` ends the job after the first proven action;
    ``"rearm"`` goes back to waiting (a monitor that runs until its deadline).
    Running out of budget, a conflict and cancellation are always possible ends.
    """

    on_success: Literal["complete", "rearm"] = "complete"
    impossible_when: str = ""

    def __post_init__(self) -> None:
        _choice(self.on_success, "end.on_success", ON_SUCCESS)
        _text(self.impossible_when, "end.impossible_when", allow_empty=True)

    def to_dict(self) -> dict[str, Any]:
        return {"on_success": self.on_success, "impossible_when": self.impossible_when}

    @classmethod
    def from_dict(cls, data: Any) -> EndConditions:
        data = _exact_keys(data, "end", ("on_success", "impossible_when"))
        return cls(
            on_success=data.get("on_success", "complete"),
            impossible_when=data.get("impossible_when", ""),
        )


@dataclass(frozen=True)
class OwnerApproval:
    """The owner's say-so for a promise change (or, later, for a step)."""

    owner: str
    reason: str = ""
    approved_at: datetime | None = None

    def __post_init__(self) -> None:
        _text(self.owner, "approval.owner")
        _text(self.reason, "approval.reason", allow_empty=True)
        _optional_aware(self.approved_at, "approval.approved_at")


# ---------------------------------------------------------------- contract


def _part(value: Any, cls: type, label: str) -> Any:
    if not isinstance(value, cls):
        raise TypeError(f"{label} must be a {cls.__name__}")
    return value


@dataclass(frozen=True, kw_only=True)
class GoalContract:
    """One job, written down once. Frozen: change it through ``ContractHistory``."""

    contract_id: str
    owner: str
    outcome: str
    trigger: Trigger
    proof: Proof | None = None
    never: tuple[str, ...] = ()
    preferences: tuple[str, ...] = ()
    sacrifice_order: tuple[str, ...] = ()
    autonomy: Autonomy = field(default_factory=Autonomy)
    budget: Budget = field(default_factory=Budget)
    notify: tuple[Notify, ...] = ()
    end: EndConditions = field(default_factory=EndConditions)
    version: int = 1
    created_at: datetime = field(default_factory=utc_now)
    previous_version: int | None = None
    approved_by: str | None = None
    approved_at: datetime | None = None
    change_reason: str = ""

    def __post_init__(self) -> None:
        _text(self.contract_id, "contract_id")
        _text(self.owner, "owner")
        _text(self.outcome, "outcome")
        _part(self.trigger, Trigger, "trigger")
        if type(self.proof) is str:
            _set(self, "proof", Proof(self.proof))
        if self.proof is not None:
            _part(self.proof, Proof, "proof")
        _set(self, "never", _texts(self.never, "never"))
        _set(self, "preferences", _texts(self.preferences, "preferences"))
        _set(self, "sacrifice_order", _texts(self.sacrifice_order, "sacrifice_order"))
        _part(self.autonomy, Autonomy, "autonomy")
        _part(self.budget, Budget, "budget")
        if isinstance(self.notify, Notify) or not isinstance(self.notify, (list, tuple)):
            raise TypeError("notify must be a list or tuple of Notify")
        _set(self, "notify", tuple(_part(n, Notify, "notify item") for n in self.notify))
        _part(self.end, EndConditions, "end")
        if type(self.version) is not int or self.version < 1:
            raise ValueError("version must be a positive integer")
        require_aware(self.created_at, "created_at")
        if self.version == 1:
            if self.previous_version is not None:
                raise ValueError("version 1 has no previous version")
        elif self.previous_version != self.version - 1:
            raise ValueError("previous_version must be version - 1")
        _optional_text(self.approved_by, "approved_by")
        _optional_aware(self.approved_at, "approved_at")
        _text(self.change_reason, "change_reason", allow_empty=True)

    @property
    def is_armable(self) -> bool:
        return self.proof is not None and bool(self.proof.source.strip())

    def to_dict(self) -> dict[str, Any]:
        return {
            "contract_id": self.contract_id,
            "owner": self.owner,
            "version": self.version,
            "created_at": _iso(self.created_at),
            "previous_version": self.previous_version,
            "approved_by": self.approved_by,
            "approved_at": _iso(self.approved_at),
            "change_reason": self.change_reason,
            "outcome": self.outcome,
            "proof": None if self.proof is None else self.proof.to_dict(),
            "never": list(self.never),
            "preferences": list(self.preferences),
            "sacrifice_order": list(self.sacrifice_order),
            "trigger": self.trigger.to_dict(),
            "autonomy": self.autonomy.to_dict(),
            "budget": self.budget.to_dict(),
            "notify": [n.to_dict() for n in self.notify],
            "end": self.end.to_dict(),
        }

    @classmethod
    def from_dict(cls, data: Any) -> GoalContract:
        data = _exact_keys(data, "contract", RECORD_FIELDS | REVISABLE_FIELDS)
        missing = {"contract_id", "owner", "version", "created_at", "outcome", "trigger"} - set(
            data
        )
        if missing:
            raise ValueError(f"contract is missing keys: {', '.join(sorted(missing))}")
        proof = data.get("proof")
        return cls(
            contract_id=data["contract_id"],
            owner=data["owner"],
            version=data["version"],
            created_at=_json.parse_datetime(data["created_at"], "created_at"),
            previous_version=data.get("previous_version"),
            approved_by=data.get("approved_by"),
            approved_at=_date(data, "approved_at"),
            change_reason=data.get("change_reason", ""),
            outcome=data["outcome"],
            proof=None if proof is None else Proof.from_dict(proof),
            never=data.get("never", ()),
            preferences=data.get("preferences", ()),
            sacrifice_order=data.get("sacrifice_order", ()),
            trigger=Trigger.from_dict(data["trigger"]),
            autonomy=Autonomy.from_dict(data.get("autonomy", {})),
            budget=Budget.from_dict(data.get("budget", {})),
            notify=[Notify.from_dict(n) for n in _list(data.get("notify", []), "notify")],
            end=EndConditions.from_dict(data.get("end", {})),
        )


def _list(value: Any, label: str) -> list[Any]:
    if type(value) is not list:
        raise TypeError(f"{label} must be a JSON array")
    return value


def ensure_armable(contract: GoalContract) -> None:
    """Raise unless the contract names a proof source. No proof, no claim of success."""
    if not isinstance(contract, GoalContract):
        raise TypeError("ensure_armable expects a GoalContract")
    if not contract.is_armable:
        raise NotArmableError("contract names no proof source; it cannot be armed")


def changed_fields(old: GoalContract, new: GoalContract) -> frozenset[str]:
    return frozenset(f for f in REVISABLE_FIELDS if getattr(old, f) != getattr(new, f))


# ---------------------------------------------------------------- history


class ContractHistory:
    """Every version of one contract, oldest first. Versions are never rewritten."""

    def __init__(self, first: GoalContract) -> None:
        _part(first, GoalContract, "first")
        if first.version != 1:
            raise ValueError("a history starts at version 1; use from_dict() to load one")
        self._versions: list[GoalContract] = [first]

    @property
    def current(self) -> GoalContract:
        return self._versions[-1]

    @property
    def contract_id(self) -> str:
        return self._versions[0].contract_id

    @property
    def owner(self) -> str:
        return self._versions[0].owner

    def versions(self) -> tuple[GoalContract, ...]:
        return tuple(self._versions)

    def version(self, number: int) -> GoalContract:
        if type(number) is not int or not 1 <= number <= len(self._versions):
            raise KeyError(number)
        return self._versions[number - 1]

    def __len__(self) -> int:
        return len(self._versions)

    def revise(
        self,
        *,
        approval: OwnerApproval | None = None,
        reason: str = "",
        now: datetime | None = None,
        **changes: Any,
    ) -> GoalContract:
        """Write version N+1 with ``changes``; version N stays as it was.

        Changing any of ``PROMISE_FIELDS`` needs ``approval`` from the owner,
        otherwise ``PromiseChangeError``. A revision that changes nothing
        returns the current version and records nothing.
        """
        fixed = set(changes) & RECORD_FIELDS
        if fixed:
            raise TypeError(f"cannot revise record fields: {', '.join(sorted(fixed))}")
        unknown = set(changes) - REVISABLE_FIELDS
        if unknown:
            raise TypeError(f"unknown contract fields: {', '.join(sorted(unknown))}")
        if approval is not None:
            _part(approval, OwnerApproval, "approval")
        _text(reason, "reason", allow_empty=True)

        current = self.current
        candidate = dataclasses.replace(current, **changes)
        changed = changed_fields(current, candidate)
        if not changed:
            return current

        promise = changed & PROMISE_FIELDS
        if promise and approval is None:
            raise PromiseChangeError(
                "changing the promise needs the owner's approval: " + ", ".join(sorted(promise)),
                promise,
            )
        if approval is not None and approval.owner != current.owner:
            raise PromiseChangeError(
                f"approval is from {approval.owner!r}, not the contract owner {current.owner!r}",
                promise,
            )

        at = utc_now() if now is None else require_aware(now, "now")
        if at < current.created_at:
            raise ValueError("a new version cannot be dated before the current one")
        revised = dataclasses.replace(
            candidate,
            version=current.version + 1,
            previous_version=current.version,
            created_at=at,
            approved_by=None if approval is None else approval.owner,
            approved_at=None if approval is None else (approval.approved_at or at),
            change_reason=reason or ("" if approval is None else approval.reason),
        )
        self._versions.append(revised)
        return revised

    # ------------------------------------------------------------ transport

    def to_dict(self) -> dict[str, Any]:
        return {"schema": SCHEMA, "versions": [v.to_dict() for v in self._versions]}

    def to_json(self) -> str:
        return _json.dumps(self.to_dict())

    @classmethod
    def from_dict(cls, data: Any) -> ContractHistory:
        """Load a history and check that it could only have come from ``revise()``."""
        data = _exact_keys(data, "history", ("schema", "versions"))
        if data.get("schema") != SCHEMA:
            raise ValueError(f"unsupported history schema: {data.get('schema')!r}")
        raw = _list(data.get("versions"), "versions")
        if not raw:
            raise ValueError("a history needs at least one version")
        versions = [GoalContract.from_dict(v) for v in raw]
        first = versions[0]
        for number, (prev, cur) in enumerate(pairwise(versions), start=2):
            if cur.version != number:
                raise ValueError(f"versions must be consecutive; expected {number}")
            if (cur.contract_id, cur.owner) != (first.contract_id, first.owner):
                raise ValueError("every version must share contract_id and owner")
            if cur.created_at < prev.created_at:
                raise ValueError(f"version {number} is dated before version {number - 1}")
            changed = changed_fields(prev, cur)
            if not changed:
                raise ValueError(f"version {number} changes nothing")
            if changed & PROMISE_FIELDS and cur.approved_by != first.owner:
                raise PromiseChangeError(
                    f"version {number} changes the promise without the owner's approval",
                    changed & PROMISE_FIELDS,
                )
        history = cls(first)
        history._versions = versions
        return history

    @classmethod
    def from_json(cls, text: str) -> ContractHistory:
        return cls.from_dict(_json.loads(text))
