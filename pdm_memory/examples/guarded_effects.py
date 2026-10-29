"""Run a simulated guarded effect and application-state recovery.

Run: python -m pdm_memory.examples.guarded_effects

The owner owns the controller, current evidence, registered functions, and
ledger. The simulated worker supplies only requests. A real deployment must
enforce that separation outside this Python object; this example does not
connect to equipment or capture another process's memory.
"""

from pathlib import Path
from tempfile import TemporaryDirectory

from pdm_memory.guarded_effects import (
    CheckResult,
    ConditionVerdict,
    EffectRequest,
    GuardedEffects,
    Opening,
)


def main() -> None:
    machine = {"running": False, "measurement_revision": 1}
    owner_evidence = {"approved": False, "revision": 1}

    def validate(request: EffectRequest) -> bool:
        return (
            set(request.parameters) == {"running"} and type(request.parameters["running"]) is bool
        )

    def current_condition(request: EffectRequest) -> ConditionVerdict:
        return ConditionVerdict(
            CheckResult.SATISFIED if owner_evidence["approved"] else CheckResult.UNKNOWN,
            "current owner decision",
            {"source": "simulated owner decision", "revision": owner_evidence["revision"]},
        )

    def set_simulated_machine(request: EffectRequest) -> dict:
        machine["running"] = request.parameters["running"]
        machine["measurement_revision"] += 1
        return {"command_received": True}

    def request(
        action_id: str, *, branch: str = "attempt-1", target: str = "sim:pump"
    ) -> EffectRequest:
        return EffectRequest(
            action_id, branch, "worker-1", "owner-policy-1", "set", target, {"running": True}
        )

    with TemporaryDirectory() as folder:
        with GuardedEffects(
            Path(folder) / "owner-ledger.db",
            owner="example-owner",
            program_version="worker-1",
            policy_version="owner-policy-1",
            openings=[
                Opening(
                    "set",
                    "sim:pump",
                    set_simulated_machine,
                    validate,
                    current_condition,
                    max_dispatches=1,
                )
            ],
        ) as guard:
            guard.start_branch("attempt-1")
            guard.checkpoint(
                "before-work", branch_id="attempt-1", state={"next_step": "start pump"}
            )
            print("Stage:", guard.stage(request("start-1")).status)
            print("No current approval:", guard.release("start-1").status)

            # A new verdict opens the boundary. No fixed waiting period grants it.
            owner_evidence.update(approved=True, revision=2)
            print("Handler returned:", guard.release("start-1").status)
            print(
                "Observed outcome:",
                guard.reconcile(
                    "start-1",
                    executed=True,
                    evidence={"source": "simulated receiver measurement", **machine},
                ).status,
            )

            guard.stage(request("unfinished-plan"))
            print(
                "Outside the opening:", guard.stage(request("outside", target="sim:other")).status
            )

            recovered = guard.recover("before-work", new_branch_id="attempt-2")
            print("Recovered application state:", recovered.state)
            print("Surviving action records:", {r.action_id: r.status for r in recovered.receipts})
            print("Machine remains running:", machine["running"])
            print("Old branch:", guard.stage(request("old-branch")).status)

            guard.stage(request("new-plan", branch="attempt-2"))
            print("Lifetime limit survives recovery:", guard.release("new-plan").status)
            print("Retained history consistent:", guard.verify_history())


if __name__ == "__main__":
    main()
