"""The wire codec: every ledger record survives JSON, hostile input is refused."""

from __future__ import annotations

import json
from dataclasses import replace
from datetime import datetime
from decimal import Decimal
from typing import Any, Literal

import pytest

from pdm_memory import ConsequenceLedger
from pdm_memory.cloud_ledgers.wire import (
    ERRORS,
    WireError,
    decode,
    encode,
    error_response,
    raise_error,
)
from pdm_memory.consequence import (
    Account,
    Cause,
    CauseHypothesis,
    ConsequenceEntry,
    EvidenceKind,
    QuantityStatus,
    Route,
)
from pdm_memory.consequence.records import Expectation
from pdm_memory.goal_loop.checker import ProofReceipt, ProofVerdict
from pdm_memory.goal_loop.executor import ActionResult
from pdm_memory.goal_loop.guard import GuardDecision
from pdm_memory.goal_loop.job import ProposedStep
from pdm_memory.goal_loop.states import JobState
from pdm_memory.goal_loop.testing import _job, _receipt
from pdm_memory.guarded_effects import CheckResult, ConditionVerdict
from pdm_memory.storage.errors import CloudStorageError
from tests.consequence_fixtures import (
    COMMIT,
    END,
    HORIZON,
    MOVED,
    POLICY,
    POSITION_MOVED,
    ROOM_TEMP,
    TASK,
    TEMP,
    VALVE,
    _guard,
    _request,
)
from tests.ledger_target import ledger_target
from tests.test_goal_contract import T0, contract
from tests.test_guarded_effects import guard, request


@pytest.fixture(scope="module")
def records(tmp_path_factory):
    tmp = tmp_path_factory.mktemp("wire")
    out = {}
    with (
        ConsequenceLedger(
            ledger_target(tmp, "c.db"),
            owner="owner-1",
            policy=POLICY,
            clock=lambda: COMMIT,
        ) as led,
        _guard(tmp, check=led.prepared_check) as g,
    ):
        g.stage(_request())
        led.prepare(VALVE)
        receipt = g.release("act-valve-1")
        led.attach_action("exp-valve-1", receipt)
        led.observe(POSITION_MOVED)
        led.observe(replace(TEMP, value=18.0))
        causes = (CauseHypothesis(Cause.EXECUTION, ("obs-pos",)),)
        ev = led.evaluate("exp-valve-1", task=TASK, causes=causes)
        for n, (account, qty) in enumerate(
            [
                (Account.AGENT_OPERATION, "2.5"),
                (Account.PRINCIPAL_BURDEN, "1"),
                (Account.AGENT_OPERATION, None),
            ]
        ):
            led.record_consequence(
                ConsequenceEntry(
                    f"e-{n}",
                    "exp-valve-1",
                    account,
                    "room 4",
                    None if qty is None else Decimal(qty),
                    "s",
                    END,
                    "controller-log",
                    QuantityStatus.UNKNOWN if qty is None else QuantityStatus.MEASURED,
                )
            )
        out.update(
            receipt=receipt,
            evaluation=ev,
            finding=ev.findings[0],
            proposal=led.propose_learning(
                ev.evaluation_id,
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
            ),
            summary=led.accounts("exp-valve-1"),
            significance=led.significance("exp-valve-1"),
            entry=ConsequenceEntry(
                "e-x",
                "exp-valve-1",
                Account.PRINCIPAL_BURDEN,
                "r",
                Decimal("1.5"),
                "s",
                END,
                "log",
                QuantityStatus.MEASURED,
            ),
        )
        out["suggestions"] = led.suggest_observation("exp-valve-1")
        out["forecast"] = led.forecast_record("valve-model")
    path = ledger_target(tmp, "g.db")
    with guard(path, lambda r: {"accepted": True}) as g:
        g.checkpoint("cp1", branch_id="b1", state={"step": 0})
        g.stage(request())
        g.release("a1")
        out["recovery"] = g.recover("cp1", new_branch_id="b2")
    return out


def _flat(records):
    for key, value in records.items():
        if isinstance(value, (tuple, list)):
            yield from ((f"{key}{i}", v) for i, v in enumerate(value))
        else:
            yield key, value


