# © 2026 Westfield Innovations LLC. Patent Pending.
# U.S. App. No. 19/739,419 | 63/953,563 | 63/953,842
# MODIFICATION PROHIBITED. USE AS SHIPPED.

"""
PostgreSQL Storage Driver — server-grade PDM backend.

Requires: pip install "pdm-memory[postgres]"  (psycopg >= 3.1)

Usage:
    driver = PostgresDriver("postgresql://user:pass@localhost:5432/pdm")
    mem = Memory(store="postgresql://user:pass@localhost:5432/pdm")
"""

from __future__ import annotations

import builtins
import logging
import threading
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from datetime import datetime
from typing import Any

from pdm_memory.core.math import P_MAX, calculate_effective_spike
from pdm_memory.core.signature import DrawerInfo, SignatureRecord
from pdm_memory.storage.base import BaseStorage
from pdm_memory.storage.schema import (
    SCHEMA_POSTGRES,
    apply_postgres_migrations,
    mapping_to_record,
    prepare_update_fields,
    signature_insert_row,
)

logger = logging.getLogger(__name__)

_INSERT_SIGNATURE_SQL = """
INSERT INTO pdm_signatures (
    id, "user", compressed_fact, compressed_fact_hash, source,
    p_magnitude, t_persistence, phase_privilege, effective_spike,
    intent_tags, question_regime, domain, drawer_domain,
    retrieval_count, last_retrieved, created_at,
    validation_prediction_total, validation_prediction_correct,
    decay_rate, t_deadline, t_event_at, urgency_rate, metadata,
    is_deleted, idempotency_key
) VALUES (
    %s, %s, %s, %s, %s,
    %s, %s, %s, %s,
    %s, %s, %s, %s,
    %s, %s, %s,
    %s, %s,
    %s, %s, %s, %s, %s,
    %s, %s
)
"""

_INSERT_DRAWER_SQL = """
INSERT INTO pdm_drawers (domain, "user", description)
VALUES (%s, %s, %s)
ON CONFLICT (domain, "user") DO NOTHING
"""


