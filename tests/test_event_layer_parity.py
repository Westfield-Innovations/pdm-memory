"""
Dialect parity, the facade, and event sync.

The Postgres half of this repo has no live database in CI — ``test_postgres_
driver.py`` runs entirely on fakes — so the plpgsql trigger and the quoting it
depends on would otherwise ship unverified. These tests check the two things
that actually break between dialects: how ``user`` is spelled, and whether a
function body survives the driver's habit of splitting DDL on ";".
"""

from __future__ import annotations

import pathlib
import re

import pytest

from pdm_memory import Memory
from pdm_memory.event_log import EventLog
from pdm_memory.storage.event_store import EventStoreMixin
from pdm_memory.storage.event_sync import EventSync
from pdm_memory.storage.events import (
    SCHEMA_EVENTS_POSTGRES,
    SCHEMA_EVENTS_SQLITE,
    TRIGGERS_EVENTS_POSTGRES,
    TRIGGERS_EVENTS_SQLITE,
    SourceEventRecord,
    apply_event_migrations_postgres,
)
from pdm_memory.storage.eventful_sqlite import EventfulSQLiteDriver
from pdm_memory.storage.sqlite_driver import SQLiteDriver


# ---------------------------------------------------------------------------
# A connection that records SQL instead of running it
# ---------------------------------------------------------------------------


class _EmptyCursor:
    def fetchone(self):
        return None

    def fetchall(self):
        return []


class _Recorder:
    def __init__(self):
        self.statements: list[str] = []

    def execute(self, sql, params=()):
        self.statements.append(sql)
        return _EmptyCursor()

    def commit(self):
        pass


class _FakePostgresHost(EventStoreMixin):
    """The mixin wired for the Postgres dialect, with no server behind it."""

    _EVENT_PLACEHOLDER = "%s"
    _EVENT_USER_COLUMN = '"user"'
    _EVENT_INTEGRITY_ERRORS = ()

    def __init__(self):
        self.recorder = _Recorder()

    def _conn(self):
        return self.recorder

    def _commit_if_idle(self, conn):
        pass


@pytest.fixture()
def pg_host() -> _FakePostgresHost:
    return _FakePostgresHost()


# ---------------------------------------------------------------------------
# Dialect
# ---------------------------------------------------------------------------


BARE_USER = re.compile(r'(?<!")\buser\b(?!")')


def _strip_comments(sql: str) -> str:
    return "\n".join(
        line for line in sql.splitlines() if not line.strip().startswith("--")
    )


class TestDialectParity:
    def test_postgres_ddl_quotes_every_user(self):
        """
        ``user`` is reserved in PostgreSQL. The stock schema quotes it in every
        occurrence for this reason; one bare column definition and the whole
        migration refuses to run.
        """
        leaked = BARE_USER.findall(_strip_comments(SCHEMA_EVENTS_POSTGRES))
        assert not leaked, f"{len(leaked)} unquoted `user` in the Postgres DDL"

    def test_sqlite_ddl_leaves_user_bare(self):
        """Parity in the other direction: the stock SQLite schema never quotes it."""
        assert BARE_USER.search(_strip_comments(SCHEMA_EVENTS_SQLITE))

    def test_both_dialects_declare_the_same_tables(self):
        tables = lambda sql: set(  # noqa: E731
            re.findall(r"CREATE TABLE IF NOT EXISTS (\w+)", sql)
        )
        assert tables(SCHEMA_EVENTS_SQLITE) == tables(SCHEMA_EVENTS_POSTGRES)

    def test_both_dialects_declare_the_same_indexes(self):
        indexes = lambda sql: set(re.findall(r"INDEX IF NOT EXISTS (\w+)", sql))  # noqa: E731
        assert indexes(SCHEMA_EVENTS_SQLITE) == indexes(SCHEMA_EVENTS_POSTGRES)

    def test_mention_id_columns_are_not_nullable_in_either_dialect(self):
        """The uniqueness index only works because these two are NOT NULL."""
        for sql in (SCHEMA_EVENTS_SQLITE, SCHEMA_EVENTS_POSTGRES):
            for column in ("source_event_id", "signature_id"):
                declaration = re.search(rf"{column}\s+TEXT([^,\n]*)", sql)
                assert declaration and "NOT NULL" in declaration.group(1)

    def test_plpgsql_bodies_are_whole_statements(self):
        """
        Regression against the Postgres driver's DDL habit: it executes schema
        strings by splitting on ";". A function body is full of semicolons, so
        the triggers are handed over as a tuple of complete statements and must
        never go through that split.
        """
        function_bodies = [s for s in TRIGGERS_EVENTS_POSTGRES if "$$" in s]
        assert function_bodies, "no plpgsql function found"
        for body in function_bodies:
            assert body.count(";") > 1, "a body this simple would not need the guard"
            assert body.rstrip().endswith("LANGUAGE plpgsql;")

    def test_both_dialects_guard_the_same_columns(self):
        """
        The erasure carve-out has to match, or the same row is rewritable on
        one backend and frozen on the other.
        """
        for frozen in ("event_type", "occurred_at", "content_hash"):
            assert frozen in TRIGGERS_EVENTS_SQLITE
            assert any(frozen in s for s in TRIGGERS_EVENTS_POSTGRES)
        for erasable in ("provenance", "raw_reference", "compliance_state"):
            assert erasable not in TRIGGERS_EVENTS_SQLITE
            assert not any(erasable in s for s in TRIGGERS_EVENTS_POSTGRES)

    def test_postgres_migration_orders_tables_columns_indexes_triggers(self):
        recorder = _Recorder()
        apply_event_migrations_postgres(recorder)
        joined = "\n".join(recorder.statements)

        first_alter = joined.index("ALTER TABLE pdm_signatures")
        first_trigger = joined.index("CREATE TRIGGER")
        assert first_alter < first_trigger, "a trigger before its ALTER aborts the migration"
        assert "ADD COLUMN IF NOT EXISTS" in joined

    def test_generated_sql_uses_postgres_placeholders(self, pg_host):
        pg_host.find_event_by_hash("abc", user="u")
        sql = pg_host.recorder.statements[-1]
        assert '"user" = %s' in sql
        assert "?" not in sql

    def test_generated_insert_quotes_user(self, pg_host):
        pg_host.save_source_event(SourceEventRecord(event_type="chat_message"))
        insert = next(
            s for s in pg_host.recorder.statements if "INSERT INTO pdm_source_events" in s
        )
        assert '"user"' in insert
        assert "?" not in insert
        assert insert.count("%s") == 12

    def test_count_query_uses_an_alias_not_a_positional(self, pg_host):
        """
        psycopg's dict_row has no row[0]. Every aggregate the mixin reads back
        is aliased so the same code works on sqlite3.Row and on a dict.
        """
        pg_host.recorder.statements.clear()
        try:
            pg_host.resolve_or_create_entity(user="u", surface_form="Alex", field_id="w")
        except TypeError:
            pass  # the empty cursor cannot satisfy the whole flow; SQL is what matters
        assert any("COUNT(*) AS n" in s for s in pg_host.recorder.statements)


