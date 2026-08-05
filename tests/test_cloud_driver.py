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
        assert mock_get.call_args.args[0].endswith("/api/v1/pdm/signatures")
        assert mock_get.call_args.kwargs["params"]["min_p"] == 0.0
        assert "min_pressure" not in mock_get.call_args.kwargs["params"]
        assert "origin_index" not in mock_get.call_args.kwargs["params"]

    @patch("httpx.get")
    def test_list_walks_pages_via_next_cursor(self, mock_get):
        from pdm_memory.storage.cloud_driver import _API_PAGE_MAX

        def _item(i: int):
            return {
                "id": f"id-{i:04d}",
                "user": "alice",
                "compressed_fact": f"Fact {i}",
                "source": "manual",
                "p_magnitude": float(100 - i),
                "intent_tags": ["a", "b", "c"],
                "drawer": "research",
            }

        page1 = {
            "signatures": [_item(i) for i in range(_API_PAGE_MAX)],
            "next_cursor_id": f"id-{_API_PAGE_MAX - 1:04d}",
        }
        page2 = {
            "signatures": [_item(_API_PAGE_MAX + i) for i in range(50)],
            "next_cursor_id": None,
        }
        mock_resp = MagicMock()
        mock_resp.status_code = 200
        mock_resp.json.side_effect = [page1, page2]
        mock_get.return_value = mock_resp

        rows = _driver().list(limit=_API_PAGE_MAX + 50)
        assert len(rows) == _API_PAGE_MAX + 50
        assert mock_get.call_count == 2
        assert mock_get.call_args_list[0].args[0].endswith("/api/v1/pdm/signatures")
        first_params = mock_get.call_args_list[0].kwargs["params"]
        second_params = mock_get.call_args_list[1].kwargs["params"]
        assert first_params["limit"] == _API_PAGE_MAX
        assert "cursor_id" not in first_params
        assert second_params["cursor_id"] == f"id-{_API_PAGE_MAX - 1:04d}"
        assert second_params["limit"] == 50

    @patch("httpx.get")
    def test_list_passes_initial_cursor_id(self, mock_get):
        mock_resp = MagicMock()
        mock_resp.status_code = 200
        mock_resp.json.return_value = {
            "signatures": [
                {
                    "id": "id-2",
                    "user": "alice",
                    "compressed_fact": "After cursor",
                    "source": "manual",
                    "p_magnitude": 60.0,
                    "intent_tags": ["a", "b", "c"],
                    "drawer": "research",
                }
            ],
            "next_cursor_id": None,
        }
        mock_get.return_value = mock_resp

        rows = _driver().list(limit=10, cursor_id="id-1")
        assert len(rows) == 1
        assert mock_get.call_args.kwargs["params"]["cursor_id"] == "id-1"

    @patch("httpx.get")
    def test_count_uses_pagination_total(self, mock_get):
        mock_resp = MagicMock()
        mock_resp.status_code = 200
        mock_resp.json.return_value = {
            "signatures": [
                {
                    "id": "x",
                    "user": "alice",
                    "compressed_fact": "one",
                    "source": "manual",
                    "p_magnitude": 70,
                    "intent_tags": ["a", "b", "c"],
                    "drawer": "research",
                }
            ],
            "pagination": {
                "page_size": 1,
                "total_count": 278,
            },
        }
        mock_get.return_value = mock_resp

        assert _driver().count() == 278
        assert mock_get.call_args.args[0].endswith("/api/v1/pdm/signatures")
        params = mock_get.call_args.kwargs["params"]
        assert params["limit"] == 1
        assert params["min_p"] == 0.0

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

    @patch("httpx.patch")
    def test_delete_soft_via_is_deleted_patch(self, mock_patch):
        mock_resp = MagicMock()
        mock_resp.status_code = 200
        mock_resp.text = "{}"
        mock_patch.return_value = mock_resp

        mid = "aaaaaaaa-bbbb-cccc-dddd-eeeeeeeeeeee"
        _driver().delete(mid)
        mock_patch.assert_called_once()
        url = mock_patch.call_args.args[0]
        assert url.endswith(f"/api/v1/pdm/signatures/{mid}")
        assert mock_patch.call_args.kwargs["json"] == {"is_deleted": True}

    @patch("httpx.delete")
    def test_hard_delete_uses_delete_method(self, mock_delete):
        mock_resp = MagicMock()
        mock_resp.status_code = 204
        mock_resp.text = ""
        mock_delete.return_value = mock_resp

        mid = "aaaaaaaa-bbbb-cccc-dddd-eeeeeeeeeeee"
        _driver().hard_delete(mid)
        mock_delete.assert_called_once()
        url = mock_delete.call_args.args[0]
        assert url.endswith(f"/api/v1/pdm/signatures/{mid}")
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


