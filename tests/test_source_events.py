"""
TKT-101 — append-only event log, entities, and the mention layer.

The tests are grouped by the acceptance criterion each one answers, plus the
two regressions that would otherwise pass review while being false: foreign
keys that parse but enforce nothing, and a uniqueness index defeated by NULLs.
"""

from __future__ import annotations

import json
import pathlib
import sqlite3
from datetime import datetime, timezone

import pytest

from pdm_memory import Memory
from pdm_memory.core.signature import SignatureRecord
from pdm_memory.storage.event_hash import (
    compute_content_hash,
    golden_vectors,
    normalize_instant,
)
from pdm_memory.storage.events import (
    AppendOnlyViolation,
    EntityMentionRecord,
    SourceEventRecord,
    storage_supports_events,
)
from pdm_memory.storage.eventful_sqlite import EventfulSQLiteDriver
from pdm_memory.storage.sqlite_driver import SQLiteDriver

OCCURRED = datetime(2026, 9, 7, 10, 0, tzinfo=timezone.utc)


@pytest.fixture()
def db_path(tmp_path: pathlib.Path) -> str:
    return str(tmp_path / "events.db")


@pytest.fixture()
def driver(db_path: str) -> EventfulSQLiteDriver:
    drv = EventfulSQLiteDriver(db_path=db_path)
    yield drv
    drv.close()


def make_event(**overrides) -> SourceEventRecord:
    base = {
        "event_type": "chat_message",
        "occurred_at": OCCURRED,
        "source_system": "azus_chat",
        "raw_reference": "chat:123:msg:456",
    }
    base.update(overrides)
    return SourceEventRecord(**base)


def make_sig(text: str, **overrides) -> SignatureRecord:
    base = {
        "compressed_fact": text,
        "source": "chat",
        "p_magnitude": 70.0,
        "intent_tags": ["release", "schedule", "orion"],
    }
    base.update(overrides)
    return SignatureRecord(**base)


# ---------------------------------------------------------------------------
# AC1 — one event, many signatures, raw content stored once
# ---------------------------------------------------------------------------


class TestAC1OneEventManySignatures:
    def test_one_event_yields_many_signatures(self, driver):
        event_id = driver.save_source_event(make_event(), payload="Orion moved to Friday")

        for text in ("Orion release moved", "Alex owns the release", "Friday is the date"):
            sig = make_sig(text)
            driver.save(sig)
            driver.link_signature(sig.id, source_event_id=event_id)

        attached = driver.signatures_for_event(event_id)
        assert len(attached) == 3
        assert {s.compressed_fact for s in attached} == {
            "Orion release moved",
            "Alex owns the release",
            "Friday is the date",
        }

    def test_raw_content_is_a_pointer_not_a_copy(self, driver):
        """The event stores where the text lives, never the text itself."""
        payload = "Moved the Orion release review to Friday."
        event_id = driver.save_source_event(make_event(), payload=payload)

        stored = driver.get_source_event(event_id)
        assert stored.raw_reference == "chat:123:msg:456"
        assert payload not in json.dumps(stored.__dict__, default=str)

    def test_duplicate_content_hash_reuses_event(self, driver):
        payload = "Moved the Orion release review to Friday."
        first = driver.save_source_event(make_event(), payload=payload)
        # A replay: same message, seen and ingested at a different moment.
        second = driver.save_source_event(
            make_event(observed_at=datetime(2026, 9, 8, tzinfo=timezone.utc)),
            payload=payload,
        )

        assert first == second
        count = driver._conn().execute(
            "SELECT COUNT(*) FROM pdm_source_events"
        ).fetchone()[0]
        assert count == 1

    def test_observation_times_are_outside_the_hash(self):
        """Two observers of one message must agree on its identity."""
        args = {
            "event_type": "chat_message",
            "occurred_at": OCCURRED,
            "source_system": "azus_chat",
            "raw_reference": "chat:1",
            "payload": "hello",
        }
        assert compute_content_hash(**args) == compute_content_hash(**args)

        sdk_side = make_event(observed_at=datetime(2026, 9, 7, 10, 0, 2, tzinfo=timezone.utc))
        companion_side = make_event(
            observed_at=datetime(2026, 9, 9, 3, 0, tzinfo=timezone.utc),
            capture_authority_state="granted",
            compliance_state="reviewed",
        )
        assert sdk_side.ensure_content_hash(payload="hello") == (
            companion_side.ensure_content_hash(payload="hello")
        )


# ---------------------------------------------------------------------------
# AC2 — history is not rewritten or deleted
# ---------------------------------------------------------------------------


