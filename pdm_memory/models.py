# © 2026 Westfield Innovations LLC. Patent Pending.
# U.S. App. No. 19/739,419 | 63/953,563 | 63/953,842
# MODIFICATION PROHIBITED. USE AS SHIPPED.

"""
SDK-facing report models (no Django dependency).

Keep these stable: they are part of the public API surface for tooling/CLI.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any


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

    cluster_key: str | None = None

    def render(self) -> str:
        """Human-readable one-liner for CLI / logs."""
        return (
            f"[{self.torsion_score:.2f}] {self.conflict_kind} | "
            f"{self.drawer}/{self.domain}\n"
            f"  {self.explanation}"
        )


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
    conflicting_goals: list[str] = field(default_factory=list)
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


@dataclass(slots=True)
class MemoryListPage:
    """Keyset-paginated list of memories."""

    items: list[Any]
    next_cursor_id: str | None = None


@dataclass(slots=True)
class RelationshipChannelResolution:
    """
    Domained communication-channel vector (TKT-301).

    Multidimensional only — never a single channel health percentage.
    Populated from Companion ``GET /api/v1/integrity/profile/``.
    """

    observer_key: str
    target_key: str
    domain: str
    recency_days: float
    frequency: int
    breadth: float
    directionality_inbound: float
    directionality_outbound: float
    directionality_bilateral: float
    information_bandwidth: float
    computation_window_days: int = 90
    last_computed_at: str | None = None
    updated_at: str | None = None

    def as_dict(self) -> dict[str, Any]:
        return {
            "observer_key": self.observer_key,
            "target_key": self.target_key,
            "domain": self.domain,
            "recency_days": round(float(self.recency_days), 4),
            "frequency": int(self.frequency),
            "breadth": round(float(self.breadth), 4),
            "directionality_inbound": round(float(self.directionality_inbound), 4),
            "directionality_outbound": round(float(self.directionality_outbound), 4),
            "directionality_bilateral": round(float(self.directionality_bilateral), 4),
            "information_bandwidth": round(float(self.information_bandwidth), 4),
            "computation_window_days": int(self.computation_window_days),
            "last_computed_at": self.last_computed_at,
            "updated_at": self.updated_at,
        }

    @classmethod
    def from_payload(cls, payload: dict[str, Any]) -> RelationshipChannelResolution:
        forbidden = {
            "channel_score",
            "channel_health",
            "resolution_percent",
            "resolution_pct",
            "relationship_channel_score",
        }
        overlap = forbidden.intersection(payload.keys())
        if overlap:
            joined = ", ".join(sorted(overlap))
            raise ValueError(
                f"Collapsed channel score fields are forbidden: {joined}. "
                "Expose RelationshipChannel vector dimensions separately."
            )
        return cls(
            observer_key=str(payload.get("observer_key", "principal")),
            target_key=str(payload.get("target_key", "operator")),
            domain=str(payload.get("domain", "*")),
            recency_days=float(payload.get("recency_days", 0.0) or 0.0),
            frequency=int(payload.get("frequency", 0) or 0),
            breadth=float(payload.get("breadth", 0.0) or 0.0),
            directionality_inbound=float(
                payload.get("directionality_inbound", 0.0) or 0.0
            ),
            directionality_outbound=float(
                payload.get("directionality_outbound", 0.0) or 0.0
            ),
            directionality_bilateral=float(
                payload.get("directionality_bilateral", 0.0) or 0.0
            ),
            information_bandwidth=float(
                payload.get("information_bandwidth", 0.0) or 0.0
            ),
            computation_window_days=int(
                payload.get("computation_window_days", 90) or 90
            ),
            last_computed_at=(
                str(payload["last_computed_at"])
                if payload.get("last_computed_at") is not None
                else None
            ),
            updated_at=(
                str(payload["updated_at"])
                if payload.get("updated_at") is not None
                else None
            ),
        )


@dataclass(slots=True)
class SurfaceReport:
    """
    Lite agent-loop snapshot: recall + torsion scan + alignment gate for one query.
    """

    hits: list[Any]
    torsion_count: int
    alignment: str
    alignment_score: float = 0.0
    torsion_reports: list[TorsionReport] = field(default_factory=list)

    def as_dict(self) -> dict[str, Any]:
        return {
            "hits": [
                {
                    "id": h.id,
                    "text": h.text,
                    "pressure": round(float(h.pressure), 2),
                    "p_raw": round(float(h.p_raw), 2),
                    "drawer": h.drawer,
                    "coupling_score": round(float(h.coupling_score), 4),
                    "tags": list(h.intent_tags),
                }
                for h in self.hits
            ],
            "torsion_count": self.torsion_count,
            "alignment": self.alignment,
            "alignment_score": round(float(self.alignment_score), 4),
        }
