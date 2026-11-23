"""JSON wire codec and error registry shared by the ledger host and its client."""

from __future__ import annotations

import math
import types
import typing
from collections.abc import Mapping
from dataclasses import MISSING, fields, is_dataclass
from datetime import datetime
from decimal import Decimal, InvalidOperation
from enum import Enum
from typing import Any, Literal, NoReturn, Union

from pdm_memory._ledger.errors import ConfigurationError, LedgerIntegrityError
from pdm_memory.consequence.records import (
    NotSupported,
    ReconcileFirst,
    Revoked,
    UnauthorizedChange,
)
from pdm_memory.goal_loop.store import JobExistsError, JobNotFoundError, StaleJobError
from pdm_memory.guarded_effects import BranchError, IdReuseError, ResourceLimitError
from pdm_memory.storage.errors import CloudStorageError

PROTOCOL = 1
MAX_DEPTH = 32


class WireError(ValueError):
    """A payload does not fit the type it was decoded as."""


ERRORS: dict[str, tuple[type[BaseException], int]] = {
    cls.__name__: (cls, status)
    for status, classes in {
        409: (
            IdReuseError,
            BranchError,
            ConfigurationError,
            JobExistsError,
            StaleJobError,
            ReconcileFirst,
            Revoked,
        ),
        404: (KeyError, JobNotFoundError),
        403: (UnauthorizedChange, PermissionError),
        422: (ResourceLimitError, NotSupported, ValueError, TypeError, WireError),
        500: (LedgerIntegrityError,),
    }.items()
    for cls in classes
}


def error_response(exc: BaseException) -> tuple[int, dict[str, Any]] | None:
    for cls in type(exc).__mro__:
        entry = ERRORS.get(cls.__name__)
        if entry and entry[0] is cls:
            message = (
                str(exc.args[0]) if isinstance(exc, KeyError) and exc.args else str(exc)
            )
            body = {
                "error_code": "LEDGER_ERROR",
                "detail": message,
                "error": {"type": cls.__name__, "message": message},
            }
            return entry[1], body
    return None


def raise_error(body: Mapping[str, Any]) -> NoReturn:
    err = body.get("error") if isinstance(body, Mapping) else None
    if not isinstance(err, Mapping):
        raise CloudStorageError("malformed error response")
    message = err.get("message")
    message = message if isinstance(message, str) else ""
    name = err.get("type")
    entry = ERRORS.get(name) if isinstance(name, str) else None
    if entry is None:
        raise CloudStorageError(f"{name}: {message}")
    raise entry[0](message)


def encode(value: Any) -> Any:
    if isinstance(value, Enum):
        return value.value
    if isinstance(value, datetime):
        if value.tzinfo is None or value.utcoffset() is None:
            raise ValueError("datetime must be timezone-aware")
        return value.isoformat()
    if isinstance(value, Decimal):
        return str(value)
    if hasattr(value, "__dataclass_fields__"):
        # Optional fields at their default stay out, matching the stored form.
        return {
            f.name: encode(getattr(value, f.name))
            for f in fields(value)
            if not (f.metadata.get("optional") and getattr(value, f.name) == f.default)
        }
    if isinstance(value, Mapping):
        if all(isinstance(k, str) for k in value):
            return {encode(k): encode(v) for k, v in value.items()}
        return [[encode(k), encode(v)] for k, v in value.items()]
    if isinstance(value, (tuple, list)):
        return [encode(v) for v in value]
    if isinstance(value, (set, frozenset)):
        return sorted(encode(v) for v in value)
    return value


def decode(tp: Any, data: Any) -> Any:
    return _decode(tp, data, 0)


