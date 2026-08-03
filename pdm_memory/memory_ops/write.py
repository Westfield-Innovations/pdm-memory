# © 2026 Westfield Innovations LLC. Patent Pending.
# U.S. App. No. 19/739,419 | 63/953,563 | 63/953,842
# MODIFICATION PROHIBITED. USE AS SHIPPED.

"""Memory write path — save / save_many."""

from __future__ import annotations

import logging
from contextlib import AbstractContextManager, nullcontext
from datetime import datetime
from typing import Any, Protocol

from pdm_memory.core.math import calculate_effective_spike, infer_domain
from pdm_memory.core.signature import SignatureRecord

logger = logging.getLogger(__name__)


class _WritableMemory(Protocol):
    _storage: Any
    _user: str

    def reinforce(self, memory_id: str, coupling_score: float = 0.5) -> None: ...
    def _run_pre_save_hooks(self, sig: SignatureRecord) -> SignatureRecord: ...
    def _run_post_save_hooks(self, sig: SignatureRecord, memory_id: str) -> None: ...
    def save(self, text: str, *args: Any, **kwargs: Any) -> str: ...


def save_memory(
    mem: _WritableMemory,
    text: str,
    source: str = "chat",
    tags: list[str] | None = None,
    p_magnitude: float = 50.0,
    t_persistence: float = 30.0,
    drawer: str = "general",
    regime: str = "neutral",
    phase_privilege: float = 1.0,
    deadline: datetime | None = None,
    event_at: datetime | None = None,
    metadata: dict[str, Any] | None = None,
    *,
    dedupe: bool = True,
    dedupe_reinforce: bool = False,
    idempotency_key: str | None = None,
) -> str:
    if not text or not text.strip():
        raise ValueError("Memory text cannot be empty.")

    text = text.strip()[:500]

    if idempotency_key:
        key = idempotency_key.strip()
        if key:
            existing = mem._storage.find_by_idempotency_key(key, user=mem._user)
            if existing is not None:
                logger.debug("[PDM] save() idempotency → %s", existing.id[:8])
                return existing.id

    if dedupe:
        from pdm_memory.storage.schema import hash_fact_text

        existing = mem._storage.find_by_hash(hash_fact_text(text), user=mem._user)
        if existing is not None:
            if dedupe_reinforce:
                mem.reinforce(existing.id)
            logger.debug("[PDM] save() dedupe → %s", existing.id[:8])
            return existing.id

    resolved_tags = tags or []
    domain = infer_domain(resolved_tags)
    eff_spike = calculate_effective_spike(p_magnitude, t_persistence, phase_privilege)

    resolved_event = event_at
    if resolved_event is None and deadline is not None:
        resolved_event = deadline

    sig = SignatureRecord(
        user=mem._user,
        compressed_fact=text,
        source=source,
        p_magnitude=p_magnitude,
        t_persistence=t_persistence,
        phase_privilege=phase_privilege,
        effective_spike=eff_spike,
        intent_tags=resolved_tags,
        question_regime=regime,
        domain=domain,
        drawer_domain=drawer,
        decay_rate=0.9,
        t_deadline=deadline,
        t_event_at=resolved_event,
        metadata=metadata or {},
        idempotency_key=idempotency_key.strip() if idempotency_key else None,
    )
    sig = mem._run_pre_save_hooks(sig)
    memory_id = mem._storage.save(sig)
    mem._run_post_save_hooks(sig, memory_id)
    logger.debug("[PDM] save() → %s (P=%.1f)", memory_id, p_magnitude)
    return memory_id


def save_many_memories(
    mem: _WritableMemory,
    items: list[dict[str, Any]],
    *,
    dedupe: bool = True,
    dedupe_reinforce: bool = False,
) -> dict[str, int]:
    saved = 0
    skipped = 0
    errors = 0

    txn = getattr(mem._storage, "transaction", None)
    ctx: AbstractContextManager[None] = txn() if callable(txn) else nullcontext()

    with ctx:
        for item in items:
            try:
                text = str(item.get("text") or item.get("compressed_fact") or "").strip()
                if not text:
                    errors += 1
                    continue

                from pdm_memory.storage.schema import hash_fact_text

                if dedupe:
                    existing = mem._storage.find_by_hash(
                        hash_fact_text(text[:500]), user=mem._user
                    )
                    if existing is not None:
                        if dedupe_reinforce:
                            mem.reinforce(existing.id)
                        skipped += 1
                        continue

                mem.save(
                    text,
                    source=str(item.get("source") or "batch"),
                    tags=item.get("tags") or item.get("intent_tags"),
                    p_magnitude=float(item.get("p_magnitude", 50.0)),
                    t_persistence=float(item.get("t_persistence", 30.0)),
                    drawer=str(item.get("drawer") or item.get("drawer_domain") or "general"),
                    regime=str(item.get("regime") or item.get("question_regime") or "neutral"),
                    deadline=item.get("deadline") or item.get("t_deadline"),
                    event_at=item.get("event_at") or item.get("t_event_at"),
                    metadata=item.get("metadata"),
                    dedupe=False,
                )
                saved += 1
            except Exception:
                errors += 1

    logger.info("[PDM] save_many saved=%d skipped=%d errors=%d", saved, skipped, errors)
    return {"saved": saved, "skipped": skipped, "errors": errors}
