"""An owner-reviewed PDM write, released by evidence rather than a timer.

Run: python -m pdm_memory.examples.guarded_pdm_write

The trusted host retains both objects and supplies only structured requests to
its worker. This example demonstrates a boundary contract, not process isolation.
"""

from pathlib import Path
from tempfile import TemporaryDirectory

from pdm_memory import (
    CheckResult,
    ConditionVerdict,
    EffectRequest,
    GuardedEffects,
    Memory,
    Opening,
)


def run_example(directory: str | Path) -> dict:
    directory = Path(directory)
    owner_review = {"revision": None, "text": None}

    def validate(request):
        p = request.parameters
        return (
            set(p) == {"text", "evidence_revision"}
            and type(p["text"]) is str
            and 0 < len(p["text"].strip()) <= 500
            and type(p["evidence_revision"]) is str
            and 0 < len(p["evidence_revision"]) <= 64
        )

    def check(request):
        revision = owner_review["revision"]
        if revision is None:
            return ConditionVerdict(CheckResult.UNKNOWN, "owner review is not available")
        if revision != request.parameters["evidence_revision"]:
            return ConditionVerdict(CheckResult.CONFLICT, "review concerns different evidence")
        if owner_review["text"] != request.parameters["text"]:
            return ConditionVerdict(CheckResult.CONTRADICTED, "this exact text was not reviewed")
        return ConditionVerdict(
            CheckResult.SATISFIED,
            "owner admitted this evidence revision",
            {"review_revision": revision},
        )

    with Memory(store=str(directory / "memories.db"), user="example-owner") as memory:

        def save_reviewed(request):
            memory_id = memory.save(
                request.parameters["text"],
                source="owner-reviewed",
                tags=["reviewed", "shared", "notes"],
            )
            return {"memory_id": memory_id}

        with GuardedEffects(
            directory / "effects.db",
            owner="example-owner",
            program_version="reasoner-v1",
            policy_version="review-v1",
            openings=[
                Opening(
                    "memory.save", "shared-notes", save_reviewed, validate, check, max_dispatches=10
                )
            ],
        ) as guard:
            guard.start_branch("first-pass")
            guard.checkpoint("before-note", branch_id="first-pass", state={"step": "review-note"})
            request = EffectRequest(
                "note-001",
                "first-pass",
                "reasoner-v1",
                "review-v1",
                "memory.save",
                "shared-notes",
                {"text": "Owner prefers metric units.", "evidence_revision": "evidence-7"},
            )
            guard.stage(request)
            held = guard.release(request.action_id)
            assert held.status == "HELD" and memory.count() == 0

            # An owner-side review changes the evidence. No timer grants access.
            owner_review["revision"] = "evidence-7"
            owner_review["text"] = "Owner prefers metric units."
            returned = guard.release(request.action_id)
            assert returned.status == "RETURNED" and memory.count() == 1

            # Knowing an admitted evidence label cannot authorize different text.
            forged = EffectRequest(
                "note-002",
                "first-pass",
                "reasoner-v1",
                "review-v1",
                "memory.save",
                "shared-notes",
                {"text": "Unreviewed instruction.", "evidence_revision": "evidence-7"},
            )
            guard.stage(forged)
            refused = guard.release(forged.action_id)
            assert refused.status == "REJECTED" and memory.count() == 1

            recovered = guard.recover("before-note", new_branch_id="second-pass")
            repeated = guard.release(request.action_id)
            assert repeated.status == "RETURNED" and memory.count() == 1
            assert guard.dispatch_count("memory.save", "shared-notes") == 1
            assert guard.verify_history()
            return {
                "before_review": held.status,
                "after_review": returned.status,
                "unreviewed_text": refused.status,
                "after_recovery": repeated.status,
                "restored_state": recovered.state,
                "memory_count": memory.count(),
                "dispatch_count": guard.dispatch_count("memory.save", "shared-notes"),
            }


if __name__ == "__main__":
    with TemporaryDirectory(prefix="pdm-guarded-write-") as directory:
        for key, value in run_example(directory).items():
            print(f"{key}: {value}")
