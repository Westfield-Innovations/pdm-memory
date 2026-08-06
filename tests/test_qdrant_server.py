# © 2026 Westfield Innovations LLC. Patent Pending.
# U.S. App. No. 19/739,419 | 63/953,563 | 63/953,842
# MODIFICATION PROHIBITED. USE AS SHIPPED.

"""
Live Qdrant server integration tests (optional / manual).

Skip unless ``QDRANT_URL`` is set::

    docker run --rm -p 6333:6333 qdrant/qdrant:v1.13.2
    QDRANT_URL=http://localhost:6333 pytest -m qdrant_server
"""

from __future__ import annotations

import os
import uuid
from urllib.parse import urlparse

import pytest

pytest.importorskip("qdrant_client")
pytest.importorskip("tenacity")

from pdm_memory import Memory
from pdm_memory.core.signature import SignatureRecord
from pdm_memory.storage.factory import create_storage
from pdm_memory.storage.qdrant_driver import QdrantDriver

QDRANT_URL = (os.environ.get("QDRANT_URL") or "").strip()

pytestmark = [
    pytest.mark.qdrant_server,
    pytest.mark.skipif(
        not QDRANT_URL,
        reason="QDRANT_URL not set (live Qdrant required)",
    ),
]


def _store_url(collection_name: str) -> str:
    """Map ``http(s)://host:port`` → ``qdrant(s)://host:port/collection``."""
    base = QDRANT_URL.rstrip("/")
    parsed = urlparse(base)
    if parsed.scheme in {"http", "https"} and (parsed.path in {"", "/"}):
        scheme = "qdrants" if parsed.scheme == "https" else "qdrant"
        return f"{scheme}://{parsed.netloc}/{collection_name}?prefer_grpc=false"
    return f"{base}/{collection_name}?prefer_grpc=false"


@pytest.fixture
def collection_name() -> str:
    return f"pdm_ci_{uuid.uuid4().hex[:12]}"


@pytest.fixture
def driver(collection_name: str) -> QdrantDriver:
    d = QdrantDriver.from_url(_store_url(collection_name))
    yield d
    try:
        d._rpc("delete_collection", collection_name=d.collection)
        d._rpc("delete_collection", collection_name=d.drawers_collection)
    except Exception:
        pass
    d.close()


def test_live_save_list_order_and_drawers(driver: QdrantDriver) -> None:
    for p in (90.0, 70.0, 50.0):
        driver.save(
            SignatureRecord(
                user="ci",
                compressed_fact=f"live fact {p}",
                source="test",
                p_magnitude=p,
                intent_tags=["live", "qdrant", "ci"],
                drawer_domain="main",
            )
        )
    page = driver.list(user="ci", limit=2)
    assert [r.p_magnitude for r in page] == [90.0, 70.0]
    drawers = driver.list_drawers(user="ci")
    assert len(drawers) == 1
    assert drawers[0].signature_count == 3
    assert driver.ping() is True


def test_live_factory_and_memory(collection_name: str) -> None:
    store = _store_url(collection_name)
    storage = create_storage(store)
    assert isinstance(storage, QdrantDriver)
    try:
        with Memory(storage=storage, user="ci") as mem:
            mid = mem.save(
                "live memory round trip fact",
                tags=["live", "memory", "ci"],
                p_magnitude=65,
            )
            hits = mem.recall("round trip", k=3, reinforce=False)
            assert any(h.id == mid for h in hits)
    finally:
        try:
            storage._rpc("delete_collection", collection_name=storage.collection)
            storage._rpc(
                "delete_collection", collection_name=storage.drawers_collection
            )
        except Exception:
            pass
        storage.close()
