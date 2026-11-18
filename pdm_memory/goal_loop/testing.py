"""A shared check for ``JobStore`` backends.

    from pdm_memory.goal_loop.testing import run_job_store_contract

    def test_my_store():
        run_job_store_contract(lambda: MyStore(fresh_database()))

``factory`` must return an empty store each time it is called. A failed check
raises ``AssertionError`` naming the check.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import replace
from datetime import datetime, timedelta, timezone

from pdm_memory.goal_loop.job import Job, StepTemplate
from pdm_memory.goal_loop.receipts import StepReceipt
from pdm_memory.goal_loop.states import JobState
from pdm_memory.goal_loop.store import (
    JobExistsError,
    JobNotFoundError,
    JobStore,
    StaleJobError,
)

__all__ = ["run_job_store_contract", "store_contract_checks"]

T0 = datetime(2026, 8, 17, 12, 0, tzinfo=timezone.utc)
HISTORY = {"schema": "pdm.goal_contract/1", "versions": []}


def _job(job_id: str = "job-1", **fields) -> Job:
    base = Job.new(
        job_id,
        contract_version=1,
        step_template=StepTemplate("sms.send", "+15550100", "Text the owner"),
        now=T0,
    )
    return replace(base, **fields) if fields else base


def _receipt(job: Job, to_state: JobState, prev: StepReceipt | None) -> StepReceipt:
    return StepReceipt.build(
        job_id=job.job_id,
        seq=1 if prev is None else prev.seq + 1,
        at=T0,
        kind="transition",
        from_state=None if prev is None else prev.to_state,
        to_state=to_state,
        contract_version=1,
        action_id=None,
        reason="test",
        prev_hash="" if prev is None else prev.receipt_hash,
    )


def _start(store: JobStore, job_id: str = "job-1", **fields) -> tuple[Job, StepReceipt]:
    job = _job(job_id, **fields)
    first = _receipt(job, JobState.DRAFTED, None)
    job = replace(job, receipt_seq=1, receipt_head=first.receipt_hash)
    store.create(job, HISTORY, [first])
    return job, first


def _next(job: Job, first: StepReceipt, state: JobState, **fields) -> tuple[Job, StepReceipt]:
    receipt = _receipt(job, state, first)
    moved = replace(
        job,
        state=state,
        revision=job.revision + 1,
        receipt_seq=receipt.seq,
        receipt_head=receipt.receipt_hash,
        **fields,
    )
    return moved, receipt


def check_create_and_load(factory: Callable[[], JobStore]) -> None:
    store = factory()
    job, first = _start(store)
    assert store.load("job-1") == job, "load must return exactly what was created"
    assert store.receipts("job-1") == [first], "create must store its receipts"
    assert store.contract_history("job-1") == HISTORY
    assert store.active() == ["job-1"]


def check_missing_job(factory: Callable[[], JobStore]) -> None:
    store = factory()
    for call in (
        lambda: store.load("nope"),
        lambda: store.receipts("nope"),
        lambda: store.contract_history("nope"),
    ):
        try:
            call()
        except JobNotFoundError:
            continue
        raise AssertionError("an unknown job id must raise JobNotFoundError")


def check_duplicate_create(factory: Callable[[], JobStore]) -> None:
    store = factory()
    _start(store)
    try:
        _start(store)
    except JobExistsError:
        return
    raise AssertionError("creating an existing job id must raise JobExistsError")


def check_commit_moves_job_and_appends_receipts(factory: Callable[[], JobStore]) -> None:
    store = factory()
    job, first = _start(store)
    moved, receipt = _next(job, first, JobState.APPROVED, updated_at=T0 + timedelta(seconds=5))
    store.commit(moved, [receipt], expected_revision=job.revision)
    assert store.load("job-1") == moved
    assert store.receipts("job-1") == [first, receipt]
    assert store.receipts("job-1", after_seq=1) == [receipt]
    assert store.receipts("job-1", after_seq=2) == []


def check_stale_commit_changes_nothing(factory: Callable[[], JobStore]) -> None:
    store = factory()
    job, first = _start(store)
    moved, receipt = _next(job, first, JobState.APPROVED)
    store.commit(moved, [receipt], expected_revision=job.revision)
    other, other_receipt = _next(job, first, JobState.CANCELLED)
    try:
        store.commit(other, [other_receipt], expected_revision=job.revision)
    except StaleJobError:
        pass
    else:
        raise AssertionError("a commit on an old revision must raise StaleJobError")
    assert store.load("job-1") == moved, "a rejected commit must not change the job"
    assert store.receipts("job-1") == [first, receipt], "a rejected commit must add no receipts"


def check_bad_receipts_change_nothing(factory: Callable[[], JobStore]) -> None:
    store = factory()
    job, first = _start(store)
    moved, receipt = _next(job, first, JobState.APPROVED)
    gap = replace(receipt, seq=5)
    try:
        store.commit(moved, [gap], expected_revision=job.revision)
    except ValueError:
        pass
    else:
        raise AssertionError("receipts with a sequence gap must be refused")
    assert store.load("job-1") == job, "a refused commit must not change the job"
    assert store.receipts("job-1") == [first]


def check_commit_can_replace_contract_history(factory: Callable[[], JobStore]) -> None:
    store = factory()
    job, first = _start(store)
    moved, receipt = _next(job, first, JobState.APPROVED)
    revised = {"schema": "pdm.goal_contract/1", "versions": [], "note": "v2"}
    store.commit(moved, [receipt], expected_revision=job.revision, contract_history=revised)
    assert store.contract_history("job-1") == revised
    again, again_receipt = _next(moved, receipt, JobState.CANCELLED)
    store.commit(again, [again_receipt], expected_revision=moved.revision)
    assert store.contract_history("job-1") == revised, "no history given must keep the old one"


def check_due_and_active(factory: Callable[[], JobStore]) -> None:
    store = factory()
    soon = T0 + timedelta(minutes=5)
    _start(store, "late", next_due_at=T0 + timedelta(hours=1), state=JobState.WAITING)
    _start(store, "soon", next_due_at=soon, state=JobState.WAITING)
    _start(store, "never", next_due_at=None, state=JobState.WAITING)
    done, first = _start(store, "done", next_due_at=soon, state=JobState.WAITING)
    ended, receipt = _next(done, first, JobState.CANCELLED, next_due_at=None)
    store.commit(ended, [receipt], expected_revision=done.revision)

    assert store.due(T0) == []
    assert store.due(soon) == ["soon"]
    assert store.due(T0 + timedelta(days=1)) == ["soon", "late"], "due is ordered by due time"
    assert store.due(soon.astimezone(timezone(timedelta(hours=-7)))) == ["soon"]
    assert store.active() == ["late", "never", "soon"], "a terminal job is not active"


def store_contract_checks() -> dict[str, Callable[[Callable[[], JobStore]], None]]:
    return {
        "create_and_load": check_create_and_load,
        "missing_job": check_missing_job,
        "duplicate_create": check_duplicate_create,
        "commit_moves_job_and_appends_receipts": check_commit_moves_job_and_appends_receipts,
        "stale_commit_changes_nothing": check_stale_commit_changes_nothing,
        "bad_receipts_change_nothing": check_bad_receipts_change_nothing,
        "commit_can_replace_contract_history": check_commit_can_replace_contract_history,
        "due_and_active": check_due_and_active,
    }


def run_job_store_contract(factory: Callable[[], JobStore]) -> None:
    """Run every check against fresh stores from ``factory``."""
    for name, check in store_contract_checks().items():
        try:
            check(factory)
        except AssertionError as exc:
            raise AssertionError(f"job store check {name!r} failed: {exc}") from exc