def _decode(tp: Any, data: Any, depth: int) -> Any:
    if depth > MAX_DEPTH:
        raise WireError("payload is nested too deeply")
    if tp is Any or tp is object:
        return _json(data, depth)
    if tp is None or tp is type(None):
        return _need(data is None, tp, data)
    origin = typing.get_origin(tp)
    args = typing.get_args(tp)
    if origin is Union or origin is types.UnionType:
        for arm in args:
            try:
                return _decode(arm, data, depth)
            except WireError:
                pass
        raise WireError(f"{data!r} fits none of {tp}")
    if origin is Literal:
        if any(type(a) is type(data) and a == data for a in args):
            return data
        raise WireError(f"{data!r} is not one of {args}")
    if isinstance(tp, type):
        if issubclass(tp, Enum):
            try:
                return tp(data)
            except ValueError as exc:
                raise WireError(f"{data!r} is not a {tp.__name__}") from exc
        if tp is bool:
            return _need(type(data) is bool, tp, data)
        if tp is int:
            return _need(type(data) is int, tp, data)
        if tp is float:
            _need(type(data) in (int, float), tp, data)
            try:
                number = float(data)
            except OverflowError as exc:
                raise WireError("integer too large for a float") from exc
            if not math.isfinite(number):
                raise WireError("float must be finite")
            return number
        if tp is str:
            return _need(isinstance(data, str), tp, data)
        if tp is datetime:
            _need(isinstance(data, str), tp, data)
            try:
                moment = datetime.fromisoformat(data)
            except ValueError as exc:
                raise WireError(f"{data!r} is not an ISO datetime") from exc
            if moment.tzinfo is None or moment.utcoffset() is None:
                raise WireError("datetime must be timezone-aware")
            return moment
        if tp is Decimal:
            _need(isinstance(data, str), tp, data)
            try:
                number = Decimal(data)
            except InvalidOperation as exc:
                raise WireError(f"{data!r} is not a decimal") from exc
            if not number.is_finite():
                raise WireError("decimal must be finite")
            return number
        if is_dataclass(tp):
            return _record(tp, data, depth)
    base = origin or tp
    if base is tuple:
        _need(isinstance(data, list), tp, data)
        if len(args) == 2 and args[1] is Ellipsis:
            return tuple(_decode(args[0], v, depth + 1) for v in data)
        if len(args) != len(data):
            raise WireError(f"expected {len(args)} items, got {len(data)}")
        return tuple(_decode(a, v, depth + 1) for a, v in zip(args, data))
    if base in (list, frozenset, set):
        _need(isinstance(data, list), tp, data)
        item = args[0] if args else Any
        return base(_decode(item, v, depth + 1) for v in data)
    if base in (dict, Mapping):
        return _mapping(args or (Any, Any), data, depth)
    raise TypeError(f"cannot decode {tp!r}")


def _need(ok: bool, tp: Any, data: Any) -> Any:
    if not ok:
        raise WireError(f"{data!r} is not a {getattr(tp, '__name__', tp)}")
    return data


def _mapping(kv: tuple[Any, ...], data: Any, depth: int) -> dict[Any, Any]:
    key_tp, val_tp = kv
    if key_tp is str or key_tp is Any:
        _need(isinstance(data, dict), dict, data)
        _need(all(isinstance(k, str) for k in data), dict, data)
        return {k: _decode(val_tp, v, depth + 1) for k, v in data.items()}
    if isinstance(data, dict):  # str-valued keys such as enum values
        pairs: list[Any] = [[k, v] for k, v in data.items()]
    else:
        pairs = _need(isinstance(data, list), dict, data)
    out: dict[Any, Any] = {}
    for pair in pairs:
        _need(isinstance(pair, list) and len(pair) == 2, "key/value pair", pair)
        out[_decode(key_tp, pair[0], depth + 1)] = _decode(val_tp, pair[1], depth + 1)
    return out


def _record(tp: Any, data: Any, depth: int) -> Any:
    _need(isinstance(data, dict), tp, data)
    spec = {f.name: f for f in fields(tp) if f.init}
    unknown = set(data) - set(spec)
    if unknown:
        raise WireError(f"{tp.__name__} has no field {sorted(map(str, unknown))}")
    hints = typing.get_type_hints(tp)
    kwargs = {}
    for name, f in spec.items():
        if name in data:
            kwargs[name] = _decode(hints[name], data[name], depth + 1)
        elif f.default is MISSING and f.default_factory is MISSING:
            raise WireError(f"{tp.__name__} is missing {name}")
    try:
        return tp(**kwargs)
    except (ValueError, TypeError, ArithmeticError) as exc:
        raise WireError(f"{tp.__name__}: {exc}") from exc


def _json(data: Any, depth: int) -> Any:
    if depth > MAX_DEPTH:
        raise WireError("payload is nested too deeply")
    if isinstance(data, float) and not math.isfinite(data):
        raise WireError("float must be finite")
    if isinstance(data, list):
        for v in data:
            _json(v, depth + 1)
    elif isinstance(data, dict):
        for k, v in data.items():
            _need(isinstance(k, str), str, k)
            _json(v, depth + 1)
    elif data is not None and not isinstance(data, (str, int, float, bool)):
        raise WireError(f"{type(data).__name__} is not JSON")
    return data
