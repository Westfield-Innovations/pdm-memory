"""
``EventLog.extract_signatures`` — spec §2.3, the SDK's local half.

Unlike Companion's chat path, this store never held the event's payload (see
``SourceEventRecord.ensure_content_hash``), so there is no resolve-from-storage
step here: text always comes from the caller and is checked against the
event's own content_hash before anything is built from it.
"""

from __future__ import annotations

import json
from datetime import datetime, timezone
from unittest.mock import MagicMock

import pytest

from pdm_memory import Memory
from pdm_memory.event_log import EventLog, PayloadMismatch
from pdm_memory.storage.eventful_sqlite import EventfulSQLiteDriver

OCCURRED = datetime(2026, 9, 7, 10, 0, tzinfo=timezone.utc)
TEXT = "Orion review moved to Friday."


def _openai_client(content: str) -> MagicMock:
    # spec=["chat"] keeps AutoSignatureGenerator's duck-typing honest: an
    # unrestricted MagicMock auto-vivifies `.messages.create` too, and the
    # generator checks the Anthropic shape first.
    client = MagicMock(spec=["chat"])
    client.chat.completions.create.return_value = MagicMock(
        choices=[MagicMock(message=MagicMock(content=content))]
    )
    return client


@pytest.fixture()
def log(tmp_path):
    mem = Memory(storage=EventfulSQLiteDriver(db_path=str(tmp_path / "l.db")))
    yield EventLog(mem)
    mem.close()


def _record(log, *, payload=TEXT, **overrides):
    base = {
        "event_type": "chat_message",
        "occurred_at": OCCURRED,
        "source_system": "azus_chat",
        "raw_reference": "chat:1:msg:1",
    }
    base.update(overrides)
    event = log.event(**base)
    return log.record(event, payload=payload)


class TestRawTextFallback:
    def test_no_llm_client_stores_the_text_verbatim(self, log):
        event_id = _record(log)

        signatures = log.extract_signatures(event_id, TEXT)

        assert len(signatures) == 1
        assert signatures[0].compressed_fact == TEXT
        # signatures_for_event is itself the proof of the link: the query
        # filters by source_event_id, which SignatureRecord does not carry
        # as its own field.
        assert log.signatures_for(event_id) == signatures

    def test_a_second_call_is_idempotent(self, log):
        event_id = _record(log)
        first = log.extract_signatures(event_id, TEXT)
        second = log.extract_signatures(event_id, TEXT)

        assert {s.id for s in first} == {s.id for s in second}

    def test_force_extracts_again_without_duplicating(self, log):
        event_id = _record(log)
        log.extract_signatures(event_id, TEXT)

        again = log.extract_signatures(event_id, TEXT, force=True)

        assert len(again) == 1


class TestPayloadMismatch:
    def test_text_that_does_not_hash_to_the_event_is_refused(self, log):
        event_id = _record(log)

        with pytest.raises(PayloadMismatch):
            log.extract_signatures(event_id, "something else entirely")

    def test_an_unknown_event_id_is_a_lookup_error(self, log):
        with pytest.raises(LookupError):
            log.extract_signatures("no-such-event", TEXT)


class TestLlmClientPath:
    def test_the_generator_output_becomes_the_signature(self, log):
        event_id = _record(log)
        client = _openai_client(
            json.dumps(
                {
                    "compressed_fact": "The Orion review is now Friday",
                    "intent_tags": ["orion", "release", "schedule"],
                    "p_magnitude": 70,
                }
            )
        )

        signatures = log.extract_signatures(event_id, TEXT, llm_client=client)

        assert len(signatures) == 1
        assert signatures[0].compressed_fact == "The Orion review is now Friday"
        assert signatures[0].intent_tags == ["orion", "release", "schedule"]

    def test_a_generator_failure_extracts_nothing(self, log):
        event_id = _record(log)
        client = _openai_client("not json")

        signatures = log.extract_signatures(event_id, TEXT, llm_client=client)

        assert signatures == []


class TestOccurredAtAmbiguity:
    """
    Whether the original recording knew ``occurred_at`` or let it default is
    not kept anywhere the row can be asked about later — only the hash that
    resulted says so. The check has to accept whichever produced it.
    """

    def test_an_event_recorded_with_a_known_occurred_at_still_verifies(self, log):
        event_id = _record(log)  # occurred_at supplied by _record's base

        assert len(log.extract_signatures(event_id, TEXT)) == 1

    def test_an_event_recorded_with_no_occurred_at_still_verifies(self, log):
        event = log.event(
            event_type="chat_message",
            source_system="azus_chat",
            raw_reference="chat:2:msg:2",
        )
        event_id = log.record(event, payload="Renew the domain by Friday.")

        signatures = log.extract_signatures(event_id, "Renew the domain by Friday.")
        assert len(signatures) == 1
