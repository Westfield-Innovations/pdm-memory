"""Save filters: a write the filter refuses never reaches the store."""

from pdm_memory import FilterRefused, FilterVerdict


def test_a_verdict_refuses_only_a_pass_that_ran_and_failed():
    assert FilterVerdict("x", "1", ran=True, passed=False, terms=("detector",)).refuses
    assert not FilterVerdict("x", "1", ran=True, passed=True).refuses
    assert not FilterVerdict("x", "1", ran=False, passed=False).refuses


def test_refused_names_the_filter_and_its_terms():
    err = FilterRefused(FilterVerdict("noun_probe", "1", True, False, ("detector",)))
    assert str(err) == "noun_probe refused the save: detector"
    assert isinstance(err, ValueError)
    assert err.verdict.terms == ("detector",)


def test_a_verdict_reads_as_metadata():
    verdict = FilterVerdict("x", "1", ran=True, passed=False, terms=("a", "b"))
    assert verdict.as_metadata() == {
        "version": "1",
        "ran": True,
        "passed": False,
        "terms": ["a", "b"],
    }
