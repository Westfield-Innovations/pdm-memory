# © 2026 Westfield Innovations LLC. Patent Pending.
# U.S. App. No. 19/739,419 | 63/953,563 | 63/953,842
# MODIFICATION PROHIBITED. USE AS SHIPPED.

"""
SDK-facing report models (no Django dependency).
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, List


@dataclass(slots=True)
class AlignmentReport:
    """
    Goal-Anchor Alignment (GAA) result for a proposed intent / ACT.

    status:
      ALIGNED  — intent resonates with high-IAW goals, low deviation
      CONFLICT — soft mismatch or insufficient anchor coverage
      TORSION  — intent contradicts a core goal (guarded agents must block ACT)
    score: composite alignment in [0, 1] (higher = safer / more aligned)
    """

    status: str
    score: float
    conflicting_goals: List[str] = field(default_factory=list)
    explanation: str = ""
    resonance: float = 0.0
    torsion: float = 0.0
    anchor_count: int = 0

    def as_dict(self) -> dict[str, Any]:
        return {
            "status": self.status,
            "score": round(float(self.score), 4),
            "conflicting_goals": list(self.conflicting_goals),
            "explanation": self.explanation,
        }

    def render(self) -> str:
        goals = "; ".join(self.conflicting_goals[:3]) if self.conflicting_goals else "(none)"
        return (
            f"[{self.status}] score={self.score:.3f} "
            f"resonance={self.resonance:.3f} torsion={self.torsion:.3f}\n"
            f"  {self.explanation}\n"
            f"  conflicting_goals: {goals}"
        )

    @property
    def is_safe_to_act(self) -> bool:
        """True only when a guarded agent may proceed with ACT."""
        return self.status == "ALIGNED"
