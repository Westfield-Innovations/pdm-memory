# © 2026 Westfield Innovations LLC. Patent Pending.
# U.S. App. No. 19/739,419 | 63/953,563 | 63/953,842
# MODIFICATION PROHIBITED. USE AS SHIPPED.

"""
PDM Retrieval Engine — pure TAS (Threshold-Adjustment Search) without Django.

Ported from companion_api/pdm/threshold_search/engine.py.
All Django ORM calls removed. Accepts List[SignatureRecord] directly.

Three-phase search:
  Phase 1 — Threshold Lowering:    θ_eff = θ_base × (1 - α × search_cost)
  Phase 2 — Impedance Matching:    coupling_score = weighted tag/domain/regime/pressure
  Phase 3 — Reinforcement delta:   Δp = REINF_BASE × log(1 + retrieval_count) × coupling
"""

from __future__ import annotations

import logging
import math
import re
from collections.abc import Iterable, Iterator, Sequence
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from pdm_memory.types import TorsionJudge

from pdm_memory.core.constraints import (
    collect_occupants,
    detect_constraint_violation,
    entity_exclusion_pair,
    parse_exclusive_slot,
    parse_presence,
)
from pdm_memory.core.math import (
    P_MAX,
    calculate_decay_factor,
    calculate_intent_weight,
    calculate_p_effective,
    calculate_temporal_geometry,
    calculate_v,
    infer_domain,
    infer_regime,
    resolve_half_life,
)
from pdm_memory.core.signature import MemoryHit, SignatureRecord
from pdm_memory.models import AlignmentReport, TorsionReport

logger = logging.getLogger(__name__)

WORD_PATTERN = re.compile(r"\b[a-zA-Z]{3,}\b")
# Standalone numerics only — reject Q3 / v2 / H1 bleed into factual torsion
NUMBER_PATTERN = re.compile(r"(?<![A-Za-z])-?\d+(?:\.\d+)?(?![A-Za-z])")

# Reverse Resonance — topic/contradiction gates (surgical, not N² fishing)
_TOPIC_GATE: float = 0.35
_SAME_DRAWER_TOPIC_GATE: float = 0.25
_SMALL_CLUSTER: int = 48
# Auto-discover virtual clusters when metadata.cluster_id is absent
_AUTO_CLUSTER_RESONANCE: float = 0.85
_ATTRIBUTE_TAG_OVERLAP: float = 0.80
_ATTRIBUTE_ROLE_TAGS: frozenset[str] = frozenset(
    {"goal", "anchor", "principle", "policy", "stewardship", "foundational"}
)
_ATTRIBUTE_HINT_TAGS: frozenset[str] = frozenset(
    {
        "date",
        "deadline",
        "pressure",
        "reading",
        "release",
        "schedule",
        "state",
        "status",
        "time",
        "value",
        "version",
    }
)
_TEMPORAL_ATTRIBUTE_VALUES: frozenset[str] = frozenset(
    {
        "monday",
        "tuesday",
        "wednesday",
        "thursday",
        "friday",
        "saturday",
        "sunday",
        "today",
        "tomorrow",
        "yesterday",
    }
)
_STATUS_ATTRIBUTE_VALUES: frozenset[str] = frozenset(
    {
        "active",
        "approved",
        "blocked",
        "cancelled",
        "closed",
        "complete",
        "completed",
        "delayed",
        "done",
        "failed",
        "failing",
        "impossible",
        "inactive",
        "merged",
        "moved",
        "open",
        "pending",
        "ready",
        "rejected",
        "scheduled",
        "started",
        "stopped",
    }
)
_INTEGRITY_DRAWERS: frozenset[str] = frozenset(
    {"anchors", "foundational", "goals", "mission", "principles", "stewardship"}
)
_INTEGRITY_TAGS: frozenset[str] = frozenset(
    {"anchor", "foundational", "goal", "policy", "principle", "rule", "stewardship"}
)
# PDM-T: how hard urgency energy lifts ranking / Phase-1 gate
_TEMPORAL_RANK_BOOST: float = 0.35
_TEMPORAL_GATE_BOOST: float = 0.25
# Recommended / API-default drawer share cap for parliamentary breadth
DEFAULT_DIVERSITY_BIAS: float = 0.40
# Extra rank weight when t_event_at falls inside a query-relative window
_EVENT_WINDOW_RANK_BOOST: float = 1.75
_NEGATION_TOKENS: frozenset[str] = frozenset(
    {
        # English (incl. typo / no-apostrophe forms users actually type)
        "not",
        "never",
        "no",
        "false",
        "without",
        "none",
        "neither",
        "nor",
        "cannot",
        "dont",
        "doesnt",
        "didnt",
        "isnt",
        "arent",
        "wasnt",
        "werent",
        "wont",
        "cant",
        "shouldnt",
        "wouldnt",
        "couldnt",
    }
)
# Applied before polarity checks — user text is messy
_CONTRACTION_MAP: tuple[tuple[str, str], ...] = (
    ("don't", " do not "),
    ("doesn't", " does not "),
    ("didn't", " did not "),
    ("isn't", " is not "),
    ("aren't", " are not "),
    ("wasn't", " was not "),
    ("weren't", " were not "),
    ("won't", " will not "),
    ("can't", " can not "),
    ("shouldn't", " should not "),
    ("wouldn't", " would not "),
    ("couldn't", " could not "),
    ("cannot", " can not "),
    ("n't", " not "),
)
_ANTONYM_PAIRS: tuple[tuple[str, str], ...] = (
    ("prefer", "avoid"),
    ("likes", "hates"),
    ("love", "hate"),
    ("always", "never"),
    ("enable", "disable"),
    ("true", "false"),
    ("yes", "no"),
    ("increase", "decrease"),
    ("allowed", "forbidden"),
    ("required", "optional"),
)
_STOPWORDS: frozenset[str] = frozenset(
    {
        "the",
        "and",
        "for",
        "how",
        "what",
        "that",
        "this",
        "with",
        "are",
        "was",
        "not",
        "can",
        "will",
        "from",
        "have",
        "been",
        "should",
        "would",
        "could",
        "which",
        "when",
        "where",
    }
)

# ---------------------------------------------------------------------------
# TAS constants (mirrors threshold_search/engine.py)
# ---------------------------------------------------------------------------

ALPHA_DEFAULT: float = 0.7  # Threshold-lowering aggressiveness
THETA_FLOOR: float = 5.0  # Absolute minimum effective threshold
THETA_BASE_DEFAULT: float = 30.0  # Default base threshold

COUPLING_MIN_DEFAULT: float = 0.3  # Minimum coupling score to count as "coupled"

