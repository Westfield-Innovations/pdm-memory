"""GeoPass SDK client — mocked cloud (spec §5.3)."""

from datetime import datetime, timezone
from unittest.mock import MagicMock, patch

import pytest

from pdm_memory.auth.jwt_handler import JWTAuth
from pdm_memory.geopass import GeoPass, GeoPassGrant, GeoPassUnavailable
from pdm_memory.storage.cloud_driver import CloudDriver
from pdm_memory.storage.errors import CloudStorageError


def _auth() -> JWTAuth:
    import base64
    import json
    import time

    payload = (
        base64.urlsafe_b64encode(json.dumps({"exp": time.time() + 3600}).encode())
        .decode()
        .rstrip("=")
    )
    token = f"x.{payload}.y"
    return JWTAuth(token=token)


class _FakeCloudMemory:
    """Just enough of Memory's shape for GeoPass(memory) to accept it."""

    def __init__(self, driver: CloudDriver) -> None:
        self._storage = driver


def _geopass() -> GeoPass:
    driver = CloudDriver(auth=_auth(), base_url="http://localhost:8000")
    return GeoPass(_FakeCloudMemory(driver))


class TestGeoPassRequiresCloud:
    def test_non_cloud_memory_raises(self):
        with pytest.raises(GeoPassUnavailable):
            GeoPass(_FakeCloudMemory.__new__(_FakeCloudMemory))  # no _storage set

    def test_local_storage_backend_raises(self):
        class _LocalMemory:
            _storage = MagicMock()  # not a CloudDriver instance

        with pytest.raises(GeoPassUnavailable):
            GeoPass(_LocalMemory())


class TestBelongsCanView:
    @patch("httpx.get")
    def test_belongs_true(self, mock_get):
        resp = MagicMock()
        resp.status_code = 200
        resp.json.return_value = {"belongs": True}
        mock_get.return_value = resp

        assert _geopass().belongs("engineering") is True
        params = mock_get.call_args.kwargs["params"]
        assert params == {"field_id": "engineering"}  # at=None dropped

    @patch("httpx.get")
    def test_belongs_sends_known_at_beside_at(self, mock_get):
        resp = MagicMock()
        resp.status_code = 200
        resp.json.return_value = {"belongs": True}
        mock_get.return_value = resp
        at = datetime(2026, 9, 1, tzinfo=timezone.utc)
        known = datetime(2026, 9, 10, tzinfo=timezone.utc)

        _geopass().belongs("engineering", at=at, known_at=known)

        params = mock_get.call_args.kwargs["params"]
        assert params["at"] == at.isoformat()
        assert params["known_at"] == known.isoformat()

    def test_belongs_refuses_known_at_without_at(self):
        with pytest.raises(ValueError):
            _geopass().belongs("engineering", known_at="2026-09-10T00:00:00+00:00")

    @patch("httpx.get")
    def test_can_false(self, mock_get):
        resp = MagicMock()
        resp.status_code = 200
        resp.json.return_value = {"can": False}
        mock_get.return_value = resp

        assert _geopass().can("reveal", "sig-1") is False

    @patch("httpx.get")
    def test_view_returns_permitted_view(self, mock_get):
        resp = MagicMock()
        resp.status_code = 200
        resp.json.return_value = {"permitted_view": "conclusion"}
        mock_get.return_value = resp

        result = _geopass().view("sig-1", "full")
        assert result == "conclusion"


