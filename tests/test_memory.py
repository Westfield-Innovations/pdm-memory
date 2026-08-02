"""Integration tests for the Memory class — end-to-end."""

import pytest

from pdm_memory import Memory
from pdm_memory.storage.base import SaveBatchResult


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
        mem.save(
            "User prefers metric units", tags=["units", "preferences", "formatting"], p_magnitude=80
        )
        mem.save(
            "User dislikes long responses",
            tags=["brevity", "preferences", "formatting"],
            p_magnitude=70,
        )
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


class TestTemporalRecall:
    def test_save_event_at_round_trips(self, mem):
        from datetime import datetime, timezone

        event = datetime(2026, 7, 27, 9, 30, tzinfo=timezone.utc)
        mid = mem.save(
            "Yesterday's architecture review: ship PDM examples",
            tags=["meeting", "architecture", "review"],
            p_magnitude=60,
            event_at=event,
        )
        hit = mem.get(mid)
        assert hit is not None
        assert hit.t_event_at is not None
        assert hit.t_event_at.date().isoformat() == "2026-07-27"

    def test_deadline_backfills_event_at(self, mem):
        from datetime import datetime, timezone

        due = datetime(2026, 8, 15, tzinfo=timezone.utc)
        mid = mem.save(
            "Orion launch deadline",
            tags=["orion", "launch", "deadline"],
            p_magnitude=70,
            deadline=due,
        )
        rec = mem._storage.get(mid, user="test_user")
        assert rec is not None
        assert rec.t_deadline == due
        assert rec.t_event_at == due

    def test_recall_populates_e_temporal_for_deadlines(self, mem):
        from datetime import datetime, timedelta, timezone

        soon = datetime.now(tz=timezone.utc) + timedelta(days=2)
        past = datetime.now(tz=timezone.utc) - timedelta(days=3)

        mem.save(
            "Ship the release notes tomorrow",
            tags=["ship", "release", "notes"],
            p_magnitude=70,
            deadline=soon,
        )
        mem.save(
            "Old standup notes from last week",
            tags=["standup", "notes", "meeting"],
            p_magnitude=70,
            deadline=past,
        )
        mem.save(
            "Evergreen: prefer short release notes",
            tags=["release", "notes", "style"],
            p_magnitude=70,
        )

        hits = mem.recall("release notes", k=5, search_cost=0.9, reinforce=False)
        by_text = {h.text: h for h in hits}

        upcoming = by_text.get("Ship the release notes tomorrow")
        expired = by_text.get("Old standup notes from last week")
        evergreen = by_text.get("Evergreen: prefer short release notes")

        assert upcoming is not None
        assert upcoming.e_temporal is not None and upcoming.e_temporal > 0.0
        if expired is not None:
            assert (expired.e_temporal or 0.0) == 0.0
        if evergreen is not None:
            assert (evergreen.e_temporal or 0.0) == 0.0

    def test_near_deadline_ranks_above_equal_pressure_peer(self, mem):
        from datetime import datetime, timedelta, timezone

        soon = datetime.now(tz=timezone.utc) + timedelta(hours=36)
        mid_a = mem.save(
            "Deploy checklist for Orion release window",
            tags=["deploy", "orion", "release"],
            p_magnitude=65,
            deadline=soon,
        )
        mid_b = mem.save(
            "Deploy checklist for Orion general process",
            tags=["deploy", "orion", "release"],
            p_magnitude=65,
        )

        hits = mem.recall("Orion deploy checklist", k=5, search_cost=0.85, reinforce=False)
        ids = [h.id for h in hits]
        assert mid_a in ids and mid_b in ids
        assert ids.index(mid_a) < ids.index(mid_b)
        urgent = next(h for h in hits if h.id == mid_a)
        assert (urgent.e_temporal or 0.0) > 0.0

    def test_yesterday_query_prioritizes_t_event_at_window(self, mem):
        from datetime import datetime, timedelta, timezone

        now = datetime.now(tz=timezone.utc)
        yesterday = datetime(now.year, now.month, now.day, 15, 0, tzinfo=timezone.utc) - timedelta(
            days=1
        )
        last_month = now - timedelta(days=25)

        id_y = mem.save(
            "Architecture review action items from the daily sync",
            tags=["architecture", "review", "sync", "meeting"],
            p_magnitude=60,
            event_at=yesterday,
        )
        id_old = mem.save(
            "Architecture review backlog from the planning sync",
            tags=["architecture", "review", "sync", "meeting"],
            p_magnitude=90,
            event_at=last_month,
        )

        hits = mem.recall(
            "what happened yesterday in architecture review",
            k=5,
            search_cost=1.0,
            reinforce=False,
            diversity_bias=None,
        )
        ids = [h.id for h in hits]
        assert id_y in ids
        assert id_old in ids
        # Window match beats higher pressure outside the window
        assert ids.index(id_y) < ids.index(id_old)

    def test_absolute_month_year_window_prioritizes_event(self, mem):
        from datetime import datetime, timezone

        jan = mem.save(
            "Capacity planning notes from the ops review",
            tags=["capacity", "planning", "ops", "review"],
            p_magnitude=55,
            event_at=datetime(2024, 1, 15, tzinfo=timezone.utc),
        )
        decoy = mem.save(
            "Capacity planning backlog from the ops review",
            tags=["capacity", "planning", "ops", "review"],
            p_magnitude=95,
            event_at=datetime(2025, 6, 1, tzinfo=timezone.utc),
        )
        hits = mem.recall(
            "capacity planning ops review in January 2024",
            k=5,
            search_cost=1.0,
            reinforce=False,
            diversity_bias=None,
        )
        ids = [h.id for h in hits]
        assert jan in ids and decoy in ids
        assert ids.index(jan) < ids.index(decoy)


