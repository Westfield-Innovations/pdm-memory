"""Where jobs live between calls.

``JobStore`` is the whole contract a storage backend implements, so the AZUS
app and outside developers can each plug in their own. Two ship here:
``InMemoryJobStore`` (tests, and the default when no store is configured) and
``SqliteJobStore`` (stdlib only; a job survives the end of the conversation and
the end of the process).

``commit`` is the one write. It stores the new job state, its receipts and, when
given, a new contract history together or not at all, and only if the stored
revision is still the one the caller read (compare-and-swap).
``pdm_memory.goal_loop.testing.run_job_store_contract`` checks a backend
against this contract.
"""

from __future__ import annotations

import sqlite3
import threading
from abc import ABC, abstractmethod
from collections.abc import Iterator, Sequence
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from pdm_memory.goal_loop import _json
from pdm_memory.goal_loop.job import Job
from pdm_memory.goal_loop.receipts import StepReceipt
from pdm_memory.goal_loop.states import TERMINAL

__all__ = [
    "InMemoryJobStore",
    "JobExistsError",
    "JobNotFoundError",
    "JobStore",
    "SqliteJobStore",
    "StaleJobError",
]


class JobExistsError(ValueError):
    """A job with this id is already stored."""


class JobNotFoundError(KeyError):
    """No job with this id is stored."""


class StaleJobError(RuntimeError):
    """The job changed since it was read. Reload and try again."""


def validate_commit(
    stored: Job, job: Job, receipts: Sequence[StepReceipt], expected_revision: int
) -> None:
    """Checks every backend makes before writing; raises before anything is stored."""
    if stored.revision != expected_revision:
        raise StaleJobError(
            f"job {job.job_id} is at revision {stored.revision}, not {expected_revision}"
        )
    if job.job_id != stored.job_id:
        raise ValueError("a commit cannot change the job id")
    if job.revision != expected_revision + 1:
        raise ValueError("the committed job must carry revision expected_revision + 1")
    seq = stored.receipt_seq
    for receipt in receipts:
        seq += 1
        if receipt.job_id != job.job_id or receipt.seq != seq:
            raise ValueError("receipts must continue the job's receipt sequence without gaps")
    if job.receipt_seq != seq or (receipts and job.receipt_head != receipts[-1].receipt_hash):
        raise ValueError("the job must point at the end of its receipt chain")


def validate_create(job: Job, receipts: Sequence[StepReceipt]) -> None:
    for number, receipt in enumerate(receipts, start=1):
        if receipt.job_id != job.job_id or receipt.seq != number:
            raise ValueError("receipts must start at sequence 1 without gaps")
    if job.receipt_seq != len(receipts) or (
        receipts and job.receipt_head != receipts[-1].receipt_hash
    ):
        raise ValueError("the job must point at the end of its receipt chain")


class JobStore(ABC):
    """Storage for jobs, their receipts and their contract histories."""

    @abstractmethod
    def create(
        self, job: Job, contract_history: dict[str, Any], receipts: Sequence[StepReceipt]
    ) -> None:
        """Store a new job. Raises ``JobExistsError`` if the id is taken."""

    @abstractmethod
    def load(self, job_id: str) -> Job:
        """Raises ``JobNotFoundError``."""

    @abstractmethod
    def commit(
        self,
        job: Job,
        receipts: Sequence[StepReceipt],
        *,
        expected_revision: int,
        contract_history: dict[str, Any] | None = None,
    ) -> None:
        """Atomically store ``job``, ``receipts`` and ``contract_history`` (if given).

        Raises ``StaleJobError`` when the stored revision is not ``expected_revision``.
        Nothing is stored when it raises.
        """

    @abstractmethod
    def receipts(self, job_id: str, *, after_seq: int = 0) -> list[StepReceipt]:
        """Receipts with ``seq > after_seq``, oldest first."""

    @abstractmethod
    def contract_history(self, job_id: str) -> dict[str, Any]: ...

    @abstractmethod
    def due(self, now: datetime) -> list[str]:
        """Ids of active jobs whose ``next_due_at`` is at or before ``now``."""

    @abstractmethod
    def active(self) -> list[str]:
        """Ids of jobs that have not reached a terminal state."""

    def close(self) -> None:  # noqa: B027 - optional hook
        """Release resources."""


def _dump_job(job: Job) -> str:
    return _json.dumps(job.to_dict())


