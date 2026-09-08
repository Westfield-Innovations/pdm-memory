"""
Canonical ``content_hash`` for PDM source events — the SDK ↔ Companion contract.

TKT-101 AC1 rests entirely on this function: one real-world event ingested
twice must produce the same hash on both sides, so the second ingest reuses
the first event instead of duplicating raw content. A divergence here does not
raise — it silently doubles the event log and quietly breaks AC1 through sync.
That is why the contract is spelled out rather than left to whatever each side
happens to serialise.

The rule, in one line: canonical JSON of a fixed field set, SHA-256, hex.

    json.dumps(doc, sort_keys=True, separators=(",", ":"), ensure_ascii=False)

Django reproduces it with the identical stdlib call — no DRF, no model layer.
``tests/fixtures/event_hash_vectors.json`` carries golden vectors both repos
assert against; regenerate with ``python -m pdm_memory.storage.event_hash``.

What is *in* the hash — the event as it happened in the world:

    event_type, occurred_at, source_system, raw_reference, payload

What is deliberately *out* — anything about our own observation of it:

    observed_at, ingested_at, provenance,
    capture_authority_state, compliance_state

Those differ between the SDK's first sight of a message and Companion's, and
between a live ingest and a replay. Including them would make the same event
hash differently per observer, which is exactly the duplication AC1 forbids.
``capture_authority_state`` and ``compliance_state`` are re-adjudicated over
the row's life, so they cannot sit under an immutable identity either.
"""

from __future__ import annotations

import hashlib
import json
from datetime import datetime, timezone
from typing import Any

# Bump only for a deliberate, coordinated change of the canonical form. The
# version travels inside the hashed document, so v1 and v2 of the same event
# never collide, and a mixed-version fleet degrades to duplicate events rather
# than to wrongly merged ones.
CONTENT_HASH_VERSION: int = 1

# Fixed field order is documentation only — sort_keys makes the actual order
# deterministic — but it keeps the Django port readable next to this one.
HASHED_FIELDS: tuple[str, ...] = (
    "event_type",
    "occurred_at",
    "payload",
    "raw_reference",
    "source_system",
    "v",
)


def normalize_instant(value: datetime | str | None) -> str:
    """
    One spelling of one moment, on both sides of the wire.

    Python's ``isoformat()`` writes ``+00:00``, JavaScript and DRF write ``Z``,
    and microseconds vanish when the value is whole seconds. Three spellings of
    the same instant hash three ways. Everything is coerced to UTC and printed
    with exactly six fractional digits and a trailing ``Z``.

    A naive datetime is read as UTC. That is a real assumption, but the
    alternative — refusing it — turns a hash helper into a validation gate, and
    the callers that pass naive values are the ones least able to fix it.
    """
    if value is None:
        return ""

    if isinstance(value, str):
        raw = value.strip()
        if not raw:
            return ""
        parsed = _parse_iso(raw)
        if parsed is None:
            # Unparseable strings pass through verbatim. Hashing something the
            # caller controls beats guessing at its meaning; both sides do the
            # same thing with it, which is all the contract requires.
            return raw
        value = parsed

    if not isinstance(value, datetime):
        raise TypeError(f"expected datetime or ISO-8601 str, got {type(value).__name__}")

    aware = value.replace(tzinfo=timezone.utc) if value.tzinfo is None else value
    return aware.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.%f") + "Z"


def _parse_iso(raw: str) -> datetime | None:
    """Parse ISO-8601, accepting the ``Z`` suffix that ``fromisoformat`` rejects."""
    candidate = raw[:-1] + "+00:00" if raw.endswith(("Z", "z")) else raw
    try:
        return datetime.fromisoformat(candidate)
    except ValueError:
        return None


