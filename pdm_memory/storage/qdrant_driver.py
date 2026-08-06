# © 2026 Westfield Innovations LLC. Patent Pending.
# U.S. App. No. 19/739,419 | 63/953,563 | 63/953,842
# MODIFICATION PROHIBITED. USE AS SHIPPED.

"""
Qdrant Storage Driver — payload document backend for PDM signatures.

Qdrant is used as a user-scoped document store (not ANN recall).
Retrieval still runs in-process via :class:`~pdm_memory.core.retrieval.RetrievalEngine`
after :meth:`list` candidates.

Requires optional extra::

    pip install "pdm-memory[qdrant]"

URLs::

    qdrant://localhost:6333/pdm_signatures
    qdrants://host:6334/pdm_signatures?api_key=SECRET
    qdrant://memory/pdm_signatures          # in-process (:memory:) for tests

Usage::

    mem = Memory(store="qdrant://localhost:6333/pdm_signatures")
"""

from __future__ import annotations

import builtins
import warnings
import json
import logging
import uuid
from collections.abc import Mapping, Sequence
from datetime import datetime, timezone
from typing import Any
from urllib.parse import parse_qs, unquote, urlparse

from pdm_memory.core.signature import DrawerInfo, SignatureRecord
from pdm_memory.storage.base import BaseStorage, SaveBatchResult, UpdateBatchResult
from pdm_memory.storage.schema import (
    encode_compressed_fact,
    parse_dt,
    prepare_update_fields,
)

logger = logging.getLogger(__name__)

try:
    from qdrant_client import QdrantClient, models
    from qdrant_client.http.exceptions import (
        ResponseHandlingException,
        UnexpectedResponse,
    )
except ImportError as _exc:  # pragma: no cover - exercised via factory
    raise ImportError(
        "Qdrant storage requires qdrant-client. "
        'Install with: pip install "pdm-memory[qdrant]"'
    ) from _exc

try:
    from tenacity import (
        retry,
        retry_if_exception,
        stop_after_attempt,
        wait_exponential_jitter,
    )
except ImportError as _exc:  # pragma: no cover
    raise ImportError(
        "Qdrant storage requires tenacity. "
        'Install with: pip install "pdm-memory[qdrant]"'
    ) from _exc

#: Dummy dense vector — Qdrant requires vectors; PDM does not use ANN here.
_VECTOR = [0.0]
_VECTOR_SIZE = 1
_SCROLL_PAGE = 256
_CHUNK = 256
_RETRY_ATTEMPTS = 3

#: Fields returned by :meth:`QdrantDriver.list` (excludes internal / filter-only keys).
_LIST_PAYLOAD_FIELDS: tuple[str, ...] = (
    "id",
    "user",
    "compressed_fact",
    "source",
    "p_magnitude",
    "t_persistence",
    "phase_privilege",
    "effective_spike",
    "intent_tags",
    "question_regime",
    "domain",
    "drawer_domain",
    "retrieval_count",
    "last_retrieved",
    "created_at",
    "validation_prediction_total",
    "validation_prediction_correct",
    "decay_rate",
    "t_deadline",
    "t_event_at",
    "urgency_rate",
    "metadata",
    "is_deleted",
    "idempotency_key",
)

_DRAWER_PAYLOAD_FIELDS: tuple[str, ...] = (
    "domain",
    "description",
    "signature_count",
    "pressure_sum",
    "user",
    "kind",
)


def _normalize_tags_lc(tags: Sequence[Any] | None) -> list[str]:
    """Lowercased tag tokens for server-side ``MatchAny`` filters."""
    out: list[str] = []
    for raw in tags or ():
        token = str(raw).strip().lower()
        if token:
            out.append(token)
    return out


def _is_retryable_qdrant_error(exc: BaseException) -> bool:
    """Network blips + 5xx/429 — not validation / 4xx client errors."""
    if isinstance(exc, (TimeoutError, ConnectionError, OSError)):
        return True
    if isinstance(exc, ResponseHandlingException):
        return True
    if isinstance(exc, UnexpectedResponse):
        status = getattr(exc, "status_code", None)
        try:
            code = int(status) if status is not None else 503
        except (TypeError, ValueError):
            code = 503
        return code >= 500 or code == 429
    return False


def _dt_iso(value: datetime | None) -> str | None:
    if value is None:
        return None
    return value.isoformat()


def _point_id(memory_id: str) -> str:
    """
    Qdrant point ids must be UUID or unsigned int.

    Valid UUID strings are used as-is; anything else maps to deterministic UUIDv5
    while the original id remains in payload[\"id\"].
    """
    raw = (memory_id or "").strip()
    if not raw:
        raise ValueError("memory id is required")
    try:
        return str(uuid.UUID(raw))
    except ValueError:
        return str(uuid.uuid5(uuid.NAMESPACE_URL, f"pdm-memory:{raw}"))


def _drawer_point_id(user: str, domain: str) -> str:
    return str(uuid.uuid5(uuid.NAMESPACE_URL, f"pdm-drawer:{user}:{domain}"))


def _idempotency_point_id(user: str, key: str) -> str:
    """Secondary point claiming ``(user, idempotency_key)`` uniqueness."""
    return str(
        uuid.uuid5(uuid.NAMESPACE_URL, f"pdm-idem:{user}:{key.strip()}")
    )


