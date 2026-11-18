"""One SQL connection for the owner-held ledgers, on SQLite or Postgres.

``GuardedEffects``, ``ConsequenceLedger`` and the goal loop's job store write
SQLite-flavoured SQL with ``?`` placeholders. A ``postgresql://`` target runs
the same statements on Postgres with the same guarantees: one writer at a time
per ledger kind, append-only tables, and a commit that is durable on return.
"""

from __future__ import annotations

import hashlib
import re
import sqlite3
import time
from collections.abc import Mapping, Sequence
from pathlib import Path
from typing import Any, Literal

__all__ = ["Database", "Row", "is_postgres"]

_SCHEMES = ("postgresql://", "postgres://")


def is_postgres(target: str | Path) -> bool:
    return isinstance(target, str) and target.startswith(_SCHEMES)


class Row(tuple):
    """A result row read by position, by column name, or as ``dict(row)``."""

    _names: tuple[str, ...]

    def __new__(cls, values: Sequence[Any], names: Sequence[str]) -> Row:  # noqa: PYI034
        row = super().__new__(cls, values)
        row._names = tuple(names)
        return row

    def __getitem__(self, key: Any) -> Any:
        if isinstance(key, str):
            try:
                return super().__getitem__(self._names.index(key))
            except ValueError:
                raise IndexError(f"no column {key!r}") from None
        return super().__getitem__(key)

    def keys(self) -> list[str]:
        return list(self._names)


def _sqlite_row(cursor: sqlite3.Cursor, values: tuple[Any, ...]) -> Row:
    return Row(values, [d[0] for d in cursor.description])


def _pg_row(cursor: Any) -> Any:
    names = [c.name for c in cursor.description or ()]
    return lambda values: Row(values, names)


def _lock_id(key: str) -> int:
    return int.from_bytes(hashlib.sha256(key.encode()).digest()[:8], "big", signed=True)


def _wal(conn: sqlite3.Connection) -> None:
    # SQLite skips its busy timeout for this switch, so processes opening a new
    # file together see "database is locked"; wait as long as a write would.
    deadline = time.monotonic() + 30
    while True:
        try:
            conn.execute("PRAGMA journal_mode=WAL")
            return
        except sqlite3.OperationalError as exc:
            if "locked" not in str(exc) or time.monotonic() > deadline:
                raise
            time.sleep(0.05)


_APPEND_ONLY_FN = """
CREATE OR REPLACE FUNCTION pdm_append_only() RETURNS trigger LANGUAGE plpgsql AS $$
BEGIN RAISE EXCEPTION '%', TG_ARGV[0]; END $$;
"""


class Database:
    """A ledger's connection. ``begin`` takes the ledger's single-writer lock."""

    dialect: Literal["sqlite", "postgres"]
    Error: type[Exception]

    def __init__(self) -> None:
        raise TypeError("use Database.open")

    @classmethod
    def open(cls, target: str | Path, *, lock_key: str) -> Database:
        db = object.__new__(cls)
        db._lock_id = _lock_id(lock_key)
        if not is_postgres(target):
            db.dialect, db.Error = "sqlite", sqlite3.DatabaseError
            conn = sqlite3.connect(
                str(target), timeout=30, isolation_level=None, check_same_thread=False
            )
            conn.row_factory = _sqlite_row
            _wal(conn)
            conn.execute("PRAGMA synchronous=FULL")
            db._conn = conn
            return db
        try:
            import psycopg
        except ImportError:
            raise ImportError("install pdm-memory[postgres] for a postgresql:// target") from None
        db.dialect, db.Error = "postgres", psycopg.Error
        conn = psycopg.connect(target, autocommit=True, row_factory=_pg_row)
        try:
            if conn.execute("SHOW fsync").fetchone()[0] == "off":
                from pdm_memory.guarded_effects import ConfigurationError

                raise ConfigurationError(
                    "the Postgres server runs with fsync=off; ledger writes would not be durable"
                )
            conn.execute("SET synchronous_commit TO on")
        except BaseException:
            conn.close()
            raise
        db._conn = conn
        return db

    def _sql(self, sql: str) -> str:
        return sql if self.dialect == "sqlite" else sql.replace("%", "%%").replace("?", "%s")

    def execute(self, sql: str, params: Sequence[Any] = ()) -> Any:
        if self.dialect == "sqlite":
            return self._conn.execute(sql, params)
        # Postgres keeps bool and integer apart; SQLite stores a bool as 0 or 1.
        values = [int(p) if type(p) is bool else p for p in params]
        return self._conn.execute(self._sql(sql), values)

    def begin(self) -> None:
        if self.dialect == "sqlite":
            self._conn.execute("BEGIN IMMEDIATE")
            return
        # shortcut: one lock per ledger kind, as SQLite has one writer per file; key it by owner if owners start waiting on each other.
        self._conn.execute("BEGIN")
        self._conn.execute("SELECT pg_advisory_xact_lock(%s)", (self._lock_id,))

    def commit(self) -> None:
        self._conn.execute("COMMIT")

    def rollback(self) -> None:
        self._conn.execute("ROLLBACK")

    @property
    def in_transaction(self) -> bool:
        if self.dialect == "sqlite":
            return self._conn.in_transaction
        return self._conn.info.transaction_status != 0  # psycopg TransactionStatus.IDLE

    def create_schema(
        self,
        sql: str,
        *,
        append_only: Mapping[str, str] | None = None,
        ordered: Sequence[str] = (),
    ) -> None:
        """Create tables from SQLite DDL; *append_only* maps a table to its refusal message.

        *ordered* tables are read back ``ORDER BY rowid``; Postgres gets that column last.
        """
        append_only = append_only or {}
        if self.dialect == "sqlite":
            triggers = "".join(
                f"CREATE TRIGGER IF NOT EXISTS {t}_no_{op} BEFORE {op.upper()} ON {t}"
                f" BEGIN SELECT RAISE(ABORT, '{msg}'); END;\n"
                for t, msg in append_only.items()
                for op in ("update", "delete")
            )
            self._conn.executescript(sql + triggers)
            return
        self.begin()
        try:
            self._conn.execute(re.sub(r"\bINTEGER\b", "BIGINT", sql))
            for table in ordered:
                self._conn.execute(
                    f"ALTER TABLE {table} ADD COLUMN IF NOT EXISTS rowid"
                    " BIGINT GENERATED ALWAYS AS IDENTITY"
                )
            if append_only:
                self._conn.execute(_APPEND_ONLY_FN)
            for table, msg in append_only.items():
                for op, scope in (("update", "ROW"), ("delete", "ROW"), ("truncate", "STATEMENT")):
                    name = f"{table}_no_{op}"
                    exists = self._conn.execute(
                        "SELECT 1 FROM pg_trigger WHERE tgname=%s AND tgrelid=to_regclass(%s)",
                        (name, table),
                    ).fetchone()
                    if not exists:
                        self._conn.execute(
                            f"CREATE TRIGGER {name} BEFORE {op.upper()} ON {table}"
                            f" FOR EACH {scope} EXECUTE PROCEDURE pdm_append_only('{msg}')"
                        )
            self.commit()
        except BaseException:
            self.rollback()
            raise

    def columns(self, table: str) -> set[str]:
        if self.dialect == "sqlite":
            return {r["name"] for r in self._conn.execute(f"PRAGMA table_info({table})")}
        rows = self._conn.execute(
            "SELECT column_name FROM information_schema.columns"
            " WHERE table_schema=current_schema() AND table_name=%s",
            (table,),
        )
        return {r[0] for r in rows}

    def close(self) -> None:
        self._conn.close()
