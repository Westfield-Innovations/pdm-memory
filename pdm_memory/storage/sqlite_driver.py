"""
SQLite Storage Driver — Task 1.2

Zero external dependencies beyond Python stdlib.
Stores all PDM signatures in a single local .db file.

Privacy guarantee: raw compressed_fact is stored by default.
If store_raw=False, only the SHA-256 hash of the text is stored
(the memory is opaque even to someone with file access).

Schema is auto-created on first run.

Usage:
    driver = SQLiteDriver("./my_app.db")
    memory_id = driver.save(sig)
    hits = driver.list(user="alice", min_pressure=50)
"""

from __future__ import annotations

import hashlib
import json
import logging
import sqlite3
import threading
from datetime import datetime, timezone
from typing import List, Optional

from pdm_memory.core.signature import DrawerInfo, SignatureRecord
from pdm_memory.storage.base import BaseStorage

logger = logging.getLogger(__name__)

# Columns allowed in UPDATE — never take raw dict keys (SQL injection).
# ``id`` and ``user`` are intentionally excluded (immutable ownership).
_UPDATABLE_COLUMNS: frozenset[str] = frozenset(
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
        "urgency_rate",
        "metadata",
    }
)

_SCHEMA = """
CREATE TABLE IF NOT EXISTS pdm_signatures (
    id                              TEXT PRIMARY KEY,
    user                            TEXT NOT NULL DEFAULT 'default',
    compressed_fact                 TEXT NOT NULL,
    compressed_fact_hash            TEXT NOT NULL,
    source                          TEXT NOT NULL DEFAULT 'chat',
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
    urgency_rate                    REAL NOT NULL DEFAULT 2.0,
    metadata                        TEXT NOT NULL DEFAULT '{}'
);

CREATE INDEX IF NOT EXISTS idx_pdm_user_pressure
    ON pdm_signatures (user, p_magnitude DESC);

CREATE INDEX IF NOT EXISTS idx_pdm_user_drawer
    ON pdm_signatures (user, drawer_domain);

CREATE TABLE IF NOT EXISTS pdm_drawers (
    domain          TEXT NOT NULL,
    user            TEXT NOT NULL DEFAULT 'default',
    description     TEXT NOT NULL DEFAULT '',
    PRIMARY KEY (domain, user)
);
"""


def _now_iso() -> str:
    return datetime.now(tz=timezone.utc).isoformat()


def _parse_dt(s: Optional[str]) -> Optional[datetime]:
    if not s:
        return None
    try:
        return datetime.fromisoformat(s)
    except Exception:
        return None


