# © 2026 Westfield Innovations LLC. Patent Pending.
# U.S. App. No. 19/739,419 | 63/953,563 | 63/953,842
# MODIFICATION PROHIBITED. USE AS SHIPPED.

"""Update / reinforce / penalize / reconcile writes."""

from __future__ import annotations

import logging
from contextlib import AbstractContextManager, nullcontext
from datetime import datetime, timezone
from typing import Any, Protocol

from pdm_memory.core.math import calculate_effective_spike, infer_domain

logger = logging.getLogger(__name__)


class _MutableMemory(Protocol):
    _storage: Any
    _user: str
    _engine: Any

    def save(self, text: str, *args: Any, **kwargs: Any) -> str: ...
    def _record_to_hit(self, rec: Any) -> Any: ...


def update_memory(
    mem: _MutableMemory,
    memory_id: str,
    *,
    text: str | None = None,
    tags: list[str] | None = None,
    p_magnitude: float | None = None,
    t_persistence: float | None = None,
    drawer: str | None = None,
    regime: str | None = None,
    source: str | None = None,
    metadata: dict[str, Any] | None = None,
    deadline: datetime | None = None,
    event_at: datetime | None = None,
) -> Any:
    rec = mem._storage.get(memory_id, user=mem._user)
    if rec is None:
        raise ValueError(f"Memory '{memory_id}' not found for user '{mem._user}'")

    fields: dict[str, Any] = {}
    if text is not None:
        if not text.strip():
            raise ValueError("Memory text cannot be empty.")
        from pdm_memory.storage.schema import encode_compressed_fact

        trimmed = text.strip()[:500]
        store_raw = getattr(mem._storage, "store_raw", True)
        stored, text_hash = encode_compressed_fact(trimmed, store_raw=store_raw)
        fields["compressed_fact"] = stored
        fields["compressed_fact_hash"] = text_hash
    if tags is not None:
        fields["intent_tags"] = tags
        fields["domain"] = infer_domain(tags)
    if p_magnitude is not None:
        if not 0.0 <= p_magnitude <= 100.0:
            raise ValueError("p_magnitude must be between 0 and 100")
        fields["p_magnitude"] = p_magnitude
    if t_persistence is not None:
        fields["t_persistence"] = t_persistence
    if drawer is not None:
        fields["drawer_domain"] = drawer
    if regime is not None:
        fields["question_regime"] = regime
    if source is not None:
        fields["source"] = source
    if metadata is not None:
        fields["metadata"] = {**(rec.metadata or {}), **metadata}
    if deadline is not None:
        fields["t_deadline"] = deadline
    if event_at is not None:
        fields["t_event_at"] = event_at
    elif deadline is not None and rec.t_event_at is None:
        fields["t_event_at"] = deadline

    if not fields:
        raise ValueError("At least one field must be provided to update()")

    new_p = fields.get("p_magnitude", rec.p_magnitude)
    new_t = fields.get("t_persistence", rec.t_persistence)
    if "p_magnitude" in fields or "t_persistence" in fields:
        fields["effective_spike"] = calculate_effective_spike(
            new_p, new_t, rec.phase_privilege
        )

    mem._storage.update(memory_id, user=mem._user, **fields)
    updated = mem._storage.get(memory_id, user=mem._user)
    if updated is None:
        raise ValueError(f"Memory '{memory_id}' not found after update")
    logger.debug("[PDM] update(%s) fields=%s", memory_id[:8], sorted(fields))
    return mem._record_to_hit(updated)


