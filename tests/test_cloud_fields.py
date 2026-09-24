"""
CloudDriver's field/relationship routes, and EventLog over a fields-only
driver.

CloudDriver carries fields and links over Companion's HTTP routes, and writes
events there too (tests/test_cloud_events.py), but keeps no event table to
read back from. What matters here is the half EventLog has to get right: a
CloudDriver-backed EventLog does everything the field methods promise and
refuses, clearly, the event reads it cannot answer.
"""

from __future__ import annotations

from datetime import datetime, timezone
from unittest.mock import MagicMock, patch

import pytest

from pdm_memory import Memory
from pdm_memory.auth.jwt_handler import JWTAuth
from pdm_memory.core.signature import SignatureRecord
from pdm_memory.event_log import EventLog
from pdm_memory.storage.cloud_driver import CloudDriver
from pdm_memory.storage.errors import CloudConflictError, CloudNotFoundError
from pdm_memory.storage.events import SourceEventRecord
from pdm_memory.storage.fields import RelationshipRecord


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


def _driver() -> CloudDriver:
    return CloudDriver(auth=_auth(), base_url="http://localhost:8000")


def _log() -> EventLog:
    return EventLog(Memory(storage=_driver(), user="default"))


def _resp(status_code: int, body: dict | list, text: str = ""):
    resp = MagicMock()
    resp.status_code = status_code
    resp.json.return_value = body
    resp.text = text or str(body)
    return resp


class TestEventLogOverCloudDriver:
    """Construction, and which half of the facade a fields-only driver opens."""

    def test_a_cloud_driver_constructs_an_event_log(self):
        _log()  # does not raise

    def test_event_reads_are_refused(self):
        # record / ingest / extract_signatures write through Companion's event
        # routes (tests/test_cloud_events.py); there is no event table to read
        # back, so the reads stay local-only.
        log = _log()
        for call in (
            lambda: log.get("e-1"),
            lambda: log.find_by_hash("h"),
            lambda: log.signatures_for("e-1"),
            lambda: log.events(),
        ):
            with pytest.raises(RuntimeError, match="does not carry source events"):
                call()

    def test_mention_confirm_and_entities_are_refused(self):
        log = _log()
        for call in (
            lambda: log.mention("Alex"),
            lambda: log.confirm("m-1", "e-1"),
            lambda: log.pending(),
            lambda: log.check_integrity(),
            lambda: log.entities(),
            lambda: log.entity("e-1"),
            lambda: log.about("e-1"),
            lambda: log.merge("e-1", "e-2"),
        ):
            with pytest.raises(RuntimeError, match="does not carry source events"):
                call()


class TestAddFieldMembership:
    @patch("httpx.post")
    def test_posts_to_field_memberships(self, mock_post):
        mock_post.return_value = _resp(201, {"id": "fm-1"})

        result = _log().add_field_membership("subject:1", "westfield")

        assert result == "fm-1"
        _, kwargs = mock_post.call_args
        assert kwargs["json"]["entity_id"] == "subject:1"
        assert kwargs["json"]["field_id"] == "westfield"
        assert "valid_from" not in kwargs["json"]

    @patch("httpx.post")
    def test_a_datetime_bound_is_serialised(self, mock_post):
        mock_post.return_value = _resp(201, {"id": "fm-2"})
        when = datetime(2026, 3, 1, tzinfo=timezone.utc)

        _log().add_field_membership("subject:1", "westfield", when)

        _, kwargs = mock_post.call_args
        assert kwargs["json"]["valid_from"] == when.isoformat()

    @patch("httpx.post")
    def test_an_iso_string_bound_passes_through(self, mock_post):
        mock_post.return_value = _resp(201, {"id": "fm-3"})

        _log().add_field_membership(
            "subject:1", "westfield", "2026-03-01T00:00:00+00:00"
        )

        _, kwargs = mock_post.call_args
        assert kwargs["json"]["valid_from"] == "2026-03-01T00:00:00+00:00"

    @patch("httpx.post")
    def test_a_second_live_membership_raises_the_specific_conflict(self, mock_post):
        mock_post.return_value = _resp(
            409, {"error_code": "MEMBERSHIP_REFUSED", "detail": "already live"}
        )

        with pytest.raises(CloudConflictError) as exc:
            _log().add_field_membership("subject:1", "westfield")
        assert exc.value.error_code == "MEMBERSHIP_REFUSED"
        assert exc.value.status_code == 409


