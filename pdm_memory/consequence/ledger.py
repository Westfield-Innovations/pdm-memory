"""The consequence ledger: an owner-held, append-only record of expectations.

Like ``GuardedEffects``, this is a separate SQLite file outside the worker's
write authority. Its history is a hash chain that cannot be updated or
deleted. Retaining an expectation is never permission to act: the host's
action guard keeps release, and ``prepared_check`` only tells that guard
whether an expectation was committed first.
"""

from __future__ import annotations

import json
import sqlite3
import threading
from collections.abc import Callable
from contextlib import contextmanager
from datetime import datetime
from pathlib import Path
from typing import Any

from pdm_memory.consequence.records import Expectation, Observation, OwnerPolicy
from pdm_memory.goal_loop._json import dumps, parse_datetime, require_aware, utc_now
from pdm_memory.guarded_effects import (
    ActionReceipt,
    CheckResult,
    ConditionVerdict,
    EffectRequest,
    IdReuseError,
    LedgerIntegrityError,
    _hash,
    _name,
)

__all__ = ["ConsequenceLedger"]

_SCHEMA = """
CREATE TABLE IF NOT EXISTS cl_expectations (
  owner TEXT NOT NULL, expectation_id TEXT NOT NULL, record_json TEXT NOT NULL,
  action_id TEXT, committed_at TEXT NOT NULL, committed_seq INTEGER NOT NULL,
  PRIMARY KEY(owner, expectation_id));
CREATE UNIQUE INDEX IF NOT EXISTS cl_expectations_action
  ON cl_expectations(owner, action_id) WHERE action_id IS NOT NULL;
CREATE TABLE IF NOT EXISTS cl_actions (
  owner TEXT NOT NULL, action_id TEXT NOT NULL, receipt_sequence INTEGER NOT NULL,
  expectation_id TEXT NOT NULL, status TEXT NOT NULL, request_hash TEXT NOT NULL,
  PRIMARY KEY(owner, action_id, receipt_sequence));
CREATE TABLE IF NOT EXISTS cl_observations (
  owner TEXT NOT NULL, observation_id TEXT NOT NULL, revision INTEGER NOT NULL,
  record_json TEXT NOT NULL, recorded_seq INTEGER NOT NULL,
  PRIMARY KEY(owner, observation_id, revision));
CREATE TABLE IF NOT EXISTS cl_events (
  sequence INTEGER PRIMARY KEY, owner TEXT NOT NULL, expectation_id TEXT,
  kind TEXT NOT NULL, payload_json TEXT NOT NULL, previous_hash TEXT NOT NULL,
  event_hash TEXT NOT NULL);
CREATE INDEX IF NOT EXISTS cl_events_owner ON cl_events(owner, sequence);
CREATE TRIGGER IF NOT EXISTS cl_events_no_update BEFORE UPDATE ON cl_events
  BEGIN SELECT RAISE(ABORT, 'consequence history is append-only'); END;
CREATE TRIGGER IF NOT EXISTS cl_events_no_delete BEFORE DELETE ON cl_events
  BEGIN SELECT RAISE(ABORT, 'consequence history is append-only'); END;
"""


