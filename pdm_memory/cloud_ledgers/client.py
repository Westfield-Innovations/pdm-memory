"""The client half: one protocol call per ledger operation, over HTTP."""

from __future__ import annotations

import re
import time
import uuid
from datetime import datetime
from typing import Any, Protocol

import httpx

from pdm_memory.auth.jwt_handler import JWTAuth
from pdm_memory.cloud_ledgers.host import OPERATIONS
from pdm_memory.cloud_ledgers.wire import PROTOCOL, encode, raise_error
from pdm_memory.storage.cloud_driver import DEFAULT_CLOUD_URL, CloudDriver
from pdm_memory.storage.errors import CloudConflictError, CloudStorageError

__all__ = ["CloudLedgers", "HttpTransport", "Transport"]

_NAMESPACE = re.compile(r"[A-Za-z0-9._-]{1,64}\Z")
_RETRY_STATUS = (502, 503, 504)
_sleep = time.sleep
_monotonic = time.monotonic


class Transport(Protocol):
    def send(
        self,
        namespace: str,
        kind: str,
        op: str,
        body: dict[str, Any],
        idempotency_key: str | None,
    ) -> tuple[int, dict[str, Any]]:
        """Deliver one call; a connection failure raises ``httpx.TransportError``."""
        ...


class HttpTransport:
    def __init__(
        self, auth: JWTAuth, base_url: str = DEFAULT_CLOUD_URL, *, timeout: float = 15.0
    ) -> None:
        self._auth = auth
        self._base_url = base_url.rstrip("/")
        self._client = httpx.Client(timeout=timeout)

    def send(
        self,
        namespace: str,
        kind: str,
        op: str,
        body: dict[str, Any],
        idempotency_key: str | None,
    ) -> tuple[int, dict[str, Any]]:
        url = f"{self._base_url}/api/v1/pdm/ledgers/{namespace}/{kind}/{op}/"
        extra = {"X-PDM-Ledger-Protocol": str(PROTOCOL)}
        if idempotency_key is not None:
            extra["Idempotency-Key"] = idempotency_key
        self._auth.ensure_fresh()
        resp = self._post(url, body, extra)
        if resp.status_code == 401 and self._auth.refresh():
            resp = self._post(url, body, extra)
        try:
            parsed = resp.json()
        except ValueError:
            parsed = {}
        return resp.status_code, parsed if isinstance(parsed, dict) else {}

    def close(self) -> None:
        self._client.close()

    def _post(self, url: str, body: dict[str, Any], extra: dict[str, str]):
        return self._client.post(
            url, json=body, headers={**self._auth.headers(), **extra}
        )


class CloudLedgers:
    """A connection to the ledger host of one namespace; ledgers call through it."""

    def __init__(
        self,
        auth: JWTAuth | None = None,
        base_url: str = DEFAULT_CLOUD_URL,
        *,
        namespace: str = "default",
        timeout: float = 15.0,
        retry_deadline_s: float = 30.0,
        transport: Transport | None = None,
    ) -> None:
        if not _NAMESPACE.match(namespace):
            raise ValueError("namespace must match [A-Za-z0-9._-]{1,64}")
        self._owned: HttpTransport | None = None
        if transport is None:
            if auth is None:
                raise ValueError("CloudLedgers needs an auth or a transport")
            transport = self._owned = HttpTransport(auth, base_url, timeout=timeout)
        self.namespace = namespace
        self.retry_deadline_s = retry_deadline_s
        self._transport = transport

    def close(self) -> None:
        """Close the transport this object built; a supplied one is the caller's."""
        if self._owned is not None:
            self._owned.close()

    @classmethod
    def from_driver(
        cls, driver: CloudDriver, *, namespace: str = "default"
    ) -> CloudLedgers:
        return cls(
            driver._auth,
            driver._base_url,
            namespace=namespace,
            timeout=driver._timeout,
        )

    def call(
        self,
        kind: str,
        op: str,
        open: dict[str, Any],
        args: dict[str, Any],
        *,
        now: datetime | None = None,
    ) -> dict[str, Any]:
        """Send one logical call, retrying transient failures under one key."""
        key = uuid.uuid4().hex if OPERATIONS.get(kind, {}).get(op) else None
        body = {
            "protocol": PROTOCOL,
            "open": open,
            "args": args,
            "now": None if now is None else encode(now),
        }
        deadline = _monotonic() + self.retry_deadline_s
        attempt = 0
        while True:
            try:
                status, reply = self._transport.send(
                    self.namespace, kind, op, body, key
                )
                cause: Exception | None = None
            except httpx.TransportError as exc:
                status, reply, cause = 0, {}, exc
            if cause is None and status not in _RETRY_STATUS:
                return self._result(status, reply, kind, op)
            delay = min(0.1 * 2**attempt, 2.0)
            if _monotonic() + delay > deadline:
                raise CloudStorageError(
                    f"ledger call {kind}/{op} failed after retries"
                    + ("" if cause else f" (HTTP {status})"),
                    status_code=status or None,
                ) from cause
            _sleep(delay)
            attempt += 1

    @staticmethod
    def _result(
        status: int, reply: dict[str, Any], kind: str, op: str
    ) -> dict[str, Any]:
        if status >= 400:
            if reply.get("error_code") == "LEDGER_ERROR":
                raise_error(reply)
            code = reply.get("error_code")
            detail = reply.get("detail")
            message = f"ledger call {kind}/{op} refused: {code or status}" + (
                f": {detail}" if detail else ""
            )
            if status == 409:
                raise CloudConflictError(message, error_code=code, detail=detail)
            raise CloudStorageError(message, status_code=status)
        if reply.get("protocol") != PROTOCOL:
            raise CloudStorageError(
                f"ledger host answered protocol {reply.get('protocol')!r}"
            )
        return reply
