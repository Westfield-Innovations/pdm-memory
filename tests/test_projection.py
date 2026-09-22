"""SDK project / projection / record_outcome — projection thin client (spec §4.6, §13)."""

from datetime import datetime, timezone
from unittest.mock import MagicMock, patch

import pytest

from pdm_memory.auth.jwt_handler import JWTAuth
from pdm_memory.memory import Memory
from pdm_memory.models import (
    FieldStateSnapshot,
    ProjectionBranch,
    ProjectionFan,
    RecordedProjection,
)
from pdm_memory.storage.cloud_driver import CloudDriver
from pdm_memory.storage.errors import (
    CloudConflictError,
    CloudNotFoundError,
    CloudStorageError,
)

FAN_PATH = "/api/v1/pdm/field-state/projection/"
PID = "7f1c2a9e-0000-4000-8000-000000000001"


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


def _branch(**overrides):
    base = {
        "branch_id": "engineering:holds",
        "domain": "engineering",
        "kind": "holds",
        "horizon_days": 90,
        "base_state_version": "abc123",
        "ghost_nodes": [],
        "ghost_relationships": [
            {"domain": "engineering", "kind": "holds", "weight": 0.7}
        ],
        "confidence_band": {
            "weight": 0.7,
            "confidence": 0.55,
            "bfr": 1.2,
            "is_currently_blackout": False,
        },
        "timing_range": {
            "start": "2026-09-22T00:00:00+00:00",
            "end": "2026-12-21T00:00:00+00:00",
        },
        "invalidation_conditions": ["channel goes quiet"],
        "state_type": "projected",
    }
    base.update(overrides)
    return base


def _fan(**overrides):
    base = {"branches": [_branch()], "record": False, "projection_ids": []}
    base.update(overrides)
    return base


def _recorded(outcome=None, **overrides):
    base = {
        "id": PID,
        "subject_ref": "subject:10",
        "domain": "engineering",
        "branch_kind": "holds",
        "weight": 0.7,
        "confidence": 0.55,
        "bfr": None,
        "projected_at": "2026-09-22T00:00:00+00:00",
        "horizon_start": "2026-09-22T00:00:00+00:00",
        "horizon_end": "2026-12-21T00:00:00+00:00",
        "invalidation_conditions": ["channel goes quiet"],
        "base_state_version": "abc123",
        "state_type": "projected",
        "outcome": outcome,
    }
    base.update(overrides)
    return base


OUTCOME = {
    "id": "out-1",
    "observed_at": "2026-11-01T00:00:00+00:00",
    "recorded_at": "2026-11-02T00:00:00+00:00",
    "timing": "within",
    "connection_geometry": "correct",
    "meaning_propagation": "partial",
    "model_update": "",
}


def _http(status: int, body):
    resp = MagicMock()
    resp.status_code = status
    resp.json.return_value = body
    resp.text = str(body)
    return resp


class TestProjectionModels:
    def test_fan_round_trip(self):
        payload = _fan(record=True, projection_ids=[PID])
        assert ProjectionFan.from_payload(payload).as_dict() == payload

    def test_a_fan_is_not_a_field_state(self):
        # §13 "programmatically distinct": isinstance alone tells them apart.
        fan = ProjectionFan.from_payload(_fan())
        assert not isinstance(fan, FieldStateSnapshot)
        assert not issubclass(ProjectionFan, FieldStateSnapshot)

    @pytest.mark.parametrize("state_type", ["measured", "", None])
    def test_a_branch_that_is_not_projected_is_refused(self, state_type):
        with pytest.raises(ValueError, match="projected"):
            ProjectionBranch.from_payload(_branch(state_type=state_type))

    def test_one_bad_branch_refuses_the_whole_fan(self):
        with pytest.raises(ValueError):
            ProjectionFan.from_payload(
                _fan(branches=[_branch(), _branch(state_type="measured")])
            )

    def test_recorded_round_trip_unsettled(self):
        payload = _recorded()
        rec = RecordedProjection.from_payload(payload)
        assert rec.outcome is None
        assert rec.as_dict() == payload

    def test_recorded_round_trip_settled(self):
        payload = _recorded(outcome=OUTCOME)
        rec = RecordedProjection.from_payload(payload)
        assert rec.outcome.timing == "within"
        assert rec.as_dict() == payload

    def test_recorded_projection_that_is_not_projected_is_refused(self):
        with pytest.raises(ValueError):
            RecordedProjection.from_payload(_recorded(state_type="measured"))