class SQLiteDriver(BaseStorage):
    """
    SQLite-backed PDM storage.  Thread-safe (one connection per thread).

    Args:
        db_path:    Path to the .db file.  Created automatically.
        store_raw:  If False, only the SHA-256 hash of compressed_fact is
                    stored — the text itself never touches disk.
    """

    def __init__(self, db_path: str = "./pdm_memory.db", store_raw: bool = True) -> None:
        self.db_path = db_path
        self.store_raw = store_raw
        self._local = threading.local()
        # Initialise schema on startup
        conn = self._conn()
        conn.execute("PRAGMA journal_mode=WAL")
        conn.executescript(_SCHEMA)
        conn.commit()
        logger.debug("[PDM-SQLite] Opened %s (store_raw=%s)", db_path, store_raw)

    # ------------------------------------------------------------------
    # Connection management (one connection per thread)
    # ------------------------------------------------------------------

    def _conn(self) -> sqlite3.Connection:
        if not hasattr(self._local, "conn") or self._local.conn is None:
            self._local.conn = sqlite3.connect(
                self.db_path,
                detect_types=sqlite3.PARSE_DECLTYPES,
                check_same_thread=False,
            )
            self._local.conn.row_factory = sqlite3.Row
            # Optimise connection performance pragmas
            self._local.conn.execute("PRAGMA synchronous=NORMAL")
            self._local.conn.execute("PRAGMA temp_store=MEMORY")
        return self._local.conn

    def close(self) -> None:
        if hasattr(self._local, "conn") and self._local.conn:
            self._local.conn.close()
            self._local.conn = None

    # ------------------------------------------------------------------
    # BaseStorage implementation
    # ------------------------------------------------------------------

    def save(self, sig: SignatureRecord) -> str:
        """Insert a new signature. Returns sig.id."""
        text = sig.compressed_fact or ""
        text_hash = hashlib.sha256(text.encode()).hexdigest()
        stored_text = text if self.store_raw else f"[HASH:{text_hash}]"

        conn = self._conn()
        conn.execute(
            """
            INSERT OR REPLACE INTO pdm_signatures (
                id, user, compressed_fact, compressed_fact_hash, source,
                p_magnitude, t_persistence, phase_privilege, effective_spike,
                intent_tags, question_regime, domain, drawer_domain,
                retrieval_count, last_retrieved, created_at,
                validation_prediction_total, validation_prediction_correct,
                decay_rate, t_deadline, urgency_rate, metadata
            ) VALUES (
                ?, ?, ?, ?, ?,
                ?, ?, ?, ?,
                ?, ?, ?, ?,
                ?, ?, ?,
                ?, ?,
                ?, ?, ?, ?
            )
            """,
            (
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
                (sig.created_at or datetime.now(tz=timezone.utc)).isoformat(),
                sig.validation_prediction_total,
                sig.validation_prediction_correct,
                sig.decay_rate,
                sig.t_deadline.isoformat() if sig.t_deadline else None,
                sig.urgency_rate,
                json.dumps(sig.metadata),
            ),
        )
        # Upsert drawer record
        conn.execute(
            "INSERT OR IGNORE INTO pdm_drawers (domain, user, description) VALUES (?, ?, ?)",
            (sig.drawer_domain, sig.user, ""),
        )
        conn.commit()
        logger.debug("[PDM-SQLite] Saved signature %s (P=%.1f)", sig.id, sig.p_magnitude)
        return sig.id

    def get(self, memory_id: str, user: str = "default") -> Optional[SignatureRecord]:
        row = self._conn().execute(
            "SELECT * FROM pdm_signatures WHERE id = ? AND user = ?",
            (memory_id, user),
        ).fetchone()
        return self._row_to_record(row) if row else None

    def update(self, memory_id: str, user: str = "default", **fields) -> None:
        """Update whitelisted columns for ``memory_id`` owned by ``user``."""
        prepared = self._prepare_update_fields(fields)
        if not prepared:
            return

        set_clause = ", ".join(f"{col} = ?" for col in prepared)
        values = list(prepared.values()) + [memory_id, user]
        cur = self._conn().execute(
            f"UPDATE pdm_signatures SET {set_clause} WHERE id = ? AND user = ?",
            values,
        )
        self._conn().commit()
        if cur.rowcount == 0:
            logger.warning(
                "[PDM-SQLite] update(%s) affected 0 rows (missing or wrong user=%s)",
                memory_id,
                user,
            )

    def update_batch(
        self,
        updates: List[tuple[str, dict]],
        user: str = "default",
    ) -> None:
        """Batch-update with the same whitelist + user scope as :meth:`update`."""
        if not updates:
            return
        conn = self._conn()
        for memory_id, fields in updates:
            prepared = self._prepare_update_fields(fields)
            if not prepared:
                continue
            set_clause = ", ".join(f"{col} = ?" for col in prepared)
            values = list(prepared.values()) + [memory_id, user]
            cur = conn.execute(
                f"UPDATE pdm_signatures SET {set_clause} WHERE id = ? AND user = ?",
                values,
            )
            if cur.rowcount == 0:
                logger.warning(
                    "[PDM-SQLite] update_batch(%s) affected 0 rows (user=%s)",
                    memory_id,
                    user,
                )
        conn.commit()

    @staticmethod
    def _prepare_update_fields(fields: dict) -> dict:
        """
        Validate column names against whitelist and serialise JSON/datetime.

        Raises:
            ValueError: If any key is not an allowed column (blocks SQL injection).
        """
        if not fields:
            return {}

        unknown = set(fields) - _UPDATABLE_COLUMNS
        if unknown:
            raise ValueError(
                f"Refusing to update non-whitelisted column(s): {sorted(unknown)}. "
                f"Allowed: {sorted(_UPDATABLE_COLUMNS)}"
            )

        prepared: dict = {}
        for col, value in fields.items():
            if col == "intent_tags":
                prepared[col] = json.dumps(value)
            elif col == "metadata":
                prepared[col] = json.dumps(value)
            elif col in ("last_retrieved", "created_at", "t_deadline") and isinstance(
                value, datetime
            ):
                prepared[col] = value.isoformat()
            else:
                prepared[col] = value
        return prepared

    def delete(self, memory_id: str, user: str = "default") -> None:
        self._conn().execute(
            "DELETE FROM pdm_signatures WHERE id = ? AND user = ?",
            (memory_id, user),
        )
        self._conn().commit()

    def list(
        self,
        user: str = "default",
        limit: int = 100,
        min_pressure: float = 0.0,
        drawer: Optional[str] = None,
    ) -> List[SignatureRecord]:
        query = (
            "SELECT * FROM pdm_signatures WHERE user = ? AND p_magnitude >= ?"
        )
        params: list = [user, min_pressure]
        if drawer:
            query += " AND drawer_domain = ?"
            params.append(drawer)
        query += " ORDER BY p_magnitude DESC LIMIT ?"
        params.append(limit)

        rows = self._conn().execute(query, params).fetchall()
        return [self._row_to_record(r) for r in rows]

    def list_drawers(self, user: str = "default") -> List[DrawerInfo]:
        rows = self._conn().execute(
            """
            SELECT
                d.domain,
                d.description,
                COUNT(s.id)       AS sig_count,
                AVG(s.p_magnitude) AS avg_pressure
            FROM pdm_drawers d
            LEFT JOIN pdm_signatures s
                ON s.drawer_domain = d.domain AND s.user = d.user
            WHERE d.user = ?
            GROUP BY d.domain, d.description
            ORDER BY d.domain
            """,
            (user,),
        ).fetchall()
        return [
            DrawerInfo(
                domain=r["domain"],
                signature_count=r["sig_count"] or 0,
                avg_pressure=round(r["avg_pressure"] or 0.0, 2),
                description=r["description"] or "",
            )
            for r in rows
        ]

    def count(self, user: str = "default") -> int:
        row = self._conn().execute(
            "SELECT COUNT(*) FROM pdm_signatures WHERE user = ?", (user,)
        ).fetchone()
        return row[0] if row else 0

    # ------------------------------------------------------------------
    # Private helpers
    # ------------------------------------------------------------------

    @staticmethod
    def _row_to_record(row: sqlite3.Row) -> SignatureRecord:
        return SignatureRecord(
            id=row["id"],
            user=row["user"],
            compressed_fact=row["compressed_fact"],
            source=row["source"],
            p_magnitude=row["p_magnitude"],
            t_persistence=row["t_persistence"],
            phase_privilege=row["phase_privilege"],
            effective_spike=row["effective_spike"],
            intent_tags=json.loads(row["intent_tags"] or "[]"),
            question_regime=row["question_regime"],
            domain=row["domain"],
            drawer_domain=row["drawer_domain"],
            retrieval_count=row["retrieval_count"],
            last_retrieved=_parse_dt(row["last_retrieved"]),
            created_at=_parse_dt(row["created_at"]),
            validation_prediction_total=row["validation_prediction_total"],
            validation_prediction_correct=row["validation_prediction_correct"],
            decay_rate=row["decay_rate"],
            t_deadline=_parse_dt(row["t_deadline"]),
            urgency_rate=row["urgency_rate"],
            metadata=json.loads(row["metadata"] or "{}"),
        )
