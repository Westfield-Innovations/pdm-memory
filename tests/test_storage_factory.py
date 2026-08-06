"""Tests for storage factory — URL resolution and custom backends."""

from __future__ import annotations

import pytest

from pdm_memory import Memory
from pdm_memory.storage.base import BaseStorage, SaveBatchResult, UpdateBatchResult
from pdm_memory.storage.factory import companion_token_refresh_url, create_storage, register_storage
from pdm_memory.storage.sqlite_driver import SQLiteDriver


class _StubStorage(BaseStorage):
    def save(self, sig):
        return sig.id

    def save_batch(self, sigs):
        return [SaveBatchResult(index=i, id=s.id) for i, s in enumerate(sigs)]

    def get(self, memory_id, user="default"):
        return None

    def get_many(self, ids, user="default"):
        return {}

    def update(self, memory_id, user="default", **fields):
        return None

    def update_batch(self, updates, user="default"):
        return [
            UpdateBatchResult(index=i, id=mid) for i, (mid, _) in enumerate(updates)
        ]

    def delete(self, memory_id, user="default"):
        return None

    def hard_delete(self, memory_id, user="default"):
        return None

    def list(
        self,
        user="default",
        limit=100,
        min_pressure=0.0,
        drawer=None,
        cursor_id=None,
        include_deleted=False,
    ):
        return []

    def list_drawers(self, user="default"):
        return []

    def find_by_idempotency_key(self, idempotency_key, user="default"):
        return None

    def find_by_hash(self, text_hash, user="default"):
        return None

    def ping(self):
        return True


class TestStorageFactory:
    def test_file_path_resolves_to_sqlite(self, tmp_path):
        db = str(tmp_path / "app.db")
        driver = create_storage(db)
        assert isinstance(driver, SQLiteDriver)
        driver.close()

    def test_sqlite_url(self, tmp_path):
        db = str(tmp_path / "url.db")
        driver = create_storage(f"sqlite:///{db}")
        assert isinstance(driver, SQLiteDriver)
        driver.close()

    def test_memory_accepts_storage_instance(self, tmp_path):
        db = str(tmp_path / "inject.db")
        stub = _StubStorage()
        mem = Memory(storage=stub, store=db, user="u")
        assert mem._storage is stub
        mem.close()

    def test_memory_rejects_invalid_storage(self):
        with pytest.raises(TypeError, match="BaseStorage"):
            Memory(storage=object())  # type: ignore[arg-type]

    def test_cloud_requires_token(self):
        with pytest.raises(ValueError, match="token"):
            create_storage("cloud")

    def test_cloud_builds_jwt_refresh_url(self):
        from pdm_memory.storage.cloud_driver import CloudDriver

        driver = create_storage(
            "cloud",
            token="access-token",
            refresh_token="refresh-token",
            cloud_url="https://api.azus.ai/",
        )
        assert isinstance(driver, CloudDriver)
        assert driver._auth._refresh_url == companion_token_refresh_url(
            "https://api.azus.ai/"
        )
        assert driver._auth._refresh_url.endswith("/api/v1/accounts/token/refresh/")
        driver.close()

    def test_postgres_requires_psycopg(self):
        from unittest.mock import patch

        # Simulate missing optional dep even when psycopg is installed locally.
        real_import = __import__

        def _block_psycopg(name, *args, **kwargs):
            if name == "psycopg" or name.startswith("psycopg."):
                raise ImportError("No module named 'psycopg'")
            if name == "pdm_memory.storage.postgres_driver":
                raise ImportError(
                    "PostgreSQL storage requires psycopg. "
                    'Install with: pip install "pdm-memory[postgres]"'
                )
            return real_import(name, *args, **kwargs)

        with (
            patch("builtins.__import__", side_effect=_block_psycopg),
            pytest.raises(ImportError, match="postgres"),
        ):
            create_storage("postgresql://localhost/pdm")

    def test_qdrant_memory_scheme(self):
        import uuid

        pytest.importorskip("qdrant_client")
        from pdm_memory.storage.qdrant_driver import QdrantDriver

        name = f"fac_{uuid.uuid4().hex[:8]}"
        driver = create_storage(f"qdrant://memory/{name}")
        assert isinstance(driver, QdrantDriver)
        driver.close()

    def test_unknown_scheme(self):
        with pytest.raises(ValueError, match="Unsupported storage scheme"):
            create_storage("mongodb://localhost/pdm")

    def test_register_custom_scheme(self, tmp_path):
        def _builder(url: str, **_: object) -> BaseStorage:
            return _StubStorage()

        register_storage("redis", _builder)
        driver = create_storage("redis://localhost:6379/0")
        assert isinstance(driver, _StubStorage)

    def test_memory_with_sqlite_url(self, tmp_path):
        db = str(tmp_path / "mem.db")
        with Memory(store=f"sqlite:///{db}", user="alice") as mem:
            mid = mem.save("fact", tags=["a", "b", "c"])
            assert mem.count() == 1
            assert mem._storage.get(mid, user="alice") is not None

    def test_update_batch_result_dataclass(self):
        res1 = UpdateBatchResult(index=0, id="sig-123")
        res2 = UpdateBatchResult(index=1, id=None, error="not found")

        assert res1.index == 0
        assert res1.id == "sig-123"
        assert res1.error is None

        assert res2.index == 1
        assert res2.id is None
        assert res2.error == "not found"
