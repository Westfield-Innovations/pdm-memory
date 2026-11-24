"""The server half: runs the local ledgers for remote clients, one JSON call at a time.

``LedgerHost.handle`` takes a parsed request body and returns ``(status, body)``;
the web layer around it only routes, authenticates and resolves the target.
The protocol is stateless: every body carries the ``open`` arguments, so any
worker can serve any call. Owner callbacks never run here: an operation that
reaches one rolls back and answers ``needs`` with the owner's chain head as a
fence, and the client sends its verdicts back with that fence.
"""

from __future__ import annotations

import hashlib
import inspect
import json
import re
import sqlite3
import sys
import threading
import time
import typing
from collections import OrderedDict
from collections.abc import Callable, Mapping
from contextvars import ContextVar
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

from pdm_memory._ledger.db import Database
from pdm_memory._ledger.errors import ConfigurationError
from pdm_memory.cloud_ledgers.wire import (
    PROTOCOL,
    WireError,
    decode,
    encode,
    error_response,
)
from pdm_memory.consequence.ledger import ConsequenceLedger
from pdm_memory.goal_loop._json import utc_now
from pdm_memory.goal_loop.store import _SqlJobStore
from pdm_memory.guarded_effects import (
    _GE_MIGRATIONS,
    _TERMINAL,
    CONDITION,
    VALIDATOR,
    ConditionVerdict,
    EffectRequest,
    GuardedEffects,
    Opening,
    _check_openings,
    _json,
    _name,
    _openings_hash,
)

__all__ = ["OPERATIONS", "LedgerHost"]


def _ops(spec: str) -> dict[str, bool]:
    return {op.rstrip("*"): op.endswith("*") for op in spec.split()}


# kind -> op -> writes (a * marks a write, which needs an Idempotency-Key).
OPERATIONS: Mapping[str, Mapping[str, bool]] = {
    "guard": _ops(
        "open* start_branch* receipt stage* dispatch_count release* complete*"
        " cancel* reconcile* checkpoint* recover* history history_raw"
    ),
    "consequence": _ops(
        "open prepare* expectation prepared_check attach_action* actions observe*"
        " observations evaluate* current_evaluation forecast_record evaluation"
        " propose_learning* update_state record_update_receipt* record_consequence*"
        " accounts significance suggest_observation* revoke_expectation*"
        " revoke_update* revocations history history_raw"
    ),
    "jobs": _ops("open create* load commit* receipts contract_history due active"),
}

_KEY = re.compile(r"[A-Za-z0-9:_-]{8,128}\Z")
_EVENTS = {
    "guard": ("ge_events", "action_id"),
    "consequence": ("cl_events", "expectation_id"),
}
_BODY = {"protocol", "open", "args", "now"}
_LOCK_KEYS = {"guard": "pdm:ge", "consequence": "pdm:cl", "jobs": "pdm:gl"}
# Guard ops that local code fences with _assert_current_config.
_FENCED = {
    "start_branch",
    "stage",
    "release",
    "cancel",
    "reconcile",
    "checkpoint",
    "recover",
}
# Open fields that select no configuration, so they stay out of the cache key.
_PER_CALL = ("replace_config", "config_epoch")
_NOW: ContextVar[datetime] = ContextVar("pdm_ledger_now")
_PRUNE_EVERY = 64
_KEEP = timedelta(days=1)

_RQ_SCHEMA = """
CREATE TABLE IF NOT EXISTS pdm_requests (
  kind TEXT NOT NULL, key TEXT NOT NULL, body_hash TEXT NOT NULL,
  response_json TEXT NOT NULL, created_at TEXT NOT NULL, PRIMARY KEY (kind, key));
"""
# Append a step to change the schema; never edit a step that has shipped.
_RQ_MIGRATIONS = (lambda db: db.create_tables(_RQ_SCHEMA),)


