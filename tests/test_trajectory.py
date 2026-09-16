"""
``EventLog.trajectory`` / ``Memory.trajectory`` — spec §7, §3, local half.

Local and mocked-cloud dispatch produce the same shape (``pdm_memory.models.
Trajectory``/``TrajectoryStep``); the SQL merge/cursor logic itself is tested
against the local driver, since that is where it lives — ``CloudDriver.
trajectory`` is a thin GET, already covered by the pattern in
``test_cloud_fields.py``.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from unittest.mock import MagicMock, patch

import pytest

from pdm_memory import Memory
from pdm_memory.event_log import EventLog
from pdm_memory.models import Trajectory, TrajectoryStep
from pdm_memory.storage.eventful_sqlite import EventfulSQLiteDriver
from pdm_memory.storage.field_store import TrajectoryCursorError

NOW = datetime(2026, 9, 7, 10, 0, tzinfo=timezone.utc)
FIELD = "westfield"


@pytest.fixture()
def log(tmp_path):
    mem = Memory(
        storage=EventfulSQLiteDriver(db_path=str(tmp_path / "t.db")), user="traj-user"
    )
    yield EventLog(mem)
    mem.close()


def _window(days=30):
    return {"start": NOW - timedelta(days=days), "end": NOW}


class TestFieldSubjectOrdering:
    def test_opened_and_closed_come_back_in_time_order(self, log):
        log.add_field_membership(
            "subject:1",
            FIELD,
            valid_from=NOW - timedelta(days=20),
            valid_to=NOW - timedelta(days=10),
            role="engineer",
        )
        log.add_field_membership(
            "subject:1", FIELD, valid_from=NOW - timedelta(days=10), role="lead"
        )

        result = log.trajectory(FIELD, **_window())

        kinds = [(s.kind, s.detail["role"]) for s in result.steps]
        assert kinds == [
            ("membership_opened", "engineer"),
            ("membership_closed", "engineer"),
            ("membership_opened", "lead"),
        ]

    def test_a_row_outside_the_window_is_absent(self, log):
        log.add_field_membership(
            "subject:1",
            FIELD,
            valid_from=NOW - timedelta(days=100),
            valid_to=NOW - timedelta(days=90),
        )

        result = log.trajectory(FIELD, **_window())
        assert result.steps == []


class TestCursorPaging:
    def test_paging_through_ties_at_one_instant_returns_each_row_once(self, log):
        shared = NOW - timedelta(days=5)
        for i in range(5):
            log.add_field_membership(f"subject:{i}", FIELD, valid_from=shared)

        first = log.trajectory(FIELD, **_window(), limit=2)
        assert len(first.steps) == 2
        assert first.next_cursor is not None
        assert first.truncated is True

        second = log.trajectory(FIELD, **_window(), limit=2, cursor=first.next_cursor)
        assert len(second.steps) == 2

        third = log.trajectory(FIELD, **_window(), limit=2, cursor=second.next_cursor)
        assert len(third.steps) == 1
        assert third.next_cursor is None
        assert third.truncated is False

        seen = {s.ref_id for s in first.steps + second.steps + third.steps}
        assert len(seen) == 5

    def test_a_malformed_cursor_is_refused(self, log):
        with pytest.raises(TrajectoryCursorError):
            log.trajectory(FIELD, **_window(), cursor="not-a-real-cursor")


class TestRangeValidation:
    def test_start_after_end_is_refused(self, log):
        with pytest.raises(ValueError):
            log.trajectory(FIELD, start=NOW, end=NOW - timedelta(days=1))


class TestEntitySubject:
    def test_links_appear_for_an_entity_subject(self, log):
        log.link(
            "subject:1", "subject:2", "colleague", valid_from=NOW - timedelta(days=3)
        )

        result = log.trajectory("subject:1", **_window())

        kinds = {s.kind for s in result.steps}
        assert "link_opened" in kinds

    def test_a_field_subject_does_not_return_links(self, log):
        log.link(
            "subject:1", "subject:2", "colleague", valid_from=NOW - timedelta(days=3)
        )
        log.add_field_membership("subject:1", FIELD, valid_from=NOW - timedelta(days=3))

        result = log.trajectory(FIELD, **_window())

        kinds = {s.kind for s in result.steps}
        assert kinds == {"membership_opened"}


class TestFactFiled:
    def test_a_filed_fact_appears_for_the_field(self, log):
        memory_id = log._memory.save("a fact", tags=["a", "b", "c"])
        membership_id = log.file_fact(
            memory_id, FIELD, valid_from=NOW - timedelta(days=2)
        )

        result = log.trajectory(FIELD, **_window())

        step = next(s for s in result.steps if s.kind == "fact_filed")
        assert step.ref_id == membership_id
        assert step.detail["signature_id"] == memory_id


class TestShape:
    def test_every_step_carries_measured(self, log):
        log.add_field_membership("subject:1", FIELD, valid_from=NOW - timedelta(days=2))

        result = log.trajectory(FIELD, **_window())

        assert result.steps
        for step in result.steps:
            assert isinstance(step, TrajectoryStep)
            assert step.state_type == "measured"

    def test_as_dict_round_trips_through_from_payload(self, log):
        log.add_field_membership("subject:1", FIELD, valid_from=NOW - timedelta(days=2))

        result = log.trajectory(FIELD, **_window())
        rebuilt = Trajectory.from_payload(result.as_dict())

        assert rebuilt.subject_id == result.subject_id
        assert [s.kind for s in rebuilt.steps] == [s.kind for s in result.steps]
        assert rebuilt.truncated == result.truncated


class TestRequiresFields:
    def test_a_fields_only_driver_still_works(self, log):
        # EventfulSQLiteDriver carries both events and fields; trajectory
        # only needs the latter.
        assert log._storage.supports_fields()
        log.trajectory(FIELD, **_window())  # does not raise


class TestMemoryDispatch:
    def test_memory_trajectory_delegates_to_the_storage_method(self, tmp_path):
        mem = Memory(
            storage=EventfulSQLiteDriver(db_path=str(tmp_path / "m.db")), user="u"
        )
        mem._storage.add_field_membership(
            "subject:1", FIELD, valid_from=NOW - timedelta(days=1)
        )

        result = mem.trajectory(FIELD, **_window())

        assert isinstance(result, Trajectory)
        mem.close()

    def test_a_driver_without_trajectory_raises(self, tmp_path):
        from pdm_memory.storage.sqlite_driver import SQLiteDriver

        mem = Memory(storage=SQLiteDriver(db_path=str(tmp_path / "s.db")), user="u")

        with pytest.raises(RuntimeError, match="does not support trajectory"):
            mem.trajectory(FIELD, **_window())
        mem.close()


class TestCloudDispatch:
    """Mocked cloud call produces the same Trajectory/TrajectoryStep shape."""

    @staticmethod
    def _auth():
        import base64
        import json
        import time

        from pdm_memory.auth.jwt_handler import JWTAuth

        payload = (
            base64.urlsafe_b64encode(json.dumps({"exp": time.time() + 3600}).encode())
            .decode()
            .rstrip("=")
        )
        return JWTAuth(token=f"x.{payload}.y")

    def test_cloud_trajectory_matches_the_local_shape(self):
        from pdm_memory.storage.cloud_driver import CloudDriver

        driver = CloudDriver(auth=self._auth(), base_url="http://localhost:8000")
        body = {
            "subject_id": FIELD,
            "start": (NOW - timedelta(days=30)).isoformat(),
            "end": NOW.isoformat(),
            "steps": [
                {
                    "at": (NOW - timedelta(days=2)).isoformat(),
                    "kind": "membership_opened",
                    "ref_id": "m-1",
                    "field_id": FIELD,
                    "detail": {"role": "engineer"},
                    "state_type": "measured",
                }
            ],
            "next_cursor": None,
        }
        resp = MagicMock()
        resp.status_code = 200
        resp.json.return_value = body

        with patch("httpx.get", return_value=resp):
            result = driver.trajectory(FIELD, NOW - timedelta(days=30), NOW)

        assert isinstance(result, Trajectory)
        assert isinstance(result.steps[0], TrajectoryStep)
        assert result.steps[0].kind == "membership_opened"
        assert result.truncated is False
