# © 2026 Westfield Innovations LLC. Patent Pending.
# U.S. App. No. 19/739,419 | 63/953,563 | 63/953,842
# MODIFICATION PROHIBITED. USE AS SHIPPED.

"""Shared PDM storage schema helpers — backend-agnostic."""

from __future__ import annotations

import hashlib
import json
import logging
from collections.abc import Mapping
from datetime import datetime, timezone
from typing import Any

from pdm_memory.core.math import DEFAULT_MIN_P, weight_of
from pdm_memory.core.signature import SignatureRecord

logger = logging.getLogger(__name__)

UPDATABLE_COLUMNS: frozenset[str] = frozenset(
    {
        "compressed_fact",
        "compressed_fact_hash",
        "source",
        "p_magnitude",
        "t_persistence",
        "phase_privilege",
        "effective_spike",
        "intent_tags",
        "question_regime",
        "domain",
        "drawer_domain",
        "retrieval_count",
        "last_retrieved",
        "created_at",
        "validation_prediction_total",
        "validation_prediction_correct",
        "decay_rate",
        "t_deadline",
        "t_event_at",
        "urgency_rate",
        "metadata",
        "is_deleted",
        "idempotency_key",
    }
)

# The carried number (PDM v1.9 change 10): P × V on the 0–100 scale. The fade
# 0.5^(t/T½) is at most 1, so the carried number is never below the weight and a
# gate on it never drops a row the exact weight would keep. Kept as an index on
# this expression: nothing writes it, so it cannot drift from the row.
CARRIED_SQL: str = (
    "(p_magnitude * (validation_prediction_correct + 1.0)"
    " / (validation_prediction_total + 2.0))"
)


def carried_of(rec: SignatureRecord) -> float:
    """The carried number of *rec*, as :data:`CARRIED_SQL` computes it."""
    return (
        rec.p_magnitude
        * (rec.validation_prediction_correct + 1.0)
        / (rec.validation_prediction_total + 2.0)
    )


# Counters a write may move by a delta, in one statement (no lost update).
INCREMENTABLE_COLUMNS: frozenset[str] = frozenset(
    {"retrieval_count", "validation_prediction_total", "validation_prediction_correct"}
)

SCHEMA_SQLITE = """
CREATE TABLE IF NOT EXISTS pdm_signatures (
    id                              TEXT PRIMARY KEY,
    user                            TEXT NOT NULL DEFAULT 'default',
    compressed_fact                 TEXT NOT NULL,
    compressed_fact_hash            TEXT NOT NULL,
    source                          TEXT NOT NULL DEFAULT 'manual',
    p_magnitude                     REAL NOT NULL DEFAULT 50.0,
    t_persistence                   REAL NOT NULL DEFAULT 30.0,
    phase_privilege                 REAL NOT NULL DEFAULT 1.0,
    effective_spike                 REAL,
    intent_tags                     TEXT NOT NULL DEFAULT '[]',
    question_regime                 TEXT NOT NULL DEFAULT 'neutral',
    domain                          TEXT NOT NULL DEFAULT 'insight',
    drawer_domain                   TEXT NOT NULL DEFAULT 'general',
    retrieval_count                 INTEGER NOT NULL DEFAULT 0,
    last_retrieved                  TEXT,
    created_at                      TEXT NOT NULL,
    validation_prediction_total     INTEGER NOT NULL DEFAULT 0,
    validation_prediction_correct   INTEGER NOT NULL DEFAULT 0,
    decay_rate                      REAL NOT NULL DEFAULT 0.9,
    t_deadline                      TEXT,
    t_event_at                      TEXT,
    urgency_rate                    REAL NOT NULL DEFAULT 2.0,
    metadata                        TEXT NOT NULL DEFAULT '{}',
    is_deleted                      INTEGER NOT NULL DEFAULT 0,
    idempotency_key                 TEXT
);

CREATE INDEX IF NOT EXISTS idx_pdm_user_pressure
    ON pdm_signatures (user, p_magnitude DESC);

CREATE INDEX IF NOT EXISTS idx_pdm_user_drawer
    ON pdm_signatures (user, drawer_domain);

CREATE INDEX IF NOT EXISTS idx_pdm_user_fact_hash
    ON pdm_signatures (user, compressed_fact_hash);

CREATE INDEX IF NOT EXISTS idx_pdm_user_active_pressure
    ON pdm_signatures (user, is_deleted, p_magnitude DESC, id DESC);

CREATE INDEX IF NOT EXISTS idx_pdm_user_id
    ON pdm_signatures (user, id);

CREATE UNIQUE INDEX IF NOT EXISTS idx_pdm_user_idempotency
    ON pdm_signatures (user, idempotency_key)
    WHERE idempotency_key IS NOT NULL;

CREATE TABLE IF NOT EXISTS pdm_drawers (
    domain          TEXT NOT NULL,
    user            TEXT NOT NULL DEFAULT 'default',
    description     TEXT NOT NULL DEFAULT '',
    PRIMARY KEY (domain, user)
);
"""

