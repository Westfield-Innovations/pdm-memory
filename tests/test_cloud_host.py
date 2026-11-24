"""The ledger host: local ledgers run for remote clients through the JSON protocol."""

from __future__ import annotations

import inspect
import itertools
import json
from dataclasses import replace
from datetime import timedelta

import pytest

from pdm_memory import ConsequenceLedger
from pdm_memory._ledger.db import Database
from pdm_memory.cloud_ledgers.host import OPERATIONS, LedgerHost, _refuse
from pdm_memory.cloud_ledgers.wire import decode, encode
from pdm_memory.consequence import Cause, CauseHypothesis, EvidenceKind, Route
from pdm_memory.goal_loop.states import JobState
from pdm_memory.goal_loop.store import PostgresJobStore, SqliteJobStore, _SqlJobStore
from pdm_memory.goal_loop.testing import HISTORY, T0, _job, _next, _receipt, _start
from pdm_memory.guarded_effects import ActionReceipt, GuardedEffects
from tests.consequence_fixtures import (
    COMMIT,
    POLICY,
    POSITION_MOVED,
    TASK,
    TEMP,
    VALVE,
    _request,
)
from tests.ledger_target import ON_POSTGRES, ledger_target

SAT = {"status": "SATISFIED", "reason": "ok", "evidence": {"seen": 1}}
GUARD = {
    "owner": "owner-1",
    "program_version": "worker-1",
    "policy_version": "policy-1",
    "openings": [
        {
            "operation": "open",
            "target": "valve-3",
            "max_dispatches": None,
            "has_check": True,
        }
    ],
    "max_request_bytes": 65_536,
    "max_pending": 128,
    "max_state_bytes": 1_048_576,
    "max_result_bytes": 65_536,
    "replace_config": False,
}
CONSEQUENCE = {"owner": "owner-1", "policy": encode(POLICY), "custom_clock": False}
OPENS = {"guard": GUARD, "consequence": CONSEQUENCE, "jobs": {}}
_keys = itertools.count()


@pytest.fixture
def host():
    h = LedgerHost()
    yield h
    h.close()


@pytest.fixture
def target(tmp_path):
    return ledger_target(tmp_path, "l.db")


def call(
    host, target, kind, op, *, args=None, open=None, now=None, key="auto", protocol=1
):
    if key == "auto":
        key = f"key-{next(_keys):08d}" if OPERATIONS[kind][op] else None
    body = {"protocol": protocol, "open": OPENS[kind] if open is None else open}
    body.update(args={} if args is None else args, now=now)
    return host.handle(
        target, kind, op, json.loads(json.dumps(body)), idempotency_key=key
    )


def ok(response):
    status, body = response
    assert status == 200, body
    return body


def scalar(target, sql):
    db = Database(target, lock_key="test")
    try:
        return db.execute(sql).fetchone()[0]
    finally:
        db.close()


def head(target, table="ge_events"):
    return scalar(target, f"SELECT COALESCE(MAX(sequence), 0) FROM {table}")


def kinds(target):
    db = Database(target, lock_key="test")
    try:
        return [
            r[0] for r in db.execute("SELECT kind FROM ge_events ORDER BY sequence")
        ]
    finally:
        db.close()


def ready(host, target, **open_changes):
    opened = {**GUARD, **open_changes}
    ok(call(host, target, "guard", "open", open=opened))
    ok(
        call(
            host, target, "guard", "start_branch", args={"branch_id": "b1"}, open=opened
        )
    )


def stage(host, target, action_id="act-valve-1"):
    request = encode(_request(action_id))
    probe = ok(call(host, target, "guard", "stage", args={"request": request}))
    args = {"request": request, "validation": SAT, "fence": probe["fence"]}
    return ok(call(host, target, "guard", "stage", args=args))["result"]


def reserve(host, target, action_id="act-valve-1"):
    probe = ok(call(host, target, "guard", "release", args={"action_id": action_id}))
    assert probe["needs"] == ["validation", "condition"]
    args = {"action_id": action_id, "validation": SAT, "condition": SAT}
    return ok(
        call(host, target, "guard", "release", args={**args, "fence": probe["fence"]})
    )


