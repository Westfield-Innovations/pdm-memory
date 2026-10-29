"""Guard lifecycle, current evidence and verdict-preserving application recovery."""

from __future__ import annotations

import sqlite3
import threading
from concurrent.futures import ThreadPoolExecutor

import pytest

from pdm_memory.guarded_effects import (
    BranchError,
    CheckResult,
    ConditionVerdict,
    ConfigurationError,
    EffectRequest,
    GuardedEffects,
    IdReuseError,
    Opening,
    ResourceLimitError,
)


def request(action_id="a1", **kwargs):
    base = dict(
        action_id=action_id,
        branch_id="b1",
        program_version="worker-1",
        policy_version="policy-1",
        operation="set",
        target="sim:pump",
        parameters={"running": True},
    )
    base.update(kwargs)
    return EffectRequest(**base)


def opening(handler, *, check=None, max_dispatches=None, validate=None):
    return Opening(
        "set",
        "sim:pump",
        handler,
        validate
        or (lambda r: set(r.parameters) == {"running"} and type(r.parameters["running"]) is bool),
        check,
        max_dispatches,
    )


def guard(path, handler, *, check=None, max_dispatches=None, **kwargs):
    g = GuardedEffects(
        path,
        owner="owner",
        program_version="worker-1",
        policy_version="policy-1",
        openings=[opening(handler, check=check, max_dispatches=max_dispatches)],
        **kwargs,
    )
    g.start_branch("b1")
    return g


def test_staging_does_not_execute_and_handler_return_is_not_observation(tmp_path):
    calls = []
    with guard(tmp_path / "g.db", lambda r: calls.append(r.parameters) or {"accepted": True}) as g:
        assert g.stage(request()).status == "HELD"
        assert calls == []
        receipt = g.release("a1")
        assert receipt.status == "RETURNED"
        assert receipt.executed is None
        assert receipt.result == {"accepted": True}
        assert g.release("a1") == receipt
        assert calls == [{"running": True}]
        assert g.dispatch_count("set", "sim:pump") == 1
        assert g.verify_history()


def test_non_time_condition_is_rechecked_and_full_evidence_retained(tmp_path):
    state = {"revision": 1, "authorized": False}
    calls = []

    def check(r):
        return ConditionVerdict(
            CheckResult.SATISFIED if state["authorized"] else CheckResult.UNKNOWN,
            "owner decision",
            {"source": "owner", "revision": state["revision"]},
        )

    with guard(tmp_path / "g.db", lambda r: calls.append(r.action_id), check=check) as g:
        g.stage(request())
        held = g.release("a1")
        assert held.status == "HELD" and held.disposition is CheckResult.UNKNOWN
        assert held.evidence == {"source": "owner", "revision": 1}
        assert calls == []
        state.update(revision=2, authorized=True)
        assert g.release("a1").status == "RETURNED"
        assert g.receipt("a1").evidence["revision"] == 2


@pytest.mark.parametrize(
    "result,status",
    [
        (CheckResult.CONTRADICTED, "REJECTED"),
        (CheckResult.UNKNOWN, "HELD"),
        (CheckResult.CONFLICT, "HELD"),
        (None, "HELD"),
        ("yes please", "HELD"),
    ],
)
def test_dispositions_never_silently_authorize(tmp_path, result, status):
    calls = []
    with guard(tmp_path / "g.db", lambda r: calls.append(r.action_id), check=lambda r: result) as g:
        g.stage(request())
        assert g.release("a1").status == status
        assert calls == []
        if isinstance(result, CheckResult):
            assert g.receipt("a1").disposition is result


def test_condition_exception_holds_without_dispatch(tmp_path):
    def broken(r):
        raise RuntimeError("no current evidence")

    with guard(tmp_path / "g.db", lambda r: pytest.fail("must not dispatch"), check=broken) as g:
        g.stage(request())
        receipt = g.release("a1")
        assert receipt.status == "HELD"
        assert receipt.disposition is CheckResult.UNKNOWN
        assert "no current evidence" in receipt.reason


def test_captured_request_cannot_be_changed_by_caller_or_check(tmp_path):
    params = {"running": True}
    received = []

    def mutation(r):
        r.parameters["running"] = False
        return True

    with guard(
        tmp_path / "g.db", lambda r: received.append(r.parameters["running"]), check=mutation
    ) as g:
        r = request(parameters=params)
        g.stage(r)
        params["running"] = False
        assert g.release("a1").status == "RETURNED"
        assert received == [True]
        with pytest.raises(IdReuseError):
            g.stage(r)