def _load_job(text: str) -> Job:
    return Job.from_dict(_json.loads(text))


class InMemoryJobStore(JobStore):
    """Process-local. Records go through the same JSON as a durable store would use."""

    def __init__(self) -> None:
        self._lock = threading.RLock()
        self._jobs: dict[str, str] = {}
        self._histories: dict[str, str] = {}
        self._receipts: dict[str, list[str]] = {}

    def create(
        self, job: Job, contract_history: dict[str, Any], receipts: Sequence[StepReceipt]
    ) -> None:
        validate_create(job, receipts)
        with self._lock:
            if job.job_id in self._jobs:
                raise JobExistsError(job.job_id)
            self._jobs[job.job_id] = _dump_job(job)
            self._histories[job.job_id] = _json.dumps(contract_history)
            self._receipts[job.job_id] = [_json.dumps(r.to_dict()) for r in receipts]

    def load(self, job_id: str) -> Job:
        with self._lock:
            try:
                return _load_job(self._jobs[job_id])
            except KeyError:
                raise JobNotFoundError(job_id) from None

    def commit(
        self,
        job: Job,
        receipts: Sequence[StepReceipt],
        *,
        expected_revision: int,
        contract_history: dict[str, Any] | None = None,
    ) -> None:
        with self._lock:
            stored = self.load(job.job_id)
            validate_commit(stored, job, receipts, expected_revision)
            encoded = [_json.dumps(r.to_dict()) for r in receipts]
            history = None if contract_history is None else _json.dumps(contract_history)
            self._jobs[job.job_id] = _dump_job(job)
            self._receipts[job.job_id].extend(encoded)
            if history is not None:
                self._histories[job.job_id] = history

    def receipts(self, job_id: str, *, after_seq: int = 0) -> list[StepReceipt]:
        with self._lock:
            if job_id not in self._jobs:
                raise JobNotFoundError(job_id)
            rows = self._receipts[job_id][max(after_seq, 0) :]
        return [StepReceipt.from_dict(_json.loads(row)) for row in rows]

    def contract_history(self, job_id: str) -> dict[str, Any]:
        with self._lock:
            try:
                return _json.loads(self._histories[job_id])
            except KeyError:
                raise JobNotFoundError(job_id) from None

    def due(self, now: datetime) -> list[str]:
        _json.require_aware(now, "now")
        with self._lock:
            jobs = [_load_job(text) for text in self._jobs.values()]
        found = [
            j
            for j in jobs
            if j.state not in TERMINAL and j.next_due_at is not None and j.next_due_at <= now
        ]
        return [j.job_id for j in sorted(found, key=lambda j: (j.next_due_at, j.job_id))]

    def active(self) -> list[str]:
        with self._lock:
            jobs = [_load_job(text) for text in self._jobs.values()]
        return sorted(j.job_id for j in jobs if j.state not in TERMINAL)


def _utc_key(value: datetime | None) -> str | None:
    """Sortable text for a moment: UTC, fixed width, so text order is time order."""
    if value is None:
        return None
    return value.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.%f")


