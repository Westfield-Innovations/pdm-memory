"""Tests for SQLiteDriver — CRUD, decay, drawers, privacy mode."""

import pytest

from pdm_memory.storage.sqlite_driver import SQLiteDriver
from pdm_memory.core.signature import SignatureRecord


@pytest.fixture
def db_path(tmp_path):
    return str(tmp_path / "test.db")


@pytest.fixture
def driver(db_path):
    d = SQLiteDriver(db_path=db_path)
    yield d
    d.close()


def make_sig(text="Test memory", p_magnitude=60.0, tags=None, user="test_user") -> SignatureRecord:
    return SignatureRecord(
        user=user,
        compressed_fact=text,
        source="test",
        p_magnitude=p_magnitude,
        intent_tags=tags or ["test", "unit", "memory"],
        drawer_domain="test_drawer",
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
        with pytest.raises(RuntimeError, match="boom"):
            with driver.transaction():
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