class TestEndFieldMembership:
    def test_has_no_cloud_route(self):
        with pytest.raises(NotImplementedError, match="authority event"):
            _log().end_membership("fm-1")


class TestFileAndUnfileFact:
    @patch("httpx.post")
    def test_files_a_signature_into_a_field(self, mock_post):
        mock_post.return_value = _resp(201, {"id": "sfm-1"})

        result = _log().file_fact("sig-1", "westfield")

        assert result == "sfm-1"
        path, kwargs = mock_post.call_args[0][0], mock_post.call_args[1]
        assert path.endswith("/api/v1/pdm/signature-field-memberships")
        assert kwargs["json"]["signature_id"] == "sig-1"

    @patch("httpx.post")
    def test_filing_the_same_field_twice_raises_the_specific_conflict(self, mock_post):
        mock_post.return_value = _resp(409, {"error_code": "MEMBERSHIP_REFUSED"})

        with pytest.raises(CloudConflictError) as exc:
            _log().file_fact("sig-1", "westfield")
        assert exc.value.error_code == "MEMBERSHIP_REFUSED"

    @patch("httpx.patch")
    def test_unfiling_patches_the_membership_by_id(self, mock_patch):
        mock_patch.return_value = _resp(200, {"id": "sfm-1", "valid_to": "…"})

        _log().unfile_fact("sfm-1", datetime(2026, 4, 1, tzinfo=timezone.utc))

        url, kwargs = mock_patch.call_args[0][0], mock_patch.call_args[1]
        assert url.endswith("/api/v1/pdm/signature-field-memberships/sfm-1")
        assert kwargs["json"]["valid_to"] == "2026-04-01T00:00:00+00:00"

    @patch("httpx.patch")
    def test_unfiling_without_at_defaults_to_now(self, mock_patch):
        mock_patch.return_value = _resp(200, {"id": "sfm-1"})

        _log().unfile_fact("sfm-1")

        _, kwargs = mock_patch.call_args
        assert kwargs["json"]["valid_to"]  # some ISO stamp was sent

    @patch("httpx.patch")
    def test_unfiling_an_already_closed_row_raises_the_specific_conflict(
        self, mock_patch
    ):
        mock_patch.return_value = _resp(
            409, {"error_code": "MEMBERSHIP_ALREADY_CLOSED"}
        )

        with pytest.raises(CloudConflictError) as exc:
            _log().unfile_fact("sfm-1")
        assert exc.value.error_code == "MEMBERSHIP_ALREADY_CLOSED"

    @patch("httpx.patch")
    def test_unfiling_someone_elses_row_raises_not_found(self, mock_patch):
        mock_patch.return_value = _resp(404, {"error_code": "MEMBERSHIP_NOT_FOUND"})

        with pytest.raises(CloudNotFoundError):
            _log().unfile_fact("sfm-not-mine")


