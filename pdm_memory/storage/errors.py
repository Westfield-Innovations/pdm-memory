# © 2026 Westfield Innovations LLC. Patent Pending.
# U.S. App. No. 19/739,419 | 63/953,563 | 63/953,842
# MODIFICATION PROHIBITED. USE AS SHIPPED.

"""Cloud storage errors — fail fast, never pretend emptiness."""

from __future__ import annotations


class CloudStorageError(RuntimeError):
    """Network / HTTP failure talking to AZUS Companion API."""

    def __init__(
        self,
        message: str,
        *,
        status_code: int | None = None,
        path: str | None = None,
    ) -> None:
        super().__init__(message)
        self.status_code = status_code
        self.path = path


class CloudNotFoundError(CloudStorageError):
    """Resource does not exist (HTTP 404). ``get()`` maps this to ``None``."""
