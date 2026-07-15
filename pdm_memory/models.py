# © 2026 Westfield Innovations LLC. Patent Pending.
# U.S. App. No. 19/739,419 | 63/953,563 | 63/953,842
# MODIFICATION PROHIBITED. USE AS SHIPPED.

"""
SDK-facing report models (no Django dependency).

Keep these stable: they are part of the public API surface for tooling/CLI.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Optional


@dataclass(slots=True)
class TorsionReport:
    """
    One Reverse Resonance hit: two signatures about the same topic that disagree.

    torsion_score in [0, 1] — product of topic similarity and contradiction strength.
    """

    signature_a_id: str
    signature_b_id: str
    signature_a_text: str
    signature_b_text: str
    drawer: str
    domain: str
    torsion_score: float
    topic_similarity: float
    contradiction_strength: float
    explanation: str
    conflict_kind: str  # deadline | factual | polarity | pressure | semantic

    cluster_key: Optional[str] = None

    def render(self) -> str:
        """Human-readable one-liner for CLI / logs."""
        return (
            f"[{self.torsion_score:.2f}] {self.conflict_kind} | "
            f"{self.drawer}/{self.domain}\n"
            f"  {self.explanation}"
        )
