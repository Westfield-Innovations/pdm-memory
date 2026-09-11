"""
SQLite driver with the evidence layer attached.

The whole of TKT-101's SDK half rides on three extension points the SDK
already publishes, so not one frozen module is edited:

* ``BaseStorage`` is declared an AUTHORIZED EXTENSION POINT — implementing it
  (here, by subclassing the shipped driver) is permitted.
* ``register_storage()`` overrides a built-in scheme, because ``create_storage``
  merges ``{**_BUILTIN_SCHEMES, **_CUSTOM_SCHEMES}`` and custom wins.
* ``Memory(storage=...)`` takes a pre-built driver outright.

So ``enable_events()`` is enough to make every ``Memory("./x.db")`` in a
process event-aware, and a caller who wants it in one place only passes the
driver directly instead.
"""

from __future__ import annotations

import logging
import sqlite3
from typing import Any

from pdm_memory.storage.event_store import EventStoreMixin
from pdm_memory.storage.field_store import FieldStore
from pdm_memory.storage.events import apply_event_migrations_sqlite
from pdm_memory.storage.fields import apply_field_migrations_sqlite
from pdm_memory.storage.sqlite_driver import SQLiteDriver

logger = logging.getLogger(__name__)

__all__ = ["EventfulSQLiteDriver", "enable_events"]


class EventfulSQLiteDriver(FieldStore, EventStoreMixin, SQLiteDriver):
    """``SQLiteDriver`` plus source events, entities and mentions."""

    _EVENT_PLACEHOLDER = "?"
    _EVENT_USER_COLUMN = "user"

    def __init__(self, db_path: str = "./pdm_memory.db", store_raw: bool = True) -> None:
        super().__init__(db_path=db_path, store_raw=store_raw)
        conn = self._conn()
        apply_event_migrations_sqlite(conn)
        apply_field_migrations_sqlite(conn)
        conn.commit()

    def _conn(self) -> sqlite3.Connection:
        """
        The stock connection, with foreign keys actually turned on.

        SQLite defaults ``foreign_keys`` to OFF, per connection. Without this
        the REFERENCES clauses on ``pdm_signatures`` parse, create no error,
        and enforce nothing — AC3 would pass review and fail in production.
        Connections are thread-local here, so the pragma belongs at creation
        rather than in ``__init__``, which only ever sees the first thread's.
        """
        existing = getattr(self._local, "conn", None)
        conn = super()._conn()
        if existing is None:
            conn.execute("PRAGMA foreign_keys=ON")
        return conn


# ---------------------------------------------------------------------------
# Opt-in wiring
# ---------------------------------------------------------------------------


def _build_eventful_sqlite(store: str, *, store_raw: bool = True, **_: Any):
    from pdm_memory.storage.factory import _sqlite_path_from_url

    db_path = _sqlite_path_from_url(store) if "://" in store else store
    return EventfulSQLiteDriver(db_path=db_path, store_raw=store_raw)


def _build_eventful_postgres(store: str, *, store_raw: bool = True, **_: Any):
    from pdm_memory.storage.eventful_postgres import EventfulPostgresDriver

    return EventfulPostgresDriver(dsn=store, store_raw=store_raw)


def enable_events() -> None:
    """
    Make ``Memory(store=...)`` event-aware for the rest of the process, on
    every local scheme.

    Opt-in rather than an import side effect: importing a module should not
    silently change what another module's constructor returns. Callers who
    want events in one place only can skip this and hand the driver to
    ``Memory(storage=EventfulSQLiteDriver(...))`` instead.
    """
    from pdm_memory.storage.factory import register_storage

    register_storage("sqlite", _build_eventful_sqlite)
    register_storage("file", _build_eventful_sqlite)
    register_storage("postgresql", _build_eventful_postgres)
    register_storage("postgres", _build_eventful_postgres)
    logger.debug("[PDM-Events] Local schemes now resolve to eventful drivers")
