"""One connection layer for the ledgers: the same behaviour on SQLite and Postgres."""

from __future__ import annotations

import multiprocessing as mp
import sys
import time
import uuid

import pytest

from pdm_memory._sqldb import Database, Row, is_postgres
from pdm_memory.guarded_effects import ConfigurationError
from tests.ledger_target import PG_URL, postgres_target

needs_pg = pytest.mark.skipif(not PG_URL, reason="PDM_TEST_POSTGRES_URL is not set")
SCHEMA = "CREATE TABLE IF NOT EXISTS t (a TEXT, b INTEGER);"
EVENTS = "CREATE TABLE IF NOT EXISTS events (n INTEGER PRIMARY KEY, kind TEXT NOT NULL);"


@pytest.fixture(params=["sqlite", pytest.param("postgres", marks=needs_pg)])
def target(request, tmp_path):
    if request.param == "sqlite":
        return tmp_path / "t.db"
    return postgres_target(f"{tmp_path}-{uuid.uuid4().hex}")


@pytest.fixture
def db(target):
    d = Database.open(target, lock_key="test")
    d.create_schema(SCHEMA)
    yield d
    d.close()


@pytest.fixture
def pg_target(tmp_path):
    if not PG_URL:
        pytest.skip("PDM_TEST_POSTGRES_URL is not set")
    return postgres_target(f"{tmp_path}-{uuid.uuid4().hex}")


def test_targets_are_told_apart(tmp_path):
    assert is_postgres("postgresql://h/db") and is_postgres("postgres://h/db")
    assert not is_postgres(tmp_path / "a.db") and not is_postgres("./postgresql.db")
    with pytest.raises(TypeError):
        Database()


def test_rows_read_by_index_name_and_dict(db):
    db.execute("INSERT INTO t VALUES (?, ?)", ("x", 1))
    row = db.execute("SELECT a, b FROM t").fetchone()
    assert isinstance(row, Row)
    assert (row[0], row["b"], dict(row), row.keys()) == ("x", 1, {"a": "x", "b": 1}, ["a", "b"])
    with pytest.raises(IndexError):
        row["missing"]


def test_percent_and_question_marks_in_values_round_trip(db):
    for value in ("50%?", '{"p": "10%"}', "%s", "?"):
        db.execute("INSERT INTO t VALUES (?, ?)", (value, 0))
        assert db.execute("SELECT a FROM t WHERE a=?", (value,)).fetchone()["a"] == value


def test_a_bool_is_stored_as_an_integer(db):
    db.execute("INSERT INTO t VALUES (?, ?)", ("flag", True))
    assert db.execute("SELECT b FROM t WHERE a='flag'").fetchone()[0] == 1


def test_a_transaction_commits_or_rolls_back_whole(db):
    db.begin()
    db.execute("INSERT INTO t VALUES ('a', 1)")
    assert db.in_transaction
    db.rollback()
    assert not db.in_transaction
    assert db.execute("SELECT COUNT(*) FROM t").fetchone()[0] == 0
    db.begin()
    db.execute("INSERT INTO t VALUES ('a', 1)")
    db.commit()
    assert not db.in_transaction
    assert db.execute("SELECT COUNT(*) FROM t").fetchone()[0] == 1


def test_append_only_tables_refuse_update_delete_and_truncate(db):
    db.create_schema(EVENTS, append_only={"events": "history is append-only"})
    db.create_schema(EVENTS, append_only={"events": "history is append-only"})  # idempotent
    db.execute("INSERT INTO events VALUES (1, 'x')")
    with pytest.raises(db.Error, match="append-only"):
        db.execute("DELETE FROM events")
    with pytest.raises(db.Error, match="append-only"):
        db.execute("UPDATE events SET kind='y'")
    if db.dialect == "postgres":
        with pytest.raises(db.Error, match="append-only"):
            db.execute("TRUNCATE events")
    assert db.execute("SELECT kind FROM events").fetchall() == [("x",)]


def test_an_ordered_table_reads_back_in_insert_order(db):
    db.create_schema("CREATE TABLE IF NOT EXISTS o (k TEXT, v TEXT);", ordered=("o",))
    db.create_schema("CREATE TABLE IF NOT EXISTS o (k TEXT, v TEXT);", ordered=("o",))
    for k in ("c", "a", "b"):
        db.execute("INSERT INTO o VALUES (?, ?)", (k, k))
    assert [r[0] for r in db.execute("SELECT k FROM o ORDER BY rowid")] == ["c", "a", "b"]
    assert [r["k"] for r in db.execute("SELECT * FROM o ORDER BY rowid DESC LIMIT 1")] == ["b"]


def test_columns_lists_a_tables_columns(db):
    assert db.columns("t") == {"a", "b"}
    assert db.columns("missing") == set()


