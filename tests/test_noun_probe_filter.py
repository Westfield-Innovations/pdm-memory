"""The real Noun Probe as a save filter."""

import pytest

from pdm_memory import FilterRefused, Memory, NounProbeFilter

USER = "test_user"
TAGS = ["budget", "planning", "finance"]
MISFRAMED = "The operator is deployed and the budget fails."


def _mem(tmp_path):
    return Memory(store=str(tmp_path / "np.db"), user=USER, filters=[NounProbeFilter()])


def _mark(mem, memory_id):
    return mem._storage.get(memory_id, user=USER).metadata["filters"]["noun_probe"]


def test_a_claim_the_probe_cannot_split_is_refused(tmp_path):
    mem = _mem(tmp_path)
    with pytest.raises(FilterRefused) as err:
        mem.save(MISFRAMED, tags=TAGS)
    assert "operator" in err.value.verdict.terms
    assert mem.count() == 0


@pytest.mark.parametrize(
    "text", ["The budget accumulates.", "Maria owns the rollback plan."]
)
def test_ordinary_facts_are_saved(tmp_path, text):
    mem = _mem(tmp_path)
    mark = _mark(mem, mem.save(text, tags=TAGS))
    assert mark == {"version": "1", "ran": True, "passed": True, "terms": []}


def test_a_cyrillic_fact_is_saved_and_marked(tmp_path):
    mem = _mem(tmp_path)
    mark = _mark(mem, mem.save("Марія відповідає за план відкату.", tags=TAGS))
    assert mark["passed"] is True
    assert "ran" in mark


def test_a_disabled_probe_saves_and_marks_it_unread(tmp_path, monkeypatch):
    monkeypatch.setenv("PDM_NOUN_PROBE_ENABLED", "0")
    mem = _mem(tmp_path)
    mark = _mark(mem, mem.save(MISFRAMED, tags=TAGS))
    assert mark["ran"] is False


def test_save_many_counts_the_misframed_fact(tmp_path):
    mem = _mem(tmp_path)
    items = [{"text": t, "tags": TAGS} for t in ["The budget accumulates.", MISFRAMED]]
    assert mem.save_many(items)["blocked"] == 1
    assert mem.count() == 1
