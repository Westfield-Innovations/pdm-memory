"""Errors shared by the owner-held ledgers and their storage layer.

``pdm_memory.guarded_effects`` re-exports both, so existing imports keep working.
"""

from __future__ import annotations

__all__ = ["ConfigurationError", "LedgerIntegrityError"]


class ConfigurationError(ValueError):
    """An owner configuration needs explicit replacement or was superseded."""


class LedgerIntegrityError(RuntimeError):
    """Stored data does not match its captured content or history chain."""
