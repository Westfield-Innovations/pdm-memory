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
from decimal import Decimal
from pathlib import Path
from typing import Any

from pdm_memory.consequence.compare import COMPARATORS, NOT_OBSERVED
from pdm_memory.consequence.records import (
    PROTECTED_TARGETS,
    Account,
    AccountSummary,
    Admission,
    Applicability,
    Application,
    CauseHypothesis,
    Comparison,
    ConsequenceEntry,
    Criterion,
    Evaluation,
    EvidenceKind,
    Expectation,
    Finding,
    ForecastRow,
    Horizon,
    LearningProposal,
    NotSupported,
    Observation,
    ObservationProposal,
    Ordering,
    OwnerPolicy,
    QuantityStatus,
    ReconcileFirst,
    Response,
    Route,
    TaskRef,
    UnauthorizedChange,
    UpdateState,
)
from pdm_memory.goal_loop._json import dumps, parse_datetime, require_aware, utc_now
from pdm_memory.guarded_effects import (
    ActionReceipt,
    CheckResult,
    ConditionVerdict,
    EffectRequest,
    IdReuseError,
    LedgerIntegrityError,
    ResourceLimitError,
    _hash,
    _name,
)

__all__ = ["ConsequenceLedger"]

_COUNT = {
    Comparison.CONFIRMED_MATCH: "match",
    Comparison.CONFIRMED_MISS: "miss",
    Comparison.UNRESOLVED: "unresolved",
    Comparison.CONFLICT: "conflict",
}
_GRADED = (Comparison.CONFIRMED_MATCH, Comparison.CONFIRMED_MISS)
_OPEN_OR_APPLIED = {UpdateState.PROPOSED.value, UpdateState.DEFERRED.value, UpdateState.APPLIED.value}
_NEXT = {
    UpdateState.PROPOSED: {UpdateState.DEFERRED, UpdateState.REJECTED, UpdateState.UNCERTAIN,
                           UpdateState.APPLIED, UpdateState.NOT_APPLIED},
    UpdateState.DEFERRED: {UpdateState.REJECTED, UpdateState.UNCERTAIN, UpdateState.APPLIED,
                           UpdateState.NOT_APPLIED},
    UpdateState.UNCERTAIN: {UpdateState.APPLIED, UpdateState.NOT_APPLIED},
    UpdateState.REJECTED: set(),
    UpdateState.APPLIED: set(),
    UpdateState.NOT_APPLIED: set(),
}

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
CREATE TABLE IF NOT EXISTS cl_updates (
  owner TEXT NOT NULL, update_id TEXT NOT NULL, expectation_id TEXT NOT NULL,
  criterion TEXT NOT NULL, target_kind TEXT NOT NULL, target_id TEXT NOT NULL,
  record_json TEXT NOT NULL, state TEXT NOT NULL,
  PRIMARY KEY(owner, update_id));
CREATE TABLE IF NOT EXISTS cl_consequences (
  owner TEXT NOT NULL, entry_id TEXT NOT NULL, expectation_id TEXT NOT NULL,
  record_json TEXT NOT NULL, PRIMARY KEY(owner, entry_id));
