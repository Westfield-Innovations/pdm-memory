"""
Findings from the review of PR #24. One test per finding, each failing before
its fix — and nothing here reaches beyond what was reported.
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
    drv = EventfulSQLiteDriver(db_path=str(tmp_path / "f.db"))
    yield drv
    drv.close()


@pytest.fixture()
def log(tmp_path):
    mem = Memory(storage=EventfulSQLiteDriver(db_path=str(tmp_path / "l.db")))
    yield EventLog(mem)
    mem.close()


class TestFinding1CrossFieldRace:
    """
    Two threads naming the same person in *different* fields both see no
    siblings, both pick the empty disambiguator, and the loser's read-back
    looks for its own field — finding nothing, because the winner filed the
    name under the other one.
    """

    def test_two_fields_at_once_both_get_an_identity(self, driver):
        errors: list[str] = []
        results: list[str] = []
        start = threading.Barrier(2)

        def create(field: str) -> None:
            try:
                start.wait()
                results.append(
                    driver.resolve_or_create_entity(
                        user="default", surface_form="Alex", field_id=field
                    )
                )
            except Exception as exc:  # noqa: BLE001
                errors.append(f"{type(exc).__name__}: {exc}")

        threads = [threading.Thread(target=create, args=(f,)) for f in ("work", "family")]
        for t in threads:
            t.start()
        for t in threads:
            t.join()

        assert not errors, errors
        assert len(set(results)) == 2, "two fields, two identities"


class TestFinding3MentionResolutionOnFailedTranslation:
    """
    When an entity could not be translated to the far side, the mention still
    carried resolution="user_confirmed" with no entity — landing in the
    unresolved queue, where resolve_mention refuses to touch it because a
    human grade is not revisable. Permanently stuck.
    """

    def test_an_unattributed_mention_arrives_unresolved(self, tmp_path):
        from pdm_memory.storage.event_sync import EventSync

        left = EventfulSQLiteDriver(db_path=str(tmp_path / "a.db"))
        right = EventfulSQLiteDriver(db_path=str(tmp_path / "b.db"))
        event_id = left.save_source_event(SourceEventRecord(raw_reference="c:1"), payload="x")
        mention = left.record_mention(
            EntityMentionRecord(surface_form="Alex", source_event_id=event_id, field_id="w")
        )
        entity = left.resolve_or_create_entity(
            user="default", surface_form="Alex", field_id="w"
        )
        left.resolve_mention(mention, entity_id=entity, method="user_confirmed")

        # Entity transfer fails; the mention must not claim a grade it cannot back.
        right.resolve_or_create_entity = lambda **kw: (_ for _ in ()).throw(RuntimeError("no"))
        EventSync(left, right).sync(direction="push")

        arrived = right.unresolved_mentions()
        assert len(arrived) == 1
        assert arrived[0].resolution == "unresolved", (
            "a mention with no entity cannot carry a human's grade"
        )
        assert arrived[0].resolved_at is None
        left.close()
        right.close()


class TestFinding4ReadPathParser:
    """
    events._parse_dt still went through fromisoformat, so on 3.10 a timestamp
    with seven fractional digits read back as None, __post_init__ substituted
    now(), and both the hash and the keyset cursor moved.
    """

    def test_the_read_path_uses_the_same_grammar_as_the_hash(self):
        import inspect

        from pdm_memory.storage import events

        source = inspect.getsource(events._parse_dt)
        assert "_parse_iso" in source, "the read path must use the hash's grammar"
        assert "datetime.fromisoformat" not in source

    def test_a_seven_digit_fraction_reads_back(self):
        from pdm_memory.storage.events import _parse_dt

        parsed = _parse_dt("2026-09-07T10:00:00.1234567Z")
        assert parsed is not None and parsed.microsecond == 123456


class TestFinding6ReuseCountAndCloudReturn:
    """
    claimed = bool(rowcount) reported False for a signature already linked to
    this same event, for an id that does not exist, and for another user's
    row. And the cloud driver returned None from a method the Protocol types
    as bool, so every fact on cloud counted as reused.
    """

    def test_relinking_to_the_same_event_is_not_a_reuse(self, driver):
        from pdm_memory.core.signature import SignatureRecord

        event_id = driver.save_source_event(SourceEventRecord(raw_reference="c:1"), payload="x")
        sig = SignatureRecord(compressed_fact="f", intent_tags=["a", "b", "c"])
        driver.save(sig)

        assert driver.link_signature(sig.id, source_event_id=event_id) is True
        assert driver.link_signature(sig.id, source_event_id=event_id) is True, (
            "the same event linking twice has not lost the signature to anyone"
        )

    def test_a_different_event_does_not_claim_it(self, driver):
        from pdm_memory.core.signature import SignatureRecord

        first = driver.save_source_event(SourceEventRecord(raw_reference="c:1"), payload="x")
        second = driver.save_source_event(SourceEventRecord(raw_reference="c:2"), payload="y")
        sig = SignatureRecord(compressed_fact="f", intent_tags=["a", "b", "c"])
        driver.save(sig)
        driver.link_signature(sig.id, source_event_id=first)

        assert driver.link_signature(sig.id, source_event_id=second) is False


class TestFinding7OffsetValidation:
    """The grammar accepted +03:99 as +04:39 and 2026-0907T as a date."""

    @pytest.mark.parametrize(
        "spelling",
        [
            "2026-09-07T10:00:00+03:99",
            "2026-09-07T10:00:00+0399",
            "2026-0907T10:00:00Z",
            "20260907T10:00:00Z",
        ],
    )
    def test_a_malformed_offset_or_date_is_refused(self, spelling):
        from pdm_memory.storage.event_hash import normalize_instant

        with pytest.raises(ValueError, match="ISO-8601"):
            normalize_instant(spelling)

    def test_the_new_forms_have_golden_vectors(self):
        import json
        import pathlib

        fixture = json.loads(
            (pathlib.Path(__file__).parent / "fixtures" / "event_hash_vectors.json")
            .read_text(encoding="utf-8")
        )
        names = {v["name"] for v in fixture["vectors"]}
        assert "basic_format_compact" in names
        assert "seven_fractional_digits_truncate" in names


class TestFinding9DoubleProbeInIngest:
    def test_ingest_asks_the_store_once(self, log, monkeypatch):
        calls: list[str] = []
        original = log._storage.find_event_by_hash
        monkeypatch.setattr(
            log._storage,
            "find_event_by_hash",
            lambda *a, **k: (calls.append("probe"), original(*a, **k))[1],
        )
        log.ingest(
            event=log.event(raw_reference="c:1"),
            payload="x",
            facts=[{"text": "f", "tags": ["a", "b", "c"]}],
        )
        assert calls == [], (
            "a fresh event needs no read at all: the insert's rowcount says it went in"
        )


class TestFinding11MergeUsesTheGuardedWrite:
    def test_every_statement_in_merge_goes_through_write(self):
        import inspect

        from pdm_memory.storage.event_store import EventStoreMixin

        body = inspect.getsource(EventStoreMixin.merge_entities)
        assert "self._run(" not in body


class TestFinding5ProvenanceCrossesTheSync:
    """
    Signatures carry two pointers and neither crossed. MemorySync moves
    signatures but its payload predates the columns; EventSync moved events,
    entities and mentions and never touched signatures. Both reported
    errors=0, and check_integrity called the result clean, because a pointer
    that is empty is not a pointer that dangles.

    The step was in the docstring — "then the signature links" — and was
    removed rather than written.
    """

    def _pair(self, tmp_path):
        left = EventfulSQLiteDriver(db_path=str(tmp_path / "left.db"))
        right = EventfulSQLiteDriver(db_path=str(tmp_path / "right.db"))
        return left, right

    def test_a_fact_keeps_the_message_it_came_from(self, tmp_path):
        from pdm_memory.storage.event_sync import EventSync
        from pdm_memory.sync import MemorySync

        left, right = self._pair(tmp_path)
        mem = Memory(storage=left)
        log = EventLog(mem)
        result = log.ingest(
            event=log.event(raw_reference="chat:123:msg:456", source_system="azus_chat"),
            payload="Moved the Orion release review to Friday.",
            facts=[
                {
                    "text": "Orion release moved to Friday",
                    "tags": ["orion", "release", "date"],
                    "about": "Alex",
                }
            ],
            field_id="work",
        )

        MemorySync(left, right).sync(direction="push")
        report = EventSync(left, right).sync(direction="push")

        assert report.errors == 0
        assert report.links_transferred == 1, report

        arrived = right.list_source_events()[0]
        assert arrived.raw_reference == "chat:123:msg:456"
        assert [s.compressed_fact for s in right.signatures_for_event(arrived.id)] == [
            "Orion release moved to Friday"
        ], "the fact arrived without the message it came from"

        entity = right.list_entities()[0]
        assert len(right.signatures_for_entity(entity.id)) == 1
        mem.close()
        right.close()

    def test_links_are_translated_not_copied(self, tmp_path):
        """
        Each side keys its own rows. A link copied verbatim would point at an
        id that means nothing on the far side — or worse, at a different row.
        """
        from pdm_memory.storage.event_sync import EventSync
        from pdm_memory.sync import MemorySync

        left, right = self._pair(tmp_path)
        # Give the right store an event of its own first, so the ids diverge.
        right.save_source_event(SourceEventRecord(raw_reference="other"), payload="other")

        mem = Memory(storage=left)
        log = EventLog(mem)
        log.ingest(
            event=log.event(raw_reference="chat:1"),
            payload="one",
            facts=[{"text": "a fact", "tags": ["a", "b", "c"]}],
        )
        MemorySync(left, right).sync(direction="push")
        EventSync(left, right).sync(direction="push")

        mine = next(e for e in right.list_source_events() if e.raw_reference == "chat:1")
        theirs = next(e for e in right.list_source_events() if e.raw_reference == "other")
        assert len(right.signatures_for_event(mine.id)) == 1
        assert right.signatures_for_event(theirs.id) == []
        mem.close()
        right.close()

    def test_a_signature_that_has_not_crossed_yet_is_reported(self, tmp_path):
        """
        EventSync does not move signatures — MemorySync does. Running them in
        the wrong order must say so rather than drop the links quietly.
        """
        from pdm_memory.storage.event_sync import EventSync

        left, right = self._pair(tmp_path)
        mem = Memory(storage=left)
        log = EventLog(mem)
        log.ingest(
            event=log.event(raw_reference="chat:1"),
            payload="one",
            facts=[{"text": "a fact", "tags": ["a", "b", "c"]}],
        )

        report = EventSync(left, right).sync(direction="push")  # no MemorySync
        assert report.links_missing_signature == 1, report
        assert "MemorySync" in report.advice
        mem.close()
        right.close()

    def test_integrity_notices_a_fact_with_no_source(self, driver):
        """
        Not an error — a signature written before the event layer legitimately
        has none — but a count the caller can see, where before there was
        nothing to look at.
        """
        from pdm_memory.core.signature import SignatureRecord

        event_id = driver.save_source_event(SourceEventRecord(raw_reference="c:1"), payload="x")
        linked = SignatureRecord(compressed_fact="linked", intent_tags=["a", "b", "c"])
        driver.save(linked)
        driver.link_signature(linked.id, source_event_id=event_id)
        driver.save(SignatureRecord(compressed_fact="loose", intent_tags=["a", "b", "c"]))

        report = driver.check_integrity()
        assert report.ok, "an absent pointer is not a broken one"
        assert report.signatures_without_provenance == 1
        assert "1 without a recorded source" in report.render()
