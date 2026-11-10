"""Goal Loop: a job described once and carried to the end (preview).

This release ships the Goal Contract, the one record every part of the loop
reads. The controller that moves a job through its states comes next.

    from pdm_memory.goal_loop import ContractHistory, GoalContract, OwnerApproval, Trigger

    contract = GoalContract(
        contract_id="watch-mu",
        owner="eric",
        outcome="Eric gets a text when MU crosses $40 from below",
        proof="sms_delivery_receipt",
        never=["Never trade MU shares"],
        trigger=Trigger(kind="threshold", metric="MU.price", direction="rising",
                        fire_at="40.00", reset_at="39.50"),
    )
    history = ContractHistory(contract)
    history.revise(approval=OwnerApproval("eric", "raise the line"), outcome="...")

No PDM store, account or network is needed.
"""

from pdm_memory.goal_loop.contract import (
    PROMISE_FIELDS,
    Autonomy,
    Budget,
    ContractHistory,
    EndConditions,
    GoalContract,
    NotArmableError,
    Notify,
    OwnerApproval,
    PromiseChangeError,
    Proof,
    Trigger,
    ensure_armable,
)

__all__ = [
    "PROMISE_FIELDS",
    "Autonomy",
    "Budget",
    "ContractHistory",
    "EndConditions",
    "GoalContract",
    "NotArmableError",
    "Notify",
    "OwnerApproval",
    "PromiseChangeError",
    "Proof",
    "Trigger",
    "ensure_armable",
]
