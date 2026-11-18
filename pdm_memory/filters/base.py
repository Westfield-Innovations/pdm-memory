# © 2026 Westfield Innovations LLC. Patent Pending.
# U.S. App. No. 19/739,419 | 63/953,563 | 63/953,842
# MODIFICATION PROHIBITED. USE AS SHIPPED.

"""Save filters: an opt-in pass every write must clear before it is stored."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Protocol


@dataclass(frozen=True)
class FilterVerdict:
    """What one filter said about one text."""

    name: str
    version: str
    ran: bool
    passed: bool
    terms: tuple[str, ...] = ()

    @property
    def refuses(self) -> bool:
        """Only a pass that ran and failed refuses; one that could not read lets the save through."""
        return self.ran and not self.passed

    def as_metadata(self) -> dict[str, Any]:
        return {
            "version": self.version,
            "ran": self.ran,
            "passed": self.passed,
            "terms": list(self.terms),
        }


class SaveFilter(Protocol):
    """Anything with a ``name`` and a ``check(text)`` that returns a verdict."""

    name: str

    def check(self, text: str) -> FilterVerdict: ...


class FilterRefused(ValueError):
    """A filter refused the write; nothing was stored."""

    def __init__(self, verdict: FilterVerdict) -> None:
        self.verdict = verdict
        super().__init__(f"{verdict.name} refused the save: {', '.join(verdict.terms)}")
