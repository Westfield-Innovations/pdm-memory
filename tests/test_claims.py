"""PDM v1.9 changes 4 and 6: the claim on a signature, coverage gaps, the owl and the duck."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest

from pdm_memory import Claim, Memory
from pdm_memory.claims import is_duck, is_owl
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


def _claim_rec(direction, *, due=FRIDAY, verdict="", outcome=""):
    return SignatureRecord(direction=direction, due=due, verdict=verdict, outcome=outcome)


def test_an_occurs_claim_past_due_with_nothing_seen_is_an_owl():
    after = FRIDAY + timedelta(hours=1)
    assert is_owl(_claim_rec("occurs"), after)
    assert is_owl(_claim_rec("occurs", verdict="not_by_due"), after)
    assert is_owl(_claim_rec("occurs", verdict="expired_unobserved"), after)
    assert not is_owl(_claim_rec("occurs", verdict="on_time"), after)


def test_an_occurs_claim_before_due_is_not_an_owl():
    assert not is_owl(_claim_rec("occurs"), NOW)


def test_an_absent_claim_without_an_observation_is_a_duck():
    assert is_duck(_claim_rec("absent"))
    assert is_duck(_claim_rec("absent", verdict="not_by_due"))  # a missing report, not a look
    assert not is_duck(_claim_rec("absent", outcome="observed_absent"))
    assert not is_duck(_claim_rec("absent", outcome="occurred"))


def test_a_plain_signature_is_neither():
    rec = SignatureRecord()
    assert not is_owl(rec, NOW + timedelta(days=999))
    assert not is_duck(rec)


def test_a_claim_survives_a_json_round_trip(mem, tmp_path):
    claim = Claim("absent", FRIDAY, outcome_source="pressure-feed", watch_window_days=1.5)
    mem.save("Tank 3 will not overpressure this week", tags=["tank", "pressure", "safety"], claim=claim)
    path = tmp_path / "export.json"
    mem.export_json(path)
    with Memory(store=str(tmp_path / "copy.db"), user=USER) as copy:
        copy.import_json(path)
        (rec,) = copy._storage.list(user=USER)
        assert (rec.direction, rec.due, rec.outcome_source, rec.watch_window) == (
            "absent",
            FRIDAY,
            "pressure-feed",
            1.5,
        )