class TestAuditAndHeal:
    def test_auto_reconciles_high_confidence_torsion(self, mem):
        from datetime import datetime, timezone

        a = mem.save(
            "Project Alpha deadline is July 10",
            tags=["project", "alpha", "deadline"],
            drawer="projects",
            p_magnitude=70,
            deadline=datetime(2026, 7, 10, tzinfo=timezone.utc),
            dedupe=False,
        )
        b = mem.save(
            "Project Alpha deadline is July 15",
            tags=["project", "alpha", "deadline"],
            drawer="projects",
            p_magnitude=72,
            deadline=datetime(2026, 7, 15, tzinfo=timezone.utc),
            dedupe=False,
        )
        assert mem.count() == 2

        summary = mem.audit_and_heal(
            torsion_threshold=0.5,
            auto_reconcile_threshold=0.85,
            run_decay=False,
        )
        assert summary["scanned_pairs"] >= 1
        assert summary["reconciled"] == 1
        assert "narrative" in summary
        assert "resolved" in summary["narrative"].lower()
        assert mem.get(a) is None
        assert mem.get(b) is None
        assert mem.count() == 1
        survivor = mem.list(limit=5).items[0]
        assert "July" in survivor.text

    def test_dry_run_does_not_write(self, mem):
        from datetime import datetime, timezone

        mem.save(
            "Launch window is August 1",
            tags=["launch", "window", "orion"],
            drawer="product",
            p_magnitude=70,
            deadline=datetime(2026, 8, 1, tzinfo=timezone.utc),
            dedupe=False,
        )
        mem.save(
            "Launch window is August 8",
            tags=["launch", "window", "orion"],
            drawer="product",
            p_magnitude=70,
            deadline=datetime(2026, 8, 8, tzinfo=timezone.utc),
            dedupe=False,
        )
        before = mem.count()
        summary = mem.audit_and_heal(
            torsion_threshold=0.5,
            auto_reconcile_threshold=0.85,
            run_decay=False,
            dry_run=True,
        )
        assert summary["dry_run"] is True
        assert summary["reconciled"] >= 1
        assert mem.count() == before


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
        mem.save(
            "Memory to decay", tags=["old", "stale", "memory"], p_magnitude=35, t_persistence=0.001
        )
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
        mid = mem.save(
            "User prefers metric units", tags=["units", "preferences", "formatting"], p_magnitude=75
        )
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

    def test_update_batch_updates_multiple_memories(self, mem):
        first_id = mem.save(
            "Patent memo",
            tags=["patent", "review", "legal"],
            metadata={"owner": "ops"},
        )
        second_id = mem.save(
            "License memo",
            tags=["license", "review", "legal"],
            metadata={"owner": "legal"},
        )

        counts = mem.update_batch(
            [
                (
                    first_id,
                    {
                        "tags": ["patent", "granted", "legal"],
                        "metadata": {"license": "pending"},
                    },
                ),
                (
                    second_id,
                    {
                        "drawer": "licensing",
                        "metadata": {"license": "apache-2.0"},
                    },
                ),
            ]
        )

        assert counts == {"updated": 2, "skipped": 0, "errors": 0}

        first = mem.get(first_id)
        second = mem.get(second_id)
        assert first is not None
        assert second is not None
        assert first.intent_tags == ["patent", "granted", "legal"]
        assert second.drawer == "licensing"

        first_rec = mem._storage.get(first_id, user="test_user")
        second_rec = mem._storage.get(second_id, user="test_user")
        assert first_rec.metadata == {"owner": "ops", "license": "pending"}
        assert second_rec.metadata == {"owner": "legal", "license": "apache-2.0"}

    def test_update_batch_tracks_skips_and_errors(self, mem):
        mid = mem.save("Fact", tags=["a", "b", "c"])

        counts = mem.update_batch(
            [
                (mid, {}),
                ("00000000-0000-0000-0000-000000000000", {"tags": ["x", "y", "z"]}),
            ]
        )

        assert counts == {"updated": 0, "skipped": 1, "errors": 1}

    def test_update_many_uses_id_or_memory_id(self, mem):
        first_id = mem.save("Patent memo", tags=["patent", "review", "legal"])
        second_id = mem.save("License memo", tags=["license", "review", "legal"])

        counts = mem.update_many(
            [
                {"id": first_id, "tags": ["patent", "approved", "legal"]},
                {"memory_id": second_id, "drawer": "licensing"},
                {"tags": ["missing", "id", "entry"]},
            ]
        )

        assert counts == {"updated": 2, "skipped": 0, "errors": 1}
        assert mem.get(first_id).intent_tags == ["patent", "approved", "legal"]
        assert mem.get(second_id).drawer == "licensing"

    def test_update_many_batches_updates(self, mem):
        first_id = mem.save(
            "Patent memo",
            tags=["patent", "review", "legal"],
            metadata={"owner": "ops"},
        )
        second_id = mem.save(
            "License memo",
            tags=["license", "review", "legal"],
            metadata={"owner": "legal"},
        )

        counts = mem.update_many(
            [
                {
                    "id": first_id,
                    "tags": ["patent", "granted", "legal"],
                    "metadata": {"license": "pending"},
                },
                {
                    "memory_id": second_id,
                    "drawer": "licensing",
                    "metadata": {"license": "apache-2.0"},
                },
            ]
        )

        assert counts == {"updated": 2, "skipped": 0, "errors": 0}

        first = mem.get(first_id)
        second = mem.get(second_id)
        assert first is not None
        assert second is not None
        assert first.intent_tags == ["patent", "granted", "legal"]
        assert second.drawer == "licensing"

        first_rec = mem._storage.get(first_id, user="test_user")
        second_rec = mem._storage.get(second_id, user="test_user")
        assert first_rec.metadata == {"owner": "ops", "license": "pending"}
        assert second_rec.metadata == {"owner": "legal", "license": "apache-2.0"}

    def test_update_many_tracks_skipped_and_errors(self, mem):
        mid = mem.save("Fact", tags=["a", "b", "c"])

        counts = mem.update_many(
            [
                {"id": mid},
                {"id": "00000000-0000-0000-0000-000000000000", "tags": ["x", "y", "z"]},
                {"tags": ["no", "id", "here"]},
            ]
        )

        assert counts == {"updated": 0, "skipped": 1, "errors": 2}


