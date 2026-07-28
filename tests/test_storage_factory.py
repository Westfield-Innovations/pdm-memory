"""Tests for storage factory — URL resolution and custom backends."""

from __future__ import annotations

import pytest

from pdm_memory import Memory
from pdm_memory.storage.base import BaseStorage
from pdm_memory.storage.factory import companion_token_refresh_url, create_storage, register_storage
from pdm_memory.storage.sqlite_driver import SQLiteDriver


class _StubStorage(BaseStorage):
    def save(self, sig):
        return sig.id

    def get(self, memory_id, user="default"):
        return None

    def update(self, memory_id, user="default", **fields):
        return None

    def delete(self, memory_id, user="default"):
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
        with pytest.raises(ImportError, match="postgres"):
            create_storage("postgresql://localhost/pdm")

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
