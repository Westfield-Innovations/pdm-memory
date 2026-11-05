"""Save filters: a write the filter refuses never reaches the store."""

import base64
import json
import time

import pytest
import respx

from pdm_memory import FilterRefused, FilterVerdict, Memory
from pdm_memory.storage.schema import hash_fact_text


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


# ---------------------------------------------------------------------------
# Memory.save and Memory.update
# ---------------------------------------------------------------------------

USER = "test_user"
TAGS = ["budget", "planning", "finance"]


class Fixed:
    def __init__(self, verdict):
        self.name = verdict.name
        self.verdict = verdict

    def check(self, text):
        return self.verdict


class Boom:
    name = "boom"

    def check(self, text):
        raise RuntimeError("down")


class Seen:
    name = "seen"

    def __init__(self):
        self.text = None

    def check(self, text):
        self.text = text
        return FilterVerdict(self.name, "1", ran=True, passed=True)


PASS = FilterVerdict("x", "1", ran=True, passed=True)
REFUSE = FilterVerdict("x", "1", ran=True, passed=False, terms=("operator",))
UNREAD = FilterVerdict("x", "1", ran=False, passed=False)


def _mem(tmp_path, *filters):
    return Memory(store=str(tmp_path / "f.db"), user=USER, filters=list(filters))


def stored(mem, memory_id):
    return mem._storage.get(memory_id, user=USER)


def test_a_refused_save_writes_nothing(tmp_path):
    mem = _mem(tmp_path, Fixed(REFUSE))
    text = "The operator is deployed and the budget fails."
    with pytest.raises(FilterRefused):
        mem.save(text, tags=TAGS)
    assert mem.count() == 0
    assert mem._storage.find_by_hash(hash_fact_text(text), user=USER) is None


def test_a_passed_save_is_written_and_marked(tmp_path):
    mem = _mem(tmp_path, Fixed(PASS))
    memory_id = mem.save("The budget accumulates.", tags=TAGS)
    assert stored(mem, memory_id).metadata["filters"]["x"] == {
        "version": "1",
        "ran": True,
        "passed": True,
        "terms": [],
    }


def test_a_save_the_filter_could_not_read_is_written_and_marked(tmp_path):
    mem = _mem(tmp_path, Fixed(UNREAD))
    memory_id = mem.save("Марія відповідає за план відкату.", tags=TAGS)
    assert stored(mem, memory_id).metadata["filters"]["x"]["ran"] is False


def test_without_filters_metadata_is_untouched(tmp_path):
    mem = Memory(store=str(tmp_path / "f.db"), user=USER)
    memory_id = mem.save("The budget accumulates.", tags=TAGS, metadata={"k": 1})
    assert "filters" not in stored(mem, memory_id).metadata
    assert stored(mem, memory_id).metadata["k"] == 1


def test_a_failing_filter_writes_nothing(tmp_path):
    mem = _mem(tmp_path, Boom())
    with pytest.raises(RuntimeError, match="down"):
        mem.save("The budget accumulates.", tags=TAGS)
    assert mem.count() == 0


def test_the_filter_sees_the_text_as_stored(tmp_path):
    seen = Seen()
    mem = _mem(tmp_path, seen)
    mem.save("  " + "a" * 600, tags=TAGS)
    assert seen.text == "a" * 500


def test_every_filter_runs_and_the_first_refusal_stops_the_save(tmp_path):
    seen = Seen()
    mem = _mem(tmp_path, seen, Fixed(REFUSE))
    with pytest.raises(FilterRefused):
        mem.save("The budget accumulates.", tags=TAGS)
    assert seen.text == "The budget accumulates."
    assert mem.count() == 0


def test_add_filter_turns_the_check_on(tmp_path):
    mem = Memory(store=str(tmp_path / "f.db"), user=USER)
    mem.add_filter(Fixed(REFUSE))
    with pytest.raises(FilterRefused):
        mem.save("The budget accumulates.", tags=TAGS)


def test_a_dedupe_hit_is_not_filtered(tmp_path):
    mem = Memory(store=str(tmp_path / "f.db"), user=USER)
    first = mem.save("The budget accumulates.", tags=TAGS)
    mem.add_filter(Fixed(REFUSE))
    assert mem.save("The budget accumulates.", tags=TAGS) == first


