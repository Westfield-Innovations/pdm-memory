"""PDM v1.9 change 3: P at ingest, verdict counts at validate, the clock at touch."""

from __future__ import annotations

import threading

import pytest

from pdm_memory import Memory

USER = "u"


@pytest.fixture
def mem(tmp_path):
    m = Memory(store=str(tmp_path / "w.db"), user=USER)
    yield m
    m.close()


def _row(mem, mid):
    rec = mem._storage.get(mid, user=USER)
    return (
        rec.p_magnitude,
        rec.last_retrieved,
        rec.retrieval_count,
        rec.validation_prediction_correct,
        rec.validation_prediction_total,
    )


def _save(mem, text="North flare igniter fails by Friday"):
    return mem.save(text, tags=["north", "flare", "igniter"], p_magnitude=80)


def test_recall_writes_nothing(mem):
    mid = _save(mem)
    before = _row(mem, mid)
    assert [h.id for h in mem.recall("north flare igniter")] == [mid]
    mem.surface("north flare igniter")
    assert _row(mem, mid) == before


def test_recall_reinforce_true_only_touches(mem):
    mid = _save(mem)
    p, _, count, correct, total = _row(mem, mid)
    with pytest.warns(DeprecationWarning, match="touch"):
        mem.recall("north flare igniter", reinforce=True)
    p2, used, count2, correct2, total2 = _row(mem, mid)
    assert (p2, correct2, total2) == (p, correct, total)
    assert count2 == count + 1
    assert used is not None


def test_touch_moves_only_the_clock(mem):
    mid = _save(mem)
    p, _, _, correct, total = _row(mem, mid)
    assert mem.touch([mid, mid, "missing-id"]) == 1
    p2, used, count, correct2, total2 = _row(mem, mid)
    assert (p2, correct2, total2, count) == (p, correct, total, 1)
    assert used is not None


def test_reinforce_counts_a_correct_prediction_and_leaves_p(mem):
    mid = _save(mem)
    assert mem.reinforce(mid) == set()
    assert _row(mem, mid) == (80.0, None, 0, 1, 1)


def test_penalize_counts_a_miss_and_leaves_p(mem):
    mid = _save(mem)
    assert mem.penalize(mid) == set()
    assert _row(mem, mid) == (80.0, None, 0, 0, 1)


def test_contrary_evidence_leaves_p(mem):
    mid = _save(mem)
    result = mem.apply_contrary_evidence(mid, "The north flare igniter was replaced")
    assert result.p_after == result.p_before == 80.0
    assert _row(mem, mid) == (80.0, None, 0, 0, 1)


def test_a_duplicate_save_touches(mem):
    mid = _save(mem)
    assert _save(mem) == mid
    mem.save("North flare igniter fails by Friday", dedupe_reinforce=True)
    mem.save_many([{"text": "North flare igniter fails by Friday"}], dedupe_reinforce=True)
    assert _row(mem, mid)[2:] == (2, 0, 0)


def test_concurrent_reinforce_loses_no_count(tmp_path):
    path = str(tmp_path / "c.db")
    with Memory(store=path, user=USER) as setup:
        mid = _save(setup)
    errors: list[BaseException] = []

    def work():
        try:
            with Memory(store=path, user=USER) as m:
                for _ in range(25):
                    m.reinforce(mid)
        except BaseException as exc:  # noqa: BLE001
            errors.append(exc)

    threads = [threading.Thread(target=work) for _ in range(8)]
    for t in threads:
        t.start()
    for t in threads:
        t.join(timeout=60)
    assert not errors
    with Memory(store=path, user=USER) as check:
        assert _row(check, mid)[3:] == (200, 200)


def test_update_refuses_p(mem):
    mid = _save(mem)
    before = _row(mem, mid)
    with pytest.raises(ValueError, match="ingest"):
        mem.update(mid, p_magnitude=90)
    assert _row(mem, mid) == before


@pytest.mark.parametrize(
    "field, value",
    [
        ("p_magnitude", 90.0),
        ("effective_spike", 90.0),
        ("validation_prediction_correct", 5),
        ("validation_prediction_total", 5),
        ("retrieval_count", 5),
        ("last_retrieved", "2026-01-01T00:00:00+00:00"),
    ],
)
def test_a_batch_update_writes_none_of_the_three_numbers(mem, field, value):
    mid = _save(mem)
    before = _row(mem, mid)
    counts = mem.update_many([{"id": mid, field: value}])
    assert counts == {"updated": 0, "skipped": 0, "errors": 1}
    assert _row(mem, mid) == before


def test_a_batch_update_still_writes_other_fields(mem):
    mid = _save(mem)
    before = _row(mem, mid)
    assert mem.update_many([{"id": mid, "text": "North flare igniter replaced"}])["updated"] == 1
    assert mem._storage.get(mid, user=USER).compressed_fact == "North flare igniter replaced"
    assert _row(mem, mid) == before
