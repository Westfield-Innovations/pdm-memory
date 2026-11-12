"""Owner-registered comparisons: exact, range, categorical and probabilistic.

Each comparator checks a criterion's declared ``expected`` once, at
``prepare``, and then compares one admissible value at a time, returning
``(met, allowed_by_model)``. ``allowed_by_model`` marks a miss the forecast
itself allowed for, such as a reading outside a 90 % band.
"""

from __future__ import annotations

import math
from collections.abc import Callable
from dataclasses import dataclass
from typing import Any

__all__ = ["COMPARATORS", "NOT_OBSERVED", "Comparator"]


class _NotObserved:
    """A valid source covered the whole window and saw nothing."""

    def __repr__(self) -> str:
        return "NOT_OBSERVED"


NOT_OBSERVED = _NotObserved()


@dataclass(frozen=True)
class Comparator:
    check_expected: Callable[[Any], None]
    compare: Callable[[Any, Any], tuple[bool, bool]]


def _number(value: Any, label: str) -> float:
    if type(value) not in (int, float) or not math.isfinite(value):
        raise TypeError(f"{label} must be a finite number")
    return float(value)


def _band(expected: Any) -> tuple[float, float]:
    if not isinstance(expected, dict) or not {"low", "high"} <= set(expected):
        raise ValueError("a band needs low and high")
    low, high = _number(expected["low"], "low"), _number(expected["high"], "high")
    if high < low:
        raise ValueError("a band's high must not be below its low")
    return low, high


def _check_probabilistic(expected: Any) -> None:
    _band(expected)
    p = _number(expected.get("probability"), "probability")
    if not 0 < p < 1:
        raise ValueError("probability must be between 0 and 1")


def _check_categorical(expected: Any) -> None:
    if not isinstance(expected, list) or not expected:
        raise ValueError("a categorical criterion lists its allowed values")


def _in_band(expected: Any, value: Any) -> bool:
    if value is NOT_OBSERVED:
        return False
    low, high = _band(expected)
    return low <= _number(value, "value") <= high


def _exact(expected: Any, value: Any) -> tuple[bool, bool]:
    return value is not NOT_OBSERVED and value == expected, False


def _range(expected: Any, value: Any) -> tuple[bool, bool]:
    return _in_band(expected, value), False


def _categorical(expected: Any, value: Any) -> tuple[bool, bool]:
    return ("absent" if value is NOT_OBSERVED else value) in expected, False


def _probabilistic(expected: Any, value: Any) -> tuple[bool, bool]:
    met = _in_band(expected, value)
    return met, not met and value is not NOT_OBSERVED


COMPARATORS: dict[tuple[str, str], Comparator] = {
    ("exact", "1"): Comparator(lambda e: None, _exact),
    ("range", "1"): Comparator(_band, _range),
    ("categorical", "1"): Comparator(_check_categorical, _categorical),
    ("probabilistic", "1"): Comparator(_check_probabilistic, _probabilistic),
}