CREATE TABLE IF NOT EXISTS cl_proposals (
  owner TEXT NOT NULL, proposal_id TEXT NOT NULL, expectation_id TEXT NOT NULL,
  criterion TEXT NOT NULL, status TEXT NOT NULL, record_json TEXT NOT NULL,
  PRIMARY KEY(owner, proposal_id));
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
        rules = list(expectation.criteria)
        if expectation.horizon.event is not None:
            rules.append(expectation.horizon.event)
        for c in rules:
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
        causes: tuple[CauseHypothesis, ...] | None = None,
    ) -> Evaluation:
        """Grade each criterion against admissible evidence, for these exact revisions.

        The same evidence, comparators and policy return the same evaluation;
        restating a cause without new evidence is not new evidence. Without
        *causes* the current evaluation's hypotheses carry forward; ``()`` drops them.
        """
        expectation, committed_at = self.expectation(expectation_id)
        if task != expectation.task:
            raise ValueError("an evaluation is bound to the expectation's own task")
        if causes is not None:
            causes = tuple(causes)
            if not all(isinstance(h, CauseHypothesis) for h in causes):
                raise TypeError("causes must be CauseHypothesis records")
        # Evidence is read under the write lock, so a slower caller can never
        # supersede a revision built from newer readings.
        with self._transaction():
            return self._evaluate(expectation, committed_at, causes)

    def _evaluate(
        self,
        expectation: Expectation,
        committed_at: datetime,
        causes: tuple[CauseHypothesis, ...] | None,
    ) -> Evaluation:
        expectation_id = expectation.expectation_id
        if causes is None:
            current = self.current_evaluation(expectation_id)
            causes = current.causes if current is not None else ()
        observables = {c.observable for c in expectation.criteria}
        latest = self.observations()
        by_id = {o.observation_id: o for o in latest}
        window = _window(expectation, latest, self.policy)
        relevant = [
            o
            for o in latest
            if o.observable in observables and window is not None and _in_window(o, window)
        ]
        cited = {ref for h in causes for ref in h.support if ref in by_id}
        inputs = sorted(
            {(o.observation_id, o.revision) for o in relevant}
            | {(o.observation_id, o.revision) for o in _event_readings(expectation, latest)}
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
        if revision > self.policy.limits.max_revisions:
            raise ResourceLimitError(
                f"{expectation_id} reached {self.policy.limits.max_revisions} evaluation"
                f" revisions; escalate: {self.policy.escalation}"
            )
        findings = tuple(
            _finding(expectation, committed_at, c, relevant, self.policy, window)
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
                h.support
                and all(
                    r in by_id and _admissible_support(by_id[r], expectation, self.policy)
                    for r in h.support
                )
                for h in causes
            ),
            response=_response(findings),
        )
        record = evaluation.to_dict()
        previous = self._conn.execute(
            "SELECT evaluation_id FROM cl_evaluations"
            " WHERE owner=? AND expectation_id=? AND superseded_by IS NULL",
            (self.owner, expectation_id),
        ).fetchone()
        self._conn.execute(
            "INSERT INTO cl_evaluations VALUES (?, ?, ?, ?, ?, NULL)",
            (self.owner, evaluation_id, expectation_id, revision, dumps(record)),
        )
        self._append("EVALUATED", {"evaluation": record}, expectation_id)
        for f in findings:
            if f.comparison is not Comparison.UNRESOLVED:
                self._close_proposals(expectation_id, f.criterion, evaluation_id)
        if previous is not None:
            # Marked, never deleted: the earlier finding stays readable.
            self._conn.execute(
                "UPDATE cl_evaluations SET superseded_by=? WHERE owner=? AND evaluation_id=?",
                (evaluation_id, self.owner, previous[0]),
            )
            self._append(
                "EVALUATION_SUPERSEDED",
                {"superseded": previous[0], "by": evaluation_id},
                expectation_id,
            )
        return evaluation

    def current_evaluation(self, expectation_id: str) -> Evaluation | None:
        with self._lock:
            row = self._conn.execute(
                "SELECT * FROM cl_evaluations"
                " WHERE owner=? AND expectation_id=? AND superseded_by IS NULL",
                (self.owner, expectation_id),
            ).fetchone()
        return None if row is None else self._evaluation_from(row)

    def forecast_record(self, predictor: str, *, version: str | None = None) -> list[ForecastRow]:
        """Confirmation and error counts of current evaluations, per comparable case.

        Retrospective findings are counted apart: they grade, but they were not forecasts.
        """
        rows: dict[tuple[Any, ...], dict[str, Any]] = {}
        with self._lock:
            current = self._conn.execute(
                "SELECT e.record_json, v.record_json AS evaluation_json FROM cl_expectations e"
                " JOIN cl_evaluations v ON v.owner=e.owner AND v.expectation_id=e.expectation_id"
                " WHERE e.owner=? AND v.superseded_by IS NULL ORDER BY v.rowid",
                (self.owner,),
            ).fetchall()
            kinds = {
                (r["observation_id"], r["revision"]): json.loads(r["record_json"])["kind"]
                for r in self._conn.execute(
                    "SELECT observation_id, revision, record_json FROM cl_observations WHERE owner=?",
                    (self.owner,),
                )
            }
        for row in current:
            expectation = Expectation.from_dict(json.loads(row["record_json"]))
            if expectation.predictor != predictor or version not in (
                None,
                expectation.predictor_version,
            ):
                continue
            evaluation = Evaluation.from_dict(json.loads(row["evaluation_json"]))
            h = expectation.horizon
            for f in evaluation.findings:
                c = expectation.criterion(f.criterion)
                used_kinds = sorted({kinds[u] for u in f.used})
                key = (
                    expectation.predictor_version,
                    c.observable,
                    _hash(dumps(expectation.to_dict()["conditions"])),
                    EvidenceKind(used_kinds[0]) if len(used_kinds) == 1 else None,
                    _horizon_s(h),
                    dumps(c.expected),
                )
                counts = rows.setdefault(
                    key,
                    {"n": 0, "match": 0, "miss": 0, "unresolved": 0, "conflict": 0,
                     "retrospective": 0, "probabilities": []},
                )
                if f.ordering is Ordering.RETROSPECTIVE:
                    counts["retrospective"] += 1
                    continue
                counts["n"] += 1
                counts[_COUNT[f.comparison]] += 1
                if c.comparator[0] == "probabilistic" and f.comparison in _GRADED:
                    counts["probabilities"].append(float(c.expected["probability"]))
        return [
            ForecastRow(
                predictor=predictor,
                predictor_version=k[0],
                observable=k[1],
                conditions_key=k[2],
                evidence_kind=k[3],
                horizon_s=k[4],
                tolerance=k[5],
                n=v["n"],
                match=v["match"],
                miss=v["miss"],
                unresolved=v["unresolved"],
                conflict=v["conflict"],
                retrospective=v["retrospective"],
                mean_probability=(
                    sum(v["probabilities"]) / len(v["probabilities"]) if v["probabilities"] else None
                ),
            )
            for k, v in rows.items()
        ]

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

    # --- learning ------------------------------------------------------------

    def propose_learning(
        self,
        evaluation_id: str,
        *,
        task: TaskRef,
        criterion: str,
        target_kind: str,
        target_id: str,
        target_version: str,
        change: dict[str, Any],
        scope: str,
        confidence: float,
        route: Route,
        evidence_kind: EvidenceKind,
        interface_id: str | None = None,
    ) -> LearningProposal:
        """Name one target and a bounded change the evaluation supports.

        Nothing is applied here: the owner's update path admits or rejects the
        proposal and its receipts come back through ``record_update_receipt``.
        """
        _name(target_kind, "target_kind")
        if target_kind in PROTECTED_TARGETS or target_kind not in self.policy.learning_targets:
            raise UnauthorizedChange(f"{target_kind} is not an owner-admitted learning target")
        _name(target_id, "target_id")
        _name(target_version, "target_version")
        _name(scope, "scope")
        route, evidence_kind = Route(route), EvidenceKind(evidence_kind)
        if type(confidence) not in (int, float) or not 0 <= confidence <= 1:
            raise ValueError("confidence must be between 0 and 1")
        if not isinstance(change, dict) or not change:
            raise ValueError("a proposal names its change")
        dumps(change, limit=self._limit)
        evaluation = self.evaluation(evaluation_id)
        expectation, _ = self.expectation(evaluation.expectation_id)
        if task != expectation.task:
            raise ValueError("a proposal is bound to the expectation's own task")
        if evaluation.superseded_by is not None:
            raise NotSupported("this evaluation was superseded; propose from the current one")
        finding = evaluation.finding(criterion)
        if finding.applicability is not Applicability.APPLICABLE:
            raise NotSupported(f"the finding is {finding.applicability.value}, not applicable")
        if finding.comparison not in _GRADED:
            raise NotSupported(f"the finding is {finding.comparison.value}")
        if finding.comparison is Comparison.CONFIRMED_MISS and not evaluation.attribution_supported:
            raise NotSupported("attribution unresolved; the miss is retained")
        if target_kind == "interface_calibration" and (
            route is not Route.OWN_ACTION
            or interface_id is None
            or interface_id != expectation.interface_id
        ):
            raise NotSupported("only this interface's own action can calibrate this interface")
        update_id = _hash(
            dumps(
                [expectation.expectation_id, criterion, target_kind, target_id, target_version, change]
            )
        )
        with self._transaction():
            old = self._conn.execute(
                "SELECT record_json FROM cl_updates WHERE owner=? AND update_id=?",
                (self.owner, update_id),
            ).fetchone()
            if old is not None:
                return LearningProposal.from_dict(json.loads(old[0]))
            same_target = (self.owner, expectation.expectation_id, criterion, target_kind, target_id)
            uncertain = self._conn.execute(
                "SELECT update_id FROM cl_updates WHERE owner=? AND expectation_id=?"
                " AND criterion=? AND target_kind=? AND target_id=? AND state=? LIMIT 1",
                (*same_target, UpdateState.UNCERTAIN.value),
            ).fetchone()
            if uncertain is not None:
                raise ReconcileFirst(f"update {uncertain[0]} has no application receipt")
            prior = self._conn.execute(
                "SELECT update_id, state FROM cl_updates WHERE owner=? AND expectation_id=?"
                " AND criterion=? AND target_kind=? AND target_id=? ORDER BY rowid DESC LIMIT 1",
                same_target,
            ).fetchone()
            reconciles = (
                prior["update_id"]
                if prior is not None and prior["state"] in _OPEN_OR_APPLIED
                else None
            )
            proposal = LearningProposal(
                update_id=update_id,
                expectation_id=expectation.expectation_id,
                evaluation_id=evaluation_id,
                criterion=criterion,
                target_kind=target_kind,
                target_id=target_id,
                target_version=target_version,
                change=change,
                scope=scope,
                confidence=float(confidence),
                route=route,
                evidence_kind=evidence_kind,
                interface_id=interface_id,
                reconciles=reconciles,
            )
            record = proposal.to_dict()
            self._conn.execute(
                "INSERT INTO cl_updates VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                (
                    self.owner,
                    update_id,
                    expectation.expectation_id,
                    criterion,
                    target_kind,
                    target_id,
                    dumps(record),
                    UpdateState.PROPOSED.value,
                ),
            )
            self._append("LEARNING_PROPOSED", {"proposal": record}, expectation.expectation_id)
        return proposal

    def update_state(self, update_id: str) -> UpdateState:
        with self._lock:
            return UpdateState(self._update_row(update_id)["state"])

    def _update_row(self, update_id: str) -> sqlite3.Row:
        row = self._conn.execute(
            "SELECT * FROM cl_updates WHERE owner=? AND update_id=?",
            (self.owner, _name(update_id, "update_id")),
        ).fetchone()
        if row is None:
            raise KeyError(f"no proposal {update_id}")
        return row

    def record_update_receipt(
        self,
        update_id: str,
        *,
        admission: Admission,
        application: Application | None = None,
        controller_ref: str,
    ) -> UpdateState:
        """Append the owner's controller decision. Acceptance alone is UNCERTAIN, not applied."""
        admission = Admission(admission)
        application = None if application is None else Application(application)
        _name(controller_ref, "controller_ref")
        if admission is not Admission.ACCEPTED and application is not None:
            raise ValueError("only an accepted proposal can carry an application receipt")
        if admission is Admission.ACCEPTED:
            new = UpdateState(application.value) if application else UpdateState.UNCERTAIN
        else:
            new = UpdateState(admission.value)
        with self._transaction():
            row = self._update_row(update_id)
            current = UpdateState(row["state"])
            if new is current:
                return current
            if new not in _NEXT[current]:
                raise ValueError(f"a {new.value} receipt cannot follow {current.value}")
            self._conn.execute(
                "UPDATE cl_updates SET state=? WHERE owner=? AND update_id=?",
                (new.value, self.owner, update_id),
            )
            self._append(
                "UPDATE_RECEIPT",
                {
                    "update_id": update_id,
                    "admission": admission.value,
                    "application": None if application is None else application.value,
                    "controller_ref": controller_ref,
                    "state": new.value,
                },
                row["expectation_id"],
            )
        return new

    # --- consequence accounts ------------------------------------------------

    def record_consequence(self, entry: ConsequenceEntry) -> str:
        """Append one account entry for a retained expectation."""
        if not isinstance(entry, ConsequenceEntry):
            raise TypeError("record_consequence takes a ConsequenceEntry")
        self.expectation(entry.expectation_id)
        record = entry.to_dict()
        encoded = dumps(record, limit=self._limit)
        with self._transaction():
            old = self._conn.execute(
                "SELECT record_json FROM cl_consequences WHERE owner=? AND entry_id=?",
                (self.owner, entry.entry_id),
            ).fetchone()
            if old:
                if old[0] != encoded:
                    raise IdReuseError("entry_id already binds a different consequence")
                return entry.entry_id
            self._conn.execute(
                "INSERT INTO cl_consequences VALUES (?, ?, ?, ?)",
                (self.owner, entry.entry_id, entry.expectation_id, encoded),
            )
            self._append("CONSEQUENCE_RECORDED", {"entry": record}, entry.expectation_id)
        return entry.entry_id

    def accounts(self, expectation_id: str) -> AccountSummary:
        """Totals per account, unit and status. Combined only under the owner's rules."""
        with self._lock:
            rows = self._conn.execute(
                "SELECT record_json FROM cl_consequences WHERE owner=? AND expectation_id=?"
                " ORDER BY rowid",
                (self.owner, expectation_id),
            ).fetchall()
        totals: dict[tuple[Account, str, QuantityStatus], Decimal] = {}
        unknown: dict[Account, int] = {}
        violated = False
        units: set[str] = set()
        for row in rows:
            data = json.loads(row[0])
            account, status = Account(data["account"]), QuantityStatus(data["status"])
            violated = violated or data["boundary_violation"]
            if data["quantity"] is None:
                unknown[account] = unknown.get(account, 0) + 1
                continue
            key = (account, data["unit"], status)
            totals[key] = totals.get(key, Decimal(0)) + Decimal(data["quantity"])
            units.add(data["unit"])
        rules = self.policy.exchange_rules
        if not rows:
            return AccountSummary(totals, unknown, None, "no consequences recorded")
        if violated:
            return AccountSummary(totals, unknown, None, "a hard boundary was violated")
        if unknown:
            return AccountSummary(totals, unknown, None, "some quantities are unknown")
        missing = sorted(units - set(rules or {}))
        if rules is None or missing:
            return AccountSummary(totals, unknown, None, f"no owner exchange rule for {missing}")
        combined = sum((q * rules[unit] for (_, unit, _), q in totals.items()), Decimal(0))
        return AccountSummary(totals, unknown, combined, "")

    # --- observation proposals -----------------------------------------------

    def suggest_observation(
        self, expectation_id: str, *, now: datetime | None = None
    ) -> tuple[ObservationProposal, ...]:
        """One proposal per unresolved criterion, coalesced and within owner limits.

        It names sources and a deadline; it never requests an action.
        """
        expectation, _ = self.expectation(expectation_id)
        now = self._now() if now is None else require_aware(now, "now")
        evaluation = self.current_evaluation(expectation_id)
        names = (
            [f.criterion for f in evaluation.findings if f.comparison is Comparison.UNRESOLVED]
            if evaluation is not None
            else [c.name for c in expectation.criteria]
        )
        limits = self.policy.limits
        deadline = expectation.horizon.latest_decision_point
        out = []
        with self._transaction():
            for name in names:
                c = expectation.criterion(name)
                proposal_id = _hash(
                    dumps([expectation_id, name, evaluation.evaluation_id if evaluation else ""])
                )
                old = self._conn.execute(
                    "SELECT record_json, status FROM cl_proposals WHERE owner=? AND proposal_id=?",
                    (self.owner, proposal_id),
                ).fetchone()
                if old is not None and now > deadline:
                    self._close_proposals(expectation_id, name, "too late")
                    out.append(ObservationProposal.from_dict(json.loads(old[0]), "TOO_LATE"))
                    continue
                if old is not None:
                    out.append(ObservationProposal.from_dict(json.loads(old[0]), old[1]))
                    continue
                sources = [s for s in self.policy.sources if c.observable in s.observables]
                ordered = len({(s.cost_unit, s.burden_unit) for s in sources}) == 1
                if ordered:
                    sources.sort(key=lambda s: (s.cost, s.burden, s.latency_s))
                if now > deadline:
                    status = "TOO_LATE"
                elif self._count_proposals(expectation_id, name) >= limits.max_repeats or (
                    self._count_outstanding(now) >= limits.max_outstanding
                ):
                    status = "EXHAUSTED"
                else:
                    status = "OPEN"
                proposal = ObservationProposal(
                    proposal_id=proposal_id,
                    expectation_id=expectation_id,
                    criterion=name,
                    distinguishes=(Comparison.CONFIRMED_MATCH, Comparison.CONFIRMED_MISS),
                    decision_ref=expectation.decision_ref,
                    sources=tuple(sources),
                    ordered=ordered,
                    deadline=deadline,
                    status=status,
                    escalation=self.policy.escalation if status == "EXHAUSTED" else "",
                )
                if status == "TOO_LATE":
                    out.append(proposal)
                    continue
                # A newer evaluation replaces the request made from an older one.
                self._close_proposals(expectation_id, name, "replaced")
                record = proposal.to_dict()
                del record["status"]
                self._conn.execute(
                    "INSERT INTO cl_proposals VALUES (?, ?, ?, ?, ?, ?)",
                    (self.owner, proposal_id, expectation_id, name, status, dumps(record)),
                )
                self._append(
                    "OBSERVATION_PROPOSED", {"proposal": proposal.to_dict()}, expectation_id
                )
                out.append(proposal)
        return tuple(out)

    def _count_proposals(self, expectation_id: str, criterion: str) -> int:
        return self._conn.execute(
            "SELECT COUNT(*) FROM cl_proposals WHERE owner=? AND expectation_id=? AND criterion=?",
            (self.owner, expectation_id, criterion),
        ).fetchone()[0]

    def _count_outstanding(self, now: datetime) -> int:
        """Open requests that can still help; one past its deadline holds no budget."""
        rows = self._conn.execute(
            "SELECT record_json FROM cl_proposals WHERE owner=? AND status='OPEN'", (self.owner,)
        ).fetchall()
        return sum(
            parse_datetime(json.loads(r[0])["deadline"], "deadline") >= now for r in rows
        )

    def _close_proposals(self, expectation_id: str, criterion: str, reason: str) -> None:
        open_ids = [
            r[0]
            for r in self._conn.execute(
                "SELECT proposal_id FROM cl_proposals"
                " WHERE owner=? AND expectation_id=? AND criterion=? AND status='OPEN'",
                (self.owner, expectation_id, criterion),
            )
        ]
        for proposal_id in open_ids:
            self._conn.execute(
                "UPDATE cl_proposals SET status='CLOSED' WHERE owner=? AND proposal_id=?",
                (self.owner, proposal_id),
            )
            self._append(
                "OBSERVATION_PROPOSAL_CLOSED",
                {"proposal_id": proposal_id, "by": reason},
                expectation_id,
            )

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