class TestGrantRevoke:
    @patch("httpx.post")
    def test_grant_returns_dataclass(self, mock_post):
        resp = MagicMock()
        resp.status_code = 201
        resp.json.return_value = {"id": "grant-1", "state": "active"}
        mock_post.return_value = resp

        grant = _geopass().grant("agent:auditor", "reveal", "sig-1")
        assert grant == GeoPassGrant(id="grant-1", state="active")

        body = mock_post.call_args.kwargs["json"]
        assert body == {
            "observer": "agent:auditor",
            "operation": "reveal",
            "target": "sig-1",
        }

    @patch("httpx.post")
    def test_grant_with_scope_and_interval(self, mock_post):
        resp = MagicMock()
        resp.status_code = 201
        resp.json.return_value = {"id": "grant-2", "state": "active"}
        mock_post.return_value = resp

        _geopass().grant(
            "agent:auditor",
            "reveal",
            "sig-1",
            scope={"domain_scope": "Finance"},
            interval=("2026-01-01T00:00:00Z", None),
            reason="quarterly review",
        )

        body = mock_post.call_args.kwargs["json"]
        assert body["scope"] == {"domain_scope": "Finance"}
        assert body["interval"] == {
            "valid_from": "2026-01-01T00:00:00Z",
            "valid_to": None,
        }
        assert body["reason"] == "quarterly review"

    @patch("httpx.patch")
    def test_revoke(self, mock_patch):
        resp = MagicMock()
        resp.status_code = 200
        resp.json.return_value = {"id": "grant-1", "state": "revoked"}
        mock_patch.return_value = resp

        revoked = _geopass().revoke("grant-1", reason="no longer needed")
        assert revoked == GeoPassGrant(id="grant-1", state="revoked")
        assert "/grants/grant-1/revoke" in mock_patch.call_args[0][0]


class TestAudit:
    @patch("httpx.get")
    def test_audit_forbidden_raises_cloud_storage_error(self, mock_get):
        resp = MagicMock()
        resp.status_code = 404
        resp.text = "missing"
        mock_get.return_value = resp

        with pytest.raises(CloudStorageError):
            _geopass().audit("11111111-1111-1111-1111-111111111111")

    @patch("httpx.get")
    def test_audit_returns_body(self, mock_get):
        resp = MagicMock()
        resp.status_code = 200
        resp.json.return_value = {
            "decision_id": "d-1",
            "resolved": True,
            "exposures": [],
        }
        mock_get.return_value = resp

        data = _geopass().audit("d-1")
        assert data["resolved"] is True


class TestTransition:
    @patch("httpx.post")
    def test_explicit_consent(self, mock_post):
        resp = MagicMock()
        resp.status_code = 201
        resp.json.return_value = {
            "id": "event-1",
            "event_type": "explicit_consent",
            "duplicate": False,
            "transitions": [{"kind": "grant"}],
        }
        mock_post.return_value = resp

        result = _geopass().transition(
            "explicit_consent",
            "subject:1",
            payload={
                "observer_id": "agent:auditor",
                "target_id": "sig-1",
                "operation": "reveal",
            },
        )
        assert result["id"] == "event-1"

    @patch("httpx.post")
    def test_court_order_surfaces_the_server_403(self, mock_post):
        """
        The client does not pre-filter event_type — the server's own
        AUTHORITY_TYPE_NOT_ALLOWED is what refuses this, surfaced as a
        CloudStorageError since companion_api's 403 body isn't the 409
        shape CloudDriver parses specially.
        """
        resp = MagicMock()
        resp.status_code = 403
        resp.text = '{"error_code": "AUTHORITY_TYPE_NOT_ALLOWED"}'
        mock_post.return_value = resp

        with pytest.raises(CloudStorageError) as exc:
            _geopass().transition("court_order", "subject:1")
        assert exc.value.status_code == 403

    @patch("httpx.post")
    def test_dry_run_is_forwarded(self, mock_post):
        resp = MagicMock()
        resp.status_code = 200
        resp.json.return_value = {
            "id": None,
            "event_type": "revocation",
            "duplicate": False,
            "dry_run": True,
            "transitions": [],
        }
        mock_post.return_value = resp

        _geopass().transition(
            "revocation", "subject:1", payload={"perspective_state_id": "x"}, dry_run=True
        )
        body = mock_post.call_args.kwargs["json"]
        assert body["dry_run"] is True
