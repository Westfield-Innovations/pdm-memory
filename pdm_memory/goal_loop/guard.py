"""The guard: is this proposed step allowed? (Not: did it work. That is the checker.)

``DefaultGuard`` asks two things, in this order:

1. Is the operation inside the contract's autonomy line? Anything not listed in
   ``autonomy.allowed`` is denied. This is the hard wall.
2. Does ``verify()`` find the step in conflict with the contract's ``never``
   rules? This is a second net, not a replacement for 1.

Only ``contract.never`` goes to ``verify()``. Rules the controller enforces by
structure (no repeat alerts, hysteresis, limits) belong in the trigger and the
budget, not in ``never``.

A guard must be a pure function of its arguments: the controller may run it
again if a concurrent write forces a retry.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Literal, Protocol

from pdm_memory.core.alignment import CONFLICT_STATUS_THRESHOLD, TORSION_STATUS_THRESHOLD, verify
from pdm_memory.goal_loop.contract import GoalContract
from pdm_memory.goal_loop.job import ProposedStep

__all__ = ["DefaultGuard", "Guard", "GuardDecision"]

Verdict = Literal["proceed", "escalate", "conflict"]


@dataclass(frozen=True)
class GuardDecision:
    verdict: Verdict
    reason: str
    report: dict[str, Any] | None = None

    def to_dict(self) -> dict[str, Any]:
        return {"verdict": self.verdict, "reason": self.reason, "report": self.report}


class Guard(Protocol):
    def check(
        self, contract: GoalContract, step: ProposedStep, *, owner_approved: bool = False
    ) -> GuardDecision: ...


class DefaultGuard:
    """Autonomy allowlist first, then ``verify()`` against ``contract.never``.

    ``torsion_policy="conflict"`` (default) ends the job when a step contradicts
    a never rule. ``"escalate"`` asks the owner instead, for hosts that prefer a
    person over a dead job while ``verify()`` still produces false alarms.
    """

    def __init__(
        self,
        *,
        torsion_threshold: float = TORSION_STATUS_THRESHOLD,
        conflict_threshold: float = CONFLICT_STATUS_THRESHOLD,
        torsion_policy: Literal["conflict", "escalate"] = "conflict",
    ) -> None:
        if torsion_policy not in ("conflict", "escalate"):
            raise ValueError("torsion_policy must be 'conflict' or 'escalate'")
        self.torsion_threshold = torsion_threshold
        self.conflict_threshold = conflict_threshold
        self.torsion_policy = torsion_policy

    def check(
        self, contract: GoalContract, step: ProposedStep, *, owner_approved: bool = False
    ) -> GuardDecision:
        if not owner_approved and step.operation not in contract.autonomy.allowed:
            where = "ask-first" if step.operation in contract.autonomy.ask_first else "not listed"
            return GuardDecision(
                "escalate", f"operation {step.operation!r} is outside the autonomy line ({where})"
            )
        if not contract.never:
            return GuardDecision("proceed", "no never rules to check")
        report = verify(
            step.summary,
            list(contract.never),
            torsion_threshold=self.torsion_threshold,
            conflict_threshold=self.conflict_threshold,
        )
        data = report.as_dict()
        if report.status == "ALIGNED":
            return GuardDecision("proceed", "verify(): aligned with every never rule", data)
        if report.status == "CONFLICT":
            if owner_approved:
                return GuardDecision("proceed", "verify(): partial conflict; owner approved", data)
            return GuardDecision("escalate", f"verify(): {report.explanation}", data)
        verdict: Verdict = "conflict" if self.torsion_policy == "conflict" else "escalate"
        return GuardDecision(verdict, f"verify(): {report.explanation}", data)
