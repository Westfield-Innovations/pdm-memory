"""PDM v1.9 changes 9, 12 and 14: retrieve and search, in order, with their flags."""

from __future__ import annotations

from datetime import datetime, timezone

import pytest

from pdm_memory.core.signature import MemoryHit, SignatureRecord
from pdm_memory.models import RetrieveResult

NOW = datetime(2026, 11, 10, 12, tzinfo=timezone.utc)


def _hit(**flags) -> MemoryHit:
    rec = SignatureRecord(compressed_fact="x", validation_status="VALIDATED")
    hit = MemoryHit.from_record(rec, 40.0, 0.0, 1.0, 0.75)
    for name, value in flags.items():
        setattr(hit, name, value)
    return hit


@pytest.mark.parametrize(
    ("flags", "status"),
    [
        ({"context": "RECOVERY", "support_in_doubt": False}, "VALIDATED"),
        ({"context": "STANDALONE", "support_in_doubt": False}, "UNVALIDATED"),
        ({"context": "RECOVERY", "support_in_doubt": True}, "UNVALIDATED"),
        ({"context": "RECOVERY", "support_in_doubt": False, "duck": True}, "UNVALIDATED"),
        ({}, "UNVALIDATED"),  # not computed is not a clearance
    ],
)
def test_action_status_is_unvalidated_whenever_a_flag_says_so(flags, status):
    assert _hit(**flags).action_status == status


def test_a_hit_carries_its_status_and_claim():
    rec = SignatureRecord(validation_status="REOPENED", direction="occurs", due=NOW, verdict="")
    hit = MemoryHit.from_record(rec, 40.0, 0.0, 1.0, 0.5)
    assert (hit.validation_status, hit.direction, hit.due, hit.verdict) == (
        "REOPENED",
        "occurs",
        NOW,
        "",
    )


def test_a_result_states_its_scope():
    result = RetrieveResult(
        hits=[_hit(context="RECOVERY", support_in_doubt=False)],
        routes=["tags"],
        area=None,
        theta=30.0,
        as_of=NOW,
        completed=False,
        continuation="tok",
        evaluated=200,
    )
    body = result.as_dict()
    assert body["scope"] == {"routes": ["tags"], "area": None, "theta": 30.0, "as_of": NOW.isoformat()}
    assert (body["completed"], body["continuation"], body["evaluated"]) == (False, "tok", 200)
    assert body["hits"][0]["action_status"] == "VALIDATED"
    assert "direction" not in body["hits"][0]
