"""SDK current_resolution — RelationshipChannel thin client (TKT-301)."""

from unittest.mock import MagicMock, patch

import pytest

from pdm_memory.auth.jwt_handler import JWTAuth
from pdm_memory.memory import Memory
from pdm_memory.models import RelationshipChannelResolution
from pdm_memory.storage.cloud_driver import CloudDriver
from pdm_memory.storage.errors import CloudStorageError


def _auth() -> JWTAuth:
    import base64
    import json
    import time

    payload = base64.urlsafe_b64encode(
        json.dumps({"exp": time.time() + 3600}).encode()
    ).decode().rstrip("=")
    return JWTAuth(token=f"x.{payload}.y")


def _channel_payload(**overrides):
    base = {
        "observer_key": "principal",
        "target_key": "operator",
        "domain": "*",
        "recency_days": 1.5,
        "frequency": 12,
        "breadth": 0.4,
        "directionality_inbound": 0.2,
        "directionality_outbound": 0.3,
        "directionality_bilateral": 0.5,
        "information_bandwidth": 0.66,
        "computation_window_days": 90,
        "last_computed_at": "2026-08-20T12:00:00Z",
        "updated_at": "2026-08-20T12:00:01Z",
    }
    base.update(overrides)
    return base


class TestRelationshipChannelResolutionModel:
    def test_from_payload_round_trip(self):
        payload = _channel_payload()
        snap = RelationshipChannelResolution.from_payload(payload)
        assert snap.frequency == 12
        assert snap.information_bandwidth == 0.66
        assert "channel_score" not in snap.as_dict()

    def test_from_payload_rejects_collapsed_score(self):
        with pytest.raises(ValueError, match="channel_score"):
            RelationshipChannelResolution.from_payload(
                _channel_payload(channel_score=0.9)
            )


class TestCloudDriverCurrentResolution:
    def test_current_resolution_parses_profile_block(self):
        driver = CloudDriver(auth=_auth(), base_url="http://localhost:8000")
        mock_resp = MagicMock()
        mock_resp.json.return_value = {
            "relationship_channel": _channel_payload(domain="trading", frequency=3)
        }
        with patch.object(driver, "_get", return_value=mock_resp) as mock_get:
            result = driver.current_resolution(
                observer="principal",
                target="operator",
                domain="trading",
            )
        mock_get.assert_called_once_with(
            "/api/v1/integrity/profile/",
            params={
                "observer": "principal",
                "target": "operator",
                "domain": "trading",
            },
        )
        assert isinstance(result, RelationshipChannelResolution)
        assert result.domain == "trading"
        assert result.frequency == 3

    def test_current_resolution_missing_block_raises(self):
        driver = CloudDriver(auth=_auth(), base_url="http://localhost:8000")
        mock_resp = MagicMock()
        mock_resp.json.return_value = {"id": 1}
        with patch.object(driver, "_get", return_value=mock_resp):
            with pytest.raises(CloudStorageError, match="relationship_channel"):
                driver.current_resolution()


class TestMemoryCurrentResolution:
    def test_local_only_raises(self, tmp_path):
        mem = Memory(store=str(tmp_path / "local.db"))
        with pytest.raises(RuntimeError, match="ecosystem/cloud"):
            mem.current_resolution()

    def test_cloud_memory_delegates_to_driver(self):
        auth = _auth()
        driver = CloudDriver(auth=auth, base_url="http://localhost:8000")
        expected = RelationshipChannelResolution.from_payload(_channel_payload())
        with patch.object(driver, "current_resolution", return_value=expected) as mock_fn:
            mem = Memory(store="cloud", token=auth.token, cloud_url="http://localhost:8000")
            # Replace storage with our patched driver
            mem._storage = driver
            mem._cloud_driver = driver
            result = mem.current_resolution(
                observer="principal",
                target="operator",
                domain="*",
            )
        mock_fn.assert_called_once_with(
            observer="principal",
            target="operator",
            domain="*",
        )
        assert result.frequency == 12
        assert result.information_bandwidth == 0.66
