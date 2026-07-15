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

from abc import ABC, abstractmethod
from typing import List, Optional

from pdm_memory.core.signature import DrawerInfo, SignatureRecord


class BaseStorage(ABC):
    """
    Abstract storage interface for PDM signatures.

    Implement all methods to create a new storage backend.
    The Memory class will call only these methods — no direct DB access.
    """

    @abstractmethod
    def save(self, sig: SignatureRecord) -> str:
        """
        Persist a new signature.

        Args:
            sig: The SignatureRecord to store.

        Returns:
            The assigned memory ID (sig.id).
        """
        ...

    @abstractmethod
    def get(self, memory_id: str, user: str = "default") -> Optional[SignatureRecord]:
        """
        Retrieve a single signature by ID.

        Args:
            memory_id: The UUID string of the signature.
            user:      Owner filter (prevents cross-user access).

        Returns:
            SignatureRecord or None if not found.
        """
        ...

    @abstractmethod
    def update(self, memory_id: str, user: str = "default", **fields) -> None:
        """
        Update specific fields of an existing signature.

        Args:
            memory_id: The UUID string of the signature.
            user:      Owner filter (required to prevent cross-user IDOR).
            **fields:  Whitelisted field names and new values to update.
        """
        ...

    def update_batch(
        self,
        updates: List[tuple[str, dict]],
        user: str = "default",
    ) -> None:
        """
        Update multiple signatures in a batch.

        Args:
            updates: List of ``(memory_id, fields_dict)`` pairs.
            user:    Owner filter applied to every row.
        """
        for memory_id, fields in updates:
            self.update(memory_id, user=user, **fields)

    @abstractmethod
    def delete(self, memory_id: str, user: str = "default") -> None:
        """
        Delete a signature by ID.

        Args:
            memory_id: The UUID string.
            user:      Owner filter for safety.
        """
        ...

    @abstractmethod
    def list(
        self,
        user: str = "default",
        limit: int = 100,
        min_pressure: float = 0.0,
        drawer: Optional[str] = None,
    ) -> List[SignatureRecord]:
        """
        List signatures for a user, optionally filtered.

        Args:
            user:         Owner filter.
            limit:        Maximum records to return.
            min_pressure: Only return signatures with p_magnitude >= this.
            drawer:       Optional drawer_domain filter.

        Returns:
            List[SignatureRecord] ordered by p_magnitude descending.
        """
        ...

    @abstractmethod
    def list_drawers(self, user: str = "default") -> List[DrawerInfo]:
        """
        List all drawers (categories) with aggregate stats.

        Returns:
            List[DrawerInfo] ordered by domain name.
        """
        ...

    def count(self, user: str = "default") -> int:
        """
        Return total number of signatures for a user.
        Default implementation via list() — backends may override for efficiency.
        """
        return len(self.list(user=user, limit=10_000))

    def close(self) -> None:
        """Optional: release any open connections. Called by Memory.close()."""
        pass