class TestCloudDriverProjection:
    def test_project_defaults_to_not_recording_and_no_horizon(self):
        driver = _driver()
        with patch.object(driver, "_post", return_value=_http(200, _fan())) as post:
            fan = driver.project()
        post.assert_called_once_with(FAN_PATH, {"record": False})
        assert isinstance(fan, ProjectionFan)
        assert fan.branches[0].branch_id == "engineering:holds"

    def test_project_sends_horizon_and_record(self):
        driver = _driver()
        with patch.object(
            driver,
            "_post",
            return_value=_http(200, _fan(record=True, projection_ids=[PID])),
        ) as post:
            fan = driver.project(horizon_days=30, record=True)
        post.assert_called_once_with(FAN_PATH, {"record": True, "horizon_days": 30})
        assert fan.projection_ids == [PID]

    def test_project_non_object_body_raises(self):
        driver = _driver()
        with (
            patch.object(driver, "_post", return_value=_http(200, ["x"])),
            pytest.raises(CloudStorageError, match="projection body"),
        ):
            driver.project()

    def test_projection_reads_one_recorded_branch(self):
        driver = _driver()
        with patch.object(
            driver, "_get", return_value=_http(200, _recorded(outcome=OUTCOME))
        ) as get:
            rec = driver.projection(PID)
        get.assert_called_once_with(f"/api/v1/pdm/projections/{PID}")
        assert rec.outcome.connection_geometry == "correct"

    def test_record_outcome_sends_no_timing(self):
        driver = _driver()
        with patch.object(
            driver, "_post", return_value=_http(201, _recorded(outcome=OUTCOME))
        ) as post:
            rec = driver.record_outcome(
                PID,
                observed_at=datetime(2026, 11, 1, tzinfo=timezone.utc),
                connection_geometry="correct",
                meaning_propagation="partial",
            )
        path, body = post.call_args.args
        assert path == f"/api/v1/pdm/projections/{PID}/outcome"
        assert body == {
            "observed_at": "2026-11-01T00:00:00+00:00",
            "connection_geometry": "correct",
            "meaning_propagation": "partial",
            "model_update": "",
        }
        assert "timing" not in body
        assert rec.outcome is not None


class TestProjectionRefusalsOverHttp:
    """The server's refusals reach the caller as typed errors, not prose."""

    @patch("httpx.post")
    def test_second_outcome_is_a_conflict_with_its_code(self, mock_post):
        mock_post.return_value = _http(
            409, {"error_code": "ALREADY_SETTLED", "detail": "settled"}
        )
        with pytest.raises(CloudConflictError) as exc:
            _driver().record_outcome(
                PID,
                observed_at="2026-11-01T00:00:00+00:00",
                connection_geometry="correct",
                meaning_propagation="correct",
            )
        assert exc.value.error_code == "ALREADY_SETTLED"

    @patch("httpx.post")
    def test_outcome_before_projection_is_422(self, mock_post):
        mock_post.return_value = _http(422, {"error_code": "OUTCOME_BEFORE_PROJECTION"})
        with pytest.raises(CloudStorageError) as exc:
            _driver().record_outcome(
                PID,
                observed_at="2020-01-01T00:00:00+00:00",
                connection_geometry="correct",
                meaning_propagation="correct",
            )
        assert exc.value.status_code == 422

    @patch("httpx.get")
    def test_foreign_or_unknown_projection_is_not_found(self, mock_get):
        mock_get.return_value = _http(404, {"error_code": "PROJECTION_NOT_FOUND"})
        with pytest.raises(CloudNotFoundError):
            _driver().projection(PID)


class TestMemoryProjection:
    @pytest.mark.parametrize(
        ("call", "name"),
        [
            (lambda m: m.project(), "project"),
            (lambda m: m.projection(PID), "projection"),
            (
                lambda m: m.record_outcome(
                    PID, "2026-11-01T00:00:00+00:00", "correct", "correct"
                ),
                "record_outcome",
            ),
        ],
    )
    def test_local_only_raises(self, tmp_path, call, name):
        mem = Memory(store=str(tmp_path / "local.db"))
        with pytest.raises(RuntimeError, match=f"{name} requires ecosystem/cloud"):
            call(mem)

    def _cloud_memory(self, driver):
        mem = Memory(
            store="cloud", token=driver._auth.token, cloud_url="http://localhost:8000"
        )
        mem._storage = driver
        mem._cloud_driver = driver
        return mem

    def test_cloud_memory_delegates_project(self):
        driver = _driver()
        expected = ProjectionFan.from_payload(_fan())
        with patch.object(driver, "project", return_value=expected) as fn:
            result = self._cloud_memory(driver).project(horizon_days=14, record=True)
        fn.assert_called_once_with(horizon_days=14, record=True)
        assert result is expected

    def test_cloud_memory_delegates_record_outcome(self):
        driver = _driver()
        expected = RecordedProjection.from_payload(_recorded(outcome=OUTCOME))
        with patch.object(driver, "record_outcome", return_value=expected) as fn:
            self._cloud_memory(driver).record_outcome(
                PID, "2026-11-01T00:00:00+00:00", "correct", "partial", "noted"
            )
        fn.assert_called_once_with(
            PID,
            observed_at="2026-11-01T00:00:00+00:00",
            connection_geometry="correct",
            meaning_propagation="partial",
            model_update="noted",
        )
