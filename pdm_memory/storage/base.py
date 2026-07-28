# © 2026 Westfield Innovations LLC. Patent Pending.
# U.S. App. No. 19/739,419 | 63/953,563 | 63/953,842
# AUTHORIZED EXTENSION POINT (Westfield OS) — implementing this interface
# (e.g. BaseStorage) is PERMITTED. Core software modification remains
# prohibited without a commercial license from Westfield Innovations LLC.

"""
BaseStorage — Abstract interface for all PDM storage backends.

Task 1.1: Any backend (SQLite, cloud, Postgres, Redis…) must implement
this contract.  The Memory class depends only on BaseStorage — it never
knows which driver is active.
"""

from __future__ import annotations

import builtins
import hashlib
import logging
from abc import ABC, abstractmethod
from collections.abc import Iterator
from contextlib import contextmanager

from pdm_memory.core.signature import DrawerInfo, SignatureRecord

logger = logging.getLogger(__name__)


class BaseStorage(ABC):
    """
    Abstract storage interface for PDM signatures.

    Implement all methods to create a new storage backend.
    The Memory class will call only these methods — no direct DB access.
    """

    @abstractmethod
    def save(self, sig: SignatureRecord) -> str:
        """Persist a new signature. Returns sig.id."""
        ...

    @abstractmethod
    def get(self, memory_id: str, user: str = "default") -> SignatureRecord | None:
        """Retrieve a single active (non-deleted) signature by ID."""
        ...

    @abstractmethod
    def update(self, memory_id: str, user: str = "default", **fields) -> None:
        """Update whitelisted columns for ``memory_id`` owned by ``user``."""
        ...

    def update_batch(
        self,
        updates: builtins.list[tuple[str, dict]],
        user: str = "default",
    ) -> None:
        for memory_id, fields in updates:
            self.update(memory_id, user=user, **fields)

    @abstractmethod
    def delete(self, memory_id: str, user: str = "default") -> None:
        """Soft-delete a signature (``is_deleted=True`` where supported)."""
        ...

    def hard_delete(self, memory_id: str, user: str = "default") -> None:
        """Permanently remove a signature. Default: same as delete()."""
        self.delete(memory_id, user=user)

    @abstractmethod
    def list(
        self,
        user: str = "default",
        limit: int = 100,
        min_pressure: float = 0.0,
        drawer: str | None = None,
        cursor_id: str | None = None,
        include_deleted: bool = False,
    ) -> builtins.list[SignatureRecord]:
        """
        List signatures ordered by ``p_magnitude DESC, id DESC``.

        Keyset pagination: pass ``cursor_id`` from the last item of the previous page.
        """
        ...

    @abstractmethod
    def list_drawers(self, user: str = "default") -> builtins.list[DrawerInfo]:
        """List all drawers with aggregate stats."""
        ...

    def count(self, user: str = "default") -> int:
        return len(self.list(user=user, limit=10_000))

    def find_by_hash(self, text_hash: str, user: str = "default") -> SignatureRecord | None:
        for rec in self.list(user=user, limit=10_000):
            fact = rec.compressed_fact or ""
            if fact.startswith("[HASH:") and fact.endswith("]"):
                existing_hash = fact[6:-1]
            else:
                existing_hash = hashlib.sha256(fact.encode()).hexdigest()
            if existing_hash == text_hash:
                return rec
        return None

    def find_by_idempotency_key(
        self,
        idempotency_key: str,
        user: str = "default",
    ) -> SignatureRecord | None:
        key = idempotency_key.strip()
        if not key:
            return None
        for rec in self.list(user=user, limit=10_000):
            if rec.idempotency_key == key:
                return rec
            meta_key = (rec.metadata or {}).get("_idempotency_key")
            if meta_key == key:
                return rec
        return None

    def ping(self) -> bool:
        """Lightweight storage connectivity check."""
        try:
            self.list(user="__pdm_ping__", limit=1)
            return True
        except Exception as exc:
            logger.warning("[PDM] storage ping failed: %s", exc)
            return False

    @contextmanager
    def transaction(self) -> Iterator[None]:
        yield

    def close(self) -> None:
        pass
