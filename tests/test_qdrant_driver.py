# © 2026 Westfield Innovations LLC. Patent Pending.
# U.S. App. No. 19/739,419 | 63/953,563 | 63/953,842
# MODIFICATION PROHIBITED. USE AS SHIPPED.

"""Tests for QdrantDriver — CRUD, soft-delete, keyset list, factory wiring."""

from __future__ import annotations

import uuid
from typing import Any

import pytest

qdrant_client = pytest.importorskip("qdrant_client")

from pdm_memory import Memory
from pdm_memory.core.signature import SignatureRecord
from pdm_memory.storage.factory import create_storage
from pdm_memory.storage.qdrant_driver import QdrantDriver
from pdm_memory.storage.schema import hash_fact_text


@pytest.fixture
def driver() -> QdrantDriver:
    # Unique collection per fixture to isolate :memory: client state.
    name = f"pdm_{uuid.uuid4().hex[:12]}"
    d = QdrantDriver.from_url(f"qdrant://memory/{name}")
    yield d
    d.close()


def make_sig(
    text: str = "Test memory",
    p_magnitude: float = 60.0,
    tags: list[str] | None = None,
    user: str = "test_user",
    drawer: str = "test_drawer",
    **kwargs,
) -> SignatureRecord:
    return SignatureRecord(
        user=user,
        compressed_fact=text,
        source="test",
        p_magnitude=p_magnitude,
        intent_tags=tags or ["test", "unit", "memory"],
        drawer_domain=drawer,
        **kwargs,
    )


