# © 2026 Westfield Innovations LLC. Patent Pending.
# U.S. App. No. 19/739,419 | 63/953,563 | 63/953,842
# MODIFICATION PROHIBITED. USE AS SHIPPED.

"""Internal retrieval result types."""

from __future__ import annotations

from dataclasses import dataclass, field


@dataclass
class NodeCoupling:
    signature_id: str
    p_effective: float
    p_magnitude_raw: float
    coupling_score: float
    is_coupled: bool
    auto_fire_eligible: bool
    reinforcement_delta: float = 0.0
    tag_overlap: float = 0.0
    domain_match: float = 0.0
    regime_match: float = 0.0
    pressure_proximity: float = 0.0


@dataclass
class RetrievalResult:
    """Full result from the TAS retrieval engine."""

    found: bool
    threshold_used: float
    base_threshold: float
    search_cost: float
    coupled_nodes: list[NodeCoupling] = field(default_factory=list)
    damped_nodes: list[NodeCoupling] = field(default_factory=list)
    top_node: NodeCoupling | None = None
    total_scanned: int = 0
    reinforced_count: int = 0