def canonical_event_document(
    *,
    event_type: str,
    occurred_at: datetime | str | None,
    source_system: str = "chat",
    raw_reference: str = "",
    payload: str = "",
    version: int = CONTENT_HASH_VERSION,
) -> dict[str, Any]:
    """
    The exact document that gets hashed. Exposed so the contract test can diff
    the *document* when hashes disagree — comparing two hex digests tells you
    nothing about which field drifted.
    """
    return {
        "v": version,
        "event_type": _clean(event_type),
        "occurred_at": normalize_instant(occurred_at),
        "source_system": _clean(source_system),
        "raw_reference": _clean(raw_reference),
        "payload": payload or "",
    }


def canonical_json(doc: dict[str, Any]) -> str:
    """Canonical JSON: sorted keys, no whitespace, real UTF-8 (not \\uXXXX)."""
    return json.dumps(doc, sort_keys=True, separators=(",", ":"), ensure_ascii=False)


def compute_content_hash(
    *,
    event_type: str,
    occurred_at: datetime | str | None,
    source_system: str = "chat",
    raw_reference: str = "",
    payload: str = "",
    version: int = CONTENT_HASH_VERSION,
) -> str:
    """
    SHA-256 hex of the canonical event document.

    ``payload`` is the raw text when the caller holds it; ``raw_reference`` is
    the pointer to where it lives. Passing either alone is normal — a Companion
    ingest usually has the text, a webhook replay usually has only the pointer.
    Passing neither hashes the event's coordinates, which still separates two
    distinct events unless they share a type, an instant, and a source.
    """
    doc = canonical_event_document(
        event_type=event_type,
        occurred_at=occurred_at,
        source_system=source_system,
        raw_reference=raw_reference,
        payload=payload,
        version=version,
    )
    return hashlib.sha256(canonical_json(doc).encode("utf-8")).hexdigest()


def _clean(value: str | None) -> str:
    return (value or "").strip()


# ---------------------------------------------------------------------------
# Golden vectors — the shared fixture both repos assert against
# ---------------------------------------------------------------------------

GOLDEN_VECTOR_INPUTS: tuple[dict[str, Any], ...] = (
    {
        "name": "chat_message_with_payload",
        "event_type": "chat_message",
        "occurred_at": "2026-09-07T10:00:00Z",
        "source_system": "azus_chat",
        "raw_reference": "chat:123:msg:456",
        "payload": "Moved the Orion release review to Friday.",
    },
    {
        "name": "pointer_only_no_payload",
        "event_type": "email",
        "occurred_at": "2026-09-07T10:00:00+00:00",
        "source_system": "gmail",
        "raw_reference": "gmail:thread:abc",
        "payload": "",
    },
    {
        "name": "offset_timezone_normalises_to_utc",
        "event_type": "calendar_entry",
        "occurred_at": "2026-09-07T13:00:00+03:00",
        "source_system": "gcal",
        "raw_reference": "gcal:evt:9",
        "payload": "",
    },
    {
        "name": "unicode_payload_not_escaped",
        "event_type": "chat_message",
        "occurred_at": "2026-09-07T10:00:00.123456Z",
        "source_system": "azus_chat",
        "raw_reference": "",
        "payload": "Зустріч з Алексом — переніс на п'ятницю 🙂",
    },
    {
        "name": "whitespace_is_stripped",
        "event_type": "  chat_message  ",
        "occurred_at": "2026-09-07T10:00:00Z",
        "source_system": " azus_chat ",
        "raw_reference": " chat:123:msg:456 ",
        "payload": "Moved the Orion release review to Friday.",
    },
)


def golden_vectors() -> list[dict[str, Any]]:
    """Inputs paired with their canonical document and expected hash."""
    vectors = []
    for spec in GOLDEN_VECTOR_INPUTS:
        args = {k: v for k, v in spec.items() if k != "name"}
        doc = canonical_event_document(**args)
        vectors.append(
            {
                "name": spec["name"],
                "input": args,
                "canonical_document": doc,
                "canonical_json": canonical_json(doc),
                "content_hash": compute_content_hash(**args),
            }
        )
    return vectors


if __name__ == "__main__":  # pragma: no cover - regeneration helper
    print(json.dumps({"version": CONTENT_HASH_VERSION, "vectors": golden_vectors()},
                     indent=2, ensure_ascii=False))
