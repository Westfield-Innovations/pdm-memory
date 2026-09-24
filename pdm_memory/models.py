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
        goals = (
            "; ".join(self.conflicting_goals[:3])
            if self.conflicting_goals
            else "(none)"
        )
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
class RelationshipState:
    """
    Point-in-time state of one relationship pair (spec §4.3): which links
    were live, which channel measurements applied, and — only within the
    channel's own recency window — the current resolution vector.

    Populated from Companion ``GET /api/v1/pdm/relationships/state/``.
    ``relationships`` and ``channels`` stay plain dicts rather than typed
    records: unlike :class:`RelationshipChannelResolution`, this route's
    per-domain shape (``bfr``, ``branches``, ``is_currently_blackout``, …)
    is its own thing, not that dataclass's flat vector, and is not
    established enough yet to freeze into a second one.

    ``current_resolution_by_domain`` is ``None`` — not an empty dict — when
    ``at_time`` fell outside the channel's recency window;
    ``current_resolution_reason`` then explains why (``"past_moment"``).
    Reading a resolution vector for a moment far enough in the past would
    be a claim about what was true then when it is only ever a claim about
    now — see the server route's own docstring.
    """

    source: str
    target: str
    domain: str
    at_time: str
    relationships: list[dict[str, Any]] = field(default_factory=list)
    channels: dict[str, dict[str, Any]] = field(default_factory=dict)
    current_resolution_by_domain: dict[str, dict[str, Any]] | None = None
    current_resolution_reason: str | None = None
    last_direct_measurement: str | None = None

    def as_dict(self) -> dict[str, Any]:
        return {
            "source": self.source,
            "target": self.target,
            "domain": self.domain,
            "at_time": self.at_time,
            "relationships": list(self.relationships),
            "channels": dict(self.channels),
            "current_resolution_by_domain": (
                dict(self.current_resolution_by_domain)
                if self.current_resolution_by_domain is not None
                else None
            ),
            "current_resolution_reason": self.current_resolution_reason,
            "last_direct_measurement": self.last_direct_measurement,
        }

    @classmethod
    def from_payload(cls, payload: dict[str, Any]) -> RelationshipState:
        raw_resolution = payload.get("current_resolution_by_domain")
        return cls(
            source=str(payload.get("source", "")),
            target=str(payload.get("target", "")),
            domain=str(payload.get("domain", "*")),
            at_time=str(payload.get("at_time", "")),
            relationships=[dict(row) for row in payload.get("relationships") or []],
            channels={
                str(domain): dict(entry)
                for domain, entry in (payload.get("channels") or {}).items()
            },
            current_resolution_by_domain=(
                {str(domain): dict(entry) for domain, entry in raw_resolution.items()}
                if raw_resolution is not None
                else None
            ),
            current_resolution_reason=(
                str(payload["current_resolution_reason"])
                if payload.get("current_resolution_reason") is not None
                else None
            ),
            last_direct_measurement=(
                str(payload["last_direct_measurement"])
                if payload.get("last_direct_measurement") is not None
                else None
            ),
        )


