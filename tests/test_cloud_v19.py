"""PDM v1.9 on the cloud store: the new calls go to the server's endpoints (no network)."""

from __future__ import annotations

import base64
import json
import time
from datetime import datetime, timedelta, timezone

import httpx
import pytest
import respx

from pdm_memory import Claim, Memory
from pdm_memory.auth.jwt_handler import JWTAuth
from pdm_memory.core.signature import SignatureRecord
from pdm_memory.storage.cloud_driver import CloudDriver

BASE = "http://cloud.test"
API = f"{BASE}/api/v1/pdm"
USER = "default"
NOW = datetime(2026, 11, 10, 12, tzinfo=timezone.utc)


def _driver() -> CloudDriver:
    claims = base64.urlsafe_b64encode(json.dumps({"exp": time.time() + 3600}).encode())
    return CloudDriver(auth=JWTAuth(token=f"x.{claims.decode().rstrip('=')}.y"), base_url=BASE)


def _row(sig_id: str, **extra) -> dict:
    rec = SignatureRecord(
        id=sig_id,
        compressed_fact=f"North flare note {sig_id}",
        intent_tags=["north", "flare", "note"],
        p_magnitude=80,
        created_at=NOW,
    )
    return {**CloudDriver._record_to_payload(rec), **extra}


@pytest.fixture
def router():
    with respx.mock(assert_all_called=False) as r:
        yield r


@pytest.fixture
def mem():
    m = Memory(storage=_driver(), user=USER)
    yield m
    m.close()


def test_cloud_touch_posts_the_ids(mem, router):
    route = router.post(f"{API}/touch").mock(return_value=httpx.Response(200, json={"touched": 2}))
    patch = router.patch(url__startswith=f"{API}/signatures/")
    assert mem.touch(["a", "b", "a", ""]) == 2
    assert json.loads(route.calls.last.request.content) == {"ids": ["a", "b"]}
    assert patch.call_count == 0


def test_cloud_save_sends_the_claim_and_support(mem, router):
    router.post(f"{API}/signatures/batch-get").mock(
        return_value=httpx.Response(200, json={"signatures": [_row("s1")]})
    )
    ingest = router.post(f"{API}/ingest").mock(return_value=httpx.Response(201, json={"id": "c1"}))
    claim = Claim("absent", NOW + timedelta(days=4), outcome_source="pressure-feed", watch_window_days=1.5)
    mid = mem.save(
        "Tank 3 will not overpressure",
        tags=["tank", "pressure", "safety"],
        claim=claim,
        rests_on=["s1", "unknown"],
        dedupe=False,
    )
    assert mid == "c1"
    body = json.loads(ingest.calls.last.request.content)
    assert (body["direction"], body["due"], body["outcome_source"], body["watch_window_days"]) == (
        "absent",
        (NOW + timedelta(days=4)).isoformat(),
        "pressure-feed",
        1.5,
    )
    assert body["rests_on"] == ["s1"]


def test_a_plain_cloud_save_sends_no_claim(mem, router):
    ingest = router.post(f"{API}/ingest").mock(return_value=httpx.Response(201, json={"id": "p1"}))
    mem.save("Plain fact", tags=["a", "b", "c"], dedupe=False)
    body = json.loads(ingest.calls.last.request.content)
    assert not {"direction", "due", "outcome_source", "watch_window_days", "rests_on"} & set(body)


def test_cloud_gap_and_silent_count(mem, router):
    gaps = router.post(f"{API}/coverage-gaps").mock(
        return_value=httpx.Response(201, json={"amended": 3})
    )
    silent = router.get(f"{API}/count-silent").mock(
        return_value=httpx.Response(200, json={"theta": 30.0, "silent": 7})
    )
    assert mem.report_coverage_gap("failure-feed", NOW - timedelta(hours=6), NOW) == 3
    assert json.loads(gaps.calls.last.request.content) == {
        "source": "failure-feed",
        "from": (NOW - timedelta(hours=6)).isoformat(),
        "to": NOW.isoformat(),
    }
    assert mem.count_silent(30, as_of=NOW) == 7
    assert silent.calls.last.request.url.params["theta"] == "30"


def test_cloud_observation_posts_to_the_claim(mem, router):
    route = router.post(f"{API}/claims/c1/observe").mock(
        return_value=httpx.Response(
            200, json={"id": "c1", "verdict": "late", "changed": True, "reopened": 2}
        )
    )
    result = mem.validate_claim("c1", "occurred", event_time=NOW)
    assert (result.verdict, result.reopened) == ("late", 2)
    assert json.loads(route.calls.last.request.content) == {
        "outcome": "occurred",
        "event_time": NOW.isoformat(),
    }


def test_cloud_observation_that_changes_nothing_is_none(mem, router):
    router.post(f"{API}/claims/c1/observe").mock(
        return_value=httpx.Response(
            200, json={"id": "c1", "verdict": "on_time", "changed": False, "reopened": 0}
        )
    )
    assert mem.validate_claim("c1", "occurred") is None


def test_the_cloud_runs_its_own_settlement_pass(mem):
    with pytest.raises(RuntimeError, match="own cycle"):
        mem.settle_due_claims(NOW)


def test_a_cloud_row_reads_its_status_and_claim_back():
    rec = SignatureRecord(
        id="c1",
        compressed_fact="Tank 3 holds",
        intent_tags=["tank", "pressure", "safety"],
        direction="absent",
        due=NOW,
        outcome_source="pressure-feed",
        watch_window=2.0,
    )
    back = CloudDriver._payload_to_record(
        {**CloudDriver._record_to_payload(rec), "validation_status": "VALIDATED"}
    )
    assert (back.validation_status, back.direction, back.due, back.outcome_source, back.watch_window) == (
        "VALIDATED",
        "absent",
        NOW,
        "pressure-feed",
        2.0,
    )
    plain = CloudDriver._payload_to_record(_row("p1"))
    assert (plain.validation_status, plain.direction, plain.due) == ("UNVALIDATED", "", None)


def test_cloud_retrieve_writes_nothing_and_is_unvalidated_for_action(mem, router):
    router.get(f"{API}/signatures").mock(
        return_value=httpx.Response(
            200, json={"signatures": [_row("s1", validation_status="VALIDATED")]}
        )
    )
    writes = [
        router.patch(url__startswith=f"{API}/signatures/"),
        router.post(f"{API}/signatures/batch-update"),
        router.post(f"{API}/touch"),
    ]
    result = mem.retrieve("north flare", as_of=NOW + timedelta(hours=1))
    (hit,) = result.hits
    assert (hit.validation_status, hit.context, hit.support_in_doubt) == ("VALIDATED", None, None)
    assert hit.action_status == "UNVALIDATED"
    mem.recall("north flare")
    assert [w.call_count for w in writes] == [0, 0, 0]
