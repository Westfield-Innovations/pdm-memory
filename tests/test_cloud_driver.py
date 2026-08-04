"""CloudDriver — fail-fast errors + full signature payload round-trip."""

from datetime import datetime, timezone
from unittest.mock import MagicMock, patch

import pytest

from pdm_memory.auth.jwt_handler import JWTAuth
from pdm_memory.core.signature import SignatureRecord
from pdm_memory.storage.cloud_driver import CloudDriver
from pdm_memory.storage.errors import CloudStorageError


def _auth() -> JWTAuth:
    # Far-future exp so ensure_fresh does not block unit tests
    import base64
    import json
    import time

    payload = base64.urlsafe_b64encode(
        json.dumps({"exp": time.time() + 3600}).encode()
    ).decode().rstrip("=")
    token = f"x.{payload}.y"
    return JWTAuth(token=token)


def _driver() -> CloudDriver:
    return CloudDriver(auth=_auth(), base_url="http://localhost:8000")


class TestCloudPayloadRoundTrip:
    def test_payload_includes_temporal_and_validation(self):
        deadline = datetime(2026, 8, 1, tzinfo=timezone.utc)
        sig = SignatureRecord(
            id="11111111-1111-1111-1111-111111111111",
            user="alice",
            compressed_fact="Meeting on Friday",
            source="chat",
            p_magnitude=70.0,
            intent_tags=["meeting", "friday", "deadline"],
            domain="reminder",
            drawer_domain="calendar",
            validation_prediction_total=4,
            validation_prediction_correct=3,
            t_deadline=deadline,
            urgency_rate=3.0,
            decay_rate=0.9,  # SDK legacy — must be clamped for API
            retrieval_count=7,
        )
        payload = CloudDriver._record_to_payload(sig)

        assert payload["t_deadline"] == deadline.isoformat()
        assert payload["urgency_rate"] == 3.0
        assert payload["validation_prediction_total"] == 4
        assert payload["validation_prediction_correct"] == 3
        assert payload["domain"] == "reminder"
        assert payload["id"] == sig.id
        assert payload["decay_rate"] == 0.05  # clamped from 0.9
        assert payload["source"] == "azus_chat"  # SDK "chat" mapped for Companion
        assert payload["metadata"]["_pdm_sdk"]["client_id"] == sig.id
        assert payload["metadata"]["_pdm_sdk"]["decay_rate_sdk"] == 0.9
        assert payload["metadata"]["_pdm_sdk"]["source_sdk"] == "chat"

    def test_payload_rejects_too_few_intent_tags(self):
        sig = SignatureRecord(
            user="alice",
            compressed_fact="Too few tags",
            source="azus_chat",
            p_magnitude=70.0,
            intent_tags=["only", "two"],
            drawer_domain="calendar",
        )
        with pytest.raises(ValueError, match="intent_tags"):
            CloudDriver._record_to_payload(sig)

    def test_payload_rejects_low_p_magnitude(self):
        sig = SignatureRecord(
            user="alice",
            compressed_fact="Too low pressure",
            source="azus_chat",
            p_magnitude=40.0,
            intent_tags=["a", "b", "c"],
            drawer_domain="calendar",
        )
        with pytest.raises(ValueError, match="p_magnitude"):
            CloudDriver._record_to_payload(sig)

    def test_payload_preserves_valid_companion_source(self):
        sig = SignatureRecord(
            user="alice",
            compressed_fact="Manual note",
            source="manual",
            p_magnitude=55.0,
            intent_tags=["a", "b", "c"],
            drawer_domain="calendar",
        )
        payload = CloudDriver._record_to_payload(sig)
        assert payload["source"] == "manual"

    def test_payload_to_record_restores_fields(self):
        deadline = datetime(2026, 8, 1, tzinfo=timezone.utc)
        payload = {
            "id": "22222222-2222-2222-2222-222222222222",
            "user": "bob",
            "compressed_fact": "Ship report",
            "source": "chat",
            "p_magnitude": 80.0,
            "t_persistence": 30.0,
            "phase_privilege": 1.0,
            "intent_tags": ["ship", "report", "work"],
            "question_regime": "engineering",
            "drawer": "projects",
            "t_deadline": deadline.isoformat(),
            "urgency_rate": 4.0,
            "decay_rate": 0.05,
            "retrieval_count": 2,
            "validation_prediction_total": 10,
            "validation_prediction_correct": 8,
            "metadata": {
                "_pdm_sdk": {
                    "domain": "reminder",
                    "client_id": "22222222-2222-2222-2222-222222222222",
                }
            },
        }
        rec = CloudDriver._payload_to_record(payload)
        assert rec.id == payload["id"]
        assert rec.t_deadline == deadline
        assert rec.urgency_rate == 4.0
        assert rec.domain == "reminder"
        assert rec.drawer_domain == "projects"
        assert rec.validation_prediction_correct == 8
        assert "_pdm_sdk" not in rec.metadata