class TestAC2AppendOnly:
    def test_update_source_event_raises_append_only(self, driver):
        event_id = driver.save_source_event(make_event())
        with pytest.raises(AppendOnlyViolation) as excinfo:
            driver.update_source_event(event_id, event_type="email")
        assert "append-only" in str(excinfo.value)

    def test_delete_source_event_raises_append_only(self, driver):
        event_id = driver.save_source_event(make_event())
        with pytest.raises(AppendOnlyViolation):
            driver.delete_source_event(event_id)

    def test_raw_connection_update_blocked_by_trigger(self, driver, db_path):
        """
        The one that matters: a second connection bypasses every Python guard.
        A driver-level check is advice; the trigger is enforcement.
        """
        event_id = driver.save_source_event(make_event())

        raw = sqlite3.connect(db_path)
        with pytest.raises(sqlite3.IntegrityError, match="append-only"):
            raw.execute(
                "UPDATE pdm_source_events SET occurred_at = ? WHERE id = ?",
                ("2020-01-01T00:00:00.000000Z", event_id),
            )
        raw.close()

    def test_raw_connection_delete_blocked_by_trigger(self, driver, db_path):
        event_id = driver.save_source_event(make_event())

        raw = sqlite3.connect(db_path)
        with pytest.raises(sqlite3.IntegrityError, match="append-only"):
            raw.execute("DELETE FROM pdm_source_events WHERE id = ?", (event_id,))
        raw.close()

    def test_mentions_are_append_only_too(self, driver, db_path):
        event_id = driver.save_source_event(make_event())
        mention_id = driver.record_mention(
            EntityMentionRecord(surface_form="Alex", source_event_id=event_id, field_id="work")
        )

        raw = sqlite3.connect(db_path)
        with pytest.raises(sqlite3.IntegrityError, match="append-only"):
            raw.execute(
                "UPDATE pdm_entity_mentions SET surface_form = ? WHERE id = ?",
                ("Bohdan", mention_id),
            )
        raw.close()

    def test_erasure_of_provenance_is_still_permitted(self, driver, db_path):
        """
        Append-only freezes what happened, not what we recorded about handling
        it. A blanket UPDATE block would make an erasure request impossible to
        honour and would freeze the row's own compliance_state — which is the
        one column certain to change.
        """
        event_id = driver.save_source_event(
            make_event(provenance={"author_email": "alex@example.com"})
        )

        raw = sqlite3.connect(db_path)
        raw.execute(
            "UPDATE pdm_source_events SET provenance = ?, raw_reference = ?, "
            "compliance_state = ? WHERE id = ?",
            ("{}", "", "erased", event_id),
        )
        raw.commit()
        raw.close()

        scrubbed = driver.get_source_event(event_id)
        assert scrubbed.provenance == {}
        assert scrubbed.compliance_state == "erased"
        # What happened is untouched.
        assert scrubbed.event_type == "chat_message"
        assert scrubbed.occurred_at == OCCURRED

    def test_bulk_insert_still_allowed(self, driver):
        """Append-only is not read-only."""
        ids = {
            driver.save_source_event(make_event(raw_reference=f"chat:{i}"))
            for i in range(5)
        }
        assert len(ids) == 5


# ---------------------------------------------------------------------------
# AC3 — entity ↔ signature relations, indexed and enforced
# ---------------------------------------------------------------------------


class TestAC3Relations:
    def test_signature_fk_rejects_unknown_event(self, driver):
        """
        Regression: SQLite leaves foreign_keys OFF per connection. Without the
        pragma this passes silently and AC3 is decorative.
        """
        sig = make_sig("Orion release moved")
        driver.save(sig)
        with pytest.raises(sqlite3.IntegrityError):
            driver.link_signature(sig.id, source_event_id="no-such-event")

    def test_foreign_keys_pragma_is_on(self, driver):
        assert driver._conn().execute("PRAGMA foreign_keys").fetchone()[0] == 1

    def test_signature_links_to_entity(self, driver):
        event_id = driver.save_source_event(make_event())
        entity_id = driver.resolve_or_create_entity(
            user="default", surface_form="Alex", field_id="work"
        )
        sig = make_sig("Alex owns the Orion release")
        driver.save(sig)
        driver.link_signature(sig.id, source_event_id=event_id, primary_entity_id=entity_id)

        assert [s.id for s in driver.signatures_for_entity(entity_id)] == [sig.id]

    def test_relation_lookup_uses_the_index(self, driver):
        plan = driver._conn().execute(
            "EXPLAIN QUERY PLAN SELECT * FROM pdm_signatures "
            "WHERE user = ? AND primary_entity_id = ?",
            ("default", "x"),
        ).fetchall()
        assert any("idx_pdm_sig_entity" in str(tuple(row)) for row in plan), plan


