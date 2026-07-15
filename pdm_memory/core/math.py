"""
PDM Core Math — Pure Python, zero external dependencies.

All formulas are ported faithfully from companion_api/pdm/kernel.py
and companion_api/pdm/models.py.  No Django, no ORM, no Celery.

Formula reference (ONE canonical decay law):
  effective_spike = min(100, P_magnitude × (t_persistence/30) × phase_privilege)
  decay_factor    = 1 - exp(-λ × t)     where λ = ln2 / half_life
  V               = (correct + 1) / (total + 2)   [Laplace smoothing]
  P_effective     = P × V × (1 - decay_factor) × intent_weight × quality × comparator

  Grace: if days_since_created ≤ t_persistence → decay_factor = 0.
  Surviving fraction = (1 - decay_factor) = exp(-λ × t).

  Legacy power-law (p × decay_rate^days) is REMOVED — it double-penalized recall.
"""

from __future__ import annotations

import math
import warnings
from typing import Dict, List, Optional


# ---------------------------------------------------------------------------
# Domain half-lives (ported from kernel.py DOMAIN_HALF_LIVES)
# ---------------------------------------------------------------------------

DOMAIN_HALF_LIVES: Dict[str, float] = {
    "market_signal": 1.0,    # 1 day
    "pattern": 14.0,         # 2 weeks
    "structural": 90.0,      # 3 months
    "core_fact": 365.0,      # 1 year
    "reminder": 7.0,         # 1 week
    "insight": 30.0,         # 1 month
    "warning": 3.0,          # 3 days
}

DEFAULT_HALF_LIFE: float = 30.0

# Pressure constants
P_MAX: float = 100.0
P_FLOOR: float = 0.0

# Decay trigger — signatures below this live P_eff are eligible for deletion
DECAY_DELETE_THRESHOLD: float = 30.0

# Kept for SignatureRecord/schema backward-compat only — NOT used by pressure decay.
DEFAULT_DECAY_RATE: float = 0.9


# ---------------------------------------------------------------------------
# Task 1.3: Core formulas (Django-free)
# ---------------------------------------------------------------------------


def calculate_effective_spike(
    p_magnitude: float,
    t_persistence: float,
    phase_privilege: float = 1.0,
) -> float:
    """
    Calculate effective_spike = p_magnitude × (t_persistence / 30) × phase_privilege.
    Capped at 100.

    Args:
        p_magnitude:    Raw pressure (0–100).
        t_persistence:  How many days the memory stays relevant.
        phase_privilege: Nesting/context multiplier, typically 1.0.

    Returns:
        effective_spike in [0, 100].
    """
    raw = p_magnitude * (t_persistence / 30.0) * phase_privilege
    return min(P_MAX, max(P_FLOOR, raw))


def resolve_half_life(domain: Optional[str]) -> float:
    """Map knowledge domain → half-life days (canonical decay clock)."""
    if not domain:
        return DEFAULT_HALF_LIFE
    return DOMAIN_HALF_LIVES.get(domain, DEFAULT_HALF_LIFE)


def calculate_decay_factor(
    days_since_retrieved: float,
    half_life: float = DEFAULT_HALF_LIFE,
    *,
    days_since_created: Optional[float] = None,
    t_persistence: float = 0.0,
) -> float:
    """
    Canonical exponential decay factor based on time since last retrieval.

    decay_factor = 1 - exp(-λ × t)   where λ = ln2 / half_life

    Grace window: if ``days_since_created`` is provided and
    ``days_since_created <= t_persistence``, returns 0.0 (no decay yet).

    A decay_factor close to 0 means the memory is fresh (little decay).
    A decay_factor close to 1 means the memory is very stale.
    """
    if days_since_created is not None and days_since_created <= max(0.0, t_persistence):
        return 0.0

    days = max(0.0, float(days_since_retrieved))
    half_life = max(0.1, half_life)
    lam = math.log(2) / half_life
    return 1.0 - math.exp(-lam * days)


def calculate_v(correct: int, total: int) -> float:
    """
    Validation Coefficient with Laplace smoothing.
    V = (correct + 1) / (total + 2)

    Range: 0.33 (no history) → approaching 1.0 (perfect accuracy).

    Args:
        correct: Number of correct predictions.
        total:   Total predictions made.

    Returns:
        V in [0.33, 1.0].
    """
    return round((correct + 1) / (total + 2), 6)