class TestCloudFailFast:
    @patch("httpx.get")
    def test_list_raises_on_500(self, mock_get):
        mock_resp = MagicMock()
        mock_resp.status_code = 500
        mock_resp.text = "boom"
        mock_get.return_value = mock_resp

        with pytest.raises(CloudStorageError) as exc:
            _driver().list()
        assert exc.value.status_code == 500

    @patch("httpx.get")
    def test_get_404_returns_none(self, mock_get):
        mock_resp = MagicMock()
        mock_resp.status_code = 404
        mock_resp.text = "missing"
        mock_get.return_value = mock_resp

        assert _driver().get("missing-id") is None

    @patch("httpx.get")
    def test_get_503_raises(self, mock_get):
        mock_resp = MagicMock()
        mock_resp.status_code = 503
        mock_resp.text = "unavailable"
        mock_get.return_value = mock_resp

        with pytest.raises(CloudStorageError) as exc:
            _driver().get("any-id")
        assert exc.value.status_code == 503

    @patch("httpx.patch")
    def test_update_raises_on_failure(self, mock_patch):
        mock_resp = MagicMock()
        mock_resp.status_code = 401
        mock_resp.text = "auth"
        mock_patch.return_value = mock_resp

        with pytest.raises(CloudStorageError):
            _driver().update("id-1", user="u", p_magnitude=90.0)

    @patch("httpx.get")
    def test_list_parses_companion_signatures_key(self, mock_get):
        mock_resp = MagicMock()
        mock_resp.status_code = 200
        mock_resp.json.return_value = {
            "count": 1,
            "signatures": [
                {
                    "id": "aaaaaaaa-bbbb-cccc-dddd-eeeeeeeeeeee",
                    "user": "alice",
                    "compressed_fact": "From companion shape",
                    "source": "azus_chat",
                    "p_magnitude": 70.0,
                    "intent_tags": ["a", "b", "c"],
                    "drawer": "research",
                }
            ],
        }
        mock_get.return_value = mock_resp

        rows = _driver().list(limit=10)
        assert len(rows) == 1
        assert rows[0].compressed_fact == "From companion shape"
        mock_get.assert_called_once()
        assert mock_get.call_args.kwargs["params"]["min_p"] == 0.0
        assert "min_pressure" not in mock_get.call_args.kwargs["params"]

    @patch("httpx.get")
    def test_list_raises_on_missing_signatures_key(self, mock_get):
        mock_resp = MagicMock()
        mock_resp.status_code = 200
        mock_resp.json.return_value = {"count": 0}
        mock_get.return_value = mock_resp

        with pytest.raises(CloudStorageError, match="signatures"):
            _driver().list()

    @patch("httpx.get")
    def test_list_raises_on_non_dict_body(self, mock_get):
        mock_resp = MagicMock()
        mock_resp.status_code = 200
        mock_resp.json.return_value = []
        mock_get.return_value = mock_resp

        with pytest.raises(CloudStorageError, match="body type"):
            _driver().list()

    @patch("httpx.delete")
    def test_delete_uses_hard_delete(self, mock_delete):
        mock_resp = MagicMock()
        mock_resp.status_code = 204
        mock_resp.text = ""
        mock_delete.return_value = mock_resp

        _driver().delete("aaaaaaaa-bbbb-cccc-dddd-eeeeeeeeeeee")
        mock_delete.assert_called_once()
        url = mock_delete.call_args.args[0]
        assert url.endswith("/api/v1/pdm/signatures/aaaaaaaa-bbbb-cccc-dddd-eeeeeeeeeeee")
        assert not url.endswith("/")

    @patch("httpx.get")
    def test_list_drawers_parses_companion_drawers_key(self, mock_get):
        mock_resp = MagicMock()
        mock_resp.status_code = 200
        mock_resp.json.return_value = {
            "drawers": [
                {"domain": "research", "signature_count": 3, "avg_pressure": 80.0},
            ]
        }
        mock_get.return_value = mock_resp

        drawers = _driver().list_drawers()
        assert len(drawers) == 1
        assert drawers[0].domain == "research"
        assert drawers[0].signature_count == 3


class TestSyncNoDuplicateOnCloudError:
    def test_push_counts_error_when_get_fails(self, tmp_path):
        from pdm_memory.storage.sqlite_driver import SQLiteDriver
        from pdm_memory.sync import MemorySync

        local = SQLiteDriver(db_path=str(tmp_path / "sync.db"))
        sig = SignatureRecord(
            user="u",
            compressed_fact="Local only",
            source="chat",
            p_magnitude=60.0,
            intent_tags=["local", "only", "test"],
        )
        local.save(sig)

        cloud = MagicMock()
        cloud.get.side_effect = CloudStorageError("down", status_code=503)
        cloud.save = MagicMock()

        report = MemorySync(local=local, cloud=cloud).sync(user="u", direction="push")
        assert report.errors == 1
        assert report.pushed == 0
        cloud.save.assert_not_called()
        local.close()