W_TAGS: float = 0.50
W_DOMAIN: float = 0.20
W_REGIME: float = 0.15
W_PRESSURE: float = 0.15

AUTO_FIRE_THRESHOLD: float = 85.0
REINF_BASE: float = 2.0


# ---------------------------------------------------------------------------
# Internal result types
# ---------------------------------------------------------------------------


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


# ---------------------------------------------------------------------------
# RetrievalEngine
# ---------------------------------------------------------------------------


class RetrievalEngine:
    """
    Pure PDM Threshold-Adjustment Search (TAS) engine.

    Standalone — no DB access. Accepts List[SignatureRecord], returns
    List[MemoryHit] ranked by (coupling_score × p_effective) descending.

    Usage:
        engine = RetrievalEngine()
        hits = engine.recall(
            records=my_signatures,
            query="how should I format the answer?",
            k=5,
        )
    """

    def __init__(
        self,
        alpha: float = ALPHA_DEFAULT,
        theta_floor: float = THETA_FLOOR,
        coupling_min: float = COUPLING_MIN_DEFAULT,
        auto_fire_threshold: float = AUTO_FIRE_THRESHOLD,
        reinforcement_base: float = REINF_BASE,
    ) -> None:
        self.alpha = max(0.0, min(1.0, alpha))
        self.theta_floor = max(0.0, theta_floor)
        self.coupling_min = max(0.0, min(1.0, coupling_min))
        self.auto_fire_threshold = auto_fire_threshold
        self.reinforcement_base = reinforcement_base

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    def recall(
        self,
        records: list[SignatureRecord],
        query: str | None = None,
        k: int = 5,
        search_cost: float = 0.5,  # 0=TIGHT, 1=LOOSE
        base_threshold: float = THETA_BASE_DEFAULT,
        target_pressure: float = 50.0,
        domain: str | None = None,
        regime: str | None = None,
        diversity_bias: float | None = DEFAULT_DIVERSITY_BIAS,
    ) -> list[MemoryHit]:
        """
        Retrieve top-k memories from a list of SignatureRecords.

        Args:
            records:         All candidate SignatureRecords.
            query:           Natural-language recall query.
            k:               Maximum number of hits to return.
            search_cost:     0.0 = strict, 1.0 = very loose threshold.
            base_threshold:  Starting pressure threshold (default 30).
            target_pressure: Pressure proximity anchor for coupling.
            domain:          Optional domain filter (None = all).
            regime:          Optional regime filter (None = all).
            diversity_bias:  Max fraction of top-k from one drawer
                             (default ``0.4``). Pass ``None`` for pure
                             score order; ``1.0`` disables the cap.

        Returns:
            List[MemoryHit] ranked by relevance, length ≤ k.
        """
        if not records:
            return []

        now = datetime.now(tz=timezone.utc)
        event_window = self._parse_relative_event_window(query, now)

        # Phase 1: threshold lowering
        theta_eff = self._compute_threshold(base_threshold, search_cost)

        coupled: list[tuple[NodeCoupling, MemoryHit]] = []
        damped: list[NodeCoupling] = []

        # Infer domain/regime from query if not specified
        query_tags = self._tokenize_query(query) if query else []
        effective_domain = domain or (infer_domain(query_tags) if query_tags else None)
        effective_regime = regime or (infer_regime(query_tags) if query_tags else None)

        for rec in records:
            # Live pressure: ONE law — domain half-life × (1 - decay_factor).
            # Do NOT mutate p_magnitude here (legacy incremental power-law removed).
            domain_key = rec.domain or infer_domain(rec.intent_tags)
            half_life = resolve_half_life(domain_key)
            days_since_touch = self._days_since(rec.last_retrieved or rec.created_at, now)
            days_since_created = self._days_since(rec.created_at, now)
            decay = calculate_decay_factor(
                days_since_touch,
                half_life,
                days_since_created=days_since_created,
                t_persistence=rec.t_persistence,
            )
            v = calculate_v(rec.validation_prediction_correct, rec.validation_prediction_total)
            i_weight = calculate_intent_weight(rec.intent_tags, query)
            p_eff = calculate_p_effective(rec.p_magnitude, v, decay, i_weight, quality=0.80)
            e_temporal, is_urgent = self._temporal_energy(rec, now)
            # Urgent deadlines get a soft lift through the Phase-1 pressure gate
            p_gate = p_eff * (1.0 + _TEMPORAL_GATE_BOOST * e_temporal)

            # Phase 1 gate
            if p_gate < theta_eff:
                coupling = self._compute_coupling(
                    rec,
                    query_tags,
                    p_eff,
                    rec.p_magnitude,
                    effective_domain,
                    effective_regime,
                    target_pressure,
                )
                damped.append(coupling)
                continue

            # Phase 2: impedance matching
            coupling = self._compute_coupling(
                rec,
                query_tags,
                p_eff,
                rec.p_magnitude,
                effective_domain,
                effective_regime,
                target_pressure,
            )

            if coupling.is_coupled:
                if query_tags and self._semantic_query_overlap(rec, query_tags) < 0.12:
                    damped.append(coupling)
                    continue
                hit = MemoryHit.from_record(
                    rec,
                    p_eff,
                    decay,
                    i_weight,
                    v,
                    coupling_score=coupling.coupling_score,
                    tag_overlap=coupling.tag_overlap,
                    domain_match=coupling.domain_match,
                    regime_match=coupling.regime_match,
                    pressure_proximity=coupling.pressure_proximity,
                    e_temporal=e_temporal,
                    is_urgent=is_urgent,
                )
                coupled.append((coupling, hit))
            else:
                damped.append(coupling)

        # Sort: event-window matches first (when query is time-relative),
        # then coupling × P_eff × (1 + deadline urgency + window boost).
        def _rank_key(item: tuple[NodeCoupling, MemoryHit]) -> tuple[int, float]:
            coupling, hit = item
            in_window = 1 if self._hit_in_event_window(hit, event_window) else 0
            window_boost = _EVENT_WINDOW_RANK_BOOST if in_window else 0.0
            score = (
                coupling.coupling_score
                * hit.p_effective
                * (1.0 + _TEMPORAL_RANK_BOOST * (hit.e_temporal or 0.0) + window_boost)
            )
            return (in_window, score)

        coupled.sort(key=_rank_key, reverse=True)

        # Soft filter: if the query named a window and we have in-window hits,
        # prefer them for top-k; fill remainder from out-of-window by score.
        if event_window is not None:
            in_w = [(c, h) for c, h in coupled if self._hit_in_event_window(h, event_window)]
            out_w = [(c, h) for c, h in coupled if not self._hit_in_event_window(h, event_window)]
            if in_w:
                coupled = in_w + out_w

        return self._select_with_diversity(coupled, k=k, diversity_bias=diversity_bias)

    @staticmethod
    def _parse_relative_event_window(
        query: str | None,
        now: datetime,
    ) -> tuple[datetime, datetime] | None:
        """
        Map temporal phrases in *query* to a half-open UTC window
        ``[start, end)`` for ``t_event_at`` prioritization.

        Recognizes relative cues (today, yesterday, tomorrow, last week,
        this week, last month, last year) and absolute month/year windows
        (``January 2024``, ``Dec 2026``, ``2024-01``).
        """
        if not query or not query.strip():
            return None
        if now.tzinfo is None:
            now = now.replace(tzinfo=timezone.utc)

        text = query.lower()
        day0 = datetime(now.year, now.month, now.day, tzinfo=timezone.utc)

        absolute = RetrievalEngine._parse_absolute_month_year_window(text)
        if absolute is not None:
            return absolute

        # Multi-word phrases first (order matters)
        if re.search(r"\blast\s+year\b", text):
            start = day0 - timedelta(days=365)
            return start, day0 + timedelta(days=1)
        if re.search(r"\blast\s+month\b", text):
            start = day0 - timedelta(days=30)
            return start, day0 + timedelta(days=1)
        if re.search(r"\blast\s+week\b|\bpast\s+week\b", text):
            start = day0 - timedelta(days=7)
            return start, day0 + timedelta(days=1)
        if re.search(r"\bthis\s+week\b", text):
            # Monday-start ISO week through end of today
            start = day0 - timedelta(days=day0.weekday())
            return start, day0 + timedelta(days=1)
        if re.search(r"\byesterday\b", text):
            start = day0 - timedelta(days=1)
            return start, day0
        if re.search(r"\btomorrow\b", text):
            start = day0 + timedelta(days=1)
            return start, day0 + timedelta(days=2)
        if re.search(r"\btoday\b", text):
            return day0, day0 + timedelta(days=1)
        return None

    @staticmethod
    def _parse_absolute_month_year_window(
        text: str,
    ) -> tuple[datetime, datetime] | None:
        """Parse ``January 2024`` / ``Dec 2026`` / ``2024-01`` into ``[start, end)``."""
        months = {
            "jan": 1,
            "january": 1,
            "feb": 2,
            "february": 2,
            "mar": 3,
            "march": 3,
            "apr": 4,
            "april": 4,
            "may": 5,
            "jun": 6,
            "june": 6,
            "jul": 7,
            "july": 7,
            "aug": 8,
            "august": 8,
            "sep": 9,
            "sept": 9,
            "september": 9,
            "oct": 10,
            "october": 10,
            "nov": 11,
            "november": 11,
            "dec": 12,
            "december": 12,
        }
        named = re.search(
            r"\b(" + "|".join(sorted(months, key=len, reverse=True)) + r")\s+(20\d{2})\b",
            text,
        )
        if named is not None:
            month = months[named.group(1)]
            year = int(named.group(2))
            start = datetime(year, month, 1, tzinfo=timezone.utc)
            end_month = month + 1
            end_year = year
            if end_month == 13:
                end_month = 1
                end_year += 1
            return start, datetime(end_year, end_month, 1, tzinfo=timezone.utc)

        iso = re.search(r"\b(20\d{2})-(0[1-9]|1[0-2])\b", text)
        if iso is not None:
            year = int(iso.group(1))
            month = int(iso.group(2))
            start = datetime(year, month, 1, tzinfo=timezone.utc)
            end_month = month + 1
            end_year = year
            if end_month == 13:
                end_month = 1
                end_year += 1
            return start, datetime(end_year, end_month, 1, tzinfo=timezone.utc)
        return None

    @staticmethod
    def _hit_in_event_window(
        hit: MemoryHit,
        window: tuple[datetime, datetime] | None,
    ) -> bool:
        if window is None:
            return False
        event_at = hit.t_event_at
        if event_at is None:
            return False
        if event_at.tzinfo is None:
            event_at = event_at.replace(tzinfo=timezone.utc)
        start, end = window
        return start <= event_at < end

    @staticmethod
    def _select_with_diversity(
        ranked: list[tuple[NodeCoupling, MemoryHit]],
        *,
        k: int,
        diversity_bias: float | None,
    ) -> list[MemoryHit]:
        """
        Cap how many top-k slots one drawer may occupy.

        When ``diversity_bias`` is set and other drawers still have candidates,
        a single drawer cannot take more than ``floor(k * bias)`` slots
        (minimum 1). Remaining slots prefer other drawers by score order.
        If not enough diverse candidates exist, overflow fills by pure score
        so we never return fewer hits than available.
        """
        if k <= 0 or not ranked:
            return []
        if diversity_bias is None:
            return [hit for _, hit in ranked[:k]]

        bias = float(diversity_bias)
        if not math.isfinite(bias):
            raise ValueError("diversity_bias must be a finite float or None")
        bias = max(0.0, min(1.0, bias))
        # bias=1.0 → no effective cap; bias=0.0 → at most 1 per drawer while diversified
        max_per_drawer = k if bias >= 1.0 else max(1, math.floor(k * bias))

        drawers_in_pool = {(hit.drawer or "general") for _, hit in ranked}
        if len(drawers_in_pool) <= 1:
            return [hit for _, hit in ranked[:k]]

        selected: list[MemoryHit] = []
        counts: dict[str, int] = {}
        overflow: list[MemoryHit] = []

        for _, hit in ranked:
            if len(selected) >= k:
                break
            drawer = hit.drawer or "general"
            used = counts.get(drawer, 0)
            if used < max_per_drawer:
                selected.append(hit)
                counts[drawer] = used + 1
            else:
                overflow.append(hit)

        if len(selected) < k:
            for hit in overflow:
                if len(selected) >= k:
                    break
                selected.append(hit)

        return selected

    @staticmethod
    def _temporal_energy(
        rec: SignatureRecord,
        now: datetime,
    ) -> tuple[float, bool]:
        """
        PDM-T urgency for a signature.

        Returns ``(e_temporal, is_urgent)``. No deadline → ``(0.0, False)``.
        Past deadline → expired geometry (E_T = 0).
        """
        deadline = rec.t_deadline
        if deadline is None:
            return 0.0, False
        if deadline.tzinfo is None:
            deadline = deadline.replace(tzinfo=timezone.utc)
        t_remaining = (deadline - now).total_seconds() / 86400.0
        geo = calculate_temporal_geometry(
            c_base=1.0,
            s_base=1.0,
            p_base=max(0.0, float(rec.p_magnitude or 0.0) / 100.0),
            urgency_rate=max(1.0, float(rec.urgency_rate or 2.0)),
            t_remaining_days=t_remaining,
            persist_days=max(1.0, float(rec.t_persistence or 30.0)),
        )
        return float(geo["e_temporal"]), bool(geo["is_urgent"])

    @staticmethod
    def _semantic_query_overlap(rec: SignatureRecord, query_tags: Sequence[str]) -> float:
        """
        Tag or fact-token overlap between query and signature.

        Blocks high-P memories from coupling when tags do not match and the
        fact text shares no meaningful tokens with the query.
        """
        if not query_tags:
            return 1.0
        cue = {t.lower() for t in query_tags if t}
        sig_tags = {t.lower() for t in (rec.intent_tags or []) if t}
        if RetrievalEngine._tags_overlap(cue, sig_tags):
            return 1.0
        fact_tokens = set(RetrievalEngine._tokenize_query(rec.compressed_fact or ""))
        if not fact_tokens:
            return 0.0
        if RetrievalEngine._tags_overlap(cue, fact_tokens):
            return 1.0
        return len(fact_tokens & cue) / max(len(fact_tokens | cue), 1)

    @staticmethod
    def _tags_overlap(cue: set[str], tags: set[str]) -> bool:
        if cue & tags:
            return True
        for q in cue:
            for t in tags:
                if len(q) >= 4 and (q in t or t in q):
                    return True
        return False

    def compute_reinforcement_delta(
        self,
        p_magnitude: float,
        retrieval_count: int,
        coupling_score: float,
    ) -> float:
        """
        Δp = REINF_BASE × (1 + log(1 + retrieval_count)) × coupling_score
        Capped at remaining headroom to P_MAX.
        """
        retrieval_count = max(0, int(retrieval_count or 0))
        log_factor = 1.0 + math.log(1.0 + retrieval_count)
        delta = self.reinforcement_base * log_factor * coupling_score
        headroom = max(0.0, P_MAX - float(p_magnitude))
        return min(delta, headroom)

    # ------------------------------------------------------------------
    # Private helpers
    # ------------------------------------------------------------------

    def _compute_threshold(self, theta_base: float, search_cost: float) -> float:
        cost = max(0.0, min(1.0, search_cost))
        theta_eff = theta_base * (1.0 - self.alpha * cost)
        return max(self.theta_floor, min(theta_base, theta_eff))

    def _compute_coupling(
        self,
        rec: SignatureRecord,
        query_tags: list[str],
        p_eff: float,
        p_raw: float,
        effective_domain: str | None,
        effective_regime: str | None,
        target_pressure: float,
    ) -> NodeCoupling:
        sig_tags = [t.lower() for t in rec.intent_tags]
        cue_tags = [t.lower() for t in query_tags]

        # Tag overlap
        if cue_tags and sig_tags:
            intersection = sum(1 for t in cue_tags if t in sig_tags)
            tag_overlap = intersection / max(len(cue_tags), len(sig_tags))
        elif not cue_tags:
            tag_overlap = 1.0
        else:
            tag_overlap = 0.0

        # Domain match
        if effective_domain is None or (rec.domain or "").lower() == effective_domain.lower():
            domain_match = 1.0
        else:
            domain_match = 0.0

        # Regime match
        sig_regime = infer_regime(sig_tags)
        if effective_regime is None or sig_regime == effective_regime:
            regime_match = 1.0
        elif sig_regime == "neutral" or effective_regime == "neutral":
            regime_match = 0.5
        else:
            regime_match = 0.0

        # Pressure proximity
        pressure_diff = abs(p_raw - target_pressure)
        pressure_proximity = max(0.0, 1.0 - pressure_diff / 100.0)

        coupling_score = (
            W_TAGS * tag_overlap
            + W_DOMAIN * domain_match
            + W_REGIME * regime_match
            + W_PRESSURE * pressure_proximity
        )
        coupling_score = max(0.0, min(1.0, coupling_score))

        return NodeCoupling(
            signature_id=rec.id,
            p_effective=round(p_eff, 3),
            p_magnitude_raw=round(p_raw, 3),
            coupling_score=round(coupling_score, 4),
            is_coupled=coupling_score >= self.coupling_min,
            auto_fire_eligible=p_raw >= self.auto_fire_threshold,
            tag_overlap=round(tag_overlap, 4),
            domain_match=round(domain_match, 4),
            regime_match=round(regime_match, 4),
            pressure_proximity=round(pressure_proximity, 4),
        )

    @staticmethod
    def _days_since(dt: datetime | None, now: datetime) -> float:
        if dt is None:
            return 0.0
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        return max(0.0, (now - dt).total_seconds() / 86400.0)

    @staticmethod
    def _tokenize_query(query: str) -> list[str]:
        """Extract meaningful tokens from a query string for tag matching."""
        words = WORD_PATTERN.findall(query.lower())
        return [w for w in words if w not in _STOPWORDS]

    # ------------------------------------------------------------------
    # Torsion / Reverse Resonance
    # ------------------------------------------------------------------

    def detect_torsion(
        self,
        records: Sequence[SignatureRecord],
        threshold: float = 0.7,
        judge: TorsionJudge | None = None,
    ) -> list[TorsionReport]:
        """
        Find Reverse Resonance pairs: high topic similarity + opposing facts/pressure.

        Clustering (in order):
          1. Explicit ``metadata['cluster_id']`` buckets.
          2. Fallback ``drawer|domain`` buckets for records without cluster_id.
          3. Auto-Discovery: records lacking ``cluster_id`` with
             ``topic_similarity > 0.85`` are unioned into temporary virtual
             clusters (so related sensors/facts compare even across drawers).
          4. Shared-location spatial clusters (e.g. Server Room presence).
          5. Exclusive-slot Entity Exclusion for capacity-1 places.

        Within a bucket, candidates come from a tag inverted index (shared intent
        tags). Small buckets may also compare all pairs. Integrity anchors use a
        deliberate anchors × signals pass across drawers; ordinary records never
        run blind global N².

        Optional ``judge`` callback may flag pairs rules-only detection missed.
        """
        if threshold < 0.0 or threshold > 1.0:
            raise ValueError("threshold must be in [0.0, 1.0]")
        if len(records) < 2:
            return []

        by_id: dict[str, SignatureRecord] = {r.id: r for r in records if r.id}
        clusters = self._build_torsion_clusters(list(by_id.values()))

        reports: list[TorsionReport] = []
        seen_pairs: set[tuple[str, str]] = set()
        reported_pairs: set[tuple[str, str]] = set()

        # Rules are asymmetric: every stewardship/foundational anchor must be
        # checked against signals from every other drawer.
        for rule, signal in self._integrity_candidate_pairs(list(by_id.values())):
            pair_key = (rule.id, signal.id) if rule.id < signal.id else (signal.id, rule.id)
            report = self._score_integrity_violation(
                rule,
                signal,
                occupancy_records=list(by_id.values()),
            )
            if report is None:
                continue
            seen_pairs.add(pair_key)
            if report.torsion_score >= threshold:
                reports.append(report)
                reported_pairs.add(pair_key)

        # Exclusive spatial slots: multiple occupants of a capacity-1 place are
        # Entity Exclusion even when cluster_id was never supplied.
        for report in self._entity_exclusion_reports(list(by_id.values())):
            pair_key = (
                (report.signature_a_id, report.signature_b_id)
                if report.signature_a_id < report.signature_b_id
                else (report.signature_b_id, report.signature_a_id)
            )
            if pair_key in seen_pairs:
                continue
            seen_pairs.add(pair_key)
            if report.torsion_score >= threshold:
                reports.append(report)
                reported_pairs.add(pair_key)

        for cluster_key, group in clusters.items():
            if len(group) < 2:
                continue
            for a, b in self._torsion_candidate_pairs(group):
                pair_key = (a.id, b.id) if a.id < b.id else (b.id, a.id)
                if pair_key in seen_pairs:
                    continue
                seen_pairs.add(pair_key)

                report = self._score_torsion_pair(a, b, cluster_key=cluster_key)
                if report is not None and report.torsion_score >= threshold:
                    reports.append(report)
                    reported_pairs.add(pair_key)

        if judge is not None:
            reports = self._merge_judge_reports(clusters, reports, threshold, judge, reported_pairs)

        reports.sort(key=lambda r: r.torsion_score, reverse=True)
        return reports

    def _integrity_candidate_pairs(
        self,
        records: Sequence[SignatureRecord],
    ) -> Iterator[tuple[SignatureRecord, SignatureRecord]]:
        anchors = [record for record in records if self._is_integrity_anchor(record)]
        signals = [record for record in records if not self._is_integrity_anchor(record)]
        for anchor in anchors:
            for signal in signals:
                yield anchor, signal

    @staticmethod
    def _is_integrity_anchor(record: SignatureRecord) -> bool:
        metadata = record.metadata or {}
        if metadata.get("is_anchor") or metadata.get("role") in {
            "anchor",
            "goal",
            "stewardship",
        }:
            return True
        drawer = (record.drawer_domain or "").strip().lower()
        tags = {tag.lower() for tag in (record.intent_tags or []) if tag}
        return drawer in _INTEGRITY_DRAWERS or bool(tags & _INTEGRITY_TAGS)

    def _score_integrity_violation(
        self,
        rule: SignatureRecord,
        signal: SignatureRecord,
        *,
        occupancy_records: Sequence[SignatureRecord] | None = None,
    ) -> TorsionReport | None:
        violation = detect_constraint_violation(
            rule,
            signal.compressed_fact or "",
            candidate_tags=signal.intent_tags or [],
            occupancy_records=occupancy_records or (),
        )
        if violation is None:
            return None

        rule_text = (rule.compressed_fact or "")[:500]
        signal_text = (signal.compressed_fact or "")[:500]
        explanation = (
            f"Integrity Violation: {violation.explanation} "
            f"Rule: '{self._fact_preview(rule_text)}'. "
            f"Signal: '{self._fact_preview(signal_text)}'."
        )
        kind = "integrity_violation" if violation.kind != "entity_exclusion" else "entity_exclusion"
        return TorsionReport(
            signature_a_id=rule.id,
            signature_b_id=signal.id,
            signature_a_text=rule_text,
            signature_b_text=signal_text,
            drawer=rule.drawer_domain or "stewardship",
            domain=rule.domain or signal.domain or "structural",
            torsion_score=round(violation.strength, 4),
            topic_similarity=round(violation.topic_similarity, 4),
            contradiction_strength=round(violation.strength, 4),
            explanation=explanation,
            conflict_kind=kind,
            cluster_key=f"integrity:{rule.id[:8]}",
        )

    def _entity_exclusion_reports(
        self,
        records: Sequence[SignatureRecord],
    ) -> list[TorsionReport]:
        """Pairwise Entity Exclusion for exclusive spatial slots."""
        reports: list[TorsionReport] = []
        by_id = {record.id: record for record in records if record.id}
        for rule in records:
            slot = parse_exclusive_slot(rule.compressed_fact or "")
            if slot is None:
                continue
            occupants = collect_occupants(records, location=slot.location)
            if len(occupants) <= slot.capacity:
                continue
            for i, left in enumerate(occupants):
                for right in occupants[i + 1 :]:
                    violation = entity_exclusion_pair(left, right, slot=slot)
                    if violation is None:
                        continue
                    left_rec = by_id.get(left.source_id or "")
                    right_rec = by_id.get(right.source_id or "")
                    if left_rec is None or right_rec is None:
                        continue
                    reports.append(
                        TorsionReport(
                            signature_a_id=left_rec.id,
                            signature_b_id=right_rec.id,
                            signature_a_text=(left_rec.compressed_fact or "")[:500],
                            signature_b_text=(right_rec.compressed_fact or "")[:500],
                            drawer=left_rec.drawer_domain or right_rec.drawer_domain or "general",
                            domain=left_rec.domain or right_rec.domain or "insight",
                            torsion_score=1.0,
                            topic_similarity=1.0,
                            contradiction_strength=1.0,
                            explanation=(
                                f"{violation.explanation} "
                                f"Rule: '{self._fact_preview(rule.compressed_fact)}'."
                            ),
                            conflict_kind="entity_exclusion",
                            cluster_key=f"slot:{slot.location}",
                        )
                    )
        return reports

    def _build_torsion_clusters(
        self,
        records: Sequence[SignatureRecord],
    ) -> dict[str, list[SignatureRecord]]:
        """
        Build torsion comparison buckets.

        Explicit ``cluster_id`` wins. Unclustered records get ``drawer|domain``
        buckets PLUS auto-discovered virtual clusters when resonance > 0.85,
        PLUS shared-location spatial clusters (Server Room occupancy, etc.).
        """
        clusters: dict[str, list[SignatureRecord]] = {}
        unclustered: list[SignatureRecord] = []

        for rec in records:
            meta = rec.metadata or {}
            cluster_id = meta.get("cluster_id")
            if cluster_id is not None and str(cluster_id).strip():
                key = f"cluster:{str(cluster_id).strip()}"
                clusters.setdefault(key, []).append(rec)
            else:
                unclustered.append(rec)
                # Keep legacy coarse bucket so mid-resonance same-drawer pairs still compare
                coarse = self._torsion_drawer_domain_key(rec)
                clusters.setdefault(coarse, []).append(rec)

        for key, group in self._auto_discover_resonance_clusters(unclustered).items():
            clusters[key] = group

        for key, group in self._auto_discover_location_clusters(unclustered).items():
            clusters[key] = group

        return clusters

    def _auto_discover_location_clusters(
        self,
        records: Sequence[SignatureRecord],
    ) -> dict[str, list[SignatureRecord]]:
        """Group presence facts that share the same place (no cluster_id needed)."""
        by_location: dict[str, list[SignatureRecord]] = {}
        for record in records:
            presence = parse_presence(record.compressed_fact or "", source_id=record.id)
            if presence is None:
                continue
            by_location.setdefault(presence.location, []).append(record)
        return {
            f"slot:{location}": members
            for location, members in by_location.items()
            if len(members) >= 2
        }

    def _auto_discover_resonance_clusters(
        self,
        records: Sequence[SignatureRecord],
        *,
        min_resonance: float = _AUTO_CLUSTER_RESONANCE,
    ) -> dict[str, list[SignatureRecord]]:
        """
        Union-Find virtual clusters for records with topic_similarity > min_resonance.

        Candidate edges come from the same surgical tag/token index used for
        torsion pairs — never blind global N².
        """
        if len(records) < 2:
            return {}

        by_id = {r.id: r for r in records if r.id}
        parent: dict[str, str] = {rid: rid for rid in by_id}

        def find(x: str) -> str:
            while parent[x] != x:
                parent[x] = parent[parent[x]]
                x = parent[x]
            return x

        def union(a: str, b: str) -> None:
            ra, rb = find(a), find(b)
            if ra != rb:
                parent[rb] = ra

        for a, b in self._torsion_candidate_pairs(list(by_id.values())):
            if self._topic_similarity(a, b) > min_resonance:
                union(a.id, b.id)

        groups: dict[str, list[SignatureRecord]] = {}
        for rid, rec in by_id.items():
            groups.setdefault(find(rid), []).append(rec)

        out: dict[str, list[SignatureRecord]] = {}
        for idx, (root, members) in enumerate(groups.items()):
            if len(members) < 2:
                continue
            out[f"auto:{idx}:{root[:8]}"] = members
        return out

    def _merge_judge_reports(
        self,
        clusters: dict[str, list[SignatureRecord]],
        reports: list[TorsionReport],
        threshold: float,
        judge: TorsionJudge,
        reported_pairs: set[tuple[str, str]],
    ) -> list[TorsionReport]:
        """Append judge-flagged pairs not already reported by rules-only detection."""
        merged = list(reports)
        seen_pairs: set[tuple[str, str]] = set()
        for group in clusters.values():
            if len(group) < 2:
                continue
            for a, b in self._torsion_candidate_pairs(group):
                pair_key = (a.id, b.id) if a.id < b.id else (b.id, a.id)
                if pair_key in seen_pairs or pair_key in reported_pairs:
                    continue
                seen_pairs.add(pair_key)
                try:
                    judged = judge(a, b)
                except Exception as exc:
                    logger.warning("[PDM] torsion_judge failed for pair: %s", exc)
                    continue
                if judged is None or judged.torsion_score < threshold:
                    continue
                reported_pairs.add(pair_key)
                merged.append(judged)
        merged.sort(key=lambda r: r.torsion_score, reverse=True)
        return merged

    def _torsion_candidate_pairs(
        self,
        group: Sequence[SignatureRecord],
    ) -> Iterable[tuple[SignatureRecord, SignatureRecord]]:
        """Yield unordered unique pairs without full N² when the cluster is large."""
        by_id = {r.id: r for r in group}
        yielded: set[tuple[str, str]] = set()

        def emit(id_a: str, id_b: str) -> Iterable[tuple[SignatureRecord, SignatureRecord]]:
            if id_a == id_b:
                return
            key = (id_a, id_b) if id_a < id_b else (id_b, id_a)
            if key in yielded:
                return
            yielded.add(key)
            yield (by_id[key[0]], by_id[key[1]])

        tag_index: dict[str, list[str]] = {}
        for rec in group:
            for tag in {t.lower() for t in (rec.intent_tags or []) if t}:
                tag_index.setdefault(tag, []).append(rec.id)

        for ids in tag_index.values():
            if len(ids) < 2:
                continue
            for i in range(len(ids)):
                for j in range(i + 1, len(ids)):
                    yield from emit(ids[i], ids[j])

        # Small clusters: also compare pairs with token overlap (no shared tags yet)
        if len(group) <= _SMALL_CLUSTER:
            token_cache = {r.id: set(self._tokenize_query(r.compressed_fact or "")) for r in group}
            ids = [r.id for r in group]
            for i in range(len(ids)):
                for j in range(i + 1, len(ids)):
                    ta, tb = token_cache[ids[i]], token_cache[ids[j]]
                    if not ta or not tb:
                        continue
                    overlap = len(ta & tb) / max(len(ta), len(tb))
                    if overlap >= 0.25:
                        yield from emit(ids[i], ids[j])

    def _score_torsion_pair(
        self,
        a: SignatureRecord,
        b: SignatureRecord,
        *,
        cluster_key: str,
    ) -> TorsionReport | None:
        topic = self._topic_similarity(a, b)
        same_drawer = self._same_drawer(a, b)
        topic_gate = _SAME_DRAWER_TOPIC_GATE if same_drawer else _TOPIC_GATE
        if topic < topic_gate:
            return None

        kind, strength, detail = self._contradiction_signals(a, b, topic)
        if strength <= 0.0:
            return None

        score = round(max(0.0, min(1.0, topic * strength)), 4)
        # A structured date disagreement or high-overlap attribute clash in the
        # same drawer is stronger evidence than lexical topic similarity alone.
        # Keep these visible at the default detect_torsion(threshold=0.7).
        if same_drawer and kind == "deadline":
            score = max(score, round(min(1.0, 0.85 + 0.10 * strength), 4))
        elif same_drawer and kind == "attribute_clash":
            score = max(score, round(min(0.95, 0.75 + 0.20 * strength), 4))
        explanation = self._humanize_torsion(a, b, kind=kind, detail=detail)
        return TorsionReport(
            signature_a_id=a.id,
            signature_b_id=b.id,
            signature_a_text=(a.compressed_fact or "")[:500],
            signature_b_text=(b.compressed_fact or "")[:500],
            drawer=(a.drawer_domain or b.drawer_domain or "general"),
            domain=(a.domain or b.domain or "insight"),
            torsion_score=score,
            topic_similarity=round(topic, 4),
            contradiction_strength=round(strength, 4),
            explanation=explanation,
            conflict_kind=kind,
            cluster_key=cluster_key,
        )

    def _topic_similarity(self, a: SignatureRecord, b: SignatureRecord) -> float:
        """Blend tag Jaccard, token Jaccard, and TAS tag-overlap coupling."""
        tags_a = {t.lower() for t in (a.intent_tags or []) if t}
        tags_b = {t.lower() for t in (b.intent_tags or []) if t}
        if tags_a or tags_b:
            tag_j = len(tags_a & tags_b) / max(len(tags_a | tags_b), 1)
        else:
            tag_j = 0.0

        tok_a = set(self._tokenize_query(a.compressed_fact or ""))
        tok_b = set(self._tokenize_query(b.compressed_fact or ""))
        if tok_a and tok_b:
            tok_j = len(tok_a & tok_b) / max(len(tok_a | tok_b), 1)
        else:
            tok_j = 0.0

        # Reuse TAS tag component: treat A's tags (else tokens) as the "query"
        query_tags = list(tags_a) if tags_a else list(tok_a)
        if query_tags:
            coupling = self._compute_coupling(
                b,
                query_tags=query_tags,
                p_eff=float(b.p_magnitude or 0.0),
                p_raw=float(b.p_magnitude or 0.0),
                effective_domain=(a.domain or None),
                effective_regime=infer_regime(list(tags_a)) if tags_a else None,
                target_pressure=float(a.p_magnitude or 50.0),
            )
            coupling_tag = coupling.tag_overlap
        else:
            coupling_tag = 0.0
        return max(0.0, min(1.0, 0.45 * tag_j + 0.35 * tok_j + 0.20 * coupling_tag))

    def _contradiction_signals(
        self,
        a: SignatureRecord,
        b: SignatureRecord,
        topic: float,
    ) -> tuple[str, float, str]:
        """Return (kind, strength, detail). Strength in [0, 1]."""
        # Prefer structured deadline over numeric bleed from the same dates in text
        if a.t_deadline is not None and b.t_deadline is not None:
            da = a.t_deadline if a.t_deadline.tzinfo else a.t_deadline.replace(tzinfo=timezone.utc)
            db = b.t_deadline if b.t_deadline.tzinfo else b.t_deadline.replace(tzinfo=timezone.utc)
            delta_days = abs((da - db).total_seconds()) / 86400.0
            if delta_days >= 1.0:
                strength = min(1.0, 0.7 + delta_days / 20.0)
                return (
                    "deadline",
                    strength,
                    f"{da.date().isoformat()} vs {db.date().isoformat()}",
                )

        best_kind = "semantic"
        best_strength = 0.0
        best_detail = ""

        # Numeric disagreement in otherwise similar facts
        nums_a = self._standalone_numbers(a.compressed_fact or "")
        nums_b = self._standalone_numbers(b.compressed_fact or "")
        if nums_a and nums_b and nums_a != nums_b and topic >= 0.4:
            strength = min(1.0, 0.5 + 0.5 * topic)
            detail = f"{self._format_numbers(nums_a)} vs {self._format_numbers(nums_b)}"
            if strength > best_strength:
                best_kind, best_strength, best_detail = "factual", strength, detail

        attribute = self._attribute_clash(a, b)
        if attribute is not None:
            strength, detail = attribute
            if strength > best_strength:
                best_kind, best_strength, best_detail = (
                    "attribute_clash",
                    strength,
                    detail,
                )

        # Negation / antonym polarity on shared content
        norm_a = self._normalize_polarity_text(a.compressed_fact or "")
        norm_b = self._normalize_polarity_text(b.compressed_fact or "")
        tok_a = set(self._tokenize_query(norm_a))
        tok_b = set(self._tokenize_query(norm_b))
        content_a = tok_a - _NEGATION_TOKENS
        content_b = tok_b - _NEGATION_TOKENS
        content_overlap = (
            len(content_a & content_b) / max(len(content_a | content_b), 1)
            if content_a or content_b
            else 0.0
        )
        neg_a = self._has_negation(norm_a)
        neg_b = self._has_negation(norm_b)
        if neg_a != neg_b and content_overlap >= 0.28:
            strength = min(1.0, 0.45 + 0.55 * content_overlap)
            if strength > best_strength:
                best_kind, best_strength, best_detail = (
                    "polarity",
                    strength,
                    "one affirms, the other negates the shared topic",
                )

        text_blob_a = f"{' '.join(a.intent_tags or [])} {norm_a}".lower()
        text_blob_b = f"{' '.join(b.intent_tags or [])} {norm_b}".lower()
        for left, right in _ANTONYM_PAIRS:
            a_has_l, a_has_r = left in text_blob_a, right in text_blob_a
            b_has_l, b_has_r = left in text_blob_b, right in text_blob_b
            crossed = (a_has_l and b_has_r) or (a_has_r and b_has_l)
            if crossed and topic >= 0.35:
                strength = min(1.0, 0.6 + 0.4 * topic)
                if strength > best_strength:
                    best_kind, best_strength, best_detail = (
                        "polarity",
                        strength,
                        f"opposing cues '{left}' / '{right}'",
                    )

        # Opposing pressure vectors (weaker; needs solid topic match)
        p_delta = abs(float(a.p_magnitude or 0.0) - float(b.p_magnitude or 0.0))
        if topic >= 0.55 and p_delta >= 40.0:
            strength = min(0.85, (p_delta / 100.0) * topic)
            if strength > best_strength:
                best_kind, best_strength, best_detail = (
                    "pressure",
                    strength,
                    f"P={a.p_magnitude:.0f} vs P={b.p_magnitude:.0f}",
                )

        return best_kind, best_strength, best_detail

    def _attribute_clash(
        self,
        a: SignatureRecord,
        b: SignatureRecord,
    ) -> tuple[float, str] | None:
        """
        Detect different attribute values for the same entity/topic.

        This is deliberately scoped to the same drawer and requires at least
        80% tag-overlap after removing role tags and mutable weekday/status
        values. It catches "Release Friday" vs "Release Saturday" without
        pretending Friday/Saturday are linguistic antonyms.
        """
        if not self._same_drawer(a, b):
            return None

        tags_a = self._attribute_identity_tags(a)
        tags_b = self._attribute_identity_tags(b)
        if not tags_a or not tags_b:
            return None
        overlap = len(tags_a & tags_b) / max(min(len(tags_a), len(tags_b)), 1)
        if overlap < _ATTRIBUTE_TAG_OVERLAP:
            return None

        tokens_a = set(self._tokenize_query(a.compressed_fact or ""))
        tokens_b = set(self._tokenize_query(b.compressed_fact or ""))
        values_a = tokens_a & (_TEMPORAL_ATTRIBUTE_VALUES | _STATUS_ATTRIBUTE_VALUES)
        values_b = tokens_b & (_TEMPORAL_ATTRIBUTE_VALUES | _STATUS_ATTRIBUTE_VALUES)
        categorical_diff = values_a != values_b and bool(values_a or values_b)

        tail_a = self._trailing_attribute(a.compressed_fact or "")
        tail_b = self._trailing_attribute(b.compressed_fact or "")
        trailing_diff = bool(tail_a and tail_b and tail_a != tail_b)
        has_attribute_context = bool((tags_a | tags_b) & _ATTRIBUTE_HINT_TAGS)
        # Arbitrary different nouns are not automatically conflicting attributes
        # ("football" vs "thing" belongs to an optional semantic judge).
        if trailing_diff and not categorical_diff and not has_attribute_context:
            trailing_diff = False
        if not categorical_diff and not trailing_diff:
            return None

        strength = 0.85
        if categorical_diff:
            strength = 1.0
        detail_values_a = sorted(values_a) or ([tail_a] if tail_a else [])
        detail_values_b = sorted(values_b) or ([tail_b] if tail_b else [])
        detail = (
            f"shared tags={overlap:.0%}; "
            f"attribute values {detail_values_a or ['unknown']} "
            f"vs {detail_values_b or ['unknown']}"
        )
        return strength, detail

    @staticmethod
    def _same_drawer(a: SignatureRecord, b: SignatureRecord) -> bool:
        drawer_a = (a.drawer_domain or "general").strip().lower() or "general"
        drawer_b = (b.drawer_domain or "general").strip().lower() or "general"
        return drawer_a == drawer_b

    @staticmethod
    def _attribute_identity_tags(rec: SignatureRecord) -> set[str]:
        return {
            tag.lower().strip()
            for tag in (rec.intent_tags or [])
            if tag
            and tag.lower().strip() not in _ATTRIBUTE_ROLE_TAGS
            and tag.lower().strip() not in _TEMPORAL_ATTRIBUTE_VALUES
            and tag.lower().strip() not in _STATUS_ATTRIBUTE_VALUES
        }

    def _trailing_attribute(self, text: str) -> str | None:
        tokens = self._tokenize_query(text)
        return tokens[-1] if tokens else None

    @staticmethod
    def _torsion_drawer_domain_key(rec: SignatureRecord) -> str:
        drawer = (rec.drawer_domain or "general").strip().lower() or "general"
        domain = (rec.domain or "insight").strip().lower() or "insight"
        return f"{drawer}|{domain}"

    @staticmethod
    def _torsion_cluster_key(rec: SignatureRecord) -> str:
        """Legacy single-key helper (explicit cluster_id or drawer|domain)."""
        meta = rec.metadata or {}
        cluster_id = meta.get("cluster_id")
        if cluster_id is not None and str(cluster_id).strip():
            return f"cluster:{str(cluster_id).strip()}"
        return RetrievalEngine._torsion_drawer_domain_key(rec)

    @staticmethod
    def _humanize_torsion(
        a: SignatureRecord,
        b: SignatureRecord,
        *,
        kind: str,
        detail: str,
    ) -> str:
        """Plain-English conflict line (Morning Brief style, English-only)."""
        a_snip = RetrievalEngine._fact_preview(a.compressed_fact)
        b_snip = RetrievalEngine._fact_preview(b.compressed_fact)
        base = f"Conflict found between Signature A ({a_snip}) and Signature B ({b_snip})"
        match kind:
            case "deadline":
                return f"{base}: deadlines disagree ({detail})."
            case "factual":
                return f"{base}: conflicting numeric/factual claims ({detail})."
            case "attribute_clash":
                return f"{base}: potential entity-attribute clash ({detail})."
            case "entity_exclusion":
                return f"{base}: exclusive-slot occupancy clash ({detail})."
            case "polarity":
                return f"{base}: opposing polarity on the same topic ({detail})."
            case "pressure":
                return f"{base}: opposing pressure vectors ({detail})."
            case _:
                return f"{base}: reverse resonance on a shared topic."

    @staticmethod
    def _normalize_polarity_text(text: str) -> str:
        """Expand contractions / slang so negation tokens become detectable."""
        t = (text or "").lower()
        for src, dst in _CONTRACTION_MAP:
            t = t.replace(src, dst)
        # Common no-apostrophe typos after apostrophe expansion
        for src, dst in (
            (" dont ", " do not "),
            (" doesnt ", " does not "),
            (" didnt ", " did not "),
            (" isnt ", " is not "),
            (" arent ", " are not "),
            (" wasnt ", " was not "),
            (" werent ", " were not "),
            (" wont ", " will not "),
            (" cant ", " can not "),
        ):
            t = t.replace(src, dst)
        # Leading typo without spaces: "i dont love" already spaced by lower()
        if t.startswith("dont "):
            t = "do not " + t[5:]
        return t

    @staticmethod
    def _has_negation(text: str) -> bool:
        """True if text contains an English negation cue after normalization."""
        norm = RetrievalEngine._normalize_polarity_text(text)
        tokens = set(WORD_PATTERN.findall(norm))
        # Also catch leftover no-apostrophe forms as whole tokens
        return bool(tokens & _NEGATION_TOKENS)

    @staticmethod
    def _standalone_numbers(text: str) -> set[str]:
        """Extract numeric tokens that are not glued to letters (skip Q3, v2)."""
        return set(NUMBER_PATTERN.findall(text))

    @staticmethod
    def _format_numbers(nums: set[str], limit: int = 3) -> str:
        ordered = sorted(nums, key=lambda n: (len(n), n))[:limit]
        return ", ".join(ordered)

    @staticmethod
    def _fact_preview(text: str | None, max_len: int = 72) -> str:
        cleaned = " ".join((text or "").split())
        if not cleaned:
            return "empty"
        if len(cleaned) <= max_len:
            return cleaned
        return cleaned[: max_len - 1].rstrip() + "…"

    # ------------------------------------------------------------------
    # Goal-Anchor Alignment (GAA)
    # ------------------------------------------------------------------

    def verify_alignment(
        self,
        records: list[SignatureRecord],
        intent_text: str,
        *,
        min_pressure: float = 60.0,
        k_goals: int = 8,
        torsion_threshold: float = 0.70,
        conflict_threshold: float = 0.40,
    ) -> AlignmentReport:
        """
        High-pressure goal-anchor check for a proposed intent.

        Pulls stewardship/foundational Goal Signatures (high IAW), then
        scores Resonance (alignment) vs Torsion (deviation).
        """
        from pdm_memory.core.alignment import verify_alignment as run_gaa

        return run_gaa(
            self,
            records,
            intent_text,
            min_pressure=min_pressure,
            k_goals=k_goals,
            torsion_threshold=torsion_threshold,
            conflict_threshold=conflict_threshold,
        )