@pytest.mark.parametrize(
    "changes",
    [
        {"parameters": {"running": False}},
        {"branch_id": "b2"},
        {"program_version": "worker-2"},
        {"policy_version": "policy-2"},
        {"operation": "delete"},
        {"target": "other"},
    ],
)
def test_stable_action_identity_binds_complete_request(tmp_path, changes):
    with guard(tmp_path / "g.db", lambda r: None) as g:
        g.stage(request())
        with pytest.raises(IdReuseError):
            g.stage(request(**changes))


@pytest.mark.parametrize(
    "changes",
    [
        {"program_version": "wrong"},
        {"policy_version": "wrong"},
        {"operation": "other"},
        {"target": "other"},
        {"branch_id": "absent"},
        {"parameters": {"running": "yes"}},
    ],
)
def test_wrong_scope_version_branch_or_parameters_rejected(tmp_path, changes):
    with guard(tmp_path / "g.db", lambda r: pytest.fail("no dispatch")) as g:
        assert g.stage(request(**changes)).status == "REJECTED"
        assert g.release("a1").status == "REJECTED"


def test_two_instances_same_id_and_counter_reservation(tmp_path):
    path = tmp_path / "g.db"
    entered, finish = threading.Event(), threading.Event()
    calls = []

    def handler(r):
        calls.append(r.action_id)
        entered.set()
        assert finish.wait(5)
        return "done"

    with guard(path, handler, max_dispatches=1) as a, guard(path, handler, max_dispatches=1) as b:
        a.stage(request())
        b.stage(request("a2"))
        with ThreadPoolExecutor(max_workers=1) as pool:
            first = pool.submit(a.release, "a1")
            assert entered.wait(5)
            assert b.release("a1").status == "UNCERTAIN"
            assert b.release("a2").status == "HELD"
            finish.set()
            assert first.result(timeout=5).status == "RETURNED"
        assert b.release("a2").status == "REJECTED"
        assert calls == ["a1"]
        assert b.dispatch_count("set", "sim:pump") == 1


def test_concurrent_stage_has_one_immutable_reservation(tmp_path):
    path = tmp_path / "g.db"
    with guard(path, lambda r: None) as a, guard(path, lambda r: None) as b:
        barrier = threading.Barrier(2)

        def run(g):
            barrier.wait(timeout=5)
            return g.stage(request())

        with ThreadPoolExecutor(max_workers=2) as pool:
            receipts = list(pool.map(run, [a, b]))
        assert receipts[0].request_hash == receipts[1].request_hash
        assert len([e for e in a.history("a1") if e["kind"] == "REQUEST_CAPTURED"]) == 1


def test_raised_handler_is_uncertain_across_reopen_and_requires_reconciliation(tmp_path):
    path = tmp_path / "g.db"
    calls = []

    def broken(r):
        calls.append(r.action_id)
        raise RuntimeError("connection lost after possible write")

    with guard(path, broken) as g:
        g.stage(request())
        assert g.release("a1").status == "UNCERTAIN"
    with guard(path, broken) as g:
        assert g.release("a1").status == "UNCERTAIN"
        g.stage(request("a2"))
        assert g.release("a2").status == "HELD"
        settled = g.reconcile("a1", executed=False, evidence={"source": "receiver", "sequence": 0})
        assert settled.status == "OBSERVED" and settled.executed is False
        assert g.release("a1").status == "OBSERVED"
        assert calls == ["a1"]


def test_dispatch_intent_left_by_interruption_is_not_retried(tmp_path):
    path = tmp_path / "g.db"
    with guard(path, lambda r: None) as g:
        g.stage(request())
        # A persisted intent with no completion models interruption between
        # the pre-dispatch commit and the handler/result record.
        with g._transaction():
            g._set("a1", "DISPATCHING", CheckResult.SATISFIED, "intent committed")
    with guard(path, lambda r: pytest.fail("must not retry interrupted dispatch")) as g:
        assert g.release("a1").status == "UNCERTAIN"


def test_invalid_handler_result_is_uncertain(tmp_path):
    with guard(tmp_path / "g.db", lambda r: object()) as g:
        g.stage(request())
        assert g.release("a1").status == "UNCERTAIN"