_Window = tuple[datetime, datetime]


def _horizon_s(h: Horizon) -> float:
    if h.event is not None and h.event.grade_for_s is not None:
        return float(h.event.grade_for_s)
    return (h.window_end - h.window_start).total_seconds()


def _inadmissible(
    o: Observation, unit: str, e: Expectation, policy: OwnerPolicy, window: _Window
) -> str:
    """Why *o* cannot grade a rule in *unit*, or "" when it can."""
    source = policy.source(o.source)
    if source is None or o.observable not in source.observables:
        return "source"
    if o.unit != unit:
        return "unit"
    if not o.valid:
        return "invalid"
    if o.kind is EvidenceKind.SIMULATED:
        return "simulated"
    if o.evidence_ref in e.inputs:
        return "own witness"
    if o.value is None:
        covered = o.coverage is not None and o.coverage[0] <= window[0]
        return "" if covered and o.coverage[1] >= window[1] else "coverage"
    return ""


def _admissible_support(o: Observation, e: Expectation, policy: OwnerPolicy) -> bool:
    """A cause's support: an admitted source, valid, measured or reported, not its own input."""
    source = policy.source(o.source)
    return (
        source is not None
        and o.observable in source.observables
        and o.valid
        and o.kind is not EvidenceKind.SIMULATED
        and o.evidence_ref not in e.inputs
    )


