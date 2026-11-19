"""Session-wide test setup."""

from __future__ import annotations

import pytest

from tests.ledger_target import drop_schemas


@pytest.fixture(scope="session", autouse=True)
def _drop_postgres_test_schemas():
    yield
    drop_schemas()