def reinforce_memory(
    mem: _MutableMemory,
    memory_id: str,
    coupling_score: float = 0.5,
) -> None:
    now = datetime.now(tz=timezone.utc)
    atomic_reinforce = getattr(mem._storage, "atomic_reinforce", None)
    if callable(atomic_reinforce):
        atomic_reinforce(
            memory_id,
            mem._user,
            compute_delta=lambda p, rc: mem._engine.compute_reinforcement_delta(
                p, rc, coupling_score
            ),
            last_retrieved=now,
        )
        logger.debug("[PDM] reinforce(%s) atomic", memory_id)
        return

    rec = mem._storage.get(memory_id, user=mem._user)
    if rec is None:
        raise ValueError(f"Memory '{memory_id}' not found for user '{mem._user}'")
    delta = mem._engine.compute_reinforcement_delta(
        rec.p_magnitude, rec.retrieval_count, coupling_score
    )
    new_p = min(100.0, rec.p_magnitude + delta)
    new_spike = calculate_effective_spike(new_p, rec.t_persistence, rec.phase_privilege)
    new_total = (rec.validation_prediction_total or 0) + 1
    new_correct = (rec.validation_prediction_correct or 0) + 1
    mem._storage.update(
        memory_id,
        user=mem._user,
        p_magnitude=new_p,
        effective_spike=new_spike,
        retrieval_count=(rec.retrieval_count or 0) + 1,
        last_retrieved=now,
        validation_prediction_total=new_total,
        validation_prediction_correct=new_correct,
    )
    logger.debug(
        "[PDM] reinforce(%s) Δp=+%.2f → P=%.1f  V_total=%d V_correct=%d",
        memory_id,
        delta,
        new_p,
        new_total,
        new_correct,
    )


def penalize_memory(
    mem: _MutableMemory,
    memory_id: str,
    coupling_score: float = 0.5,
) -> None:
    rec = mem._storage.get(memory_id, user=mem._user)
    if rec is None:
        logger.warning("[PDM] penalize(%s): not found", memory_id)
        return
    delta = mem._engine.compute_reinforcement_delta(
        rec.p_magnitude, rec.retrieval_count, coupling_score
    )
    new_p = max(0.0, rec.p_magnitude - delta)
    new_spike = calculate_effective_spike(new_p, rec.t_persistence, rec.phase_privilege)
    new_total = (rec.validation_prediction_total or 0) + 1
    mem._storage.update(
        memory_id,
        user=mem._user,
        p_magnitude=new_p,
        effective_spike=new_spike,
        retrieval_count=(rec.retrieval_count or 0) + 1,
        last_retrieved=datetime.now(tz=timezone.utc),
        validation_prediction_total=new_total,
    )
    logger.debug(
        "[PDM] penalize(%s) Δp=-%.2f → P=%.1f  V_total=%d V_correct=%d",
        memory_id,
        delta,
        new_p,
        new_total,
        rec.validation_prediction_correct or 0,
    )


def reconcile_torsion_pair(
    mem: _MutableMemory,
    signature_a_id: str,
    signature_b_id: str,
    reconciled_text: str,
) -> str:
    rec_a = mem._storage.get(signature_a_id, user=mem._user)
    rec_b = mem._storage.get(signature_b_id, user=mem._user)
    if rec_a is None or rec_b is None:
        raise ValueError("One or both signatures not found")
    text = reconciled_text.strip()[:500]
    if not text:
        raise ValueError("reconciled_text cannot be empty")

    tags = sorted({t for t in rec_a.intent_tags + rec_b.intent_tags if t})
    drawer = rec_a.drawer_domain or rec_b.drawer_domain or "general"
    p_mag = min(100.0, max(rec_a.p_magnitude, rec_b.p_magnitude) + 8.0)

    txn = getattr(mem._storage, "transaction", None)
    ctx: AbstractContextManager[None] = txn() if callable(txn) else nullcontext()
    with ctx:
        new_id = mem.save(
            text,
            tags=tags,
            drawer=drawer,
            p_magnitude=p_mag,
            source="reconcile",
            dedupe=False,
        )
        mem._storage.delete(signature_a_id, user=mem._user)
        mem._storage.delete(signature_b_id, user=mem._user)
    logger.info(
        "[PDM] reconcile_torsion %s+%s → %s",
        signature_a_id[:8],
        signature_b_id[:8],
        new_id[:8],
    )
    return new_id
