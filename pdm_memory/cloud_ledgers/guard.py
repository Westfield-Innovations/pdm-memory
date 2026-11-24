"""A GuardedEffects whose ledger lives on a host; owner callbacks run only here."""

from __future__ import annotations

import json
import threading
import time
import typing
from typing import Any

from pdm_memory._ledger.chain import verify_rows
from pdm_memory._ledger.errors import LedgerIntegrityError
from pdm_memory.cloud_ledgers.client import CloudLedgers
from pdm_memory.cloud_ledgers.wire import decode, encode
from pdm_memory.guarded_effects import (
    CONDITION,
    VALIDATOR,
    ActionReceipt,
    CheckResult,
    EffectRequest,
    GuardedEffects,
    Opening,
    RecoveryContext,
    _check_openings,
    _json,
    _name,
)
from pdm_memory.storage.errors import CloudConflictError, CloudStorageError

__all__ = ["CloudGuardedEffects", "DispatchUnrecorded"]

_PAGE = 1000


class DispatchUnrecorded(CloudStorageError):
    """A handler ran, but its completion never reached the host; reconcile it.

    A handler's KeyboardInterrupt or SystemExit is re-raised instead, with this
    error as its ``__context__``.
    """

    def __init__(
        self, action_id: str, result: Any, error: BaseException | None
    ) -> None:
        super().__init__(
            f"the handler of {action_id} ran but its completion is unrecorded"
        )
        self.action_id, self.result, self.error = action_id, result, error