SCHEMA_POSTGRES = """
CREATE TABLE IF NOT EXISTS pdm_signatures (
    id                              TEXT PRIMARY KEY,
    "user"                          TEXT NOT NULL DEFAULT 'default',
    compressed_fact                 TEXT NOT NULL,
    compressed_fact_hash            TEXT NOT NULL,
    source                          TEXT NOT NULL DEFAULT 'manual',
    p_magnitude                     DOUBLE PRECISION NOT NULL DEFAULT 50.0,
    t_persistence                   DOUBLE PRECISION NOT NULL DEFAULT 30.0,
    phase_privilege                 DOUBLE PRECISION NOT NULL DEFAULT 1.0,
    effective_spike                 DOUBLE PRECISION,
    intent_tags                     TEXT NOT NULL DEFAULT '[]',
    question_regime                 TEXT NOT NULL DEFAULT 'neutral',
    domain                          TEXT NOT NULL DEFAULT 'insight',
    drawer_domain                   TEXT NOT NULL DEFAULT 'general',
    retrieval_count                 INTEGER NOT NULL DEFAULT 0,
    last_retrieved                  TEXT,
    created_at                      TEXT NOT NULL,
    validation_prediction_total     INTEGER NOT NULL DEFAULT 0,
    validation_prediction_correct   INTEGER NOT NULL DEFAULT 0,
    decay_rate                      DOUBLE PRECISION NOT NULL DEFAULT 0.9,
    t_deadline                      TEXT,
    t_event_at                      TEXT,
    urgency_rate                    DOUBLE PRECISION NOT NULL DEFAULT 2.0,
    metadata                        TEXT NOT NULL DEFAULT '{}',
    is_deleted                      INTEGER NOT NULL DEFAULT 0,
    idempotency_key                 TEXT
);

CREATE INDEX IF NOT EXISTS idx_pdm_user_pressure
    ON pdm_signatures ("user", p_magnitude DESC);

CREATE INDEX IF NOT EXISTS idx_pdm_user_drawer
    ON pdm_signatures ("user", drawer_domain);

CREATE INDEX IF NOT EXISTS idx_pdm_user_fact_hash
    ON pdm_signatures ("user", compressed_fact_hash);

CREATE INDEX IF NOT EXISTS idx_pdm_user_active_pressure
    ON pdm_signatures ("user", is_deleted, p_magnitude DESC, id DESC);

CREATE INDEX IF NOT EXISTS idx_pdm_user_id
    ON pdm_signatures ("user", id);

CREATE UNIQUE INDEX IF NOT EXISTS idx_pdm_user_idempotency
    ON pdm_signatures ("user", idempotency_key)
    WHERE idempotency_key IS NOT NULL;

CREATE TABLE IF NOT EXISTS pdm_drawers (
    domain          TEXT NOT NULL,
    "user"          TEXT NOT NULL DEFAULT 'default',
    description     TEXT NOT NULL DEFAULT '',
    PRIMARY KEY (domain, "user")
);
"""


def apply_sqlite_migrations(conn: Any) -> None:
    """Add Tier-3 / temporal columns/indexes to existing SQLite databases."""
    cols = {row[1] for row in conn.execute("PRAGMA table_info(pdm_signatures)")}
    if "is_deleted" not in cols:
        conn.execute(
            "ALTER TABLE pdm_signatures ADD COLUMN is_deleted INTEGER NOT NULL DEFAULT 0"
        )
    if "idempotency_key" not in cols:
        conn.execute("ALTER TABLE pdm_signatures ADD COLUMN idempotency_key TEXT")
    if "t_event_at" not in cols:
        conn.execute("ALTER TABLE pdm_signatures ADD COLUMN t_event_at TEXT")
    conn.execute(
        "CREATE INDEX IF NOT EXISTS idx_pdm_user_active_pressure "
        "ON pdm_signatures (user, is_deleted, p_magnitude DESC, id DESC)"
    )
    conn.execute(
        "CREATE INDEX IF NOT EXISTS idx_pdm_user_id "
        "ON pdm_signatures (user, id)"
    )
    conn.execute(
        "CREATE UNIQUE INDEX IF NOT EXISTS idx_pdm_user_idempotency "
        "ON pdm_signatures (user, idempotency_key) "
        "WHERE idempotency_key IS NOT NULL"
    )
    conn.execute(
        "CREATE INDEX IF NOT EXISTS idx_pdm_user_event_at "
        "ON pdm_signatures (user, t_event_at)"
    )
    upgrading = (
        conn.execute(
            "SELECT 1 FROM sqlite_master WHERE type = 'index' "
            "AND name = 'idx_pdm_user_carried'"
        ).fetchone()
        is None
    )
    if upgrading:
        restore_silent(conn, dialect="sqlite")
    conn.execute(
        "CREATE INDEX IF NOT EXISTS idx_pdm_user_carried "
        f"ON pdm_signatures (user, {CARRIED_SQL})"
    )


