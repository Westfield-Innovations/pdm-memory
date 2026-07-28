# © 2026 Westfield Innovations LLC. Patent Pending.
# U.S. App. No. 19/739,419 | 63/953,563 | 63/953,842
# MODIFICATION PROHIBITED. USE AS SHIPPED.

"""JSON export/import for PDM signatures — backup and cross-backend migration."""

from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from pdm_memory.core.signature import SignatureRecord
from pdm_memory.storage.base import BaseStorage
from pdm_memory.storage.schema import hash_fact_text

_EXPORT_VERSION = "1"


def _dt_to_iso(value: datetime | None) -> str | None:
    if value is None:
        return None
    if value.tzinfo is None:
        value = value.replace(tzinfo=timezone.utc)
    return value.isoformat()


def _iso_to_dt(value: str | None) -> datetime | None:
    if not value:
        return None
    try:
        parsed = datetime.fromisoformat(value)
    except (TypeError, ValueError):
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed


def record_to_dict(rec: SignatureRecord) -> dict[str, Any]:
    """Serialize a SignatureRecord to a JSON-safe dict."""
    return {
        "id": rec.id,
        "user": rec.user,
        "compressed_fact": rec.compressed_fact,
        "source": rec.source,
        "p_magnitude": rec.p_magnitude,
        "t_persistence": rec.t_persistence,
        "phase_privilege": rec.phase_privilege,
        "effective_spike": rec.effective_spike,
        "intent_tags": list(rec.intent_tags or []),
        "question_regime": rec.question_regime,
        "domain": rec.domain,
        "drawer_domain": rec.drawer_domain,
        "retrieval_count": rec.retrieval_count,
        "last_retrieved": _dt_to_iso(rec.last_retrieved),
        "created_at": _dt_to_iso(rec.created_at),
        "validation_prediction_total": rec.validation_prediction_total,
        "validation_prediction_correct": rec.validation_prediction_correct,
        "decay_rate": rec.decay_rate,
        "t_deadline": _dt_to_iso(rec.t_deadline),
        "urgency_rate": rec.urgency_rate,
        "metadata": dict(rec.metadata or {}),
    }


def dict_to_record(data: dict[str, Any], *, user: str) -> SignatureRecord:
    """Deserialize a JSON dict into SignatureRecord."""
    text = str(data.get("compressed_fact") or data.get("text") or "").strip()
    tags = data.get("intent_tags") or data.get("tags") or []
    if isinstance(tags, str):
        tags = [t.strip() for t in tags.split(",") if t.strip()]

    return SignatureRecord(
        id=str(data.get("id") or ""),
        user=user,
        compressed_fact=text,
        source=str(data.get("source") or "import"),
        p_magnitude=float(data.get("p_magnitude", 50.0)),
        t_persistence=float(data.get("t_persistence", 30.0)),
        phase_privilege=float(data.get("phase_privilege", 1.0)),
        effective_spike=data.get("effective_spike"),
        intent_tags=list(tags),
        question_regime=str(data.get("question_regime") or "neutral"),
        domain=str(data.get("domain") or "insight"),
        drawer_domain=str(data.get("drawer_domain") or data.get("drawer") or "general"),
        retrieval_count=int(data.get("retrieval_count") or 0),
        last_retrieved=_iso_to_dt(data.get("last_retrieved")),
        created_at=_iso_to_dt(data.get("created_at")),
        validation_prediction_total=int(data.get("validation_prediction_total") or 0),
        validation_prediction_correct=int(data.get("validation_prediction_correct") or 0),
        decay_rate=float(data.get("decay_rate", 0.9)),
        t_deadline=_iso_to_dt(data.get("t_deadline")),
        urgency_rate=float(data.get("urgency_rate", 2.0)),
        metadata=dict(data.get("metadata") or {}),
    )


def export_signatures_json(
    storage: BaseStorage,
    path: str | Path,
    *,
    user: str = "default",
    limit: int = 100_000,
) -> int:
    """
    Export all signatures for ``user`` to a JSON file.

    Returns:
        Number of signatures written.
    """
    records = storage.list(user=user, limit=limit)
    payload = {
        "version": _EXPORT_VERSION,
        "exported_at": datetime.now(tz=timezone.utc).isoformat(),
        "user": user,
        "count": len(records),
        "signatures": [record_to_dict(r) for r in records],
    }
    out = Path(path)
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps(payload, indent=2, ensure_ascii=False), encoding="utf-8")
    return len(records)


def import_signatures_json(
    storage: BaseStorage,
    path: str | Path,
    *,
    user: str = "default",
    skip_duplicates: bool = True,
) -> dict[str, int]:
    """
    Import signatures from a JSON export file.

    When ``skip_duplicates`` is True, skips rows whose id or fact hash already exist.
    """
    raw = json.loads(Path(path).read_text(encoding="utf-8"))
    items: list[dict[str, Any]] = raw.get("signatures") or raw.get("records") or []
    if not isinstance(items, list):
        raise ValueError("JSON must contain a 'signatures' array")

    saved = 0
    skipped = 0
    errors = 0

    txn = getattr(storage, "transaction", None)
    if callable(txn):
        ctx = txn()
    else:
        from contextlib import nullcontext

        ctx = nullcontext()

    with ctx:
        for item in items:
            if not isinstance(item, dict):
                errors += 1
                continue
            try:
                rec = dict_to_record(item, user=user)
                if not rec.compressed_fact:
                    errors += 1
                    continue
                if not rec.id:
                    import uuid

                    rec.id = str(uuid.uuid4())

                if skip_duplicates:
                    if storage.get(rec.id, user=user) is not None:
                        skipped += 1
                        continue
                    fact = rec.compressed_fact
                    if fact.startswith("[HASH:") and fact.endswith("]"):
                        text_hash = fact[6:-1]
                    else:
                        text_hash = hash_fact_text(fact.strip()[:500])
                    if storage.find_by_hash(text_hash, user=user) is not None:
                        skipped += 1
                        continue

                storage.save(rec)
                saved += 1
            except Exception:
                errors += 1

    return {"saved": saved, "skipped": skipped, "errors": errors}
