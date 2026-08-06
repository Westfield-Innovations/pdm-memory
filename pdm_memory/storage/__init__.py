# © 2026 Westfield Innovations LLC. Patent Pending.
# U.S. App. No. 19/739,419 | 63/953,563 | 63/953,842
# MODIFICATION PROHIBITED. USE AS SHIPPED.

"""pdm_memory.storage package."""

from __future__ import annotations

from typing import Any

from pdm_memory.storage.base import BaseStorage
from pdm_memory.storage.factory import create_storage, register_storage
from pdm_memory.storage.sqlite_driver import SQLiteDriver

__all__ = [
    "BaseStorage",
    "SQLiteDriver",
    "QdrantDriver",
    "create_storage",
    "register_storage",
]


def __getattr__(name: str) -> Any:
    # Lazy optional drivers — ImportError only when accessed without extras.
    if name == "QdrantDriver":
        from pdm_memory.storage.qdrant_driver import QdrantDriver

        return QdrantDriver
    if name == "PostgresDriver":
        from pdm_memory.storage.postgres_driver import PostgresDriver

        return PostgresDriver
    raise AttributeError(f"module {__name__!r} has no attribute {name!r}")
