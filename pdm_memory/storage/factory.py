# © 2026 Westfield Innovations LLC. Patent Pending.
# U.S. App. No. 19/739,419 | 63/953,563 | 63/953,842
# MODIFICATION PROHIBITED. USE AS SHIPPED.

"""
Storage factory — resolve ``store`` URLs / paths into concrete drivers.

Supported ``store`` values:

    ./local.db                          SQLite file path (legacy)
    sqlite:///./local.db                SQLite URL
    postgresql://user:pass@host/db        PostgreSQL (requires ``pdm-memory[postgres]``)
    cloud                               AZUS Companion API (requires ``token``)

Custom backends::

    from pdm_memory.storage import register_storage, create_storage

    register_storage("redis", lambda url, **kw: RedisDriver.from_url(url, **kw))
    mem = Memory(store="redis://localhost:6379/0")
"""

from __future__ import annotations

import logging
from collections.abc import Callable
from typing import Any
from urllib.parse import unquote, urlparse

from pdm_memory.storage.base import BaseStorage

logger = logging.getLogger(__name__)

StorageBuilder = Callable[..., BaseStorage]

_BUILTIN_SCHEMES: dict[str, StorageBuilder] = {}
_CUSTOM_SCHEMES: dict[str, StorageBuilder] = {}


def companion_token_refresh_url(cloud_url: str) -> str:
    """Companion API JWT refresh endpoint derived from ``cloud_url`` base."""
    return f"{cloud_url.rstrip('/')}/api/v1/accounts/token/refresh/"


def register_storage(scheme: str, builder: StorageBuilder) -> None:
    """
    Register a custom storage backend for a URL scheme.

    Args:
        scheme:  URL scheme without ``://`` (e.g. ``redis``, ``dynamodb``).
        builder: Callable ``(store_url, **options) -> BaseStorage``.
    """
    key = scheme.lower().strip()
    if not key:
        raise ValueError("scheme cannot be empty")
    _CUSTOM_SCHEMES[key] = builder
    logger.debug("[PDM] Registered storage scheme %r", key)


def _build_sqlite(store: str, *, store_raw: bool, **_: Any) -> BaseStorage:
    from pdm_memory.storage.sqlite_driver import SQLiteDriver

    if "://" in store:
        db_path = _sqlite_path_from_url(store)
    else:
        db_path = store
    return SQLiteDriver(db_path=db_path, store_raw=store_raw)


def _build_postgres(store: str, *, store_raw: bool, **_: Any) -> BaseStorage:
    try:
        from pdm_memory.storage.postgres_driver import PostgresDriver
    except ImportError as exc:
        raise ImportError(
            "PostgreSQL storage requires psycopg. "
            'Install with: pip install "pdm-memory[postgres]"'
        ) from exc
    return PostgresDriver(dsn=store, store_raw=store_raw)


def _build_cloud(
    store: str,
    *,
    user: str,
    token: str | None,
    refresh_token: str | None,
    cloud_url: str,
    **_: Any,
) -> BaseStorage:
    if not token:
        raise ValueError(
            "Cloud mode requires a JWT token. Pass token='eyJ...' to Memory()."
        )
    from pdm_memory.auth.jwt_handler import JWTAuth
    from pdm_memory.storage.cloud_driver import CloudDriver

    auth = JWTAuth(
        token=token,
        refresh_token=refresh_token,
        refresh_url=companion_token_refresh_url(cloud_url),
    )
    return CloudDriver(auth=auth, base_url=cloud_url, user=user)


def _sqlite_path_from_url(store: str) -> str:
    parsed = urlparse(store)
    if parsed.scheme.lower() not in {"sqlite", "file"}:
        raise ValueError(f"Not a sqlite URL: {store}")
    if parsed.path in ("", "/"):
        raise ValueError(f"sqlite URL missing database path: {store}")

    path = unquote(parsed.path)
    # sqlite:///relative.db → strip one leading slash for relative paths
    if path.startswith("/") and not path.startswith("//"):
        relative = path[1:]
        if relative and not relative.startswith("/"):
            return relative
    return path


def _resolve_scheme(store: str) -> tuple[str, str]:
    """Return (scheme, normalized_store_url_or_path)."""
    trimmed = store.strip()
    if trimmed == "cloud":
        return "cloud", trimmed
    if "://" not in trimmed:
        return "sqlite", trimmed

    parsed = urlparse(trimmed)
    scheme = parsed.scheme.lower()
    if scheme in {"sqlite", "file"}:
        return "sqlite", trimmed
    if scheme in {"postgresql", "postgres"}:
        return "postgresql", trimmed
    if scheme == "cloud":
        return "cloud", trimmed
    return scheme, trimmed


def create_storage(
    store: str,
    *,
    user: str = "default",
    token: str | None = None,
    refresh_token: str | None = None,
    cloud_url: str = "https://api.azus.ai",
    store_raw: bool = True,
) -> BaseStorage:
    """
    Build a storage backend from a path, DSN, or URL.

    Raises:
        ValueError: Unknown scheme or missing cloud token.
        ImportError: Optional driver dependency not installed.
    """
    scheme, normalized = _resolve_scheme(store)
    builders = {**_BUILTIN_SCHEMES, **_CUSTOM_SCHEMES}

    if scheme not in builders:
        known = sorted({*_BUILTIN_SCHEMES, *_CUSTOM_SCHEMES})
        raise ValueError(
            f"Unsupported storage scheme {scheme!r} in {store!r}. "
            f"Known schemes: {', '.join(known)}. "
            "Implement BaseStorage and register_storage() for custom backends."
        )

    driver = builders[scheme](
        normalized,
        user=user,
        token=token,
        refresh_token=refresh_token,
        cloud_url=cloud_url,
        store_raw=store_raw,
    )
    logger.debug("[PDM] Storage driver %s for store=%s", type(driver).__name__, store)
    return driver


# Register built-ins at import time
_BUILTIN_SCHEMES.update(
    {
        "sqlite": _build_sqlite,
        "file": _build_sqlite,
        "postgresql": _build_postgres,
        "postgres": _build_postgres,
        "cloud": _build_cloud,
    }
)