def calculate_intent_weight(
    intent_tags: List[str],
    query: Optional[str] = None,
) -> float:
    """
    Intent match weight: 0.8 base + 0.2 boost proportional to tag-query overlap.

    Args:
        intent_tags: Tags associated with the memory.
        query:       The recall query string.

    Returns:
        Weight in [0.8, 1.0] — 1.0 when query is absent or all tags match.
    """
    if not query or not intent_tags:
        return 1.0
    query_lower = query.lower()
    matches = sum(1 for tag in intent_tags if tag.lower() in query_lower)
    return round(0.8 + 0.2 * (matches / len(intent_tags)), 6)


def calculate_p_effective(
    p_magnitude: float,
    v: float = 0.75,
    decay_factor: float = 0.0,
    intent_weight: float = 1.0,
    quality: float = 0.80,
    comparator: float = 1.0,
) -> float:
    """
    P_effective — the live pressure value used for retrieval ranking.

    P_eff = P_magnitude × V × (1 - decay_factor) × intent_weight × quality × comparator

    Args:
        p_magnitude:  Raw stored pressure (0–100).
        v:            Validation coefficient (Laplace, 0.33–1.0).
        decay_factor: Exponential decay (0=fresh, 1=fully decayed).
        intent_weight: Tag-query match weight (0.8–1.0).
        quality:      Signal quality score (0–1), default 0.80.
        comparator:   Optional UCA-style proxy multiplier (0.85–1.15).

    Returns:
        P_effective in [0, 100].
    """
    p_eff = (
        p_magnitude
        * v
        * (1.0 - decay_factor)
        * intent_weight
        * quality
        * comparator
    )
    return round(min(P_MAX, max(P_FLOOR, p_eff)), 6)


# ---------------------------------------------------------------------------
# Maintenance projection of stored pressure (same half-life law as P_effective)
# ---------------------------------------------------------------------------


def calculate_half_life_pressure(
    p_magnitude: float,
    days_since_retrieved: float,
    half_life: float,
    t_persistence: float,
    phase_privilege: float = 1.0,
    *,
    days_since_created: Optional[float] = None,
) -> tuple[float, float]:
    """
    Project stored pressure after canonical half-life decay.

    Uses the SAME law as live scoring:
        new_p = p_magnitude × (1 - decay_factor) = p_magnitude × exp(-λ × t)

    Prefer deleting by live ``P_effective`` over rewriting ``p_magnitude``.
    This helper exists for maintenance tooling / tests that need a projected P.

    Returns:
        Tuple (projected_p_magnitude, new_effective_spike).
    """
    created_days = (
        days_since_created
        if days_since_created is not None
        else days_since_retrieved
    )
    decay = calculate_decay_factor(
        days_since_retrieved,
        half_life,
        days_since_created=created_days,
        t_persistence=t_persistence,
    )
    new_p = max(P_FLOOR, min(P_MAX, p_magnitude * (1.0 - decay)))
    new_spike = calculate_effective_spike(new_p, t_persistence, phase_privilege)
    return round(new_p, 6), round(new_spike, 6)


def calculate_incremental_decay(
    p_magnitude: float,
    days_elapsed: float,
    t_persistence: float,
    phase_privilege: float = 1.0,
    decay_per_day: float = DEFAULT_DECAY_RATE,
    half_life: float = DEFAULT_HALF_LIFE,
) -> tuple[float, float]:
    """
    DEPRECATED alias for :func:`calculate_half_life_pressure`.

    The old power-law ``p × decay_per_day^days`` double-counted with live
    half-life scoring and is no longer applied. ``decay_per_day`` is ignored.
    """
    warnings.warn(
        "calculate_incremental_decay is deprecated; use calculate_half_life_pressure "
        "(canonical domain half-life). decay_per_day is ignored.",
        DeprecationWarning,
        stacklevel=2,
    )
    return calculate_half_life_pressure(
        p_magnitude=p_magnitude,
        days_since_retrieved=max(0.0, days_elapsed - max(0.0, t_persistence)),
        half_life=half_life,
        t_persistence=t_persistence,
        phase_privilege=phase_privilege,
        days_since_created=days_elapsed,
    )


# ---------------------------------------------------------------------------
# Task 1.3: Temporal geometry (PDM-T), ported from models.py
# ---------------------------------------------------------------------------