class CloudGuardedEffects(GuardedEffects):
    """The host keeps the ledger; ``validate``, ``check`` and ``handler`` run in this process.

    A ``stage`` or ``release`` that reaches an owner callback is answered with
    ``needs`` and the owner's chain head; the verdicts are sent back with that
    head as a fence, so a check made before someone else's dispatch is refused.
    """

    def __init__(
        self,
        cloud: CloudLedgers,
        *,
        owner: str,
        program_version: str,
        policy_version: str,
        openings: list[Opening],
        max_request_bytes: int = 65_536,
        max_pending: int = 128,
        max_state_bytes: int = 1_048_576,
        max_result_bytes: int = 65_536,
        replace_config: bool = False,
    ) -> None:
        if type(replace_config) is not bool:
            raise TypeError("replace_config must be bool")
        self.owner = _name(owner, "owner")
        self.program_version = _name(program_version, "program_version")
        self.policy_version = _name(policy_version, "policy_version")
        for label, value in [
            ("max_request_bytes", max_request_bytes),
            ("max_pending", max_pending),
            ("max_state_bytes", max_state_bytes),
            ("max_result_bytes", max_result_bytes),
        ]:
            if type(value) is not int or value < 1:
                raise ValueError(f"{label} must be a positive integer")
        self.max_request_bytes, self.max_pending = max_request_bytes, max_pending
        self.max_state_bytes, self.max_result_bytes = max_state_bytes, max_result_bytes
        self._openings = _check_openings(openings)
        self._lock = threading.RLock()
        self._in_flight: set[str] = set()
        self._verified: tuple[int, str] | None = None
        self._cloud = cloud
        self._open: dict[str, Any] = {
            "owner": self.owner,
            "program_version": self.program_version,
            "policy_version": self.policy_version,
            "openings": [
                {
                    "operation": op,
                    "target": target,
                    "max_dispatches": o.max_dispatches,
                    "has_check": o.check is not None,
                }
                for (op, target), o in sorted(self._openings.items())
            ],
            "max_request_bytes": max_request_bytes,
            "max_pending": max_pending,
            "max_state_bytes": max_state_bytes,
            "max_result_bytes": max_result_bytes,
            "replace_config": False,
        }
        body = cloud.call(
            "guard", "open", {**self._open, "replace_config": replace_config}, {}
        )
        self._config_epoch = decode(int, body["result"])
        self._open["config_epoch"] = self._config_epoch

    def _call(self, op: str, **args: Any) -> Any:
        body = self._cloud.call("guard", op, self._open, encode(args))
        return decode(
            typing.get_type_hints(getattr(GuardedEffects, op))["return"], body["result"]
        )

    def _send(self, op: str, args: dict[str, Any]) -> dict[str, Any]:
        """Run a two-step op: answer each ``needs`` with fresh verdicts until it lands."""
        start = time.monotonic()
        body = self._cloud.call("guard", op, self._open, encode(args))
        while "needs" in body:
            body = self._cloud.call(
                "guard", op, self._open, {**encode(args), **self._verdicts(body)}
            )
            if (
                "needs" in body
                and time.monotonic() - start > self._cloud.retry_deadline_s
            ):
                raise CloudConflictError(
                    f"guard/{op} kept losing its fence to other writes",
                    error_code="LEDGER_BUSY",
                )
        return body

    def _verdicts(self, body: dict[str, Any]) -> dict[str, Any]:
        encoded = body["request"]
        request = self._request(encoded)
        opening = self._openings[(request.operation, request.target)]
        # The local order: a condition is asked only after the parameters hold.
        validation = self._judge(opening.validate, encoded, VALIDATOR)
        verdicts = {"validation": encode(validation), "fence": body["fence"]}
        if "condition" in body["needs"] and validation.status == CheckResult.SATISFIED:
            verdicts["condition"] = encode(
                self._judge(opening.check, encoded, CONDITION)
            )
        return verdicts

    def close(self) -> None:
        """Nothing to release here: the CloudLedgers connection belongs to the caller."""

    def start_branch(self, branch_id: str) -> None:
        self._call("start_branch", branch_id=branch_id)

    def receipt(self, action_id: str) -> ActionReceipt:
        return self._call("receipt", action_id=action_id)

    def stage(self, request: EffectRequest) -> ActionReceipt:
        return decode(
            ActionReceipt, self._send("stage", {"request": request})["result"]
        )

    def dispatch_count(self, operation: str, target: str) -> int:
        return self._call("dispatch_count", operation=operation, target=target)

    def release(self, action_id: str) -> ActionReceipt:
        action_id = _name(action_id, "action_id")
        with self._lock:
            if action_id in self._in_flight:
                return self.receipt(action_id)
            body = self._send("release", {"action_id": action_id})
            receipt = decode(ActionReceipt, body["result"])
            if "dispatch" not in body:
                return receipt
            encoded = body["dispatch"]["request"]
            request = self._request(encoded)
            handler = self._openings[(request.operation, request.target)].handler
            self._in_flight.add(action_id)
        try:
            # The host committed the reservation before the handler is called.
            try:
                result = json.loads(
                    _json(handler(self._request(encoded)), self.max_result_bytes)
                )
            except BaseException as exc:
                failure = f"{type(exc).__name__}: {str(exc)[:512]}"
                if not isinstance(exc, Exception):
                    try:
                        self._finish(action_id, None, failure, exc)
                    except DispatchUnrecorded:
                        # An interrupt outranks the lost record; it stays its __context__.
                        raise exc
                    raise
                return self._finish(action_id, None, failure, exc)
            return self._finish(action_id, result, None, None)
        finally:
            with self._lock:
                self._in_flight.discard(action_id)

    def _finish(
        self,
        action_id: str,
        result: Any,
        failure: str | None,
        error: BaseException | None,
    ) -> ActionReceipt:
        args = {"action_id": action_id, "result": result, "failure": failure}
        try:
            body = self._cloud.call("guard", "complete", self._open, args)
        except CloudStorageError as exc:
            raise DispatchUnrecorded(action_id, result, error) from exc
        return decode(ActionReceipt, body["result"])

    def cancel(
        self, action_id: str, *, reason: str = "owner cancelled pending work"
    ) -> ActionReceipt:
        return self._call("cancel", action_id=action_id, reason=reason)

    def reconcile(
        self, action_id: str, *, executed: bool | None, evidence: Any
    ) -> ActionReceipt:
        if executed is not None and type(executed) is not bool:
            raise TypeError("executed must be bool or None")
        return self._call(
            "reconcile", action_id=action_id, executed=executed, evidence=evidence
        )

    def checkpoint(self, checkpoint_id: str, *, branch_id: str, state: Any) -> str:
        return self._call(
            "checkpoint", checkpoint_id=checkpoint_id, branch_id=branch_id, state=state
        )

    def recover(
        self, checkpoint_id: str, *, new_branch_id: str, receipt_limit: int = 128
    ) -> RecoveryContext:
        return self._call(
            "recover",
            checkpoint_id=checkpoint_id,
            new_branch_id=new_branch_id,
            receipt_limit=receipt_limit,
        )

    def history(
        self, action_id: str | None = None, *, after_sequence: int = 0, limit: int = 100
    ) -> list[dict[str, Any]]:
        return self._call(
            "history", action_id=action_id, after_sequence=after_sequence, limit=limit
        )

    def verify_history(self) -> bool:
        """Check the served chain; it must also extend the one verified last time."""
        with self._lock:
            previous, after, seen = "", 0, None
            mark = self._verified
            while True:
                body = self._cloud.call(
                    "guard",
                    "history_raw",
                    self._open,
                    {"after_sequence": after, "limit": _PAGE},
                )
                rows = decode(list[list[Any]], body["result"])
                previous = verify_rows(rows, self.owner, "guard", previous=previous)
                for row in rows:
                    if mark is not None and row[0] == mark[0]:
                        seen = row[6]
                if rows:
                    after = rows[-1][0]
                if len(rows) < _PAGE:
                    break
            if mark is not None and seen != mark[1]:
                raise LedgerIntegrityError(
                    "retained guard history is shorter than, or differs from, the chain verified before"
                )
            if after:
                self._verified = (after, previous)
            return True
