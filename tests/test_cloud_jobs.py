"""CloudJobStore: retries stay exactly-once, and the controller runs on it."""

from __future__ import annotations

from pdm_memory.cloud_ledgers import CloudJobStore
from pdm_memory.cloud_ledgers.testing import in_process
from pdm_memory.goal_loop import JobState
from pdm_memory.goal_loop.testing import _next, _start


def test_a_commit_whose_response_was_lost_is_not_reported_stale(tmp_path):
    store = CloudJobStore(
        in_process(tmp_path / "jobs.db", drop_response_after={"commit"})
    )
    job, first = _start(store)
    moved, receipt = _next(job, first, JobState.ARMED)
    store.commit(moved, [receipt], expected_revision=job.revision)
    assert store.load("job-1").revision == job.revision + 1


def test_the_controller_runs_a_job_on_the_cloud_store(tmp_path, monkeypatch):
    from pdm_memory.examples import goal_loop_watch_price as example

    monkeypatch.setattr(
        example, "SqliteJobStore", lambda path: CloudJobStore(in_process(path))
    )
    result = example.run_example(tmp_path)
    assert result["texts_sent"] == 1 and result["state"] == "waiting"
    assert result["chain_intact"] and result["promise_change_refused"]
