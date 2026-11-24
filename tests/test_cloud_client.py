"""CloudLedgers: the HTTP transport, retries, error mapping, and the in-process pairing."""

from __future__ import annotations

import base64
import json
import re
import time

import httpx
import pytest

from pdm_memory import CloudLedgers
from pdm_memory.auth.jwt_handler import JWTAuth
from pdm_memory.cloud_ledgers import client
from pdm_memory.cloud_ledgers.host import LedgerHost
from pdm_memory.cloud_ledgers.testing import in_process
from pdm_memory.guarded_effects import IdReuseError
from pdm_memory.storage.cloud_driver import CloudDriver
from pdm_memory.storage.errors import CloudConflictError, CloudStorageError
from tests.test_cloud_host import GUARD

URL = "https://api.azus.ai/api/v1/pdm/ledgers/default/guard/stage/"
OK = {"protocol": 1, "result": {"x": 1}}


@pytest.fixture
def auth():
    payload = (
        base64.urlsafe_b64encode(json.dumps({"exp": time.time() + 3600}).encode())
        .decode()
        .rstrip("=")
    )
    return JWTAuth(token=f"x.{payload}.y")


@pytest.fixture
def ledgers(auth):
    return CloudLedgers(auth)


@pytest.fixture(autouse=True)
def no_sleep(monkeypatch):
    monkeypatch.setattr(client, "_sleep", lambda s: None)


def stage(ledgers):
    return ledgers.call("guard", "stage", GUARD, {"request": {}})


def test_a_call_posts_to_the_ledger_route_with_protocol_and_key(respx_mock, ledgers):
    route = respx_mock.post(URL).respond(200, json=OK)
    assert stage(ledgers) == OK
    request = route.calls[0].request
    assert request.headers["X-PDM-Ledger-Protocol"] == "1"
    assert re.fullmatch(r"[0-9a-f]{32}", request.headers["Idempotency-Key"])
    assert request.headers["Authorization"].startswith("Bearer ")
    assert json.loads(request.content)["protocol"] == 1


def test_a_401_refreshes_the_token_once(respx_mock, ledgers, auth, monkeypatch):
    route = respx_mock.post(URL).mock(
        side_effect=[httpx.Response(401), httpx.Response(200, json=OK)]
    )
    refreshed = []
    monkeypatch.setattr(auth, "refresh", lambda: refreshed.append(1) or True)
    assert stage(ledgers) == OK
    assert len(route.calls) == 2 and refreshed == [1]


def test_a_503_is_retried_with_the_same_key(respx_mock, ledgers):
    route = respx_mock.post(URL).mock(
        side_effect=[
            httpx.Response(503),
            httpx.ConnectError("down"),
            httpx.Response(200, json=OK),
        ]
    )
    assert stage(ledgers) == OK
    assert len({c.request.headers["Idempotency-Key"] for c in route.calls}) == 1
    assert len(route.calls) == 3


def test_a_read_sends_no_key(respx_mock, ledgers):
    route = respx_mock.post(URL.replace("stage", "receipt")).respond(200, json=OK)
    ledgers.call("guard", "receipt", GUARD, {"action_id": "a"})
    assert "Idempotency-Key" not in route.calls[0].request.headers


def test_retries_stop_at_the_deadline(respx_mock, auth, monkeypatch):
    clock = iter(range(0, 1000, 10))
    monkeypatch.setattr(client, "_monotonic", lambda: float(next(clock)))
    route = respx_mock.post(URL).respond(503)
    with pytest.raises(CloudStorageError) as err:
        stage(CloudLedgers(auth, retry_deadline_s=25.0))
    assert err.value.status_code == 503 and 1 < len(route.calls) < 5


def test_a_transport_failure_past_the_deadline_keeps_its_cause(
    respx_mock, auth, monkeypatch
):
    clock = iter(range(0, 1000, 10))
    monkeypatch.setattr(client, "_monotonic", lambda: float(next(clock)))
    respx_mock.post(URL).mock(side_effect=httpx.ReadTimeout("slow"))
    with pytest.raises(CloudStorageError) as err:
        stage(CloudLedgers(auth, retry_deadline_s=25.0))
    assert isinstance(err.value.__cause__, httpx.ReadTimeout)


