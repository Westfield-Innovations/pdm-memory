# © 2026 Westfield Innovations LLC. Patent Pending.
# U.S. App. No. 19/739,419 | 63/953,563 | 63/953,842
# MODIFICATION PROHIBITED. USE AS SHIPPED.

"""
Goal-Anchor Alignment (GAA) — Integrity Parliament Lite for the SDK.

Retrieves high-pressure stewardship / foundational Goal Signatures (high IAW),
scores intent resonance vs torsion (deviation), and returns an AlignmentReport
suitable as a final gate before an agent ACT.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import List, Optional, Sequence, TYPE_CHECKING

from pdm_memory.core.math import calculate_intent_weight, infer_domain
from pdm_memory.core.signature import SignatureRecord
from pdm_memory.models import AlignmentReport

if TYPE_CHECKING:
    from pdm_memory.core.retrieval import RetrievalEngine

_WORD_RE = re.compile(r"[a-zA-Z]{3,}")

# Drawers / domains treated as goal-anchor stores
ANCHOR_DRAWERS: frozenset[str] = frozenset(
    {
        "stewardship",
        "foundational",
        "goals",
        "mission",
        "anchors",
        "principles",
    }
)
ANCHOR_DOMAINS: frozenset[str] = frozenset({"structural", "core_fact"})
ANCHOR_TAG_HINTS: frozenset[str] = frozenset(
    {
        "goal",
        "anchor",
        "mission",
        "stewardship",
        "foundational",
        "integrity",
        "principle",
        "reliability",
        "safety",
        "security",
    }
)

# Intent token → goal token pairs that imply dangerous deviation
_INTENT_VS_GOAL_OPPOSITION: tuple[tuple[str, str], ...] = (
    ("ignore", "reliability"),
    ("ignore", "reliable"),
    ("ignore", "quality"),
    ("ignore", "safety"),
    ("ignore", "error"),
    ("ignore", "errors"),
    ("skip", "validation"),
    ("skip", "validate"),
    ("skip", "test"),
    ("skip", "testing"),
    ("bypass", "security"),
    ("bypass", "safety"),
    ("bypass", "auth"),
    ("disable", "safety"),
    ("disable", "security"),
    ("disable", "guard"),
    ("hack", "integrity"),
    ("rush", "quality"),
    ("rush", "careful"),
    ("hide", "transparent"),
    ("hide", "honesty"),
    ("silence", "honest"),
    ("lie", "honest"),
    ("fabricate", "truth"),
    ("delete", "audit"),
    ("wipe", "audit"),
)

_NEGATION: frozenset[str] = frozenset(
    {"not", "never", "no", "without", "ignore", "skip", "avoid", "disable", "bypass"}
)

DEFAULT_MIN_PRESSURE: float = 60.0
DEFAULT_K_GOALS: int = 8
TORSION_STATUS_THRESHOLD: float = 0.70
CONFLICT_STATUS_THRESHOLD: float = 0.40
ALIGNED_MIN_SCORE: float = 0.45
ALIGNED_MIN_RESONANCE: float = 0.30


@dataclass(slots=True)
class _AnchorScore:
    record: SignatureRecord
    iaw: float
    resonance: float
    torsion: float
    detail: str


def compute_iaw(rec: SignatureRecord) -> float:
    """
    Identity Anchor Weight for a signature.

    Prefer explicit ``metadata['iaw']`` / ``identity_anchor_weight``.
    Else proxy from pressure, phase privilege, and stewardship membership.
    """
    meta = rec.metadata or {}
    raw = meta.get("iaw", meta.get("identity_anchor_weight"))
    if raw is not None:
        try:
            return max(0.0, min(1.0, float(raw)))
        except (TypeError, ValueError):
            pass

    p_norm = max(0.0, min(1.0, float(rec.p_magnitude or 0.0) / 100.0))
    phase = max(0.0, min(1.0, float(rec.phase_privilege or 1.0) / 2.0))
    drawer = (rec.drawer_domain or "").strip().lower()
    domain = (rec.domain or infer_domain(rec.intent_tags or [])).strip().lower()
    tags = {t.lower() for t in (rec.intent_tags or []) if t}

    membership = 0.0
    if drawer in ANCHOR_DRAWERS:
        membership += 0.35
    if domain in ANCHOR_DOMAINS:
        membership += 0.20
    if tags & ANCHOR_TAG_HINTS:
        membership += 0.15
    membership = min(1.0, membership)

    return round(max(0.0, min(1.0, 0.50 * p_norm + 0.20 * phase + 0.30 * membership)), 4)


def is_goal_signature(rec: SignatureRecord) -> bool:
    """True if this record belongs in the GAA anchor pool."""
    meta = rec.metadata or {}
    if meta.get("is_anchor") or meta.get("role") in {"goal", "anchor", "stewardship"}:
        return True
    if meta.get("iaw") is not None or meta.get("identity_anchor_weight") is not None:
        return True
    drawer = (rec.drawer_domain or "").strip().lower()
    if drawer in ANCHOR_DRAWERS:
        return True
    domain = (rec.domain or "").strip().lower()
    if domain in ANCHOR_DOMAINS:
        return True
    tags = {t.lower() for t in (rec.intent_tags or []) if t}
    return bool(tags & ANCHOR_TAG_HINTS)


def select_goal_anchors(
    records: Sequence[SignatureRecord],
    *,
    min_pressure: float = DEFAULT_MIN_PRESSURE,
    k: int = DEFAULT_K_GOALS,
) -> List[SignatureRecord]:
    """High-pressure stewardship/foundational signatures ranked by IAW."""
    pool: List[tuple[float, SignatureRecord]] = []
    for rec in records:
        if float(rec.p_magnitude or 0.0) < min_pressure:
            continue
        if not is_goal_signature(rec):
            continue
        pool.append((compute_iaw(rec), rec))
    pool.sort(key=lambda item: (item[0], float(item[1].p_magnitude or 0.0)), reverse=True)
    return [rec for _, rec in pool[: max(1, k)]]


def intent_goal_torsion(intent_text: str, goal: SignatureRecord) -> tuple[float, str]:
    """
    Deviation of a proposed intent from one Goal Signature.
    Returns (torsion in [0,1], short detail).
    """
    intent_l = (intent_text or "").lower()
    goal_text = goal.compressed_fact or ""
    goal_l = f"{goal_text} {' '.join(goal.intent_tags or [])}".lower()
    intent_tokens = set(_tokenize(intent_l))
    goal_tokens = set(_tokenize(goal_l))

    best = 0.0
    detail = ""

    for intent_cue, goal_cue in _INTENT_VS_GOAL_OPPOSITION:
        if intent_cue in intent_tokens and (
            goal_cue in goal_tokens or goal_cue in goal_l
        ):
            strength = 0.85
            if strength > best:
                best = strength
                detail = f"intent '{intent_cue}' opposes goal cue '{goal_cue}'"

    # Shared topic + negation only on the intent side
    shared = intent_tokens & goal_tokens
    if shared and (_NEGATION & intent_tokens) and not (_NEGATION & goal_tokens):
        strength = min(1.0, 0.45 + 0.10 * len(shared))
        if strength > best:
            best = strength
            detail = (
                "intent negates topics affirmed by the goal "
                f"({', '.join(sorted(shared)[:4])})"
            )

    # Goal forbids scoped predicates after never/not ("never ignore errors")
    forbidden = _forbidden_after_negation(goal_text)
    hit = forbidden & intent_tokens
    if hit:
        strength = min(1.0, 0.75 + 0.05 * len(hit))
        if strength > best:
            best = strength
            detail = f"intent performs what the goal forbids ({', '.join(sorted(hit)[:3])})"

    return round(best, 4), detail


def _forbidden_after_negation(goal_text: str) -> set[str]:
    """Tokens that a 'never/not X Y' goal treats as forbidden actions/objects."""
    ordered = _tokenize(goal_text)
    forbidden: set[str] = set()
    for i, tok in enumerate(ordered):
        if tok not in {"never", "not", "without"}:
            continue
        for nxt in ordered[i + 1 : i + 4]:
            if nxt in _NEGATION:
                continue
            forbidden.add(nxt)
    return forbidden


def intent_goal_resonance(
    engine: "RetrievalEngine",
    intent_text: str,
    goal: SignatureRecord,
) -> float:
    """Resonance of intent against one goal via TAS tag-overlap + lexical coverage."""
    intent_tags = engine._tokenize_query(intent_text)
    coupling = engine._compute_coupling(
        goal,
        query_tags=intent_tags,
        p_eff=float(goal.p_magnitude or 0.0),
        p_raw=float(goal.p_magnitude or 0.0),
        effective_domain=None,
        effective_regime=None,
        target_pressure=float(goal.p_magnitude or 50.0),
    )
    i_weight = calculate_intent_weight(goal.intent_tags or [], intent_text)
    i_norm = max(0.0, min(1.0, (i_weight - 0.8) / 0.2)) if i_weight else 0.0

    intent_bag = _normalize_bag(intent_tags)
    goal_bag = _normalize_bag(
        list(engine._tokenize_query(goal.compressed_fact or ""))
        + [t.lower() for t in (goal.intent_tags or []) if t]
    )
    if intent_bag and goal_bag:
        shared = intent_bag & goal_bag
        tok_j = len(shared) / max(len(intent_bag | goal_bag), 1)
        coverage = len(shared) / max(len(goal_bag), 1)
        # Stewardship vocabulary hit → strong positive signal (GAA is goal-centric)
        steward_hit = shared & (
            ANCHOR_TAG_HINTS
            | {
                "validation",
                "validate",
                "quality",
                "reliable",
                "reliability",
                "deploy",
                "ship",
                "careful",
                "secure",
                "safety",
                "security",
                "integrity",
                "test",
                "testing",
            }
        )
        if steward_hit:
            coverage = max(coverage, min(1.0, 0.50 + 0.12 * len(steward_hit)))
            tok_j = max(tok_j, min(1.0, 0.35 + 0.10 * len(steward_hit)))
    else:
        tok_j = 0.0
        coverage = 0.0

    return round(
        max(
            0.0,
            min(
                1.0,
                0.20 * coupling.tag_overlap
                + 0.15 * i_norm
                + 0.30 * tok_j
                + 0.35 * coverage,
            ),
        ),
        4,
    )


def _normalize_bag(tokens: Sequence[str]) -> set[str]:
    """Normalize tokens so ship/shipping and validate/validation align."""
    out: set[str] = set()
    for raw in tokens:
        t = (raw or "").lower().strip()
        if not t:
            continue
        out.add(t)
        out.add(_stem(t))
    return {x for x in out if x}


def _stem(token: str) -> str:
    t = token
    if t.endswith("ing") and len(t) > 5:
        base = t[:-3]
        if base.endswith("pp"):  # shipping -> ship
            base = base[:-1]
        return base
    if t.endswith("tion") and len(t) > 6:
        return t[:-4] + "e"  # validation -> validate (approx)
    if t.endswith("ies") and len(t) > 4:
        return t[:-3] + "y"
    if t.endswith("es") and len(t) > 4:
        return t[:-2]
    if t.endswith("s") and len(t) > 4 and not t.endswith("ss"):
        return t[:-1]
    return t


def verify_alignment(
    engine: "RetrievalEngine",
    records: Sequence[SignatureRecord],
    intent_text: str,
    *,
    min_pressure: float = DEFAULT_MIN_PRESSURE,
    k_goals: int = DEFAULT_K_GOALS,
    torsion_threshold: float = TORSION_STATUS_THRESHOLD,
    conflict_threshold: float = CONFLICT_STATUS_THRESHOLD,
) -> AlignmentReport:
    """
    Compare a proposed intent to high-IAW goal anchors.

    Fail-closed when no anchors exist (guarded agents must not ACT blind).
    """
    text = (intent_text or "").strip()
    if not text:
        raise ValueError("intent_text cannot be empty.")

    anchors = select_goal_anchors(records, min_pressure=min_pressure, k=k_goals)
    if not anchors:
        return AlignmentReport(
            status="CONFLICT",
            score=0.0,
            conflicting_goals=[],
            explanation=(
                "No high-pressure goal anchors found in stewardship/foundational "
                "domains; cannot verify intent safely — block ACT."
            ),
            resonance=0.0,
            torsion=0.0,
            anchor_count=0,
        )

    scored: List[_AnchorScore] = []
    for goal in anchors:
        iaw = compute_iaw(goal)
        resonance = intent_goal_resonance(engine, text, goal)
        torsion, detail = intent_goal_torsion(text, goal)
        # Weight deviation by IAW — contradicting a strong identity anchor hurts more
        torsion_w = min(1.0, torsion * (0.55 + 0.45 * iaw))
        scored.append(
            _AnchorScore(
                record=goal,
                iaw=iaw,
                resonance=resonance,
                torsion=torsion_w,
                detail=detail,
            )
        )

    # IAW-weighted mean resonance; peak torsion drives status
    iaw_sum = sum(s.iaw for s in scored) or 1.0
    resonance = sum(s.resonance * s.iaw for s in scored) / iaw_sum
    peak = max(scored, key=lambda s: s.torsion)
    torsion = peak.torsion

    score = round(max(0.0, min(1.0, resonance * (1.0 - torsion))), 4)
    conflicting = [
        (s.record.compressed_fact or "").strip()
        for s in scored
        if s.torsion >= conflict_threshold
    ]
    conflicting = [c for c in conflicting if c]

    if torsion >= torsion_threshold:
        status = "TORSION"
        explanation = _explain_torsion(text, peak)
    elif (
        torsion >= conflict_threshold
        or score < ALIGNED_MIN_SCORE
        or resonance < ALIGNED_MIN_RESONANCE
    ):
        status = "CONFLICT"
        explanation = _explain_conflict(text, resonance, torsion, scored)
    else:
        status = "ALIGNED"
        explanation = (
            f"Intent aligns with {len(anchors)} high-IAW goal anchor(s) "
            f"(resonance={resonance:.2f}, torsion={torsion:.2f})."
        )

    return AlignmentReport(
        status=status,
        score=score,
        conflicting_goals=conflicting,
        explanation=explanation,
        resonance=round(resonance, 4),
        torsion=round(torsion, 4),
        anchor_count=len(anchors),
    )


def _explain_torsion(intent_text: str, peak: _AnchorScore) -> str:
    goal_snip = _snip(peak.record.compressed_fact)
    intent_snip = _snip(intent_text)
    why = peak.detail or "semantic opposition to a core goal"
    return (
        f"This intent is dangerous for system integrity: "
        f"'{intent_snip}' contradicts Goal Anchor '{goal_snip}' ({why}). "
        f"Block ACT until the intent is revised."
    )


def _explain_conflict(
    intent_text: str,
    resonance: float,
    torsion: float,
    scored: Sequence[_AnchorScore],
) -> str:
    intent_snip = _snip(intent_text)
    if torsion >= CONFLICT_STATUS_THRESHOLD:
        peak = max(scored, key=lambda s: s.torsion)
        return (
            f"Intent '{intent_snip}' partially conflicts with goal "
            f"'{_snip(peak.record.compressed_fact)}' "
            f"(resonance={resonance:.2f}, torsion={torsion:.2f}). "
            f"Review before ACT."
        )
    return (
        f"Intent '{intent_snip}' does not resonate strongly enough with stored "
        f"goal anchors (resonance={resonance:.2f}). Raise alignment before ACT."
    )


def _tokenize(text: str) -> List[str]:
    words = _WORD_RE.findall((text or "").lower())
    stop = {
        "the", "and", "for", "how", "what", "that", "this", "with",
        "are", "was", "can", "will", "from", "have", "been",
        "should", "would", "could", "which", "when", "where",
    }
    return [w for w in words if w not in stop]


def _snip(text: Optional[str], max_len: int = 80) -> str:
    cleaned = " ".join((text or "").split())
    if not cleaned:
        return "empty"
    if len(cleaned) <= max_len:
        return cleaned
    return cleaned[: max_len - 1].rstrip() + "…"
