"""Strict, canonical JSON for Goal Loop records.

The same rules as the Boundary Lock ledger: no NaN or infinity, string keys
only, no cycles, bounded depth and size. ``Decimal`` values travel as strings
so a price such as ``39.50`` never passes through a float, and datetimes
travel as ISO 8601 text with an explicit offset.
"""

from __future__ import annotations

import json
import math
from datetime import datetime, timezone
from decimal import Decimal, InvalidOperation
from typing import Any

from pdm_memory.guarded_effects import ResourceLimitError

MAX_DEPTH = 32
MAX_VALUES = 10_000
DEFAULT_LIMIT = 1_048_576


def utc_now() -> datetime:
    return datetime.now(timezone.utc)


def require_aware(value: Any, label: str) -> datetime:
    if not isinstance(value, datetime):
        raise TypeError(f"{label} must be a datetime")
    if value.tzinfo is None or value.utcoffset() is None:
        raise ValueError(f"{label} must be timezone-aware")
    return value


def to_decimal(value: Any, label: str) -> Decimal:
    """Accept int, Decimal, finite float or numeric text; never bool."""
    if type(value) is bool:
        raise TypeError(f"{label} must be a number, not bool")
    if isinstance(value, Decimal):
        result = value
    elif type(value) is int:
        result = Decimal(value)
    elif type(value) is float:
        if not math.isfinite(value):
            raise ValueError(f"{label} must be finite")
        result = Decimal(str(value))
    elif type(value) is str:
        try:
            result = Decimal(value.strip())
        except InvalidOperation:
            raise ValueError(f"{label} is not a number: {value!r}") from None
    else:
        raise TypeError(f"{label} must be a number")
    if not result.is_finite():
        raise ValueError(f"{label} must be finite")
    return result


def format_datetime(value: datetime) -> str:
    return require_aware(value, "datetime").isoformat()


def parse_datetime(value: Any, label: str) -> datetime:
    if type(value) is not str:
        raise TypeError(f"{label} must be an ISO 8601 string")
    if value.endswith(("Z", "z")):
        # Python 3.10 fromisoformat does not read the Z suffix.
        value = value[:-1] + "+00:00"
    try:
        parsed = datetime.fromisoformat(value)
    except ValueError:
        raise ValueError(f"{label} is not ISO 8601: {value!r}") from None
    return require_aware(parsed, label)


def _check(value: Any) -> None:
    count = 0
    ancestors: set[int] = set()

    def visit(v: Any, depth: int) -> None:
        nonlocal count
        count += 1
        if count > MAX_VALUES or depth > MAX_DEPTH:
            raise ResourceLimitError("JSON exceeds the value-count or nesting bound")
        if v is None or type(v) in (bool, int, str):
            return
        if type(v) is float:
            if not math.isfinite(v):
                raise ValueError("JSON numbers must be finite")
            return
        if type(v) not in (dict, list):
            raise TypeError(f"Only plain JSON values are accepted, got {type(v).__name__}")
        if id(v) in ancestors:
            raise ValueError("JSON is cyclic")
        ancestors.add(id(v))
        try:
            if type(v) is dict:
                for k, child in v.items():
                    if type(k) is not str:
                        raise TypeError("JSON object keys must be strings")
                    visit(child, depth + 1)
            else:
                for child in v:
                    visit(child, depth + 1)
        finally:
            ancestors.remove(id(v))

    visit(value, 0)


def dumps(value: Any, *, limit: int = DEFAULT_LIMIT) -> str:
    """Canonical text: sorted keys, no spaces, UTF-8, bounded."""
    _check(value)
    encoded = json.dumps(
        value, ensure_ascii=False, sort_keys=True, separators=(",", ":"), allow_nan=False
    )
    if len(encoded.encode("utf-8")) > limit:
        raise ResourceLimitError(f"JSON exceeds the {limit}-byte bound")
    return encoded


def _reject_constant(name: str) -> Any:
    raise ValueError(f"JSON constant {name} is not accepted")


def loads(text: str, *, limit: int = DEFAULT_LIMIT) -> Any:
    if type(text) is not str:
        raise TypeError("JSON input must be str")
    if len(text.encode("utf-8")) > limit:
        raise ResourceLimitError(f"JSON exceeds the {limit}-byte bound")
    value = json.loads(text, parse_constant=_reject_constant)
    _check(value)
    return value