def complete(host, target, action_id="act-valve-1", key="auto", **args):
    args = {"action_id": action_id, **args}
    return call(host, target, "guard", "complete", args=args, key=key)


def test_a_staged_request_needs_a_verdict_and_writes_nothing_until_given_one(
    host, target
):
    ready(host, target)
    before = head(target)
    request = encode(_request())
    probe = ok(call(host, target, "guard", "stage", args={"request": request}))
    assert probe["needs"] == ["validation"] and probe["fence"] == before
    assert json.loads(probe["request"]) == request and head(target) == before
    args = {"request": request, "validation": SAT, "fence": probe["fence"]}
    receipt = ok(call(host, target, "guard", "stage", args=args))["result"]
    assert receipt["status"] == "HELD" and head(target) > before
    assert receipt["evidence"] == {
        "validation": "SATISFIED",
        "reason": "ok",
        "evidence": {"seen": 1},
    }


def test_a_moved_head_sends_the_verdict_back(host, target):
    ready(host, target)
    stage(host, target)
    probe = ok(
        call(host, target, "guard", "release", args={"action_id": "act-valve-1"})
    )
    stage(host, target, "act-valve-2")  # another client moves the owner's head
    args = {"action_id": "act-valve-1", "validation": SAT, "condition": SAT}
    again = ok(
        call(host, target, "guard", "release", args={**args, "fence": probe["fence"]})
    )
    assert again["needs"] and again["fence"] > probe["fence"]
    receipt = ok(
        call(host, target, "guard", "receipt", args={"action_id": "act-valve-1"})
    )
    assert receipt["result"]["status"] == "HELD"
    final = ok(
        call(host, target, "guard", "release", args={**args, "fence": again["fence"]})
    )
    assert final["result"]["status"] == "DISPATCHING"


def test_a_reserved_release_hands_back_the_request_and_never_runs_a_handler(
    host, target
):
    ready(host, target)
    stage(host, target)
    body = reserve(host, target)
    assert body["result"]["status"] == "DISPATCHING"
    assert json.loads(body["dispatch"]["request"]) == encode(_request())
    args = {"operation": "open", "target": "valve-3"}
    assert ok(call(host, target, "guard", "dispatch_count", args=args))["result"] == 1
    assert "UNCERTAIN" not in kinds(target)
    with pytest.raises(RuntimeError):
        _refuse(_request())


def test_complete_records_returned_and_a_failure_records_uncertain(host, target):
    ready(host, target)
    stage(host, target)
    stage(host, target, "act-valve-2")
    reserve(host, target)
    done = ok(complete(host, target, result={"accepted": True}))["result"]
    assert (done["status"], done["result"]) == ("RETURNED", {"accepted": True})
    assert done["evidence"] == {"seen": 1}
    reserve(host, target, "act-valve-2")
    failed = ok(complete(host, target, "act-valve-2", failure="RuntimeError: boom"))
    assert failed["result"]["status"] == "UNCERTAIN"
    assert "RuntimeError: boom" in failed["result"]["reason"]


def test_a_result_the_ledger_cannot_keep_is_recorded_as_a_failure(host, target):
    ready(host, target)
    stage(host, target)
    reserve(host, target)
    receipt = ok(complete(host, target, result="x" * 70_000))["result"]
    assert (
        receipt["status"] == "UNCERTAIN" and "ResourceLimitError" in receipt["reason"]
    )


def test_complete_refuses_a_held_action_and_records_a_late_return(host, target):
    ready(host, target)
    stage(host, target)
    status, body = complete(host, target, result={})
    assert status == 422 and body["error"]["type"] == "ValueError"
    reserve(host, target)
    first = ok(complete(host, target, result={"n": 1}))["result"]
    late = [complete(host, target, result={"n": 2}, key="k-late-0001") for _ in "ab"]
    assert late[0] == late[1] and ok(late[0])["result"] == first
    assert kinds(target).count("LATE_HANDLER_RETURN") == 1
    before = head(target)
    assert ok(complete(host, target, failure="RuntimeError: x"))["result"] == first
    assert head(target) == before