# ---------------------------------------------------------------------------
# The facade
# ---------------------------------------------------------------------------


@pytest.fixture()
def log(tmp_path: pathlib.Path):
    mem = Memory(storage=EventfulSQLiteDriver(db_path=str(tmp_path / "log.db")))
    yield EventLog(mem)
    mem.close()


class TestEventLog:
    def test_refuses_a_driver_without_events(self, tmp_path):
        mem = Memory(storage=SQLiteDriver(db_path=str(tmp_path / "plain.db")))
        with pytest.raises(RuntimeError, match="does not carry source events"):
            EventLog(mem)
        mem.close()

    def test_ingest_writes_one_event_and_many_signatures(self, log):
        result = log.ingest(
            event=log.event("chat_message", raw_reference="chat:123:msg:456"),
            payload="Moved the Orion release review to Friday. Alex is on it.",
            facts=[
                {"text": "Orion release moved to Friday",
                 "tags": ["orion", "release", "date"]},
                {"text": "Alex owns the Orion release",
                 "tags": ["orion", "alex", "owner"], "about": "Alex"},
            ],
            field_id="work",
        )

        assert len(result["signature_ids"]) == 2
        assert result["deduplicated"] is False
        assert len(log.signatures_for(result["source_event_id"])) == 2
        assert "Alex" in result["entity_ids"]

    def test_re_ingest_reuses_the_event(self, log):
        args = {
            "payload": "Moved the Orion release review to Friday.",
            "facts": [{"text": "Orion release moved", "tags": ["orion", "release", "date"]}],
            "field_id": "work",
        }
        first = log.ingest(event=log.event(raw_reference="chat:1"), **args)
        second = log.ingest(event=log.event(raw_reference="chat:1"), **args)

        assert first["source_event_id"] == second["source_event_id"]
        assert second["deduplicated"] is True
        assert len(log.events()) == 1

    def test_about_reads_back_everything_on_one_identity(self, log):
        result = log.ingest(
            event=log.event(raw_reference="chat:2"),
            payload="Alex is on the release.",
            facts=[{"text": "Alex owns the Orion release",
                    "tags": ["orion", "alex", "owner"], "about": "Alex"}],
            field_id="work",
        )
        entity_id = result["entity_ids"]["Alex"]
        assert [s.compressed_fact for s in log.about(entity_id)] == [
            "Alex owns the Orion release"
        ]

    def test_two_alexes_in_two_fields_stay_apart(self, log):
        work = log.ingest(
            event=log.event(raw_reference="chat:work"),
            payload="Release review with Alex.",
            facts=[{"text": "Alex reviewed the release",
                    "tags": ["release", "alex", "review"], "about": "Alex"}],
            field_id="work",
        )
        family = log.ingest(
            event=log.event(raw_reference="chat:family"),
            payload="Lunch with Alex.",
            facts=[{"text": "Lunch with Alex on Sunday",
                    "tags": ["lunch", "alex", "sunday"], "about": "Alex"}],
            field_id="family",
        )
        assert work["entity_ids"]["Alex"] != family["entity_ids"]["Alex"]
        assert len(log.about(work["entity_ids"]["Alex"])) == 1

    def test_unresolved_mention_waits_in_the_queue(self, log):
        event_id = log.record(log.event(raw_reference="chat:3"))
        assert log.mention("Alex", source_event_id=event_id, resolve=False) is None
        assert len(log.pending()) == 1

    def test_confirmation_beats_a_later_automatic_pass(self, log):
        event_id = log.record(log.event(raw_reference="chat:4"))
        log.mention("Alex", field_id="work", source_event_id=event_id, resolve=False)
        pending = log.pending()[0]

        other = log.mention("Alex", field_id="family", source_event_id=event_id,
                            signature_id="s-other")
        log.confirm(pending.id, other)

        # An automatic pass now tries to claim the same mention.
        log.mention("Alex", field_id="work", source_event_id=event_id)
        assert log._storage.get_mention(pending.id).resolution == "user_confirmed"

    def test_merge_is_reversible_evidence(self, log):
        work = log.mention("Alex", field_id="work", source_event_id="", signature_id="s1")
        family = log.mention("Alex", field_id="family", source_event_id="", signature_id="s2")
        log.merge(work, family)

        closed = log.entity(family)
        assert closed.dissolved_at is not None and closed.merged_into == work
        assert [m.entity_id for m in log._storage.mentions_for_entity(work)].count(work) == 2