def test_recovery_preserves_verdict_counts_and_invalidates_old_branch(tmp_path):
    path = tmp_path / "g.db"
    state = {"step": 0, "pending": []}
    with guard(path, lambda r: {"accepted": True}, max_dispatches=1) as g:
        g.checkpoint("cp1", branch_id="b1", state=state)
        state["step"] = 99
        g.stage(request())
        g.release("a1")
        g.stage(request("waiting"))
        g.stage(request("denied", target="outside"))
        before = g.history(limit=1000)
        ctx = g.recover("cp1", new_branch_id="b2")
        assert ctx.state == {"step": 0, "pending": []}
        assert ctx.current_policy_version == "policy-1"
        assert g.receipt("a1").status == "RETURNED"
        assert g.receipt("denied").status == "REJECTED"
        assert g.receipt("waiting").status == "CANCELLED"
        assert g.history(limit=1000)[: len(before)] == before
        assert {r.action_id for r in ctx.receipts} == {"a1", "waiting", "denied"}
        assert g.stage(request("old-branch")).status == "REJECTED"
        with pytest.raises(BranchError):
            g.start_branch("b1")
        g.stage(request("a2", branch_id="b2"))
        assert g.release("a2").status == "REJECTED"
        assert g.dispatch_count("set", "sim:pump") == 1
        assert g.verify_history()


def test_recovery_does_not_reintroduce_old_policy_and_old_controller_is_fenced(tmp_path):
    path = tmp_path / "g.db"
    a = guard(path, lambda r: pytest.fail("old controller must be fenced"))
    a.checkpoint("cp", branch_id="b1", state={"step": 1})
    a.stage(request())
    with GuardedEffects(
        path,
        owner="owner",
        program_version="worker-1",
        policy_version="policy-2",
        openings=[opening(lambda r: None)],
        replace_config=True,
    ) as b:
        with pytest.raises(ConfigurationError, match="superseded"):
            a.release("a1")
        ctx = b.recover("cp", new_branch_id="b2")
        assert ctx.checkpoint_policy_version == "policy-1"
        assert ctx.current_policy_version == "policy-2"
        assert b.stage(request("old-policy", branch_id="b2")).status == "REJECTED"
        b.stage(request("new-policy", branch_id="b2", policy_version="policy-2"))
        assert b.release("new-policy").status == "RETURNED"
    a.close()


def test_program_version_change_refuses_incompatible_checkpoint(tmp_path):
    path = tmp_path / "g.db"
    with guard(path, lambda r: None) as g:
        g.checkpoint("cp", branch_id="b1", state={"step": 1})
    with GuardedEffects(
        path,
        owner="owner",
        program_version="worker-2",
        policy_version="policy-1",
        openings=[opening(lambda r: None)],
        replace_config=True,
    ) as g:
        with pytest.raises(ValueError, match="incompatible"):
            g.recover("cp", new_branch_id="b2")


def test_config_replacement_is_explicit_and_fences_every_mutation(tmp_path):
    path = tmp_path / "g.db"
    with guard(path, lambda r: None) as a:
        a.stage(request())
        a.checkpoint("cp", branch_id="b1", state={"step": 0})
        with pytest.raises(ConfigurationError, match="replace_config"):
            GuardedEffects(
                path,
                owner="owner",
                program_version="worker-1",
                policy_version="policy-2",
                openings=[opening(lambda r: None)],
            )
        assert a.stage(request("still-current")).status == "HELD"
        with GuardedEffects(
            path,
            owner="owner",
            program_version="worker-1",
            policy_version="policy-2",
            openings=[opening(lambda r: None)],
            replace_config=True,
        ):
            for mutation in [
                lambda: a.start_branch("b1"),
                lambda: a.stage(request("new")),
                lambda: a.release("a1"),
                lambda: a.cancel("a1"),
                lambda: a.reconcile("a1", executed=False, evidence={"source": "owner"}),
                lambda: a.checkpoint("cp2", branch_id="b1", state={}),
                lambda: a.recover("cp", new_branch_id="b2"),
            ]:
                with pytest.raises(ConfigurationError, match="superseded"):
                    mutation()
        # A later owner activation of the old labels still creates a new epoch.
        with GuardedEffects(
            path,
            owner="owner",
            program_version="worker-1",
            policy_version="policy-1",
            openings=[opening(lambda r: pytest.fail("old staged work must not revive"))],
            replace_config=True,
        ) as current:
            with pytest.raises(ConfigurationError, match="superseded"):
                a.stage(request("stale"))
            assert current.release("a1").status == "REJECTED"
            assert "epoch" in current.receipt("a1").reason
            assert current.verify_history()


@pytest.mark.parametrize("path", ["", ":memory:"])
def test_durable_controller_refuses_known_ephemeral_storage(path):
    with pytest.raises(ValueError, match="on-disk"):
        GuardedEffects(
            path, owner="owner", program_version="worker-1", policy_version="policy-1", openings=[]
        )