class TestQdrantCRUD:
    def test_save_and_get(self, driver: QdrantDriver) -> None:
        sig = make_sig()
        memory_id = driver.save(sig)
        assert memory_id == sig.id
        got = driver.get(memory_id, user=sig.user)
        assert got is not None
        assert got.compressed_fact == sig.compressed_fact
        assert got.p_magnitude == pytest.approx(sig.p_magnitude)

    def test_get_wrong_user_returns_none(self, driver: QdrantDriver) -> None:
        sig = make_sig(user="alice")
        driver.save(sig)
        assert driver.get(sig.id, user="bob") is None

    def test_duplicate_save_fails_fast(self, driver: QdrantDriver) -> None:
        sig = make_sig()
        driver.save(sig)
        with pytest.raises(ValueError, match="Duplicate"):
            driver.save(sig)

    def test_soft_delete_and_hard_delete(self, driver: QdrantDriver) -> None:
        sig = make_sig()
        driver.save(sig)
        driver.delete(sig.id, user=sig.user)
        assert driver.get(sig.id, user=sig.user) is None
        # Still countable as deleted point until hard_delete
        assert driver.get_many([sig.id], user=sig.user) == {}
        driver.hard_delete(sig.id, user=sig.user)
        assert driver._get_point_payload(sig.id, include_deleted=True) is None

    def test_update_and_batch(self, driver: QdrantDriver) -> None:
        a = make_sig(text="alpha facts here", p_magnitude=70)
        b = make_sig(text="beta facts here", p_magnitude=40)
        driver.save(a)
        driver.save(b)
        driver.update(a.id, user=a.user, p_magnitude=88.0)
        got = driver.get(a.id, user=a.user)
        assert got is not None
        assert got.p_magnitude == pytest.approx(88.0)

        results = driver.update_batch(
            [(b.id, {"p_magnitude": 11.0}), ("missing-id", {"p_magnitude": 1.0})],
            user=b.user,
        )
        assert results[0].error is None
        assert results[1].error is not None
        assert driver.get(b.id, user=b.user).p_magnitude == pytest.approx(11.0)

    def test_find_by_hash_and_idempotency(self, driver: QdrantDriver) -> None:
        text = "unique fact for hash"
        sig = make_sig(text=text, idempotency_key="idem-1")
        driver.save(sig)
        digest = hash_fact_text(text)
        by_hash = driver.find_by_hash(digest, user=sig.user)
        assert by_hash is not None
        assert by_hash.id == sig.id
        by_key = driver.find_by_idempotency_key("idem-1", user=sig.user)
        assert by_key is not None
        assert by_key.id == sig.id

        other = make_sig(text="another unique fact row", idempotency_key="idem-1")
        with pytest.raises(ValueError, match="Idempotency key already used"):
            driver.save(other)

    def test_save_batch_idempotency_collision(self, driver: QdrantDriver) -> None:
        a = make_sig(text="batch idem a fact", idempotency_key="shared-key")
        b = make_sig(text="batch idem b fact", idempotency_key="shared-key")
        results = driver.save_batch([a, b])
        assert results[0].id == a.id
        assert results[1].error is not None
        assert "idempotency" in results[1].error.lower()

    def test_list_order_and_keyset(self, driver: QdrantDriver) -> None:
        ids: list[str] = []
        for p in (90.0, 80.0, 70.0, 60.0, 50.0):
            sig = make_sig(text=f"fact at {p}", p_magnitude=p)
            driver.save(sig)
            ids.append(sig.id)

        page1 = driver.list(user="test_user", limit=2)
        assert [r.p_magnitude for r in page1] == [90.0, 80.0]

        page2 = driver.list(user="test_user", limit=2, cursor_id=page1[-1].id)
        assert [r.p_magnitude for r in page2] == [70.0, 60.0]

        page3 = driver.list(user="test_user", limit=10, cursor_id=page2[-1].id)
        assert [r.p_magnitude for r in page3] == [50.0]

    def test_list_tag_any(self, driver: QdrantDriver) -> None:
        driver.save(
            make_sig(text="aaa tagged fact", tags=["Alpha", "Beta", "Gamma"])
        )
        driver.save(make_sig(text="zzz other fact", tags=["zeta", "other", "stuff"]))
        hits = driver.list(user="test_user", tag_any=["alpha"], limit=10)
        assert len(hits) == 1
        assert "Alpha" in hits[0].intent_tags

    def test_list_scroll_uses_payload_include(
        self, driver: QdrantDriver, monkeypatch
    ) -> None:
        driver.save(make_sig(text="payload include fact one"))
        seen: list[Any] = []
        real_rpc = driver._rpc

        def tracking_rpc(method: str, /, *args, **kwargs):
            if method == "scroll":
                seen.append(kwargs.get("with_payload"))
            return real_rpc(method, *args, **kwargs)

        monkeypatch.setattr(driver, "_rpc", tracking_rpc)
        driver.list(user="test_user", limit=5)
        assert seen
        assert isinstance(seen[0], list)
        assert "compressed_fact" in seen[0]
        assert "kind" not in seen[0]
        assert "intent_tags_lc" not in seen[0]

    def test_update_intent_tags_refreshes_lc_index(self, driver: QdrantDriver) -> None:
        sig = make_sig(text="retag me please", tags=["oldtag", "keep", "x"])
        driver.save(sig)
        driver.update(
            sig.id,
            user=sig.user,
            intent_tags=["NewTag", "keep", "y"],
        )
        hits = driver.list(user="test_user", tag_any=["newtag"], limit=5)
        assert any(h.id == sig.id for h in hits)
        assert driver.list(user="test_user", tag_any=["oldtag"], limit=5) == []

    def test_list_drawers_and_count(self, driver: QdrantDriver) -> None:
        driver.save(make_sig(text="drawer a fact one", drawer="alpha"))
        driver.save(make_sig(text="drawer a fact two", drawer="alpha", p_magnitude=40))
        driver.save(make_sig(text="drawer b fact", drawer="beta"))
        drawers = {d.domain: d for d in driver.list_drawers(user="test_user")}
        assert drawers["alpha"].signature_count == 2
        assert drawers["beta"].signature_count == 1
        assert driver.count(user="test_user") == 3

    def test_drawer_counters_track_delete_and_pressure_update(
        self, driver: QdrantDriver
    ) -> None:
        a = make_sig(text="counter alpha one", drawer="alpha", p_magnitude=80)
        b = make_sig(text="counter alpha two", drawer="alpha", p_magnitude=40)
        driver.save(a)
        driver.save(b)
        drawers = {d.domain: d for d in driver.list_drawers(user="test_user")}
        assert drawers["alpha"].signature_count == 2
        assert drawers["alpha"].avg_pressure == pytest.approx(60.0)

        driver.update(a.id, user=a.user, p_magnitude=20.0)
        drawers = {d.domain: d for d in driver.list_drawers(user="test_user")}
        assert drawers["alpha"].signature_count == 2
        assert drawers["alpha"].avg_pressure == pytest.approx(30.0)

        driver.delete(b.id, user=b.user)
        drawers = {d.domain: d for d in driver.list_drawers(user="test_user")}
        assert drawers["alpha"].signature_count == 1
        assert drawers["alpha"].avg_pressure == pytest.approx(20.0)

    def test_update_uses_set_payload_not_full_replace(
        self, driver: QdrantDriver, monkeypatch
    ) -> None:
        sig = make_sig(text="set payload fact")
        driver.save(sig)
        calls: list[str] = []
        real_rpc = driver._rpc

        def tracking_rpc(method: str, /, *args, **kwargs):
            calls.append(method)
            return real_rpc(method, *args, **kwargs)

        monkeypatch.setattr(driver, "_rpc", tracking_rpc)
        driver.update(sig.id, user=sig.user, p_magnitude=33.0)
        assert "batch_update_points" in calls
        got = driver.get(sig.id, user=sig.user)
        assert got is not None
        assert got.p_magnitude == pytest.approx(33.0)
        assert got.compressed_fact == "set payload fact"

    def test_store_raw_false(self) -> None:
        name = f"pdm_priv_{uuid.uuid4().hex[:8]}"
        d = QdrantDriver.from_url(f"qdrant://memory/{name}", store_raw=False)
        try:
            sig = make_sig(text="secret preferences text")
            d.save(sig)
            got = d.get(sig.id, user=sig.user)
            assert got is not None
            assert got.compressed_fact.startswith("[HASH:")
        finally:
            d.close()

    def test_ping_and_save_batch(self, driver: QdrantDriver) -> None:
        assert driver.ping() is True
        a = make_sig(text="batch one fact")
        b = make_sig(text="batch two fact")
        results = driver.save_batch([a, b, a])
        assert results[0].id == a.id
        assert results[1].id == b.id
        assert results[2].error == "Duplicate id in batch"


class TestQdrantFactory:
    def test_create_storage_memory_url(self) -> None:
        name = f"pdm_fac_{uuid.uuid4().hex[:8]}"
        driver = create_storage(f"qdrant://memory/{name}")
        assert isinstance(driver, QdrantDriver)
        assert driver.ping() is True
        driver.close()

    def test_memory_end_to_end(self) -> None:
        name = f"pdm_e2e_{uuid.uuid4().hex[:8]}"
        driver = QdrantDriver.from_url(f"qdrant://memory/{name}")
        with Memory(storage=driver, user="alice") as mem:
            mid = mem.save(
                "User prefers metric units always",
                tags=["units", "metric", "prefs"],
                p_magnitude=75,
            )
            hits = mem.recall("metric units", k=3, reinforce=False)
            assert any(h.id == mid for h in hits)
            assert mem._storage.ping() is True
            assert type(mem._storage).__name__ == "QdrantDriver"

    def test_missing_collection_in_url_fails_fast(self) -> None:
        with pytest.raises(ValueError, match="collection"):
            QdrantDriver.from_url("qdrant://localhost:6333")
