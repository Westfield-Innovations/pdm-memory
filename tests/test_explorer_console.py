# © 2026 Westfield Innovations LLC. Patent Pending.

"""Tests for Explorer console API actions."""

from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path

import pytest

pytest.importorskip("fastapi")

from fastapi.testclient import TestClient

from pdm_memory import Memory
from pdm_memory.tools.server import create_app


@pytest.fixture()
def console_db(tmp_path: Path) -> Path:
    db = tmp_path / "console.db"
    with Memory(store=str(db), user="default") as mem:
        mem.save(
            "Launch date for Orion is 2026-08-01",
            tags=["orion", "launch", "date"],
            drawer="product",
            p_magnitude=85,
            deadline=datetime(2026, 8, 1, tzinfo=timezone.utc),
        )
        mem.save(
            "Launch date for Orion is 2026-09-01",
            tags=["orion", "launch", "date"],
            drawer="product",
            p_magnitude=80,
            deadline=datetime(2026, 9, 1, tzinfo=timezone.utc),
        )
        mem.save(
            "User prefers dark mode",
            tags=["ui", "prefs", "theme"],
            drawer="prefs",
            p_magnitude=40,
        )
    return db


@pytest.fixture()
def client(console_db: Path) -> TestClient:
    return TestClient(create_app(store=str(console_db), user="default"))


class TestExplorerConsoleAPI:
    def test_search_recall(self, client: TestClient) -> None:
        res = client.get("/api/v1/search", params={"q": "orion launch"})
        assert res.status_code == 200
        body = res.json()
        assert body["count"] >= 1
        assert any("orion" in h["text"].lower() for h in body["hits"])

    def test_search_cost_finds_low_pressure_memory(self, client: TestClient) -> None:
        tight = client.get("/api/v1/search", params={"q": "dark mode", "search_cost": 0.65})
        assert tight.status_code == 200
        assert tight.json()["count"] == 0

        loose = client.get("/api/v1/search", params={"q": "dark mode", "search_cost": 0.9})
        assert loose.status_code == 200
        assert loose.json()["count"] >= 1
        assert loose.json()["search_cost"] == pytest.approx(0.9)

    def test_reinforce_memory(self, client: TestClient) -> None:
        with Memory(store=str(client.app.state.store), user="default") as mem:
            mid = mem._storage.list(user="default", limit=1)[0].id
            before = mem._storage.get(mid, user="default").p_magnitude

        res = client.post(f"/api/v1/memories/{mid}/reinforce", json={"coupling_score": 0.8})
        assert res.status_code == 200
        body = res.json()
        assert body["ok"] is True
        assert body["node"]["p_magnitude"] > before

    def test_delete_memory(self, client: TestClient) -> None:
        with Memory(store=str(client.app.state.store), user="default") as mem:
            mid = mem._storage.list(user="default", limit=1)[0].id

        res = client.delete(f"/api/v1/memories/{mid}")
        assert res.status_code == 200
        assert res.json()["deleted"] is True

        with Memory(store=str(client.app.state.store), user="default") as mem:
            assert mem._storage.get(mid, user="default") is None

    def test_resolve_torsion_heuristic(self, client: TestClient) -> None:
        tor = client.get("/api/v1/torsion").json()
        assert tor["count"] >= 1
        pair = tor["latest"]
        res = client.post(
            "/api/v1/torsion/resolve",
            json={
                "signature_a_id": pair["signature_a_id"],
                "signature_b_id": pair["signature_b_id"],
                "use_ai": False,
            },
        )
        assert res.status_code == 200
        body = res.json()
        assert body["ok"] is True
        assert body["method"] == "heuristic"
        assert body["new_memory_id"]
        assert len(body["deleted_ids"]) == 2

    def test_memory_map_projection_fields(self, client: TestClient) -> None:
        res = client.get("/api/v1/memory-map", params={"projected_days": 30})
        assert res.status_code == 200
        body = res.json()
        assert body["projected_days"] == 30
        node = body["nodes"][0]
        for key in ("half_life", "days_since_touch", "t_persistence", "v_correct", "v_total"):
            assert key in node


class TestMemoryDeleteReconcile:
    def test_delete_and_reconcile(self, tmp_path: Path) -> None:
        db = tmp_path / "mem.db"
        with Memory(store=str(db), user="default") as mem:
            a = mem.save("Fact A", tags=["x"], p_magnitude=50)
            b = mem.save("Fact B", tags=["x"], p_magnitude=45)
            new_id = mem.reconcile_torsion(a, b, "Merged fact A+B")
            assert new_id not in (a, b)
            assert mem._storage.get(a, user="default") is None
            assert mem._storage.get(b, user="default") is None
            merged = mem._storage.get(new_id, user="default")
            assert merged is not None
            assert merged.compressed_fact == "Merged fact A+B"

        with Memory(store=str(db), user="default") as mem:
            mid = mem.save("temp", tags=["t"], p_magnitude=30)
            assert mem.delete(mid) is True
            assert mem.delete("missing") is False
