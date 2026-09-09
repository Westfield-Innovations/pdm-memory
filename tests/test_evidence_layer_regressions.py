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