def apply_postgres_migrations(conn: Any) -> None:
    """Add Tier-3 / temporal columns/indexes to existing PostgreSQL databases."""
    conn.execute(
        "ALTER TABLE pdm_signatures ADD COLUMN IF NOT EXISTS is_deleted INTEGER NOT NULL DEFAULT 0"
    )
    conn.execute(
        "ALTER TABLE pdm_signatures ADD COLUMN IF NOT EXISTS idempotency_key TEXT"
    )
    conn.execute(
        "ALTER TABLE pdm_signatures ADD COLUMN IF NOT EXISTS t_event_at TEXT"
    )
    conn.execute(
        "CREATE INDEX IF NOT EXISTS idx_pdm_user_active_pressure "
        'ON pdm_signatures ("user", is_deleted, p_magnitude DESC, id DESC)'
    )
    conn.execute(
        "CREATE INDEX IF NOT EXISTS idx_pdm_user_id "
        'ON pdm_signatures ("user", id)'
    )
    conn.execute(
        "CREATE UNIQUE INDEX IF NOT EXISTS idx_pdm_user_idempotency "
        'ON pdm_signatures ("user", idempotency_key) '
        "WHERE idempotency_key IS NOT NULL"
    )
    conn.execute(
        "CREATE INDEX IF NOT EXISTS idx_pdm_user_event_at "
        'ON pdm_signatures ("user", t_event_at)'
    )
    upgrading = (
        conn.execute(
            "SELECT 1 FROM pg_indexes WHERE indexname = 'idx_pdm_user_carried'"
        ).fetchone()
        is None
    )
    if upgrading:
        restore_silent(conn, dialect="postgres")
    conn.execute(
        "CREATE INDEX IF NOT EXISTS idx_pdm_user_carried "
        f'ON pdm_signatures ("user", {CARRIED_SQL})'
    )


# The old decay() deleted below P × V × fade × 0.80 < 30, i.e. weight < 37.5;
# weight only falls with disuse, so a deleted row at or over this was a caller's.
OLD_DECAY_REACH: float = DEFAULT_MIN_P / 0.80


def restore_silent(conn: Any, *, dialect: str) -> tuple[int, int]:
    """
    Return to the memory the rows the old ``decay()`` deleted: ``(restored, kept)``.

    Run once, on the upgrade that adds the carried index (PDM v1.9: nothing is
    deleted). A flagged row the old pass could have deleted (weight now under
    :data:`OLD_DECAY_REACH`) comes back; under 30 it is silent. One at or over
    the reach stays deleted: only a caller could have deleted it. A row whose
    fact is already live for the same user stays deleted too.
    """
    ph, user_col = ("?", "user") if dialect == "sqlite" else ("%s", '"user"')
    flagged = conn.execute(
        "SELECT COUNT(*) AS n FROM pdm_signatures WHERE is_deleted = 1"
    ).fetchone()["n"]
    if not flagged:
        return 0, 0
    rows = conn.execute(
        "SELECT * FROM pdm_signatures s WHERE s.is_deleted = 1 AND NOT EXISTS ("
        f"SELECT 1 FROM pdm_signatures l WHERE l.{user_col} = s.{user_col} "
        "AND l.compressed_fact_hash = s.compressed_fact_hash AND l.is_deleted = 0)"
    ).fetchall()
    now = datetime.now(tz=timezone.utc)
    quiet = [r["id"] for r in rows if weight_of(mapping_to_record(r), now)[0] < OLD_DECAY_REACH]
    for i in range(0, len(quiet), 500):
        chunk = quiet[i : i + 500]
        conn.execute(
            "UPDATE pdm_signatures SET is_deleted = 0 "
            f"WHERE id IN ({', '.join([ph] * len(chunk))})",
            chunk,
        )
    kept = int(flagged) - len(quiet)
    logger.warning(
        "[PDM] upgrade: restored %d rows the old decay() could have deleted; "
        "kept %d deleted (a caller's delete, or a live copy exists)",
        len(quiet),
        kept,
    )
    return len(quiet), kept


def parse_dt(value: str | None) -> datetime | None:
    if not value:
        return None
    try:
        return datetime.fromisoformat(value)
    except (TypeError, ValueError):
        return None