class TestCloudBatchMethods:
    """save_batch / get_many / update_batch — bulk routes, fail-fast (no sequential fallback)."""

    @patch("httpx.post")
    def test_save_batch_uses_ingest_batch(self, mock_post):
        mock_resp = MagicMock()
        mock_resp.status_code = 200
        mock_resp.content = b"{}"
        mock_resp.json.return_value = {
            "results": [
                {"index": 0, "id": "id-a", "error": None},
                {"index": 1, "id": None, "error": "Validation failed"},
            ]
        }
        mock_post.return_value = mock_resp

        s1 = SignatureRecord(
            user="alice",
            compressed_fact="Batch fact A",
            source="azus_chat",
            p_magnitude=60.0,
            intent_tags=["a", "b", "c"],
            drawer_domain="research",
        )
        s2 = SignatureRecord(
            user="alice",
            compressed_fact="Batch fact B",
            source="azus_chat",
            p_magnitude=61.0,
            intent_tags=["a", "b", "c"],
            drawer_domain="research",
        )
        results = _driver().save_batch([s1, s2])
        mock_post.assert_called_once()
        url = mock_post.call_args.args[0]
        assert url.endswith("/api/v1/pdm/ingest/batch")
        body = mock_post.call_args.kwargs["json"]
        assert len(body["items"]) == 2
        assert results[0].id == "id-a" and results[0].error is None
        assert results[1].id is None and "Validation" in (results[1].error or "")

    def test_save_batch_client_validates_before_http(self):
        bad = SignatureRecord(
            user="alice",
            compressed_fact="too few tags",
            source="azus_chat",
            p_magnitude=60.0,
            intent_tags=["only", "two"],
            drawer_domain="research",
        )
        with patch("httpx.post") as mock_post:
            results = _driver().save_batch([bad])
            mock_post.assert_not_called()
        assert results[0].id is None
        assert "intent_tags" in (results[0].error or "")

    @patch("httpx.post")
    def test_save_batch_raises_on_404(self, mock_post):
        batch_404 = MagicMock()
        batch_404.status_code = 404
        batch_404.text = "not found"
        batch_404.content = b"not found"
        mock_post.return_value = batch_404

        sig = SignatureRecord(
            user="alice",
            compressed_fact="No fallback single",
            source="azus_chat",
            p_magnitude=60.0,
            intent_tags=["a", "b", "c"],
            drawer_domain="research",
        )
        with pytest.raises(CloudStorageError) as exc:
            _driver().save_batch([sig])
        assert exc.value.status_code == 404
        mock_post.assert_called_once()
        assert mock_post.call_args.args[0].endswith("/ingest/batch")

    @patch("httpx.post")
    def test_save_batch_mid_stream_404_raises_after_partial(self, mock_post):
        """404 on a later chunk raises; no sequential re-ingest of prior or remaining rows."""
        from pdm_memory.storage.cloud_driver import _API_BATCH_MAX

        def _sig(i: int) -> SignatureRecord:
            return SignatureRecord(
                user="alice",
                compressed_fact=f"Chunk fact {i}",
                source="azus_chat",
                p_magnitude=60.0,
                intent_tags=["a", "b", "c"],
                drawer_domain="research",
            )

        n = _API_BATCH_MAX + 2
        sigs = [_sig(i) for i in range(n)]

        ok_chunk = MagicMock()
        ok_chunk.status_code = 200
        ok_chunk.content = b"{}"
        ok_chunk.json.return_value = {
            "results": [
                {"index": i, "id": f"bulk-{i}", "error": None}
                for i in range(_API_BATCH_MAX)
            ]
        }
        gone = MagicMock()
        gone.status_code = 404
        gone.text = "not found"
        gone.content = b"not found"
        mock_post.side_effect = [ok_chunk, gone]

        with pytest.raises(CloudStorageError) as exc:
            _driver().save_batch(sigs)
        assert exc.value.status_code == 404
        assert mock_post.call_count == 2
        assert all("/ingest/batch" in c.args[0] for c in mock_post.call_args_list)

    @patch("httpx.post")
    def test_get_many_404_raises(self, mock_post):
        gone = MagicMock()
        gone.status_code = 404
        gone.text = "not found"
        gone.content = b"not found"
        mock_post.return_value = gone

        with pytest.raises(CloudStorageError) as exc:
            _driver().get_many(["id-1"])
        assert exc.value.status_code == 404

    @patch("httpx.post")
    def test_update_batch_404_raises(self, mock_post):
        gone = MagicMock()
        gone.status_code = 404
        gone.text = "not found"
        gone.content = b"not found"
        mock_post.return_value = gone

        with pytest.raises(CloudStorageError) as exc:
            _driver().update_batch([("mid-1", {"p_magnitude": 80.0})])
        assert exc.value.status_code == 404

    @patch("httpx.post")
    def test_get_many_uses_batch_get(self, mock_post):
        mock_resp = MagicMock()
        mock_resp.status_code = 200
        mock_resp.content = b"{}"
        mock_resp.json.return_value = {
            "signatures": [
                {
                    "id": "aaaaaaaa-bbbb-cccc-dddd-eeeeeeeeeeee",
                    "compressed_fact": "Found",
                    "p_magnitude": 70,
                    "t_persistence": 30,
                    "intent_tags": ["a", "b", "c"],
                    "source": "manual",
                    "user": "alice",
                    "drawer": "research",
                    "is_deleted": False,
                }
            ]
        }
        mock_post.return_value = mock_resp

        mid = "aaaaaaaa-bbbb-cccc-dddd-eeeeeeeeeeee"
        out = _driver().get_many([mid, "missing-id"])
        assert mid in out
        assert out[mid].compressed_fact == "Found"
        mock_post.assert_called_once()
        assert mock_post.call_args.args[0].endswith("/signatures/batch-get")
        assert mock_post.call_args.kwargs["json"]["ids"] == [mid, "missing-id"]

    @patch("httpx.post")
    def test_update_batch_uses_batch_update(self, mock_post):
        mock_resp = MagicMock()
        mock_resp.status_code = 200
        mock_resp.content = b"{}"
        mock_resp.json.return_value = {
            "results": [
                {"index": 0, "id": "mid-1", "error": None},
                {
                    "index": 1,
                    "id": "mid-2",
                    "error": "Memory not found or wrong user",
                },
            ]
        }
        mock_post.return_value = mock_resp

        results = _driver().update_batch(
            [
                ("mid-1", {"p_magnitude": 80.0}),
                ("mid-2", {"p_magnitude": 81.0}),
            ]
        )
        assert results[0].error is None and results[0].id == "mid-1"
        assert results[1].error and "not found" in results[1].error
        assert mock_post.call_args.args[0].endswith("/signatures/batch-update")
        body = mock_post.call_args.kwargs["json"]
        assert body["updates"][0] == {"id": "mid-1", "p_magnitude": 80.0}

    def test_get_many_empty(self):
        assert _driver().get_many([]) == {}

    def test_update_batch_empty(self):
        assert _driver().update_batch([]) == []

    def test_payload_includes_idempotency_key(self):
        sig = SignatureRecord(
            user="alice",
            compressed_fact="Idempotent fact",
            source="manual",
            p_magnitude=70.0,
            intent_tags=["a", "b", "c"],
            drawer_domain="calendar",
            idempotency_key="pay-123",
        )
        payload = CloudDriver._record_to_payload(sig)
        assert payload["idempotency_key"] == "pay-123"
        assert payload["metadata"]["_idempotency_key"] == "pay-123"

    @patch("httpx.get")
    def test_find_by_idempotency_key_hits_lookup_route(self, mock_get):
        mid = "11111111-2222-3333-4444-555555555555"
        mock_resp = MagicMock()
        mock_resp.status_code = 200
        mock_resp.content = b"{}"
        mock_resp.json.return_value = {
            "id": mid,
            "user": "alice",
            "compressed_fact": "Pay invoice",
            "source": "manual",
            "p_magnitude": 70.0,
            "intent_tags": ["a", "b", "c"],
            "drawer": "billing",
            "idempotency_key": "pay-123",
            "is_deleted": False,
        }
        mock_get.return_value = mock_resp

        rec = _driver().find_by_idempotency_key("pay-123", user="alice")
        assert rec is not None
        assert rec.id == mid
        assert rec.idempotency_key == "pay-123"
        assert mock_get.call_args.args[0].endswith(
            "/signatures/by-idempotency-key"
        )
        assert mock_get.call_args.kwargs["params"]["key"] == "pay-123"

    @patch("httpx.get")
    def test_find_by_idempotency_key_404(self, mock_get):
        from pdm_memory.storage.errors import CloudNotFoundError

        d = _driver()
        with patch.object(
            d, "_get", side_effect=CloudNotFoundError("x", status_code=404)
        ):
            assert d.find_by_idempotency_key("missing") is None