def _writer(target, worker, rounds):
    d = Database.open(target, lock_key="test")
    out = []
    for _ in range(rounds):
        d.begin()
        n = d.execute("SELECT COALESCE(MAX(b), 0) FROM t").fetchone()[0]
        time.sleep(0.02)  # widen the gap between read and write
        d.execute("INSERT INTO t VALUES (?, ?)", (worker, n + 1))
        d.commit()
        out.append(n + 1)
    d.close()
    return out


def _opener(target):
    d = Database.open(target, lock_key="test")
    d.create_schema(SCHEMA + EVENTS, append_only={"events": "append-only"}, ordered=("t",))
    d.close()
    return True


def _pool():
    return mp.get_context("spawn").Pool(4)


def test_writers_are_serialized(db, target):
    with _pool() as pool:
        results = pool.starmap(_writer, [(target, f"w{i}", 5) for i in range(4)])
    assert sorted(n for r in results for n in r) == list(range(1, 21))


def test_two_processes_create_the_schema_at_once(target):
    with _pool() as pool:
        assert pool.map(_opener, [target] * 4) == [True] * 4
    d = Database.open(target, lock_key="test")
    assert d.columns("events") == {"n", "kind"}
    d.close()


# --- Postgres only -----------------------------------------------------------


def test_a_dropped_connection_loses_no_committed_record(pg_target):
    import psycopg

    d = Database.open(pg_target, lock_key="test")
    d.create_schema(SCHEMA)
    d.execute("INSERT INTO t VALUES ('kept', 1)")
    d.begin()
    d.execute("INSERT INTO t VALUES ('lost', 2)")
    pid = d.execute("SELECT pg_backend_pid()").fetchone()[0]
    with psycopg.connect(PG_URL, autocommit=True) as admin:
        admin.execute("SELECT pg_terminate_backend(%s)", (pid,))
    with pytest.raises(psycopg.OperationalError):
        d.commit()
    again = Database.open(pg_target, lock_key="test")
    assert [r[0] for r in again.execute("SELECT a FROM t")] == ["kept"]
    again.close()


def test_a_server_without_fsync_is_refused(pg_target, monkeypatch):
    import psycopg

    real = psycopg.Connection.execute

    def fake(self, query, *args, **kwargs):
        if query == "SHOW fsync":
            return real(self, "SELECT 'off'")
        return real(self, query, *args, **kwargs)

    monkeypatch.setattr(psycopg.Connection, "execute", fake)
    with pytest.raises(ConfigurationError, match="fsync=off"):
        Database.open(pg_target, lock_key="test")


@needs_pg
def test_an_unreachable_server_fails_at_open():
    import psycopg

    with pytest.raises(psycopg.OperationalError):
        Database.open("postgresql://nobody@127.0.0.1:1/x?connect_timeout=2", lock_key="t")


def test_postgres_without_psycopg_names_the_extra(monkeypatch):
    monkeypatch.setitem(sys.modules, "psycopg", None)
    with pytest.raises(ImportError, match=r"pdm-memory\[postgres\]"):
        Database.open("postgresql://h/db", lock_key="t")


def test_sqlite_never_needs_psycopg(tmp_path, monkeypatch):
    monkeypatch.setitem(sys.modules, "psycopg", None)
    d = Database.open(tmp_path / "a.db", lock_key="t")
    d.create_schema(SCHEMA)
    assert d.dialect == "sqlite"
    d.close()


def test_ledgers_in_different_schemas_do_not_share_a_writer_lock(pg_target, tmp_path):
    import threading

    other = postgres_target(f"{tmp_path}-other-{uuid.uuid4().hex}")
    a = Database.open(pg_target, lock_key="test")
    b = Database.open(other, lock_key="test")
    a.begin()
    done = []
    thread = threading.Thread(target=lambda: (b.begin(), b.commit(), done.append(True)), daemon=True)
    thread.start()
    thread.join(5)
    blocked = not done
    a.commit()
    thread.join(5)
    assert not blocked, "a ledger in another schema waited on this one's lock"
    a.close()
    b.close()


def test_a_wait_for_the_writer_lock_is_bounded_like_sqlites(pg_target):
    import psycopg

    holder = Database.open(pg_target, lock_key="test")
    waiter = Database.open(pg_target, lock_key="test")
    assert waiter.execute("SHOW lock_timeout").fetchone()[0] == "30s"
    waiter.execute("SET lock_timeout TO '1s'")  # the same bound, shortened for the test
    holder.begin()
    with pytest.raises(psycopg.errors.LockNotAvailable):
        waiter.begin()
    waiter.rollback()
    holder.commit()
    holder.close()
    waiter.close()
