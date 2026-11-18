"""PDM v1.9 change 10: the carried number and the gate on it."""

from __future__ import annotations

import random
from datetime import datetime, timedelta, timezone

from pdm_memory import Memory
from pdm_memory.core.math import weight_of
from pdm_memory.core.signature import SignatureRecord
from pdm_memory.storage.schema import CARRIED_SQL
from pdm_memory.storage.sqlite_driver import SQLiteDriver

USER = "u"
NOW = datetime.now(tz=timezone.utc)


def test_carried_is_never_below_the_weight(tmp_path):
    rng = random.Random(7)
    driver = SQLiteDriver(str(tmp_path / "c.db"))
    recs = []
    for i in range(20):
        total = rng.randint(0, 6)
        rec = SignatureRecord(
            user=USER,
            compressed_fact=f"row {i}",
            p_magnitude=rng.uniform(1, 100),
            t_persistence=rng.choice([0.0, 3.0, 30.0]),
            domain=rng.choice(["insight", "warning", "core_fact", "market_signal"]),
            created_at=NOW - timedelta(days=rng.uniform(0, 400)),
            validation_prediction_total=total,
            validation_prediction_correct=rng.randint(0, total),
        )
        driver.save(rec)
        recs.append(rec)
    for rec in recs:
        carried = driver._conn().execute(
            f"SELECT {CARRIED_SQL} FROM pdm_signatures WHERE id = ?", (rec.id,)
        ).fetchone()[0]
        assert carried >= weight_of(rec, NOW)[0] - 1e-6
    driver.close()


def test_the_gate_reads_the_index(tmp_path):
    driver = SQLiteDriver(str(tmp_path / "c.db"))
    plan = driver._conn().execute(
        "EXPLAIN QUERY PLAN SELECT id FROM pdm_signatures "
        f"WHERE user = ? AND {CARRIED_SQL} >= ?",
        (USER, 30.0),
    ).fetchall()
    assert "idx_pdm_user_carried" in " ".join(str(tuple(r)) for r in plan)
    driver.close()


def test_a_grade_moves_the_carried_number(tmp_path):
    with Memory(store=str(tmp_path / "c.db"), user=USER) as mem:
        mid = mem.save("North flare igniter fails by Friday", p_magnitude=50)
        assert mem._storage.list(user=USER, min_carried=30) == []  # 50 × 0.5 = 25
        mem.reinforce(mid)
        mem.reinforce(mid)  # V = 3/4 → 37.5
        assert [r.id for r in mem._storage.list(user=USER, min_carried=30)] == [mid]
        assert mem._storage.count_below_carried(USER, 40) == 1
        assert mem._storage.count_below_carried(USER, 30) == 0