def encode_compressed_fact(text: str, *, store_raw: bool) -> tuple[str, str]:
    text_hash = hashlib.sha256(text.encode()).hexdigest()
    stored_text = text if store_raw else f"[HASH:{text_hash}]"
    return stored_text, text_hash


def hash_fact_text(text: str) -> str:
    """SHA-256 of normalized fact text (used for dedupe)."""
    return hashlib.sha256(text.encode()).hexdigest()


def prepare_update_fields(fields: dict) -> dict:
    """Validate column names and serialise JSON/datetime values."""
    if not fields:
        return {}

    unknown = set(fields) - UPDATABLE_COLUMNS
    if unknown:
        raise ValueError(
            f"Refusing to update non-whitelisted column(s): {sorted(unknown)}. "
            f"Allowed: {sorted(UPDATABLE_COLUMNS)}"
        )

    prepared: dict = {}
    for col, value in fields.items():
        if col in ("intent_tags", "metadata"):
            prepared[col] = json.dumps(value)
        elif col in (
            "last_retrieved",
            "created_at",
            "t_deadline",
            "t_event_at",
        ) and isinstance(value, datetime):
            prepared[col] = value.isoformat()
        else:
            prepared[col] = value
    return prepared


def increment_set_clause(
    deltas: Mapping[str, int], fields: dict, placeholder: str
) -> tuple[str, list[Any]]:
    """SET clause adding *deltas* to counters and assigning *fields*, with its values."""
    unknown = set(deltas) - INCREMENTABLE_COLUMNS
    if unknown:
        raise ValueError(f"Not a counter: {sorted(unknown)}")
    prepared = prepare_update_fields(fields)
    parts = [f"{col} = {col} + {placeholder}" for col in deltas]
    parts += [f"{col} = {placeholder}" for col in prepared]
    return ", ".join(parts), [int(v) for v in deltas.values()] + list(prepared.values())


def signature_insert_row(sig: SignatureRecord, *, store_raw: bool) -> tuple[Any, ...]:
    """Values tuple for INSERT into pdm_signatures (column order fixed)."""
    text = sig.compressed_fact or ""
    stored_text, text_hash = encode_compressed_fact(text, store_raw=store_raw)
    created = (sig.created_at or datetime.now(tz=timezone.utc)).isoformat()
    return (
        sig.id,
        sig.user,
        stored_text,
        text_hash,
        sig.source,
        sig.p_magnitude,
        sig.t_persistence,
        sig.phase_privilege,
        sig.effective_spike,
        json.dumps(sig.intent_tags),
        sig.question_regime,
        sig.domain,
        sig.drawer_domain,
        sig.retrieval_count,
        sig.last_retrieved.isoformat() if sig.last_retrieved else None,
        created,
        sig.validation_prediction_total,
        sig.validation_prediction_correct,
        sig.decay_rate,
        sig.t_deadline.isoformat() if sig.t_deadline else None,
        sig.t_event_at.isoformat() if sig.t_event_at else None,
        sig.urgency_rate,
        json.dumps(sig.metadata),
        1 if getattr(sig, "is_deleted", False) else 0,
        getattr(sig, "idempotency_key", None),
    )


def mapping_to_record(row: Mapping[str, Any]) -> SignatureRecord:
    """Map a DB row (sqlite3.Row or psycopg dict) to SignatureRecord."""

    def col(name: str, default: Any = None) -> Any:
        if hasattr(row, "keys") and name not in row.keys():  # noqa: SIM118
            return default
        try:
            return row[name]
        except (KeyError, IndexError, TypeError):
            return default

    return SignatureRecord(
        id=col("id"),
        user=col("user"),
        compressed_fact=col("compressed_fact", ""),
        source=col("source", "manual"),
        p_magnitude=float(col("p_magnitude", 50.0)),
        t_persistence=float(col("t_persistence", 30.0)),
        phase_privilege=float(col("phase_privilege", 1.0)),
        effective_spike=col("effective_spike"),
        intent_tags=json.loads(col("intent_tags") or "[]"),
        question_regime=col("question_regime", "neutral"),
        domain=col("domain", "insight"),
        drawer_domain=col("drawer_domain", "general"),
        retrieval_count=int(col("retrieval_count", 0)),
        last_retrieved=parse_dt(col("last_retrieved")),
        created_at=parse_dt(col("created_at")),
        validation_prediction_total=int(col("validation_prediction_total", 0)),
        validation_prediction_correct=int(col("validation_prediction_correct", 0)),
        decay_rate=float(col("decay_rate", 0.9)),
        t_deadline=parse_dt(col("t_deadline")),
        t_event_at=parse_dt(col("t_event_at")),
        urgency_rate=float(col("urgency_rate", 2.0)),
        metadata=json.loads(col("metadata") or "{}"),
        is_deleted=bool(col("is_deleted", 0)),
        idempotency_key=col("idempotency_key"),
    )