class _NeedVerdict(Exception):
    def __init__(self, needs: list[str], fence: int, request: str) -> None:
        super().__init__("owner verdicts needed")
        self.body = {
            "protocol": PROTOCOL,
            "needs": needs,
            "fence": fence,
            "request": request,
        }


class _KeyReused(Exception):
    pass


class _StaleEpoch(Exception):
    """The client's epoch is old; the cached ledger itself is fine."""


def _superseded() -> ConfigurationError:
    return ConfigurationError(
        "controller configuration has been superseded by the owner"
    )


def _unavailable(exc: Exception) -> bool:
    """A lock wait that ran out or a dropped connection: worth a retry, not a bug."""
    if isinstance(exc, sqlite3.OperationalError):
        return str(exc).startswith(("database is locked", "database is busy"))
    psycopg = sys.modules.get("psycopg")
    return psycopg is not None and isinstance(exc, psycopg.OperationalError)


def _refuse(request: EffectRequest) -> Any:
    raise RuntimeError("owner callbacks run on the client, never on the ledger host")


@dataclass(frozen=True)
class _OpeningSpec:
    operation: str
    target: str
    max_dispatches: int | None = None
    has_check: bool = False


@dataclass
class _Entry:
    ledger: Any
    used: float
    closed: bool = False


def _canonical(value: Any) -> str:
    return json.dumps(
        value,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        allow_nan=False,
    )


