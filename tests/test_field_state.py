"""SDK state_at / current_state — field-state thin client."""

from datetime import datetime, timezone
from unittest.mock import MagicMock, patch

import pytest

from pdm_memory.auth.jwt_handler import JWTAuth
from pdm_memory.memory import Memory
from pdm_memory.models import FieldStateSnapshot
from pdm_memory.storage.cloud_driver import CloudDriver
from pdm_memory.storage.errors import CloudStorageError

PATH = "/api/v1/pdm/field-state/"


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


def _state_payload(**overrides):
    base = {
        "field_id": "westfield",
        "at_time": "2026-03-01T12:00:00+00:00",
        "state_type": "measured",
        "entities": [
            {
                "signature_id": "sig-1",
                "domain": "engineering",
                "state_type": "measured",
            }
        ],
        "entities_next_cursor": None,
        "envelope_included": True,
        "relationships": [
            {"target_id": "subject:31", "band": "structural", "state_type": "measured"}
        ],
        "field_memberships": [{"role": "engineer", "state_type": "measured"}],
        "relationship_bandwidth": [
            {"domain": "engineering", "state_type": "measured"}
        ],
        "projection_branches": [{"kind": "holds", "state_type": "projected"}],
        "provenance": [{"field_id": "westfield", "valid_to": None}],
        "permission_view": {"observer_id": "subject:10"},
        "truncated": [],
    }
    base.update(overrides)
    return base


class TestFieldStateSnapshotModel:
    def test_from_payload_round_trip(self):
        payload = _state_payload()
        snap = FieldStateSnapshot.from_payload(payload)
        assert snap.as_dict() == payload

    def test_measured_and_projected_stay_separable(self):
        snap = FieldStateSnapshot.from_payload(_state_payload())
        assert snap.entities[0]["state_type"] == "measured"
        assert snap.projection_branches[0]["state_type"] == "projected"

    @pytest.mark.parametrize(
        "section",
        [
            "entities",
            "relationships",
            "field_memberships",
            "relationship_bandwidth",
            "projection_branches",
        ],
    )
    def test_untagged_item_rejected(self, section):
        with pytest.raises(ValueError, match=section):
            FieldStateSnapshot.from_payload(
                _state_payload(**{section: [{"id": "x"}]})
            )

    def test_provenance_and_permission_view_need_no_tag(self):
        # Neither is an item that could be measured or projected, so neither
        # goes through the tag check — the server does not tag them either.
        snap = FieldStateSnapshot.from_payload(_state_payload())
        assert snap.provenance == [{"field_id": "westfield", "valid_to": None}]
        assert snap.permission_view == {"observer_id": "subject:10"}

    def test_absent_envelope_is_omitted_not_emptied(self):
        payload = {
            "field_id": "westfield",
            "at_time": "2026-03-01T12:00:00+00:00",
            "state_type": "measured",
            "entities": [{"signature_id": "sig-2", "state_type": "measured"}],
            "entities_next_cursor": "cursor-2",
            "envelope_included": False,
        }
        rendered = FieldStateSnapshot.from_payload(payload).as_dict()
        # [] read as "no relationships" would take a page-2 response for a
        # statement about the field.
        assert "relationships" not in rendered
        assert rendered == payload

    def test_envelope_defaults_true_when_the_key_is_absent(self):
        payload = _state_payload()
        del payload["envelope_included"]
        assert FieldStateSnapshot.from_payload(payload).envelope_included is True


class TestCloudDriverFieldState:
    def test_state_at_sends_the_moment(self):
        driver = _driver()
        resp = MagicMock()
        resp.json.return_value = _state_payload()
        with patch.object(driver, "_get", return_value=resp) as mock_get:
            result = driver.state_at(
                "westfield",
                datetime(2026, 3, 1, 12, tzinfo=timezone.utc),
                limit=50,
            )
        mock_get.assert_called_once_with(
            PATH,
            params={
                "field_id": "westfield",
                "envelope": "true",
                "at_time": "2026-03-01T12:00:00+00:00",
                "page_size": 50,
            },
        )
        assert isinstance(result, FieldStateSnapshot)
        assert result.field_id == "westfield"

    def test_current_state_omits_the_moment(self):
        driver = _driver()
        resp = MagicMock()
        resp.json.return_value = _state_payload()
        with patch.object(driver, "_get", return_value=resp) as mock_get:
            driver.current_state("westfield")
        # Not our clock: a machine running ahead would name a moment the
        # server reads as future and be refused for the skew.
        assert "at_time" not in mock_get.call_args.kwargs["params"]

    def test_envelope_is_sent_as_the_spelling_the_server_accepts(self):
        driver = _driver()
        resp = MagicMock()
        resp.json.return_value = _state_payload(envelope_included=False)
        with patch.object(driver, "_get", return_value=resp) as mock_get:
            driver.current_state("westfield", envelope=False)
        assert mock_get.call_args.kwargs["params"]["envelope"] == "false"

    def test_cursor_is_sent_when_given(self):
        driver = _driver()
        resp = MagicMock()
        resp.json.return_value = _state_payload()
        with patch.object(driver, "_get", return_value=resp) as mock_get:
            driver.current_state("westfield", cursor="opaque-1")
        assert mock_get.call_args.kwargs["params"]["cursor"] == "opaque-1"

    def test_non_object_body_raises(self):
        driver = _driver()
        resp = MagicMock()
        resp.json.return_value = ["not", "a", "state"]
        with patch.object(driver, "_get", return_value=resp):
            with pytest.raises(CloudStorageError, match="field-state body"):
                driver.current_state("westfield")


class TestMemoryFieldState:
    def test_state_at_local_only_raises(self, tmp_path):
        mem = Memory(store=str(tmp_path / "local.db"))
        with pytest.raises(RuntimeError, match="state_at requires ecosystem/cloud"):
            mem.state_at("westfield", datetime(2026, 3, 1, tzinfo=timezone.utc))

    def test_current_state_local_only_raises(self, tmp_path):
        mem = Memory(store=str(tmp_path / "local.db"))
        with pytest.raises(
            RuntimeError, match="current_state requires ecosystem/cloud"
        ):
            mem.current_state("westfield")

    def test_cloud_memory_delegates_state_at(self):
        auth = _auth()
        driver = CloudDriver(auth=auth, base_url="http://localhost:8000")
        expected = FieldStateSnapshot.from_payload(_state_payload())
        moment = datetime(2026, 3, 1, 12, tzinfo=timezone.utc)
        with patch.object(driver, "state_at", return_value=expected) as mock_fn:
            mem = Memory(
                store="cloud", token=auth.token, cloud_url="http://localhost:8000"
            )
            mem._storage = driver
            mem._cloud_driver = driver
            result = mem.state_at("westfield", moment, limit=10)
        mock_fn.assert_called_once_with(
            "westfield", moment, cursor=None, limit=10, envelope=True
        )
        assert result.field_id == "westfield"

    def test_cloud_memory_delegates_current_state(self):
        auth = _auth()
        driver = CloudDriver(auth=auth, base_url="http://localhost:8000")
        expected = FieldStateSnapshot.from_payload(_state_payload())
        with patch.object(driver, "current_state", return_value=expected) as mock_fn:
            mem = Memory(
                store="cloud", token=auth.token, cloud_url="http://localhost:8000"
            )
            mem._storage = driver
            mem._cloud_driver = driver
            result = mem.current_state("westfield", envelope=False)
        mock_fn.assert_called_once_with(
            "westfield", cursor=None, limit=None, envelope=False
        )
        assert result.field_id == "westfield"