@dataclass(slots=True)
class FieldStateSnapshot:
    """
    One bounded field's reconciled state at one moment, for one observer.

    Populated from Companion ``GET /api/v1/pdm/field-state/``, which serves
    both ``state_at`` and ``current_state``.

    Every item in a tagged section carries its own ``state_type``, and
    ``from_payload`` refuses a section where one does not — measured history
    and projected state stay distinguishable per item, never by convention.

    ``envelope_included`` is False when only the entities page was asked for.
    ``as_dict`` then omits the envelope keys rather than emitting empty lists,
    mirroring the server: ``[]`` read as "no relationships" would take a
    page-2 response for a statement about the field.
    """

    field_id: str
    at_time: str
    state_type: str
    entities: list[dict[str, Any]] = field(default_factory=list)
    entities_next_cursor: str | None = None
    envelope_included: bool = True
    relationships: list[dict[str, Any]] = field(default_factory=list)
    field_memberships: list[dict[str, Any]] = field(default_factory=list)
    relationship_bandwidth: list[dict[str, Any]] = field(default_factory=list)
    projection_branches: list[dict[str, Any]] = field(default_factory=list)
    provenance: list[dict[str, Any]] = field(default_factory=list)
    permission_view: dict[str, Any] = field(default_factory=dict)
    truncated: list[str] = field(default_factory=list)

    def as_dict(self) -> dict[str, Any]:
        payload: dict[str, Any] = {
            "field_id": self.field_id,
            "at_time": self.at_time,
            "state_type": self.state_type,
            "entities": list(self.entities),
            "entities_next_cursor": self.entities_next_cursor,
            "envelope_included": self.envelope_included,
        }
        if not self.envelope_included:
            return payload

        payload.update(
            {
                "relationships": list(self.relationships),
                "field_memberships": list(self.field_memberships),
                "relationship_bandwidth": list(self.relationship_bandwidth),
                "projection_branches": list(self.projection_branches),
                "provenance": list(self.provenance),
                "permission_view": dict(self.permission_view),
                "truncated": list(self.truncated),
            }
        )
        return payload

    @classmethod
    def from_payload(cls, payload: dict[str, Any]) -> FieldStateSnapshot:
        def section(name: str) -> list[dict[str, Any]]:
            """Tagged section: provenance and permission_view are not items."""
            rows = [dict(row) for row in payload.get(name) or []]
            untagged = sum(1 for row in rows if not row.get("state_type"))
            if untagged:
                raise ValueError(
                    f"{name} carries {untagged} item(s) with no state_type. "
                    "An untagged item cannot be read as measured or projected."
                )
            return rows

        return cls(
            field_id=str(payload.get("field_id", "")),
            at_time=str(payload.get("at_time", "")),
            state_type=str(payload.get("state_type", "")),
            entities=section("entities"),
            entities_next_cursor=(
                str(payload["entities_next_cursor"])
                if payload.get("entities_next_cursor") is not None
                else None
            ),
            # True only when the key is absent, as an older server would leave
            # it; a server that says False means it.
            envelope_included=bool(payload.get("envelope_included", True)),
            relationships=section("relationships"),
            field_memberships=section("field_memberships"),
            relationship_bandwidth=section("relationship_bandwidth"),
            projection_branches=section("projection_branches"),
            provenance=[dict(row) for row in payload.get("provenance") or []],
            permission_view=dict(payload.get("permission_view") or {}),
            truncated=[str(name) for name in payload.get("truncated") or []],
        )


@dataclass(slots=True)
class TrajectoryStep:
    """
    One transition — a field entered, a fact filed, a link formed — from
    ``Memory.trajectory`` (spec §7, §3).

    Mirrors Companion's own item shape field for field: ``at``, ``kind``,
    ``ref_id``, ``field_id``, ``detail``, ``state_type``. ``kind`` is one of
    ``membership_opened`` / ``membership_closed`` / ``fact_filed`` /
    ``fact_unfiled`` / ``link_opened`` / ``link_closed`` locally, plus
    ``grant_changed`` / ``projection_recorded`` / ``outcome_recorded`` when
    the trajectory came from the cloud — the local store has no perspective
    log or projection table to produce the last three from, and inventing
    them here would be a claim about data this process never held.
    """

    at: str
    kind: str
    ref_id: str
    field_id: str
    detail: dict[str, Any]
    state_type: str

    @classmethod
    def from_payload(cls, payload: dict[str, Any]) -> TrajectoryStep:
        if not payload.get("state_type"):
            raise ValueError(
                "trajectory step carries no state_type — every item crossing "
                "this boundary must declare whether it is measured or "
                "projected."
            )
        return cls(
            at=str(payload.get("at", "")),
            kind=str(payload.get("kind", "")),
            ref_id=str(payload.get("ref_id", "")),
            field_id=str(payload.get("field_id", "")),
            detail=dict(payload.get("detail") or {}),
            state_type=str(payload["state_type"]),
        )

    def as_dict(self) -> dict[str, Any]:
        return {
            "at": self.at,
            "kind": self.kind,
            "ref_id": self.ref_id,
            "field_id": self.field_id,
            "detail": dict(self.detail),
            "state_type": self.state_type,
        }


