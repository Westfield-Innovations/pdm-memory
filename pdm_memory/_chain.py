"""The append-only, hash-chained event history the owner-held ledgers share.

Each event table has the columns ``(sequence, owner, subject, kind,
payload_json, previous_hash, event_hash)`` in that order. An event's hash
covers its sequence, owner, subject, kind, payload and the previous hash, so
editing or reordering a retained event breaks every later link.
"""

from __future__ import annotations

import hashlib
import json

from pdm_memory._errors import LedgerIntegrityError
from pdm_memory._sqldb import Database, _ident

__all__ = ["append_event", "verify_events"]


def _hash(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def _body(
    sequence: int,
    owner: str,
    subject: str | None,
    kind: str,
    payload_json: str,
    previous_hash: str,
) -> str:
    return json.dumps(
        [sequence, owner, subject, kind, payload_json, previous_hash],
        ensure_ascii=False,
        separators=(",", ":"),
    )


def append_event(
    db: Database,
    table: str,
    owner: str,
    subject: str | None,
    kind: str,
    payload_json: str,
) -> int:
    """Append one event inside the caller's transaction and return its sequence."""
    _ident(table)
    sequence = db.execute(
        f"SELECT COALESCE(MAX(sequence), 0)+1 FROM {table}"
    ).fetchone()[0]
    prev = db.execute(
        f"SELECT event_hash FROM {table} WHERE owner=? ORDER BY sequence DESC LIMIT 1",
        (owner,),
    ).fetchone()
    previous_hash = prev[0] if prev else ""
    event_hash = _hash(
        _body(sequence, owner, subject, kind, payload_json, previous_hash)
    )
    db.execute(
        f"INSERT INTO {table} VALUES (?, ?, ?, ?, ?, ?, ?)",
        (sequence, owner, subject, kind, payload_json, previous_hash, event_hash),
    )
    return sequence


def verify_events(db: Database, table: str, owner: str, name: str) -> bool:
    """Check *owner*'s chain in *table*; this cannot detect privileged tail removal."""
    _ident(table)
    previous = ""
    for row in db.execute(
        f"SELECT * FROM {table} WHERE owner=? ORDER BY sequence", (owner,)
    ):
        sequence, _, subject, kind, payload_json, previous_hash, event_hash = row[:7]
        expected = _hash(
            _body(sequence, owner, subject, kind, payload_json, previous_hash)
        )
        if previous_hash != previous or expected != event_hash:
            raise LedgerIntegrityError(f"retained {name} history chain is inconsistent")
        previous = event_hash
    return True
