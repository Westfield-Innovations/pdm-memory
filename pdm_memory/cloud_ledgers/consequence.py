"""A ConsequenceLedger whose ledger lives on a host; the host's clock stamps time."""

from __future__ import annotations

import typing
from datetime import datetime
from typing import Any

from pdm_memory._ledger.chain import verify_rows
from pdm_memory._ledger.errors import LedgerIntegrityError
from pdm_memory.cloud_ledgers.client import CloudLedgers
from pdm_memory.cloud_ledgers.wire import decode, encode
from pdm_memory.consequence.ledger import ConsequenceLedger
from pdm_memory.consequence.records import (
    AccountSummary,
    Admission,
    Application,
    CauseHypothesis,
    ConsequenceEntry,
    Evaluation,
    EvidenceKind,
    Expectation,
    ForecastRow,
    LearningProposal,
    Observation,
    ObservationProposal,
    OwnerPolicy,
    Route,
    Significance,
    TaskRef,
    UpdateState,
)
from pdm_memory.goal_loop._json import require_aware, utc_now
from pdm_memory.guarded_effects import (
    ActionReceipt,
    ConditionVerdict,
    EffectRequest,
    _name,
)

__all__ = ["CloudConsequenceLedger"]

_PAGE = 1000


class CloudConsequenceLedger(ConsequenceLedger):
    """The host keeps the ledger; a custom ``clock`` is sent as ``now`` and the host must trust it."""

    def __init__(
        self,
        cloud: CloudLedgers,
        *,
        owner: str,
        policy: OwnerPolicy,
        clock: typing.Callable[[], datetime] = utc_now,
    ) -> None:
        if not isinstance(policy, OwnerPolicy):
            raise TypeError("policy must be an OwnerPolicy")
        self.owner = _name(owner, "owner")
        self.policy = policy
        self._clock = clock
        self._custom_clock = clock is not utc_now
        self._cloud = cloud
        self._verified: tuple[int, str] | None = None
        self._open: dict[str, Any] = {
            "owner": self.owner,
            "policy": encode(policy),
            "custom_clock": self._custom_clock,
        }
        cloud.call("consequence", "open", self._open, {}, now=self._stamp())

    def _stamp(self) -> datetime | None:
        return require_aware(self._clock(), "clock()") if self._custom_clock else None

    def _call(self, op: str, **args: Any) -> Any:
        body = self._cloud.call(
            "consequence", op, self._open, encode(args), now=self._stamp()
        )
        hint = typing.get_type_hints(getattr(ConsequenceLedger, op))["return"]
        return decode(hint, body["result"])

    def close(self) -> None:
        """Nothing to release here: the CloudLedgers connection belongs to the caller."""

    def prepare(self, expectation: Expectation) -> str:
        return self._call("prepare", expectation=expectation)

    def expectation(self, expectation_id: str) -> tuple[Expectation, datetime]:
        return self._call("expectation", expectation_id=expectation_id)

    def prepared_check(self, request: EffectRequest) -> ConditionVerdict:
        return self._call("prepared_check", request=request)

    def attach_action(self, expectation_id: str, receipt: ActionReceipt) -> None:
        self._call("attach_action", expectation_id=expectation_id, receipt=receipt)

    def actions(self, expectation_id: str) -> list[dict[str, Any]]:
        return self._call("actions", expectation_id=expectation_id)

    def observe(self, observation: Observation) -> tuple[str, int]:
        return self._call("observe", observation=observation)

    def observations(self, observable: str | None = None) -> list[Observation]:
        return self._call("observations", observable=observable)

    def evaluate(
        self,
        expectation_id: str,
        *,
        task: TaskRef,
        causes: tuple[CauseHypothesis, ...] | None = None,
    ) -> Evaluation:
        return self._call(
            "evaluate", expectation_id=expectation_id, task=task, causes=causes
        )

    def current_evaluation(self, expectation_id: str) -> Evaluation | None:
        return self._call("current_evaluation", expectation_id=expectation_id)

    def forecast_record(
        self, predictor: str, *, version: str | None = None
    ) -> list[ForecastRow]:
        return self._call("forecast_record", predictor=predictor, version=version)

    def evaluation(self, evaluation_id: str) -> Evaluation:
        return self._call("evaluation", evaluation_id=evaluation_id)

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
        return self._call(
            "propose_learning",
            evaluation_id=evaluation_id,
            task=task,
            criterion=criterion,
            target_kind=target_kind,
            target_id=target_id,
            target_version=target_version,
            change=change,
            scope=scope,
            confidence=confidence,
            route=route,
            evidence_kind=evidence_kind,
            interface_id=interface_id,
        )

    def update_state(self, update_id: str) -> UpdateState:
        return self._call("update_state", update_id=update_id)

    def record_update_receipt(
        self,
        update_id: str,
        *,
        admission: Admission,
        application: Application | None = None,
        controller_ref: str,
    ) -> UpdateState:
        return self._call(
            "record_update_receipt",
            update_id=update_id,
            admission=admission,
            application=application,
            controller_ref=controller_ref,
        )

    def record_consequence(self, entry: ConsequenceEntry) -> str:
        return self._call("record_consequence", entry=entry)

    def accounts(self, expectation_id: str) -> AccountSummary:
        return self._call("accounts", expectation_id=expectation_id)

    def significance(self, expectation_id: str) -> Significance:
        return self._call("significance", expectation_id=expectation_id)

    def suggest_observation(
        self, expectation_id: str, *, now: datetime | None = None
    ) -> tuple[ObservationProposal, ...]:
        return self._call("suggest_observation", expectation_id=expectation_id, now=now)

    def revoke_expectation(
        self, expectation_id: str, *, controller_ref: str, reason: str
    ) -> None:
        self._call(
            "revoke_expectation",
            expectation_id=expectation_id,
            controller_ref=controller_ref,
            reason=reason,
        )

    def revoke_update(
        self, update_id: str, *, controller_ref: str, reason: str
    ) -> UpdateState:
        return self._call(
            "revoke_update",
            update_id=update_id,
            controller_ref=controller_ref,
            reason=reason,
        )

    def revocations(self, expectation_id: str | None = None) -> list[dict[str, Any]]:
        return self._call("revocations", expectation_id=expectation_id)

    def history(
        self,
        expectation_id: str | None = None,
        *,
        after_sequence: int = 0,
        limit: int = 100,
    ) -> list[dict[str, Any]]:
        return self._call(
            "history",
            expectation_id=expectation_id,
            after_sequence=after_sequence,
            limit=limit,
        )

    def verify_history(self) -> bool:
        """Check the served chain; it must also extend the one verified last time."""
        previous, after, seen = "", 0, None
        mark = self._verified
        while True:
            body = self._cloud.call(
                "consequence",
                "history_raw",
                self._open,
                {"after_sequence": after, "limit": _PAGE},
                now=self._stamp(),
            )
            rows = decode(list[list[Any]], body["result"])
            previous = verify_rows(rows, self.owner, "consequence", previous=previous)
            for row in rows:
                if mark is not None and row[0] == mark[0]:
                    seen = row[6]
            if rows:
                after = rows[-1][0]
            if len(rows) < _PAGE:
                break
        if mark is not None and seen != mark[1]:
            raise LedgerIntegrityError(
                "retained consequence history is shorter than, or differs from, the chain verified before"
            )
        if after:
            self._verified = (after, previous)
        return True