def test_a_refused_update_leaves_the_record(tmp_path):
    mem = Memory(store=str(tmp_path / "f.db"), user=USER)
    memory_id = mem.save("The budget accumulates.", tags=TAGS)
    mem.add_filter(Fixed(REFUSE))
    with pytest.raises(FilterRefused):
        mem.update(memory_id, text="The operator is deployed.")
    assert mem.get(memory_id).text == "The budget accumulates."


def test_a_passed_update_is_marked(tmp_path):
    mem = _mem(tmp_path, Fixed(PASS))
    memory_id = mem.save("The budget accumulates.", tags=TAGS)
    mem.update(memory_id, text="The budget grows.")
    assert mem.get(memory_id).text == "The budget grows."
    assert stored(mem, memory_id).metadata["filters"]["x"]["passed"] is True


def test_an_update_without_text_is_not_filtered(tmp_path):
    mem = Memory(store=str(tmp_path / "f.db"), user=USER)
    memory_id = mem.save("The budget accumulates.", tags=TAGS)
    mem.add_filter(Boom())
    mem.update(memory_id, p_magnitude=70.0)


def _token():
    payload = (
        base64.urlsafe_b64encode(json.dumps({"exp": time.time() + 3600}).encode())
        .decode()
        .rstrip("=")
    )
    return f"x.{payload}.y"


def test_a_refused_cloud_save_sends_nothing():
    with respx.mock(assert_all_called=False) as router:
        route = router.route(host="localhost")
        mem = Memory(
            store="cloud",
            token=_token(),
            cloud_url="http://localhost:8000",
            user=USER,
            filters=[Fixed(REFUSE)],
        )
        before = route.call_count
        with pytest.raises(FilterRefused):
            mem.save("The operator is deployed.", tags=TAGS, dedupe=False)
        assert route.call_count == before == 0


# ---------------------------------------------------------------------------
# Memory.save_many and Memory.ingest
# ---------------------------------------------------------------------------


class RefuseOperator:
    name = "x"

    def check(self, text):
        refused = "operator" in text
        return FilterVerdict(
            "x", "1", ran=True, passed=not refused, terms=("operator",) if refused else ()
        )


MIXED = [
    "The budget accumulates.",
    "The operator is deployed.",
    "Maria owns the rollback plan.",
]


def test_save_many_counts_what_the_filter_refused(tmp_path):
    mem = _mem(tmp_path, RefuseOperator())
    result = mem.save_many([{"text": t, "tags": TAGS} for t in MIXED])
    assert result == {"saved": 2, "skipped": 0, "errors": 0, "blocked": 1}
    assert mem.count() == 2
    assert all(
        stored(mem, hit.id).metadata["filters"]["x"]["passed"] for hit in mem.list().items
    )


def test_a_failing_filter_stops_save_many_before_anything_is_written(tmp_path):
    mem = _mem(tmp_path, Boom())
    with pytest.raises(RuntimeError, match="down"):
        mem.save_many([{"text": t, "tags": TAGS} for t in MIXED])
    assert mem.count() == 0


def test_without_filters_save_many_returns_what_it_did(tmp_path):
    mem = Memory(store=str(tmp_path / "f.db"), user=USER)
    result = mem.save_many([{"text": t, "tags": TAGS} for t in MIXED])
    assert set(result) == {"saved", "skipped", "errors"}


def test_ingest_counts_what_the_filter_refused(tmp_path):
    mem = _mem(tmp_path, RefuseOperator())
    result = mem.ingest([{"text": t} for t in MIXED])
    assert result["blocked"] == 1
    assert result["saved"] == 2
    assert mem.count() == 2


def test_a_failing_filter_writes_no_ingested_row(tmp_path):
    mem = _mem(tmp_path, Boom())
    result = mem.ingest([{"text": t} for t in MIXED])
    assert result == {"saved": 0, "skipped": 0, "errors": 3, "blocked": 0}
    assert mem.count() == 0


def test_without_filters_ingest_returns_what_it_did(tmp_path):
    mem = Memory(store=str(tmp_path / "f.db"), user=USER)
    result = mem.ingest([{"text": t} for t in MIXED])
    assert result == {"saved": 3, "skipped": 0, "errors": 0}
