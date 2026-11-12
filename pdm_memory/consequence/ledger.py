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
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any

from pdm_memory.consequence.compare import COMPARATORS, NOT_OBSERVED
from pdm_memory.consequence.records import (
    Applicability,
    CauseHypothesis,
    Comparison,
    Criterion,
    Evaluation,
    EvidenceKind,
    Expectation,
    Finding,
    Observation,
    Ordering,
    OwnerPolicy,
    Response,
    TaskRef,
)
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
CREATE TABLE IF NOT EXISTS cl_evaluations (
  owner TEXT NOT NULL, evaluation_id TEXT NOT NULL, expectation_id TEXT NOT NULL,
  revision INTEGER NOT NULL, record_json TEXT NOT NULL, superseded_by TEXT,
  PRIMARY KEY(owner, evaluation_id), UNIQUE(owner, expectation_id, revision));
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
            COMPARATORS[c.comparator].check_expected(c.expected)
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

    # --- evaluation ----------------------------------------------------------

    def evaluate(
        self,
        expectation_id: str,
        *,
        task: TaskRef,
        causes: tuple[CauseHypothesis, ...] = (),
    ) -> Evaluation:
        """Grade each criterion against admissible evidence, for these exact revisions.

        The same evidence, comparators and policy return the same evaluation;
        restating a cause without new evidence is not new evidence.
        """
        expectation, committed_at = self.expectation(expectation_id)
        if task != expectation.task:
            raise ValueError("an evaluation is bound to the expectation's own task")
        causes = tuple(causes)
        if not all(isinstance(h, CauseHypothesis) for h in causes):
            raise TypeError("causes must be CauseHypothesis records")
        observables = {c.observable for c in expectation.criteria}
        latest = self.observations()
        by_id = {o.observation_id: o for o in latest}
        relevant = [o for o in latest if o.observable in observables]
        cited = {ref for h in causes for ref in h.support if ref in by_id}
        inputs = sorted(
            {(o.observation_id, o.revision) for o in relevant}
            | {(ref, by_id[ref].revision) for ref in cited}
        )
        evaluation_id = _hash(
            dumps(
                [
                    expectation_id,
                    [list(i) for i in inputs],
                    sorted(list(c.comparator) for c in expectation.criteria),
                    self.policy.policy_version,
                ]
            )
        )
        with self._transaction():
            old = self._evaluation_row(evaluation_id)
            if old is not None:
                return self._evaluation_from(old)
            revision = (
                self._conn.execute(
                    "SELECT COALESCE(MAX(revision), 0) FROM cl_evaluations"
                    " WHERE owner=? AND expectation_id=?",
                    (self.owner, expectation_id),
                ).fetchone()[0]
                + 1
            )
            findings = tuple(
                _finding(expectation, committed_at, c, relevant, self.policy)
                for c in expectation.criteria
            )
            evaluation = Evaluation(
                evaluation_id=evaluation_id,
                expectation_id=expectation_id,
                revision=revision,
                findings=findings,
                policy_version=self.policy.policy_version,
                causes=causes,
                attribution_supported=any(
                    h.support and all(r in by_id and r not in expectation.inputs for r in h.support)
                    for h in causes
                ),
                response=_response(findings),
            )
            record = evaluation.to_dict()
            self._conn.execute(
                "INSERT INTO cl_evaluations VALUES (?, ?, ?, ?, ?, NULL)",
                (self.owner, evaluation_id, expectation_id, revision, dumps(record)),
            )
            self._append("EVALUATED", {"evaluation": record}, expectation_id)
        return evaluation

    def evaluation(self, evaluation_id: str) -> Evaluation:
        with self._lock:
            row = self._evaluation_row(_name(evaluation_id, "evaluation_id"))
        if row is None:
            raise KeyError(f"no evaluation {evaluation_id}")
        return self._evaluation_from(row)

    def _evaluation_row(self, evaluation_id: str) -> sqlite3.Row | None:
        return self._conn.execute(
            "SELECT * FROM cl_evaluations WHERE owner=? AND evaluation_id=?",
            (self.owner, evaluation_id),
        ).fetchone()

    @staticmethod
    def _evaluation_from(row: sqlite3.Row) -> Evaluation:
        return Evaluation.from_dict(json.loads(row["record_json"]), row["superseded_by"])

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


# --- grading ---------------------------------------------------------------


