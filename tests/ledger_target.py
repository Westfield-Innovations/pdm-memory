"""Where a ledger test keeps its database: a file, or a Postgres schema.

With ``PDM_TEST_LEDGER_BACKEND=postgres`` and ``PDM_TEST_POSTGRES_URL`` set,
each (tmp_path, name) maps to its own schema, so reopening the same name
reopens the same ledger. Schemas are dropped when the test run ends.
"""

from __future__ import annotations

import atexit
import hashlib
import os
from pathlib import Path

PG_URL = os.environ.get("PDM_TEST_POSTGRES_URL")
ON_POSTGRES = os.environ.get("PDM_TEST_LEDGER_BACKEND") == "postgres"
_created: list[str] = []


def postgres_target(key: str) -> str:
    """A URL whose search_path is a fresh schema named after *key*."""
    import psycopg

    schema = "t_" + hashlib.sha1(key.encode()).hexdigest()[:16]
    with psycopg.connect(PG_URL, autocommit=True) as conn:
        conn.execute(f"CREATE SCHEMA IF NOT EXISTS {schema}")
    if not _created:
        atexit.register(_drop_schemas)
    if schema not in _created:
        _created.append(schema)
    return f"{PG_URL}{'&' if '?' in PG_URL else '?'}options=-csearch_path%3D{schema}"


def ledger_target(tmp_path: Path, name: str) -> str | Path:
    if not ON_POSTGRES:
        return tmp_path / name
    if not PG_URL:
        raise RuntimeError("PDM_TEST_LEDGER_BACKEND=postgres needs PDM_TEST_POSTGRES_URL")
    return postgres_target(str(tmp_path / name))


def _drop_schemas() -> None:
    import psycopg

    with psycopg.connect(PG_URL, autocommit=True) as conn:
        for schema in _created:
            conn.execute(f"DROP SCHEMA IF EXISTS {schema} CASCADE")
