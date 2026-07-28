"""Integration tests for the Memory class — end-to-end."""

import pytest

from pdm_memory import Memory


@pytest.fixture
def mem(tmp_path):
    m = Memory(store=str(tmp_path / "test.db"), user="test_user")
    yield m
    m.close()


class TestMemorySaveRecall:
    def test_save_returns_id(self, mem):
        mid = mem.save("User prefers metric units", tags=["units", "preferences", "formatting"])
        assert isinstance(mid, str)
        assert len(mid) == 36  # UUID

    def test_recall_basic(self, mem):
        mem.save("User prefers metric units", tags=["units", "preferences", "formatting"], p_magnitude=80)
        mem.save("User dislikes long responses", tags=["brevity", "preferences", "formatting"], p_magnitude=70)
        hits = mem.recall("how should I format this?", k=5)
        assert len(hits) >= 1
        assert all(hasattr(h, "text") for h in hits)
        assert all(hasattr(h, "pressure") for h in hits)

    def test_recall_returns_most_relevant(self, mem):
        mem.save("User hates Python 2", tags=["python", "coding", "style"], p_magnitude=90)
        mem.save("User prefers dark mode", tags=["ui", "dark_mode", "preferences"], p_magnitude=60)
        hits = mem.recall("which Python version should I use?", k=3)
        texts = [h.text for h in hits]
        # The coding memory should rank higher
        assert any("Python" in t for t in texts)

    def test_recall_filters_unrelated_high_pressure(self, mem):
        mem.save(
            "User wants answers in Ukrainian, short and direct",
            tags=["language", "prefs", "style"],
            p_magnitude=72,
        )
        mem.save(
            "Launch date for Orion is 2026-08-01",
            tags=["orion", "launch", "date", "product"],
            p_magnitude=85,
        )
        hits = mem.recall(
            "what language should I use?",
            k=3,
            reinforce=False,
            search_cost=0.65,
        )
        assert hits
        assert not any("orion" in h.text.lower() for h in hits)

    def test_recall_empty_db(self, mem):
        hits = mem.recall("anything", k=5)
        assert hits == []

    def test_save_text_truncated_at_500(self, mem):
        long_text = "x" * 600
        mid = mem.save(long_text, tags=["test", "long", "text"])
        rec = mem._storage.get(mid, user="test_user")
        assert len(rec.compressed_fact) == 500

    def test_save_empty_raises(self, mem):
        with pytest.raises(ValueError):
            mem.save("")

    def test_save_dedupe_returns_existing_id(self, mem):
        first = mem.save("Duplicate fact", tags=["a", "b", "c"], p_magnitude=50)
        second = mem.save("Duplicate fact", tags=["x", "y", "z"], p_magnitude=90)
        assert second == first
        assert mem.count() == 1

    def test_save_dedupe_reinforce(self, mem):
        mid = mem.save("Repeat me", tags=["a", "b", "c"], p_magnitude=50)
        before = mem._storage.get(mid, user="test_user").p_magnitude
        mem.save("Repeat me", tags=["a", "b", "c"], dedupe_reinforce=True)
        after = mem._storage.get(mid, user="test_user").p_magnitude
        assert after > before

    def test_save_dedupe_disabled(self, mem):
        mem.save("Same text", tags=["a", "b", "c"], dedupe=False)
        mem.save("Same text", tags=["d", "e", "f"], dedupe=False)
        assert mem.count() == 2

    def test_count(self, mem):
        assert mem.count() == 0
        mem.save("first", tags=["a", "b", "c"])
        mem.save("second", tags=["d", "e", "f"])
        assert mem.count() == 2


class TestReconcileTorsion:
    def test_reconcile_does_not_return_deleted_dedupe_id(self, mem):
        """Reconciled text matching an existing fact must not reuse that ID."""
        a = mem.save("Fact A", tags=["x", "y", "z"], p_magnitude=50)
        b = mem.save("Fact B", tags=["x", "y", "z"], p_magnitude=45)
        new_id = mem.reconcile_torsion(a, b, "Fact A")

        assert new_id != a
        merged = mem.get(new_id)
        assert merged is not None
        assert merged.text == "Fact A"
        assert mem.get(a) is None
        assert mem.get(b) is None
        assert mem.count() == 1


class TestMemoryFromEnv:
    def test_from_env_sqlite(self, tmp_path, monkeypatch):
        db = str(tmp_path / "env.db")
        monkeypatch.setenv("PDM_STORE", db)
        monkeypatch.setenv("PDM_USER", "env_user")
        with Memory.from_env() as mem:
            mem.save("env fact", tags=["env", "test", "unit"])
            assert mem.count() == 1
            assert mem._user == "env_user"

    def test_from_env_missing_store(self, monkeypatch):
        monkeypatch.delenv("PDM_STORE", raising=False)
        with pytest.raises(ValueError, match="PDM_STORE"):
            Memory.from_env()


class TestMemoryReinforce:
    def test_reinforce_increases_pressure(self, mem):
        mid = mem.save("Important fact", tags=["fact", "important", "core"], p_magnitude=60)
        original = mem._storage.get(mid, user="test_user")
        mem.reinforce(mid, coupling_score=0.8)
        updated = mem._storage.get(mid, user="test_user")
        assert updated.p_magnitude > original.p_magnitude

    def test_reinforce_missing_id(self, mem):
        with pytest.raises(ValueError, match="not found"):
            mem.reinforce("nonexistent-id-1234-5678-90ab")


