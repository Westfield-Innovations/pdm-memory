"""CloudGuardedEffects: at most one dispatch, owner callbacks only on the client."""

from __future__ import annotations

import httpx
import pytest

from pdm_memory.cloud_ledgers import CloudGuardedEffects, DispatchUnrecorded
from pdm_memory.cloud_ledgers.testing import in_process
from pdm_memory.guarded_effects import (
    CheckResult,
    EffectRequest,
    GuardedEffects,
    LedgerIntegrityError,
    Opening,
)
from pdm_memory.storage.errors import CloudConflictError


def request(action_id="A", **kwargs):
    base = {
        "action_id": action_id,
        "branch_id": "b1",
        "program_version": "w-1",
        "policy_version": "p-1",
        "operation": "open",
        "target": "valve-3",
        "parameters": {"percent": 40},
    }
    return EffectRequest(**{**base, **kwargs})


class Owner:
    """An owner's callbacks that count their calls."""

    def __init__(self, result=None, check=True):
        self.calls = {"validate": 0, "check": 0, "handler": 0}
        self.result = {"opened": True} if result is None else result
        self.verdict = check

    def validate(self, r):
        self.calls["validate"] += 1
        return r.parameters == {"percent": 40}

    def check(self, r):
        self.calls["check"] += 1
        return self.verdict

    def handler(self, r):
        self.calls["handler"] += 1
        if isinstance(self.result, BaseException):
            raise self.result
        return self.result


def guard(cloud, owner=None):
    owner = owner or Owner()
    g = GuardedEffects(
        cloud,
        owner="o",
        program_version="w-1",
        policy_version="p-1",
        openings=[
            Opening("open", "valve-3", owner.handler, owner.validate, owner.check)
        ],
    )
    g.start_branch("b1")
    return g


def wrap(cloud, around):
    """Route every send through ``around(op, body, forward)``."""
    inner = cloud._transport.send

    def send(namespace, kind, op, body, key):
        return around(op, body, lambda b=body: inner(namespace, kind, op, b, key))

    cloud._transport.send = send
    return cloud


def kinds(events):
    return [e["kind"] for e in events]


def test_the_cloud_class_overrides_every_public_method():
    public = {
        n
        for n, v in vars(GuardedEffects).items()
        if callable(v) and not n.startswith("_")
    }
    assert public <= set(vars(CloudGuardedEffects))


def test_a_cloud_target_builds_the_cloud_class(tmp_path):
    assert type(guard(in_process(tmp_path / "g.db"))) is CloudGuardedEffects
    local = GuardedEffects(
        tmp_path / "l.db",
        owner="o",
        program_version="w",
        policy_version="p",
        openings=[],
    )
    assert type(local) is GuardedEffects
    local.close()


def test_handler_and_checks_run_on_the_client_only(tmp_path):
    owner = Owner()
    g = guard(in_process(tmp_path / "g.db"), owner)
    assert g.stage(request()).status == "HELD"
    assert owner.calls == {"validate": 1, "check": 0, "handler": 0}
    owner.calls = dict.fromkeys(owner.calls, 0)
    receipt = g.release("A")
    # The host's callbacks refuse to run, so RETURNED means none ran there.
    assert receipt.status == "RETURNED" and receipt.result == {"opened": True}
    assert owner.calls == {"validate": 1, "check": 1, "handler": 1}
    assert g.release("A") == receipt
    assert owner.calls["handler"] == 1


def test_a_lost_reservation_response_still_dispatches_once(tmp_path):
    lost = []

    def drop_reservation(op, body, forward):
        status, reply = forward()
        if op == "release" and "dispatch" in reply and not lost:
            lost.append(reply)
            raise httpx.ReadTimeout("response dropped")
        return status, reply

    owner = Owner()
    g = guard(wrap(in_process(tmp_path / "g.db"), drop_reservation), owner)
    g.stage(request())
    assert g.release("A").status == "RETURNED"
    assert lost and owner.calls["handler"] == 1
    assert g.dispatch_count("open", "valve-3") == 1


def test_a_lost_completion_response_records_one_return(tmp_path):
    g = guard(in_process(tmp_path / "g.db", drop_response_after={"complete"}))
    g.stage(request())
    assert g.release("A").status == "RETURNED"
    assert kinds(g.history()).count("RETURNED") == 1
    assert "LATE_HANDLER_RETURN" not in kinds(g.history())


