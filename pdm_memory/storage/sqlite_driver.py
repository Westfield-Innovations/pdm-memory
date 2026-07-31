# © 2026 Westfield Innovations LLC. Patent Pending.
# U.S. App. No. 19/739,419 | 63/953,563 | 63/953,842
# MODIFICATION PROHIBITED. USE AS SHIPPED.

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

import builtins
import hashlib
import json
import logging
import sqlite3
import threading
from collections.abc import Iterator
from contextlib import contextmanager
from datetime import datetime, timezone

from pdm_memory.core.signature import DrawerInfo, SignatureRecord
from pdm_memory.storage.base import BaseStorage, SaveManyResult
from pdm_memory.storage.schema import (
    SCHEMA_SQLITE,
    UPDATABLE_COLUMNS,
    apply_sqlite_migrations,
    mapping_to_record,
    signature_insert_row,
)

logger = logging.getLogger(__name__)


_INSERT_SIGNATURE_SQL = """
INSERT INTO pdm_signatures (
    id, user, compressed_fact, compressed_fact_hash, source,
    p_magnitude, t_persistence, phase_privilege, effective_spike,
    intent_tags, question_regime, domain, drawer_domain,
    retrieval_count, last_retrieved, created_at,
    validation_prediction_total, validation_prediction_correct,
    decay_rate, t_deadline, t_event_at, urgency_rate, metadata,
    is_deleted, idempotency_key
) VALUES (
    ?, ?, ?, ?, ?,
    ?, ?, ?, ?,
    ?, ?, ?, ?,
    ?, ?, ?,
    ?, ?,
    ?, ?, ?, ?, ?,
    ?, ?
)
"""

_INSERT_DRAWER_SQL = """
INSERT OR IGNORE INTO pdm_drawers (domain, user, description)
VALUES (?, ?, ?)
"""


def _now_iso() -> str:
    return datetime.now(tz=timezone.utc).isoformat()


