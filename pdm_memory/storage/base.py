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

    **Required** (abstract — must implement in every driver):
    ``save``, ``get``, ``update``, ``delete``, ``list``, ``list_drawers``.

    **Optional hooks with defaults** (override for real performance):
    ``save_batch`` / ``save_many``, ``update_batch``, ``get_many``,
    ``count``, ``find_by_hash``, ``find_by_idempotency_key``, ``ping``,
    ``hard_delete``, ``transaction``, ``close``.

    The Memory class depends only on BaseStorage — it never knows which
    driver is active.
    """

    @abstractmethod
    def save(self, sig: SignatureRecord) -> str:
        """Persist a new signature. Returns sig.id."""
        ...

    def save_batch(self, sigs: list[SignatureRecord]) -> list[SaveBatchResult]:
        results = []
        with self.transaction():
            for i, sig in enumerate(sigs):
                try:
                    new_id = self.save(sig)
                    results.append(SaveBatchResult(index=i, id=new_id))
                except Exception as e:
                    results.append(SaveBatchResult(index=i, id=None, error=str(e)))
        return results

    save_many = save_batch

    @abstractmethod
    def get(self, memory_id: str, user: str = "default") -> SignatureRecord | None:
        """Retrieve a single active (non-deleted) signature by ID."""
        ...

    def get_many(
        self,
        ids: builtins.list[str],
        user: str = "default",
    ) -> dict[str, SignatureRecord]:
        """Fetch multiple signatures by id in one call.

        Default: loops over ``get()``.  Drivers should override with a real
        bulk query (``WHERE id IN (...)``) for real performance.

        Returns a dict keyed by id; ids that don't exist (or belong to
        another user) are simply absent from the result — no error per id,
        since a missing record is a valid, expected outcome here (not a
        batch-operation failure like in save_batch/update_batch).
        """
        result: dict[str, SignatureRecord] = {}
        for memory_id in ids:
            rec = self.get(memory_id, user=user)
            if rec is not None:
                result[memory_id] = rec
        return result

    @abstractmethod
    def update(self, memory_id: str, user: str = "default", **fields) -> None:
        """Update whitelisted columns for ``memory_id`` owned by ``user``."""
        ...

    def update_batch(
        self,
        updates: builtins.list[tuple[str, dict]],
        user: str = "default",
    ) -> builtins.list[UpdateBatchResult]:
        if not updates:
            return []
        results: list[UpdateBatchResult] = []
        with self.transaction():
            for i, (memory_id, fields) in enumerate(updates):
                try:
                    self.update(memory_id, user=user, **fields)
                    results.append(UpdateBatchResult(index=i, id=memory_id))
                except Exception as e:
                    results.append(UpdateBatchResult(index=i, id=memory_id, error=str(e)))
        return results

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

    def ping(self) -> bool:
        """Lightweight storage connectivity check."""
        try:
            self.list(user="__pdm_ping__", limit=1)
            return True
        except Exception as exc:
            logger.warning("[PDM] storage ping failed: %s", exc)
            return False

    @contextmanager
    def transaction(self) -> Generator[None]:
        yield

    def close(self) -> None:
        pass
