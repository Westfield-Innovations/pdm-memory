"""Storage shared by the owner-held ledgers: one SQL layer for SQLite and Postgres,
the hash-chained event history, and the errors both raise.

Internal: users import the ledgers and their errors, not this package."""

from pdm_memory._ledger.chain import append_event, verify_events
from pdm_memory._ledger.db import Database, Row, is_postgres
from pdm_memory._ledger.errors import ConfigurationError, LedgerIntegrityError

__all__ = [
    "ConfigurationError",
    "Database",
    "LedgerIntegrityError",
    "Row",
    "append_event",
    "is_postgres",
    "verify_events",
]
