"""An in-process transport and host for tests: real ledger calls, no network."""

from __future__ import annotations

import json
import threading
from collections.abc import Callable, Iterable
from pathlib import Path
from typing import Any

import httpx

from pdm_memory.cloud_ledgers.client import CloudLedgers
from pdm_memory.cloud_ledgers.host import LedgerHost

__all__ = ["InProcessTransport", "in_process", "strip_time"]

_hosts: dict[bool, LedgerHost] = {}
_lock = threading.Lock()


class InProcessTransport:
    """Run requests on a host; each op in *drop_response_after* loses its first reply."""

    def __init__(
        self,
        host: LedgerHost,
        resolve: Callable[[str], str | Path],
        *,
        drop_response_after: Iterable[str] = (),
    ) -> None:
        self._host, self._resolve = host, resolve
        self._drop = set(drop_response_after)

    def send(
        self,
        namespace: str,
        kind: str,
        op: str,
        body: dict[str, Any],
        idempotency_key: str | None,
    ) -> tuple[int, dict[str, Any]]:
        status, reply = self._host.handle(
            self._resolve(namespace),
            kind,
            op,
            json.loads(json.dumps(body, allow_nan=False)),
            idempotency_key=idempotency_key,
        )
        if op in self._drop:
            self._drop.discard(op)
            raise httpx.ReadTimeout("response dropped")
        return status, json.loads(json.dumps(reply, allow_nan=False))


def in_process(
    target: str | Path,
    *,
    trust_client_clock: bool = True,
    drop_response_after: Iterable[str] = (),
) -> CloudLedgers:
    with _lock:
        host = _hosts.get(trust_client_clock)
        if host is None:
            host = _hosts[trust_client_clock] = LedgerHost(
                trust_client_clock=trust_client_clock
            )
    transport = InProcessTransport(
        host, lambda ns: target, drop_response_after=drop_response_after
    )
    return CloudLedgers(transport=transport)


def close_hosts() -> None:
    with _lock:
        for host in _hosts.values():
            host.close()
        _hosts.clear()


def strip_time(events: list[dict]) -> list[tuple[str, dict]]:
    """``(kind, payload without recorded_at)`` for comparing histories from different times."""
    return [
        (e["kind"], {k: v for k, v in e["payload"].items() if k != "recorded_at"})
        for e in events
    ]
