"""
The evidence layer against a real PostgreSQL, or skipped.

Everything Postgres-shaped in this feature — the plpgsql trigger bodies, the
DO block that swallows a duplicate, ON CONFLICT without a conflict target,
psycopg's rowcount — has only ever been asserted as strings. A recorder proves
the SQL was composed; it cannot prove the server accepts it.

Set PDM_TEST_POSTGRES_URL to run these. CI sets it from a service container.
"""

from __future__ import annotations

import os
import threading
import uuid

import pytest

from pdm_memory.core.signature import SignatureRecord
from pdm_memory.storage.events import (
    AppendOnlyViolation,
    EntityMentionRecord,
    SourceEventRecord,
)

DSN = os.environ.get("PDM_TEST_POSTGRES_URL")

pytestmark = pytest.mark.skipif(
    not DSN,
    reason=(
        "PDM_TEST_POSTGRES_URL is not set — skipping the live PostgreSQL suite. "
        "Example: export PDM_TEST_POSTGRES_URL=postgresql://postgres@localhost:5432/pdm_test"
    ),
)


@pytest.fixture()
def driver():
    pytest.importorskip("psycopg")
    from pdm_memory.storage.eventful_postgres import EventfulPostgresDriver

    drv = EventfulPostgresDriver(dsn=DSN)
    yield drv
    # Each test owns a fresh user, so the shared database needs no teardown
    # beyond dropping what this run wrote.
    drv.close()


@pytest.fixture()
def user() -> str:
    return f"t{uuid.uuid4().hex[:12]}"


def make_event(user: str, **kw) -> SourceEventRecord:
    base = {"user": user, "raw_reference": f"chat:{uuid.uuid4().hex[:8]}"}
    base.update(kw)
    return SourceEventRecord(**base)


class TestSchemaInstalls:
    def test_migration_runs_and_is_idempotent(self, driver):
        """A second driver on the same database must not fight the first."""
        from pdm_memory.storage.eventful_postgres import EventfulPostgresDriver

        second = EventfulPostgresDriver(dsn=DSN)
        second.close()

    def test_reserved_word_column_survived_the_ddl(self, driver, user):
        """`user` is reserved here; an unquoted one would have failed above."""
        assert driver.find_event_by_hash("nothing", user=user) is None


class TestAppendOnlyTrigger:
    def test_the_plpgsql_trigger_refuses_a_rewrite(self, driver, user):
        import psycopg

        event_id = driver.save_source_event(make_event(user), payload="x")
        with pytest.raises(psycopg.errors.RaiseException, match="append-only"):
            driver._conn().execute(
                "UPDATE pdm_source_events SET event_type = 'other' WHERE id = %s",
                (event_id,),
            )
        driver._conn().rollback()

    def test_the_trigger_refuses_a_delete(self, driver, user):
        import psycopg

        event_id = driver.save_source_event(make_event(user), payload="y")
        with pytest.raises(psycopg.errors.RaiseException, match="append-only"):
            driver._conn().execute(
                "DELETE FROM pdm_source_events WHERE id = %s", (event_id,)
            )
        driver._conn().rollback()

    def test_erasure_of_provenance_is_still_permitted(self, driver, user):
        """The carve-out has to behave the same on both backends."""
        event_id = driver.save_source_event(
            make_event(user, provenance={"author": "someone@example.com"}), payload="z"
        )
        driver._conn().execute(
            "UPDATE pdm_source_events SET provenance = '{}', compliance_state = 'erased' "
            "WHERE id = %s",
            (event_id,),
        )
        driver._conn().commit()
        after = driver.get_source_event(event_id)
        assert after.provenance == {}
        assert after.compliance_state == "erased"

    def test_the_python_guard_still_refuses_first(self, driver, user):
        event_id = driver.save_source_event(make_event(user), payload="w")
        with pytest.raises(AppendOnlyViolation):
            driver.delete_source_event(event_id)


class TestOnConflictAndRowcount:
    def test_the_same_event_twice_yields_one_row(self, driver, user):
        event = make_event(user, raw_reference="chat:same")
        first = driver.save_source_event(event, payload="identical")
        again = make_event(user, raw_reference="chat:same")
        second = driver.save_source_event(again, payload="identical")
        assert first == second
        assert again.was_deduplicated is True

    def test_rowcount_reports_a_fresh_insert(self, driver, user):
        event = make_event(user)
        driver.save_source_event(event, payload="fresh")
        assert event.was_deduplicated is False

    def test_link_signature_reports_what_it_claimed(self, driver, user):
        first = driver.save_source_event(make_event(user), payload="a")
        second = driver.save_source_event(make_event(user), payload="b")
        sig = SignatureRecord(user=user, compressed_fact="f", intent_tags=["a", "b", "c"])
        driver.save(sig)

        assert driver.link_signature(sig.id, source_event_id=first, user=user) is True
        assert driver.link_signature(sig.id, source_event_id=first, user=user) is True
        assert driver.link_signature(sig.id, source_event_id=second, user=user) is False


class TestIdentityOnPostgres:
    def test_same_name_two_fields_two_identities(self, driver, user):
        work = driver.resolve_or_create_entity(
            user=user, surface_form="Alex", field_id="work"
        )
        family = driver.resolve_or_create_entity(
            user=user, surface_form="Alex", field_id="family"
        )
        assert work != family

    def test_concurrent_creation_settles_on_one(self, driver, user):
        """
        One driver, many threads — its connections are thread-local, so this is
        how an application actually uses it.

        Deliberately not six drivers constructed at once: every driver runs the
        schema migration in __init__, and concurrent DDL deadlocks on
        AccessExclusiveLock. That is true of the stock PostgresDriver too,
        without any of this feature's tables, so it is not what this test is
        about.
        """
        results: list[str] = []
        errors: list[str] = []
        start = threading.Barrier(6)

        def create() -> None:
            try:
                start.wait()
                results.append(
                    driver.resolve_or_create_entity(
                        user=user, surface_form="Nadia", field_id="work"
                    )
                )
            except Exception as exc:  # noqa: BLE001
                errors.append(f"{type(exc).__name__}: {exc}")

        threads = [threading.Thread(target=create) for _ in range(6)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()

        assert not errors, errors
        assert len(set(results)) == 1

    def test_a_failed_write_leaves_the_session_usable(self, driver, user):
        """
        Postgres aborts the whole transaction on a constraint violation, with
        no savepoint to fall back on. If the failure is not cleared, the next
        statement dies with InFailedSqlTransaction rather than doing its job.
        """
        import psycopg

        sig = SignatureRecord(user=user, compressed_fact="g", intent_tags=["a", "b", "c"])
        driver.save(sig)
        with pytest.raises(psycopg.Error):
            driver.link_signature(sig.id, source_event_id="no-such-event", user=user)

        assert driver.find_event_by_hash("anything", user=user) is None


class TestIntegrityOnPostgres:
    def test_check_integrity_runs_and_reports_clean(self, driver, user):
        event_id = driver.save_source_event(make_event(user), payload="q")
        mention = driver.record_mention(
            EntityMentionRecord(user=user, surface_form="Alex", source_event_id=event_id)
        )
        entity = driver.resolve_or_create_entity(
            user=user, surface_form="Alex", field_id="work"
        )
        driver.resolve_mention(mention, entity_id=entity, method="user_confirmed")

        report = driver.check_integrity(user=user)
        assert report.ok, report.render()

    def test_paging_walks_every_row(self, driver, user):
        for n in range(12):
            driver.save_source_event(make_event(user), payload=f"m{n}")
        assert len(list(driver.iter_source_events(user=user, batch=5))) == 12
