"""PDM v1.9 change 5: settling claims, supplied observations and late gap reports."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest

from pdm_memory import Claim, Memory
from pdm_memory.claims import counts_for

USER = "u"
NOW = datetime.now(tz=timezone.utc).replace(microsecond=0)
PAST_DUE = NOW - timedelta(hours=1)
LATER = NOW + timedelta(days=4)
TAGS = ["flare", "igniter", "north"]


@pytest.fixture
def mem(tmp_path):
    m = Memory(store=str(tmp_path / "s.db"), user=USER)
    yield m
    m.close()


def _claim(mem, direction, due, source=None, window=None, text="North igniter fails"):
    claim = Claim(direction, due, outcome_source=source, watch_window_days=window)
    mid = mem.save(text, tags=TAGS, claim=claim)
    # The claim was made two days ago, so its interval runs up to its due.
    mem._storage.update(mid, user=USER, created_at=NOW - timedelta(days=2))
    return mid


def _rec(mem, mid):
    return mem._storage.get(mid, user=USER)


def _counts(mem, mid):
    rec = _rec(mem, mid)
    return rec.validation_prediction_correct, rec.validation_prediction_total


@pytest.mark.parametrize(
    ("direction", "verdict", "expected"),
    [
        ("occurs", "on_time", (1, 1)),
        ("occurs", "late", (0, 1)),
        ("occurs", "observed_absent", (0, 1)),
        ("occurs", "not_by_due", (0, 1)),
        ("occurs", "expired_unobserved", (0, 0)),
        ("absent", "on_time", (0, 1)),
        ("absent", "late", (1, 1)),
        ("absent", "observed_absent", (1, 1)),
        ("absent", "not_by_due", (1, 1)),
        ("absent", "expired_unobserved", (0, 0)),
        ("occurs", "", (0, 0)),
    ],
)
def test_grading_matches_the_table(direction, verdict, expected):
    assert counts_for(direction, verdict) == expected


def test_a_bound_claim_with_nothing_reported_did_not_occur(mem):
    occurs = _claim(mem, "occurs", PAST_DUE, source="feed")
    absent = _claim(mem, "absent", PAST_DUE, source="feed", text="Tank 3 holds")
    report = mem.settle_due_claims(NOW)
    assert report.settled == {"not_by_due": 2}
    assert _rec(mem, occurs).verdict == "not_by_due"
    assert _counts(mem, occurs) == (0, 1)
    assert _counts(mem, absent) == (1, 1)
    assert _rec(mem, absent).validation_status == "VALIDATED"


def test_an_unbound_claim_expires_unobserved_and_grades_nothing(mem):
    mid = _claim(mem, "occurs", PAST_DUE)
    report = mem.settle_due_claims(NOW)
    assert report.settled == {"expired_unobserved": 1}
    assert _rec(mem, mid).verdict == "expired_unobserved"
    assert _counts(mem, mid) == (0, 0)


def test_a_gap_over_the_interval_keeps_a_missing_report_from_grading(mem):
    mid = _claim(mem, "absent", PAST_DUE, source="feed")
    mem.report_coverage_gap("feed", NOW - timedelta(days=1), NOW - timedelta(hours=2))
    mem.settle_due_claims(NOW)
    assert _rec(mem, mid).verdict == "expired_unobserved"
    assert _counts(mem, mid) == (0, 0)


def test_a_gap_on_another_source_does_not_count(mem):
    mid = _claim(mem, "absent", PAST_DUE, source="feed")
    mem.report_coverage_gap("other", NOW - timedelta(days=1), NOW)
    mem.settle_due_claims(NOW)
    assert _rec(mem, mid).verdict == "not_by_due"


def test_claims_not_yet_due_are_left_open(mem):
    mid = _claim(mem, "occurs", LATER, source="feed")
    assert mem.settle_due_claims(NOW).settled == {}
    assert _rec(mem, mid).verdict == ""


def test_the_pass_is_idempotent(mem):
    mid = _claim(mem, "occurs", PAST_DUE, source="feed")
    mem.settle_due_claims(NOW)
    assert mem.settle_due_claims(NOW + timedelta(hours=1)).settled == {}
    assert _counts(mem, mid) == (0, 1)


def test_a_plain_signature_is_never_settled(mem):
    mid = mem.save("Plain fact", tags=TAGS)
    assert mem.settle_due_claims(NOW).settled == {}
    assert _rec(mem, mid).verdict == ""


def test_an_occurrence_on_time_is_correct_for_occurs(mem):
    mid = _claim(mem, "occurs", LATER, source="feed")
    result = mem.validate_claim(mid, "occurred", event_time=NOW, report_time=NOW)
    assert result.verdict == "on_time"
    assert _counts(mem, mid) == (1, 1)
    assert _rec(mem, mid).validation_status == "VALIDATED"


def test_an_occurrence_inside_the_watch_window_is_late(mem):
    mid = _claim(mem, "absent", PAST_DUE, source="feed", window=2.0)
    result = mem.validate_claim(mid, "occurred", event_time=NOW, report_time=NOW)
    assert result.verdict == "late"
    assert _counts(mem, mid) == (1, 1)  # the absence held through its interval


def test_an_occurrence_past_the_window_changes_nothing(mem):
    mid = _claim(mem, "occurs", PAST_DUE, window=0.0)
    assert mem.validate_claim(mid, "occurred", event_time=NOW, report_time=NOW) is None
    assert _rec(mem, mid).verdict == ""


def test_the_same_observation_twice_changes_nothing(mem):
    mid = _claim(mem, "occurs", LATER, source="feed")
    mem.validate_claim(mid, "occurred", event_time=NOW, report_time=NOW)
    assert mem.validate_claim(mid, "occurred", event_time=NOW, report_time=NOW) is None
    assert _counts(mem, mid) == (1, 1)


def test_unknown_grades_nothing(mem):
    mid = _claim(mem, "occurs", LATER, source="feed")
    assert mem.validate_claim(mid, "unknown") is None
    assert _counts(mem, mid) == (0, 0)


def test_absence_cannot_be_observed_before_the_due(mem):
    mid = _claim(mem, "absent", LATER)
    with pytest.raises(ValueError, match="at or after"):
        mem.validate_claim(mid, "observed_absent", event_time=NOW, report_time=NOW)


def test_only_a_claim_takes_an_observation(mem):
    mid = mem.save("Plain fact", tags=TAGS)
    with pytest.raises(ValueError, match="No claim"):
        mem.validate_claim(mid, "occurred")


def test_a_late_report_amends_the_one_verdict_and_moves_its_count(mem):
    mid = _claim(mem, "occurs", PAST_DUE, source="feed", window=1.0)
    mem.settle_due_claims(NOW)
    assert _counts(mem, mid) == (0, 1)
    result = mem.validate_claim(
        mid, "occurred", event_time=PAST_DUE - timedelta(minutes=5), report_time=NOW
    )
    assert result.verdict == "on_time"
    assert _counts(mem, mid) == (1, 1)  # moved, never a second count


def test_a_bound_verdict_is_not_amended_after_its_window(mem):
    mid = _claim(mem, "occurs", PAST_DUE, source="feed", window=0.0)
    mem.settle_due_claims(NOW)
    assert (
        mem.validate_claim(mid, "occurred", event_time=PAST_DUE, report_time=NOW) is None
    )
    assert _rec(mem, mid).verdict == "not_by_due"


def test_an_unbound_claim_can_still_be_settled_after_it_expired(mem):
    mid = _claim(mem, "absent", PAST_DUE)
    mem.settle_due_claims(NOW)
    result = mem.validate_claim(mid, "observed_absent", event_time=NOW, report_time=NOW)
    assert result.verdict == "observed_absent"
    assert _counts(mem, mid) == (1, 1)
    assert mem.get(mid).duck is False


def test_a_late_gap_amends_not_by_due_and_restores_the_count(mem):
    mid = _claim(mem, "absent", PAST_DUE, source="feed", window=1.0)
    mem.settle_due_claims(NOW)
    assert _counts(mem, mid) == (1, 1)
    amended = mem.report_coverage_gap("feed", NOW - timedelta(days=1), NOW)
    assert amended == 1
    assert _rec(mem, mid).verdict == "expired_unobserved"
    assert _counts(mem, mid) == (0, 0)
    assert _rec(mem, mid).validation_status == "UNVALIDATED"


def test_a_late_gap_after_the_window_amends_nothing(mem):
    mid = _claim(mem, "absent", PAST_DUE, source="feed", window=0.0)
    mem.settle_due_claims(NOW)
    assert mem.report_coverage_gap("feed", NOW - timedelta(days=1), NOW) == 0
    assert _rec(mem, mid).verdict == "not_by_due"


def test_a_late_gap_leaves_supplied_observations_alone(mem):
    mid = _claim(mem, "absent", LATER, source="feed", window=1.0)
    mem.validate_claim(mid, "observed_absent", event_time=LATER, report_time=LATER)
    assert mem.report_coverage_gap("feed", NOW - timedelta(days=1), LATER) == 0
    assert _rec(mem, mid).verdict == "observed_absent"


def test_a_wrong_verdict_on_a_validated_claim_reopens_what_rests_on_it(mem):
    root = _claim(mem, "occurs", PAST_DUE, source="feed", window=1.0)
    mem.reinforce(root)
    leaf = mem.save("Plan around the north igniter", tags=TAGS, rests_on=[root])
    report = mem.settle_due_claims(NOW)
    assert report.reopened == 1
    assert _rec(mem, root).validation_status == "UNVALIDATED"
    assert _rec(mem, leaf).validation_status == "REOPENED"
    # A gap that restores the root clears none of its dependents' reopen marks.
    mem.report_coverage_gap("feed", NOW - timedelta(days=1), NOW)
    assert _rec(mem, leaf).validation_status == "REOPENED"


def test_a_verdict_computed_from_a_stale_read_writes_nothing(mem):
    mid = _claim(mem, "occurs", PAST_DUE, source="feed")
    stale = _rec(mem, mid)  # what a settlement pass read before the observation landed
    mem.validate_claim(mid, "occurred", event_time=PAST_DUE - timedelta(minutes=5))
    assert mem._write_verdict(stale, "not_by_due", "", None, NOW) is None
    assert _rec(mem, mid).verdict == "on_time"
    assert _counts(mem, mid) == (1, 1)
