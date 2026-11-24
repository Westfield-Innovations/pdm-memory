"""Where a ledger test keeps its database: a file, or a Postgres schema.

With ``PDM_TEST_LEDGER_BACKEND=postgres`` and ``PDM_TEST_POSTGRES_URL`` set,
each (tmp_path, name) maps to its own schema, so reopening the same name
reopens the same ledger. Schemas are dropped when the pytest session ends.
"""

from __future__ import annotations

import hashlib
import os
from pathlib import Path
from typing import Any

import pytest

PG_URL = os.environ.get("PDM_TEST_POSTGRES_URL")
ON_POSTGRES = os.environ.get("PDM_TEST_LEDGER_BACKEND") == "postgres"
ON_CLOUD = os.environ.get("PDM_TEST_LEDGER_BACKEND") == "cloud"
_created: list[str] = []
# For what only exists in a file: an older file's layout, a :memory: path.
sqlite_only = pytest.mark.skipif(ON_POSTGRES, reason="exercises a SQLite file")
# For what reads the ledger's own connection, which a cloud ledger does not have.
local_only = pytest.mark.skipif(ON_CLOUD, reason="reads the ledger's own connection")


def postgres_target(key: str) -> str:
    """A URL whose search_path is a fresh schema named after *key*."""
    import psycopg

    schema = "t_" + hashlib.sha1(key.encode()).hexdigest()[:16]
    with psycopg.connect(PG_URL, autocommit=True) as conn:
        conn.execute(f"CREATE SCHEMA IF NOT EXISTS {schema}")
    if schema not in _created:
        _created.append(schema)
    return f"{PG_URL}{'&' if '?' in PG_URL else '?'}options=-csearch_path%3D{schema}"


def ledger_target(tmp_path: Path, name: str) -> Any:
    if ON_CLOUD:
        from pdm_memory.cloud_ledgers.testing import in_process

        return in_process(tmp_path / name)
    if not ON_POSTGRES:
        return tmp_path / name
    if not PG_URL:
        raise RuntimeError(
            "PDM_TEST_LEDGER_BACKEND=postgres needs PDM_TEST_POSTGRES_URL"
        )
    return postgres_target(str(tmp_path / name))


def drop_schemas() -> None:
    """Drop every schema this run created; tests/conftest.py calls it once at the end."""
    if ON_CLOUD:
        from pdm_memory.cloud_ledgers.testing import close_hosts

        close_hosts()
    if not _created:
        return
    import psycopg

    with psycopg.connect(PG_URL, autocommit=True) as conn:
        for schema in _created:
            conn.execute(f"DROP SCHEMA IF EXISTS {schema} CASCADE")
    _created.clear()
