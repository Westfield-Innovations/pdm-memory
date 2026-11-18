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


# --- retrieve and search --------------------------------------------------

from datetime import timedelta  # noqa: E402

from pdm_memory import Claim, Memory  # noqa: E402

USER = "u"


@pytest.fixture
def mem(tmp_path):
    m = Memory(store=str(tmp_path / "r.db"), user=USER)
    yield m
    m.close()


def _pump(mem, i, p=80, **kw):
    # Fresh, inside grace: weight = P × 0.5.
    return mem.save(f"Pump note {i}", tags=["pump", "note", f"n{i}"], p_magnitude=p, **kw)


def _snapshot(mem):
    return [
        (r.id, r.p_magnitude, r.last_retrieved, r.retrieval_count, r.validation_prediction_total)
        for r in mem._storage.list(user=USER, limit=1000)
    ]


def test_retrieve_writes_nothing(mem):
    for i in range(3):
        _pump(mem, i)
    before = _snapshot(mem)
    assert mem.retrieve("pump note").hits
    mem.search("pump note", min_p_lowered=12)
    assert _snapshot(mem) == before


def test_one_theta_at_every_gate(mem):
    quiet = _pump(mem, 1, p=26)  # weight 13: under the default 30, over 12
    assert mem.retrieve("pump note").hits == []
    result = mem.search("pump note", min_p_lowered=12)
    assert result.theta == 12
    assert [h.id for h in result.hits] == [quiet]


def test_search_never_goes_below_the_floor_without_an_area(mem):
    assert mem.search("pump", min_p_lowered=2).theta == 10
    assert mem.search("pump", min_p_lowered=2, area="general").theta == 2


@pytest.mark.parametrize(("tagged", "routes"), [(30, ["tags", "listing"]), (60, ["tags"])])
def test_a_budget_stop_says_so_and_resumes(mem, tagged, routes):
    for i in range(tagged):
        _pump(mem, i, p=60 + i % 30)
    for i in range(5):
        mem.save(f"Valve log {i}", tags=["valve", "log", f"v{i}"], p_magnitude=90)
    whole = mem.retrieve("pump note", k=1000, budget=1000)
    assert whole.completed and whole.routes == routes
    seen, token, calls, first_as_of = [], None, 0, None
    while True:
        part = mem.retrieve("pump note", k=1000, budget=10, continuation=token)
        calls += 1
        first_as_of = first_as_of or part.as_of
        assert part.as_of == first_as_of
        seen += [h.id for h in part.hits]
        if part.completed:
            break
        assert part.continuation and part.evaluated == 10
        token = part.continuation
        mem.touch([seen[0]])  # a use between pages changes nothing under one as-of
    assert calls > 1
    assert len(seen) == len(set(seen))
    assert set(seen) == {h.id for h in whole.hits}


def test_a_foreign_continuation_is_refused(mem):
    with pytest.raises(ValueError, match="continuation"):
        mem.retrieve("pump", continuation="2026-11-10T00:00:00+00:00|X|abc")


def test_a_bare_signature_is_standalone_and_unvalidated_for_action(mem):
    a = _pump(mem, 1)
    mem.reinforce(a)
    (hit,) = mem.retrieve("pump note").hits
    assert (hit.validation_status, hit.context, hit.support_in_doubt) == (
        "VALIDATED",
        "STANDALONE",
        False,
    )
    assert hit.action_status == "UNVALIDATED"


def test_a_loud_neighbour_makes_recovery(mem):
    loud = mem.save("Sensor read 9 bar", tags=["sensor", "read", "bar"], p_magnitude=90)
    quiet = mem.save("Old sensor note", tags=["sensor", "old", "note"], p_magnitude=20)
    a = _pump(mem, 1, rests_on=[loud])
    b = _pump(mem, 2, rests_on=[quiet])
    mem.reinforce(a)
    hits = {h.id: h for h in mem.retrieve("pump note").hits}
    assert hits[a].context == "RECOVERY" and hits[a].action_status == "VALIDATED"
    assert hits[b].context == "STANDALONE"


def test_support_in_doubt_when_a_support_reopens(mem):
    root = mem.save("Root reading", tags=["root", "reading", "x"], p_magnitude=90)
    support = mem.save("Derived reading", tags=["derived", "reading", "y"], p_magnitude=90, rests_on=[root])
    a = _pump(mem, 1, rests_on=[support])
    mem.reinforce(root)
    mem.penalize(root)  # support is REOPENED now
    (hit,) = mem.retrieve("pump note").hits
    assert hit.id == a and hit.support_in_doubt is True
    assert hit.action_status == "UNVALIDATED"


def test_support_in_doubt_when_a_support_v_falls(mem):
    support = mem.save("Sensor read 9 bar", tags=["sensor", "read", "bar"], p_magnitude=90)
    mem.reinforce(support)  # V 2/3 at the link
    a = _pump(mem, 1, rests_on=[support])
    (hit,) = mem.retrieve("pump note").hits
    assert hit.id == a and hit.support_in_doubt is False
    mem._storage.increment(support, USER, {"validation_prediction_total": 1})  # V 1/2
    (hit,) = mem.retrieve("pump note").hits
    assert hit.support_in_doubt is True


def test_a_claim_carries_its_verdict_due_and_flags(mem):
    now = datetime.now(tz=timezone.utc)
    owl = _pump(mem, 1, claim=Claim("occurs", now - timedelta(hours=1), outcome_source="feed"))
    duck = _pump(mem, 2, claim=Claim("absent", now + timedelta(days=3)))
    hits = {h.id: h for h in mem.retrieve("pump note").hits}
    assert (hits[owl].owl, hits[owl].duck, hits[owl].direction) == (True, False, "occurs")
    assert (hits[duck].owl, hits[duck].duck, hits[duck].verdict) == (False, True, "")
    assert hits[duck].action_status == "UNVALIDATED"
    body = mem.retrieve("pump note").as_dict()
    assert {h["direction"] for h in body["hits"]} == {"occurs", "absent"}


def test_the_scope_names_the_listing_route(mem):
    _pump(mem, 1)
    result = mem.retrieve("nothing tagged like this")
    assert result.routes == ["tags", "listing"]
    assert mem.retrieve("", area="general").routes == ["area", "listing"]