def _inadmissible(o: Observation, c: Criterion, e: Expectation, policy: OwnerPolicy) -> str:
    """Why *o* cannot grade *c*, or "" when it can."""
    source = policy.source(o.source)
    if source is None or o.observable not in source.observables:
        return "source"
    if o.unit != c.unit:
        return "unit"
    if not o.valid:
        return "invalid"
    if o.kind is EvidenceKind.SIMULATED:
        return "simulated"
    if o.evidence_ref in e.inputs:
        return "own witness"
    h = e.horizon
    if o.value is None:
        covered = o.coverage is not None and o.coverage[0] <= h.window_start
        return "" if covered and o.coverage[1] >= h.window_end else "coverage"
    if not h.window_start <= o.event_time <= h.window_end:
        return "window"
    return ""


def _ordering(o: Observation, e: Expectation, committed_at: datetime) -> Ordering:
    recorded = e.source_sequences.get(o.source)
    if recorded is not None and o.source_sequence is not None:
        return Ordering.PROSPECTIVE if o.source_sequence > recorded else Ordering.RETROSPECTIVE
    # A silence is timed by when its coverage began, not when it was reported.
    t = o.coverage[0] if o.value is None else o.event_time
    u = timedelta(seconds=o.clock_uncertainty_s)
    if t - u > committed_at:
        return Ordering.PROSPECTIVE
    if t + u <= committed_at:
        return Ordering.RETROSPECTIVE
    return Ordering.UNCERTAIN


def _applicability(found: list[Observation], e: Expectation) -> Applicability:
    if not found:
        return Applicability.UNRESOLVED
    keys = e.horizon.invalidated_by
    seen = {tuple(o.conditions[k] for k in keys) for o in found if all(k in o.conditions for k in keys)}
    if len(seen) > 1:
        return Applicability.CONFLICTING
    results = set()
    for o in found:
        result = Applicability.APPLICABLE
        for k, v in e.horizon.environment_versions.items():
            if k in o.configuration and o.configuration[k] != v:
                result = Applicability.OUT_OF_SCOPE
        for k in keys:
            if k not in o.conditions:
                result = result if result is Applicability.OUT_OF_SCOPE else Applicability.UNRESOLVED
            elif o.conditions[k] != e.conditions.get(k):
                result = Applicability.OUT_OF_SCOPE
        results.add(result)
    for kind in (Applicability.OUT_OF_SCOPE, Applicability.UNRESOLVED):
        if kind in results:
            return kind
    return Applicability.APPLICABLE


def _finding(
    e: Expectation,
    committed_at: datetime,
    c: Criterion,
    observations: list[Observation],
    policy: OwnerPolicy,
) -> Finding:
    compare = COMPARATORS[c.comparator].compare
    excluded: list[tuple[str, str]] = []
    graded: list[tuple[Observation, Ordering, bool, bool]] = []
    for o in observations:
        if o.observable != c.observable:
            continue
        reason = _inadmissible(o, c, e, policy)
        if reason:
            excluded.append((o.observation_id, reason))
            continue
        ordering = _ordering(o, e, committed_at)
        if ordering is Ordering.UNCERTAIN:
            excluded.append((o.observation_id, "ordering"))
            continue
        try:
            met, allowed = compare(c.expected, NOT_OBSERVED if o.value is None else o.value)
        except (TypeError, ValueError):
            excluded.append((o.observation_id, "value"))
            continue
        graded.append((o, ordering, met, allowed))
    if not graded:
        reasons = sorted({r for _, r in excluded})
        missing = "no admissible observation" + (f" ({', '.join(reasons)})" if reasons else "")
        return Finding(c.name, None, Applicability.UNRESOLVED, Comparison.UNRESOLVED, (),
                       tuple(excluded), False, missing)
    mets = {met for _, _, met, _ in graded}
    if mets == {True}:
        comparison = Comparison.CONFIRMED_MATCH
    elif mets == {False}:
        comparison = Comparison.CONFIRMED_MISS
    else:
        comparison = Comparison.CONFLICT
    orderings = {ordering for _, ordering, _, _ in graded}
    return Finding(
        criterion=c.name,
        ordering=Ordering.RETROSPECTIVE if Ordering.RETROSPECTIVE in orderings else Ordering.PROSPECTIVE,
        applicability=_applicability([o for o, *_ in graded], e),
        comparison=comparison,
        used=tuple((o.observation_id, o.revision) for o, *_ in graded),
        excluded=tuple(excluded),
        allowed_by_model=comparison is Comparison.CONFIRMED_MISS and all(a for *_, a in graded),
        missing="",
    )


def _response(findings: tuple[Finding, ...]) -> Response:
    seen = {f.comparison for f in findings}
    if Comparison.CONFLICT in seen:
        return Response.RESOLVE_CONFLICT
    if Comparison.CONFIRMED_MISS in seen:
        return Response.OPEN_REVIEW
    if Comparison.UNRESOLVED in seen:
        return Response.OBSERVE
    return Response.CONTINUE
