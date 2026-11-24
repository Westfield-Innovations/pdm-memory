"""A goal-loop job store whose every method is one call to a ledger host."""

from __future__ import annotations

from collections.abc import Sequence
from datetime import datetime
from typing import Any

from pdm_memory.cloud_ledgers.client import CloudLedgers
from pdm_memory.cloud_ledgers.wire import decode, encode
from pdm_memory.goal_loop.job import Job
from pdm_memory.goal_loop.receipts import StepReceipt
from pdm_memory.goal_loop.store import JobStore

__all__ = ["CloudJobStore"]


class CloudJobStore(JobStore):
    """The server runs the SQL job store; this class only carries arguments and results."""

    def __init__(self, cloud: CloudLedgers) -> None:
        self._cloud = cloud
        cloud.call("jobs", "open", {}, {})

    def _result(self, op: str, **args: Any) -> Any:
        return self._cloud.call("jobs", op, {}, encode(args))["result"]

    def create(
        self,
        job: Job,
        contract_history: dict[str, Any],
        receipts: Sequence[StepReceipt],
    ) -> None:
        self._result(
            "create", job=job, contract_history=contract_history, receipts=receipts
        )

    def load(self, job_id: str) -> Job:
        return decode(Job, self._result("load", job_id=job_id))

    def commit(
        self,
        job: Job,
        receipts: Sequence[StepReceipt],
        *,
        expected_revision: int,
        contract_history: dict[str, Any] | None = None,
    ) -> None:
        self._result(
            "commit",
            job=job,
            receipts=receipts,
            expected_revision=expected_revision,
            contract_history=contract_history,
        )

    def receipts(self, job_id: str, *, after_seq: int = 0) -> list[StepReceipt]:
        return decode(
            list[StepReceipt],
            self._result("receipts", job_id=job_id, after_seq=after_seq),
        )

    def contract_history(self, job_id: str) -> dict[str, Any]:
        return decode(dict[str, Any], self._result("contract_history", job_id=job_id))

    def due(self, now: datetime) -> list[str]:
        return decode(list[str], self._result("due", now=now))

    def active(self) -> list[str]:
        return decode(list[str], self._result("active"))