def calculate_temporal_geometry(
    c_base: float,
    s_base: float,
    p_base: float,          # p_magnitude / 100.0
    urgency_rate: float,
    t_remaining_days: float,
    persist_days: float,
    decay_rate: float = 0.05,
    temporal_weight: float = 1.0,
) -> dict:
    """
    PDM-T Temporal Deformation Geometry.

    Pre-deadline:
        C_T = c_base × (1 + persist / T_remaining)
        S_T = s_base / (1 + urgency_rate × (1 - T_remaining / persist))
        P_T = p_base × urgency_rate^(1 / T_remaining_days)
        E_T = C_T × transmission × P_T   (normalized via asymptotic scaling)

    Post-deadline:
        E_T = 0, weight decays by decay_rate per day past deadline.

    Args:
        c_base:          Initial curvature (0–1).
        s_base:          Initial membrane thickness (0–1).
        p_base:          Normalized pressure (p_magnitude / 100).
        urgency_rate:    Pressure multiplier ramp (1–10).
        t_remaining_days: Days to deadline (negative = past deadline).
        persist_days:    t_persistence value.
        decay_rate:      Post-deadline fade rate per day.
        temporal_weight: Current weight (decays post-deadline).

    Returns:
        dict with keys: c_temporal, s_temporal, p_temporal, e_temporal,
                        is_urgent, temporal_weight, status.
    """
    if t_remaining_days <= 0:
        days_past = abs(t_remaining_days)
        new_weight = temporal_weight * (1 - decay_rate) ** days_past
        return {
            "c_temporal": 0.0,
            "s_temporal": 1.0,
            "p_temporal": 0.0,
            "e_temporal": 0.0,
            "is_urgent": False,
            "temporal_weight": max(0.0, new_weight),
            "status": "EXPIRED",
        }

    t_rem = max(0.1, t_remaining_days)
    persist = max(1.0, persist_days)

    c_t = min(5.0, c_base * (1 + persist / t_rem))
    time_factor = max(0.0, min(1.0, 1 - (t_rem / persist)))
    s_t = max(0.01, s_base / (1 + urgency_rate * time_factor))
    p_t = min(10.0, p_base * (urgency_rate ** (1 / t_rem)))

    transmission = 1.0 / s_t
    raw_e_t = c_t * transmission * p_t
    e_t = min(1.0, max(0.0, raw_e_t / (raw_e_t + 10.0)))

    return {
        "c_temporal": round(c_t, 4),
        "s_temporal": round(s_t, 4),
        "p_temporal": round(p_t, 4),
        "e_temporal": round(e_t, 4),
        "is_urgent": e_t > 0.75,
        "temporal_weight": temporal_weight,
        "status": "URGENT" if e_t > 0.75 else "ACTIVE",
    }


# ---------------------------------------------------------------------------
# Domain / regime inference helpers (ported from kernel.py + TAS engine)
# ---------------------------------------------------------------------------


def infer_domain(tags: List[str]) -> str:
    """Infer memory domain from intent tags."""
    if not tags:
        return "insight"
    tag_str = " ".join(tags).lower()
    if any(k in tag_str for k in ["market", "signal", "price", "trade", "stock"]):
        return "market_signal"
    if any(k in tag_str for k in ["pattern", "historical", "analogue"]):
        return "pattern"
    if any(k in tag_str for k in ["structure", "structural", "model"]):
        return "structural"
    if any(k in tag_str for k in ["remind", "deadline", "due", "by "]):
        return "reminder"
    if any(k in tag_str for k in ["warning", "risk", "danger"]):
        return "warning"
    if any(k in tag_str for k in ["fact", "law", "rule", "principle"]):
        return "core_fact"
    return "insight"


def infer_regime(tags: List[str]) -> str:
    """Infer question regime from intent tags."""
    tag_str = " ".join(tags).lower()
    if any(k in tag_str for k in ["trade", "stock", "market", "price"]):
        return "trading"
    if any(k in tag_str for k in ["code", "engineer", "deploy", "bug", "api"]):
        return "engineering"
    if any(k in tag_str for k in ["personal", "health", "family"]):
        return "personal"
    if any(k in tag_str for k in ["patent", "ip", "monetize", "license"]):
        return "ip_monetize"
    return "neutral"
