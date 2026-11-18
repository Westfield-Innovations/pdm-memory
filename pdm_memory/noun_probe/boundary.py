# © 2026 Westfield Innovations LLC. Patent Pending.
# U.S. App. No. 19/739,419 | 63/953,563 | 63/953,842
# MODIFICATION PROHIBITED. USE AS SHIPPED.

"""
Phase 0 — Boundary Lock (runtime gate for the Noun Probe).

Fail Fast: an incomplete or undeclared boundary aborts the run instead of
silently degrading. This is the mandatory precondition for every later
phase — nothing perturbs until the boundary holds.
"""

from __future__ import annotations

from collections.abc import Iterable

from pdm_memory.noun_probe.contracts import Boundary

_REQUIRED_AXES: tuple[str, ...] = ("system", "domain", "scale")


class BoundaryError(ValueError):
    """Raised when a boundary is missing, incomplete, or undeclared (Law 0)."""


def lock_boundary(
    *,
    system: str,
    domain: str,
    scale: str,
    not_analyzed: Iterable[str],
) -> Boundary:
    """
    Validate and freeze a `Boundary`.

    Raises:
        BoundaryError: if any required axis is blank, or if `not_analyzed`
        does not explicitly declare at least one out-of-scope item.
    """
    axes = {"system": system, "domain": domain, "scale": scale}
    blanks = [name for name in _REQUIRED_AXES if not _is_filled(axes[name])]
    if blanks:
        raise BoundaryError(f"boundary axes not declared: {', '.join(blanks)}")

    declared = _normalize_not_analyzed(not_analyzed)
    if not declared:
        raise BoundaryError("not_analyzed must explicitly declare what is out of scope")

    return Boundary(
        system=system.strip(),
        domain=domain.strip(),
        scale=scale.strip(),
        not_analyzed=declared,
    )


def _is_filled(value: object) -> bool:
    return isinstance(value, str) and bool(value.strip())


def _normalize_not_analyzed(items: Iterable[str]) -> tuple[str, ...]:
    # A bare string is iterable char-by-char — that is never a declared set.
    if isinstance(items, (str, bytes)):
        raise BoundaryError(
            "not_analyzed must be an iterable of strings, not a single string"
        )

    seen: set[str] = set()
    unique: list[str] = []
    for item in items:
        if not _is_filled(item):
            continue
        cleaned = item.strip()
        if cleaned not in seen:
            seen.add(cleaned)
            unique.append(cleaned)
    return tuple(unique)