class TestSignatureFieldsAndFieldsOf:
    @patch("httpx.get")
    def test_signature_fields_collects_field_ids_across_pages(self, mock_get):
        mock_get.side_effect = [
            _resp(
                200,
                {
                    "signature_field_memberships": [{"field_id": "westfield"}],
                    "next_cursor_id": "cursor-1",
                },
            ),
            _resp(
                200,
                {
                    "signature_field_memberships": [{"field_id": "family"}],
                    "next_cursor_id": None,
                },
            ),
        ]

        result = _log().fact_fields("sig-1")

        assert result == ["family", "westfield"]
        assert mock_get.call_count == 2
        second_params = mock_get.call_args_list[1][1]["params"]
        assert second_params["cursor_id"] == "cursor-1"

    @patch("httpx.get")
    def test_fields_of_reads_the_field_memberships_route(self, mock_get):
        mock_get.return_value = _resp(
            200,
            {"field_memberships": [{"field_id": "westfield"}], "next_cursor_id": None},
        )

        result = _log().fields_of("subject:1")

        assert result == ["westfield"]
        path = mock_get.call_args[0][0]
        assert path.endswith("/api/v1/pdm/field-memberships")

    @patch("httpx.get")
    def test_a_missing_key_raises_rather_than_returning_empty(self, mock_get):
        mock_get.return_value = _resp(200, {"unexpected": []})

        with pytest.raises(Exception, match="field_memberships"):
            _log().fields_of("subject:1")


class TestMembersOf:
    def test_has_no_cloud_route(self):
        with pytest.raises(NotImplementedError, match="roster"):
            _log().members_of("westfield")


class TestLinkAndEndLink:
    @patch("httpx.post")
    def test_links_two_entities(self, mock_post):
        mock_post.return_value = _resp(201, {"id": "rel-1"})

        result = _log().link("subject:1", "subject:2", "manager")

        assert result == "rel-1"
        _, kwargs = mock_post.call_args
        assert kwargs["json"]["relationship_type"] == "manager"
        assert kwargs["json"]["directionality"] == "directed"

    @patch("httpx.post")
    def test_a_second_live_link_of_the_same_kind_raises_the_specific_conflict(
        self, mock_post
    ):
        mock_post.return_value = _resp(409, {"error_code": "RELATIONSHIP_REFUSED"})

        with pytest.raises(CloudConflictError) as exc:
            _log().link("subject:1", "subject:2", "manager")
        assert exc.value.error_code == "RELATIONSHIP_REFUSED"

    @patch("httpx.patch")
    def test_ending_a_link_patches_it_closed(self, mock_patch):
        mock_patch.return_value = _resp(200, {"id": "rel-1", "state": "historical"})

        _log().end_link("rel-1", datetime(2026, 5, 1, tzinfo=timezone.utc))

        url, kwargs = mock_patch.call_args[0][0], mock_patch.call_args[1]
        assert url.endswith("/api/v1/pdm/relationships/rel-1")
        assert kwargs["json"]["valid_to"] == "2026-05-01T00:00:00+00:00"


