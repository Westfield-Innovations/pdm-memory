"""Executors: the hands. They act; they do not decide and they do not prove.

An executor receives the step and an idempotency key (the action id, the same
on every retry of that action) so an outside service can deduplicate too.
Returning means "returned", not "done": the checker proves the outcome.
"""

from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal
from typing import Any, Protocol

from pdm_memory.goal_loop import _json

__all__ = ["ActionResult", "Executor", "NotExecuted"]

RESULT_LIMIT = 65_536


class NotExecuted(Exception):
    """Raise when the executor knows for certain that nothing happened.

    That makes a retry safe. Any other exception means the outcome is unknown
    and the job escalates instead of repeating the step.
    """


@dataclass(frozen=True)
class ActionResult:
    reference: str | None = None  # for example the provider's message id
    cost: Decimal = Decimal(0)
    evidence: Any = None

    def __post_init__(self) -> None:
        if self.reference is not None and type(self.reference) is not str:
            raise TypeError("result reference must be a string")
        cost = _json.to_decimal(self.cost, "result cost")
        if cost < 0:
            raise ValueError("result cost must not be negative")
        object.__setattr__(self, "cost", cost)
        _json.dumps(self.evidence, limit=RESULT_LIMIT)

    def to_dict(self) -> dict[str, Any]:
        return {"reference": self.reference, "cost": str(self.cost), "evidence": self.evidence}

    @classmethod
    def from_dict(cls, data: Any) -> ActionResult:
        return cls(
            reference=data.get("reference"),
            cost=data.get("cost", "0"),
            evidence=data.get("evidence"),
        )


class Executor(Protocol):
    def __call__(self, step: Any, *, idempotency_key: str) -> ActionResult: ...