# ---------------------------------------------------------------------------
# D6 — identity resolution, and the regressions it exists to prevent
# ---------------------------------------------------------------------------


class TestIdentityResolution:
    def test_same_name_same_field_is_one_person(self, driver):
        first = driver.resolve_or_create_entity(
            user="default", surface_form="Alex", field_id="work"
        )
        second = driver.resolve_or_create_entity(
            user="default", surface_form="alex", field_id="work"
        )
        assert first == second

    def test_same_name_different_field_stays_two_identities(self, driver):
        """
        The deliberate inversion. Merging two Alexes is silent and
        unrecoverable; splitting one is visible and asks a question.
        """
        work = driver.resolve_or_create_entity(
            user="default", surface_form="Alex", field_id="work"
        )
        family = driver.resolve_or_create_entity(
            user="default", surface_form="Alex", field_id="family"
        )
        assert work != family
        assert driver.get_entity(work).disambiguator == ""
        assert driver.get_entity(family).disambiguator == "family"

    def test_mention_is_idempotent_before_a_signature_exists(self, driver):
        """
        Regression: with nullable id columns this index never fires, because
        NULLs do not conflict in a UNIQUE index in SQLite or PostgreSQL. The
        duplicate mentions appear precisely on the path where the signature id
        is not known yet — the common one.
        """
        event_id = driver.save_source_event(make_event())
        ids = {
            driver.record_mention(
                EntityMentionRecord(
                    surface_form="Alex", source_event_id=event_id, field_id="work"
                )
            )
            for _ in range(3)
        }
        assert len(ids) == 1

        count = driver._conn().execute(
            "SELECT COUNT(*) FROM pdm_entity_mentions"
        ).fetchone()[0]
        assert count == 1

    def test_user_confirmation_survives_a_later_automatic_pass(self, driver):
        event_id = driver.save_source_event(make_event())
        mention_id = driver.record_mention(
            EntityMentionRecord(surface_form="Alex", source_event_id=event_id, field_id="work")
        )
        confirmed = driver.resolve_or_create_entity(
            user="default", surface_form="Alex", field_id="work"
        )
        other = driver.resolve_or_create_entity(
            user="default", surface_form="Alex", field_id="family"
        )

        driver.resolve_mention(mention_id, entity_id=confirmed, method="user_confirmed")
        driver.resolve_mention(mention_id, entity_id=other, method="llm")

        still = driver.get_mention(mention_id)
        assert still.entity_id == confirmed
        assert still.resolution == "user_confirmed"

    def test_merge_repoints_mentions_and_closes_the_row(self, driver):
        event_id = driver.save_source_event(make_event())
        keep = driver.resolve_or_create_entity(
            user="default", surface_form="Alex", field_id="work"
        )
        merge = driver.resolve_or_create_entity(
            user="default", surface_form="Alex", field_id="family"
        )
        mention_id = driver.record_mention(
            EntityMentionRecord(
                surface_form="Alex", source_event_id=event_id, field_id="family"
            )
        )
        driver.resolve_mention(mention_id, entity_id=merge, method="same_name_same_field")

        driver.merge_entities(keep, merge, method="user_confirmed")

        assert driver.get_mention(mention_id).entity_id == keep
        closed = driver.get_entity(merge)
        assert closed is not None, "a merged entity is closed, never deleted"
        assert closed.dissolved_at is not None
        assert closed.merged_into == keep
        # Asking for the merged identity now lands on the survivor.
        assert driver.resolve_or_create_entity(
            user="default", surface_form="Alex", field_id="family"
        ) == keep

    def test_mentions_never_merge_on_their_own(self, driver):
        """Recording evidence is always safe: it attributes nothing."""
        event_id = driver.save_source_event(make_event())
        driver.record_mention(
            EntityMentionRecord(
                surface_form="Alex", source_event_id=event_id,
                signature_id="s1", field_id="work",
            )
        )
        driver.record_mention(
            EntityMentionRecord(
                surface_form="Alex", source_event_id=event_id,
                signature_id="s2", field_id="family",
            )
        )
        rows = driver._conn().execute(
            "SELECT entity_id, resolution FROM pdm_entity_mentions"
        ).fetchall()
        assert len(rows) == 2
        assert all(r["entity_id"] is None and r["resolution"] == "unresolved" for r in rows)


# ---------------------------------------------------------------------------
# Migration
# ---------------------------------------------------------------------------