class TestRelationshipEvidence:
    """
    spec §4.4 — ``EventLog.reinforce``/``apply_contrary_evidence`` dispatch
    by target type: a ``RelationshipRecord`` goes to the new evidence route,
    anything else (a bare string id, or a ``SignatureRecord``) goes to the
    existing, unchanged ``Memory.reinforce``/``apply_contrary_evidence``.
    """

    @patch("httpx.post")
    def test_reinforcing_a_relationship_cites_a_signature_by_id(self, mock_post):
        mock_post.return_value = _resp(
            201, {"relationship_id": "rel-1", "kind": "reinforce", "domains": ["*"]}
        )
        relationship = RelationshipRecord(
            id="rel-1",
            source_entity_id="subject:1",
            target_entity_id="entity:abc",
            relationship_type="colleague",
        )

        result = _log().reinforce(relationship, "sig-1")

        assert result["relationship_id"] == "rel-1"
        url = mock_post.call_args[0][0]
        assert url.endswith("/api/v1/pdm/relationships/rel-1/evidence")
        body = mock_post.call_args[1]["json"]
        assert body == {"kind": "reinforce", "signature_id": "sig-1"}

    @patch("httpx.post")
    def test_reinforcing_a_relationship_cites_a_signature_record(self, mock_post):
        mock_post.return_value = _resp(201, {"domains": ["*"]})
        relationship = RelationshipRecord(
            id="rel-1",
            source_entity_id="subject:1",
            target_entity_id="entity:abc",
            relationship_type="colleague",
        )

        _log().reinforce(relationship, SignatureRecord(id="sig-2"))

        body = mock_post.call_args[1]["json"]
        assert body == {"kind": "reinforce", "signature_id": "sig-2"}

    @patch("httpx.post")
    def test_contrary_evidence_on_a_relationship_cites_a_source_event(self, mock_post):
        mock_post.return_value = _resp(201, {"domains": ["*"]})
        relationship = RelationshipRecord(
            id="rel-1",
            source_entity_id="subject:1",
            target_entity_id="entity:abc",
            relationship_type="colleague",
        )

        _log().apply_contrary_evidence(relationship, SourceEventRecord(id="evt-1"))

        url = mock_post.call_args[0][0]
        assert url.endswith("/api/v1/pdm/relationships/rel-1/evidence")
        body = mock_post.call_args[1]["json"]
        assert body == {"kind": "contrary", "source_event_id": "evt-1"}

    def test_relationship_evidence_with_no_citation_is_refused_locally(self):
        relationship = RelationshipRecord(
            id="rel-1",
            source_entity_id="subject:1",
            target_entity_id="entity:abc",
            relationship_type="colleague",
        )

        with pytest.raises(ValueError, match="must cite an existing"):
            _log().reinforce(relationship, None)

    @patch("httpx.post")
    def test_resubmitting_the_same_evidence_raises_the_specific_conflict(
        self, mock_post
    ):
        mock_post.return_value = _resp(409, {"error_code": "EVIDENCE_ALREADY_APPLIED"})
        relationship = RelationshipRecord(
            id="rel-1",
            source_entity_id="subject:1",
            target_entity_id="entity:abc",
            relationship_type="colleague",
        )

        with pytest.raises(CloudConflictError) as exc:
            _log().reinforce(relationship, "sig-1")
        assert exc.value.error_code == "EVIDENCE_ALREADY_APPLIED"

    def test_a_bare_string_target_is_read_as_a_memory_not_a_relationship(self):
        # No relationship route is hit at all — dispatched straight to the
        # existing, unchanged Memory.reinforce.
        log = _log()
        with patch.object(log._memory, "reinforce") as mock_reinforce:
            log.reinforce("sig-1", "irrelevant-for-a-memory-target")

        mock_reinforce.assert_called_once_with("sig-1", coupling_score=0.5)

    def test_a_signature_record_target_dispatches_to_memory_apply_contrary(self):
        log = _log()
        sig = SignatureRecord(id="sig-1")
        with patch.object(log._memory, "apply_contrary_evidence") as mock_apply:
            log.apply_contrary_evidence(sig, "the new contrary fact")

        mock_apply.assert_called_once_with(
            sig,
            "the new contrary fact",
            coupling_score=0.5,
            persist_evidence=True,
            evidence_tags=None,
            evidence_shape=None,
        )


