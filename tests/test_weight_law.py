"""PDM v1.9 change 1 and 2: the scale and the weight law."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest

from pdm_memory import Memory
from pdm_memory.core.math import SEARCH_FLOOR, weight_of
from pdm_memory.core.retrieval import RetrievalEngine
from pdm_memory.core.signature import SignatureRecord

NOW = datetime(2026, 11, 9, 12, 0, tzinfo=timezone.utc)
HALF_LIFE = 30.0  # the "insight" domain


def _rec(**kw) -> SignatureRecord:
    kw.setdefault("compressed_fact", "pump note")
    kw.setdefault("domain", "insight")
    kw.setdefault("t_persistence", 0.0)
    return SignatureRecord(**kw)


def test_weight_is_p_times_v_times_fade():
    rec = _rec(p_magnitude=80, created_at=NOW - timedelta(days=10))
    weight, v, _ = weight_of(rec, NOW)
    assert v == 0.5
    assert weight == pytest.approx(80 * 0.5 * 0.5 ** (10 / HALF_LIFE))


def test_v_multiplies_once():
    rec = _rec(
        p_magnitude=80,
        created_at=NOW - timedelta(days=10),
        last_retrieved=NOW,
        validation_prediction_total=1,
    )
    day0 = weight_of(rec, NOW)[0]
    day1 = weight_of(rec, NOW + timedelta(days=1))[0]
    assert day0 == pytest.approx(80 / 3, abs=1e-4)
    assert day1 / day0 == pytest.approx(0.5 ** (1 / HALF_LIFE), abs=1e-5)


def test_clock_runs_from_the_later_of_created_and_used():
    created = NOW - timedelta(days=10)
    early_use = _rec(p_magnitude=80, created_at=created, last_retrieved=created - timedelta(days=20))
    no_use = _rec(p_magnitude=80, created_at=created)
    assert weight_of(early_use, NOW) == weight_of(no_use, NOW)


def test_grace_holds_the_fade_at_zero():
    rec = _rec(p_magnitude=80, t_persistence=30.0, created_at=NOW - timedelta(days=5))
    weight, _, decay = weight_of(rec, NOW)
    assert decay == 0.0
    assert weight == 40.0


def test_modifiers_stay_out_of_the_gate():
    # Weight 40 × 0.5 = 20. The old gate read 20 × quality 0.8 = 16 and dropped it.
    rec = _rec(p_magnitude=40, t_persistence=30.0, intent_tags=["valve", "pressure"])
    hits = RetrievalEngine().recall([rec], query="valve pressure", theta=20.0)
    assert [h.id for h in hits] == [rec.id]
    assert hits[0].p_effective == 20.0


def test_decay_at_reports_the_weight(tmp_path):
    with Memory(store=str(tmp_path / "w.db"), user="u") as mem:
        mid = mem.save("The north flare igniter is due for service", p_magnitude=80)
        assert mem.decay_at(mid).p_effective == pytest.approx(40.0)


def test_search_floor_is_a_tenth_of_full():
    assert SEARCH_FLOOR == 10.0
    assert RetrievalEngine().theta_floor == 10.0
