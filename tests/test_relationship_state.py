"""SDK relationship_state — point-in-time relationship pair client (spec §4.3)."""

from unittest.mock import MagicMock, patch

import pytest

from pdm_memory.auth.jwt_handler import JWTAuth
from pdm_memory.memory import Memory
from pdm_memory.models import RelationshipState
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


def _state_payload(**overrides):
    base = {
        "source": "subject:1",
        "target": "entity:abc",
        "domain": "*",
        "at_time": "2026-08-20T12:00:00Z",
        "relationships": [
            {
                "id": "rel-1",
                "source_entity_id": "subject:1",
                "target_entity_id": "entity:abc",
                "relationship_type": "colleague",
                "directionality": "directed",
                "state": "active",
                "valid_from": "2026-01-01T00:00:00Z",
                "valid_to": None,
                "domain_scope": [],
            }
        ],
        "channels": {
            "*": {
                "observer_key": "subject:1",
                "target_key": "entity:abc",
                "domain": "*",
                "valid_from": None,
                "valid_to": None,
                "frequency": 3,
                "recency_days": 1.2,
                "information_bandwidth": 0.5,
                "last_computed_at": "2026-08-20T12:00:00Z",
                "last_direct_measurement": None,
                "last_indirect_measurement": "2026-08-19T00:00:00Z",
            }
        },
        "current_resolution_by_domain": {
            "*": {
                "bfr": 1.1,
                "branches": [],
                "information_bandwidth": 0.5,
                "source_quality": 0.8,
                "change_velocity_estimate": 0.1,
                "confidence": 0.6,
                "is_currently_blackout": False,
                "recency_days": 1.2,
                "frequency": 3,
            }
        },
        "current_resolution_reason": None,
        "last_direct_measurement": None,
    }
    base.update(overrides)
    return base


class TestRelationshipStateModel:
    def test_from_payload_round_trip(self):
        payload = _state_payload()
        state = RelationshipState.from_payload(payload)

        assert state.source == "subject:1"
        assert state.target == "entity:abc"
        assert len(state.relationships) == 1
        assert state.relationships[0]["relationship_type"] == "colleague"
        assert state.channels["*"]["frequency"] == 3
        assert state.current_resolution_by_domain["*"]["bfr"] == 1.1
        assert state.current_resolution_reason is None

    def test_past_moment_reason_survives_a_null_resolution(self):
        payload = _state_payload(
            current_resolution_by_domain=None,
            current_resolution_reason="past_moment",
        )
        state = RelationshipState.from_payload(payload)

        assert state.current_resolution_by_domain is None
        assert state.current_resolution_reason == "past_moment"

    def test_as_dict_round_trips_a_null_resolution(self):
        payload = _state_payload(
            current_resolution_by_domain=None,
            current_resolution_reason="past_moment",
        )
        state = RelationshipState.from_payload(payload)

        assert state.as_dict()["current_resolution_by_domain"] is None
        assert state.as_dict()["current_resolution_reason"] == "past_moment"


class TestCloudDriverRelationshipState:
    def test_relationship_state_parses_the_body(self):
        driver = CloudDriver(auth=_auth(), base_url="http://localhost:8000")
        mock_resp = MagicMock()
        mock_resp.json.return_value = _state_payload(domain="trading")
        with patch.object(driver, "_get", return_value=mock_resp) as mock_get:
            result = driver.relationship_state(
                "subject:1", "entity:abc", domain="trading"
            )

        mock_get.assert_called_once_with(
            "/api/v1/pdm/relationships/state/",
            params={
                "source": "subject:1",
                "target": "entity:abc",
                "domain": "trading",
            },
        )
        assert isinstance(result, RelationshipState)
        assert result.domain == "trading"

    def test_at_time_is_stamped_when_given(self):
        from datetime import datetime, timezone

        driver = CloudDriver(auth=_auth(), base_url="http://localhost:8000")
        mock_resp = MagicMock()
        mock_resp.json.return_value = _state_payload()
        at = datetime(2026, 3, 1, 12, tzinfo=timezone.utc)
        with patch.object(driver, "_get", return_value=mock_resp) as mock_get:
            driver.relationship_state("subject:1", "entity:abc", at_time=at)

        _, kwargs = mock_get.call_args
        assert kwargs["params"]["at"] == "2026-03-01T12:00:00+00:00"

    def test_omitting_at_time_and_domain_sends_neither(self):
        driver = CloudDriver(auth=_auth(), base_url="http://localhost:8000")
        mock_resp = MagicMock()
        mock_resp.json.return_value = _state_payload()
        with patch.object(driver, "_get", return_value=mock_resp) as mock_get:
            driver.relationship_state("subject:1", "entity:abc")

        _, kwargs = mock_get.call_args
        assert "at" not in kwargs["params"]
        assert "domain" not in kwargs["params"]

    def test_a_non_dict_body_raises(self):
        driver = CloudDriver(auth=_auth(), base_url="http://localhost:8000")
        mock_resp = MagicMock()
        mock_resp.json.return_value = ["not", "a", "dict"]
        with patch.object(driver, "_get", return_value=mock_resp):
            with pytest.raises(CloudStorageError, match="relationship state"):
                driver.relationship_state("subject:1", "entity:abc")


class TestMemoryRelationshipState:
    def test_local_only_raises(self, tmp_path):
        mem = Memory(store=str(tmp_path / "local.db"), user="default")
        with pytest.raises(RuntimeError, match="ecosystem/cloud"):
            mem.relationship_state("subject:1", "entity:abc")

    def test_cloud_memory_delegates_to_driver(self):
        auth = _auth()
        driver = CloudDriver(auth=auth, base_url="http://localhost:8000")
        expected = RelationshipState.from_payload(_state_payload())
        with patch.object(
            driver, "relationship_state", return_value=expected
        ) as mock_fn:
            mem = Memory(
                store="cloud", token=auth.token, cloud_url="http://localhost:8000"
, user="default")
            mem._storage = driver
            mem._cloud_driver = driver
            result = mem.relationship_state(
                "subject:1", "entity:abc", domain="trading"
            )

        mock_fn.assert_called_once_with(
            "subject:1", "entity:abc", at_time=None, domain="trading"
        )
        assert result.source == "subject:1"