class TestMigration:
    def test_migration_on_existing_db_is_idempotent(self, db_path):
        EventfulSQLiteDriver(db_path=db_path).close()
        EventfulSQLiteDriver(db_path=db_path).close()
        drv = EventfulSQLiteDriver(db_path=db_path)
        assert drv.supports_events()
        drv.close()

    def test_migration_preserves_existing_signatures(self, db_path):
        plain = SQLiteDriver(db_path=db_path)
        sig = make_sig("Written before the event log existed")
        plain.save(sig)
        plain.close()

        upgraded = EventfulSQLiteDriver(db_path=db_path)
        restored = upgraded.get(sig.id)
        assert restored is not None
        assert restored.compressed_fact == "Written before the event log existed"
        assert upgraded.count() == 1
        upgraded.close()

    def test_trigger_survives_a_second_migration_pass(self, db_path):
        """
        Order regression: triggers must be created after ALTER TABLE, or the
        migration aborts on the RAISE it just installed.
        """
        drv = EventfulSQLiteDriver(db_path=db_path)
        event_id = drv.save_source_event(make_event())
        drv.close()

        reopened = EventfulSQLiteDriver(db_path=db_path)
        raw = sqlite3.connect(db_path)
        with pytest.raises(sqlite3.IntegrityError, match="append-only"):
            raw.execute("DELETE FROM pdm_source_events WHERE id = ?", (event_id,))
        raw.close()
        reopened.close()


# ---------------------------------------------------------------------------
# D4 — optional capability, no abstract method on BaseStorage
# ---------------------------------------------------------------------------


class TestOptionalCapability:
    def test_driver_without_events_reports_unsupported(self, tmp_path):
        plain = SQLiteDriver(db_path=str(tmp_path / "plain.db"))
        assert storage_supports_events(plain) is False
        plain.close()

    def test_eventful_driver_reports_supported(self, driver):
        assert storage_supports_events(driver) is True

    def test_memory_accepts_an_injected_eventful_driver(self, db_path):
        """The documented injection path: no frozen module is touched."""
        drv = EventfulSQLiteDriver(db_path=db_path)
        mem = Memory(storage=drv)
        mem.save("Orion release moved to Friday", tags=["orion", "release", "date"])

        event_id = drv.save_source_event(make_event())
        stored = mem.list(limit=5).items
        drv.link_signature(stored[0].id, source_event_id=event_id)

        assert len(drv.signatures_for_event(event_id)) == 1
        mem.close()

    def test_enable_events_overrides_the_sqlite_scheme(self, db_path):
        from pdm_memory.storage import factory
        from pdm_memory.storage.eventful_sqlite import enable_events

        saved = dict(factory._CUSTOM_SCHEMES)
        try:
            enable_events()
            storage = factory.create_storage(db_path)
            assert isinstance(storage, EventfulSQLiteDriver)
            storage.close()
        finally:
            factory._CUSTOM_SCHEMES.clear()
            factory._CUSTOM_SCHEMES.update(saved)


# ---------------------------------------------------------------------------
# The SDK ↔ Companion hash contract
# ---------------------------------------------------------------------------


class TestContentHashContract:
    def test_golden_vectors_match_the_committed_fixture(self):
        """
        Companion asserts against this same file. A drift here is the failure
        mode that breaks AC1 silently through sync rather than loudly at the
        call site, so the fixture is committed rather than computed.
        """
        fixture = json.loads(
            (pathlib.Path(__file__).parent / "fixtures" / "event_hash_vectors.json")
            .read_text(encoding="utf-8")
        )
        live = {v["name"]: v["content_hash"] for v in golden_vectors()}
        committed = {v["name"]: v["content_hash"] for v in fixture["vectors"]}
        assert live == committed

    @pytest.mark.parametrize(
        "spelling",
        [
            "2026-09-07T10:00:00Z",
            "2026-09-07T10:00:00+00:00",
            "2026-09-07T13:00:00+03:00",
            datetime(2026, 9, 7, 10, 0, tzinfo=timezone.utc),
            datetime(2026, 9, 7, 10, 0),
        ],
    )
    def test_one_instant_hashes_one_way(self, spelling):
        """
        Python writes +00:00, DRF and JavaScript write Z, whole seconds drop
        the microseconds. Three spellings, one moment, one hash.
        """
        assert normalize_instant(spelling) == "2026-09-07T10:00:00.000000Z"

    def test_payload_change_changes_the_hash(self):
        args = {
            "event_type": "chat_message",
            "occurred_at": OCCURRED,
            "source_system": "azus_chat",
            "raw_reference": "chat:1",
        }
        assert compute_content_hash(**args, payload="a") != compute_content_hash(
            **args, payload="b"
        )

    def test_unicode_is_not_escaped_away(self):
        """ensure_ascii=False on both sides, or Cyrillic hashes differently."""
        vectors = {v["name"]: v for v in golden_vectors()}
        canonical = vectors["unicode_payload_not_escaped"]["canonical_json"]
        assert "Алексом" in canonical
        assert "\\u" not in canonical