def _parse_dt(s: str | None) -> datetime | None:
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
        conn.executescript(SCHEMA_SQLITE)
        apply_sqlite_migrations(conn)
        conn.commit()
        logger.debug("[PDM-SQLite] Opened %s (store_raw=%s)", db_path, store_raw)

    @contextmanager
    def transaction(self) -> Iterator[None]:
        """Atomic batch — defer commits until context exit."""
        conn = self._conn()
        depth = getattr(self._local, "txn_depth", 0)
        if depth == 0:
            conn.execute("BEGIN IMMEDIATE")
        self._local.txn_depth = depth + 1
        try:
            yield
            self._local.txn_depth = depth
            if depth == 0:
                conn.commit()
        except Exception:
            self._local.txn_depth = depth
            if depth == 0:
                conn.rollback()
            raise

    def _commit_if_idle(self, conn: sqlite3.Connection) -> None:
        if getattr(self._local, "txn_depth", 0) == 0:
            conn.commit()

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
            INSERT INTO pdm_signatures (
                id, user, compressed_fact, compressed_fact_hash, source,
                p_magnitude, t_persistence, phase_privilege, effective_spike,
                intent_tags, question_regime, domain, drawer_domain,
                retrieval_count, last_retrieved, created_at,
                validation_prediction_total, validation_prediction_correct,
                decay_rate, t_deadline, t_event_at, urgency_rate, metadata,
                is_deleted, idempotency_key
            ) VALUES (
                ?, ?, ?, ?, ?,
                ?, ?, ?, ?,
                ?, ?, ?, ?,
                ?, ?, ?,
                ?, ?,
                ?, ?, ?, ?, ?,
                ?, ?
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
                sig.t_event_at.isoformat() if sig.t_event_at else None,
                sig.urgency_rate,
                json.dumps(sig.metadata),
                1 if sig.is_deleted else 0,
                sig.idempotency_key,
            ),
        )
        # Upsert drawer record
        conn.execute(
            "INSERT OR IGNORE INTO pdm_drawers (domain, user, description) VALUES (?, ?, ?)",
            (sig.drawer_domain, sig.user, ""),
        )
        self._commit_if_idle(conn)
        logger.debug("[PDM-SQLite] Saved signature %s (P=%.1f)", sig.id, sig.p_magnitude)
        return sig.id

    def save_many(self, sigs: builtins.list[SignatureRecord]) -> builtins.list[SaveManyResult]:
        if not sigs:
            return []

        results = [SaveManyResult(index=i, id=None) for i in range(len(sigs))]
        seen_ids: set[str] = set()
        pending: list[tuple[int, SignatureRecord]] = []
        for index, sig in enumerate(sigs):
            if sig.id in seen_ids:
                results[index] = SaveManyResult(
                    index=index,
                    id=None,
                    error="Duplicate id in batch",
                )
                continue
            seen_ids.add(sig.id)
            pending.append((index, sig))

        if not pending:
            return results

        conn = self._conn()
        with self.transaction():
            for start in range(0, len(pending), 500):
                chunk = pending[start : start + 500]
                grouped: dict[str, list[tuple[int, SignatureRecord]]] = {}
                for index, sig in chunk:
                    grouped.setdefault(sig.user, []).append((index, sig))

                write_rows: list[tuple[int, SignatureRecord]] = []
                drawer_rows: list[tuple[str, str, str]] = []
                for user, user_chunk in grouped.items():
                    ids = [sig.id for _, sig in user_chunk]
                    placeholders = ", ".join("?" for _ in ids)
                    existing_rows = conn.execute(
                        f"SELECT id FROM pdm_signatures WHERE user = ? AND id IN ({placeholders})",
                        [user, *ids],
                    ).fetchall()
                    existing_ids = {row["id"] for row in existing_rows}

                    for index, sig in user_chunk:
                        if sig.id in existing_ids:
                            results[index] = SaveManyResult(
                                index=index,
                                id=None,
                                error="Duplicate id already exists",
                            )
                            continue
                        write_rows.append((index, sig))
                        drawer_rows.append((sig.drawer_domain, sig.user, ""))

                if not write_rows:
                    continue

                conn.executemany(
                    _INSERT_SIGNATURE_SQL,
                    [signature_insert_row(sig, store_raw=self.store_raw) for _, sig in write_rows],
                )
                conn.executemany(_INSERT_DRAWER_SQL, drawer_rows)
                for index, sig in write_rows:
                    results[index] = SaveManyResult(index=index, id=sig.id)

        return results

    def get(self, memory_id: str, user: str = "default") -> SignatureRecord | None:
        row = self._conn().execute(
            "SELECT * FROM pdm_signatures WHERE id = ? AND user = ? AND is_deleted = 0",
            (memory_id, user),
        ).fetchone()
        return mapping_to_record(row) if row else None

    def find_by_hash(self, text_hash: str, user: str = "default") -> SignatureRecord | None:
        row = self._conn().execute(
            "SELECT * FROM pdm_signatures WHERE user = ? AND compressed_fact_hash = ? "
            "AND is_deleted = 0 LIMIT 1",
            (user, text_hash),
        ).fetchone()
        return mapping_to_record(row) if row else None

    def find_by_idempotency_key(
        self,
        idempotency_key: str,
        user: str = "default",
    ) -> SignatureRecord | None:
        row = self._conn().execute(
            "SELECT * FROM pdm_signatures WHERE user = ? AND idempotency_key = ? "
            "AND is_deleted = 0 LIMIT 1",
            (user, idempotency_key.strip()),
        ).fetchone()
        return mapping_to_record(row) if row else None

    def ping(self) -> bool:
        try:
            row = self._conn().execute("SELECT 1 AS ok").fetchone()
            return bool(row and row["ok"] == 1)
        except Exception as exc:
            logger.warning("[PDM-SQLite] ping failed: %s", exc)
            return False

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
        self._commit_if_idle(self._conn())
        if cur.rowcount == 0:
            logger.warning(
                "[PDM-SQLite] update(%s) affected 0 rows (missing or wrong user=%s)",
                memory_id,
                user,
            )

    def update_batch(
        self,
        updates: builtins.list[tuple[str, dict]],
        user: str = "default",
    ) -> None:
        """Batch-update with the same whitelist + user scope as :meth:`update`."""
        if not updates:
            return

        grouped_updates: dict[tuple[str, ...], list[tuple[object, ...]]] = {}
        for memory_id, fields in updates:
            prepared = self._prepare_update_fields(fields)
            if not prepared:
                continue
            columns = tuple(sorted(prepared))
            grouped_updates.setdefault(columns, []).append(
                tuple(prepared[column] for column in columns) + (memory_id, user)
            )

        if not grouped_updates:
            return

        conn = self._conn()
        with self.transaction():
            for columns, rows in grouped_updates.items():
                set_clause = ", ".join(f"{col} = ?" for col in columns)
                cur = conn.executemany(
                    f"UPDATE pdm_signatures SET {set_clause} WHERE id = ? AND user = ?",
                    rows,
                )
                if cur.rowcount not in (-1, len(rows)):
                    logger.warning(
                        "[PDM-SQLite] update_batch touched %s/%s rows (user=%s columns=%s)",
                        cur.rowcount,
                        len(rows),
                        user,
                        list(columns),
                    )

    @staticmethod
    def _prepare_update_fields(fields: dict) -> dict:
        """
        Validate column names against whitelist and serialise JSON/datetime.

        Raises:
            ValueError: If any key is not an allowed column (blocks SQL injection).
        """
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
            if col == "intent_tags" or col == "metadata":
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

    def delete(self, memory_id: str, user: str = "default") -> None:
        conn = self._conn()
        conn.execute(
            "UPDATE pdm_signatures SET is_deleted = 1 WHERE id = ? AND user = ? AND is_deleted = 0",
            (memory_id, user),
        )
        self._commit_if_idle(conn)

    def hard_delete(self, memory_id: str, user: str = "default") -> None:
        conn = self._conn()
        conn.execute(
            "DELETE FROM pdm_signatures WHERE id = ? AND user = ?",
            (memory_id, user),
        )
        self._commit_if_idle(conn)

    def list(
        self,
        user: str = "default",
        limit: int = 100,
        min_pressure: float = 0.0,
        drawer: str | None = None,
        cursor_id: str | None = None,
        include_deleted: bool = False,
    ) -> builtins.list[SignatureRecord]:
        where = ["user = ?", "p_magnitude >= ?"]
        params: list = [user, min_pressure]
        if not include_deleted:
            where.append("is_deleted = 0")
        if drawer:
            where.append("drawer_domain = ?")
            params.append(drawer)
        if cursor_id:
            cursor = self._conn().execute(
                "SELECT p_magnitude FROM pdm_signatures WHERE id = ? AND user = ?",
                (cursor_id, user),
            ).fetchone()
            if cursor is not None:
                where.append("(p_magnitude < ? OR (p_magnitude = ? AND id < ?))")
                p_cursor = float(cursor["p_magnitude"])
                params.extend([p_cursor, p_cursor, cursor_id])

        query = (
            f"SELECT * FROM pdm_signatures WHERE {' AND '.join(where)} "
            "ORDER BY p_magnitude DESC, id DESC LIMIT ?"
        )
        params.append(limit)
        rows = self._conn().execute(query, params).fetchall()
        return [mapping_to_record(r) for r in rows]

    def list_drawers(self, user: str = "default") -> builtins.list[DrawerInfo]:
        rows = self._conn().execute(
            """
            SELECT
                d.domain,
                d.description,
                COUNT(s.id)       AS sig_count,
                AVG(s.p_magnitude) AS avg_pressure
            FROM pdm_drawers d
            LEFT JOIN pdm_signatures s
                ON s.drawer_domain = d.domain AND s.user = d.user AND s.is_deleted = 0
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
            "SELECT COUNT(*) FROM pdm_signatures WHERE user = ? AND is_deleted = 0",
            (user,),
        ).fetchone()
        return row[0] if row else 0
