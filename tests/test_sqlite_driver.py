"""Tests for SQLiteDriver — CRUD, decay, drawers, privacy mode."""

import pytest

from pdm_memory.core.signature import SignatureRecord
from pdm_memory.storage.sqlite_driver import SQLiteDriver


@pytest.fixture
def db_path(tmp_path):
    return str(tmp_path / "test.db")


@pytest.fixture
def driver(db_path):
    d = SQLiteDriver(db_path=db_path)
    yield d
    d.close()


def make_sig(text="Test memory", p_magnitude=60.0, tags=None, user="test_user", drawer="test_drawer") -> SignatureRecord:
    return SignatureRecord(
        user=user,
        compressed_fact=text,
        source="test",
        p_magnitude=p_magnitude,
        intent_tags=tags or ["test", "unit", "memory"],
        drawer_domain=drawer,
    )


class TestSQLiteDriverCRUD:
    def test_save_and_get(self, driver):
        sig = make_sig()
        memory_id = driver.save(sig)
        assert memory_id == sig.id

        retrieved = driver.get(memory_id, user=sig.user)
        assert retrieved is not None
        assert retrieved.compressed_fact == sig.compressed_fact
        assert retrieved.p_magnitude == sig.p_magnitude

    def test_get_wrong_user_returns_none(self, driver):
        sig = make_sig(user="alice")
        driver.save(sig)
        assert driver.get(sig.id, user="bob") is None

    def test_save_and_get_t_event_at(self, driver):
        from datetime import datetime, timezone

        event = datetime(2026, 7, 27, 15, 0, tzinfo=timezone.utc)
        sig = make_sig(text="Yesterday standup notes")
        sig.t_event_at = event
        driver.save(sig)
        got = driver.get(sig.id, user=sig.user)
        assert got is not None
        assert got.t_event_at is not None
        assert got.t_event_at.isoformat().startswith("2026-07-27T15:00:00")

    def test_migration_adds_t_event_at_column(self, db_path):
        """Existing Tier-3 DBs get t_event_at via apply_sqlite_migrations on open."""
        import sqlite3

        conn = sqlite3.connect(db_path)
        conn.executescript(
            """
            CREATE TABLE pdm_signatures (
                id TEXT PRIMARY KEY,
                user TEXT,
                compressed_fact TEXT,
                compressed_fact_hash TEXT,
                source TEXT,
                p_magnitude REAL,
                t_persistence REAL,
                phase_privilege REAL,
                effective_spike REAL,
                intent_tags TEXT,
                question_regime TEXT,
                domain TEXT,
                drawer_domain TEXT,
                retrieval_count INTEGER,
                last_retrieved TEXT,
                created_at TEXT,
                validation_prediction_total INTEGER,
                validation_prediction_correct INTEGER,
                decay_rate REAL,
                t_deadline TEXT,
                urgency_rate REAL,
                metadata TEXT,
                is_deleted INTEGER NOT NULL DEFAULT 0,
                idempotency_key TEXT
            );
            CREATE TABLE pdm_drawers (
                domain TEXT, user TEXT, description TEXT,
                PRIMARY KEY (domain, user)
            );
            """
        )
        conn.commit()
        conn.close()

        driver = SQLiteDriver(db_path=db_path)
        cols = {
            row[1]
            for row in driver._conn().execute("PRAGMA table_info(pdm_signatures)")
        }
        assert "t_event_at" in cols
        driver.close()

    def test_update(self, driver):
        sig = make_sig()
        driver.save(sig)
        driver.update(sig.id, user=sig.user, p_magnitude=90.0)
        updated = driver.get(sig.id, user=sig.user)
        assert updated.p_magnitude == pytest.approx(90.0)

    def test_update_wrong_user_is_noop(self, driver):
        """IDOR guard: Bob cannot mutate Alice's row by id alone."""
        sig = make_sig(user="alice", p_magnitude=60.0)
        driver.save(sig)
        driver.update(sig.id, user="bob", p_magnitude=1.0)
        assert driver.get(sig.id, user="alice").p_magnitude == pytest.approx(60.0)

    def test_update_rejects_unknown_column(self, driver):
        sig = make_sig()
        driver.save(sig)
        with pytest.raises(ValueError, match="non-whitelisted"):
            driver.update(
                sig.id,
                user=sig.user,
                **{"p_magnitude": 1, "x = 0; DROP TABLE pdm_signatures;--": "evil"},
            )
        # Original row intact
        assert driver.get(sig.id, user=sig.user).p_magnitude == pytest.approx(60.0)

    def test_update_batch_scoped_to_user(self, driver):
        alice = make_sig(user="alice", text="alice mem", p_magnitude=50.0)
        bob = make_sig(user="bob", text="bob mem", p_magnitude=50.0)
        driver.save(alice)
        driver.save(bob)
        driver.update_batch(
            [(alice.id, {"p_magnitude": 99.0}), (bob.id, {"p_magnitude": 11.0})],
            user="alice",
        )
        assert driver.get(alice.id, user="alice").p_magnitude == pytest.approx(99.0)
        # Bob's row must be untouched (wrong owner on batch)
        assert driver.get(bob.id, user="bob").p_magnitude == pytest.approx(50.0)

    def test_save_many_inserts_multiple_rows(self, driver):
        sigs = [
            make_sig(text="Bulk fact 1", drawer="bulk"),
            make_sig(text="Bulk fact 2", drawer="bulk"),
            make_sig(text="Bulk fact 3", drawer="bulk"),
        ]

        results = driver.save_many(sigs)

        assert [result.error for result in results] == [None, None, None]
        assert [result.id for result in results] == [sig.id for sig in sigs]
        assert driver.count(user="test_user") == 3