def record_to_payload(sig: SignatureRecord, *, store_raw: bool) -> dict[str, Any]:
    text = sig.compressed_fact or ""
    stored_text, text_hash = encode_compressed_fact(text, store_raw=store_raw)
    created = sig.created_at or datetime.now(tz=timezone.utc)
    tags = list(sig.intent_tags or [])
    return {
        "id": sig.id,
        "user": sig.user,
        "compressed_fact": stored_text,
        "compressed_fact_hash": text_hash,
        "source": sig.source,
        "p_magnitude": float(sig.p_magnitude),
        "t_persistence": float(sig.t_persistence),
        "phase_privilege": float(sig.phase_privilege),
        "effective_spike": sig.effective_spike,
        "intent_tags": tags,
        "intent_tags_lc": _normalize_tags_lc(tags),
        "question_regime": sig.question_regime,
        "domain": sig.domain,
        "drawer_domain": sig.drawer_domain,
        "retrieval_count": int(sig.retrieval_count),
        "last_retrieved": _dt_iso(sig.last_retrieved),
        "created_at": created.isoformat(),
        "validation_prediction_total": int(sig.validation_prediction_total),
        "validation_prediction_correct": int(sig.validation_prediction_correct),
        "decay_rate": float(sig.decay_rate),
        "t_deadline": _dt_iso(sig.t_deadline),
        "t_event_at": _dt_iso(sig.t_event_at),
        "urgency_rate": float(sig.urgency_rate),
        "metadata": dict(sig.metadata or {}),
        "is_deleted": 1 if sig.is_deleted else 0,
        "idempotency_key": sig.idempotency_key,
        "kind": "signature",
    }


def payload_to_record(payload: dict[str, Any]) -> SignatureRecord:
    tags = payload.get("intent_tags") or []
    if isinstance(tags, str):
        tags = json.loads(tags)
    meta = payload.get("metadata") or {}
    if isinstance(meta, str):
        meta = json.loads(meta)
    return SignatureRecord(
        id=str(payload.get("id") or ""),
        user=str(payload.get("user") or "default"),
        compressed_fact=str(payload.get("compressed_fact") or ""),
        source=str(payload.get("source") or "chat"),
        p_magnitude=float(payload.get("p_magnitude", 50.0)),
        t_persistence=float(payload.get("t_persistence", 30.0)),
        phase_privilege=float(payload.get("phase_privilege", 1.0)),
        effective_spike=payload.get("effective_spike"),
        intent_tags=list(tags),
        question_regime=str(payload.get("question_regime") or "neutral"),
        domain=str(payload.get("domain") or "insight"),
        drawer_domain=str(payload.get("drawer_domain") or "general"),
        retrieval_count=int(payload.get("retrieval_count") or 0),
        last_retrieved=parse_dt(payload.get("last_retrieved")),
        created_at=parse_dt(payload.get("created_at")),
        validation_prediction_total=int(
            payload.get("validation_prediction_total") or 0
        ),
        validation_prediction_correct=int(
            payload.get("validation_prediction_correct") or 0
        ),
        decay_rate=float(payload.get("decay_rate", 0.9)),
        t_deadline=parse_dt(payload.get("t_deadline")),
        t_event_at=parse_dt(payload.get("t_event_at")),
        urgency_rate=float(payload.get("urgency_rate", 2.0)),
        metadata=dict(meta),
        is_deleted=bool(payload.get("is_deleted", 0)),
        idempotency_key=payload.get("idempotency_key"),
    )