def test_input_queue_checkpoint_and_result_bounds(tmp_path):
    with guard(
        tmp_path / "g.db",
        lambda r: "x" * 200,
        max_request_bytes=300,
        max_pending=1,
        max_state_bytes=100,
        max_result_bytes=100,
    ) as g:
        with pytest.raises(ResourceLimitError):
            g.stage(request(parameters={"x": "x" * 400}))
        g.stage(request())
        with pytest.raises(ResourceLimitError):
            g.stage(request("a2"))
        with pytest.raises(ResourceLimitError):
            g.checkpoint("cp", branch_id="b1", state={"text": "x" * 200})
        assert g.release("a1").status == "UNCERTAIN"


@pytest.mark.parametrize("bad", [{1: "bad"}, {"x": float("nan")}, {"x": object()}])
def test_request_json_is_strict_and_lossless(tmp_path, bad):
    with guard(tmp_path / "g.db", lambda r: None) as g:
        with pytest.raises((TypeError, ValueError)):
            g.stage(request(parameters=bad))


def test_history_and_checkpoint_records_have_sql_immutability_guards(tmp_path):
    path = tmp_path / "g.db"
    with guard(path, lambda r: None) as g:
        g.checkpoint("cp", branch_id="b1", state={"step": 0})
        g.stage(request())
        raw = sqlite3.connect(path)
        with pytest.raises(sqlite3.IntegrityError, match="append-only"):
            raw.execute("DELETE FROM ge_events")
        raw.rollback()
        with pytest.raises(sqlite3.IntegrityError, match="immutable"):
            raw.execute("UPDATE ge_checkpoints SET state_json='{}'")
        raw.close()
        assert g.verify_history()
        assert g._conn.execute("PRAGMA synchronous").fetchone()[0] == 2


def test_owner_namespaces_are_separate(tmp_path):
    path = tmp_path / "g.db"
    with guard(path, lambda r: None) as a:
        a.stage(request())
        with GuardedEffects(
            path,
            owner="other",
            program_version="worker-1",
            policy_version="policy-1",
            openings=[opening(lambda r: None)],
        ) as b:
            with pytest.raises(KeyError):
                b.receipt("a1")
            b.start_branch("b1")
            b.stage(request())
            assert b.release("a1").status == "RETURNED"
            assert a.receipt("a1").status == "HELD"
            assert b.verify_history() and a.verify_history()


def test_same_controller_rerelease_during_dispatch_does_not_mark_uncertain(tmp_path):
    entered, finish = threading.Event(), threading.Event()
    calls = []

    def handler(r):
        calls.append(r.action_id)
        entered.set()
        assert finish.wait(5)
        return "done"

    with guard(tmp_path / "g.db", handler) as g:
        g.stage(request())
        with ThreadPoolExecutor(max_workers=1) as pool:
            first = pool.submit(g.release, "a1")
            assert entered.wait(5)
            assert g.release("a1").status == "DISPATCHING"
            finish.set()
            assert first.result(timeout=5).status == "RETURNED"
        assert calls == ["a1"]
        assert "UNCERTAIN" not in [e["kind"] for e in g.history("a1")]


def test_unknown_reconciliation_of_returned_action_keeps_namespace_open(tmp_path):
    with guard(tmp_path / "g.db", lambda r: {"accepted": True}) as g:
        g.stage(request())
        g.release("a1")
        noted = g.reconcile("a1", executed=None, evidence={"source": "receiver", "seen": False})
        assert noted.status == "RETURNED" and noted.executed is None
        assert noted.evidence == {"source": "receiver", "seen": False}
        assert noted.result == {"accepted": True}
        g.stage(request("a2"))
        assert g.release("a2").status == "RETURNED"


@pytest.mark.parametrize(
    "changed",
    [
        dict(max_dispatches=5),
        dict(check=lambda r: True),
        dict(extra=Opening("set", "sim:valve", lambda r: None, lambda r: True)),
    ],
)
def test_changed_openings_require_explicit_config_replacement(tmp_path, changed):
    path = tmp_path / "g.db"
    guard(path, lambda r: None).close()
    extra = changed.pop("extra", None)
    openings = [opening(lambda r: None, **changed)] + ([extra] if extra else [])
    kwargs = dict(
        owner="owner", program_version="worker-1", policy_version="policy-1", openings=openings
    )
    with pytest.raises(ConfigurationError, match="replace_config"):
        GuardedEffects(path, **kwargs)
    with GuardedEffects(path, replace_config=True, **kwargs) as g:
        assert g._config_epoch == 2
    # Same openings with different handler objects is the same configuration.
    with GuardedEffects(path, **kwargs) as g:
        assert g._config_epoch == 2
