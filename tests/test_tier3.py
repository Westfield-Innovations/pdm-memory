# © 2026 Westfield Innovations LLC. Patent Pending.
"""Tier 3 tests — pagination, soft delete, idempotency, ping, recall hook."""

from __future__ import annotations

import pytest

from pdm_memory import Memory
from pdm_memory.models import MemoryListPage
from pdm_memory.storage.factory import create_storage
from pdm_memory.storage.sqlite_driver import SQLiteDriver


class TestKeysetPagination:
    def test_list_pages(self, tmp_path):
        db = str(tmp_path / "pages.db")
        with Memory(store=db, user="u") as mem:
            for i in range(12):
                mem.save(f"Fact {i:02d}", tags=["page", "test", str(i)], p_magnitude=50 + i)

            page1 = mem.list(limit=5)
            assert isinstance(page1, MemoryListPage)
            assert len(page1.items) == 5
            assert page1.next_cursor_id is not None

            page2 = mem.list(limit=5, cursor_id=page1.next_cursor_id)
            assert len(page2.items) == 5
            assert page2.items[0].p_raw <= page1.items[-1].p_raw

    def test_recall_uses_pagination_not_single_bulk(self, tmp_path, monkeypatch):
        db = str(tmp_path / "recall_page.db")
        calls: list[int] = []

        with Memory(store=db, user="u") as mem:
            for i in range(8):
                mem.save(f"Memory {i}", tags=["recall", "page", str(i)], p_magnitude=40 + i)

            original_list = mem._storage.list

            def tracking_list(*args, **kwargs):
                limit = kwargs.get("limit", args[2] if len(args) > 2 else 100)
                calls.append(limit)
                return original_list(*args, **kwargs)

            monkeypatch.setattr(mem._storage, "list", tracking_list)
            hits = mem.recall(
                "memory",
                k=3,
                reinforce=False,
                candidate_limit=8,
                page_size=3,
                search_cost=0.85,
            )
            assert hits
            assert len(calls) >= 2
            assert max(calls) <= 3


class TestSoftDelete:
    def test_delete_is_soft(self, tmp_path):
        db = str(tmp_path / "soft.db")
        with Memory(store=db, user="u") as mem:
            mid = mem.save("Soft delete me", tags=["soft", "delete", "test"])
            assert mem.delete(mid) is True
            assert mem.get(mid) is None
            assert mem.count() == 0

            driver = mem._storage
            assert isinstance(driver, SQLiteDriver)
            row = driver._conn().execute(
                "SELECT is_deleted FROM pdm_signatures WHERE id = ?", (mid,)
            ).fetchone()
            assert row["is_deleted"] == 1


class TestIdempotencyKey:
    def test_save_idempotency_key(self, tmp_path):
        db = str(tmp_path / "idem.db")
        with Memory(store=db, user="u") as mem:
            a = mem.save("Pay invoice", tags=["pay", "invoice"], idempotency_key="pay-123")
            b = mem.save("Different text", tags=["x"], idempotency_key="pay-123")
            assert a == b
            assert mem.count() == 1


class TestStoragePing:
    def test_sqlite_ping(self, tmp_path):
        driver = create_storage(str(tmp_path / "ping.db"))
        assert driver.ping() is True
        driver.close()

    def test_explorer_health_includes_storage(self, tmp_path):
        pytest.importorskip("fastapi")
        from fastapi.testclient import TestClient

        from pdm_memory.tools.server import create_app

        db = str(tmp_path / "health.db")
        client = TestClient(create_app(store=db, user="default"))
        res = client.get("/api/v1/health")
        assert res.status_code == 200
        body = res.json()
        assert body["status"] == "ok"
        assert body["storage_ok"] is True


class TestRecallHook:
    def test_on_recall_callback(self, tmp_path):
        db = str(tmp_path / "hook.db")
        seen: list[str] = []

        with Memory(store=db, user="u") as mem:
            mem.save("Hook target", tags=["hook", "test", "recall"], p_magnitude=80)
            hits = mem.recall(
                "hook target",
                k=3,
                reinforce=False,
                on_recall=lambda h: seen.append(h.id),
            )
            assert hits
            assert seen == [hits[0].id]
