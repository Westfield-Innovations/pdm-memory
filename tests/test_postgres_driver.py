"""Unit tests for PostgresDriver batch insert behavior."""

from __future__ import annotations

import threading

from pdm_memory.core.signature import SignatureRecord
from pdm_memory.storage.postgres_driver import PostgresDriver


def make_sig(text="Test memory", p_magnitude=60.0, user="test_user", drawer="test_drawer"):
    return SignatureRecord(
        user=user,
        compressed_fact=text,
        source="test",
        p_magnitude=p_magnitude,
        intent_tags=["test", "unit", "memory"],
        drawer_domain=drawer,
    )


class _FakeCursor:
    def __init__(self, rows=None, rowcount=0):
        self._rows = rows or []
        self.rowcount = rowcount

    def fetchall(self):
        return list(self._rows)

    def fetchone(self):
        return self._rows[0] if self._rows else None


class _FakePostgresCursor(_FakeCursor):
    def __init__(self, conn):
        super().__init__()
        self._conn = conn

    def executemany(self, sql, params_seq):
        result = self._conn.executemany(sql, params_seq)
        self.rowcount = result.rowcount
        return result


class _FakePostgresConnection:
    def __init__(self):
        self.calls: list[tuple[str, list[object]]] = []
        self.executemany_calls: list[tuple[str, list[tuple[object, ...]]]] = []
        self.signature_rows: list[tuple[object, ...]] = []
        self.drawer_rows: list[tuple[object, ...]] = []
        self.commits = 0
        self.rollbacks = 0

    def execute(self, sql, params=None):
        param_list = list(params or [])
        normalized_sql = " ".join(sql.split())
        self.calls.append((normalized_sql, param_list))

        if normalized_sql == "BEGIN":
            return _FakeCursor()

        if normalized_sql.startswith("SELECT id FROM pdm_signatures WHERE"):
            user = param_list[0]
            ids = set(param_list[1:])
            rows = [{"id": row[0]} for row in self.signature_rows if row[1] == user and row[0] in ids]
            return _FakeCursor(rows=rows, rowcount=len(rows))

        return _FakeCursor()

    def executemany(self, sql, params_seq):
        normalized_sql = " ".join(sql.split())
        rows = [tuple(params) for params in params_seq]
        self.executemany_calls.append((normalized_sql, rows))
        if normalized_sql.startswith("UPDATE pdm_signatures SET"):
            return _FakeCursor(rowcount=len(rows))
        if normalized_sql.startswith("INSERT INTO pdm_signatures"):
            self.signature_rows.extend(rows)
            return _FakeCursor(rowcount=len(rows))
        if normalized_sql.startswith("INSERT INTO pdm_drawers"):
            self.drawer_rows.extend(rows)
            return _FakeCursor(rowcount=len(rows))
        return _FakeCursor()

    def cursor(self):
        return _FakePostgresCursor(self)

    def commit(self):
        self.commits += 1

    def rollback(self):
        self.rollbacks += 1

    def close(self):
        return None


def _make_driver(conn: _FakePostgresConnection) -> PostgresDriver:
    driver = PostgresDriver.__new__(PostgresDriver)
    driver.dsn = "postgresql://tests/pdm"
    driver.store_raw = True
    driver._local = threading.local()
    driver._local.conn = conn
    return driver


class TestPostgresDriverSaveMany:
    def test_save_many_best_effort_uses_bulk_insert(self):
        conn = _FakePostgresConnection()
        driver = _make_driver(conn)

        sigs = [make_sig(text=f"Fact {i}") for i in range(10)]
        sigs[8].id = sigs[1].id
        sigs[9].id = sigs[3].id

        results = driver.save_many(sigs)

        assert [result.index for result in results] == list(range(10))
        assert sum(result.error is None for result in results) == 8
        assert sum(result.error is not None for result in results) == 2
        assert results[8].id is None
        assert "Duplicate id in batch" in results[8].error
        assert results[9].id is None
        assert "Duplicate id in batch" in results[9].error

        signature_inserts = [
            rows for sql, rows in conn.executemany_calls if sql.startswith("INSERT INTO pdm_signatures")
        ]
        drawer_inserts = [
            rows for sql, rows in conn.executemany_calls if sql.startswith("INSERT INTO pdm_drawers")
        ]

        assert len(signature_inserts) == 1
        assert len(drawer_inserts) == 1
        assert len(signature_inserts[0]) == 8
        assert len(drawer_inserts[0]) == 8
        assert conn.commits == 1
        assert conn.rollbacks == 0

    def test_save_many_chunks_signature_and_drawer_inserts(self):
        conn = _FakePostgresConnection()
        driver = _make_driver(conn)

        sigs = [
            make_sig(text=f"Fact {i}", drawer=f"drawer-{i}")
            for i in range(501)
        ]

        results = driver.save_many(sigs)

        assert all(result.error is None for result in results)

        signature_insert_params = [
            params
            for sql, params in conn.executemany_calls
            if sql.startswith("INSERT INTO pdm_signatures")
        ]
        drawer_insert_params = [
            params
            for sql, params in conn.executemany_calls
            if sql.startswith("INSERT INTO pdm_drawers")
        ]

        assert [len(params) for params in signature_insert_params] == [500, 1]
        assert [len(params) for params in drawer_insert_params] == [500, 1]


class TestPostgresDriverUpdateBatch:
    def test_update_batch_groups_rows_by_column_set(self):
        conn = _FakePostgresConnection()
        driver = _make_driver(conn)

        driver.update_batch(
            [
                ("sig-1", {"p_magnitude": 80.0, "retrieval_count": 4}),
                ("sig-2", {"retrieval_count": 9, "p_magnitude": 55.0}),
                ("sig-3", {"drawer_domain": "licenses"}),
            ],
            user="alice",
        )

        assert len(conn.executemany_calls) == 2
        first_sql, first_rows = conn.executemany_calls[0]
        second_sql, second_rows = conn.executemany_calls[1]

        assert 'UPDATE pdm_signatures SET p_magnitude = %s, retrieval_count = %s WHERE id = %s AND "user" = %s' == first_sql
        assert first_rows == [
            (80.0, 4, "sig-1", "alice"),
            (55.0, 9, "sig-2", "alice"),
        ]
        assert second_sql == (
            'UPDATE pdm_signatures SET drawer_domain = %s WHERE id = %s AND "user" = %s'
        )
        assert second_rows == [("licenses", "sig-3", "alice")]
        assert ("BEGIN", []) in conn.calls
        assert conn.commits == 1
        assert conn.rollbacks == 0