class QdrantDriver(BaseStorage):
    """
    Qdrant-backed PDM storage (sync ``qdrant-client``).

    Args:
        url:            HTTP(S) base URL (``http://localhost:6333``). Ignored when
                        ``client`` is injected.
        collection:     Signatures collection name.
        api_key:        Optional Qdrant API key.
        store_raw:      If False, store ``[HASH:…]`` instead of raw fact text.
        client:         Pre-built :class:`QdrantClient` (tests / advanced wiring).
        prefer_grpc:    Prefer gRPC (needs port 6334). Default False — HTTP on
                        6333 always works with stock Docker. Opt in via URL
                        ``?prefer_grpc=true``.
        timeout:        Client timeout seconds.
    """

    def __init__(
        self,
        url: str = "http://localhost:6333",
        *,
        collection: str = "pdm_signatures",
        api_key: str | None = None,
        store_raw: bool = True,
        client: QdrantClient | None = None,
        prefer_grpc: bool = False,
        timeout: float | None = 30.0,
    ) -> None:
        if not collection or not str(collection).strip():
            raise ValueError("collection name is required")
        self.collection = str(collection).strip()
        self.drawers_collection = f"{self.collection}__drawers"
        self.store_raw = store_raw
        self._url = url
        self._prefer_grpc = prefer_grpc
        if client is not None:
            self._client = client
        else:
            kwargs: dict[str, Any] = {
                "url": url,
                "prefer_grpc": prefer_grpc,
            }
            if api_key:
                kwargs["api_key"] = api_key
            if timeout is not None:
                kwargs["timeout"] = timeout
            self._client = QdrantClient(**kwargs)
        self._ensure_collections()
        logger.debug(
            "[PDM-Qdrant] Ready collection=%s store_raw=%s url=%s grpc=%s",
            self.collection,
            store_raw,
            url,
            prefer_grpc,
        )

    def _rpc(self, method: str, /, *args: Any, **kwargs: Any) -> Any:
        """Call ``QdrantClient`` with exponential backoff + jitter on blips."""
        fn = getattr(self._client, method)

        @retry(
            reraise=True,
            stop=stop_after_attempt(_RETRY_ATTEMPTS),
            wait=wait_exponential_jitter(initial=0.05, max=1.5),
            retry=retry_if_exception(_is_retryable_qdrant_error),
        )
        def _run() -> Any:
            return fn(*args, **kwargs)

        return _run()

    # ------------------------------------------------------------------
    # Factory helpers
    # ------------------------------------------------------------------

    @classmethod
    def from_url(cls, store: str, *, store_raw: bool = True) -> QdrantDriver:
        """
        Parse ``qdrant://…`` / ``qdrants://…`` / ``qdrant+memory://…`` URLs.

        Examples:
            ``qdrant://localhost:6333/pdm_signatures``
            ``qdrants://cloud.example:6334/pdm?api_key=secret``
            ``qdrant://memory/pdm_signatures``
        """
        trimmed = store.strip()
        parsed = urlparse(trimmed)
        scheme = parsed.scheme.lower()
        qs = {k.lower(): v for k, v in parse_qs(parsed.query).items()}

        def _q(name: str, default: str | None = None) -> str | None:
            vals = qs.get(name)
            if not vals:
                return default
            return unquote(vals[0])

        api_key = _q("api_key") or _q("api-key")
        prefer_raw = _q("prefer_grpc")
        if prefer_raw is None:
            prefer_grpc = False  # HTTP/6333 works out of the box
        else:
            prefer_grpc = prefer_raw.lower() in {"1", "true", "yes", "on"}
        timeout_raw = _q("timeout")
        timeout = float(timeout_raw) if timeout_raw else 30.0

        path_parts = [p for p in parsed.path.split("/") if p]

        # In-memory client (local mode) — tests / zero-infra.
        if scheme in {"qdrant+memory", "qdrant-memory"} or (
            scheme.startswith("qdrant") and (parsed.hostname or "").lower() == "memory"
        ):
            collection = path_parts[0] if path_parts else (_q("collection") or "pdm_signatures")
            return cls(
                url=":memory:",
                collection=collection,
                store_raw=store_raw,
                client=QdrantClient(":memory:"),
            )

        if scheme not in {"qdrant", "qdrants", "qdrant+http", "qdrant+https"}:
            raise ValueError(
                f"Unsupported qdrant URL scheme {scheme!r}. "
                "Use qdrant://host:6333/collection or qdrants://host:6334/collection"
            )

        collection = path_parts[0] if path_parts else _q("collection")
        if not collection:
            raise ValueError(
                "qdrant URL must include a collection path: "
                "qdrant://localhost:6333/pdm_signatures"
            )

        use_https = scheme in {"qdrants", "qdrant+https"}
        host = parsed.hostname or "localhost"
        default_port = 6334 if use_https else 6333
        port = parsed.port or default_port
        http_url = f"{'https' if use_https else 'http'}://{host}:{port}"
        return cls(
            url=http_url,
            collection=collection,
            api_key=api_key,
            store_raw=store_raw,
            prefer_grpc=prefer_grpc,
            timeout=timeout,
        )

    # ------------------------------------------------------------------
    # Collection bootstrap
    # ------------------------------------------------------------------

    def _ensure_collections(self) -> None:
        self._ensure_collection(self.collection, indexed=True)
        self._ensure_collection(self.drawers_collection, indexed=False)

    def _ensure_collection(self, name: str, *, indexed: bool) -> None:
        if self._rpc("collection_exists", name):
            if indexed:
                self._ensure_payload_indexes(name)
            return
        self._rpc(
            "create_collection",
            collection_name=name,
            vectors_config=models.VectorParams(
                size=_VECTOR_SIZE,
                distance=models.Distance.COSINE,
            ),
        )
        if indexed:
            self._ensure_payload_indexes(name)

    def _ensure_payload_indexes(self, name: str) -> None:
        # Local (:memory:) warns that indexes have no effect — silence noise.
        specs: list[tuple[str, models.PayloadSchemaType]] = [
            ("user", models.PayloadSchemaType.KEYWORD),
            ("id", models.PayloadSchemaType.KEYWORD),
            ("is_deleted", models.PayloadSchemaType.INTEGER),
            ("p_magnitude", models.PayloadSchemaType.FLOAT),
            ("compressed_fact_hash", models.PayloadSchemaType.KEYWORD),
            ("idempotency_key", models.PayloadSchemaType.KEYWORD),
            ("drawer_domain", models.PayloadSchemaType.KEYWORD),
            ("kind", models.PayloadSchemaType.KEYWORD),
            ("intent_tags", models.PayloadSchemaType.KEYWORD),
            ("intent_tags_lc", models.PayloadSchemaType.KEYWORD),
        ]
        with warnings.catch_warnings():
            warnings.simplefilter("ignore", UserWarning)
            for field_name, schema in specs:
                try:
                    self._rpc(
                        "create_payload_index",
                        collection_name=name,
                        field_name=field_name,
                        field_schema=schema,
                    )
                except Exception as exc:
                    logger.debug(
                        "[PDM-Qdrant] payload index %s.%s skipped: %s",
                        name,
                        field_name,
                        exc,
                    )

    # ------------------------------------------------------------------
    # Internals
    # ------------------------------------------------------------------

    def _upsert_points(
        self,
        points: Sequence[models.PointStruct],
        *,
        collection: str | None = None,
    ) -> None:
        if not points:
            return
        self._rpc(
            "upsert",
            collection_name=collection or self.collection,
            points=list(points),
            wait=True,
        )


    def _retrieve_payloads(
        self,
        memory_ids: Sequence[str],
        *,
        user: str | None = None,
        include_deleted: bool = True,
    ) -> dict[str, dict[str, Any]]:
        """One retrieve RTT for many ids → payload keyed by domain ``id``."""
        unique = list(dict.fromkeys(i for i in memory_ids if i))
        if not unique:
            return {}
        out: dict[str, dict[str, Any]] = {}
        for start in range(0, len(unique), _CHUNK):
            chunk = unique[start : start + _CHUNK]
            points = self._rpc(
                "retrieve",
                collection_name=self.collection,
                ids=[_point_id(i) for i in chunk],
                with_payload=True,
                with_vectors=False,
            )
            for point in points:
                payload = dict(point.payload or {})
                if not payload:
                    continue
                kind = payload.get("kind")
                if kind is not None and kind != "signature":
                    continue
                domain_id = str(payload.get("id") or "")
                if not domain_id:
                    continue
                if user is not None and payload.get("user") != user:
                    continue
                if not include_deleted and bool(payload.get("is_deleted", 0)):
                    continue
                out[domain_id] = payload
        return out

    def _get_point_payload(
        self,
        memory_id: str,
        *,
        user: str | None = None,
        include_deleted: bool = True,
    ) -> dict[str, Any] | None:
        found = self._retrieve_payloads(
            [memory_id], user=user, include_deleted=include_deleted
        )
        # UUID lookups must match payload id exactly.
        try:
            uuid.UUID(memory_id)
        except ValueError:
            # Non-UUID domain ids map via uuid5 point id; accept payload.id.
            if found:
                return next(iter(found.values()))
            return None
        return found.get(memory_id)

    def _must_filter(
        self,
        *,
        user: str,
        include_deleted: bool,
        min_pressure: float | None = None,
        drawer: str | None = None,
        extra: Sequence[models.Condition | models.Filter] | None = None,
    ) -> models.Filter:
        must: list[models.Condition | models.Filter] = [
            models.FieldCondition(key="user", match=models.MatchValue(value=user)),
            models.FieldCondition(
                key="kind", match=models.MatchValue(value="signature")
            ),
        ]
        if not include_deleted:
            must.append(
                models.FieldCondition(
                    key="is_deleted", match=models.MatchValue(value=0)
                )
            )
        if min_pressure is not None and min_pressure > 0:
            must.append(
                models.FieldCondition(
                    key="p_magnitude",
                    range=models.Range(gte=float(min_pressure)),
                )
            )
        if drawer:
            must.append(
                models.FieldCondition(
                    key="drawer_domain", match=models.MatchValue(value=drawer)
                )
            )
        if extra:
            must.extend(list(extra))
        return models.Filter(must=must)

    def _adjust_drawers(
        self,
        deltas: Mapping[tuple[str, str], tuple[int, float]],
    ) -> None:
        """
        Apply ``(delta_count, delta_pressure)`` to drawer aggregate points.

        Counters are best-effort under concurrency (Qdrant has no row locks).
        """
        if not deltas:
            return
        keys = list(deltas.keys())
        ids = [_drawer_point_id(u, d) for u, d in keys]
        existing_points = self._rpc(
            "retrieve",
            collection_name=self.drawers_collection,
            ids=ids,
            with_payload=True,
            with_vectors=False,
        )
        by_id = {str(p.id): p for p in existing_points}
        out: list[models.PointStruct] = []
        for (user, domain), (delta_count, delta_pressure) in deltas.items():
            pid = _drawer_point_id(user, domain)
            point = by_id.get(pid)
            if point is not None and point.payload:
                payload = dict(point.payload)
                count = max(0, int(payload.get("signature_count") or 0) + delta_count)
                psum = float(payload.get("pressure_sum") or 0.0) + float(delta_pressure)
            else:
                payload = {
                    "user": user,
                    "domain": domain,
                    "description": "",
                    "kind": "drawer",
                }
                count = max(0, int(delta_count))
                psum = max(0.0, float(delta_pressure))
            if psum < 0.0:
                psum = 0.0
            payload.update(
                {
                    "user": user,
                    "domain": domain,
                    "kind": "drawer",
                    "signature_count": count,
                    "pressure_sum": psum,
                }
            )
            payload.setdefault("description", "")
            out.append(
                models.PointStruct(id=pid, vector=_VECTOR, payload=payload)
            )
        self._upsert_points(out, collection=self.drawers_collection)

    def _payload_patch_from_update(
        self,
        fields: dict[str, Any],
        prepared: dict[str, Any],
    ) -> dict[str, Any]:
        """Native payload fragment for ``set_payload`` (not JSON-encoded)."""
        patch: dict[str, Any] = {}
        for col, value in prepared.items():
            if col in ("intent_tags", "metadata"):
                patch[col] = json.loads(value) if isinstance(value, str) else value
            elif col == "is_deleted":
                patch[col] = 1 if value else 0
            else:
                patch[col] = value
        if "intent_tags" in patch:
            patch["intent_tags_lc"] = _normalize_tags_lc(patch["intent_tags"])
        if "compressed_fact" in fields and "compressed_fact_hash" not in fields:
            text = str(fields["compressed_fact"] or "")
            stored, digest = encode_compressed_fact(text, store_raw=self.store_raw)
            patch["compressed_fact"] = stored
            patch["compressed_fact_hash"] = digest
        return patch

    def _set_payloads(
        self, patches: Sequence[tuple[str, dict[str, Any]]]
    ) -> None:
        """Batch ``set_payload`` (partial update — no vector rewrite)."""
        if not patches:
            return
        ops: list[models.SetPayloadOperation] = []
        for memory_id, patch in patches:
            if not patch:
                continue
            ops.append(
                models.SetPayloadOperation(
                    set_payload=models.SetPayload(
                        payload=patch,
                        points=[_point_id(memory_id)],
                    )
                )
            )
        if not ops:
            return
        self._rpc(
            "batch_update_points",
            collection_name=self.collection,
            update_operations=ops,
            wait=True,
        )

    @staticmethod
    def _drawer_deltas_for_update(
        old: Mapping[str, Any],
        patch: Mapping[str, Any],
    ) -> dict[tuple[str, str], tuple[int, float]]:
        """Compute drawer counter deltas from an update patch."""
        user = str(old.get("user") or "default")
        old_deleted = bool(old.get("is_deleted", 0))
        new_deleted = (
            bool(patch["is_deleted"])
            if "is_deleted" in patch
            else old_deleted
        )
        old_drawer = str(old.get("drawer_domain") or "general")
        new_drawer = (
            str(patch["drawer_domain"])
            if "drawer_domain" in patch
            else old_drawer
        )
        old_p = float(old.get("p_magnitude") or 0.0)
        new_p = (
            float(patch["p_magnitude"])
            if "p_magnitude" in patch
            else old_p
        )

        deltas: dict[tuple[str, str], list[float]] = {}

        def _add(domain: str, dc: int, dp: float) -> None:
            key = (user, domain)
            cur = deltas.setdefault(key, [0.0, 0.0])
            cur[0] += dc
            cur[1] += dp

        if old_deleted and new_deleted:
            return {}
        if not old_deleted and new_deleted:
            _add(old_drawer, -1, -old_p)
        elif old_deleted and not new_deleted:
            _add(new_drawer, 1, new_p)
        else:
            if old_drawer != new_drawer:
                _add(old_drawer, -1, -old_p)
                _add(new_drawer, 1, new_p)
            elif new_p != old_p:
                _add(old_drawer, 0, new_p - old_p)
        return {k: (int(v[0]), float(v[1])) for k, v in deltas.items() if v[0] or v[1]}

    def _claim_idempotency_points(
        self, claims: Sequence[tuple[str, str, str]]
    ) -> list[models.PointStruct]:
        """``(user, key, memory_id)`` → secondary uniqueness points."""
        return [
            models.PointStruct(
                id=_idempotency_point_id(user, key),
                vector=_VECTOR,
                payload={
                    "kind": "idempotency",
                    "user": user,
                    "idempotency_key": key,
                    "memory_id": memory_id,
                },
            )
            for user, key, memory_id in claims
        ]

    def _existing_idempotency_owners(
        self, pairs: Sequence[tuple[str, str]]
    ) -> dict[tuple[str, str], str]:
        """Map (user, key) → memory_id for already-claimed keys."""
        unique = list(dict.fromkeys(pairs))
        if not unique:
            return {}
        ids = [_idempotency_point_id(u, k) for u, k in unique]
        out: dict[tuple[str, str], str] = {}
        for start in range(0, len(ids), _CHUNK):
            chunk_ids = ids[start : start + _CHUNK]
            chunk_pairs = unique[start : start + _CHUNK]
            points = self._rpc(
                "retrieve",
                collection_name=self.collection,
                ids=chunk_ids,
                with_payload=True,
                with_vectors=False,
            )
            by_point = {str(p.id): p for p in points}
            for (user, key), pid in zip(chunk_pairs, chunk_ids):
                point = by_point.get(pid)
                if point is None or not point.payload:
                    continue
                owner = point.payload.get("memory_id")
                if owner:
                    out[(user, key)] = str(owner)
        return out

    def _assert_idempotency_free(
        self, user: str, key: str | None, memory_id: str
    ) -> None:
        if not key or not str(key).strip():
            return
        cleaned = str(key).strip()
        owners = self._existing_idempotency_owners([(user, cleaned)])
        owner = owners.get((user, cleaned))
        if owner is not None and owner != memory_id:
            raise ValueError(
                f"Idempotency key already used for user={user!r}: {cleaned!r}"
            )


    @staticmethod
    def _after_cursor(
        rec: SignatureRecord,
        *,
        cursor_id: str,
        cursor_p: float,
    ) -> bool:
        """True if ``rec`` is strictly after cursor in (p DESC, id DESC) order."""
        if rec.p_magnitude < cursor_p:
            return True
        if rec.p_magnitude > cursor_p:
            return False
        return rec.id < cursor_id

    # ------------------------------------------------------------------
    # BaseStorage
    # ------------------------------------------------------------------

    def save(self, sig: SignatureRecord) -> str:
        existing = self._get_point_payload(sig.id, user=None, include_deleted=True)
        if existing is not None:
            raise ValueError(f"Duplicate signature id already exists: {sig.id}")
        self._assert_idempotency_free(sig.user, sig.idempotency_key, sig.id)

        payload = record_to_payload(sig, store_raw=self.store_raw)
        points: list[models.PointStruct] = [
            models.PointStruct(
                id=_point_id(sig.id),
                vector=_VECTOR,
                payload=payload,
            )
        ]
        if sig.idempotency_key and str(sig.idempotency_key).strip():
            points.extend(
                self._claim_idempotency_points(
                    [(sig.user, str(sig.idempotency_key).strip(), sig.id)]
                )
            )
        self._upsert_points(points)
        if not bool(sig.is_deleted):
            self._adjust_drawers(
                {
                    (sig.user, sig.drawer_domain): (
                        1,
                        float(sig.p_magnitude),
                    )
                }
            )
        logger.debug("[PDM-Qdrant] Saved %s (P=%.1f)", sig.id, sig.p_magnitude)
        return sig.id

    def save_batch(self, sigs: list[SignatureRecord]) -> list[SaveBatchResult]:
        if not sigs:
            return []
        results = [SaveBatchResult(index=i, id=None) for i in range(len(sigs))]
        seen: set[str] = set()
        pending: list[tuple[int, SignatureRecord]] = []
        for index, sig in enumerate(sigs):
            if sig.id in seen:
                results[index] = SaveBatchResult(
                    index=index, id=None, error="Duplicate id in batch"
                )
                continue
            seen.add(sig.id)
            pending.append((index, sig))

        if not pending:
            return results

        existing = self._retrieve_payloads(
            [sig.id for _, sig in pending], include_deleted=True
        )
        idem_pairs = [
            (sig.user, str(sig.idempotency_key).strip())
            for _, sig in pending
            if sig.idempotency_key and str(sig.idempotency_key).strip()
        ]
        idem_owners = self._existing_idempotency_owners(idem_pairs)
        # In-batch idempotency collisions.
        seen_idem: set[tuple[str, str]] = set()

        to_write: list[tuple[int, SignatureRecord]] = []
        for index, sig in pending:
            if sig.id in existing:
                results[index] = SaveBatchResult(
                    index=index,
                    id=None,
                    error="Duplicate id already exists",
                )
                continue
            key = (
                str(sig.idempotency_key).strip()
                if sig.idempotency_key and str(sig.idempotency_key).strip()
                else None
            )
            if key:
                pair = (sig.user, key)
                owner = idem_owners.get(pair)
                if owner is not None and owner != sig.id:
                    results[index] = SaveBatchResult(
                        index=index,
                        id=None,
                        error=f"Idempotency key already used: {key!r}",
                    )
                    continue
                if pair in seen_idem:
                    results[index] = SaveBatchResult(
                        index=index,
                        id=None,
                        error=f"Duplicate idempotency key in batch: {key!r}",
                    )
                    continue
                seen_idem.add(pair)
            to_write.append((index, sig))

        if not to_write:
            return results

        sig_points = [
            models.PointStruct(
                id=_point_id(sig.id),
                vector=_VECTOR,
                payload=record_to_payload(sig, store_raw=self.store_raw),
            )
            for _, sig in to_write
        ]
        idem_points = self._claim_idempotency_points(
            [
                (sig.user, str(sig.idempotency_key).strip(), sig.id)
                for _, sig in to_write
                if sig.idempotency_key and str(sig.idempotency_key).strip()
            ]
        )
        self._upsert_points([*sig_points, *idem_points])

        drawer_acc: dict[tuple[str, str], list[float]] = {}
        for _, sig in to_write:
            if bool(sig.is_deleted):
                continue
            key = (sig.user, sig.drawer_domain)
            bucket = drawer_acc.setdefault(key, [0.0, 0.0])
            bucket[0] += 1.0
            bucket[1] += float(sig.p_magnitude)
        self._adjust_drawers(
            {k: (int(v[0]), float(v[1])) for k, v in drawer_acc.items()}
        )
        for index, sig in to_write:
            results[index] = SaveBatchResult(index=index, id=sig.id)
        return results

    def get(self, memory_id: str, user: str = "default") -> SignatureRecord | None:
        payload = self._get_point_payload(
            memory_id, user=user, include_deleted=False
        )
        if payload is None:
            return None
        return payload_to_record(payload)

    def get_many(
        self,
        ids: builtins.list[str],
        user: str = "default",
    ) -> dict[str, SignatureRecord]:
        if not ids:
            return {}
        payloads = self._retrieve_payloads(ids, user=user, include_deleted=False)
        return {mid: payload_to_record(p) for mid, p in payloads.items()}

    def update(self, memory_id: str, user: str = "default", **fields) -> None:
        prepared = prepare_update_fields(fields)
        if not prepared:
            return
        payload = self._get_point_payload(memory_id, user=user, include_deleted=True)
        if payload is None:
            logger.warning(
                "[PDM-Qdrant] update(%s) missed (missing or wrong user=%s)",
                memory_id,
                user,
            )
            return
        if "idempotency_key" in fields:
            self._assert_idempotency_free(
                user, fields.get("idempotency_key"), memory_id
            )
        patch = self._payload_patch_from_update(fields, prepared)
        self._set_payloads([(memory_id, patch)])
        new_key = fields.get("idempotency_key")
        if new_key and str(new_key).strip():
            self._upsert_points(
                self._claim_idempotency_points(
                    [(user, str(new_key).strip(), memory_id)]
                )
            )
        self._adjust_drawers(self._drawer_deltas_for_update(payload, patch))

    def update_batch(
        self,
        updates: builtins.list[tuple[str, dict]],
        user: str = "default",
    ) -> builtins.list[UpdateBatchResult]:
        if not updates:
            return []

        results: list[UpdateBatchResult] = [
            UpdateBatchResult(index=i, id=mid) for i, (mid, _) in enumerate(updates)
        ]
        prepared_rows: list[tuple[int, str, dict[str, Any], dict[str, Any]]] = []
        for index, (memory_id, fields) in enumerate(updates):
            try:
                prepared = prepare_update_fields(fields)
                if not prepared:
                    continue
                prepared_rows.append((index, memory_id, fields, prepared))
            except (TypeError, ValueError, json.JSONDecodeError) as exc:
                results[index] = UpdateBatchResult(
                    index=index, id=memory_id, error=str(exc)
                )

        if not prepared_rows:
            return results

        payloads = self._retrieve_payloads(
            [mid for _, mid, _, _ in prepared_rows],
            user=user,
            include_deleted=True,
        )
        patches: list[tuple[str, dict[str, Any]]] = []
        idem_claims: list[tuple[str, str, str]] = []
        drawer_acc: dict[tuple[str, str], list[float]] = {}

        for index, memory_id, fields, prepared in prepared_rows:
            payload = payloads.get(memory_id)
            if payload is None:
                results[index] = UpdateBatchResult(
                    index=index,
                    id=memory_id,
                    error="Memory not found or wrong user",
                )
                continue
            try:
                if "idempotency_key" in fields:
                    self._assert_idempotency_free(
                        user, fields.get("idempotency_key"), memory_id
                    )
                patch = self._payload_patch_from_update(fields, prepared)
                patches.append((memory_id, patch))
                new_key = fields.get("idempotency_key")
                if new_key and str(new_key).strip():
                    idem_claims.append((user, str(new_key).strip(), memory_id))
                for key, (dc, dp) in self._drawer_deltas_for_update(
                    payload, patch
                ).items():
                    bucket = drawer_acc.setdefault(key, [0.0, 0.0])
                    bucket[0] += dc
                    bucket[1] += dp
            except (TypeError, ValueError, json.JSONDecodeError) as exc:
                results[index] = UpdateBatchResult(
                    index=index, id=memory_id, error=str(exc)
                )

        self._set_payloads(patches)
        if idem_claims:
            self._upsert_points(self._claim_idempotency_points(idem_claims))
        self._adjust_drawers(
            {k: (int(v[0]), float(v[1])) for k, v in drawer_acc.items()}
        )
        return results

    def delete(self, memory_id: str, user: str = "default") -> None:
        payload = self._get_point_payload(memory_id, user=user, include_deleted=False)
        if payload is None:
            return
        self._set_payloads([(memory_id, {"is_deleted": 1})])
        domain = str(payload.get("drawer_domain") or "general")
        self._adjust_drawers(
            {
                (user, domain): (
                    -1,
                    -float(payload.get("p_magnitude") or 0.0),
                )
            }
        )

    def hard_delete(self, memory_id: str, user: str = "default") -> None:
        payload = self._get_point_payload(memory_id, user=user, include_deleted=True)
        if payload is None:
            return
        if not bool(payload.get("is_deleted", 0)):
            domain = str(payload.get("drawer_domain") or "general")
            self._adjust_drawers(
                {
                    (user, domain): (
                        -1,
                        -float(payload.get("p_magnitude") or 0.0),
                    )
                }
            )
        delete_ids: list[str | int] = [_point_id(memory_id)]
        key = payload.get("idempotency_key")
        if key and str(key).strip():
            delete_ids.append(_idempotency_point_id(user, str(key).strip()))
        self._rpc(
            "delete",
            collection_name=self.collection,
            points_selector=models.PointIdsList(points=delete_ids),
            wait=True,
        )

    def list(
        self,
        user: str = "default",
        limit: int = 100,
        min_pressure: float = 0.0,
        drawer: str | None = None,
        cursor_id: str | None = None,
        include_deleted: bool = False,
        tag_any: Sequence[str] | None = None,
    ) -> builtins.list[SignatureRecord]:
        if limit <= 0:
            return []

        tags = _normalize_tags_lc(tag_any)[:32]

        cursor_p: float | None = None
        if cursor_id:
            cursor_payload = self._get_point_payload(
                cursor_id, user=user, include_deleted=True
            )
            if cursor_payload is not None:
                cursor_p = float(cursor_payload.get("p_magnitude", 0.0))

        extra: list[models.Condition | models.Filter] = []
        if cursor_p is not None:
            # Server window: p <= cursor_p. Tie-break on id is client-side
            # (local Qdrant cannot OrderBy keyword composites reliably).
            extra.append(
                models.FieldCondition(
                    key="p_magnitude",
                    range=models.Range(lte=float(cursor_p)),
                )
            )
        if tags:
            # Case-insensitive tag match via denormalized lowercase tokens.
            extra.append(
                models.FieldCondition(
                    key="intent_tags_lc",
                    match=models.MatchAny(any=list(tags)),
                )
            )

        scroll_filter = self._must_filter(
            user=user,
            include_deleted=include_deleted,
            min_pressure=min_pressure,
            drawer=drawer,
            extra=extra,
        )

        out: list[SignatureRecord] = []
        offset: Any = None
        # Server-side tag filter → fetch closer to page size (still pad for
        # keyset id tie-break discards within the same pressure band).
        page_size = max(limit, min(_SCROLL_PAGE, limit * 2 if cursor_id else limit))

        while len(out) < limit:
            points, offset = self._rpc(
                "scroll",
                collection_name=self.collection,
                scroll_filter=scroll_filter,
                limit=page_size,
                offset=offset,
                order_by=models.OrderBy(
                    key="p_magnitude",
                    direction=models.Direction.DESC,
                ),
                with_payload=list(_LIST_PAYLOAD_FIELDS),
                with_vectors=False,
            )
            if not points:
                break

            page_recs = [payload_to_record(dict(p.payload or {})) for p in points]
            # p DESC, id DESC — matches SQLite ORDER BY.
            page_recs.sort(key=lambda r: (r.p_magnitude, r.id), reverse=True)

            for rec in page_recs:
                if cursor_id and cursor_p is not None:
                    if not self._after_cursor(
                        rec, cursor_id=cursor_id, cursor_p=cursor_p
                    ):
                        continue
                out.append(rec)
                if len(out) >= limit:
                    break

            if offset is None:
                break

        return out[:limit]

    def list_drawers(self, user: str = "default") -> builtins.list[DrawerInfo]:
        """O(drawers) via denormalized counters; scan fallback for legacy rows."""
        drawers: list[dict[str, Any]] = []
        offset: Any = None
        while True:
            points, offset = self._rpc(
                "scroll",
                collection_name=self.drawers_collection,
                scroll_filter=models.Filter(
                    must=[
                        models.FieldCondition(
                            key="user", match=models.MatchValue(value=user)
                        )
                    ]
                ),
                limit=_SCROLL_PAGE,
                offset=offset,
                with_payload=list(_DRAWER_PAYLOAD_FIELDS),
                with_vectors=False,
            )
            for point in points:
                payload = dict(point.payload or {})
                if str(payload.get("domain") or ""):
                    drawers.append(payload)
            if offset is None or not points:
                break

        use_counters = bool(drawers) and all(
            "signature_count" in d for d in drawers
        )
        if use_counters:
            result: list[DrawerInfo] = []
            for payload in sorted(drawers, key=lambda d: str(d.get("domain") or "")):
                domain = str(payload.get("domain") or "")
                n = int(payload.get("signature_count") or 0)
                total = float(payload.get("pressure_sum") or 0.0)
                avg = round(total / n, 2) if n else 0.0
                result.append(
                    DrawerInfo(
                        domain=domain,
                        signature_count=n,
                        avg_pressure=avg,
                        description=str(payload.get("description") or ""),
                    )
                )
            return result

        # Legacy drawers without counters: one-pass signature scan.
        meta = {
            str(d.get("domain")): str(d.get("description") or "")
            for d in drawers
            if d.get("domain")
        }
        stats: dict[str, list[float]] = {}
        offset = None
        while True:
            points, offset = self._rpc(
                "scroll",
                collection_name=self.collection,
                scroll_filter=self._must_filter(user=user, include_deleted=False),
                limit=_SCROLL_PAGE,
                offset=offset,
                with_payload=["drawer_domain", "p_magnitude"],
                with_vectors=False,
            )
            for point in points:
                payload = dict(point.payload or {})
                domain = str(payload.get("drawer_domain") or "general")
                bucket = stats.setdefault(domain, [0.0, 0.0])
                bucket[0] += 1.0
                bucket[1] += float(payload.get("p_magnitude") or 0.0)
            if offset is None or not points:
                break

        domains = sorted(set(meta) | set(stats))
        out: list[DrawerInfo] = []
        for domain in domains:
            count, total = stats.get(domain, [0.0, 0.0])
            n = int(count)
            avg = round(total / n, 2) if n else 0.0
            out.append(
                DrawerInfo(
                    domain=domain,
                    signature_count=n,
                    avg_pressure=avg,
                    description=meta.get(domain, ""),
                )
            )
        return out

    def find_by_idempotency_key(
        self,
        idempotency_key: str,
        user: str = "default",
    ) -> SignatureRecord | None:
        key = (idempotency_key or "").strip()
        if not key:
            return None
        points, _ = self._rpc(
            "scroll",
            collection_name=self.collection,
            scroll_filter=self._must_filter(
                user=user,
                include_deleted=False,
                extra=[
                    models.FieldCondition(
                        key="idempotency_key",
                        match=models.MatchValue(value=key),
                    )
                ],
            ),
            limit=1,
            with_payload=True,
            with_vectors=False,
        )
        if not points:
            return None
        return payload_to_record(dict(points[0].payload or {}))

    def find_by_hash(
        self, text_hash: str, user: str = "default"
    ) -> SignatureRecord | None:
        digest = (text_hash or "").strip()
        if not digest:
            return None
        points, _ = self._rpc(
            "scroll",
            collection_name=self.collection,
            scroll_filter=self._must_filter(
                user=user,
                include_deleted=False,
                extra=[
                    models.FieldCondition(
                        key="compressed_fact_hash",
                        match=models.MatchValue(value=digest),
                    )
                ],
            ),
            limit=1,
            with_payload=True,
            with_vectors=False,
        )
        if not points:
            return None
        return payload_to_record(dict(points[0].payload or {}))

    def find_by_hashes(
        self,
        hashes: builtins.list[str],
        user: str = "default",
    ) -> dict[str, SignatureRecord]:
        cleaned = [h.strip() for h in hashes if h and h.strip()]
        unique = list(dict.fromkeys(cleaned))
        if not unique:
            return {}
        points, _ = self._rpc(
            "scroll",
            collection_name=self.collection,
            scroll_filter=self._must_filter(
                user=user,
                include_deleted=False,
                extra=[
                    models.FieldCondition(
                        key="compressed_fact_hash",
                        match=models.MatchAny(any=unique),
                    )
                ],
            ),
            limit=max(len(unique), 1),
            with_payload=True,
            with_vectors=False,
        )
        out: dict[str, SignatureRecord] = {}
        for point in points:
            payload = dict(point.payload or {})
            digest = payload.get("compressed_fact_hash")
            if digest:
                out[str(digest)] = payload_to_record(payload)
        return out

    def find_by_idempotency_keys(
        self,
        keys: builtins.list[str],
        user: str = "default",
    ) -> dict[str, SignatureRecord]:
        cleaned = [k.strip() for k in keys if k and str(k).strip()]
        unique = list(dict.fromkeys(cleaned))
        if not unique:
            return {}
        points, _ = self._rpc(
            "scroll",
            collection_name=self.collection,
            scroll_filter=self._must_filter(
                user=user,
                include_deleted=False,
                extra=[
                    models.FieldCondition(
                        key="idempotency_key",
                        match=models.MatchAny(any=unique),
                    )
                ],
            ),
            limit=max(len(unique), 1),
            with_payload=True,
            with_vectors=False,
        )
        out: dict[str, SignatureRecord] = {}
        for point in points:
            payload = dict(point.payload or {})
            key = payload.get("idempotency_key")
            if key:
                out[str(key)] = payload_to_record(payload)
        return out

    def ping(self) -> bool:
        try:
            return bool(self._rpc("collection_exists", self.collection))
        except Exception as exc:
            logger.warning("[PDM-Qdrant] ping failed: %s", exc)
            return False

    def count(self, user: str = "default") -> int:
        result = self._rpc(
            "count",
            collection_name=self.collection,
            count_filter=self._must_filter(user=user, include_deleted=False),
            exact=True,
        )
        return int(getattr(result, "count", 0) or 0)

    def close(self) -> None:
        close = getattr(self._client, "close", None)
        if callable(close):
            close()