class TestRelated:
    @patch("httpx.get")
    def test_a_directed_link_is_followed_one_way(self, mock_get):
        mock_get.return_value = _resp(
            200,
            {
                "relationships": [
                    {
                        "source_entity_id": "subject:1",
                        "target_entity_id": "subject:2",
                        "relationship_type": "manager",
                        "directionality": "directed",
                    }
                ],
                "next_cursor_id": None,
            },
        )

        assert _log().related("subject:1") == {"subject:2"}

    @patch("httpx.get")
    def test_a_directed_link_does_not_reach_backward(self, mock_get):
        mock_get.return_value = _resp(
            200,
            {
                "relationships": [
                    {
                        "source_entity_id": "subject:1",
                        "target_entity_id": "subject:2",
                        "relationship_type": "manager",
                        "directionality": "directed",
                    }
                ],
                "next_cursor_id": None,
            },
        )

        assert _log().related("subject:2") == set()

    @patch("httpx.get")
    def test_a_symmetric_link_is_followed_both_ways(self, mock_get):
        mock_get.return_value = _resp(
            200,
            {
                "relationships": [
                    {
                        "source_entity_id": "subject:1",
                        "target_entity_id": "subject:2",
                        "relationship_type": "colleague",
                        "directionality": "symmetric",
                    }
                ],
                "next_cursor_id": None,
            },
        )

        assert _log().related("subject:1") == {"subject:2"}
        assert _log().related("subject:2") == {"subject:1"}

    @patch("httpx.get")
    def test_relationship_type_narrows_the_result(self, mock_get):
        mock_get.return_value = _resp(
            200,
            {
                "relationships": [
                    {
                        "source_entity_id": "subject:1",
                        "target_entity_id": "subject:2",
                        "relationship_type": "manager",
                        "directionality": "directed",
                    },
                    {
                        "source_entity_id": "subject:1",
                        "target_entity_id": "subject:3",
                        "relationship_type": "neighbour",
                        "directionality": "directed",
                    },
                ],
                "next_cursor_id": None,
            },
        )

        # EventLog.related() has no relationship_type parameter; the filter
        # is exercised directly on the storage method it delegates to.
        result = _driver().related_entities("subject:1", relationship_type="manager")
        assert result == {"subject:2"}


class TestExtractSignatures:
    """
    CloudDriver.extract_signatures — spec §2.3's cloud half.

    Called directly on the driver, not through EventLog: this driver has no
    local event table (see TestEventLogOverCloudDriver above), so there is
    nothing for ``_require_events()`` to allow through. The server decides
    everything that matters; this is a thin POST and a passed-through body.
    """

    @patch("httpx.post")
    def test_posts_to_the_extract_route(self, mock_post):
        mock_post.return_value = _resp(
            200,
            {"event_id": "e-1", "signature_ids": ["s-1"], "count": 1},
        )

        result = _driver().extract_signatures("e-1")

        assert result == {"event_id": "e-1", "signature_ids": ["s-1"], "count": 1}
        args, kwargs = mock_post.call_args
        assert args[0] == "http://localhost:8000/api/v1/pdm/source-events/e-1/extract"
        assert kwargs["json"] == {}

    @patch("httpx.post")
    def test_text_and_force_are_forwarded(self, mock_post):
        mock_post.return_value = _resp(
            200, {"event_id": "e-1", "signature_ids": [], "count": 0}
        )

        _driver().extract_signatures("e-1", "the message text", force=True)

        _, kwargs = mock_post.call_args
        assert kwargs["json"] == {"text": "the message text", "force": True}

    @patch("httpx.post")
    def test_omitted_text_is_not_sent_as_null(self, mock_post):
        mock_post.return_value = _resp(
            200, {"event_id": "e-1", "signature_ids": [], "count": 0}
        )

        _driver().extract_signatures("e-1")

        _, kwargs = mock_post.call_args
        assert "text" not in kwargs["json"]

    @patch("httpx.post")
    def test_an_unknown_event_raises_not_found(self, mock_post):
        mock_post.return_value = _resp(
            404, {"error_code": "EVENT_NOT_FOUND", "detail": "no such event"}
        )

        with pytest.raises(CloudNotFoundError):
            _driver().extract_signatures("no-such-event")

    @patch("httpx.post")
    def test_a_payload_mismatch_is_a_cloud_storage_error(self, mock_post):
        from pdm_memory.storage.errors import CloudStorageError

        mock_post.return_value = _resp(
            422,
            {"error_code": "PAYLOAD_MISMATCH", "detail": "not this event's content"},
        )

        with pytest.raises(CloudStorageError) as exc:
            _driver().extract_signatures("e-1", "wrong text")
        assert exc.value.status_code == 422
