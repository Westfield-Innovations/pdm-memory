"""Owner-controlled effect dispatch and recoverable application state.

This is an opt-in cooperative controller, not a process sandbox. Keep the
controller, its database and the registered functions outside the worker's
write authority. An independent execution boundary must prevent other routes
to the resources being protected.

No elapsed-time delay grants permission. Each release asks the current owner
condition again. The database keeps verdicts, dispatch reservations and counts
when application state is recovered. It does not capture arbitrary process
memory, rewind equipment, or automatically compensate an external effect.

Default bounds: 64 KiB request/result/check evidence, 128 pending actions,
1 MiB per application checkpoint, 32 JSON levels, 10,000 JSON values. The
append-only history and checkpoints require owner-managed disk capacity and
retention; they are never silently decayed like ordinary memories.
"""

from __future__ import annotations

import hashlib
import json
import math
import sqlite3
import threading
from collections.abc import Callable, Mapping
from contextlib import contextmanager
from dataclasses import dataclass, field
from datetime import datetime, timezone
from enum import Enum
from pathlib import Path
from typing import Any

__all__ = [
    "ActionReceipt",
    "BranchError",
    "CheckResult",
    "ConditionVerdict",
    "ConfigurationError",
    "EffectRequest",
    "GuardedEffects",
    "IdReuseError",
    "LedgerIntegrityError",
    "Opening",
    "RecoveryContext",
    "ResourceLimitError",
]


class CheckResult(str, Enum):
    SATISFIED = "SATISFIED"
    CONTRADICTED = "CONTRADICTED"
    UNKNOWN = "UNKNOWN"
    CONFLICT = "CONFLICT"


class IdReuseError(ValueError):
    """An existing immutable identifier was offered with different content."""


class BranchError(ValueError):
    """A branch is absent, retired, or would replace current work implicitly."""


class ConfigurationError(ValueError):
    """An owner configuration needs explicit replacement or was superseded."""


class ResourceLimitError(ValueError):
    """A declared input, queue or state bound was exceeded."""


class LedgerIntegrityError(RuntimeError):
    """Stored data does not match its captured content or history chain."""


@dataclass(frozen=True)
class ConditionVerdict:
    status: CheckResult
    reason: str = ""
    evidence: Any = field(default_factory=dict)


@dataclass(frozen=True)
class EffectRequest:
    action_id: str
    branch_id: str
    program_version: str
    policy_version: str
    operation: str
    target: str
    parameters: dict[str, Any] = field(default_factory=dict)


@dataclass(frozen=True)
class Opening:
    operation: str
    target: str
    handler: Callable[[EffectRequest], Any]
    validate: Callable[[EffectRequest], Any]
    check: Callable[[EffectRequest], Any] | None = None
    max_dispatches: int | None = None


@dataclass(frozen=True)
class ActionReceipt:
    action_id: str
    status: str
    disposition: CheckResult
    reason: str
    branch_id: str
    program_version: str
    policy_version: str
    request_hash: str
    sequence: int
    result: Any = None
    evidence: Any = None
    executed: bool | None = None


@dataclass(frozen=True)
class RecoveryContext:
    checkpoint_id: str
    branch_id: str
    state: Any
    checkpoint_program_version: str
    checkpoint_policy_version: str
    current_program_version: str
    current_policy_version: str
    receipts: tuple[ActionReceipt, ...]
    has_more_receipts: bool
    after_sequence: int


def _name(value: Any, label: str) -> str:
    if not isinstance(value, str) or not value.strip() or len(value) > 256:
        raise ValueError(f"{label} must be a nonempty string of at most 256 characters")
    return value


def _json(value: Any, limit: int) -> str:
    """Capture a strict JSON tree, without lossy key coercion or NaN values."""
    count = 0
    ancestors: set[int] = set()

    def visit(v: Any, depth: int) -> None:
        nonlocal count
        count += 1
        if count > 10_000 or depth > 32:
            raise ResourceLimitError("JSON exceeds the value-count or nesting bound")
        if v is None or type(v) in (bool, int):
            return
        if type(v) is float:
            if not math.isfinite(v):
                raise ValueError("JSON numbers must be finite")
            return
        if type(v) is str:
            if len(v.encode("utf-8")) > limit:
                raise ResourceLimitError("JSON string exceeds the byte bound")
            return
        if type(v) not in (dict, list):
            raise TypeError("Only plain JSON objects, arrays and scalar values are accepted")
        if len(v) > 10_000 or id(v) in ancestors:
            raise ResourceLimitError("JSON is cyclic or exceeds the value-count bound")
        ancestors.add(id(v))
        try:
            if isinstance(v, dict):
                for k, child in v.items():
                    if type(k) is not str:
                        raise TypeError("JSON object keys must be strings")
                    visit(k, depth + 1)
                    visit(child, depth + 1)
            else:
                for child in v:
                    visit(child, depth + 1)
        finally:
            ancestors.remove(id(v))

    visit(value, 0)
    encoded = json.dumps(
        value, ensure_ascii=False, sort_keys=True, separators=(",", ":"), allow_nan=False
    )
    if len(encoded.encode("utf-8")) > limit:
        raise ResourceLimitError(f"JSON exceeds the {limit}-byte bound")
    return encoded


