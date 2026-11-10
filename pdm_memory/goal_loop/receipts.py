"""Step receipts: one per transition, chained by hash.

Every receipt carries the hash of the one before it, so a removed, reordered or
edited receipt breaks the chain. That is tamper-evidence, not protection from
someone who can rewrite the whole store: the same limit as the Boundary Lock
ledger. The controller computes the hashes, so the chain is the same whatever
``JobStore`` holds it.
"""

from __future__ import annotations

import hashlib
from collections.abc import Sequence
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any

from pdm_memory.goal_loop import _json
from pdm_memory.goal_loop.checker import ProofReceipt
from pdm_memory.goal_loop.states import JobState
from pdm_memory.guarded_effects import LedgerIntegrityError

__all__ = ["StepReceipt", "verify_chain"]


def _hash(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


@dataclass(frozen=True, kw_only=True)
class StepReceipt:
    """What happened, to which job, under which contract version, and why.

    ``to_state`` is ``None`` for a receipt that records something without moving
    the job (a rejected proof, a crossing that arrived while the job was busy).
    """

    job_id: str
    seq: int
    at: datetime
    kind: str
    from_state: JobState | None
    to_state: JobState | None
    contract_version: int
    action_id: str | None
    reason: str
    guard: dict[str, Any] | None = None
    proof: ProofReceipt | None = None
    detail: dict[str, Any] = field(default_factory=dict)
    prev_hash: str = ""
    receipt_hash: str = ""

    def body(self) -> dict[str, Any]:
        return {
            "job_id": self.job_id,
            "seq": self.seq,
            "at": _json.format_datetime(self.at),
            "kind": self.kind,
            "from_state": None if self.from_state is None else self.from_state.value,
            "to_state": None if self.to_state is None else self.to_state.value,
            "contract_version": self.contract_version,
            "action_id": self.action_id,
            "reason": self.reason,
            "guard": self.guard,
            "proof": None if self.proof is None else self.proof.to_dict(),
            "detail": self.detail,
            "prev_hash": self.prev_hash,
        }

    def compute_hash(self) -> str:
        return _hash(_json.dumps(self.body()))

    @classmethod
    def build(cls, **fields: Any) -> StepReceipt:
        draft = cls(**fields)
        return cls(**{**fields, "receipt_hash": draft.compute_hash()})

    def to_dict(self) -> dict[str, Any]:
        return {**self.body(), "receipt_hash": self.receipt_hash}

    @classmethod
    def from_dict(cls, data: Any) -> StepReceipt:
        guard, proof = data.get("guard"), data.get("proof")
        return cls(
            job_id=data["job_id"],
            seq=data["seq"],
            at=_json.parse_datetime(data["at"], "at"),
            kind=data["kind"],
            from_state=None if data["from_state"] is None else JobState(data["from_state"]),
            to_state=None if data["to_state"] is None else JobState(data["to_state"]),
            contract_version=data["contract_version"],
            action_id=data.get("action_id"),
            reason=data["reason"],
            guard=guard,
            proof=None if proof is None else ProofReceipt.from_dict(proof),
            detail=data.get("detail", {}),
            prev_hash=data["prev_hash"],
            receipt_hash=data["receipt_hash"],
        )


def verify_chain(receipts: Sequence[StepReceipt], *, expected_seq: int, expected_head: str) -> bool:
    """Check seq, links and hashes, and that the chain ends where the job says it does."""
    prev = ""
    for number, receipt in enumerate(receipts, start=1):
        if receipt.seq != number:
            raise LedgerIntegrityError(f"receipt {number} is missing or out of order")
        if receipt.prev_hash != prev:
            raise LedgerIntegrityError(f"receipt {number} does not follow the one before it")
        if receipt.receipt_hash != receipt.compute_hash():
            raise LedgerIntegrityError(f"receipt {number} does not match its hash")
        prev = receipt.receipt_hash
    if len(receipts) != expected_seq or prev != expected_head:
        raise LedgerIntegrityError("the receipt chain does not end where the job says it does")
    return True
