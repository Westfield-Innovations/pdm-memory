"""The checker: did it work? (Not: is it allowed. That is the guard.)

A checker looks at the world, not at what the executor said. It returns a
``ProofVerdict``. ``PROVEN`` carries a ``ProofReceipt`` naming the contract's
proof source; the controller refuses to complete a job on anything less, so a
checker cannot end a job with "looks done to me".
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any, Literal, Protocol

from pdm_memory.goal_loop import _json
from pdm_memory.goal_loop.contract import GoalContract

__all__ = ["CallableChecker", "Checker", "ProofReceipt", "ProofVerdict"]

STATUSES = ("PROVEN", "NOT_YET", "FAILED", "IMPOSSIBLE")
EVIDENCE_LIMIT = 65_536


@dataclass(frozen=True)
class ProofReceipt:
    """What the checker saw: the source it asked, a reference, when, and the evidence."""

    source: str
    reference: str
    observed_at: datetime
    evidence: Any = None

    def __post_init__(self) -> None:
        if type(self.source) is not str or not self.source.strip():
            raise ValueError("proof receipt source must not be blank")
        if type(self.reference) is not str:
            raise TypeError("proof receipt reference must be a string")
        _json.require_aware(self.observed_at, "proof receipt observed_at")
        _json.dumps(self.evidence, limit=EVIDENCE_LIMIT)

    def to_dict(self) -> dict[str, Any]:
        return {
            "source": self.source,
            "reference": self.reference,
            "observed_at": _json.format_datetime(self.observed_at),
            "evidence": self.evidence,
        }

    @classmethod
    def from_dict(cls, data: Any) -> ProofReceipt:
        return cls(
            source=data["source"],
            reference=data["reference"],
            observed_at=_json.parse_datetime(data["observed_at"], "observed_at"),
            evidence=data.get("evidence"),
        )


@dataclass(frozen=True)
class ProofVerdict:
    """``PROVEN`` (with a receipt), ``NOT_YET`` (ask again later), ``FAILED`` (try again,
    the step did not work) or ``IMPOSSIBLE`` (it cannot work; stop)."""

    status: Literal["PROVEN", "NOT_YET", "FAILED", "IMPOSSIBLE"]
    reason: str = ""
    proof: ProofReceipt | None = field(default=None)

    def __post_init__(self) -> None:
        if self.status not in STATUSES:
            raise ValueError(f"verdict status must be one of {', '.join(STATUSES)}")
        if type(self.reason) is not str:
            raise TypeError("verdict reason must be a string")
        if self.proof is not None and not isinstance(self.proof, ProofReceipt):
            raise TypeError("verdict proof must be a ProofReceipt")


class Checker(Protocol):
    """Judges the real result. ``source`` is the proof source it can verify."""

    source: str

    def check(
        self,
        contract: GoalContract,
        step: Any,
        result: Any,
        evidence: Any,
        now: datetime,
    ) -> ProofVerdict: ...


class CallableChecker:
    """Wrap a function ``fn(contract, step, result, evidence, now) -> ProofVerdict``."""

    def __init__(self, source: str, fn: Callable[..., ProofVerdict]) -> None:
        if type(source) is not str or not source.strip():
            raise ValueError("checker source must not be blank")
        if not callable(fn):
            raise TypeError("fn must be callable")
        self.source = source
        self._fn = fn

    def check(
        self,
        contract: GoalContract,
        step: Any,
        result: Any,
        evidence: Any,
        now: datetime,
    ) -> ProofVerdict:
        return self._fn(contract, step, result, evidence, now)