def test_a_client_failure_is_cut_to_the_local_format(host, target):
    ready(host, target)
    stage(host, target)
    reserve(host, target)
    receipt = ok(complete(host, target, failure="RuntimeError: " + "y" * 2000))
    reason = receipt["result"]["reason"]
    assert reason.endswith("RuntimeError: " + "y" * 512)


def test_another_workers_same_config_replacement_does_not_refuse_a_client(target):
    w1, w2 = LedgerHost(), LedgerHost()
    ready(w1, target)
    stage(w2, target)  # w2 caches an instance under the same open
    ok(call(w1, target, "guard", "open", open={**GUARD, "replace_config": True}))
    receipt = ok(call(w2, target, "guard", "cancel", args={"action_id": "act-valve-1"}))
    assert receipt["result"]["status"] == "CANCELLED"
    changed = {**GUARD, "replace_config": True, "policy_version": "p2"}
    ok(call(w1, target, "guard", "open", open=changed))
    status, body = call(w2, target, "guard", "start_branch", args={"branch_id": "b1"})
    assert status == 409 and body["error"]["type"] == "ConfigurationError"
    w1.close()
    w2.close()


def test_a_same_config_replacement_fences_a_client_holding_the_old_epoch(host, target):
    first = ok(call(host, target, "guard", "open"))["result"]
    old = {**GUARD, "config_epoch": first}
    ok(call(host, target, "guard", "start_branch", args={"branch_id": "b1"}, open=old))
    replaced = {**GUARD, "replace_config": True}
    current = ok(call(host, target, "guard", "open", open=replaced))["result"]
    assert current == first + 1
    status, body = call(
        host, target, "guard", "start_branch", args={"branch_id": "b1"}, open=old
    )
    assert status == 409 and body["error"]["type"] == "ConfigurationError"
    assert "superseded" in body["detail"]
    new = {**GUARD, "config_epoch": current}
    ok(call(host, target, "guard", "start_branch", args={"branch_id": "b1"}, open=new))
    assert ok(call(host, target, "guard", "history_raw", open=old))["result"]


def test_a_retried_write_with_the_same_key_replays_the_response(host, target):
    ready(host, target)
    request = encode(_request())
    probe = ok(call(host, target, "guard", "stage", args={"request": request}))
    args = {"request": request, "validation": SAT, "fence": probe["fence"]}
    a = call(host, target, "guard", "stage", args=args, key="k-12345678")
    b = call(host, target, "guard", "stage", args=args, key="k-12345678")
    assert a == b and a[0] == 200
    assert kinds(target).count("REQUEST_CAPTURED") == 1


def test_the_same_key_with_another_body_is_refused(host, target):
    ready(host, target)
    stage(host, target)
    args = {"action_id": "act-valve-1"}
    ok(call(host, target, "guard", "cancel", args=args, key="k-12345678"))
    status, body = call(
        host,
        target,
        "guard",
        "cancel",
        args={**args, "reason": "other"},
        key="k-12345678",
    )
    assert status == 422 and body["error_code"] == "IDEMPOTENCY_KEY_REUSED"


@pytest.mark.parametrize("key", [None, "short", "has space in it", "x" * 129])
def test_a_write_without_a_key_is_refused(host, target, key):
    status, body = call(host, target, "guard", "open", key=key)
    assert status == 400 and body["error_code"] == "IDEMPOTENCY_KEY_REQUIRED"


def test_every_call_works_after_the_cache_is_evicted(host, target):
    ready(host, target)
    stage(host, target)
    host.evict()
    reserve(host, target)
    host.evict(target)
    assert ok(complete(host, target, result={}))["result"]["status"] == "RETURNED"


def test_a_replaced_configuration_fences_the_old_one(host, target):
    ready(host, target)
    stage(host, target)
    ok(
        call(
            host,
            target,
            "guard",
            "open",
            open={**GUARD, "replace_config": True, "policy_version": "p2"},
        )
    )
    for _ in range(2):  # the cached instance, then a fresh one
        status, body = call(
            host, target, "guard", "cancel", args={"action_id": "act-valve-1"}
        )
        assert status == 409 and body["error"]["type"] == "ConfigurationError"


