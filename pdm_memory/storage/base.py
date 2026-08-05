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
import logging
from abc import ABC, abstractmethod
from collections.abc import Generator, Sequence
from contextlib import contextmanager
from dataclasses import dataclass

from pdm_memory.core.signature import DrawerInfo, SignatureRecord

logger = logging.getLogger(__name__)


@dataclass
class SaveBatchResult:
    index: int
    id: str | None
    error: str | None = None


@dataclass
class UpdateBatchResult:
    index: int
    id: str | None
    error: str | None = None


class BaseStorage(ABC):
    """
    Abstract storage interface for PDM signatures.

    Every concrete driver (SQLite, Postgres, Cloud) implements the full
    required surface. Defaults remain only for helpers that are not yet
    uniform across drivers (``count``, ``find_by_hashes``,
    ``find_by_idempotency_keys``) and for no-ops (``transaction``, ``close``).

    The Memory class depends only on BaseStorage — it never knows which
    driver is active.
    """

    @abstractmethod
    def save(self, sig: SignatureRecord) -> str:
        """Persist a new signature. Returns sig.id."""
        ...

    @abstractmethod
    def save_batch(self, sigs: list[SignatureRecord]) -> list[SaveBatchResult]:
        """Bulk create. Partial success: per-index ``SaveBatchResult``."""
        ...

    @abstractmethod
    def get(self, memory_id: str, user: str = "default") -> SignatureRecord | None:
        """Retrieve a single active (non-deleted) signature by ID."""
        ...

    @abstractmethod
    def get_many(
        self,
        ids: builtins.list[str],
        user: str = "default",
    ) -> dict[str, SignatureRecord]:
        """
        Fetch multiple signatures by id.

        Returns a dict keyed by id; missing / foreign-user ids are omitted.
        """
        ...

    @abstractmethod
    def update(self, memory_id: str, user: str = "default", **fields) -> None:
        """Update whitelisted columns for ``memory_id`` owned by ``user``."""
        ...

    @abstractmethod
    def update_batch(
        self,
        updates: builtins.list[tuple[str, dict]],
        user: str = "default",
    ) -> builtins.list[UpdateBatchResult]:
        """Bulk update. Partial success: per-index ``UpdateBatchResult``."""
        ...

    @abstractmethod
    def delete(self, memory_id: str, user: str = "default") -> None:
        """Soft-delete a signature (``is_deleted=True`` where supported)."""
        ...

    @abstractmethod
    def hard_delete(self, memory_id: str, user: str = "default") -> None:
        """Permanently remove a signature."""
        ...

    @abstractmethod
    def list(
        self,
        user: str = "default",
        limit: int = 100,
        min_pressure: float = 0.0,
        drawer: str | None = None,
        cursor_id: str | None = None,
        include_deleted: bool = False,
        tag_any: Sequence[str] | None = None,
    ) -> builtins.list[SignatureRecord]:
        """
        List signatures ordered by ``p_magnitude DESC, id DESC``.

        Keyset pagination: pass ``cursor_id`` from the last item of the previous page.
        Optional ``tag_any``: prefer records whose ``intent_tags`` contain any token
        (drivers without native support may ignore or filter client-side).
        """
        ...

    @abstractmethod
    def list_drawers(self, user: str = "default") -> builtins.list[DrawerInfo]:
        """List all drawers with aggregate stats."""
        ...

    @abstractmethod
    def find_by_idempotency_key(
        self,
        idempotency_key: str,
        user: str = "default",
    ) -> SignatureRecord | None:
        """Return the live signature for ``idempotency_key``, if any."""
        ...

    def find_by_idempotency_keys(
        self,
        keys: builtins.list[str],
        user: str = "default",
    ) -> dict[str, SignatureRecord]:
        """Map idempotency_key → record. Default: loop find_by_idempotency_key."""
        result: dict[str, SignatureRecord] = {}
        for raw in keys:
            key = (raw or "").strip()
            if not key or key in result:
                continue
            rec = self.find_by_idempotency_key(key, user=user)
            if rec is not None:
                result[key] = rec
        return result

    @abstractmethod
    def find_by_hash(
        self, text_hash: str, user: str = "default"
    ) -> SignatureRecord | None:
        """Return the live signature matching content hash, if any."""
        ...

    def find_by_hashes(
        self,
        hashes: builtins.list[str],
        user: str = "default",
    ) -> dict[str, SignatureRecord]:
        """Map fact-hash → record for hashes that exist. Default: loop find_by_hash."""
        result: dict[str, SignatureRecord] = {}
        for text_hash in hashes:
            key = (text_hash or "").strip()
            if not key or key in result:
                continue
            rec = self.find_by_hash(key, user=user)
            if rec is not None:
                result[key] = rec
        return result

    @abstractmethod
    def ping(self) -> bool:
        """Lightweight storage connectivity check."""
        ...

    def count(self, user: str = "default") -> int:
        """Default: list + len (local drivers override with COUNT(*))."""
        return len(self.list(user=user, limit=10_000))

    @contextmanager
    def transaction(self) -> Generator[None]:
        yield

    def close(self) -> None:
        pass