class TestMemoryDecay:
    def test_decay_returns_counts(self, mem):
        mem.save("Memory to decay", tags=["old", "stale", "memory"], p_magnitude=35, t_persistence=0.001)
        counts = mem.decay()
        assert "decayed" in counts
        assert "deleted" in counts
        assert "skipped" in counts

    def test_decay_dry_run_no_changes(self, mem):
        mid = mem.save("Memory", tags=["a", "b", "c"], p_magnitude=80)
        before = mem._storage.get(mid, user="test_user")
        mem.decay(dry_run=True)
        after = mem._storage.get(mid, user="test_user")
        assert before.p_magnitude == after.p_magnitude

    def test_recall_does_not_rewrite_stored_pressure(self, mem):
        """Single decay law: live scoring must not mutate p_magnitude on read."""
        mid = mem.save(
            "User prefers metric units",
            tags=["units", "preferences", "formatting"],
            p_magnitude=80,
            t_persistence=30,
        )
        before = mem._storage.get(mid, user="test_user").p_magnitude
        mem.recall("format units", k=5, reinforce=False)
        after = mem._storage.get(mid, user="test_user").p_magnitude
        assert after == before


class TestMemoryExplain:
    def test_explain_returns_report(self, mem):
        mid = mem.save("User prefers metric units", tags=["units", "preferences", "formatting"], p_magnitude=75)
        report = mem.explain(mid)
        assert report.memory_id == mid
        assert report.p_magnitude == pytest.approx(75.0)
        assert "units" in report.intent_tags
        assert report.p_effective >= 0

    def test_explain_with_query(self, mem):
        mid = mem.save("User uses Python", tags=["python", "coding", "language"], p_magnitude=80)
        report = mem.explain(mid, query="what language do they prefer?")
        assert report.intent_weight is not None
        assert report.coupling_score is not None

    def test_explain_missing_raises(self, mem):
        with pytest.raises(KeyError):
            mem.explain("nonexistent-00000000-0000-0000-0000")

    def test_explain_render(self, mem):
        mid = mem.save("Test memory", tags=["test", "dev", "memory"], p_magnitude=60)
        report = mem.explain(mid)
        rendered = report.render()
        assert "P_effective" in rendered
        assert "Decay factor" in rendered


class TestMemoryDrawers:
    def test_list_drawers(self, mem):
        mem.save("Fact 1", tags=["a", "b", "c"], drawer="science")
        mem.save("Fact 2", tags=["d", "e", "f"], drawer="finance")
        drawers = mem.list_drawers()
        domains = [d.domain for d in drawers]
        assert "science" in domains
        assert "finance" in domains


class TestMemoryContextManager:
    def test_context_manager(self, tmp_path):
        db = str(tmp_path / "cm.db")
        with Memory(store=db, user="u") as mem:
            mem.save("fact", tags=["a", "b", "c"])
            assert mem.count() == 1
        # After close, file should still exist
        import os
        assert os.path.exists(db)


class TestMemoryGetUpdate:
    def test_get_returns_hit(self, mem):
        mid = mem.save("User prefers metric units", tags=["units", "prefs"], p_magnitude=70)
        hit = mem.get(mid)
        assert hit is not None
        assert hit.id == mid
        assert hit.text == "User prefers metric units"
        assert hit.p_raw == pytest.approx(70.0)
        assert hit.pressure > 0

    def test_get_missing_returns_none(self, mem):
        assert mem.get("00000000-0000-0000-0000-000000000000") is None

    def test_update_text_and_tags(self, mem):
        mid = mem.save("Old fact", tags=["old", "a", "b"], p_magnitude=50)
        hit = mem.update(mid, text="New fact", tags=["new", "c", "d"])
        assert hit.text == "New fact"
        assert hit.intent_tags == ["new", "c", "d"]
        stored = mem._storage.get(mid, user="test_user")
        assert stored.compressed_fact == "New fact"

    def test_update_pressure_recalculates_spike(self, mem):
        mid = mem.save("Fact", tags=["a", "b", "c"], p_magnitude=40, t_persistence=30)
        mem.update(mid, p_magnitude=90)
        stored = mem._storage.get(mid, user="test_user")
        assert stored.p_magnitude == pytest.approx(90.0)
        assert stored.effective_spike is not None
        assert stored.effective_spike > 0

    def test_update_missing_raises(self, mem):
        with pytest.raises(ValueError, match="not found"):
            mem.update("00000000-0000-0000-0000-000000000000", text="nope")

    def test_update_empty_fields_raises(self, mem):
        mid = mem.save("Fact", tags=["a", "b", "c"])
        with pytest.raises(ValueError, match="At least one field"):
            mem.update(mid)

    def test_update_empty_text_raises(self, mem):
        mid = mem.save("Fact", tags=["a", "b", "c"])
        with pytest.raises(ValueError, match="cannot be empty"):
            mem.update(mid, text="   ")


class TestMemoryIngest:
    def test_ingest_list_of_dicts(self, mem):
        data = [
            {"text": "User prefers dark mode", "tags": ["ui", "dark_mode", "preferences"]},
            {"content": "Team uses Slack", "categories": "communication,slack,team"},
        ]
        counts = mem.ingest(data)
        assert counts["saved"] == 2
        assert counts["errors"] == 0

    def test_ingest_with_mapping(self, mem):
        data = [{"msg": "User hates Comic Sans", "weight": 85}]
        counts = mem.ingest(data, mapping={"msg": "compressed_fact", "weight": "p_magnitude"})
        assert counts["saved"] == 1

    def test_ingest_csv_string(self, mem):
        csv_content = "text,tags,p_magnitude\nUser likes Python,coding python preferences,80"
        counts = mem.ingest(csv_content)
        assert counts["saved"] >= 1