# ---------------------------------------------------------------------------
# Event sync
# ---------------------------------------------------------------------------


class TestEventSync:
    def test_push_moves_events_and_dedupes_on_the_far_side(self, tmp_path):
        left = EventfulSQLiteDriver(db_path=str(tmp_path / "left.db"))
        right = EventfulSQLiteDriver(db_path=str(tmp_path / "right.db"))

        for ref in ("chat:1", "chat:2", "chat:3"):
            left.save_source_event(SourceEventRecord(raw_reference=ref), payload=ref)

        report = EventSync(left, right).sync(direction="push")
        assert report.events_pushed == 3
        assert report.errors == 0

        # Running it again moves nothing: identity is the hash, so there is
        # nothing to reconcile, only something to skip.
        again = EventSync(left, right).sync(direction="push")
        assert again.events_pushed == 0
        assert again.events_deduplicated == 3

        left.close()
        right.close()

    def test_sync_reports_a_store_that_cannot_carry_events(self, tmp_path):
        eventful = EventfulSQLiteDriver(db_path=str(tmp_path / "a.db"))
        plain = SQLiteDriver(db_path=str(tmp_path / "b.db"))

        report = EventSync(eventful, plain).sync(direction="push")
        assert report.unsupported == ["cloud"]
        assert report.errors == 0

        eventful.close()
        plain.close()

    def test_pull_brings_events_back(self, tmp_path):
        local = EventfulSQLiteDriver(db_path=str(tmp_path / "local.db"))
        cloud = EventfulSQLiteDriver(db_path=str(tmp_path / "cloud.db"))
        cloud.save_source_event(SourceEventRecord(raw_reference="chat:9"), payload="x")

        report = EventSync(local, cloud).sync(direction="pull")
        assert report.events_pulled == 1
        assert len(local.list_source_events()) == 1

        local.close()
        cloud.close()


# ---------------------------------------------------------------------------
# Cloud delegation
# ---------------------------------------------------------------------------


class TestCloudDelegation:
    def test_append_only_is_refused_before_the_round_trip(self):
        from pdm_memory.storage.eventful_cloud import EventfulCloudDriver
        from pdm_memory.storage.events import AppendOnlyViolation

        driver = object.__new__(EventfulCloudDriver)
        with pytest.raises(AppendOnlyViolation):
            driver.update_source_event("evt", event_type="email")
        with pytest.raises(AppendOnlyViolation):
            driver.delete_source_event("evt")

    def test_event_payload_carries_the_hash_and_not_the_content(self):
        from pdm_memory.storage.eventful_cloud import EventfulCloudDriver

        event = SourceEventRecord(raw_reference="chat:123:msg:456")
        event.ensure_content_hash(payload="the actual message text")
        payload = EventfulCloudDriver.event_payload(event)

        assert payload["content_hash"] == event.content_hash
        assert payload["raw_reference"] == "chat:123:msg:456"
        assert "the actual message text" not in str(payload)

    def test_payload_round_trips_through_the_wire_shape(self):
        from pdm_memory.storage.eventful_cloud import EventfulCloudDriver

        event = SourceEventRecord(
            raw_reference="chat:7", provenance={"channel": "general"}
        )
        event.ensure_content_hash(payload="hello")
        restored = EventfulCloudDriver.event_from_payload(
            {**EventfulCloudDriver.event_payload(event), "id": event.id}
        )
        assert restored.content_hash == event.content_hash
        assert restored.provenance == {"channel": "general"}
        assert restored.occurred_at == event.occurred_at