def test_a_ledger_error_body_raises_the_ledger_class(respx_mock, ledgers):
    body = {
        "error_code": "LEDGER_ERROR",
        "detail": "reused",
        "error": {"type": "IdReuseError", "message": "reused"},
    }
    respx_mock.post(URL).respond(409, json=body)
    with pytest.raises(IdReuseError, match="reused"):
        stage(ledgers)


def test_other_refusals_map_to_cloud_errors(respx_mock, ledgers):
    respx_mock.post(URL).respond(409, json={"error_code": "NAMESPACE_LIMIT"})
    with pytest.raises(CloudConflictError) as conflict:
        stage(ledgers)
    assert conflict.value.error_code == "NAMESPACE_LIMIT"
    respx_mock.post(URL).respond(404, json={"error_code": "NAMESPACE_NOT_FOUND"})
    with pytest.raises(CloudStorageError) as missing:
        stage(ledgers)
    assert missing.value.status_code == 404


def test_a_foreign_protocol_in_the_reply_is_refused(respx_mock, ledgers):
    respx_mock.post(URL).respond(200, json={"protocol": 2, "result": {}})
    with pytest.raises(CloudStorageError):
        stage(ledgers)


def test_a_bad_namespace_is_refused_before_any_request(respx_mock, auth):
    for bad in ("a/b", "", "x" * 65):
        with pytest.raises(ValueError):
            CloudLedgers(auth, namespace=bad)
    assert not respx_mock.calls


def test_it_needs_an_auth_or_a_transport():
    with pytest.raises(ValueError):
        CloudLedgers()


def test_from_driver_reuses_the_drivers_auth_and_url(respx_mock, auth):
    driver = CloudDriver(auth=auth, base_url="http://localhost:8000/", timeout=3.0)
    route = respx_mock.post(
        "http://localhost:8000/api/v1/pdm/ledgers/ns1/guard/stage/"
    ).respond(200, json=OK)
    assert stage(CloudLedgers.from_driver(driver, namespace="ns1")) == OK
    assert route.called


def test_in_process_runs_a_guard_open_then_start_branch_and_a_read(tmp_path):
    ledgers = in_process(tmp_path / "l.db")
    opened = ledgers.call("guard", "open", GUARD, {})
    assert opened["protocol"] == 1
    ledgers.call("guard", "start_branch", GUARD, {"branch_id": "b1"})
    assert ledgers.call("guard", "history", GUARD, {})["result"]


def test_in_process_raises_the_ledger_class_for_a_ledger_error(tmp_path):
    ledgers = in_process(tmp_path / "l.db")
    ledgers.call("guard", "open", GUARD, {})
    with pytest.raises(KeyError):
        ledgers.call("guard", "receipt", GUARD, {"action_id": "nope"})


def test_a_dropped_response_is_replayed_under_the_same_key(tmp_path, monkeypatch):
    seen = []
    real = LedgerHost.handle

    def spy(self, target, kind, op, body, *, idempotency_key):
        result = real(self, target, kind, op, body, idempotency_key=idempotency_key)
        seen.append((op, idempotency_key, result))
        return result

    monkeypatch.setattr(LedgerHost, "handle", spy)
    ledgers = in_process(tmp_path / "l.db", drop_response_after=["start_branch"])
    ledgers.call("guard", "open", GUARD, {})
    reply = ledgers.call("guard", "start_branch", GUARD, {"branch_id": "b1"})
    sent = [(key, result) for op, key, result in seen if op == "start_branch"]
    assert len(sent) == 2 and sent[0][0] == sent[1][0]
    assert sent[0][1] == sent[1][1] and reply == sent[1][1][1]
    history = ledgers.call("guard", "history", GUARD, {})["result"]
    assert [e["kind"] for e in history].count("BRANCH_STARTED") == 1
