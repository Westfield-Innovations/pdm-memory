# © 2026 Westfield Innovations LLC. Patent Pending.
# U.S. App. No. 19/739,419 | 63/953,563 | 63/953,842
# MODIFICATION PROHIBITED. USE AS SHIPPED.

"""Explorer console actions — AI reconciliation and node serialization."""

from __future__ import annotations

import logging
import os
from datetime import datetime, timezone
from typing import Any, Optional

from pdm_memory.core.math import (
    calculate_decay_factor,
    calculate_p_effective,
    calculate_v,
    infer_domain,
    resolve_half_life,
)
from pdm_memory.core.signature import SignatureRecord

logger = logging.getLogger(__name__)

_RECONCILE_PROMPT = """You are Azus, a memory integrity engine. Two stored facts conflict.

Fact A: {fact_a}
Fact B: {fact_b}
Conflict type: {conflict_kind}
Analysis: {explanation}

Write ONE authoritative reconciled fact that resolves the contradiction.
Rules: max 480 characters, declarative tone, no preamble, no quotes."""


def _days_since(dt: Optional[datetime], now: datetime) -> float:
    if dt is None:
        return 0.0
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return max(0.0, (now - dt).total_seconds() / 86400.0)


def live_p_effective(
    rec: SignatureRecord,
    now: datetime,
    *,
    extra_days: float = 0.0,
) -> float:
    """Live P_effective with optional forward projection in days."""
    domain = rec.domain or infer_domain(rec.intent_tags)
    half_life = resolve_half_life(domain)
    days_since_touch = _days_since(rec.last_retrieved or rec.created_at, now) + extra_days
    days_since_created = _days_since(rec.created_at, now) + extra_days
    decay = calculate_decay_factor(
        days_since_touch,
        half_life,
        days_since_created=days_since_created,
        t_persistence=rec.t_persistence,
    )
    v = calculate_v(rec.validation_prediction_correct, rec.validation_prediction_total)
    return calculate_p_effective(rec.p_magnitude, v, decay, intent_weight=1.0, quality=0.80)


def record_to_node(
    rec: SignatureRecord,
    now: datetime,
    *,
    torsion_status: str = "clear",
    extra_days: float = 0.0,
) -> dict[str, Any]:
    """Serialize a signature for the Explorer graph."""
    domain = rec.domain or infer_domain(rec.intent_tags)
    p_eff = live_p_effective(rec, now, extra_days=extra_days)
    return {
        "id": rec.id,
        "text": rec.compressed_fact,
        "p_magnitude": round(float(rec.p_magnitude), 2),
        "p_effective": round(float(p_eff), 2),
        "tags": list(rec.intent_tags),
        "torsion_status": torsion_status,
        "drawer": rec.drawer_domain,
        "domain": domain,
        "source": rec.source,
        "retrieval_count": rec.retrieval_count,
        "t_persistence": float(rec.t_persistence),
        "half_life": resolve_half_life(domain),
        "days_since_touch": round(
            _days_since(rec.last_retrieved or rec.created_at, now), 2
        ),
        "days_since_created": round(_days_since(rec.created_at, now), 2),
        "v_correct": int(rec.validation_prediction_correct),
        "v_total": int(rec.validation_prediction_total),
    }


def _heuristic_reconcile(
    fact_a: str,
    fact_b: str,
    *,
    explanation: str,
    conflict_kind: str,
) -> str:
    detail = explanation.strip() or f"{conflict_kind} conflict"
    if conflict_kind == "deadline":
        return f"Canonical timeline: {fact_a}. Supersedes: {fact_b}."[:480]
    if conflict_kind == "factual":
        return f"Verified fact: {fact_a}. Retired variant: {fact_b}."[:480]
    return f"Reconciled memory: {fact_a} (retired conflicting variant: {fact_b}). {detail}"[:480]


def _try_ollama_reconcile(
    fact_a: str,
    fact_b: str,
    *,
    explanation: str,
    conflict_kind: str,
) -> Optional[str]:
    host = os.environ.get("OLLAMA_HOST", "http://127.0.0.1:11434").rstrip("/")
    model = os.environ.get("PDM_EXPLORER_LLM_MODEL", "llama3.2")
    prompt = _RECONCILE_PROMPT.format(
        fact_a=fact_a,
        fact_b=fact_b,
        conflict_kind=conflict_kind,
        explanation=explanation or "semantic tension between statements",
    )
    try:
        import httpx

        resp = httpx.post(
            f"{host}/api/chat",
            json={
                "model": model,
                "messages": [{"role": "user", "content": prompt}],
                "stream": False,
            },
            timeout=45.0,
        )
        resp.raise_for_status()
        body = resp.json()
        content = str(body.get("message", {}).get("content", "")).strip()
        return content[:480] if content else None
    except Exception as exc:
        logger.info("[Explorer] Ollama reconcile unavailable: %s", exc)
        return None


def generate_reconciliation(
    fact_a: str,
    fact_b: str,
    *,
    explanation: str = "",
    conflict_kind: str = "semantic",
    use_ai: bool = True,
) -> tuple[str, str]:
    """
    Produce reconciled fact text.

    Returns:
        (reconciled_text, method) where method is ``ai`` or ``heuristic``.
    """
    if use_ai:
        ai_text = _try_ollama_reconcile(
            fact_a,
            fact_b,
            explanation=explanation,
            conflict_kind=conflict_kind,
        )
        if ai_text:
            return ai_text, "ai"
    text = _heuristic_reconcile(
        fact_a,
        fact_b,
        explanation=explanation,
        conflict_kind=conflict_kind,
    )
    return text, "heuristic"
