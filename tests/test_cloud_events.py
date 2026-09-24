"""EventLog.record / ingest / extract_signatures over a CloudDriver (spec §7 save_event, extract_signatures)."""

from __future__ import annotations

from datetime import datetime, timezone
from unittest.mock import MagicMock, patch

import pytest

from pdm_memory import Memory
from pdm_memory.auth.jwt_handler import JWTAuth
from pdm_memory.core.signature import SignatureRecord
from pdm_memory.event_log import EventLog, PayloadMismatch
from pdm_memory.storage.cloud_driver import CloudDriver
from pdm_memory.storage.errors import CloudNotFoundError, CloudStorageError
from pdm_memory.storage.event_hash import compute_content_hash

TEXT = "The Orion review moved to Friday."


def _auth() -> JWTAuth:
    import base64
    import json
    import time

    payload = (
        base64.urlsafe_b64encode(json.dumps({"exp": time.time() + 3600}).encode())
        .decode()
        .rstrip("=")
    )
    return JWTAuth(token=f"x.{payload}.y")


def _resp(status_code: int, body):
    resp = MagicMock()
    resp.status_code = status_code
    resp.json.return_value = body
    resp.text = str(body)
    return resp


@pytest.fixture
def driver() -> CloudDriver:
    return CloudDriver(auth=_auth(), base_url="http://localhost:8000")


@pytest.fixture
def log(driver) -> EventLog:
    return EventLog(Memory(storage=driver, user="default"))


class TestRecord:
    def test_posts_the_block_without_the_payload(self, log, driver):
        event = log.event("chat_message", source_system="sdk", raw_reference="sdk://1")
        with patch.object(
            driver,
            "_post",
            return_value=_resp(201, {"id": "ev-1", "deduplicated": False}),
        ) as post:
            event_id = log.record(event, payload=TEXT)

        path, block = post.call_args.args
        assert path == "/api/v1/pdm/source-events"
        assert "payload" not in block
        assert event_id == "ev-1" and event.id == "ev-1"
        assert event.was_deduplicated is False

    def test_an_unknown_occurred_at_is_held_out_of_the_hash(self, log, driver):
        event = log.event("chat_message", source_system="sdk", raw_reference="sdk://1")
        with patch.object(
            driver,
            "_post",
            return_value=_resp(201, {"id": "ev-1", "deduplicated": False}),
        ) as post:
            log.record(event, payload=TEXT)

        block = post.call_args.args[1]
        assert block["content_hash"] == compute_content_hash(
            event_type="chat_message",
            occurred_at=None,
            source_system="sdk",
            raw_reference="sdk://1",
            payload=TEXT,
        )
        assert block["occurred_at"]  # the column still gets a value

    def test_a_known_occurred_at_is_in_the_hash(self, log, driver):
        when = datetime(2026, 9, 1, 12, tzinfo=timezone.utc)
        event = log.event("chat_message", occurred_at=when, raw_reference="sdk://2")
        with patch.object(
            driver,
            "_post",
            return_value=_resp(201, {"id": "ev-2", "deduplicated": False}),
        ) as post:
            log.record(event, payload=TEXT)

        assert post.call_args.args[1]["content_hash"] == compute_content_hash(
            event_type="chat_message",
            occurred_at=when,
            source_system="chat",
            raw_reference="sdk://2",
            payload=TEXT,
        )

    def test_a_repeat_reports_deduplicated(self, log, driver):
        event = log.event("chat_message")
        with patch.object(
            driver,
            "_post",
            return_value=_resp(200, {"id": "ev-1", "deduplicated": True}),
        ):
            log.record(event, payload=TEXT)
        assert event.was_deduplicated is True