class TestDisambiguatorCollisions:
    def test_fieldless_identity_after_a_fielded_one(self, driver):
        """
        Ordering regression: the fielded entity already holds the empty
        disambiguator, so a later fieldless one must not claim it too.
        """
        work = driver.resolve_or_create_entity(
            user="default", surface_form="Alex", field_id="work"
        )
        loose = driver.resolve_or_create_entity(
            user="default", surface_form="Alex", field_id=""
        )
        assert work != loose
        assert driver.get_entity(work).disambiguator == ""
        assert driver.get_entity(loose).disambiguator == "alt"

        # And it is stable on a second call.
        assert driver.resolve_or_create_entity(
            user="default", surface_form="Alex", field_id=""
        ) == loose

    def test_many_fields_each_get_their_own_identity(self, driver):
        ids = {
            driver.resolve_or_create_entity(
                user="default", surface_form="Alex", field_id=f"field-{n}"
            )
            for n in range(6)
        }
        assert len(ids) == 6


class TestDefaultedOccurredAt:
    def test_our_clock_does_not_decide_identity(self, driver):
        """
        Regression. A caller who does not supply occurred_at gets one filled
        in — and if that invented moment entered the hash, ingesting the same
        message twice would make two events a millisecond apart. AC1 would
        fail on the most ordinary path there is.
        """
        payload = "Moved the Orion release review to Friday."
        first = driver.save_source_event(
            SourceEventRecord(raw_reference="chat:1"), payload=payload
        )
        second = driver.save_source_event(
            SourceEventRecord(raw_reference="chat:1"), payload=payload
        )
        assert first == second

    def test_a_supplied_occurred_at_still_separates_events(self, driver):
        """The fix must not blunt the hash for callers who do know."""
        monday = driver.save_source_event(
            SourceEventRecord(raw_reference="chat:1", occurred_at=OCCURRED),
            payload="standup",
        )
        tuesday = driver.save_source_event(
            SourceEventRecord(
                raw_reference="chat:1",
                occurred_at=datetime(2026, 9, 8, 10, 0, tzinfo=timezone.utc),
            ),
            payload="standup",
        )
        assert monday != tuesday

    def test_the_column_is_populated_either_way(self, driver):
        """Held out of the hash, not left empty in the row."""
        event_id = driver.save_source_event(SourceEventRecord(raw_reference="chat:2"))
        assert driver.get_source_event(event_id).occurred_at is not None


class TestConcurrentIdempotency:
    """
    The Python check in record_mention reads, then writes — two statements a
    second writer can slip between. Single-threaded tests pass either way,
    which is exactly why they are not enough: they would keep passing if the
    unique index were dropped tomorrow. These put real threads on it, so the
    index is the thing under test.
    """

    def test_concurrent_identical_mentions_collapse_to_one(self, driver):
        import threading

        event_id = driver.save_source_event(make_event())
        results: list[str] = []
        errors: list[Exception] = []
        start = threading.Barrier(8)

        def record() -> None:
            try:
                start.wait()
                results.append(
                    driver.record_mention(
                        EntityMentionRecord(
                            surface_form="Alex",
                            source_event_id=event_id,
                            field_id="work",
                        )
                    )
                )
            except Exception as exc:  # noqa: BLE001 - the point is to see it
                errors.append(exc)

        threads = [threading.Thread(target=record) for _ in range(8)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()

        assert not errors, f"record_mention raised under contention: {errors}"
        assert len(set(results)) == 1, "eight racing writers produced more than one mention"
        count = driver._conn().execute(
            "SELECT COUNT(*) FROM pdm_entity_mentions"
        ).fetchone()[0]
        assert count == 1

    def test_concurrent_identical_events_collapse_to_one(self, driver):
        import threading

        results: list[str] = []
        errors: list[Exception] = []
        start = threading.Barrier(8)

        def record() -> None:
            try:
                start.wait()
                results.append(
                    driver.save_source_event(
                        make_event(raw_reference="chat:race"), payload="same message"
                    )
                )
            except Exception as exc:  # noqa: BLE001
                errors.append(exc)

        threads = [threading.Thread(target=record) for _ in range(8)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()

        assert not errors, f"save_source_event raised under contention: {errors}"
        assert len(set(results)) == 1