def _hash(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


_TERMINAL = {"REJECTED", "CANCELLED", "RETURNED", "OBSERVED"}


class GuardedEffects:
    """Dispatch registered effects after current checks and durable intent.

    ``validate`` and ``check`` take a freshly reconstructed EffectRequest and
    return CheckResult, ConditionVerdict, bool, or None (UNKNOWN). Conditions
    must read trusted current evidence; request parameters are only claims.
    A handler receives another fresh request, so a check cannot change what
    the handler will receive. Checks are serialized by this database's write
    transaction, including other owner namespaces in the same database; keep
    them bounded and do not recursively call mutating controller methods.

    A different owner configuration requires ``replace_config=True`` on a new
    controller. This increments a durable epoch and fences previous objects
    and their staged work, even if version labels are later reused. Normal
    same-version reopen does not change the epoch. Configuration replacement
    cannot interrupt an already dispatched handler; quiesce handlers first.

    FULL SQLite synchronization requests durable commits on supported storage;
    the filesystem/hardware must honor those requests. This is not protection
    against an administrator replacing the database or deleting its tail.
    """

    def __init__(
        self,
        path: str | Path,
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
        if not str(path) or str(path) == ":memory:":
            raise ValueError("a durable on-disk ledger path is required")
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
        self._openings: dict[tuple[str, str], Opening] = {}
        for opening in openings:
            key = (_name(opening.operation, "operation"), _name(opening.target, "target"))
            if key in self._openings:
                raise ValueError(f"duplicate opening {key}")
            if not callable(opening.handler) or not callable(opening.validate):
                raise TypeError("each opening requires trusted handler and validate callables")
            if opening.check is not None and not callable(opening.check):
                raise TypeError("opening check must be callable")
            if opening.max_dispatches is not None and (
                type(opening.max_dispatches) is not int or opening.max_dispatches < 0
            ):
                raise ValueError("max_dispatches must be a nonnegative integer or None")
            self._openings[key] = opening
        self._lock = threading.RLock()
        self._conn = sqlite3.connect(
            str(path), timeout=30, isolation_level=None, check_same_thread=False
        )
        self._conn.row_factory = sqlite3.Row
        self._conn.execute("PRAGMA journal_mode=WAL")
        self._conn.execute("PRAGMA synchronous=FULL")
        self._conn.executescript("""
        CREATE TABLE IF NOT EXISTS ge_owners (
          owner TEXT PRIMARY KEY, active_branch TEXT NOT NULL);
        CREATE TABLE IF NOT EXISTS ge_config (
          owner TEXT PRIMARY KEY, program_version TEXT NOT NULL,
          policy_version TEXT NOT NULL, epoch INTEGER NOT NULL);
        CREATE TABLE IF NOT EXISTS ge_branches (
          owner TEXT NOT NULL, branch_id TEXT NOT NULL, active INTEGER NOT NULL,
          PRIMARY KEY(owner, branch_id));
        CREATE TABLE IF NOT EXISTS ge_actions (
          owner TEXT NOT NULL, action_id TEXT NOT NULL, request_json TEXT NOT NULL,
          request_hash TEXT NOT NULL, branch_id TEXT NOT NULL, status TEXT NOT NULL,
          disposition TEXT NOT NULL, reason TEXT NOT NULL, result_json TEXT,
          evidence_json TEXT, executed INTEGER, last_sequence INTEGER NOT NULL,
          config_epoch INTEGER NOT NULL,
          PRIMARY KEY(owner, action_id));
        CREATE TABLE IF NOT EXISTS ge_counts (
          owner TEXT NOT NULL, operation TEXT NOT NULL, target TEXT NOT NULL,
          dispatches INTEGER NOT NULL, PRIMARY KEY(owner, operation, target));
        CREATE TABLE IF NOT EXISTS ge_events (
          sequence INTEGER PRIMARY KEY, owner TEXT NOT NULL, action_id TEXT,
          kind TEXT NOT NULL, payload_json TEXT NOT NULL, previous_hash TEXT NOT NULL,
          event_hash TEXT NOT NULL);
        CREATE INDEX IF NOT EXISTS ge_events_owner ON ge_events(owner, sequence);
        CREATE TABLE IF NOT EXISTS ge_checkpoints (
          owner TEXT NOT NULL, checkpoint_id TEXT NOT NULL, branch_id TEXT NOT NULL,
          program_version TEXT NOT NULL, policy_version TEXT NOT NULL,
          state_json TEXT NOT NULL, state_hash TEXT NOT NULL, sequence INTEGER NOT NULL,
          PRIMARY KEY(owner, checkpoint_id));
        CREATE TRIGGER IF NOT EXISTS ge_events_no_update BEFORE UPDATE ON ge_events
          BEGIN SELECT RAISE(ABORT, 'guard history is append-only'); END;
        CREATE TRIGGER IF NOT EXISTS ge_events_no_delete BEFORE DELETE ON ge_events
          BEGIN SELECT RAISE(ABORT, 'guard history is append-only'); END;
        CREATE TRIGGER IF NOT EXISTS ge_checkpoints_no_update BEFORE UPDATE ON ge_checkpoints
          BEGIN SELECT RAISE(ABORT, 'guard checkpoints are immutable'); END;
        CREATE TRIGGER IF NOT EXISTS ge_checkpoints_no_delete BEFORE DELETE ON ge_checkpoints
          BEGIN SELECT RAISE(ABORT, 'guard checkpoints are immutable'); END;
        """)
        try:
            with self._transaction():
                current = self._conn.execute(
                    "SELECT * FROM ge_config WHERE owner=?", (self.owner,)
                ).fetchone()
                if current is None:
                    self._config_epoch = 1
                    self._conn.execute(
                        "INSERT INTO ge_config VALUES (?, ?, ?, ?)",
                        (self.owner, self.program_version, self.policy_version, self._config_epoch),
                    )
                elif replace_config:
                    self._config_epoch = current["epoch"] + 1
                    self._conn.execute(
                        "UPDATE ge_config SET program_version=?,policy_version=?,epoch=? WHERE owner=?",
                        (self.program_version, self.policy_version, self._config_epoch, self.owner),
                    )
                else:
                    if (current["program_version"], current["policy_version"]) != (
                        self.program_version,
                        self.policy_version,
                    ):
                        raise ConfigurationError(
                            "different current owner configuration; explicit replace_config=True is required"
                        )
                    self._config_epoch = current["epoch"]
                if current is None or replace_config:
                    self._append(
                        "CONFIGURATION_ACTIVATED",
                        {
                            "program_version": self.program_version,
                            "policy_version": self.policy_version,
                            "config_epoch": self._config_epoch,
                        },
                    )
        except BaseException:
            self._conn.close()
            raise

    @contextmanager
    def _transaction(self):
        with self._lock:
            self._conn.execute("BEGIN IMMEDIATE")
            try:
                yield
                self._conn.commit()
            except BaseException:
                self._conn.rollback()
                raise

    def close(self) -> None:
        with self._lock:
            self._conn.close()

    def __enter__(self) -> GuardedEffects:
        return self

    def __exit__(self, *args: Any) -> None:
        self.close()

    def _append(self, kind: str, payload: dict[str, Any], action_id: str | None = None) -> int:
        sequence = self._conn.execute(
            "SELECT COALESCE(MAX(sequence), 0)+1 FROM ge_events"
        ).fetchone()[0]
        prev = self._conn.execute(
            "SELECT event_hash FROM ge_events WHERE owner=? ORDER BY sequence DESC LIMIT 1",
            (self.owner,),
        ).fetchone()
        previous_hash = prev[0] if prev else ""
        # Payload sizes are bounded at their individual input boundaries.
        payload = {**payload, "recorded_at": datetime.now(timezone.utc).isoformat()}
        payload_json = json.dumps(
            payload, ensure_ascii=False, sort_keys=True, separators=(",", ":"), allow_nan=False
        )
        body = json.dumps(
            [sequence, self.owner, action_id, kind, payload_json, previous_hash],
            ensure_ascii=False,
            separators=(",", ":"),
        )
        self._conn.execute(
            "INSERT INTO ge_events VALUES (?, ?, ?, ?, ?, ?, ?)",
            (sequence, self.owner, action_id, kind, payload_json, previous_hash, _hash(body)),
        )
        return sequence

    def start_branch(self, branch_id: str) -> None:
        """Start the initial branch; a retired name can never be reactivated."""
        branch_id = _name(branch_id, "branch_id")
        with self._transaction():
            self._assert_current_config()
            row = self._conn.execute(
                "SELECT active_branch FROM ge_owners WHERE owner=?", (self.owner,)
            ).fetchone()
            if row:
                if row[0] == branch_id:
                    return
                raise BranchError("an active branch already exists; use recover to replace it")
            used = self._conn.execute(
                "SELECT 1 FROM ge_branches WHERE owner=? AND branch_id=?", (self.owner, branch_id)
            ).fetchone()
            if used:
                raise BranchError("branch name has already been used")
            self._conn.execute("INSERT INTO ge_owners VALUES (?, ?)", (self.owner, branch_id))
            self._conn.execute("INSERT INTO ge_branches VALUES (?, ?, 1)", (self.owner, branch_id))
            self._append(
                "BRANCH_STARTED",
                {
                    "branch_id": branch_id,
                    "program_version": self.program_version,
                    "policy_version": self.policy_version,
                },
            )

    def _capture(self, request: EffectRequest) -> str:
        if not isinstance(request, EffectRequest) or type(request.parameters) is not dict:
            raise TypeError("request must be EffectRequest with a JSON-object parameters field")
        fields = {
            name: _name(getattr(request, name), name)
            for name in (
                "action_id",
                "branch_id",
                "program_version",
                "policy_version",
                "operation",
                "target",
            )
        }
        fields["parameters"] = request.parameters
        return _json(fields, self.max_request_bytes)

    @staticmethod
    def _request(encoded: str) -> EffectRequest:
        return EffectRequest(**json.loads(encoded))

    def _row(self, action_id: str) -> sqlite3.Row:
        row = self._conn.execute(
            "SELECT * FROM ge_actions WHERE owner=? AND action_id=?", (self.owner, action_id)
        ).fetchone()
        if row is None:
            raise KeyError(action_id)
        if self._request_hash(row["request_json"], row["config_epoch"]) != row["request_hash"]:
            raise LedgerIntegrityError("captured request content no longer matches its hash")
        return row

    @staticmethod
    def _request_hash(encoded: str, epoch: int) -> str:
        # The worker supplies the complete request; the owner adds its epoch.
        # Include both in the captured identity so epoch tampering is detected.
        return _hash(json.dumps([epoch, encoded], ensure_ascii=False, separators=(",", ":")))

    @staticmethod
    def _receipt(row: sqlite3.Row) -> ActionReceipt:
        request = json.loads(row["request_json"])
        return ActionReceipt(
            row["action_id"],
            row["status"],
            CheckResult(row["disposition"]),
            row["reason"],
            row["branch_id"],
            request["program_version"],
            request["policy_version"],
            row["request_hash"],
            row["last_sequence"],
            json.loads(row["result_json"]) if row["result_json"] is not None else None,
            json.loads(row["evidence_json"]) if row["evidence_json"] is not None else None,
            bool(row["executed"]) if row["executed"] is not None else None,
        )

    def receipt(self, action_id: str) -> ActionReceipt:
        with self._lock:
            return self._receipt(self._row(_name(action_id, "action_id")))

    def _set(
        self,
        action_id: str,
        status: str,
        disposition: CheckResult,
        reason: str,
        *,
        evidence: Any = None,
        result: Any = None,
        executed: bool | None = None,
    ) -> ActionReceipt:
        reason = str(reason)[:1024]
        evidence_json = _json(evidence, self.max_result_bytes)
        result_json = _json(result, self.max_result_bytes)
        seq = self._append(
            status,
            {
                "disposition": disposition.value,
                "reason": reason,
                "evidence": json.loads(evidence_json),
                "result": json.loads(result_json),
                "executed": executed,
            },
            action_id,
        )
        self._conn.execute(
            """UPDATE ge_actions SET status=?, disposition=?, reason=?,
          evidence_json=?, result_json=?, executed=?, last_sequence=? WHERE owner=? AND action_id=?""",
            (
                status,
                disposition.value,
                reason,
                evidence_json,
                result_json,
                executed,
                seq,
                self.owner,
                action_id,
            ),
        )
        return self._receipt(self._row(action_id))

    def _assert_current_config(self) -> None:
        config = self._conn.execute(
            "SELECT program_version,policy_version,epoch FROM ge_config WHERE owner=?",
            (self.owner,),
        ).fetchone()
        if config is None or tuple(config) != (
            self.program_version,
            self.policy_version,
            self._config_epoch,
        ):
            raise ConfigurationError("controller configuration has been superseded by the owner")

    def _binding_failure(
        self, request: EffectRequest, captured_epoch: int | None = None
    ) -> str | None:
        if captured_epoch is not None and captured_epoch != self._config_epoch:
            return "request was captured under a superseded owner configuration epoch"
        if request.program_version != self.program_version:
            return "running program version differs from the request's admitted version"
        if request.policy_version != self.policy_version:
            return "current owner policy version differs from the request's policy version"
        branch = self._conn.execute(
            "SELECT active_branch FROM ge_owners WHERE owner=?", (self.owner,)
        ).fetchone()
        if not branch or branch[0] != request.branch_id:
            return "request belongs to an absent or retired branch"
        if (request.operation, request.target) not in self._openings:
            return "operation and target have no owner-registered opening"
        return None

    def _judge(
        self, callback: Callable[[EffectRequest], Any] | None, encoded: str, label: str
    ) -> ConditionVerdict:
        if callback is None:
            return ConditionVerdict(CheckResult.SATISFIED, "no additional owner condition")
        try:
            result = callback(self._request(encoded))
            if isinstance(result, ConditionVerdict):
                verdict = ConditionVerdict(
                    CheckResult(result.status),
                    str(result.reason)[:1024],
                    json.loads(_json(result.evidence, self.max_result_bytes)),
                )
            elif isinstance(result, CheckResult):
                verdict = ConditionVerdict(result)
            elif type(result) is bool:
                verdict = ConditionVerdict(
                    CheckResult.SATISFIED if result else CheckResult.CONTRADICTED
                )
            elif result is None:
                verdict = ConditionVerdict(CheckResult.UNKNOWN)
            else:
                return ConditionVerdict(
                    CheckResult.UNKNOWN, f"{label} returned an unsupported verdict"
                )
            return verdict
        except Exception as exc:
            return ConditionVerdict(
                CheckResult.UNKNOWN, f"{label} raised {type(exc).__name__}: {str(exc)[:512]}"
            )

    def stage(self, request: EffectRequest) -> ActionReceipt:
        """Capture an immutable request. Staging is never execution permission."""
        encoded = self._capture(request)
        captured = self._request(encoded)
        with self._transaction():
            self._assert_current_config()
            old = self._conn.execute(
                "SELECT * FROM ge_actions WHERE owner=? AND action_id=?",
                (self.owner, captured.action_id),
            ).fetchone()
            if old:
                if old["request_json"] != encoded:
                    raise IdReuseError("action_id already binds a different complete request")
                return self._receipt(self._row(captured.action_id))
            failure = self._binding_failure(captured)
            verdict = (
                ConditionVerdict(CheckResult.CONTRADICTED, failure)
                if failure
                else self._judge(
                    self._openings[(captured.operation, captured.target)].validate,
                    encoded,
                    "parameter validator",
                )
            )
            held = verdict.status != CheckResult.CONTRADICTED
            if held:
                pending = self._conn.execute(
                    "SELECT COUNT(*) FROM ge_actions WHERE owner=? AND status='HELD'", (self.owner,)
                ).fetchone()[0]
                if pending >= self.max_pending:
                    raise ResourceLimitError("pending action bound reached")
            self._conn.execute(
                "INSERT INTO ge_actions VALUES (?, ?, ?, ?, ?, 'HELD', ?, '', NULL, NULL, NULL, 0, ?)",
                (
                    self.owner,
                    captured.action_id,
                    encoded,
                    self._request_hash(encoded, self._config_epoch),
                    captured.branch_id,
                    CheckResult.UNKNOWN.value,
                    self._config_epoch,
                ),
            )
            self._append(
                "REQUEST_CAPTURED",
                {"request": json.loads(encoded), "config_epoch": self._config_epoch},
                captured.action_id,
            )
            if not held:
                return self._set(
                    captured.action_id,
                    "REJECTED",
                    verdict.status,
                    verdict.reason,
                    evidence=verdict.evidence,
                )
            return self._set(
                captured.action_id,
                "HELD",
                CheckResult.UNKNOWN,
                "staged; current release conditions have not yet been admitted",
                evidence={
                    "validation": verdict.status.value,
                    "reason": verdict.reason,
                    "evidence": verdict.evidence,
                },
            )

    def dispatch_count(self, operation: str, target: str) -> int:
        """Lifetime dispatch attempts, retained across recovery and policy changes."""
        with self._lock:
            row = self._conn.execute(
                "SELECT dispatches FROM ge_counts WHERE owner=? AND operation=? AND target=?",
                (self.owner, operation, target),
            ).fetchone()
            return row[0] if row else 0

    def release(self, action_id: str) -> ActionReceipt:
        """Rejudge and reserve once, then invoke only the registered handler.

        A handler return is RETURNED, not an observed physical success. A
        failed/invalid return is UNCERTAIN because the function may already
        have affected the outside world. Releasing that ID again never calls
        the handler again. Other effects wait while an unresolved dispatch
        exists in this owner namespace.
        """
        action_id = _name(action_id, "action_id")
        with self._transaction():
            self._assert_current_config()
            row = self._row(action_id)
            if row["status"] in _TERMINAL or row["status"] == "UNCERTAIN":
                return self._receipt(row)
            if row["status"] == "DISPATCHING":
                return self._set(
                    action_id,
                    "UNCERTAIN",
                    CheckResult.UNKNOWN,
                    "dispatch intent exists; delivery or completion must be reconciled",
                )
            encoded = row["request_json"]
            request = self._request(encoded)
            failure = self._binding_failure(request, row["config_epoch"])
            if failure:
                return self._set(action_id, "REJECTED", CheckResult.CONTRADICTED, failure)
            pending = self._conn.execute(
                "SELECT action_id FROM ge_actions WHERE owner=? AND status IN ('DISPATCHING','UNCERTAIN') LIMIT 1",
                (self.owner,),
            ).fetchone()
            if pending:
                return self._set(
                    action_id,
                    "HELD",
                    CheckResult.UNKNOWN,
                    "another dispatch is still running or requires reconciliation",
                    evidence={"blocking_action_id": pending[0]},
                )
            opening = self._openings[(request.operation, request.target)]
            validation = self._judge(opening.validate, encoded, "parameter validator")
            if validation.status != CheckResult.SATISFIED:
                return self._set(
                    action_id,
                    "REJECTED" if validation.status == CheckResult.CONTRADICTED else "HELD",
                    validation.status,
                    validation.reason or "parameters not established",
                    evidence=validation.evidence,
                )
            verdict = self._judge(opening.check, encoded, "current condition")
            if verdict.status != CheckResult.SATISFIED:
                return self._set(
                    action_id,
                    "REJECTED" if verdict.status == CheckResult.CONTRADICTED else "HELD",
                    verdict.status,
                    verdict.reason or "required condition not satisfied",
                    evidence=verdict.evidence,
                )
            count = self.dispatch_count(request.operation, request.target)
            if opening.max_dispatches is not None and count >= opening.max_dispatches:
                return self._set(
                    action_id,
                    "REJECTED",
                    CheckResult.CONTRADICTED,
                    "owner opening dispatch limit reached",
                    evidence={"dispatch_count": count},
                )
            self._conn.execute(
                """INSERT INTO ge_counts VALUES (?, ?, ?, 1)
              ON CONFLICT(owner, operation, target) DO UPDATE SET dispatches=dispatches+1""",
                (self.owner, request.operation, request.target),
            )
            self._set(
                action_id,
                "DISPATCHING",
                CheckResult.SATISFIED,
                verdict.reason or "current conditions satisfied; dispatch intent secured",
                evidence=verdict.evidence,
            )
        # The transaction committed before any outward action is attempted.
        try:
            result_json = _json(opening.handler(self._request(encoded)), self.max_result_bytes)
        except BaseException as exc:
            with self._transaction():
                row = self._row(action_id)
                if row["status"] not in _TERMINAL:
                    receipt = self._set(
                        action_id,
                        "UNCERTAIN",
                        CheckResult.UNKNOWN,
                        f"handler completion uncertain after {type(exc).__name__}: {str(exc)[:512]}",
                    )
                else:
                    receipt = self._receipt(row)
            if not isinstance(exc, Exception):
                raise
            return receipt
        with self._transaction():
            row = self._row(action_id)
            if row["status"] in _TERMINAL:
                self._append("LATE_HANDLER_RETURN", {"result": json.loads(result_json)}, action_id)
                return self._receipt(row)
            return self._set(
                action_id,
                "RETURNED",
                CheckResult.SATISFIED,
                "registered handler returned; physical outcome is not inferred",
                result=json.loads(result_json),
                evidence=verdict.evidence,
            )

    def cancel(
        self, action_id: str, *, reason: str = "owner cancelled pending work"
    ) -> ActionReceipt:
        with self._transaction():
            self._assert_current_config()
            row = self._row(_name(action_id, "action_id"))
            if row["status"] != "HELD":
                return self._receipt(row)
            return self._set(action_id, "CANCELLED", CheckResult.UNKNOWN, reason)

    def reconcile(self, action_id: str, *, executed: bool | None, evidence: Any) -> ActionReceipt:
        """Record trusted outside evidence, never dispatch the action again.

        Call this from the owner side after settling any live handler. A
        worker's assertion is not independent observation. ``None`` retains
        uncertainty. ``False`` does not grant automatic retry permission.
        """
        if executed is not None and type(executed) is not bool:
            raise TypeError("executed must be bool or None")
        evidence = json.loads(_json(evidence, self.max_result_bytes))
        if not evidence:
            raise ValueError("reconciliation requires explicit observation evidence")
        with self._transaction():
            self._assert_current_config()
            row = self._row(_name(action_id, "action_id"))
            if row["status"] not in ("UNCERTAIN", "RETURNED"):
                raise ValueError("only uncertain or returned actions can be reconciled")
            return self._set(
                action_id,
                "UNCERTAIN" if executed is None else "OBSERVED",
                CheckResult.UNKNOWN if executed is None else CheckResult.SATISFIED,
                "owner supplied current outcome evidence",
                evidence=evidence,
                result=json.loads(row["result_json"]) if row["result_json"] else None,
                executed=executed,
            )

    def checkpoint(self, checkpoint_id: str, *, branch_id: str, state: Any) -> str:
        """Store owner-captured explicit application state, not arbitrary RAM.

        Storage means the supplied state was captured, not that all internal
        behavior has been verified. The trusted adapter chooses a coherent
        boundary and state representation. The worker must not own this API.
        """
        checkpoint_id = _name(checkpoint_id, "checkpoint_id")
        branch_id = _name(branch_id, "branch_id")
        state_json = _json(state, self.max_state_bytes)
        with self._transaction():
            self._assert_current_config()
            active = self._conn.execute(
                "SELECT active_branch FROM ge_owners WHERE owner=?", (self.owner,)
            ).fetchone()
            if not active or active[0] != branch_id:
                raise BranchError("checkpoint must belong to the current branch")
            old = self._conn.execute(
                "SELECT * FROM ge_checkpoints WHERE owner=? AND checkpoint_id=?",
                (self.owner, checkpoint_id),
            ).fetchone()
            if old:
                if (
                    old["state_json"],
                    old["branch_id"],
                    old["program_version"],
                    old["policy_version"],
                ) != (state_json, branch_id, self.program_version, self.policy_version):
                    raise IdReuseError("checkpoint_id already binds different state or versions")
                return checkpoint_id
            seq = self._append(
                "CHECKPOINT_CAPTURED",
                {
                    "checkpoint_id": checkpoint_id,
                    "branch_id": branch_id,
                    "state_hash": _hash(state_json),
                    "program_version": self.program_version,
                    "policy_version": self.policy_version,
                    "config_epoch": self._config_epoch,
                },
            )
            self._conn.execute(
                "INSERT INTO ge_checkpoints VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                (
                    self.owner,
                    checkpoint_id,
                    branch_id,
                    self.program_version,
                    self.policy_version,
                    state_json,
                    _hash(state_json),
                    seq,
                ),
            )
            return checkpoint_id

    def recover(
        self, checkpoint_id: str, *, new_branch_id: str, receipt_limit: int = 128
    ) -> RecoveryContext:
        """Retire current work and return captured state plus surviving receipts.

        This does not stop another process or interrupt an already dispatched
        handler. The integration must quiesce its worker. In-flight dispatches
        become uncertain and block further releases until reconciled.
        Current owner configuration is retained, never loaded from the past.
        """
        checkpoint_id = _name(checkpoint_id, "checkpoint_id")
        new_branch_id = _name(new_branch_id, "new_branch_id")
        if type(receipt_limit) is not int or not 1 <= receipt_limit <= 1000:
            raise ValueError("receipt_limit must be between 1 and 1000")
        with self._transaction():
            self._assert_current_config()
            point = self._conn.execute(
                "SELECT * FROM ge_checkpoints WHERE owner=? AND checkpoint_id=?",
                (self.owner, checkpoint_id),
            ).fetchone()
            if point is None:
                raise KeyError(checkpoint_id)
            if _hash(point["state_json"]) != point["state_hash"]:
                raise LedgerIntegrityError("checkpoint content does not match its capture hash")
            if point["program_version"] != self.program_version:
                raise ValueError(
                    "checkpoint program version is incompatible with the current worker"
                )
            used = self._conn.execute(
                "SELECT 1 FROM ge_branches WHERE owner=? AND branch_id=?",
                (self.owner, new_branch_id),
            ).fetchone()
            if used:
                raise BranchError("new branch name has already been used")
            active = self._conn.execute(
                "SELECT active_branch FROM ge_owners WHERE owner=?", (self.owner,)
            ).fetchone()
            if not active:
                raise BranchError("no current branch to recover")
            old_branch = active[0]
            rows = self._conn.execute(
                "SELECT action_id,status FROM ge_actions WHERE owner=? AND branch_id=? AND status IN ('HELD','DISPATCHING')",
                (self.owner, old_branch),
            ).fetchall()
            for row in rows:
                if row["status"] == "HELD":
                    self._set(
                        row["action_id"],
                        "CANCELLED",
                        CheckResult.UNKNOWN,
                        "old branch retired during recovery",
                    )
                else:
                    self._set(
                        row["action_id"],
                        "UNCERTAIN",
                        CheckResult.UNKNOWN,
                        "recovery began after dispatch intent; external outcome must be reconciled",
                    )
            self._conn.execute(
                "UPDATE ge_branches SET active=0 WHERE owner=? AND branch_id=?",
                (self.owner, old_branch),
            )
            self._conn.execute(
                "INSERT INTO ge_branches VALUES (?, ?, 1)", (self.owner, new_branch_id)
            )
            self._conn.execute(
                "UPDATE ge_owners SET active_branch=? WHERE owner=?", (new_branch_id, self.owner)
            )
            self._append(
                "WORKER_STATE_RECOVERED",
                {
                    "checkpoint_id": checkpoint_id,
                    "retired_branch": old_branch,
                    "new_branch": new_branch_id,
                    "program_version": self.program_version,
                    "current_policy_version": self.policy_version,
                    "checkpoint_policy_version": point["policy_version"],
                },
            )
            rows = self._conn.execute(
                "SELECT * FROM ge_actions WHERE owner=? AND last_sequence>? ORDER BY last_sequence LIMIT ?",
                (self.owner, point["sequence"], receipt_limit + 1),
            ).fetchall()
            receipts = tuple(self._receipt(row) for row in rows[:receipt_limit])
            return RecoveryContext(
                checkpoint_id,
                new_branch_id,
                json.loads(point["state_json"]),
                point["program_version"],
                point["policy_version"],
                self.program_version,
                self.policy_version,
                receipts,
                len(rows) > receipt_limit,
                point["sequence"],
            )

    def history(
        self, action_id: str | None = None, *, after_sequence: int = 0, limit: int = 100
    ) -> list[dict[str, Any]]:
        """Read bounded ordered evidence. Continue with the last sequence."""
        if type(limit) is not int or not 1 <= limit <= 1000:
            raise ValueError("limit must be between 1 and 1000")
        if type(after_sequence) is not int or after_sequence < 0:
            raise ValueError("after_sequence must be a nonnegative integer")
        with self._lock:
            sql = "SELECT * FROM ge_events WHERE owner=? AND sequence>?"
            args: list[Any] = [self.owner, after_sequence]
            if action_id is not None:
                sql += " AND action_id=?"
                args.append(_name(action_id, "action_id"))
            rows = self._conn.execute(sql + " ORDER BY sequence LIMIT ?", [*args, limit]).fetchall()
            return [
                {
                    "sequence": row["sequence"],
                    "action_id": row["action_id"],
                    "kind": row["kind"],
                    "payload": json.loads(row["payload_json"]),
                    "previous_hash": row["previous_hash"],
                    "event_hash": row["event_hash"],
                }
                for row in rows
            ]

    def verify_history(self) -> bool:
        """Check the retained chain; this cannot detect privileged tail removal."""
        with self._lock:
            rows = self._conn.execute(
                "SELECT * FROM ge_events WHERE owner=? ORDER BY sequence", (self.owner,)
            )
            previous = ""
            for row in rows:
                body = json.dumps(
                    [
                        row["sequence"],
                        self.owner,
                        row["action_id"],
                        row["kind"],
                        row["payload_json"],
                        row["previous_hash"],
                    ],
                    ensure_ascii=False,
                    separators=(",", ":"),
                )
                if row["previous_hash"] != previous or _hash(body) != row["event_hash"]:
                    raise LedgerIntegrityError("retained guard history chain is inconsistent")
                previous = row["event_hash"]
            return True