class SqliteJobStore(JobStore):
    """Durable, stdlib only. WAL with full synchronization, like the Boundary Lock ledger."""

    def __init__(self, path: str | Path) -> None:
        if not str(path):
            raise ValueError("a database path is required")
        self._lock = threading.RLock()
        self._conn = sqlite3.connect(
            str(path), timeout=30, isolation_level=None, check_same_thread=False
        )
        self._conn.row_factory = sqlite3.Row
        self._conn.execute("PRAGMA journal_mode=WAL")
        self._conn.execute("PRAGMA synchronous=FULL")
        self._conn.executescript(
            """
            CREATE TABLE IF NOT EXISTS gl_jobs (
              job_id TEXT PRIMARY KEY, revision INTEGER NOT NULL, active INTEGER NOT NULL,
              next_due_at TEXT, job_json TEXT NOT NULL);
            CREATE INDEX IF NOT EXISTS gl_jobs_due ON gl_jobs(active, next_due_at);
            CREATE TABLE IF NOT EXISTS gl_histories (
              job_id TEXT PRIMARY KEY, history_json TEXT NOT NULL);
            CREATE TABLE IF NOT EXISTS gl_receipts (
              job_id TEXT NOT NULL, seq INTEGER NOT NULL, receipt_json TEXT NOT NULL,
              PRIMARY KEY(job_id, seq));
            """
        )

    @contextmanager
    def _transaction(self) -> Iterator[None]:
        with self._lock:
            self._conn.execute("BEGIN IMMEDIATE")
            try:
                yield
            except BaseException:
                self._conn.execute("ROLLBACK")
                raise
            else:
                self._conn.execute("COMMIT")

    def _write_job(self, job: Job, insert: bool) -> None:
        row = (
            job.job_id,
            job.revision,
            0 if job.state in TERMINAL else 1,
            _utc_key(job.next_due_at),
            _dump_job(job),
        )
        if insert:
            self._conn.execute("INSERT INTO gl_jobs VALUES (?, ?, ?, ?, ?)", row)
        else:
            self._conn.execute(
                "UPDATE gl_jobs SET revision=?, active=?, next_due_at=?, job_json=? WHERE job_id=?",
                row[1:] + (row[0],),
            )

    def _insert_receipts(self, receipts: Sequence[StepReceipt]) -> None:
        for r in receipts:
            self._conn.execute(
                "INSERT INTO gl_receipts VALUES (?, ?, ?)",
                (r.job_id, r.seq, _json.dumps(r.to_dict())),
            )

    def create(
        self, job: Job, contract_history: dict[str, Any], receipts: Sequence[StepReceipt]
    ) -> None:
        validate_create(job, receipts)
        history = _json.dumps(contract_history)
        with self._transaction():
            if self._conn.execute("SELECT 1 FROM gl_jobs WHERE job_id=?", (job.job_id,)).fetchone():
                raise JobExistsError(job.job_id)
            self._write_job(job, insert=True)
            self._conn.execute("INSERT INTO gl_histories VALUES (?, ?)", (job.job_id, history))
            self._insert_receipts(receipts)

    def load(self, job_id: str) -> Job:
        with self._lock:
            row = self._conn.execute(
                "SELECT job_json FROM gl_jobs WHERE job_id=?", (job_id,)
            ).fetchone()
        if row is None:
            raise JobNotFoundError(job_id)
        return _load_job(row[0])

    def commit(
        self,
        job: Job,
        receipts: Sequence[StepReceipt],
        *,
        expected_revision: int,
        contract_history: dict[str, Any] | None = None,
    ) -> None:
        history = None if contract_history is None else _json.dumps(contract_history)
        with self._transaction():
            row = self._conn.execute(
                "SELECT job_json FROM gl_jobs WHERE job_id=?", (job.job_id,)
            ).fetchone()
            if row is None:
                raise JobNotFoundError(job.job_id)
            validate_commit(_load_job(row[0]), job, receipts, expected_revision)
            self._write_job(job, insert=False)
            self._insert_receipts(receipts)
            if history is not None:
                self._conn.execute(
                    "UPDATE gl_histories SET history_json=? WHERE job_id=?", (history, job.job_id)
                )

    def receipts(self, job_id: str, *, after_seq: int = 0) -> list[StepReceipt]:
        with self._lock:
            if not self._conn.execute("SELECT 1 FROM gl_jobs WHERE job_id=?", (job_id,)).fetchone():
                raise JobNotFoundError(job_id)
            rows = self._conn.execute(
                "SELECT receipt_json FROM gl_receipts WHERE job_id=? AND seq>? ORDER BY seq",
                (job_id, after_seq),
            ).fetchall()
        return [StepReceipt.from_dict(_json.loads(r[0])) for r in rows]

    def contract_history(self, job_id: str) -> dict[str, Any]:
        with self._lock:
            row = self._conn.execute(
                "SELECT history_json FROM gl_histories WHERE job_id=?", (job_id,)
            ).fetchone()
        if row is None:
            raise JobNotFoundError(job_id)
        return _json.loads(row[0])

    def due(self, now: datetime) -> list[str]:
        _json.require_aware(now, "now")
        with self._lock:
            rows = self._conn.execute(
                "SELECT job_id FROM gl_jobs WHERE active=1 AND next_due_at IS NOT NULL "
                "AND next_due_at<=? ORDER BY next_due_at, job_id",
                (_utc_key(now),),
            ).fetchall()
        return [r[0] for r in rows]

    def active(self) -> list[str]:
        with self._lock:
            rows = self._conn.execute(
                "SELECT job_id FROM gl_jobs WHERE active=1 ORDER BY job_id"
            ).fetchall()
        return [r[0] for r in rows]

    def close(self) -> None:
        with self._lock:
            self._conn.close()