class TestUpdateBatch:
    """Tests for the pre-check update_batch() path (SQLite-specific semantics)."""

    def test_mixed_existing_and_missing_ids(self, driver):
        """Batch with some ids that exist and some that do not.

        Expected:
        - index 0 (existing) → UpdateBatchResult(error=None), p_magnitude updated
        - index 1 (missing)  → UpdateBatchResult(error="Memory not found or wrong user")
        - index 2 (existing) → UpdateBatchResult(error=None), p_magnitude updated
        - index 3 (missing)  → UpdateBatchResult(error="Memory not found or wrong user")
        """
        from pdm_memory.storage.base import UpdateBatchResult

        real0 = make_sig(text="Real sig 0", p_magnitude=40.0)
        real2 = make_sig(text="Real sig 2", p_magnitude=45.0)
        driver.save(real0)
        driver.save(real2)

        ghost_id1 = "nonexistent-id-1"
        ghost_id3 = "nonexistent-id-2"

        updates = [
            (real0.id,  {"p_magnitude": 90.0}),
            (ghost_id1, {"p_magnitude": 10.0}),
            (real2.id,  {"p_magnitude": 95.0}),
            (ghost_id3, {"p_magnitude": 10.0}),
        ]
        results = driver.update_batch(updates, user="test_user")

        assert len(results) == 4

        # Existing ids → success, DB updated.
        assert results[0].error is None
        assert results[0].id == real0.id
        assert driver.get(real0.id, user="test_user").p_magnitude == pytest.approx(90.0)

        assert results[2].error is None
        assert results[2].id == real2.id
        assert driver.get(real2.id, user="test_user").p_magnitude == pytest.approx(95.0)

        # Ghost ids → error, no crash.
        assert results[1].id == ghost_id1
        assert results[1].error == "Memory not found or wrong user"

        assert results[3].id == ghost_id3
        assert results[3].error == "Memory not found or wrong user"

    def test_wrong_user_ids_treated_as_missing(self, driver):
        """An id that belongs to a different user must appear as not-found."""
        alice = make_sig(user="alice", text="alice mem", p_magnitude=50.0)
        driver.save(alice)

        results = driver.update_batch(
            [(alice.id, {"p_magnitude": 1.0})],
            user="bob",  # wrong user
        )

        assert results[0].error == "Memory not found or wrong user"
        # Alice's record must be untouched.
        assert driver.get(alice.id, user="alice").p_magnitude == pytest.approx(50.0)

    def test_all_missing_returns_all_errors(self, driver):
        """When every id is missing the method still returns a result per entry."""
        results = driver.update_batch(
            [("ghost-a", {"p_magnitude": 1.0}), ("ghost-b", {"p_magnitude": 2.0})],
            user="test_user",
        )
        assert len(results) == 2
        assert all(r.error == "Memory not found or wrong user" for r in results)

    def test_empty_batch_returns_empty_list(self, driver):
        assert driver.update_batch([], user="test_user") == []