@dataclass(slots=True)
class Trajectory:
    """
    One page of a subject's transitions, oldest first (spec §7, §3).

    ``truncated`` is not a field the wire carries — a trajectory has exactly
    one truncatable thing, the step list itself, and ``next_cursor`` already
    says whether more remain. It is derived rather than duplicated so the two
    can never disagree.
    """

    subject_id: str
    start: str
    end: str
    steps: list[TrajectoryStep] = field(default_factory=list)
    next_cursor: str | None = None

    @property
    def truncated(self) -> bool:
        return self.next_cursor is not None

    @classmethod
    def from_payload(cls, payload: dict[str, Any]) -> Trajectory:
        return cls(
            subject_id=str(payload.get("subject_id", "")),
            start=str(payload.get("start", "")),
            end=str(payload.get("end", "")),
            steps=[
                TrajectoryStep.from_payload(row) for row in payload.get("steps") or []
            ],
            next_cursor=(
                str(payload["next_cursor"])
                if payload.get("next_cursor") is not None
                else None
            ),
        )

    def as_dict(self) -> dict[str, Any]:
        return {
            "subject_id": self.subject_id,
            "start": self.start,
            "end": self.end,
            "steps": [step.as_dict() for step in self.steps],
            "next_cursor": self.next_cursor,
            "truncated": self.truncated,
        }


def _require_projected(payload: dict[str, Any], what: str) -> str:
    """
    §13 asks for a projection to stay distinct from measured history
    "visibly and programmatically": a payload that does not say
    ``projected`` is refused rather than typed as a projection anyway.
    """
    state_type = payload.get("state_type")
    if state_type != "projected":
        raise ValueError(
            f"{what} arrived with state_type={state_type!r}; a projection "
            "that does not declare itself projected cannot be told apart "
            "from measured history."
        )
    return state_type


@dataclass(slots=True)
class ProjectionBranch:
    """
    One branch of a forward fan (spec §4.6), mirroring Companion's item.

    ``confidence_band`` carries ``weight`` (this branch's share of the fan),
    ``confidence``, ``bfr`` and ``is_currently_blackout``; ``timing_range``
    is always a window, never a point. ``ghost_nodes`` is empty on every
    server today — nothing derives projected entities, by design.
    """

    branch_id: str
    domain: str
    kind: str
    horizon_days: int
    base_state_version: str
    confidence_band: dict[str, Any]
    timing_range: dict[str, Any]
    invalidation_conditions: list[str]
    ghost_nodes: list[dict[str, Any]]
    ghost_relationships: list[dict[str, Any]]
    state_type: str

    @classmethod
    def from_payload(cls, payload: dict[str, Any]) -> ProjectionBranch:
        return cls(
            branch_id=str(payload.get("branch_id", "")),
            domain=str(payload.get("domain", "")),
            kind=str(payload.get("kind", "")),
            horizon_days=int(payload.get("horizon_days") or 0),
            base_state_version=str(payload.get("base_state_version") or ""),
            confidence_band=dict(payload.get("confidence_band") or {}),
            timing_range=dict(payload.get("timing_range") or {}),
            invalidation_conditions=[
                str(c) for c in payload.get("invalidation_conditions") or []
            ],
            ghost_nodes=[dict(n) for n in payload.get("ghost_nodes") or []],
            ghost_relationships=[
                dict(r) for r in payload.get("ghost_relationships") or []
            ],
            state_type=_require_projected(payload, "projection branch"),
        )

    def as_dict(self) -> dict[str, Any]:
        return {
            "branch_id": self.branch_id,
            "domain": self.domain,
            "kind": self.kind,
            "horizon_days": self.horizon_days,
            "base_state_version": self.base_state_version,
            "confidence_band": dict(self.confidence_band),
            "timing_range": dict(self.timing_range),
            "invalidation_conditions": list(self.invalidation_conditions),
            "ghost_nodes": [dict(n) for n in self.ghost_nodes],
            "ghost_relationships": [dict(r) for r in self.ghost_relationships],
            "state_type": self.state_type,
        }