def test_a_retried_replacement_does_not_replace_twice(host, target):
    ready(host, target)
    replaced = {**GUARD, "replace_config": True}
    a = call(host, target, "guard", "open", open=replaced, key="k-replace-1")
    b = call(host, target, "guard", "open", open=replaced, key="k-replace-1")
    assert a == b and kinds(target).count("CONFIGURATION_ACTIVATED") == 2


def _source_of(kind, op):
    cls = {
        "guard": GuardedEffects,
        "consequence": ConsequenceLedger,
        "jobs": _SqlJobStore,
    }[kind]
    name = {
        "open": "__init__",
        "stage": "_stage",
        "release": "_reserve",
        "complete": "_complete",
    }
    return inspect.getsource(
        getattr(cls, name.get(op, op) if kind == "guard" or op == "open" else op)
    )


@pytest.mark.parametrize(
    "kind, op",
    [(k, o) for k, ops in OPERATIONS.items() for o in ops if o != "history_raw"],
)
def test_an_operation_writes_exactly_when_its_method_opens_a_transaction(kind, op):
    assert ("self._transaction()" in _source_of(kind, op)) is OPERATIONS[kind][op]


def _jobs_store(target):
    return (PostgresJobStore if ON_POSTGRES else SqliteJobStore)(target)


@pytest.fixture
def filled(host, target):
    """Every ledger in *target* holds records; returns the args each read takes."""
    ready(host, target)
    stage(host, target)
    reserve(host, target)
    receipt = ok(complete(host, target, result={}))["result"]
    with ConsequenceLedger(
        target, owner="owner-1", policy=POLICY, clock=lambda: COMMIT
    ) as led:
        led.prepare(VALVE)
        led.attach_action("exp-valve-1", decode(ActionReceipt, receipt))
        led.observe(POSITION_MOVED)
        led.observe(replace(TEMP, value=18.0))
        causes = (CauseHypothesis(Cause.EXECUTION, ("obs-pos",)),)
        evaluation = led.evaluate("exp-valve-1", task=TASK, causes=causes)
        proposal = led.propose_learning(
            evaluation.evaluation_id,
            task=TASK,
            criterion="room_temp",
            target_kind="belief",
            target_id="b",
            target_version="1",
            change={"reopen": True},
            scope="loop",
            confidence=0.7,
            route=Route.OWN_ACTION,
            evidence_kind=EvidenceKind.MEASURED,
        )
    store = _jobs_store(target)
    try:
        _start(store)
    finally:
        store.close()
    return {
        ("guard", "receipt"): {"action_id": "act-valve-1"},
        ("guard", "dispatch_count"): {"operation": "open", "target": "valve-3"},
        ("consequence", "expectation"): {"expectation_id": "exp-valve-1"},
        ("consequence", "prepared_check"): {"request": encode(_request())},
        ("consequence", "actions"): {"expectation_id": "exp-valve-1"},
        ("consequence", "current_evaluation"): {"expectation_id": "exp-valve-1"},
        ("consequence", "forecast_record"): {"predictor": "valve-model"},
        ("consequence", "evaluation"): {"evaluation_id": evaluation.evaluation_id},
        ("consequence", "accounts"): {"expectation_id": "exp-valve-1"},
        ("consequence", "update_state"): {"update_id": proposal.update_id},
        ("consequence", "significance"): {"expectation_id": "exp-valve-1"},
        ("jobs", "load"): {"job_id": "job-1"},
        ("jobs", "receipts"): {"job_id": "job-1"},
        ("jobs", "contract_history"): {"job_id": "job-1"},
        ("jobs", "due"): {"now": T0.isoformat()},
    }


def fingerprint(target):
    return [
        scalar(target, sql)
        for sql in (
            "SELECT COALESCE(MAX(sequence), 0) FROM ge_events",
            "SELECT COALESCE(MAX(sequence), 0) FROM cl_events",
            "SELECT COUNT(*) FROM pdm_requests",
            "SELECT COUNT(*) FROM gl_receipts",
            "SELECT SUM(revision) FROM gl_jobs",
        )
    ]