STATIC = {
    "expectation": VALVE,
    "observation": POSITION_MOVED,
    "policy": POLICY,
    "task": TASK,
    "criterion_a": MOVED,
    "criterion_b": ROOM_TEMP,
    "horizon": HORIZON,
    "request": _request(),
    "verdict": ConditionVerdict(
        CheckResult.SATISFIED,
        "ok",
        {"a": [1, 2.5]},
    ),
    "contract": contract(),
    "job": _job(),
    "job_step": _job(
        step=ProposedStep("a1", 1, "sms.send", "+1", "text", {"p": 1}),
        state=JobState.ARMED,
    ),
    "step_receipt": _receipt(_job(), JobState.DRAFTED, None),
    "proof_receipt": ProofReceipt("src", "ref", T0, {"x": 1}),
    "proof_verdict": ProofVerdict("PROVEN", "seen", ProofReceipt("src", "ref", T0)),
    "action_result": ActionResult("m-1", Decimal("0.02"), {"ok": True}),
    "guard_decision": GuardDecision("proceed", "fine", {"k": 1}),
}


def test_every_record_survives_a_json_round_trip(records):
    checked = 0
    for name, record in [*STATIC.items(), *_flat(records)]:
        back = decode(type(record), json.loads(json.dumps(encode(record))))
        assert back == record, name
        checked += 1
    assert checked > 25


def test_tuple_keys_travel_as_pairs(records):
    wire = json.loads(json.dumps(encode(records["summary"])))
    assert isinstance(wire["totals"], list) and wire["totals"][0][0][0] in {
        a.value for a in Account
    }
    assert records["summary"].totals


def test_encode_matches_the_stored_form():
    from pdm_memory.consequence.records import _plain

    for record in (VALVE, POSITION_MOVED, POLICY, TASK):
        assert encode(record) == _plain(record)


TS = "2026-11-12T09:00:00+00:00"
OBS = encode(POSITION_MOVED)


def _nested(n):
    value = 1
    for _ in range(n):
        value = [value]
    return value


@pytest.mark.parametrize(
    "tp, data",
    [
        (datetime, "2026-11-12T09:00:00"),
        (datetime, "not a date"),
        (int, True),
        (float, True),
        (float, float("nan")),
        (float, float("inf")),
        (Decimal, "NaN"),
        (str, 3),
        (bool, 1),
        (type(POSITION_MOVED), {**OBS, "surprise": 1}),
        (type(POSITION_MOVED), {k: v for k, v in OBS.items() if k != "source"}),
        (type(POSITION_MOVED), {**OBS, "kind": "NOT_A_KIND"}),
        (type(POSITION_MOVED), {**OBS, "event_time": "2026-11-12T09:00:00"}),
        (type(POSITION_MOVED), {**OBS, "revision": True}),
        (type(POSITION_MOVED), {**OBS, "revision": 0}),
        (type(POSITION_MOVED), []),
        (Literal["a", "b"], "c"),
        (Literal[1], True),
        (tuple[int, int], [1]),
        (tuple[int, ...], [1, "2"]),
        (dict[str, int], {"a": 1.5}),
        (dict[tuple[str, int], int], [[["a", 1]]]),
        (list[int], _nested(33)),
        (Any, _nested(40)),
        (Any, [float("nan")]),
        (int | None, "x"),
    ],
)
def test_untrusted_input_is_refused(tp, data):
    with pytest.raises(WireError):
        decode(tp, data)


def test_unsupported_types_are_a_programming_error():
    with pytest.raises(TypeError):
        decode(complex, 1)
    assert decode(float, 3) == 3.0 and decode(int | None, None) is None
    assert decode(Expectation, encode(VALVE)) == VALVE


def make(name):
    cls = ERRORS[name][0]
    return KeyError("missing thing") if cls is KeyError else cls("the message")


@pytest.mark.parametrize("name", sorted(ERRORS))
def test_each_registered_error_comes_back_as_its_own_class(name):
    status, body = error_response(make(name))
    assert status == ERRORS[name][1]
    with pytest.raises(ERRORS[name][0]) as caught:
        raise_error(json.loads(json.dumps(body)))
    assert type(caught.value) is ERRORS[name][0]
    assert body["error"] == {"type": name, "message": str(caught.value.args[0])}


def test_a_subclass_reports_its_nearest_registered_class():
    class Mine(KeyError):
        pass

    assert error_response(Mine("k"))[1]["error"]["type"] == "KeyError"
    assert error_response(RuntimeError("x")) is None


def test_an_unregistered_error_type_never_imports_anything():
    for body in (
        {"error": {"type": "os.system", "message": "x"}},
        {"error": {"type": "OSError", "message": "x"}},
        {"error": "boom"},
        {},
    ):
        with pytest.raises(CloudStorageError):
            raise_error(body)
