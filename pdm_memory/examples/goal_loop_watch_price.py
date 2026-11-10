"""Watch a price and text the owner once when it crosses the line.

Run: python -m pdm_memory.examples.goal_loop_watch_price

"Text me if MU crosses forty from below. Don't trade. Don't tell me twice while
it stays above. Stop watching after September."

The example writes the job as a Goal Contract, lets the owner approve it, feeds
the controller seven readings (six of them above the line) and shows that one
text went out, that the job proved it with a delivery receipt, and that the
receipts form an intact chain. The SMS carrier is simulated; a real deployment
supplies its own executor and checker. No PDM store, account or network is used.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from pathlib import Path
from tempfile import TemporaryDirectory
from typing import Any

from pdm_memory.goal_loop import (
    ActionResult,
    Autonomy,
    Budget,
    CallableChecker,
    ContractHistory,
    EndConditions,
    GoalContract,
    GoalLoopController,
    Observation,
    OwnerApproval,
    PromiseChangeError,
    Proof,
    ProofReceipt,
    ProofVerdict,
    SqliteJobStore,
    StepTemplate,
    Trigger,
)


def run_example(directory: Path | None = None) -> dict[str, Any]:
    start = datetime(2026, 9, 1, 13, 30, tzinfo=timezone.utc)
    clock = {"now": start}
    carrier: dict[str, str] = {}  # message id -> delivery status, as a carrier would report

    def send_sms(step, *, idempotency_key):
        message_id = f"msg-{len(carrier) + 1}"
        carrier[message_id] = "delivered"
        print(f"  SMS to {step.target}: {step.summary} ({idempotency_key})")
        return ActionResult(reference=message_id)

    def delivery_receipt(contract, step, result, evidence, now):
        status = carrier.get(result.reference) if result is not None else None
        if status == "delivered":
            receipt = ProofReceipt(
                "sms_delivery_receipt", result.reference, now, {"status": status}
            )
            return ProofVerdict("PROVEN", "the carrier reports delivery", receipt)
        return ProofVerdict("NOT_YET", "no delivery receipt yet")

    contract = GoalContract(
        contract_id="watch-mu",
        owner="eric",
        outcome="Eric gets one text when MU crosses $40 from below",
        proof=Proof("sms_delivery_receipt", "carrier delivery receipt for the text"),
        never=["Never trade MU shares"],
        sacrifice_order=["wording of the text", "a minute of delay"],
        trigger=Trigger(
            kind="threshold",
            metric="MU.price",
            direction="rising",
            fire_at="40.00",
            reset_at="39.50",
        ),
        autonomy=Autonomy(allowed=["sms.send"]),
        budget=Budget(deadline=datetime(2026, 9, 30, 23, 59, 59, tzinfo=timezone.utc)),
        end=EndConditions(on_success="rearm"),
        created_at=start,
    )

    with TemporaryDirectory() as tmp:
        path = (directory or Path(tmp)) / "goal_loop.db"
        store = SqliteJobStore(path)
        controller = GoalLoopController(
            store=store,
            checkers={
                "sms_delivery_receipt": CallableChecker("sms_delivery_receipt", delivery_receipt)
            },
            executors={"sms.send": send_sms},
            clock=lambda: clock["now"],
        )
        job_id = controller.create(
            ContractHistory(contract),
            step=StepTemplate(
                "sms.send", "+15550100", "Send SMS to owner: {metric} crossed {fire_at}"
            ),
        )
        controller.approve(job_id, OwnerApproval("eric", "this is what I asked for"))
        print("armed:", controller.arm(job_id).state.value)

        for number, price in enumerate(
            ["39.00", "40.10", "40.30", "40.20", "40.50", "40.40", "40.60"]
        ):
            clock["now"] += timedelta(minutes=1)
            job = controller.observe(
                job_id,
                Observation(
                    observation_id=f"tick-{number}",
                    observed_at=clock["now"],
                    metric="MU.price",
                    value=price,
                ),
            )
            print(f"  MU {price}: job is {job.state.value}")

        try:
            controller.revise_contract(job_id, outcome="Eric gets a text at every tick")
            refused = False
        except PromiseChangeError as exc:
            refused = True
            print("promise change without the owner:", exc)

        receipts = controller.receipts(job_id)
        result = {
            "texts_sent": len(carrier),
            "state": controller.job(job_id).state.value,
            "receipts": len(receipts),
            "chain_intact": controller.verify_receipts(job_id),
            "promise_change_refused": refused,
            "proof": next((r.proof for r in receipts if r.proof is not None), None),
        }
        store.close()
    print(
        f"texts sent: {result['texts_sent']}; receipts: {result['receipts']}; "
        f"chain intact: {result['chain_intact']}"
    )
    return result


def main() -> None:
    run_example()


if __name__ == "__main__":
    main()
