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


class CloudConflictError(CloudStorageError):
    """
    The server refused a write because a live row already occupies its scope
    (HTTP 409) — a second open membership, filing or link where at most one
    may be live at a time.

    ``error_code`` carries the server's own code (``MEMBERSHIP_REFUSED``,
    ``RELATIONSHIP_REFUSED``, ``MEMBERSHIP_ALREADY_CLOSED``, …) when the body
    supplied one, so a caller can react to which conflict this was rather than
    parsing prose out of the message.
    """

    def __init__(
        self,
        message: str,
        *,
        error_code: str | None = None,
        status_code: int = 409,
        path: str | None = None,
    ) -> None:
        super().__init__(message, status_code=status_code, path=path)
        self.error_code = error_code
