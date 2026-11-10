"""PDM v1.9 changes 7 and 8: support and coaccess links, and re-open on a correction."""

from __future__ import annotations

import pytest

from pdm_memory import Memory

USER = "u"


@pytest.fixture
def mem(tmp_path):
    m = Memory(store=str(tmp_path / "l.db"), user=USER)
    yield m
    m.close()


def _save(mem, text, rests_on=None):
    return mem.save(text, tags=["a", "b", "c"], rests_on=rests_on)


def _status(mem, mid):
    return mem._storage.get(mid, user=USER).validation_status


def _links(mem, mid, kind):
    return mem._storage.links(USER, [mid], side="out", kind=kind)


def test_rests_on_writes_support_with_the_status_and_v_at_write(mem):
    a = _save(mem, "pressure reading jumped")
    mem.reinforce(a)  # VALIDATED, V = 2/3
    claim = _save(mem, "igniter fails by Friday", rests_on=[a, "no-such-id"])
    (link,) = _links(mem, claim, "support")
    assert (link.neighbor_id, link.status_at_write) == (a, "VALIDATED")
    assert link.v_at_write == pytest.approx(2 / 3, abs=1e-6)


def test_self_support_is_rejected(mem):
    a = _save(mem, "a")
    assert mem._write_support(a, [a]) == 0
    assert _links(mem, a, "support") == []


def test_touch_links_at_most_five_by_match_score(mem):
    ids = [_save(mem, f"row {i}") for i in range(7)]
    scores = {mid: float(i) for i, mid in enumerate(ids)}  # the last five score best
    assert mem.touch(ids, match_scores=scores) == 7
    linked = {lnk.pressure_id for mid in ids for lnk in _links(mem, mid, "coaccess")}
    assert linked == set(ids[2:])
    assert sum(len(_links(mem, mid, "coaccess")) for mid in ids) == 20


def test_touch_never_rewrites_support(mem):
    a = _save(mem, "a")
    b = _save(mem, "b", rests_on=[a])
    (before,) = _links(mem, b, "support")
    mem.reinforce(a)
    mem.touch([a, b])
    assert _links(mem, b, "support") == [before]
    assert [lnk.neighbor_id for lnk in _links(mem, b, "coaccess")] == [a]


def test_root_count_counts_direct_dependents(mem):
    a = _save(mem, "a")
    _save(mem, "b", rests_on=[a])
    c = _save(mem, "c", rests_on=[a])
    _save(mem, "d", rests_on=[c])
    assert mem.root_count(a) == 2


def test_a_correction_reopens_the_whole_chain(mem):
    a = _save(mem, "sensor reading")
    b = _save(mem, "claim", rests_on=[a])
    c = _save(mem, "work order", rests_on=[b])
    mem.reinforce(a)
    assert mem.penalize(a) == {b, c}
    assert (_status(mem, a), _status(mem, b), _status(mem, c)) == (
        "UNVALIDATED",
        "REOPENED",
        "REOPENED",
    )


def test_a_support_cycle_is_walked_once(mem):
    a = _save(mem, "a")
    b = _save(mem, "b", rests_on=[a])
    mem._write_support(a, [b])
    mem.reinforce(a)
    assert mem.penalize(a) == {b}


def test_coaccess_never_carries_a_correction(mem):
    a = _save(mem, "sensor")
    schedule = _save(mem, "valve schedule")
    mem.touch([a, schedule])
    mem.reinforce(a)
    assert mem.penalize(a) == set()
    assert _status(mem, schedule) == "UNVALIDATED"


def test_a_miss_on_an_unvalidated_row_reopens_nothing(mem):
    a = _save(mem, "a")
    b = _save(mem, "b", rests_on=[a])
    assert mem.penalize(a) == set()
    assert _status(mem, b) == "UNVALIDATED"


def test_reinforce_clears_reopened(mem):
    a = _save(mem, "a")
    b = _save(mem, "b", rests_on=[a])
    mem.reinforce(a)
    mem.penalize(a)
    assert _status(mem, b) == "REOPENED"
    mem.reinforce(b)
    assert _status(mem, b) == "VALIDATED"


def test_contrary_evidence_on_a_validated_row_reopens(mem):
    a = _save(mem, "User lives in Lviv")
    b = _save(mem, "Ship the parcel to Lviv", rests_on=[a])
    mem.reinforce(a)
    mem.apply_contrary_evidence(a, "User lives in Kyiv", evidence_tags=["x", "y", "z"])
    assert _status(mem, b) == "REOPENED"


def test_the_status_survives_a_json_round_trip(mem, tmp_path):
    a = _save(mem, "validated fact")
    mem.reinforce(a)
    path = tmp_path / "export.json"
    mem.export_json(path)
    with Memory(store=str(tmp_path / "copy.db"), user=USER) as copy:
        copy.import_json(path)
        (rec,) = copy._storage.list(user=USER)
        assert rec.validation_status == "VALIDATED"