class TestIngest:
    def _wire(self, driver, *, linked=(True, True), mentions=None):
        driver.save_source_event = MagicMock(
            side_effect=lambda event, payload="": (
                setattr(event, "was_deduplicated", False) or "ev-1"
            )
        )
        driver.attach_signature = MagicMock(
            side_effect=[
                {"linked": flag, "mentions": (mentions or {}).get(i, [])}
                for i, flag in enumerate(linked)
            ]
        )
        driver.file_signature_in_field = MagicMock(return_value="sfm-1")

    def test_one_event_many_signatures(self, log, driver):
        self._wire(driver)
        with patch.object(log._memory, "save", side_effect=["sig-1", "sig-2"]):
            out = log.ingest(
                event=log.event("chat_message"),
                facts=[{"text": "A"}, {"text": "B"}],
                payload=TEXT,
            )

        assert out == {
            "source_event_id": "ev-1",
            "signature_ids": ["sig-1", "sig-2"],
            "signatures_reused": 0,
            "entity_ids": {},
            "deduplicated": False,
        }
        assert [c.args[:2] for c in driver.attach_signature.call_args_list] == [
            ("ev-1", "sig-1"),
            ("ev-1", "sig-2"),
        ]
        driver.file_signature_in_field.assert_not_called()

    def test_a_fact_already_on_another_event_counts_as_reused(self, log, driver):
        self._wire(driver, linked=(False, True))
        with patch.object(log._memory, "save", side_effect=["sig-1", "sig-2"]):
            out = log.ingest(
                event=log.event("chat_message"), facts=[{"text": "A"}, {"text": "B"}]
            )
        assert out["signatures_reused"] == 1

    def test_about_travels_as_a_mention_and_field_id_files_the_fact(self, log, driver):
        self._wire(
            driver,
            linked=(True,),
            mentions={0: [{"mention_id": "m", "entity_id": "ent-1"}]},
        )
        with patch.object(log._memory, "save", side_effect=["sig-1"]):
            out = log.ingest(
                event=log.event("chat_message"),
                facts=[
                    {"text": "Roman shipped DQS", "about": "Roman", "tags": ["work"]}
                ],
                field_id="westfield",
            )

        assert driver.attach_signature.call_args.kwargs["entities"] == [
            {"surface_form": "Roman", "field_id": "westfield"}
        ]
        assert out["entity_ids"] == {"Roman": "ent-1"}
        driver.file_signature_in_field.assert_called_once_with(
            "sig-1", "westfield", user=log._user
        )


class TestExtractSignatures:
    def test_delegates_and_reads_the_records_back(self, log, driver):
        record = SignatureRecord(compressed_fact=TEXT)
        with (
            patch.object(
                driver,
                "extract_signatures",
                return_value={
                    "event_id": "ev-1",
                    "signature_ids": ["sig-1"],
                    "count": 1,
                },
            ) as extract,
            patch.object(driver, "get", return_value=record),
        ):
            out = log.extract_signatures("ev-1", TEXT, force=True)

        extract.assert_called_once_with("ev-1", TEXT, force=True, user=log._user)
        assert out == [record]

    def test_an_llm_client_is_refused_not_ignored(self, log):
        with pytest.raises(ValueError, match="server"):
            log.extract_signatures("ev-1", TEXT, llm_client=object())

    def test_a_mismatch_arrives_as_payload_mismatch(self, log, driver):
        with (
            patch.object(
                driver,
                "extract_signatures",
                side_effect=CloudStorageError(
                    'Cloud HTTP 422 for /x body={"error_code":"PAYLOAD_MISMATCH"}',
                    status_code=422,
                ),
            ),
            pytest.raises(PayloadMismatch),
        ):
            log.extract_signatures("ev-1", "not the event")

    def test_someone_elses_event_arrives_as_lookup_error(self, log, driver):
        with (
            patch.object(
                driver,
                "extract_signatures",
                side_effect=CloudNotFoundError("nope", status_code=404),
            ),
            pytest.raises(LookupError),
        ):
            log.extract_signatures("ev-1", TEXT)

    def test_other_server_errors_pass_through(self, log, driver):
        with (
            patch.object(
                driver,
                "extract_signatures",
                side_effect=CloudStorageError("boom", status_code=500),
            ),
            pytest.raises(CloudStorageError),
        ):
            log.extract_signatures("ev-1", TEXT)