class TestSaveBatch:
    """Tests for the bulk-insert save_batch() path (SQLite-specific semantics)."""

    def test_mixed_new_and_existing_ids(self, driver):
        """Batch with some ids already in the DB and some fresh ones.

        Expected:
        - index 0 (new)      → SaveBatchResult(id=sig0.id, error=None)
        - index 1 (existing) → SaveBatchResult(id=None, error="Duplicate id already exists")
        - index 2 (new)      → SaveBatchResult(id=sig2.id, error=None)
        - index 3 (existing) → SaveBatchResult(id=None, error="Duplicate id already exists")
        - index 4 (new)      → SaveBatchResult(id=sig4.id, error=None)
        """
        # Pre-insert two sigs so they are "already in the DB".
        pre1 = make_sig(text="Pre-existing 1", drawer="mix")
        pre2 = make_sig(text="Pre-existing 2", drawer="mix")
        driver.save(pre1)
        driver.save(pre2)

        # Fresh sigs that are NOT yet in the DB.
        new0 = make_sig(text="New 0", drawer="mix")
        new2 = make_sig(text="New 2", drawer="mix")
        new4 = make_sig(text="New 4", drawer="mix")

        batch = [new0, pre1, new2, pre2, new4]
        results = driver.save_batch(batch)

        assert len(results) == 5

        # New records → success.
        assert results[0].id == new0.id
        assert results[0].error is None

        assert results[2].id == new2.id
        assert results[2].error is None

        assert results[4].id == new4.id
        assert results[4].error is None

        # Pre-existing records → duplicate error.
        assert results[1].id is None
        assert results[1].error == "Duplicate id already exists"

        assert results[3].id is None
        assert results[3].error == "Duplicate id already exists"

        # DB count: 2 pre-existing + 3 new = 5 total.
        assert driver.count(user="test_user") == 5

    def test_all_duplicate_ids_in_batch_itself(self, driver):
        """Same id appears twice in the batch — second occurrence is rejected."""
        sig = make_sig(text="Unique fact")
        results = driver.save_batch([sig, sig])

        assert len(results) == 2
        assert results[0].id == sig.id
        assert results[0].error is None
        # Second occurrence is a within-batch duplicate.
        assert results[1].id is None
        assert results[1].error == "Duplicate id in batch"

    def test_drawers_created_for_inserted_sigs(self, driver):
        """Drawers must be upserted for every successfully inserted sig."""
        sigs = [
            make_sig(text="Drawer A fact 1", drawer="drawer_a"),
            make_sig(text="Drawer B fact 1", drawer="drawer_b"),
        ]
        results = driver.save_batch(sigs)

        assert all(r.error is None for r in results)
        drawers = {d.domain for d in driver.list_drawers(user="test_user")}
        assert "drawer_a" in drawers
        assert "drawer_b" in drawers

    def test_empty_batch_returns_empty_list(self, driver):
        assert driver.save_batch([]) == []