class PostgresDriver(BaseStorage):
    """
    PostgreSQL-backed PDM storage.

    Args:
        dsn:       psycopg connection string (postgresql://…).
        store_raw: If False, only SHA-256 hashes of text are persisted.
    """

    def __init__(self, dsn: str, store_raw: bool = True) -> None:
        try:
            import psycopg
            from psycopg.rows import dict_row
        except ImportError as exc:
            raise ImportError(
                'PostgreSQL storage requires psycopg. '
                'Install with: pip install "pdm-memory[postgres]"'
            ) from exc

        self.dsn = dsn
        self.store_raw = store_raw
        self._local = threading.local()
        self._psycopg = psycopg
        self._dict_row = dict_row
        conn = self._conn()
        for statement in SCHEMA_POSTGRES.split(";"):
            stmt = statement.strip()
            if stmt:
                conn.execute(stmt)
        apply_postgres_migrations(conn)
        self._commit_if_idle(conn)
        logger.debug("[PDM-Postgres] Connected (store_raw=%s)", store_raw)

    @contextmanager
    def transaction(self) -> Iterator[None]:
        conn = self._conn()
        depth = getattr(self._local, "txn_depth", 0)
        if depth == 0:
            conn.execute("BEGIN")
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

    def _conn(self) -> Any:
        if not hasattr(self._local, "conn") or self._local.conn is None:
            self._local.conn = self._psycopg.connect(
                self.dsn,
                autocommit=False,
                row_factory=self._dict_row,
            )
        return self._local.conn

    def _commit_if_idle(self, conn: Any) -> None:
        if getattr(self._local, "txn_depth", 0) == 0:
            conn.commit()

    def close(self) -> None:
        if hasattr(self._local, "conn") and self._local.conn is not None:
            self._local.conn.close()
            self._local.conn = None

    def save(self, sig: SignatureRecord) -> str:
        conn = self._conn()
        conn.execute(_INSERT_SIGNATURE_SQL, signature_insert_row(sig, store_raw=self.store_raw))
        conn.execute(_INSERT_DRAWER_SQL, (sig.drawer_domain, sig.user, ""))
        self._commit_if_idle(conn)
        logger.debug("[PDM-Postgres] Saved signature %s (P=%.1f)", sig.id, sig.p_magnitude)
        return sig.id

    def get(self, memory_id: str, user: str = "default") -> SignatureRecord | None:
        row = self._conn().execute(
            'SELECT * FROM pdm_signatures WHERE id = %s AND "user" = %s AND is_deleted = 0',
            (memory_id, user),
        ).fetchone()
        return mapping_to_record(row) if row else None

    def find_by_hash(self, text_hash: str, user: str = "default") -> SignatureRecord | None:
        row = self._conn().execute(
            'SELECT * FROM pdm_signatures WHERE "user" = %s AND compressed_fact_hash = %s '
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
            'SELECT * FROM pdm_signatures WHERE "user" = %s AND idempotency_key = %s '
            "AND is_deleted = 0 LIMIT 1",
            (user, idempotency_key.strip()),
        ).fetchone()
        return mapping_to_record(row) if row else None

    def ping(self) -> bool:
        try:
            row = self._conn().execute("SELECT 1 AS ok").fetchone()
            return bool(row and row["ok"] == 1)
        except Exception as exc:
            logger.warning("[PDM-Postgres] ping failed: %s", exc)
            return False

    def update(self, memory_id: str, user: str = "default", **fields) -> None:
        prepared = prepare_update_fields(fields)
        if not prepared:
            return

        set_clause = ", ".join(f"{col} = %s" for col in prepared)
        values = list(prepared.values()) + [memory_id, user]
        cur = self._conn().execute(
            f'UPDATE pdm_signatures SET {set_clause} WHERE id = %s AND "user" = %s',
            values,
        )
        self._commit_if_idle(self._conn())
        if cur.rowcount == 0:
            logger.warning(
                "[PDM-Postgres] update(%s) affected 0 rows (missing or wrong user=%s)",
                memory_id,
                user,
            )

    def atomic_reinforce(
        self,
        memory_id: str,
        user: str,
        *,
        compute_delta: Callable[[float, int], float],
        last_retrieved: datetime,
    ) -> None:
        """
        Row-locked reinforce: SELECT … FOR UPDATE then atomic counter increment.

        Prevents lost updates when multiple threads reinforce the same signature.
        """
        with self.transaction():
            conn = self._conn()
            row = conn.execute(
                """
                SELECT p_magnitude, retrieval_count, t_persistence, phase_privilege
                FROM pdm_signatures
                WHERE id = %s AND "user" = %s AND is_deleted = 0
                FOR UPDATE
                """,
                (memory_id, user),
            ).fetchone()
            if row is None:
                raise ValueError(
                    f"Memory '{memory_id}' not found for user '{user}'"
                )

            p_magnitude = float(row["p_magnitude"])
            retrieval_count = int(row["retrieval_count"] or 0)
            delta = compute_delta(p_magnitude, retrieval_count)
            new_p = min(P_MAX, p_magnitude + delta)
            new_spike = calculate_effective_spike(
                new_p,
                float(row["t_persistence"]),
                float(row["phase_privilege"]),
            )
            conn.execute(
                """
                UPDATE pdm_signatures
                SET p_magnitude = %s,
                    effective_spike = %s,
                    retrieval_count = retrieval_count + 1,
                    last_retrieved = %s
                WHERE id = %s AND "user" = %s AND is_deleted = 0
                """,
                (new_p, new_spike, last_retrieved, memory_id, user),
            )

    def update_batch(
        self,
        updates: builtins.list[tuple[str, dict]],
        user: str = "default",
    ) -> None:
        if not updates:
            return
        conn = self._conn()
        for memory_id, fields in updates:
            prepared = prepare_update_fields(fields)
            if not prepared:
                continue
            set_clause = ", ".join(f"{col} = %s" for col in prepared)
            values = list(prepared.values()) + [memory_id, user]
            cur = conn.execute(
                f'UPDATE pdm_signatures SET {set_clause} WHERE id = %s AND "user" = %s',
                values,
            )
            if cur.rowcount == 0:
                logger.warning(
                    "[PDM-Postgres] update_batch(%s) affected 0 rows (user=%s)",
                    memory_id,
                    user,
                )
        self._commit_if_idle(conn)

    def delete(self, memory_id: str, user: str = "default") -> None:
        conn = self._conn()
        conn.execute(
            'UPDATE pdm_signatures SET is_deleted = 1 WHERE id = %s AND "user" = %s '
            "AND is_deleted = 0",
            (memory_id, user),
        )
        self._commit_if_idle(conn)

    def hard_delete(self, memory_id: str, user: str = "default") -> None:
        conn = self._conn()
        conn.execute(
            'DELETE FROM pdm_signatures WHERE id = %s AND "user" = %s',
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
        where = ['"user" = %s', "p_magnitude >= %s"]
        params: list[Any] = [user, min_pressure]
        if not include_deleted:
            where.append("is_deleted = 0")
        if drawer:
            where.append("drawer_domain = %s")
            params.append(drawer)
        if cursor_id:
            cursor = self._conn().execute(
                'SELECT p_magnitude FROM pdm_signatures WHERE id = %s AND "user" = %s',
                (cursor_id, user),
            ).fetchone()
            if cursor is not None:
                where.append("(p_magnitude < %s OR (p_magnitude = %s AND id < %s))")
                p_cursor = float(cursor["p_magnitude"])
                params.extend([p_cursor, p_cursor, cursor_id])

        query = (
            f'SELECT * FROM pdm_signatures WHERE {" AND ".join(where)} '
            "ORDER BY p_magnitude DESC, id DESC LIMIT %s"
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
                COUNT(s.id)        AS sig_count,
                AVG(s.p_magnitude) AS avg_pressure
            FROM pdm_drawers d
            LEFT JOIN pdm_signatures s
                ON s.drawer_domain = d.domain AND s."user" = d."user" AND s.is_deleted = 0
            WHERE d."user" = %s
            GROUP BY d.domain, d.description
            ORDER BY d.domain
            """,
            (user,),
        ).fetchall()
        return [
            DrawerInfo(
                domain=r["domain"],
                signature_count=r["sig_count"] or 0,
                avg_pressure=round(float(r["avg_pressure"] or 0.0), 2),
                description=r["description"] or "",
            )
            for r in rows
        ]

    def count(self, user: str = "default") -> int:
        row = self._conn().execute(
            'SELECT COUNT(*) AS cnt FROM pdm_signatures WHERE "user" = %s AND is_deleted = 0',
            (user,),
        ).fetchone()
        return int(row["cnt"]) if row else 0
