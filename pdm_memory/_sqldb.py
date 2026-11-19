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
from functools import lru_cache
from pathlib import Path
from typing import Any, Literal

from pdm_memory._errors import ConfigurationError

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


_IDENT = re.compile(r"[a-z_][a-z0-9_]*\Z")
_LITERAL = re.compile(r"'(?:[^']|'')*'")


def _ident(name: str) -> str:
    """A table or trigger name written into SQL: plain lowercase identifiers only."""
    if not isinstance(name, str) or not _IDENT.match(name):
        raise ValueError(f"{name!r} is not a plain SQL identifier")
    return name


def _message(text: str) -> str:
    if not isinstance(text, str) or not text or "'" in text or not text.isprintable():
        raise ValueError(f"{text!r} cannot be a trigger message")
    return text


@lru_cache(maxsize=1024)
def _checked(sql: str) -> str:
    """Refuse SQL the Postgres translation would misread, on both backends alike."""
    if any("?" in literal for literal in _LITERAL.findall(sql)):
        raise ValueError("a ? inside a quoted SQL literal reads as a parameter; pass it as one")
    return sql


@lru_cache(maxsize=1024)
def _to_postgres(sql: str) -> str:
    return _checked(sql).replace("%", "%%").replace("?", "%s")


_APPEND_ONLY_FN = """
CREATE OR REPLACE FUNCTION pdm_append_only() RETURNS trigger LANGUAGE plpgsql AS $$
BEGIN RAISE EXCEPTION '%', TG_ARGV[0]; END $$;
"""


class Database:
    """A ledger's connection. ``begin`` takes the ledger's single-writer lock."""

    dialect: Literal["sqlite", "postgres"]
    Error: type[Exception]

    def __init__(self, target: str | Path, *, lock_key: str) -> None:
        self._lock_id = _lock_id(lock_key)
        if not is_postgres(target):
            self.dialect, self.Error = "sqlite", sqlite3.DatabaseError
            conn = sqlite3.connect(
                str(target), timeout=30, isolation_level=None, check_same_thread=False
            )
            conn.row_factory = _sqlite_row
            _wal(conn)
            conn.execute("PRAGMA synchronous=FULL")
            self._conn: Any = conn
            return
        try:
            import psycopg
        except ImportError:
            raise ImportError("install pdm-memory[postgres] for a postgresql:// target") from None
        self.dialect, self.Error = "postgres", psycopg.Error
        conn = psycopg.connect(target, autocommit=True, row_factory=_pg_row)
        try:
            if conn.execute("SHOW fsync").fetchone()[0] == "off":
                raise ConfigurationError(
                    "the Postgres server runs with fsync=off; ledger writes would not be durable"
                )
            conn.execute("SET synchronous_commit TO on")
            # A SQLite file has its own writer lock and waits 30 s for it; here the
            # schema plays the file, and a wait past 30 s raises instead of hanging.
            conn.execute("SET lock_timeout TO '30s'")
            schema = conn.execute("SELECT current_schema()").fetchone()[0]
            self._lock_id = _lock_id(f"{lock_key}:{schema}")
        except BaseException:
            conn.close()
            raise
        self._conn = conn

    def execute(self, sql: str, params: Sequence[Any] = ()) -> Any:
        if self.dialect == "sqlite":
            return self._conn.execute(_checked(sql), params)
        # Postgres keeps bool and integer apart; SQLite stores a bool as 0 or 1.
        values = [int(p) if type(p) is bool else p for p in params]
        return self._conn.execute(_to_postgres(sql), values)

    def begin(self) -> None:
        if self.dialect == "sqlite":
            self._conn.execute("BEGIN IMMEDIATE")
            return
        # shortcut: one writer per ledger kind and schema, as SQLite has one per file; key it by owner if owners start waiting on each other.
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
        append_only = {_ident(t): _message(m) for t, m in (append_only or {}).items()}
        ordered = [_ident(t) for t in ordered]
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
        _ident(table)
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
