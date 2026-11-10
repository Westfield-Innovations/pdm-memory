"""PDM v1.9 change 4: the claim on a signature and coverage gaps."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest

from pdm_memory import Claim, Memory
from pdm_memory.core.signature import SignatureRecord
from pdm_memory.storage.sqlite_driver import SQLiteDriver

USER = "u"
NOW = datetime(2026, 11, 10, 12, tzinfo=timezone.utc)
FRIDAY = NOW + timedelta(days=4)


@pytest.fixture
def mem(tmp_path):
    m = Memory(store=str(tmp_path / "c.db"), user=USER)
    yield m
    m.close()


def _get(mem, mid):
    return mem._storage.get(mid, user=USER)


def test_a_claim_is_stored_with_its_columns(mem):
    claim = Claim("occurs", FRIDAY, outcome_source=" failure-feed ", watch_window_days=2.0)
    mid = mem.save("North igniter fails by Friday", tags=["north", "flare", "igniter"], claim=claim)
    rec = _get(mem, mid)
    assert (rec.direction, rec.due, rec.outcome_source, rec.watch_window) == (
        "occurs",
        FRIDAY,
        "failure-feed",
        2.0,
    )
    assert (rec.outcome, rec.verdict, rec.event_time, rec.report_time) == ("", "", None, None)


def test_a_claim_is_its_own_prediction(mem):
    plain = mem.save("Tank 3 holds pressure", tags=["tank", "pressure", "safety"])
    claim = mem.save(
        "Tank 3 holds pressure", tags=["tank", "pressure", "safety"], claim=Claim("absent", FRIDAY)
    )
    assert claim != plain
    assert _get(mem, claim).direction == "absent"


def test_a_claim_needs_an_aware_due(mem):
    with pytest.raises(ValueError, match="timezone"):
        Claim("occurs", datetime(2026, 11, 14))  # noqa: DTZ001
    assert mem.count() == 0


def test_a_claim_needs_a_known_direction():
    with pytest.raises(ValueError, match="occurs or is absent"):
        Claim("maybe", FRIDAY)  # type: ignore[arg-type]
    with pytest.raises(ValueError, match="negative"):
        Claim("occurs", FRIDAY, watch_window_days=-1)


def test_a_plain_save_carries_no_claim(mem):
    rec = _get(mem, mem.save("Plain fact", tags=["a", "b", "c"]))
    assert (rec.direction, rec.due, rec.outcome_source) == ("", None, "")


def test_a_coverage_gap_is_recorded(mem):
    assert mem.report_coverage_gap(" failure-feed ", NOW, FRIDAY) == 0
    rows = mem._storage._conn().execute(
        "SELECT user, source, gap_from, gap_to FROM pdm_coverage_gaps"
    ).fetchall()
    assert [tuple(r) for r in rows] == [(USER, "failure-feed", NOW.isoformat(), FRIDAY.isoformat())]


@pytest.mark.parametrize(
    ("source", "start", "end", "match"),
    [
        ("  ", NOW, FRIDAY, "names its source"),
        ("feed", datetime(2026, 11, 10), FRIDAY, "timezone"),  # noqa: DTZ001
        ("feed", FRIDAY, NOW, "ends after"),
    ],
)
def test_a_coverage_gap_is_checked(mem, source, start, end, match):
    with pytest.raises(ValueError, match=match):
        mem.report_coverage_gap(source, start, end)


def test_old_rows_read_as_plain_signatures(tmp_path):
    path = str(tmp_path / "old.db")
    d = SQLiteDriver(path)
    pid = d.save(SignatureRecord(user=USER, compressed_fact="old"))
    d._conn().execute("DROP INDEX idx_pdm_open_claims")
    for col in ("direction", "outcome_source", "due", "watch_window", "outcome", "verdict"):
        d._conn().execute(f"ALTER TABLE pdm_signatures DROP COLUMN {col}")
    d._conn().commit()
    d.close()
    d = SQLiteDriver(path)
    rec = d.get(pid, user=USER)
    assert (rec.direction, rec.due, rec.verdict) == ("", None, "")
    d.close()

