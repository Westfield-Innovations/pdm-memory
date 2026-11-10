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
from collections.abc import Generator, Sequence
from contextlib import contextmanager
from datetime import datetime
from typing import Any

from pdm_memory.core.signature import DrawerInfo, LinkRow, SignatureRecord
from pdm_memory.storage.base import BaseStorage, SaveBatchResult, UpdateBatchResult
from pdm_memory.storage.schema import (
    CARRIED_SQL,
    SCHEMA_POSTGRES,
    apply_postgres_migrations,
    increment_set_clause,
    link_insert_row,
    mapping_to_link,
    mapping_to_record,
    prepare_update_fields,
    signature_insert_row,
    tag_words,
    write_tags,
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
    is_deleted, idempotency_key, validation_status,
    direction, outcome_source, due, watch_window, outcome, verdict, event_time, report_time
) VALUES (
    %s, %s, %s, %s, %s,
    %s, %s, %s, %s,
    %s, %s, %s, %s,
    %s, %s, %s,
    %s, %s,
    %s, %s, %s, %s, %s,
    %s, %s, %s,
    %s, %s, %s, %s, %s, %s, %s, %s
)
ON CONFLICT (id) DO NOTHING
RETURNING id
"""

_INSERT_DRAWER_SQL = """
INSERT INTO pdm_drawers (domain, "user", description)
VALUES (%s, %s, %s)
ON CONFLICT (domain, "user") DO NOTHING
"""


class PostgresDriver(BaseStorage):
    _CHUNK_SIZE = 500
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
    def transaction(self) -> Generator[None]:
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
        cur = conn.execute(_INSERT_SIGNATURE_SQL, signature_insert_row(sig, store_raw=self.store_raw))
        row = cur.fetchone() if hasattr(cur, "fetchone") else None
        if not row and hasattr(cur, "rowcount") and cur.rowcount == 0:
            raise ValueError(f"Signature record with id '{sig.id}' already exists")
        write_tags(conn, [(sig.user, sig.id, sig.intent_tags)], dialect="postgres")
        conn.execute(_INSERT_DRAWER_SQL, (sig.drawer_domain, sig.user, ""))
        self._commit_if_idle(conn)
        logger.debug("[PDM-Postgres] Saved signature %s (P=%.1f)", sig.id, sig.p_magnitude)
        return sig.id

    def save_batch(self, sigs: builtins.list[SignatureRecord]) -> builtins.list[SaveBatchResult]:
        if not sigs:
            return []

        results = [SaveBatchResult(index=i, id=None) for i in range(len(sigs))]
        seen_ids: set[str] = set()
        pending: list[tuple[int, SignatureRecord]] = []
        for index, sig in enumerate(sigs):
            if sig.id in seen_ids:
                results[index] = SaveBatchResult(
                    index=index, id=None, error="Duplicate id in batch",
                )
                continue
            seen_ids.add(sig.id)
            pending.append((index, sig))

        if not pending:
            return results

        conn = self._conn()
        with self.transaction():
            seen_drawers: set[tuple[str, str]] = set()

            for start in range(0, len(pending), self._CHUNK_SIZE):
                chunk = pending[start:start + self._CHUNK_SIZE]
                params = [
                    signature_insert_row(sig, store_raw=self.store_raw)
                    for _, sig in chunk
                ]

                cur = conn.cursor()
                cur.executemany(_INSERT_SIGNATURE_SQL, params, returning=True)

                rows: list[Any] = []
                while True:
                    row = cur.fetchone()
                    rows.append(row)
                    if not cur.nextset():
                        break

                write_tags(
                    conn,
                    [
                        (sig.user, sig.id, sig.intent_tags)
                        for (_, sig), row in zip(chunk, rows)
                        if row is not None
                    ],
                    dialect="postgres",
                )
                drawer_rows: list[tuple[str, str, str]] = []
                for (index, sig), row in zip(chunk, rows):
                    if row is not None:
                        drawer_key = (sig.drawer_domain, sig.user)
                        if drawer_key not in seen_drawers:
                            drawer_rows.append((sig.drawer_domain, sig.user, ""))
                            seen_drawers.add(drawer_key)
                        results[index] = SaveBatchResult(index=index, id=sig.id)
                    else:
                        results[index] = SaveBatchResult(
                            index=index, id=None, error="Duplicate id already exists",
                        )

                if drawer_rows:
                    conn.cursor().executemany(_INSERT_DRAWER_SQL, drawer_rows)

        return results

    def get(self, memory_id: str, user: str = "default") -> SignatureRecord | None:
        row = self._conn().execute(
            'SELECT * FROM pdm_signatures WHERE id = %s AND "user" = %s AND is_deleted = 0',
            (memory_id, user),
        ).fetchone()
        return mapping_to_record(row) if row else None

    def get_many(
        self,
        ids: builtins.list[str],
        user: str = "default",
    ) -> dict[str, SignatureRecord]:
        """Bulk-fetch via WHERE id IN (...), chunked by _CHUNK_SIZE.

        Strategy:
        1. De-dup *ids* (preserve order via dict.fromkeys).
        2. For each chunk of :attr:`_CHUNK_SIZE`, run one
           ``SELECT * … WHERE "user" = %s AND id IN (…) AND is_deleted = 0``.
        3. Return a dict keyed by id; missing / foreign-user ids are simply
           absent — no error, since "not found" is an expected outcome here.
        """
        if not ids:
            return {}

        unique_ids = list(dict.fromkeys(ids))
        result: dict[str, SignatureRecord] = {}
        conn = self._conn()

        for start in range(0, len(unique_ids), self._CHUNK_SIZE):
            chunk = unique_ids[start : start + self._CHUNK_SIZE]
            placeholders = ", ".join(["%s"] * len(chunk))
            rows = conn.execute(
                f'SELECT * FROM pdm_signatures WHERE "user" = %s AND id IN ({placeholders}) '
                "AND is_deleted = 0",
                [user, *chunk],
            ).fetchall()
            for row in rows:
                rec = mapping_to_record(row)
                result[rec.id] = rec

        return result

    def find_by_hash(self, text_hash: str, user: str = "default") -> SignatureRecord | None:
        row = self._conn().execute(
            'SELECT * FROM pdm_signatures WHERE "user" = %s AND compressed_fact_hash = %s '
            "AND is_deleted = 0 LIMIT 1",
            (user, text_hash),
        ).fetchone()
        return mapping_to_record(row) if row else None

    def find_by_hashes(
        self,
        hashes: builtins.list[str],
        user: str = "default",
    ) -> dict[str, SignatureRecord]:
        cleaned = [h.strip() for h in hashes if h and h.strip()]
        if not cleaned:
            return {}
        unique: list[str] = list(dict.fromkeys(cleaned))
        placeholders = ",".join("%s" for _ in unique)
        rows = self._conn().execute(
            f'SELECT * FROM pdm_signatures WHERE "user" = %s AND is_deleted = 0 '
            f"AND compressed_fact_hash IN ({placeholders})",
            [user, *unique],
        ).fetchall()
        result: dict[str, SignatureRecord] = {}
        for row in rows:
            rec = mapping_to_record(row)
            result[str(row["compressed_fact_hash"])] = rec
        return result

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

    def find_by_idempotency_keys(
        self,
        keys: builtins.list[str],
        user: str = "default",
    ) -> dict[str, SignatureRecord]:
        cleaned = [k.strip() for k in keys if k and str(k).strip()]
        if not cleaned:
            return {}
        unique: list[str] = list(dict.fromkeys(cleaned))
        placeholders = ",".join("%s" for _ in unique)
        rows = self._conn().execute(
            f'SELECT * FROM pdm_signatures WHERE "user" = %s AND is_deleted = 0 '
            f"AND idempotency_key IN ({placeholders})",
            [user, *unique],
        ).fetchall()
        result: dict[str, SignatureRecord] = {}
        for row in rows:
            key = row["idempotency_key"]
            if key:
                result[str(key)] = mapping_to_record(row)
        return result

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
        if cur.rowcount and "intent_tags" in fields:
            write_tags(self._conn(), [(user, memory_id, fields["intent_tags"])], dialect="postgres")
        self._commit_if_idle(self._conn())
        if cur.rowcount == 0:
            logger.warning(
                "[PDM-Postgres] update(%s) affected 0 rows (missing or wrong user=%s)",
                memory_id,
                user,
            )

    def increment(
        self,
        memory_id: str,
        user: str = "default",
        deltas: dict[str, int] | None = None,
        **fields,
    ) -> bool:
        """Add *deltas* to counters and set *fields* in one UPDATE."""
        set_clause, values = increment_set_clause(deltas or {}, fields, "%s")
        if not set_clause:
            return self.get(memory_id, user=user) is not None
        cur = self._conn().execute(
            f'UPDATE pdm_signatures SET {set_clause} WHERE id = %s AND "user" = %s',
            values + [memory_id, user],
        )
        self._commit_if_idle(self._conn())
        return cur.rowcount > 0

    def update_batch(
        self,
        updates: builtins.list[tuple[str, dict]],
        user: str = "default",
    ) -> builtins.list[UpdateBatchResult]:
        if not updates:
            return []

        results = [UpdateBatchResult(index=i, id=memory_id) for i, (memory_id, _) in enumerate(updates)]
        index_map = {memory_id: i for i, (memory_id, _) in enumerate(updates)}

        grouped: dict[tuple[str, ...], list[tuple[str, tuple]]] = {}
        for index, (memory_id, fields) in enumerate(updates):
            try:
                prepared = prepare_update_fields(fields)
                if not prepared:
                    continue
                columns = tuple(sorted(prepared))
                params = tuple(prepared[c] for c in columns) + (memory_id, user)
                grouped.setdefault(columns, []).append((memory_id, params))
            except Exception as exc:
                results[index] = UpdateBatchResult(index=index, id=memory_id, error=str(exc))

        if not grouped:
            return results

        conn = self._conn()
        with self.transaction():
            for columns, entries in grouped.items():
                set_clause = ", ".join(f"{col} = %s" for col in columns)
                sql = (
                    f'UPDATE pdm_signatures SET {set_clause} '
                    f'WHERE id = %s AND "user" = %s RETURNING id'
                )

                for start in range(0, len(entries), self._CHUNK_SIZE):
                    chunk = entries[start:start + self._CHUNK_SIZE]
                    params_list = [p for _, p in chunk]

                    cur = conn.cursor()
                    cur.executemany(sql, params_list, returning=True)

                    touched: set[str] = set()
                    while True:
                        row = cur.fetchone()
                        if row is not None:
                            mid = row["id"] if isinstance(row, dict) or hasattr(row, "keys") else row[0]
                            touched.add(mid)
                        if not hasattr(cur, "nextset") or not cur.nextset():
                            break

                    if len(touched) != len(chunk):
                        missing = [mid for mid, _ in chunk if mid not in touched]
                        for mid in missing:
                            idx = index_map[mid]
                            results[idx] = UpdateBatchResult(index=idx, id=mid, error="Memory not found or wrong user")
                        logger.warning(
                            "[PDM-Postgres] update_batch: %s/%s rows touched "
                            "(user=%s columns=%s); missing ids: %s",
                            len(touched), len(chunk), user, list(columns), missing[:20],
                        )

            write_tags(
                conn,
                [
                    (user, mid, fields["intent_tags"])
                    for i, (mid, fields) in enumerate(updates)
                    if "intent_tags" in fields and not results[i].error
                ],
                dialect="postgres",
            )

        return results

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
        conn.execute(
            'DELETE FROM pdm_links WHERE "user" = %s AND (pressure_id = %s OR neighbor_id = %s)',
            (user, memory_id, memory_id),
        )
        conn.execute(
            'DELETE FROM pdm_tags WHERE pressure_id = %s AND "user" = %s', (memory_id, user)
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
        tag_any: builtins.list[str] | tuple[str, ...] | None = None,
        min_carried: float = 0.0,
    ) -> builtins.list[SignatureRecord]:
        where = ['"user" = %s', "p_magnitude >= %s"]
        params: list[Any] = [user, min_pressure]
        if not include_deleted:
            where.append("is_deleted = 0")
        if min_carried > 0:
            where.append(f"{CARRIED_SQL} >= %s")
            params.append(min_carried)
        if drawer:
            where.append("drawer_domain = %s")
            params.append(drawer)
        tags = tag_words(tag_any)[:32]
        if tags:
            where.append(
                'id IN (SELECT pressure_id FROM pdm_tags WHERE "user" = %s '
                "AND word = ANY(%s))"
            )
            params.extend([user, tags])
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

    def write_links(self, user: str, rows: builtins.list[LinkRow]) -> int:
        if not rows:
            return 0
        cur = self._conn().cursor()
        cur.executemany(
            'INSERT INTO pdm_links ("user", pressure_id, neighbor_id, kind, status_at_write, '
            "v_at_write, written_at) VALUES (%s, %s, %s, %s, %s, %s, %s) ON CONFLICT DO NOTHING",
            [link_insert_row(user, link) for link in rows],
        )
        self._commit_if_idle(self._conn())
        return max(0, cur.rowcount)

    def links(
        self,
        user: str,
        ids: Sequence[str],
        *,
        side: str,
        kind: str | None = None,
    ) -> builtins.list[LinkRow]:
        column = "pressure_id" if side == "out" else "neighbor_id"
        ids = list(dict.fromkeys(ids))
        found: builtins.list[LinkRow] = []
        for i in range(0, len(ids), 500):
            chunk = ids[i : i + 500]
            sql = f'SELECT * FROM pdm_links WHERE "user" = %s AND {column} = ANY(%s)'
            params: list[Any] = [user, chunk]
            if kind:
                sql += " AND kind = %s"
                params.append(kind)
            found += [mapping_to_link(r) for r in self._conn().execute(sql, params)]
        return found

    def save_coverage_gap(
        self, user: str, source: str, gap_from: datetime, gap_to: datetime
    ) -> None:
        self._conn().execute(
            'INSERT INTO pdm_coverage_gaps ("user", source, gap_from, gap_to) VALUES (%s, %s, %s, %s)',
            (user, source, gap_from.isoformat(), gap_to.isoformat()),
        )
        self._commit_if_idle(self._conn())

    def count_below_carried(self, user: str, theta: float) -> int:
        row = self._conn().execute(
            'SELECT COUNT(*) FROM pdm_signatures WHERE "user" = %s AND is_deleted = 0 '
            f"AND {CARRIED_SQL} < %s",
            (user, theta),
        ).fetchone()
        return int(row[0]) if row else 0

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
