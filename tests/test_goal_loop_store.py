"""Job stores: the shared contract, durability and the stdlib-only promise."""

from __future__ import annotations

import itertools
import sqlite3
import threading
from dataclasses import replace
from datetime import datetime, timedelta, timezone

import pytest

from pdm_memory.goal_loop import InMemoryJobStore, JobState, SqliteJobStore, StaleJobError
from pdm_memory.goal_loop.testing import (
    _next,
    _start,
    run_job_store_contract,
    store_contract_checks,
)

T0 = datetime(2026, 8, 17, 12, 0, tzinfo=timezone.utc)


@pytest.fixture(params=["memory", "sqlite"])
def factory(request, tmp_path):
    counter = itertools.count()
    if request.param == "memory":
        return InMemoryJobStore
    return lambda: SqliteJobStore(tmp_path / f"jobs{next(counter)}.db")


def test_store_passes_the_shared_contract(factory):
    run_job_store_contract(factory)


@pytest.mark.parametrize("name", sorted(store_contract_checks()))
def test_each_check_is_a_separate_failure_point(factory, name):
    store_contract_checks()[name](factory)


def test_a_broken_store_is_reported_by_check_name():
    class Forgetful(InMemoryJobStore):
        def receipts(self, job_id, *, after_seq=0):
            return []

    with pytest.raises(AssertionError, match="create_and_load"):
        run_job_store_contract(Forgetful)


def test_sqlite_store_keeps_everything_across_reopen(tmp_path):
    path = tmp_path / "jobs.db"
    store = SqliteJobStore(path)
    job, first = _start(store)
    moved, receipt = _next(job, first, JobState.APPROVED)
    store.commit(moved, [receipt], expected_revision=job.revision)
    store.close()

    reopened = SqliteJobStore(path)
    assert reopened.load("job-1") == moved
    assert reopened.receipts("job-1") == [first, receipt]
    assert reopened.active() == ["job-1"]


def test_sqlite_due_compares_moments_not_text(tmp_path):
    """Due times in different UTC offsets are compared as the same timeline."""
    store = SqliteJobStore(tmp_path / "jobs.db")
    east = timezone(timedelta(hours=14))
    west = timezone(timedelta(hours=-12))
    _start(
        store, "east", state=JobState.WAITING, next_due_at=datetime(2026, 8, 18, 9, 0, tzinfo=east)
    )
    _start(
        store, "west", state=JobState.WAITING, next_due_at=datetime(2026, 8, 17, 21, 0, tzinfo=west)
    )
    # 09:00+14:00 on the 18th is 19:00 UTC on the 17th; 21:00-12:00 is 09:00 UTC on the 18th.
    assert store.due(datetime(2026, 8, 17, 19, 0, tzinfo=timezone.utc)) == ["east"]
    assert store.due(datetime(2026, 8, 18, 9, 0, tzinfo=timezone.utc)) == ["east", "west"]


def test_due_refuses_a_naive_time(factory):
    with pytest.raises(ValueError, match="timezone-aware"):
        factory().due(datetime(2026, 8, 17))


def test_only_one_of_two_racing_commits_wins(factory):
    store = factory()
    job, first = _start(store)
    outcomes = []
    gate = threading.Barrier(2)

    def attempt(state):
        moved, receipt = _next(job, first, state)
        gate.wait()
        try:
            store.commit(moved, [receipt], expected_revision=job.revision)
            outcomes.append("won")
        except StaleJobError:
            outcomes.append("stale")

    threads = [
        threading.Thread(target=attempt, args=(s,)) for s in (JobState.APPROVED, JobState.CANCELLED)
    ]
    [t.start() for t in threads]
    [t.join() for t in threads]
    assert sorted(outcomes) == ["stale", "won"]
    assert len(store.receipts("job-1")) == 2


def test_a_failed_commit_leaves_the_database_untouched(tmp_path):
    path = tmp_path / "jobs.db"
    store = SqliteJobStore(path)
    job, first = _start(store)
    moved, receipt = _next(job, first, JobState.APPROVED)
    with pytest.raises(ValueError):
        store.commit(replace(moved, receipt_seq=9), [receipt], expected_revision=job.revision)
    with sqlite3.connect(path) as db:
        assert db.execute("SELECT COUNT(*) FROM gl_receipts").fetchone()[0] == 1
        assert db.execute("SELECT revision FROM gl_jobs").fetchone()[0] == job.revision


def test_stores_use_only_the_standard_library():
    import pdm_memory.goal_loop.store as module

    imported = {
        line.split()[1].split(".")[0]
        for line in open(module.__file__, encoding="utf-8")
        if line.startswith(("import ", "from "))
    }
    assert imported <= {
        "__future__",
        "sqlite3",
        "threading",
        "abc",
        "collections",
        "contextlib",
        "datetime",
        "pathlib",
        "typing",
        "pdm_memory",
    }
