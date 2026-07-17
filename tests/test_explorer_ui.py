# © 2026 Westfield Innovations LLC. Patent Pending.
# U.S. App. No. 19/739,419 | 63/953,563 | 63/953,842
# MODIFICATION PROHIBITED. USE AS SHIPPED.

"""Tests for PDM Explorer FastAPI dashboard."""

from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path

import pytest

pytest.importorskip("fastapi")

from fastapi.testclient import TestClient

from pdm_memory import Memory
from pdm_memory.tools.server import STATIC_DIR, create_app


@pytest.fixture()
def explorer_db(tmp_path: Path) -> Path:
    db = tmp_path / "explorer.db"
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
def client(explorer_db: Path) -> TestClient:
    app = create_app(store=str(explorer_db), user="default")
    return TestClient(app)


class TestExplorerAPI:
    def test_health(self, client: TestClient) -> None:
        res = client.get("/api/v1/health")
        assert res.status_code == 200
        body = res.json()
        assert body["status"] == "ok"

    def test_index_served(self, client: TestClient) -> None:
        assert STATIC_DIR.joinpath("index.html").is_file()
        res = client.get("/")
        assert res.status_code == 200
        assert "PDM" in res.text
        assert "d3" in res.text.lower() or "D3" in res.text

    def test_memory_map(self, client: TestClient) -> None:
        res = client.get("/api/v1/memory-map")
        assert res.status_code == 200
        body = res.json()
        assert body["count"] == 3
        assert len(body["nodes"]) == 3
        node = body["nodes"][0]
        for key in ("id", "text", "p_magnitude", "p_effective", "tags", "torsion_status"):
            assert key in node
        assert body["links"], "expected resonance links between orion memories"
        torsion_nodes = [n for n in body["nodes"] if n["torsion_status"] == "torsion"]
        assert len(torsion_nodes) >= 2

    def test_torsion_latest(self, client: TestClient) -> None:
        res = client.get("/api/v1/torsion")
        assert res.status_code == 200
        body = res.json()
        assert body["count"] >= 1
        assert body["latest"] is not None
        assert "torsion_score" in body["latest"]
        assert "explanation" in body["latest"]