def test_a_client_that_dies_after_reserving_leaves_the_action_uncertain_for_others(
    tmp_path,
):
    owner = Owner()
    a = guard(in_process(tmp_path / "g.db"), owner)
    b = guard(in_process(tmp_path / "g.db"))
    a.stage(request())
    reserved = a._send("release", {"action_id": "A"})  # then the client dies
    assert reserved["result"]["status"] == "DISPATCHING" and "dispatch" in reserved
    assert owner.calls["handler"] == 0
    assert b.release("A").status == "UNCERTAIN"
    assert b.reconcile("A", executed=True, evidence={"seen": 1}).status == "OBSERVED"


def test_a_dispatch_between_check_and_reservation_forces_a_fresh_check(tmp_path):
    b = guard(in_process(tmp_path / "g.db"))
    owner = Owner()
    raced = []

    def race(op, body, forward):
        if op == "release" and "fence" in body["args"] and not raced:
            raced.append(b.release("A1"))
        return forward()

    a = guard(wrap(in_process(tmp_path / "g.db"), race), owner)
    a.stage(request("A1"))
    a.stage(request("A2"))
    owner.calls["check"] = 0
    a.release("A2")
    assert raced[0].status == "RETURNED"
    assert owner.calls["check"] == 2
    assert a.receipt("A2").status in ("RETURNED", "HELD")
    assert a.dispatch_count("open", "valve-3") == 2


def test_verdicts_that_never_catch_up_end_in_ledger_busy(tmp_path):
    def stale(op, body, forward):
        if "fence" in body["args"]:
            body = {**body, "args": {**body["args"], "fence": -1}}
        return forward(body)

    cloud = wrap(in_process(tmp_path / "g.db"), stale)
    g = guard(cloud)
    cloud.retry_deadline_s = 0.05
    with pytest.raises(CloudConflictError) as caught:
        g.stage(request())
    assert caught.value.error_code == "LEDGER_BUSY"


def test_a_handler_exception_is_uncertain_and_a_keyboard_interrupt_still_propagates(
    tmp_path,
):
    owner = Owner(result=RuntimeError("valve did not answer"))
    g = guard(in_process(tmp_path / "g.db"), owner)
    g.stage(request())
    failed = g.release("A")
    assert failed.status == "UNCERTAIN"
    assert failed.reason.endswith("RuntimeError: valve did not answer")
    g.reconcile("A", executed=False, evidence={"seen": 0})
    owner.result = KeyboardInterrupt("stop")
    g.stage(request("B"))
    with pytest.raises(KeyboardInterrupt):
        g.release("B")
    assert g.receipt("B").status == "UNCERTAIN"
    assert "KeyboardInterrupt: stop" in g.receipt("B").reason


def test_an_unreachable_completion_raises_dispatch_unrecorded_with_the_result(
    tmp_path,
):
    def unreachable(op, body, forward):
        if op == "complete":
            raise httpx.ConnectError("host gone")
        return forward()

    cloud = wrap(in_process(tmp_path / "g.db"), unreachable)
    g = guard(cloud)
    cloud.retry_deadline_s = 0.05
    g.stage(request())
    with pytest.raises(DispatchUnrecorded) as caught:
        g.release("A")
    assert caught.value.action_id == "A"
    assert caught.value.result == {"opened": True} and caught.value.error is None
    assert g.receipt("A").status == "DISPATCHING"
    other = guard(in_process(tmp_path / "g.db"))
    assert other.release("A").status == "UNCERTAIN"


def test_verify_history_notices_a_shortened_chain(tmp_path):
    cut = []

    def shorten(op, body, forward):
        status, reply = forward()
        if op == "history_raw" and cut:
            reply = {**reply, "result": reply["result"][:-1]}
        return status, reply

    g = guard(wrap(in_process(tmp_path / "g.db"), shorten))
    g.stage(request())
    assert g.verify_history() is True
    assert g.verify_history() is True
    cut.append(True)
    with pytest.raises(LedgerIntegrityError):
        g.verify_history()


def test_a_cloud_receipt_matches_the_local_one(tmp_path):
    local = guard(tmp_path / "local.db")
    cloud = guard(in_process(tmp_path / "cloud.db"))
    for g in (local, cloud):
        g.stage(request())
        g.release("A")
    a, b = local.receipt("A"), cloud.receipt("A")
    assert (a.status, a.disposition, a.result, a.evidence) == (
        b.status,
        b.disposition,
        b.result,
        b.evidence,
    )
    assert a.disposition is CheckResult.SATISFIED
    local.close()
