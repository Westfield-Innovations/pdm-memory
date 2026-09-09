"""
Regressions from the branch review — written before the fixes, so each one
fails first and the fix is what turns it green.

Every bug here slipped past a suite of 68 tests because each test exercised a
path the same code wrote. These start from the caller's side instead.
"""

from __future__ import annotations

import threading

import pytest

from pdm_memory import Memory
from pdm_memory.event_log import EventLog
from pdm_memory.storage.events import EntityMentionRecord, SourceEventRecord
from pdm_memory.storage.eventful_sqlite import EventfulSQLiteDriver


@pytest.fixture()
def driver(tmp_path):
    drv = EventfulSQLiteDriver(db_path=str(tmp_path / "reg.db"))
    yield drv
    drv.close()


@pytest.fixture()
def log(tmp_path):
    mem = Memory(storage=EventfulSQLiteDriver(db_path=str(tmp_path / "log.db")))
    yield EventLog(mem)
    mem.close()


class TestProvenanceIsNotRewritten:
    """
    Finding 1. Memory.save dedupes by text, so a fact repeated across two
    messages returns the first signature — and link_signature then moved its
    provenance to the second event. An evidence layer that silently rewrites
    provenance is not an evidence layer.
    """

    def test_second_event_does_not_steal_an_existing_signature(self, log):
        first = log.ingest(
            event=log.event(raw_reference="chat:1"),
            payload="message one",
            facts=[{"text": "Same fact text", "tags": ["a", "b", "c"]}],
        )
        log.ingest(
            event=log.event(raw_reference="chat:2"),
            payload="message two",
            facts=[{"text": "Same fact text", "tags": ["a", "b", "c"]}],
        )
        assert len(log.signatures_for(first["source_event_id"])) == 1, (
            "the first event lost the signature it actually produced"
        )

    def test_link_signature_will_not_overwrite_provenance(self, driver):
        a = driver.save_source_event(SourceEventRecord(raw_reference="chat:1"), payload="a")
        b = driver.save_source_event(SourceEventRecord(raw_reference="chat:2"), payload="b")
        from pdm_memory.core.signature import SignatureRecord

        sig = SignatureRecord(compressed_fact="fact", intent_tags=["a", "b", "c"])
        driver.save(sig)
        driver.link_signature(sig.id, source_event_id=a)
        driver.link_signature(sig.id, source_event_id=b)

        assert [s.id for s in driver.signatures_for_event(a)] == [sig.id]
        assert driver.signatures_for_event(b) == []

    def test_reingest_reports_the_reused_signature(self, log):
        facts = [{"text": "Same fact text", "tags": ["a", "b", "c"]}]
        log.ingest(event=log.event(raw_reference="chat:1"), payload="one", facts=facts)
        second = log.ingest(
            event=log.event(raw_reference="chat:2"), payload="two", facts=facts
        )
        assert second["signatures_reused"] == 1, (
            "the caller cannot tell a stored fact from a deduplicated one"
        )