class ConsequenceLedger:
    """Retain expectations, action references and observations for one owner.

    The policy is the owner's; nothing on this object edits it. Every record
    is immutable once written: a correction is a new revision naming the one
    it supersedes.
    """

    def __init__(
        self,
        path: str | Path,
        *,
        owner: str,
        policy: OwnerPolicy,
        clock: Callable[[], datetime] = utc_now,
    ) -> None:
        if not str(path) or str(path) == ":memory:":
            raise ValueError("a durable on-disk ledger path is required")
        if not isinstance(policy, OwnerPolicy):
            raise TypeError("policy must be an OwnerPolicy")
        self.owner = _name(owner, "owner")
        self.policy = policy
        self._clock = clock
        self._limit = policy.limits.max_payload_bytes
        self._lock = threading.RLock()
        self._conn = sqlite3.connect(
            str(path), timeout=30, isolation_level=None, check_same_thread=False
        )
        self._conn.row_factory = sqlite3.Row
        self._conn.execute("PRAGMA journal_mode=WAL")
        self._conn.execute("PRAGMA synchronous=FULL")
        self._conn.executescript(_SCHEMA)

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

    def __enter__(self) -> ConsequenceLedger:
        return self

    def __exit__(self, *args: object) -> None:
        self.close()

    def _now(self) -> datetime:
        return require_aware(self._clock(), "clock()")

    # Same chain as GuardedEffects._append.
    # shortcut: copied hash-chain from GuardedEffects, extract a shared helper when a third ledger appears.
    def _append(self, kind: str, payload: dict[str, Any], expectation_id: str | None = None) -> int:
        sequence = self._conn.execute(
            "SELECT COALESCE(MAX(sequence), 0)+1 FROM cl_events"
        ).fetchone()[0]
        prev = self._conn.execute(
            "SELECT event_hash FROM cl_events WHERE owner=? ORDER BY sequence DESC LIMIT 1",
            (self.owner,),
        ).fetchone()
        previous_hash = prev[0] if prev else ""
        payload_json = dumps({**payload, "recorded_at": self._now().isoformat()})
        body = json.dumps(
            [sequence, self.owner, expectation_id, kind, payload_json, previous_hash],
            ensure_ascii=False,
            separators=(",", ":"),
        )
        self._conn.execute(
            "INSERT INTO cl_events VALUES (?, ?, ?, ?, ?, ?, ?)",
            (sequence, self.owner, expectation_id, kind, payload_json, previous_hash, _hash(body)),
        )
        return sequence

    # --- expectations -----------------------------------------------------

    def prepare(self, expectation: Expectation) -> str:
        """Retain *expectation* and return its id, before the action is released."""
        if not isinstance(expectation, Expectation):
            raise TypeError("prepare takes an Expectation")
        for c in expectation.criteria:
            if c.comparator not in self.policy.comparators:
                raise ValueError(f"comparator {c.comparator} is not registered by the owner")
        record = expectation.to_dict()
        encoded = dumps(record, limit=self._limit)
        with self._transaction():
            old = self._conn.execute(
                "SELECT record_json FROM cl_expectations WHERE owner=? AND expectation_id=?",
                (self.owner, expectation.expectation_id),
            ).fetchone()
            if old:
                if old["record_json"] != encoded:
                    raise IdReuseError("expectation_id already binds a different expectation")
                return expectation.expectation_id
            if expectation.action_id is not None and self._expectation_for(expectation.action_id):
                raise IdReuseError("this action already has a retained expectation")
            committed_at = self._now()
            seq = self._append(
                "EXPECTATION_COMMITTED",
                {"expectation": record, "committed_at": committed_at.isoformat()},
                expectation.expectation_id,
            )
            self._conn.execute(
                "INSERT INTO cl_expectations VALUES (?, ?, ?, ?, ?, ?)",
                (
                    self.owner,
                    expectation.expectation_id,
                    encoded,
                    expectation.action_id,
                    committed_at.isoformat(),
                    seq,
                ),
            )
        return expectation.expectation_id

    def expectation(self, expectation_id: str) -> tuple[Expectation, datetime]:
        """The retained expectation and when it was committed."""
        with self._lock:
            row = self._conn.execute(
                "SELECT * FROM cl_expectations WHERE owner=? AND expectation_id=?",
                (self.owner, _name(expectation_id, "expectation_id")),
            ).fetchone()
        if row is None:
            raise KeyError(f"no retained expectation {expectation_id}")
        return (
            Expectation.from_dict(json.loads(row["record_json"])),
            parse_datetime(row["committed_at"], "committed_at"),
        )

    def _expectation_for(self, action_id: str) -> str | None:
        row = self._conn.execute(
            "SELECT expectation_id FROM cl_expectations WHERE owner=? AND action_id=?",
            (self.owner, action_id),
        ).fetchone()
        return row[0] if row else None

    def prepared_check(self, request: EffectRequest) -> ConditionVerdict:
        """A ``GuardedEffects`` check: release only after an expectation is retained.

        Missing is UNKNOWN, so the guard holds the action instead of rejecting it.
        """
        with self._lock:
            expectation_id = self._expectation_for(request.action_id)
        if expectation_id is None:
            return ConditionVerdict(CheckResult.UNKNOWN, "no retained expectation for this action")
        return ConditionVerdict(
            CheckResult.SATISFIED, "expectation retained", {"expectation_id": expectation_id}
        )

    # --- actions and observations -------------------------------------------

    def attach_action(self, expectation_id: str, receipt: ActionReceipt) -> None:
        """Append a guard receipt. A handler return is execution state, not an effect."""
        if not isinstance(receipt, ActionReceipt):
            raise TypeError("attach_action takes the guard's ActionReceipt")
        expectation, _ = self.expectation(expectation_id)
        if expectation.action_id != receipt.action_id:
            raise ValueError("the receipt's action is not this expectation's action")
        with self._transaction():
            old = self._conn.execute(
                "SELECT status, request_hash FROM cl_actions"
                " WHERE owner=? AND action_id=? AND receipt_sequence=?",
                (self.owner, receipt.action_id, receipt.sequence),
            ).fetchone()
            if old:
                if (old["status"], old["request_hash"]) != (receipt.status, receipt.request_hash):
                    raise IdReuseError("this receipt sequence already binds a different receipt")
                return
            self._append(
                "ACTION_ATTACHED",
                {
                    "action_id": receipt.action_id,
                    "receipt_sequence": receipt.sequence,
                    "status": receipt.status,
                    "disposition": receipt.disposition.value,
                    "request_hash": receipt.request_hash,
                    "executed": receipt.executed,
                },
                expectation_id,
            )
            self._conn.execute(
                "INSERT INTO cl_actions VALUES (?, ?, ?, ?, ?, ?)",
                (
                    self.owner,
                    receipt.action_id,
                    receipt.sequence,
                    expectation_id,
                    receipt.status,
                    receipt.request_hash,
                ),
            )

    def actions(self, expectation_id: str) -> list[dict[str, Any]]:
        with self._lock:
            rows = self._conn.execute(
                "SELECT action_id, receipt_sequence, status FROM cl_actions"
                " WHERE owner=? AND expectation_id=? ORDER BY receipt_sequence",
                (self.owner, expectation_id),
            ).fetchall()
        return [dict(r) for r in rows]

    def observe(self, observation: Observation) -> tuple[str, int]:
        """Append one observation revision; a correction names what it supersedes."""
        if not isinstance(observation, Observation):
            raise TypeError("observe takes an Observation")
        record = observation.to_dict()
        encoded = dumps(record, limit=self._limit)
        key = (observation.observation_id, observation.revision)
        with self._transaction():
            old = self._conn.execute(
                "SELECT record_json FROM cl_observations"
                " WHERE owner=? AND observation_id=? AND revision=?",
                (self.owner, *key),
            ).fetchone()
            if old:
                if old["record_json"] != encoded:
                    raise IdReuseError("this observation revision already binds different evidence")
                return key
            latest = self._conn.execute(
                "SELECT MAX(revision) FROM cl_observations WHERE owner=? AND observation_id=?",
                (self.owner, observation.observation_id),
            ).fetchone()[0]
            if observation.revision != (latest or 0) + 1 or observation.supersedes != latest:
                raise ValueError(
                    f"revision {observation.revision} must follow revision {latest or 0}"
                    f" and name supersedes={latest}"
                )
            seq = self._append("OBSERVATION_RECORDED", {"observation": record})
            self._conn.execute(
                "INSERT INTO cl_observations VALUES (?, ?, ?, ?, ?)",
                (self.owner, *key, encoded, seq),
            )
        return key

    def observations(self, observable: str | None = None) -> list[Observation]:
        """The latest revision of every observation, in recording order."""
        with self._lock:
            rows = self._conn.execute(
                "SELECT o.record_json FROM cl_observations o"
                " WHERE o.owner=? AND o.revision = (SELECT MAX(revision) FROM cl_observations"
                " WHERE owner=o.owner AND observation_id=o.observation_id)"
                " ORDER BY o.recorded_seq",
                (self.owner,),
            ).fetchall()
        out = [Observation.from_dict(json.loads(r[0])) for r in rows]
        return [o for o in out if observable is None or o.observable == observable]

    # --- history -------------------------------------------------------------

    def history(
        self, expectation_id: str | None = None, *, after_sequence: int = 0, limit: int = 100
    ) -> list[dict[str, Any]]:
        """Read bounded ordered evidence. Continue with the last sequence."""
        if type(limit) is not int or not 1 <= limit <= 1000:
            raise ValueError("limit must be between 1 and 1000")
        if type(after_sequence) is not int or after_sequence < 0:
            raise ValueError("after_sequence must be a nonnegative integer")
        sql = "SELECT * FROM cl_events WHERE owner=? AND sequence>?"
        args: list[Any] = [self.owner, after_sequence]
        if expectation_id is not None:
            sql += " AND expectation_id=?"
            args.append(_name(expectation_id, "expectation_id"))
        with self._lock:
            rows = self._conn.execute(sql + " ORDER BY sequence LIMIT ?", [*args, limit]).fetchall()
        return [
            {
                "sequence": row["sequence"],
                "expectation_id": row["expectation_id"],
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
                "SELECT * FROM cl_events WHERE owner=? ORDER BY sequence", (self.owner,)
            )
            previous = ""
            for row in rows:
                body = json.dumps(
                    [
                        row["sequence"],
                        self.owner,
                        row["expectation_id"],
                        row["kind"],
                        row["payload_json"],
                        row["previous_hash"],
                    ],
                    ensure_ascii=False,
                    separators=(",", ":"),
                )
                if row["previous_hash"] != previous or _hash(body) != row["event_hash"]:
                    raise LedgerIntegrityError("retained consequence history chain is inconsistent")
                previous = row["event_hash"]
        return True