def _sha(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def _stamp(moment: datetime) -> str:
    # Fixed-width UTC, so text order is time order on both backends.
    return moment.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.%f")


def _refusal(status: int, code: str, detail: str) -> tuple[int, dict[str, Any]]:
    return status, {"error_code": code, "detail": detail}


def _bind(
    fn: Callable[..., Any], args: Mapping[str, Any], skip: tuple[str, ...] = ()
) -> dict[str, Any]:
    """Decode *args* by *fn*'s annotations; unknown or missing names are a WireError."""
    params = {
        n: p
        for n, p in inspect.signature(fn).parameters.items()
        if n not in ("self", *skip)
    }
    unknown = sorted(set(args) - set(params))
    if unknown:
        raise WireError(f"unexpected arguments {unknown}")
    hints = typing.get_type_hints(fn)
    kwargs = {n: decode(hints[n], v) for n, v in args.items()}
    missing = [n for n, p in params.items() if p.default is p.empty and n not in kwargs]
    if missing:
        raise WireError(f"missing arguments {missing}")
    return kwargs


def _page(after_sequence: int = 0, limit: int = 1000) -> tuple[int, int]:
    if after_sequence < 0 or not 1 <= limit <= 1000:
        raise ValueError(
            "after_sequence must be nonnegative and limit between 1 and 1000"
        )
    return after_sequence, limit


def _lookup(db: Database, kind: str, key: str, body_hash: str) -> dict[str, Any] | None:
    row = db.execute(
        "SELECT body_hash, response_json FROM pdm_requests WHERE kind=? AND key=?",
        (kind, key),
    ).fetchone()
    if row is None:
        return None
    if row[0] != body_hash:
        raise _KeyReused
    return json.loads(row[1])


def _attach(
    target: str | Path,
    openings: list[Opening],
    kwargs: dict[str, Any],
    epoch: int,
) -> GuardedEffects:
    """Join a superseded client's ledger without the constructor's configuration check.

    The constructor already validated *kwargs* before refusing. A fenced op on this
    instance still fails ``_assert_current_config``: its labels or epoch are not current.
    """
    ledger = object.__new__(GuardedEffects)
    ledger.owner = kwargs["owner"]
    ledger.program_version = kwargs["program_version"]
    ledger.policy_version = kwargs["policy_version"]
    for name in (
        "max_request_bytes",
        "max_pending",
        "max_state_bytes",
        "max_result_bytes",
    ):
        setattr(ledger, name, kwargs.get(name, _DEFAULTS[name]))
    ledger._openings = _check_openings(openings)
    ledger._openings_hash = _openings_hash(ledger._openings)
    ledger._lock = threading.RLock()
    ledger._in_flight = set()
    ledger._config_epoch = epoch
    ledger._conn = Database(target, lock_key="pdm:ge")
    try:
        ledger._conn.migrate("ge", _GE_MIGRATIONS)  # a newer schema is still refused
    except BaseException:
        ledger._conn.close()
        raise
    return ledger


_DEFAULTS = {
    n: p.default
    for n, p in inspect.signature(GuardedEffects.__init__).parameters.items()
    if n.startswith("max_")
}


class LedgerHost:
    """Serve ledger calls on targets (SQLite files or Postgres URLs) for remote clients.

    Open ledgers are cached per (target, kind, open arguments), least recently
    used first out. Every write stores its response in ``pdm_requests`` in its
    own transaction, so a retry with the same Idempotency-Key gets the same
    answer instead of a second write.
    """

    def __init__(
        self,
        *,
        max_open: int = 8,
        idle_s: float = 300.0,
        trust_client_clock: bool = False,
        clock: Callable[[], datetime] = utc_now,
    ) -> None:
        if type(max_open) is not int or max_open < 1:
            raise ValueError("max_open must be a positive integer")
        if not idle_s > 0:
            raise ValueError("idle_s must be positive")
        self._max_open, self._idle_s = max_open, idle_s
        self._trust, self._clock = trust_client_clock, clock
        self._lock = threading.Lock()
        self._replacing = threading.Lock()
        self._cache: OrderedDict[tuple[str, str, str], _Entry] = OrderedDict()
        self._writes = 0

    def handle(
        self,
        target: str | Path,
        kind: str,
        op: str,
        body: Mapping[str, Any],
        *,
        idempotency_key: str | None,
    ) -> tuple[int, dict[str, Any]]:
        """Run one call. Ledger errors become their registered response; bugs propagate."""
        if op not in OPERATIONS.get(kind, {}):
            return _refusal(
                404, "UNKNOWN_OPERATION", f"no {kind}/{op} ledger operation"
            )
        protocol = body.get("protocol") if isinstance(body, Mapping) else None
        if type(protocol) is not int or protocol != PROTOCOL:
            return _refusal(
                400, "PROTOCOL_UNSUPPORTED", f"this host speaks protocol {PROTOCOL}"
            )
        key = idempotency_key if OPERATIONS[kind][op] else None
        if OPERATIONS[kind][op] and not (isinstance(key, str) and _KEY.match(key)):
            return _refusal(
                400,
                "IDEMPOTENCY_KEY_REQUIRED",
                "a write needs an Idempotency-Key header",
            )
        try:
            return self._handle(target, kind, op, body, key)
        except _NeedVerdict as need:
            return 200, need.body
        except _KeyReused:
            return _refusal(
                422,
                "IDEMPOTENCY_KEY_REUSED",
                "this Idempotency-Key was used for another request",
            )
        except Exception as exc:
            if _unavailable(exc):
                return _refusal(503, "LEDGERS_UNAVAILABLE", "ledger database busy")
            # An integer past what the database stores is bad input, not a bug.
            response = error_response(
                WireError(str(exc)) if isinstance(exc, OverflowError) else exc
            )
            if response is None:
                raise
            return response

    def _handle(
        self,
        target: str | Path,
        kind: str,
        op: str,
        body: Mapping[str, Any],
        key: str | None,
    ) -> tuple[int, dict[str, Any]]:
        unknown = sorted(set(body) - _BODY)
        if unknown:
            raise WireError(f"unexpected body fields {unknown}")
        open_ = decode(dict[str, Any], body.get("open", {}))
        args = decode(dict[str, Any], body.get("args", {}))
        now = decode(datetime | None, body.get("now"))
        body_hash = _sha(
            _canonical({"op": op, "open": open_, "args": args, "now": body.get("now")})
        )
        custom = kind == "consequence" and decode(
            bool, open_.get("custom_clock", False)
        )
        if not self._trust and (
            custom or (op == "suggest_observation" and args.get("now") is not None)
        ):
            raise ConfigurationError(
                "this ledger host keeps its own clock; it takes no client time"
            )
        token = _NOW.set(now if custom and now is not None else self._clock())
        try:
            replace = (
                kind == "guard" and op == "open" and open_.get("replace_config") is True
            )
            if replace and key is not None:
                # One at a time, so a retry racing its original finds its reply.
                with self._replacing:
                    stored = self._stored(target, kind, key, body_hash)
                    if stored is not None:
                        return 200, stored
                    return self._serve(
                        target, kind, op, open_, args, key, body_hash, True
                    )
            return self._serve(target, kind, op, open_, args, key, body_hash, False)
        finally:
            _NOW.reset(token)

    def _serve(
        self,
        target: str | Path,
        kind: str,
        op: str,
        open_: dict[str, Any],
        args: dict[str, Any],
        key: str | None,
        body_hash: str,
        replace: bool,
    ) -> tuple[int, dict[str, Any]]:
        # Complete and the reads work for a superseded client, as locally.
        attach = kind == "guard" and op not in _FENCED and op != "open"
        retry_stale = True
        while True:
            try:
                cache_key, entry, built = self._entry(
                    target, kind, open_, replace, attach
                )
            except ConfigurationError:
                # A write that landed before the configuration changed still replays.
                if key is not None and not replace:
                    stored = self._stored(target, kind, key, body_hash)
                    if stored is not None:
                        return 200, stored
                # A client holding an epoch was built under a configuration
                # that is no longer current: superseded, as locally.
                if "config_epoch" not in open_:
                    raise
                raise _superseded() from None
            replace = False
            with entry.ledger._lock:
                if entry.closed:
                    continue  # evicted between lookup and use
                try:
                    return self._run(
                        entry.ledger, kind, op, args, key, body_hash, open_
                    )
                except _StaleEpoch:
                    raise _superseded() from None
                except ConfigurationError:
                    self._drop(cache_key, entry)
                    # A cached instance may predate another worker's replacement;
                    # a fresh build still refuses a configuration that differs.
                    if built or not retry_stale:
                        raise
                    retry_stale = False

    def _run(
        self,
        ledger: Any,
        kind: str,
        op: str,
        args: dict[str, Any],
        key: str | None,
        body_hash: str,
        open_: dict[str, Any],
    ) -> tuple[int, dict[str, Any]]:
        if key is None:
            return 200, self._call(ledger, kind, op, args)
        conn: Database = ledger._conn
        # One transaction: key check, the operation, its stored response. Anything
        # raised (a verdict request included) leaves the with and rolls all back.
        with ledger._transaction():
            stored = _lookup(conn, kind, key, body_hash)
            if stored is not None:
                return 200, stored
            if kind == "guard" and op in _FENCED and "config_epoch" in open_:
                self._fence_epoch(ledger, decode(int, open_["config_epoch"]))
            text = _canonical(self._call(ledger, kind, op, args))
            moment = self._clock()
            conn.execute(
                "INSERT INTO pdm_requests VALUES (?, ?, ?, ?, ?)",
                (kind, key, body_hash, text, _stamp(moment)),
            )
            with self._lock:
                self._writes += 1
                prune = self._writes % _PRUNE_EVERY == 0
            if prune:
                conn.execute(
                    "DELETE FROM pdm_requests WHERE created_at<?",
                    (_stamp(moment - _KEEP),),
                )
        return 200, json.loads(text)

    @staticmethod
    def _fence_epoch(ledger: GuardedEffects, epoch: int) -> None:
        row = ledger._conn.execute(
            "SELECT epoch FROM ge_config WHERE owner=?", (ledger.owner,)
        ).fetchone()
        if row is None or row[0] != epoch:
            raise _StaleEpoch

    def _stored(
        self, target: str | Path, kind: str, key: str, body_hash: str
    ) -> dict[str, Any] | None:
        # A replacing open writes in the guard's constructor, and a build can
        # refuse, before any instance exists to join: look up on a connection of its own.
        db = Database(target, lock_key=_LOCK_KEYS[kind])
        try:
            db.migrate("rq", _RQ_MIGRATIONS)
            return _lookup(db, kind, key, body_hash)
        finally:
            db.close()

    def _call(
        self, ledger: Any, kind: str, op: str, args: dict[str, Any]
    ) -> dict[str, Any]:
        extra: dict[str, Any] = {}
        if op == "open" and kind == "guard":
            # A stale cached instance raises here, so its rebuild reports the epoch.
            ledger._assert_current_config()
            result = ledger._config_epoch
        elif op == "open":
            result = None
        elif op == "history_raw":
            result = self._history_raw(ledger, kind, args)
        elif kind == "guard" and op in ("stage", "release"):
            result, extra = self._two_step(ledger, op, args)
        elif kind == "guard" and op == "complete":
            result = self._complete(ledger, args)
        else:
            method = getattr(ledger, op)
            result = method(**_bind(method, args))
        return {"protocol": PROTOCOL, "result": encode(result), **extra}

    def _two_step(
        self, ledger: GuardedEffects, op: str, args: dict[str, Any]
    ) -> tuple[Any, dict[str, Any]]:
        args = dict(args)
        verdicts = {
            VALIDATOR: decode(ConditionVerdict | None, args.pop("validation", None)),
            CONDITION: decode(ConditionVerdict | None, args.pop("condition", None)),
        }
        fence = decode(int | None, args.pop("fence", None))

        def judge(callback: Any, encoded: str, label: str) -> ConditionVerdict:
            if callback is None:
                return ledger._judge(None, encoded, label)
            verdict = verdicts[label]
            head = ledger._conn.execute(
                "SELECT COALESCE(MAX(sequence), 0) FROM ge_events WHERE owner=?",
                (ledger.owner,),
            ).fetchone()[0]
            if verdict is None or fence != head:
                request = json.loads(encoded)
                opening = ledger._openings[(request["operation"], request["target"])]
                needs = ["validation"]
                if op == "release" and opening.check is not None:
                    needs.append("condition")
                raise _NeedVerdict(needs, head, encoded)
            # The local judge normalizes the client's verdict exactly as an in-process one.
            return ledger._judge(lambda _request: verdict, encoded, label)

        if op == "stage":
            return ledger._stage(_bind(ledger.stage, args)["request"], judge), {}
        action_id = _name(_bind(ledger.release, args)["action_id"], "action_id")
        receipt, dispatch = ledger._reserve(action_id, judge)
        extra = {} if dispatch is None else {"dispatch": {"request": dispatch.encoded}}
        return receipt, extra

    def _complete(self, ledger: GuardedEffects, args: dict[str, Any]) -> Any:
        args = dict(args)
        result = args.pop("result", None)
        kwargs = _bind(ledger._complete, args)
        action_id = _name(kwargs["action_id"], "action_id")
        failure = kwargs.get("failure")
        if failure is not None:
            name, sep, message = failure.partition(": ")
            failure = f"{name}: {message[:512]}" if sep else failure[:512]
        status = ledger._row(action_id)["status"]
        if status not in _TERMINAL and status not in ("DISPATCHING", "UNCERTAIN"):
            raise ValueError("only a dispatched action can be completed")
        if failure is None:
            try:
                _json(result, ledger.max_result_bytes)
            except Exception as exc:  # noqa: BLE001 - as release treats a bad return
                failure = f"{type(exc).__name__}: {str(exc)[:512]}"
        if failure is not None:
            return ledger._complete(action_id, failure=failure)
        return ledger._complete(action_id, result=result)

    def _history_raw(
        self, ledger: Any, kind: str, args: dict[str, Any]
    ) -> list[list[Any]]:
        table, subject = _EVENTS[kind]
        after, limit = _page(**_bind(_page, args))
        rows = ledger._conn.execute(
            f"SELECT sequence, owner, {subject}, kind, payload_json, previous_hash, event_hash"
            f" FROM {table} WHERE owner=? AND sequence>? ORDER BY sequence LIMIT ?",
            (ledger.owner, after, limit),
        ).fetchall()
        return [list(r) for r in rows]

    def _build(
        self,
        target: str | Path,
        kind: str,
        open_: dict[str, Any],
        replace: bool,
        attach: bool,
    ) -> Any:
        if kind == "guard":
            rest = {
                k: v for k, v in open_.items() if k not in ("openings", "config_epoch")
            }
            kwargs = _bind(GuardedEffects.__init__, rest, skip=("path", "openings"))
            kwargs["replace_config"] = replace
            specs = decode(list[_OpeningSpec], open_.get("openings", []))
            openings = [
                Opening(
                    s.operation,
                    s.target,
                    handler=_refuse,
                    validate=_refuse,
                    check=_refuse if s.has_check else None,
                    max_dispatches=s.max_dispatches,
                )
                for s in specs
            ]
            try:
                ledger: Any = GuardedEffects(target, openings=openings, **kwargs)
            except ConfigurationError:
                if not attach:
                    raise
                epoch = decode(int, open_.get("config_epoch", 0))
                ledger = _attach(target, openings, kwargs, epoch)
        elif kind == "consequence":
            rest = {k: v for k, v in open_.items() if k != "custom_clock"}
            kwargs = _bind(ConsequenceLedger.__init__, rest, skip=("path", "clock"))
            ledger = ConsequenceLedger(target, clock=_NOW.get, **kwargs)
        else:
            _bind(lambda: None, open_)
            ledger = _SqlJobStore(target)
        try:
            ledger._conn.migrate("rq", _RQ_MIGRATIONS)
        except BaseException:
            ledger.close()
            raise
        return ledger

    def _entry(
        self,
        target: str | Path,
        kind: str,
        open_: dict[str, Any],
        replace: bool,
        attach: bool,
    ) -> tuple[tuple[str, str, str], _Entry, bool]:
        same = {k: v for k, v in open_.items() if k not in _PER_CALL}
        cache_key = (str(target), kind, _sha(_canonical(same)))
        now = time.monotonic()
        with self._lock:
            idle = [k for k, e in self._cache.items() if now - e.used > self._idle_s]
            dropped = [self._cache.pop(k) for k in idle]
            entry = None if replace else self._cache.get(cache_key)
            if entry is not None:
                self._cache.move_to_end(cache_key)
                entry.used = now
        self._close(dropped)
        if entry is not None:
            return cache_key, entry, False
        built = _Entry(self._build(target, kind, open_, replace, attach), now)
        with self._lock:
            entry = self._cache.pop(cache_key, None)
            if entry is not None and not replace:
                dropped = [built]  # another call built it meanwhile; keep theirs
            else:
                dropped = [] if entry is None else [entry]
                entry = built
            self._cache[cache_key] = entry
            while len(self._cache) > self._max_open:
                dropped.append(self._cache.popitem(last=False)[1])
        self._close(dropped)
        return cache_key, entry, entry is built

    def _drop(self, cache_key: tuple[str, str, str], entry: _Entry) -> None:
        with self._lock:
            if self._cache.get(cache_key) is entry:
                del self._cache[cache_key]
        self._close([entry])

    @staticmethod
    def _close(entries: list[_Entry]) -> None:
        for entry in entries:
            with entry.ledger._lock:  # waits for a call still running on it
                if not entry.closed:
                    entry.closed = True
                    entry.ledger.close()

    def evict(self, target: str | Path | None = None) -> None:
        """Close cached ledgers of *target*, or all of them."""
        with self._lock:
            keys = [k for k in self._cache if target is None or k[0] == str(target)]
            dropped = [self._cache.pop(k) for k in keys]
        self._close(dropped)

    def close(self) -> None:
        self.evict()