class TestEntityCreationUnderContention:
    """
    Finding 3. The same read-then-write race that 60a9d15 fixed for events and
    mentions was left in place for entities — and its failed INSERT parks the
    write lock, so other threads fail with `database is locked` several frames
    from the cause.
    """

    def test_concurrent_creation_of_one_name_collapses_to_one_entity(self, driver):
        results: list[str] = []
        errors: list[Exception] = []
        start = threading.Barrier(8)

        def create() -> None:
            try:
                start.wait()
                results.append(
                    driver.resolve_or_create_entity(
                        user="default", surface_form="Nadia", field_id="work"
                    )
                )
            except Exception as exc:  # noqa: BLE001 - seeing it is the point
                errors.append(exc)

        threads = [threading.Thread(target=create) for _ in range(8)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()

        assert not errors, f"resolve_or_create_entity raised under contention: {errors}"
        assert len(set(results)) == 1


class TestMergeIsRecoverable:
    """
    Finding 8. Merging A into B and then B into A dissolved both rows, leaving
    every mention pointing at a retired identity and list_entities() empty.
    And the merge overwrote each mention's resolution grade, so a person's
    confirmed answer was downgraded to whatever the merge was called with.
    """

    def test_merging_into_a_dissolved_entity_is_refused(self, driver):
        a = driver.resolve_or_create_entity(user="default", surface_form="Alex", field_id="work")
        b = driver.resolve_or_create_entity(user="default", surface_form="Alex", field_id="family")
        driver.merge_entities(a, b, method="user_confirmed")

        with pytest.raises(ValueError, match="dissolved|merged"):
            driver.merge_entities(b, a, method="user_confirmed")

        assert len(driver.list_entities()) == 1

    def test_merge_keeps_a_human_confirmation(self, driver):
        event_id = driver.save_source_event(SourceEventRecord(raw_reference="c:1"), payload="x")
        keep = driver.resolve_or_create_entity(user="default", surface_form="Alex", field_id="work")
        merge = driver.resolve_or_create_entity(
            user="default", surface_form="Alex", field_id="family"
        )
        mention = driver.record_mention(
            EntityMentionRecord(surface_form="Alex", source_event_id=event_id, field_id="family")
        )
        driver.resolve_mention(mention, entity_id=merge, method="user_confirmed")

        driver.merge_entities(keep, merge, method="llm")

        after = driver.get_mention(mention)
        assert after.entity_id == keep, "the mention should follow the surviving identity"
        assert after.resolution == "user_confirmed", (
            "a merge repoints a mention; it does not downgrade who decided it"
        )


class TestCloudCapabilityIsHonest:
    """
    Finding 2. supports_events() returned True while eight of the mixin's
    nineteen methods were missing, so EventLog accepted the driver and then
    died on AttributeError — after writing an event, a signature, a mention
    and an entity remotely.
    """

    def test_cloud_driver_implements_what_it_claims(self):
        from pdm_memory.storage.event_store import EventStoreMixin
        from pdm_memory.storage.eventful_cloud import EventfulCloudDriver

        required = [
            name
            for name in dir(EventStoreMixin)
            if not name.startswith("_") and callable(getattr(EventStoreMixin, name, None))
        ]
        missing = [n for n in required if not hasattr(EventfulCloudDriver, n)]
        assert not missing, f"EventfulCloudDriver is missing: {missing}"

    def test_event_log_refuses_a_driver_whose_backend_is_not_there(self, tmp_path):
        """
        Until Companion ships the routes this driver targets, claiming the
        capability is a promise the backend cannot keep. Refusing at
        construction beats a partial remote write.
        """
        from pdm_memory.storage.eventful_cloud import EventfulCloudDriver

        driver = object.__new__(EventfulCloudDriver)
        assert driver.supports_events() is False


class TestHashIsVersionIndependent:
    """
    Finding 7. normalize_instant passed anything it could not parse straight
    into the hash, and `datetime.fromisoformat` accepts a different set of
    spellings on 3.10 than on 3.11+. The same event ingested by an SDK on 3.10
    and a Companion on 3.12 would hash differently — silently doubling the
    event log, which is the exact failure the module docstring warns about.
    """

    @pytest.mark.parametrize(
        "spelling",
        [
            "07/09/2026 10:00",           # not ISO at all
            "2026-09-07 10:00 CEST",      # named zone
            "yesterday",
            "2026-13-45T99:99:99Z",       # ISO-shaped, impossible
        ],
    )
    def test_an_unparseable_instant_is_refused_not_hashed(self, spelling):
        from pdm_memory.storage.event_hash import normalize_instant

        with pytest.raises(ValueError, match="ISO-8601"):
            normalize_instant(spelling)

    def test_seven_fractional_digits_normalise_the_same_everywhere(self):
        """
        The spelling 3.10 rejects and 3.11 accepts. Whatever the runtime, this
        must reduce to one canonical form rather than pass through.
        """
        from pdm_memory.storage.event_hash import normalize_instant

        assert (
            normalize_instant("2026-09-07T10:00:00.1234567Z")
            == "2026-09-07T10:00:00.123456Z"
        )

    def test_golden_vectors_still_hold(self):
        import json
        import pathlib

        from pdm_memory.storage.event_hash import golden_vectors

        fixture = json.loads(
            (pathlib.Path(__file__).parent / "fixtures" / "event_hash_vectors.json")
            .read_text(encoding="utf-8")
        )
        live = {v["name"]: v["content_hash"] for v in golden_vectors()}
        assert live == {v["name"]: v["content_hash"] for v in fixture["vectors"]}


class TestOneTimeFormatPerDatabase:
    """
    Finding 15. Events were written through normalize_instant (UTC, trailing Z)
    while entities and mentions used the caller's offset. Both columns are
    TEXT, so `ORDER BY observed_at` compared "…+03:00" against "…Z" as strings
    and returned a later moment first.
    """

    def test_every_timestamp_column_uses_one_spelling(self, driver):
        import sqlite3

        event_id = driver.save_source_event(SourceEventRecord(raw_reference="c:1"), payload="x")
        driver.record_mention(
            EntityMentionRecord(surface_form="Alex", source_event_id=event_id, field_id="w")
        )
        entity = driver.resolve_or_create_entity(
            user="default", surface_form="Alex", field_id="w"
        )
        driver.resolve_mention(
            driver.unresolved_mentions()[0].id, entity_id=entity, method="user_confirmed"
        )

        raw = sqlite3.connect(driver.db_path)
        stamps = [
            row[0]
            for row in raw.execute(
                "SELECT occurred_at FROM pdm_source_events "
                "UNION ALL SELECT observed_at FROM pdm_entity_mentions "
                "UNION ALL SELECT resolved_at FROM pdm_entity_mentions "
                "UNION ALL SELECT created_at FROM pdm_entities"
            )
            if row[0]
        ]
        raw.close()

        offenders = [s for s in stamps if not s.endswith("Z")]
        assert not offenders, f"not normalised to UTC: {offenders}"

    def test_mentions_sort_chronologically_as_text(self, driver):
        from datetime import datetime, timedelta, timezone

        event_id = driver.save_source_event(SourceEventRecord(raw_reference="c:2"), payload="y")
        base = datetime(2026, 9, 7, 10, 0, tzinfo=timezone.utc)
        for offset_hours, tz_hours in ((0, 0), (1, 3), (2, -5)):
            driver.record_mention(
                EntityMentionRecord(
                    surface_form=f"Name{offset_hours}",
                    source_event_id=event_id,
                    signature_id=f"s{offset_hours}",
                    observed_at=(base + timedelta(hours=offset_hours)).astimezone(
                        timezone(timedelta(hours=tz_hours))
                    ),
                )
            )
        order = [m.surface_form for m in driver.unresolved_mentions()]
        assert order == ["Name0", "Name1", "Name2"], (
            f"TEXT ordering disagrees with chronology: {order}"
        )


class TestSyncCarriesEverything:
    """
    Finding 4. list_source_events was `ORDER BY occurred_at DESC LIMIT n` with
    no cursor, so a store holding more than one page re-sent the same newest
    page every time and the older rows never left. _push_mentions read only
    unresolved mentions, while EventLog.mention() resolves on the spot — so
    ordinary mentions were never pushed at all. And _pull moved events only.
    Every one of these reported errors=0.
    """

    def _pair(self, tmp_path):
        left = EventfulSQLiteDriver(db_path=str(tmp_path / "left.db"))
        right = EventfulSQLiteDriver(db_path=str(tmp_path / "right.db"))
        return left, right

    def test_every_event_crosses_not_just_the_newest_page(self, tmp_path):
        from datetime import datetime, timedelta, timezone

        from pdm_memory.storage.event_sync import EventSync

        left, right = self._pair(tmp_path)
        base = datetime(2026, 1, 1, tzinfo=timezone.utc)
        for n in range(25):
            left.save_source_event(
                SourceEventRecord(raw_reference=f"chat:{n}", occurred_at=base + timedelta(days=n)),
                payload=f"message {n}",
            )

        report = EventSync(left, right, page_size=5).sync(direction="push")
        assert report.errors == 0
        assert len(right.list_source_events(limit=100)) == 25, (
            "older events never left the source store"
        )
        left.close()
        right.close()

    def test_resolved_mentions_are_pushed_too(self, tmp_path):
        from pdm_memory.storage.event_sync import EventSync

        left, right = self._pair(tmp_path)
        event_id = left.save_source_event(SourceEventRecord(raw_reference="c:1"), payload="x")
        mention = left.record_mention(
            EntityMentionRecord(surface_form="Alex", source_event_id=event_id, field_id="work")
        )
        entity = left.resolve_or_create_entity(
            user="default", surface_form="Alex", field_id="work"
        )
        left.resolve_mention(mention, entity_id=entity, method="user_confirmed")

        report = EventSync(left, right).sync(direction="push")
        assert report.mentions_pushed == 1, (
            "a resolved mention is still evidence and still has to cross"
        )
        assert report.errors == 0
        left.close()
        right.close()

    def test_pull_brings_entities_and_mentions_not_only_events(self, tmp_path):
        from pdm_memory.storage.event_sync import EventSync

        local, cloud = self._pair(tmp_path)
        event_id = cloud.save_source_event(SourceEventRecord(raw_reference="c:9"), payload="y")
        mention = cloud.record_mention(
            EntityMentionRecord(surface_form="Bohdan", source_event_id=event_id, field_id="work")
        )
        entity = cloud.resolve_or_create_entity(
            user="default", surface_form="Bohdan", field_id="work"
        )
        cloud.resolve_mention(mention, entity_id=entity, method="user_confirmed")

        report = EventSync(local, cloud).sync(direction="pull")
        assert report.errors == 0
        assert len(local.list_source_events()) == 1
        assert [e.canonical_name for e in local.list_entities()] == ["Bohdan"]
        assert len(local.unresolved_mentions()) + len(
            local.mentions_for_entity(local.list_entities()[0].id)
        ) == 1
        local.close()
        cloud.close()


class TestPullScopesToTheRequestedUser:
    """
    Finding 12. _pull stored each row under the user carried in the payload —
    falling back to "default" — while deduplicating under the user the caller
    asked for. Pulling twice for `alice` wrote two rows under `default` and
    counted both as freshly pulled.
    """

    def test_pulled_rows_belong_to_the_requested_user(self, tmp_path):
        from pdm_memory.storage.event_sync import EventSync

        local = EventfulSQLiteDriver(db_path=str(tmp_path / "local.db"))
        cloud = EventfulSQLiteDriver(db_path=str(tmp_path / "cloud.db"))
        cloud.save_source_event(
            SourceEventRecord(user="alice", raw_reference="c:1"), payload="x"
        )

        first = EventSync(local, cloud).sync(user="alice", direction="pull")
        second = EventSync(local, cloud).sync(user="alice", direction="pull")

        assert first.events_pulled == 1
        assert second.events_pulled == 0, "the second pull duplicated instead of deduplicating"
        assert second.events_deduplicated == 1
        assert len(local.list_source_events(user="alice")) == 1
        assert local.list_source_events(user="default") == []
        local.close()
        cloud.close()


class TestPostgresTriggerInstallIsCheap:
    """
    Finding 6. The Postgres path dropped and recreated its triggers on every
    driver __init__ — an ACCESS EXCLUSIVE lock at each process start, and two
    workers booting together raced into DuplicateObject. Postgres has no
    CREATE TRIGGER IF NOT EXISTS, so the install has to swallow the duplicate
    itself.
    """

    def test_install_does_not_drop_an_existing_trigger(self):
        from pdm_memory.storage.events import TRIGGERS_EVENTS_POSTGRES

        joined = "\n".join(TRIGGERS_EVENTS_POSTGRES)
        assert "DROP TRIGGER" not in joined

    def test_install_tolerates_a_trigger_that_is_already_there(self):
        from pdm_memory.storage.events import TRIGGERS_EVENTS_POSTGRES

        creates = [s for s in TRIGGERS_EVENTS_POSTGRES if "CREATE TRIGGER" in s]
        assert creates, "no trigger is installed at all"
        for statement in creates:
            assert "duplicate_object" in statement, (
                "a second worker booting at the same time will fail this install"
            )

    def test_merge_scopes_its_updates_to_one_user(self):
        """
        Finding 16, checked where it is cheap to check: the only index on
        mentions is (user, entity_id), so a repoint without the user scans
        every mention in the store.
        """
        import inspect

        from pdm_memory.storage.event_store import EventStoreMixin

        body = inspect.getsource(EventStoreMixin.merge_entities)
        repoints = [
            line for line in body.splitlines() if "UPDATE pdm_entity_mentions" in line
        ]
        assert repoints, "merge no longer repoints mentions?"
        assert "{user}" in body.split("UPDATE pdm_entity_mentions")[1][:200]


class TestAFailedWriteReleasesTheLock:
    """
    Finding 3, second half — the one the first pass missed. link_signature can
    violate a foreign key, and a failed statement leaves SQLite's implicit
    transaction open with the write lock still held. Every other writer then
    queues behind a connection that has already raised.

    The review asked for try/finally rather than an except clause per error
    class, and it was right: `database is locked` parks the transaction just
    as thoroughly as a constraint violation does.
    """

    def test_a_foreign_key_violation_does_not_park_the_transaction(self, driver):
        import sqlite3

        from pdm_memory.core.signature import SignatureRecord

        sig = SignatureRecord(compressed_fact="x", intent_tags=["a", "b", "c"])
        driver.save(sig)

        with pytest.raises(sqlite3.IntegrityError):
            driver.link_signature(sig.id, source_event_id="no-such-event")

        assert not driver._conn().in_transaction, (
            "the failed write is still holding the write lock"
        )

    def test_another_writer_is_not_blocked_by_the_failure(self, driver, tmp_path):
        from pdm_memory.core.signature import SignatureRecord

        sig = SignatureRecord(compressed_fact="x", intent_tags=["a", "b", "c"])
        driver.save(sig)
        with pytest.raises(Exception):
            driver.link_signature(sig.id, source_event_id="no-such-event")

        errors: list[str] = []

        def write() -> None:
            try:
                other = EventfulSQLiteDriver(db_path=driver.db_path)
                other.save(
                    SignatureRecord(compressed_fact="y", intent_tags=["a", "b", "c"])
                )
                other.close()
            except Exception as exc:  # noqa: BLE001
                errors.append(f"{type(exc).__name__}: {exc}")

        thread = threading.Thread(target=write)
        thread.start()
        thread.join(timeout=10)
        assert not errors, errors


class TestIntegrityCheck:
    """
    Finding 18, the half that can be closed cheaply. `PRAGMA foreign_keys` is
    per connection, so a client writing raw SQL past both drivers can leave a
    signature pointing at an event that does not exist. Enforcing that with a
    trigger would put a rule on pdm_signatures — the table every caller writes
    to, including those who never touch the event layer — and make every insert
    pay for it. Finding the damage costs nothing and risks nothing.
    """

    def _dangle(self, driver):
        """Do what only raw SQL can: point a signature at nothing."""
        import sqlite3

        from pdm_memory.core.signature import SignatureRecord

        sig = SignatureRecord(compressed_fact="x", intent_tags=["a", "b", "c"])
        driver.save(sig)
        raw = sqlite3.connect(driver.db_path)
        raw.execute(
            "UPDATE pdm_signatures SET source_event_id = 'no-such-event' WHERE id = ?",
            (sig.id,),
        )
        raw.commit()
        raw.close()
        return sig.id

    def test_a_clean_store_reports_clean(self, driver):
        event_id = driver.save_source_event(SourceEventRecord(raw_reference="c:1"), payload="x")
        from pdm_memory.core.signature import SignatureRecord

        sig = SignatureRecord(compressed_fact="ok", intent_tags=["a", "b", "c"])
        driver.save(sig)
        driver.link_signature(sig.id, source_event_id=event_id)

        report = driver.check_integrity()
        assert report.ok
        assert report.total == 0

    def test_a_dangling_signature_is_found(self, driver):
        sig_id = self._dangle(driver)

        report = driver.check_integrity()
        assert not report.ok
        assert sig_id in report.signatures_without_event
        assert report.total == 1
        assert "no-such-event" not in str(report), "the report names rows, not values"

    def test_a_mention_pointing_at_a_deleted_entity_is_found(self, driver):
        import sqlite3

        event_id = driver.save_source_event(SourceEventRecord(raw_reference="c:2"), payload="y")
        mention = driver.record_mention(
            EntityMentionRecord(surface_form="Alex", source_event_id=event_id, field_id="w")
        )
        entity = driver.resolve_or_create_entity(
            user="default", surface_form="Alex", field_id="w"
        )
        driver.resolve_mention(mention, entity_id=entity, method="user_confirmed")

        raw = sqlite3.connect(driver.db_path)
        raw.execute("DELETE FROM pdm_entities WHERE id = ?", (entity,))
        raw.commit()
        raw.close()

        report = driver.check_integrity()
        assert mention in report.mentions_without_entity

    def test_the_check_reads_and_never_writes(self, driver):
        """A diagnostic that repairs things by surprise is not a diagnostic."""
        self._dangle(driver)
        before = driver.check_integrity()
        after = driver.check_integrity()
        assert before.signatures_without_event == after.signatures_without_event

    def test_it_is_reachable_from_the_facade(self, log):
        assert log.check_integrity().ok


class TestMappersStayInStepWithTheRecords:
    """
    Finding 17's root cause. Columns are written out by hand in eleven places —
    three row readers, three row writers, five wire mappers — and they had
    already drifted: event_from_payload left provenance a string, mention_payload
    omitted resolved_at, mention_from_payload did not exist. Fixing the drift
    without a test that notices the next one just resets the clock.

    Each record's field set is the contract. Every mapper has to carry it.
    """

    def _fields(self, record_cls) -> set[str]:
        import dataclasses

        return {
            f.name
            for f in dataclasses.fields(record_cls)
            if f.init  # transient flags like was_deduplicated are not columns
        }

    def test_every_source_event_field_survives_the_database(self, driver):
        from datetime import datetime, timezone

        stored = driver.get_source_event(
            driver.save_source_event(
                SourceEventRecord(
                    user="default",
                    event_type="email",
                    occurred_at=datetime(2026, 5, 4, 3, 2, 1, tzinfo=timezone.utc),
                    source_system="gmail",
                    provenance={"author": "someone@example.com"},
                    raw_reference="gmail:1",
                    capture_authority_state="granted",
                    compliance_state="reviewed",
                ),
                payload="hello",
            )
        )
        for name in self._fields(SourceEventRecord) - {"id", "observed_at", "ingested_at"}:
            assert getattr(stored, name), f"{name} did not survive the round trip"
        assert stored.provenance == {"author": "someone@example.com"}

    def test_every_source_event_field_survives_the_wire(self):
        from datetime import datetime, timezone

        from pdm_memory.storage.eventful_cloud import EventfulCloudDriver as C

        event = SourceEventRecord(
            event_type="email",
            occurred_at=datetime(2026, 5, 4, tzinfo=timezone.utc),
            source_system="gmail",
            provenance={"author": "someone@example.com"},
            raw_reference="gmail:1",
            capture_authority_state="granted",
            compliance_state="reviewed",
        )
        event.ensure_content_hash(payload="hello")
        back = C.event_from_payload({**C.event_payload(event), "id": event.id})

        for name in self._fields(SourceEventRecord) - {"observed_at", "ingested_at"}:
            assert getattr(back, name) == getattr(event, name), f"{name} lost on the wire"

    def test_every_mention_field_survives_the_wire(self):
        from pdm_memory.storage.eventful_cloud import EventfulCloudDriver as C

        mention = EntityMentionRecord(
            surface_form="Alex",
            source_event_id="e1",
            signature_id="s1",
            field_id="work",
            entity_id="n1",
            resolution="user_confirmed",
            confidence=0.9,
        )
        back = C.mention_from_payload(C.mention_payload(mention))
        for name in self._fields(EntityMentionRecord) - {"resolved_at"}:
            assert getattr(back, name) == getattr(mention, name), f"{name} lost on the wire"

    def test_every_entity_field_survives_the_wire(self):
        from pdm_memory.storage.events import EntityRecord
        from pdm_memory.storage.eventful_cloud import EventfulCloudDriver as C

        entity = EntityRecord(
            entity_type="project",
            canonical_name="Orion",
            disambiguator="work",
            origin_field_id="work",
            aliases=["orion", "Project Orion"],
            current_state_version=3,
        )
        payload = {
            "id": entity.id,
            "user": entity.user,
            "entity_type": entity.entity_type,
            "canonical_name": entity.canonical_name,
            "disambiguator": entity.disambiguator,
            "origin_field_id": entity.origin_field_id,
            "aliases": entity.aliases,
            "current_state_version": entity.current_state_version,
        }
        back = C.entity_from_payload(payload)
        for name in self._fields(EntityRecord) - {"created_at", "dissolved_at", "merged_into"}:
            assert getattr(back, name) == getattr(entity, name), f"{name} lost on the wire"

    def test_insert_row_width_matches_the_ddl(self):
        """A column added to the table but not to the tuple fails at runtime."""
        import re

        from pdm_memory.storage.events import (
            SCHEMA_EVENTS_SQLITE,
            entity_insert_row,
            event_insert_row,
            mention_insert_row,
        )
        from pdm_memory.storage.events import EntityRecord as E

        for table, builder, record in (
            ("pdm_source_events", event_insert_row, SourceEventRecord()),
            ("pdm_entities", entity_insert_row, E(canonical_name="x")),
            ("pdm_entity_mentions", mention_insert_row, EntityMentionRecord(surface_form="x")),
        ):
            block = re.search(
                rf"CREATE TABLE IF NOT EXISTS {table} \((.*?)\n\);",
                SCHEMA_EVENTS_SQLITE,
                re.S,
            ).group(1)
            columns = [
                line.strip().split()[0]
                for line in block.splitlines()
                if line.strip() and not line.strip().startswith("--")
            ]
            assert len(builder(record)) == len(columns), (
                f"{table}: DDL has {len(columns)} columns, "
                f"the insert tuple carries {len(builder(record))}"
            )


class TestTheParserDoesNotDependOnThePythonVersion:
    """
    CI caught what my local runs could not: raising on an unparseable string
    made the failure loud but left it version-dependent. `fromisoformat`
    widened its grammar in 3.11, so seven fractional digits raise on 3.10 and
    parse on 3.12 — the same timestamp accepted by a Companion and refused by
    an SDK. The parser is now our own, and the grammar is the same everywhere.
    """

    def test_normalisation_never_calls_fromisoformat(self):
        import inspect

        from pdm_memory.storage import event_hash

        source = inspect.getsource(event_hash._parse_iso)
        assert "fromisoformat" not in source, (
            "the accepted grammar would move with the runtime again"
        )

    @pytest.mark.parametrize(
        ("spelling", "expected"),
        [
            ("2026-09-07T10:00:00.1234567Z", "2026-09-07T10:00:00.123456Z"),
            ("2026-09-07T10:00:00.1Z", "2026-09-07T10:00:00.100000Z"),
            ("20260907T100000Z", "2026-09-07T10:00:00.000000Z"),
            ("2026-09-07T10:00:00,123Z", "2026-09-07T10:00:00.123000Z"),
            ("2026-09-07T13:00:00+03:00", "2026-09-07T10:00:00.000000Z"),
            ("2026-09-07T13:00:00+0300", "2026-09-07T10:00:00.000000Z"),
            ("2026-09-07T05:00:00-05", "2026-09-07T10:00:00.000000Z"),
            ("2026-09-07 10:00:00", "2026-09-07T10:00:00.000000Z"),
            ("2026-09-07", "2026-09-07T00:00:00.000000Z"),
        ],
    )
    def test_one_grammar_on_every_runtime(self, spelling, expected):
        from pdm_memory.storage.event_hash import normalize_instant

        assert normalize_instant(spelling) == expected

    @pytest.mark.parametrize(
        "spelling",
        ["2026-13-45T99:99:99Z", "07/09/2026 10:00", "yesterday", "2026-09-07T10"],
    )
    def test_what_is_not_a_timestamp_is_still_refused(self, spelling):
        from pdm_memory.storage.event_hash import normalize_instant

        with pytest.raises(ValueError, match="ISO-8601"):
            normalize_instant(spelling)