class TestMemoryIngest:
    def test_save_many_uses_storage_batch_api(self, mem):
        captured = {}

        def fake_save_many(sigs):
            captured["facts"] = [sig.compressed_fact for sig in sigs]
            return [
                SaveBatchResult(index=0, id="sig-1"),
                SaveBatchResult(index=1, id="sig-2"),
            ]

        mem._storage.save_many = fake_save_many

        counts = mem.save_many(
            [
                {"text": "First batch fact", "tags": ["one", "two", "three"]},
                {"text": "Second batch fact", "tags": ["alpha", "beta", "gamma"]},
            ],
            dedupe=False,
        )

        assert counts == {"saved": 2, "skipped": 0, "errors": 0}
        assert captured["facts"] == ["First batch fact", "Second batch fact"]

    def test_save_many_dedupes_idempotency_key(self, mem):
        counts = mem.save_many(
            [
                {
                    "text": "First idem fact",
                    "tags": ["one", "two", "three"],
                    "idempotency_key": "idem-123",
                },
                {
                    "text": "Second idem fact",
                    "tags": ["four", "five", "six"],
                    "idempotency_key": "idem-123",
                },
            ],
            dedupe=False,
        )

        assert counts == {"saved": 1, "skipped": 1, "errors": 0}
        assert mem.count() == 1

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