class TestSQLiteDriverCRUDExtra:
    """Additional CRUD tests (delete, list, find_by_hash, count)."""

    def test_delete(self, driver):
        sig = make_sig()
        driver.save(sig)
        driver.delete(sig.id, user=sig.user)
        assert driver.get(sig.id, user=sig.user) is None

    def test_list(self, driver):
        for i in range(5):
            sig = make_sig(text=f"Memory {i}", p_magnitude=50 + i * 5)
            driver.save(sig)
        records = driver.list(user="test_user")
        assert len(records) == 5
        # Should be ordered by p_magnitude DESC
        pressures = [r.p_magnitude for r in records]
        assert pressures == sorted(pressures, reverse=True)

    def test_list_min_pressure_filter(self, driver):
        for p in [30, 50, 70, 90]:
            driver.save(make_sig(p_magnitude=p))
        records = driver.list(user="test_user", min_pressure=60)
        assert all(r.p_magnitude >= 60 for r in records)
        assert len(records) == 2

    def test_find_by_hash(self, driver):
        sig = make_sig(text="Unique dedupe text")
        driver.save(sig)
        from pdm_memory.storage.schema import hash_fact_text

        found = driver.find_by_hash(hash_fact_text("Unique dedupe text"), user=sig.user)
        assert found is not None
        assert found.id == sig.id
        assert driver.find_by_hash("deadbeef", user=sig.user) is None

    def test_count(self, driver):
        for _ in range(3):
            driver.save(make_sig())
        assert driver.count(user="test_user") == 3


class TestSQLiteDriverDrawers:
    def test_list_drawers(self, driver):
        sig = SignatureRecord(
            user="alice",
            compressed_fact="fact 1",
            source="test",
            p_magnitude=75,
            intent_tags=["a", "b", "c"],
            drawer_domain="finance",
        )
        driver.save(sig)
        drawers = driver.list_drawers(user="alice")
        assert any(d.domain == "finance" for d in drawers)

    def test_drawer_counts(self, driver):
        for _ in range(3):
            sig = make_sig()
            sig.drawer_domain = "my_drawer"
            driver.save(sig)
        drawers = driver.list_drawers(user="test_user")
        my_drawer = next((d for d in drawers if d.domain == "my_drawer"), None)
        assert my_drawer is not None
        assert my_drawer.signature_count == 3


class TestPrivacyMode:
    def test_store_raw_false(self, tmp_path):
        db_path = str(tmp_path / "private.db")
        driver = SQLiteDriver(db_path=db_path, store_raw=False)
        sig = make_sig(text="Sensitive information about user")
        driver.save(sig)

        # Raw text should not appear in the DB
        import sqlite3
        conn = sqlite3.connect(db_path)
        row = conn.execute("SELECT compressed_fact FROM pdm_signatures").fetchone()
        conn.close()
        driver.close()

        assert "Sensitive information" not in row[0]
        assert "[HASH:" in row[0]


class TestSQLiteDriverPersistence:
    def test_transaction_rollback(self, driver):
        sig_keep = make_sig(text="keep")
        driver.save(sig_keep)
        sig_rollback = make_sig(text="rollback")
        with pytest.raises(RuntimeError, match="boom"), driver.transaction():
            driver.save(sig_rollback)
            raise RuntimeError("boom")
        assert driver.get(sig_keep.id, user=sig_keep.user) is not None
        assert driver.get(sig_rollback.id, user=sig_rollback.user) is None

    def test_data_persists_across_instances(self, db_path):
        # Write with one driver instance
        d1 = SQLiteDriver(db_path=db_path)
        sig = make_sig()
        d1.save(sig)
        d1.close()

        # Read with a fresh instance
        d2 = SQLiteDriver(db_path=db_path)
        retrieved = d2.get(sig.id, user=sig.user)
        d2.close()

        assert retrieved is not None
        assert retrieved.compressed_fact == sig.compressed_fact
