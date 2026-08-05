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
from collections.abc import Generator
from contextlib import contextmanager
from datetime import datetime, timezone

from pdm_memory.core.signature import DrawerInfo, SignatureRecord
from pdm_memory.storage.base import BaseStorage, SaveBatchResult, UpdateBatchResult
from pdm_memory.storage.schema import (
    SCHEMA_SQLITE,
    apply_sqlite_migrations,
    mapping_to_record,
    prepare_update_fields,
    signature_insert_row,
)

logger = logging.getLogger(__name__)


_INSERT_SIGNATURE_SQL = """
INSERT OR IGNORE INTO pdm_signatures (
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


_BULK_INSERT_SIGNATURE_SQL = """
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
) ON CONFLICT(id) DO NOTHING
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

    _CHUNK_SIZE = 500

    def __init__(self, db_path: str = "./pdm_memory.db", store_raw: bool = True) -> None:
        self.db_path = db_path
        self.store_raw = store_raw
        self._local = threading.local()
        conn = self._conn()
        conn.execute("PRAGMA journal_mode=WAL")
        conn.executescript(SCHEMA_SQLITE)
        apply_sqlite_migrations(conn)
        conn.commit()
        logger.debug("[PDM-SQLite] Opened %s (store_raw=%s)", db_path, store_raw)

    @contextmanager
    def transaction(self) -> Generator[None]:
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

    def _find_existing_ids(
        self,
        conn: sqlite3.Connection,
        user: str,
        ids: builtins.list[str],
    ) -> set[str]:
        """Return existing signature IDs for *user* among *ids* in chunks."""
        if not ids:
            return set()

        existing_ids: set[str] = set()
        chunk_size = self._CHUNK_SIZE
        for chunk_start in range(0, len(ids), chunk_size):
            chunk_ids = ids[chunk_start : chunk_start + chunk_size]
            placeholders = ",".join("?" * len(chunk_ids))
            rows = conn.execute(
                f"SELECT id FROM pdm_signatures WHERE user = ? AND id IN ({placeholders})",
                [user, *chunk_ids],
            ).fetchall()
            existing_ids.update(row[0] for row in rows)
        return existing_ids


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

    def save_batch(self, sigs: builtins.list[SignatureRecord]) -> builtins.list[SaveBatchResult]:
        """Bulk-insert *sigs* in chunks of :attr:`_CHUNK_SIZE`.

        Strategy (SQLite-specific):

        1. **In-batch dedup** — duplicate *id* values within *sigs* itself are
           rejected early (``"Duplicate id in batch"``).
        2. **Pre-check SELECT** — for each chunk, one ``SELECT id … WHERE id IN
           (…)`` query discovers ids already present in the DB; those receive
           ``SaveBatchResult(id=None, error="Duplicate id already exists")``.
        3. **executemany INSERT … ON CONFLICT(id) DO NOTHING** — true bulk
           insert for the survivors.  The ``ON CONFLICT`` clause is a silent
           safety net for any rare race between steps 2 and 3; it prevents
           ``IntegrityError`` from aborting the whole transaction.
        4. If ``cur.rowcount`` for a chunk is less than expected (race
           occurred), a ``WARNING`` is emitted.  Per-row attribution is not
           possible without ``RETURNING`` per-row support in sqlite3.
        5. Drawers for inserted sigs are upserted via a single ``executemany``.
        """
        if not sigs:
            return []

        results = [SaveBatchResult(index=i, id=None) for i in range(len(sigs))]

        seen_ids: set[str] = set()
        pending: list[tuple[int, SignatureRecord]] = []
        for index, sig in enumerate(sigs):
            if sig.id in seen_ids:
                results[index] = SaveBatchResult(
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
        chunk_size = self._CHUNK_SIZE

        with self.transaction():
            seen_drawers: set[tuple[str, str]] = set()

            for chunk_start in range(0, len(pending), chunk_size):
                chunk = pending[chunk_start : chunk_start + chunk_size]

                chunk_by_user: dict[str, list[tuple[int, SignatureRecord]]] = {}
                for idx, sig in chunk:
                    chunk_by_user.setdefault(sig.user, []).append((idx, sig))

                existing_ids: set[str] = set()
                for user, user_items in chunk_by_user.items():
                    ids_for_user = [sig.id for _, sig in user_items]
                    existing_ids.update(self._find_existing_ids(conn, user, ids_for_user))

                to_insert: list[tuple[int, SignatureRecord]] = []
                for idx, sig in chunk:
                    if sig.id in existing_ids:
                        results[idx] = SaveBatchResult(
                            index=idx,
                            id=None,
                            error="Duplicate id already exists",
                        )
                    else:
                        to_insert.append((idx, sig))

                if not to_insert:
                    continue

                insert_rows = [
                    signature_insert_row(sig, store_raw=self.store_raw)
                    for _, sig in to_insert
                ]
                cur = conn.executemany(_BULK_INSERT_SIGNATURE_SQL, insert_rows)

                if cur.rowcount != len(to_insert):
                    logger.warning(
                        "[PDM-SQLite] save_batch chunk inserted %d/%d rows "
                        "(possible race on ids: %s)",
                        cur.rowcount,
                        len(to_insert),
                        [sig.id for _, sig in to_insert],
                    )

                for idx, sig in to_insert:
                    results[idx] = SaveBatchResult(index=idx, id=sig.id)
                    drawer_key = (sig.drawer_domain, sig.user)
                    seen_drawers.add(drawer_key)

            if seen_drawers:
                conn.executemany(
                    _INSERT_DRAWER_SQL,
                    [(domain, user, "") for domain, user in seen_drawers],
                )

        return results

    def get(self, memory_id: str, user: str = "default") -> SignatureRecord | None:
        row = self._conn().execute(
            "SELECT * FROM pdm_signatures WHERE id = ? AND user = ? AND is_deleted = 0",
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
           ``SELECT * … WHERE user = ? AND id IN (…) AND is_deleted = 0``.
           The ``idx_pdm_user_id`` covering index ensures O(k log N) lookup.
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
            placeholders = ",".join("?" * len(chunk))
            rows = conn.execute(
                f"SELECT * FROM pdm_signatures WHERE user = ? AND id IN ({placeholders}) "
                "AND is_deleted = 0",
                [user, *chunk],
            ).fetchall()
            for row in rows:
                rec = mapping_to_record(row)
                result[rec.id] = rec

        return result

    def find_by_hash(self, text_hash: str, user: str = "default") -> SignatureRecord | None:
        row = self._conn().execute(
            "SELECT * FROM pdm_signatures WHERE user = ? AND compressed_fact_hash = ? "
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
        # Deduplicate while preserving order for stable tests
        unique: list[str] = list(dict.fromkeys(cleaned))
        placeholders = ",".join("?" for _ in unique)
        rows = self._conn().execute(
            f"SELECT * FROM pdm_signatures WHERE user = ? AND is_deleted = 0 "
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
            "SELECT * FROM pdm_signatures WHERE user = ? AND idempotency_key = ? "
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
        placeholders = ",".join("?" for _ in unique)
        rows = self._conn().execute(
            f"SELECT * FROM pdm_signatures WHERE user = ? AND is_deleted = 0 "
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
            logger.warning("[PDM-SQLite] ping failed: %s", exc)
            return False

    def update(self, memory_id: str, user: str = "default", **fields) -> None:
        """Update whitelisted columns for ``memory_id`` owned by ``user``."""
        prepared = prepare_update_fields(fields)
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
    ) -> builtins.list[UpdateBatchResult]:
        """Batch-update with the same whitelist + user scope as :meth:`update`.

        Strategy (SQLite-specific — no per-row RETURNING from executemany):

        1. **Pre-check SELECT** — one query per chunk discovers which
           *memory_id* values actually exist in the DB for *user*.  Missing
           ids are immediately assigned
           ``UpdateBatchResult(error="Memory not found or wrong user")``.
        2. **executemany UPDATE** — runs only for confirmed-existing ids.
           A ``rowcount`` warning fires on the rare race between steps 1 and 2.
        """
        if not updates:
            return []

        results = [
            UpdateBatchResult(index=i, id=memory_id)
            for i, (memory_id, _) in enumerate(updates)
        ]
        index_map: dict[str, int] = {
            memory_id: i for i, (memory_id, _) in enumerate(updates)
        }
        grouped_updates: dict[tuple[str, ...], list[tuple[str, tuple]]] = {}
        for index, (memory_id, fields) in enumerate(updates):
            try:
                prepared = prepare_update_fields(fields)
                if not prepared:
                    continue
                columns = tuple(sorted(prepared))
                params = tuple(prepared[col] for col in columns) + (memory_id, user)
                grouped_updates.setdefault(columns, []).append((memory_id, params))
            except Exception as exc:
                results[index] = UpdateBatchResult(index=index, id=memory_id, error=str(exc))

        if not grouped_updates:
            return results

        all_ids: list[str] = list(
            {mid for entries in grouped_updates.values() for mid, _ in entries}
        )

        conn = self._conn()

        existing_ids = self._find_existing_ids(conn, user, all_ids)

        missing_ids = set(all_ids) - existing_ids
        for mid in missing_ids:
            idx = index_map[mid]
            results[idx] = UpdateBatchResult(
                index=idx,
                id=mid,
                error="Memory not found or wrong user",
            )
        with self.transaction():
            for columns, entries in grouped_updates.items():
                confirmed = [(mid, params) for mid, params in entries if mid in existing_ids]
                if not confirmed:
                    continue

                set_clause = ", ".join(f"{col} = ?" for col in columns)
                param_rows = [params for _, params in confirmed]
                cur = conn.executemany(
                    f"UPDATE pdm_signatures SET {set_clause} WHERE id = ? AND user = ?",
                    param_rows,
                )

                if cur.rowcount not in (-1, len(confirmed)):
                    logger.warning(
                        "[PDM-SQLite] update_batch touched %s/%s rows "
                        "(user=%s columns=%s); possible race on ids: %s",
                        cur.rowcount,
                        len(confirmed),
                        user,
                        list(columns),
                        [mid for mid, _ in confirmed],
                    )

        return results

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
        tag_any: builtins.list[str] | tuple[str, ...] | None = None,
    ) -> builtins.list[SignatureRecord]:
        where = ["user = ?", "p_magnitude >= ?"]
        params: list = [user, min_pressure]
        if not include_deleted:
            where.append("is_deleted = 0")
        if drawer:
            where.append("drawer_domain = ?")
            params.append(drawer)
        tags = [t.strip().lower() for t in (tag_any or ()) if t and str(t).strip()]
        if tags:
            # intent_tags stored as JSON array text — substring match on quoted token.
            tag_clauses: list[str] = []
            for tag in tags[:32]:
                tag_clauses.append("LOWER(intent_tags) LIKE ?")
                params.append(f'%"{tag}"%')
            where.append("(" + " OR ".join(tag_clauses) + ")")
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