def _in_window(o: Observation, window: _Window) -> bool:
    """Readings outside the window are not this expectation's evidence at all."""
    start, end = window
    if o.value is None:
        return o.coverage is not None and o.coverage[0] < end and o.coverage[1] > start
    return start <= o.event_time <= end


def _event_readings(e: Expectation, observations: list[Observation]) -> list[Observation]:
    """Every reading that could open an event horizon; a correction to one is new evidence."""
    h, event = e.horizon, e.horizon.event
    if event is None:
        return []
    return [
        o
        for o in observations
        if o.observable == event.observable and h.window_start <= o.event_time <= h.window_end
    ]


def _window(e: Expectation, observations: list[Observation], policy: OwnerPolicy) -> _Window | None:
    """The span readings are graded over; None while the awaited event is unseen."""
    h, event = e.horizon, e.horizon.event
    if event is None:
        return h.window_start, h.window_end
    compare = COMPARATORS[event.comparator].compare
    times = []
    for o in _event_readings(e, observations):
        # A silence cannot be the event, and the event needs admissible evidence.
        if o.value is None or _inadmissible(o, event.unit, e, policy, (h.window_start, h.window_end)):
            continue
        try:
            met, _ = compare(event.expected, o.value)
        except (TypeError, ValueError):
            continue
        if met:
            times.append(o.event_time)
    if not times:
        return None
    start = min(times)
    if event.grade_for_s is None:
        return start, h.window_end
    return start, min(h.window_end, start + timedelta(seconds=event.grade_for_s))


def _ordering(o: Observation, e: Expectation, committed_at: datetime, window: _Window) -> Ordering:
    recorded = e.source_sequences.get(o.source)
    if recorded is not None and o.source_sequence is not None:
        return Ordering.PROSPECTIVE if o.source_sequence > recorded else Ordering.RETROSPECTIVE
    # A silence is graded over the window it covers, so the window's opening times it.
    t = window[0] if o.value is None else o.event_time
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
    window: _Window | None,
) -> Finding:
    if window is None:
        return Finding(c.name, None, Applicability.UNRESOLVED, Comparison.UNRESOLVED, (), (),
                       False, "event not observed")
    compare = COMPARATORS[c.comparator].compare
    excluded: list[tuple[str, str]] = []
    graded: list[tuple[Observation, Ordering, bool, bool]] = []
    for o in observations:
        if o.observable != c.observable:
            continue
        reason = _inadmissible(o, c.unit, e, policy, window)
        if reason:
            excluded.append((o.observation_id, reason))
            continue
        ordering = _ordering(o, e, committed_at, window)
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