def test_read_operations_append_nothing(host, target, filled):
    reads = [(k, o) for k, ops in OPERATIONS.items() for o, w in ops.items() if not w]
    before = fingerprint(target)
    for kind, op in reads:
        ok(call(host, target, kind, op, args=filled.get((kind, op), {})))
    assert fingerprint(target) == before


def test_history_raw_returns_the_owners_chain_rows(host, target):
    ready(host, target)
    rows = ok(call(host, target, "guard", "history_raw", args={"limit": 1000}))[
        "result"
    ]
    assert [r[3] for r in rows] == ["CONFIGURATION_ACTIVATED", "BRANCH_STARTED"]
    assert all(len(r) == 7 and r[1] == "owner-1" for r in rows)
    assert call(host, target, "guard", "history_raw", args={"limit": 1001})[0] == 422


def test_jobs_travel_through_the_host(host, target):
    job = _job()
    first = _receipt(job, JobState.DRAFTED, None)
    job = replace(job, receipt_seq=1, receipt_head=first.receipt_hash)
    args = {
        "job": encode(job),
        "contract_history": HISTORY,
        "receipts": [encode(first)],
    }
    ok(call(host, target, "jobs", "create", args=args))
    moved, second = _next(job, first, JobState.APPROVED)
    args = {
        "job": encode(moved),
        "receipts": [encode(second)],
        "expected_revision": job.revision,
    }
    ok(call(host, target, "jobs", "commit", args=args))
    loaded = ok(call(host, target, "jobs", "load", args={"job_id": "job-1"}))["result"]
    assert loaded == encode(moved)
    status, body = call(host, target, "jobs", "commit", args=args)
    assert status == 409 and body["error"]["type"] == "StaleJobError"


def test_an_explicit_now_is_refused_without_a_trusted_clock(host, target):
    args = {"expectation_id": "exp-valve-1", "now": COMMIT.isoformat()}
    status, body = call(host, target, "consequence", "suggest_observation", args=args)
    assert status == 409 and body["error"]["type"] == "ConfigurationError"
    status, body = call(
        host, target, "consequence", "open", open={**CONSEQUENCE, "custom_clock": True}
    )
    assert status == 409 and body["error"]["type"] == "ConfigurationError"


def test_a_trusted_client_clock_stamps_the_record(target):
    custom = {**CONSEQUENCE, "custom_clock": True}
    host = LedgerHost(trust_client_clock=True)
    args = {"expectation": encode(VALVE)}
    now = COMMIT.isoformat()
    ok(call(host, target, "consequence", "prepare", args=args, open=custom, now=now))
    args = {"expectation_id": "exp-valve-1"}
    body = ok(call(host, target, "consequence", "expectation", args=args, open=custom))
    host.close()
    assert body["result"][1] == now


def test_old_request_rows_are_pruned_after_a_day(target):
    moment = [COMMIT]
    host = LedgerHost(clock=lambda: moment[0])
    count = "SELECT COUNT(*) FROM pdm_requests"
    ready(host, target)  # two writes
    moment[0] += timedelta(hours=25)
    host._writes = 62
    stage(host, target)  # the 63rd write; the probe stores nothing
    assert scalar(target, count) == 3
    ok(call(host, target, "guard", "cancel", args={"action_id": "act-valve-1"}))
    assert scalar(target, count) == 2  # the 64th write dropped the two from a day ago
    host.close()


def test_an_unknown_operation_and_a_wrong_protocol_are_refused(host, target):
    assert (
        host.handle(
            target, "guard", "drop_table", {"protocol": 1}, idempotency_key=None
        )[0]
        == 404
    )
    status, body = host.handle(
        target, "nope", "open", {"protocol": 1}, idempotency_key=None
    )
    assert status == 404 and body["error_code"] == "UNKNOWN_OPERATION"
    for protocol in (2, True, None):
        status, body = call(host, target, "jobs", "open", protocol=protocol)
        assert status == 400 and body["error_code"] == "PROTOCOL_UNSUPPORTED"
    status, body = call(host, target, "jobs", "active", args={"surprise": 1})
    assert status == 422 and body["error"]["type"] == "WireError"
    status, body = call(host, target, "jobs", "open", open={"owner": "x"})
    assert status == 422