@dataclass(slots=True)
class ProjectionFan:
    """
    The caller's forward fan from ``Memory.project`` (spec §7).

    A separate type from ``FieldStateSnapshot`` on purpose, sharing no base
    class: ``isinstance`` alone tells a projection from a measured state.

    ``projection_ids`` lists the rows this call wrote. With ``record=True``
    it can still be empty: the server records one fan per subject, domain
    and kind per UTC day, and a repeat the same day writes nothing.
    """

    branches: list[ProjectionBranch] = field(default_factory=list)
    record: bool = False
    projection_ids: list[str] = field(default_factory=list)

    @classmethod
    def from_payload(cls, payload: dict[str, Any]) -> ProjectionFan:
        return cls(
            branches=[
                ProjectionBranch.from_payload(b) for b in payload.get("branches") or []
            ],
            record=bool(payload.get("record", False)),
            projection_ids=[str(i) for i in payload.get("projection_ids") or []],
        )

    def as_dict(self) -> dict[str, Any]:
        return {
            "branches": [b.as_dict() for b in self.branches],
            "record": self.record,
            "projection_ids": list(self.projection_ids),
        }


@dataclass(slots=True)
class ProjectionOutcomeRecord:
    """
    What actually happened, set against a recorded projection (spec §13).

    ``timing`` (``early`` / ``within`` / ``late``) is derived by the server
    from ``observed_at`` against the projection's own window — never sent.
    """

    id: str
    observed_at: str
    recorded_at: str
    timing: str
    connection_geometry: str
    meaning_propagation: str
    model_update: str

    @classmethod
    def from_payload(cls, payload: dict[str, Any]) -> ProjectionOutcomeRecord:
        return cls(
            id=str(payload.get("id", "")),
            observed_at=str(payload.get("observed_at", "")),
            recorded_at=str(payload.get("recorded_at", "")),
            timing=str(payload.get("timing", "")),
            connection_geometry=str(payload.get("connection_geometry", "")),
            meaning_propagation=str(payload.get("meaning_propagation", "")),
            model_update=str(payload.get("model_update") or ""),
        )

    def as_dict(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "observed_at": self.observed_at,
            "recorded_at": self.recorded_at,
            "timing": self.timing,
            "connection_geometry": self.connection_geometry,
            "meaning_propagation": self.meaning_propagation,
            "model_update": self.model_update,
        }


@dataclass(slots=True)
class RecordedProjection:
    """
    One persisted projection branch, with its outcome once settled.

    From ``Memory.projection`` and ``Memory.record_outcome``. ``outcome`` is
    None until something records what happened; a projection settles once.
    """

    id: str
    subject_ref: str
    domain: str
    branch_kind: str
    weight: float
    confidence: float | None
    bfr: float | None
    projected_at: str
    horizon_start: str
    horizon_end: str
    invalidation_conditions: list[str]
    base_state_version: str
    state_type: str
    outcome: ProjectionOutcomeRecord | None = None

    @classmethod
    def from_payload(cls, payload: dict[str, Any]) -> RecordedProjection:
        outcome = payload.get("outcome")
        return cls(
            id=str(payload.get("id", "")),
            subject_ref=str(payload.get("subject_ref", "")),
            domain=str(payload.get("domain", "")),
            branch_kind=str(payload.get("branch_kind", "")),
            weight=float(payload.get("weight") or 0.0),
            confidence=(
                float(payload["confidence"])
                if payload.get("confidence") is not None
                else None
            ),
            bfr=float(payload["bfr"]) if payload.get("bfr") is not None else None,
            projected_at=str(payload.get("projected_at", "")),
            horizon_start=str(payload.get("horizon_start", "")),
            horizon_end=str(payload.get("horizon_end", "")),
            invalidation_conditions=[
                str(c) for c in payload.get("invalidation_conditions") or []
            ],
            base_state_version=str(payload.get("base_state_version") or ""),
            state_type=_require_projected(payload, "recorded projection"),
            outcome=(
                ProjectionOutcomeRecord.from_payload(outcome)
                if isinstance(outcome, dict)
                else None
            ),
        )

    def as_dict(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "subject_ref": self.subject_ref,
            "domain": self.domain,
            "branch_kind": self.branch_kind,
            "weight": self.weight,
            "confidence": self.confidence,
            "bfr": self.bfr,
            "projected_at": self.projected_at,
            "horizon_start": self.horizon_start,
            "horizon_end": self.horizon_end,
            "invalidation_conditions": list(self.invalidation_conditions),
            "base_state_version": self.base_state_version,
            "state_type": self.state_type,
            "outcome": self.outcome.as_dict() if self.outcome else None,
        }


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
