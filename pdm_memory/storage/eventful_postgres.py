"""
PostgreSQL driver with the evidence layer attached.

Parity with the SQLite driver, not a second implementation: the store methods
come from ``EventStoreMixin`` and only the dialect differs — ``%s`` for the
placeholder, a quoted ``"user"``, psycopg's own integrity error, and a plpgsql
trigger where SQLite uses ``RAISE(ABORT)``.

Postgres enforces foreign keys unconditionally, so there is no pragma to set
here; the SQLite driver's override exists only because SQLite does not.
"""

from __future__ import annotations

import logging

from pdm_memory.storage.event_store import EventStoreMixin
from pdm_memory.storage.field_store import FieldStore
from pdm_memory.storage.events import apply_event_migrations_postgres
from pdm_memory.storage.fields import apply_field_migrations_postgres
from pdm_memory.storage.postgres_driver import PostgresDriver

logger = logging.getLogger(__name__)

__all__ = ["EventfulPostgresDriver"]


class EventfulPostgresDriver(FieldStore, EventStoreMixin, PostgresDriver):
    """``PostgresDriver`` plus source events, entities and mentions."""

    _EVENT_PLACEHOLDER = "%s"
    _EVENT_USER_COLUMN = '"user"'

    def __init__(self, dsn: str, store_raw: bool = True) -> None:
        super().__init__(dsn=dsn, store_raw=store_raw)
        conn = self._conn()
        apply_event_migrations_postgres(conn)
        apply_field_migrations_postgres(conn)
        conn.commit()
