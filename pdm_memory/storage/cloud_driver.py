# © 2026 Westfield Innovations LLC. Patent Pending.
# U.S. App. No. 19/739,419 | 63/953,563 | 63/953,842
# MODIFICATION PROHIBITED. USE AS SHIPPED.

"""
Cloud Storage Driver — Task 2.1

Implements BaseStorage by making HTTP requests to the AZUS Companion API
instead of reading/writing a local file.

Requires: httpx (listed as a main dependency in pyproject.toml).

Fail-fast: transport / auth / 5xx / unexpected 4xx raise CloudStorageError.
Only HTTP 404 on get() is soft (returns None) so sync can distinguish
"missing" from "API is down" (which must NOT trigger duplicate saves).
"""

from __future__ import annotations

import builtins
import json
import logging
from datetime import datetime
from typing import Any

from pdm_memory.auth.jwt_handler import JWTAuth
from pdm_memory.core.signature import DrawerInfo, SignatureRecord
from pdm_memory.storage.base import BaseStorage, SaveBatchResult, UpdateBatchResult
from pdm_memory.storage.errors import CloudNotFoundError, CloudStorageError

logger = logging.getLogger(__name__)

# Default AZUS Companion API base URL
DEFAULT_CLOUD_URL = "http://localhost:8000"

# Match companion_api.pdm.signature_mutations.MAX_BATCH_SIZE
_API_BATCH_MAX = 100
# Must match companion_api.pdm.apis.pdm_views._RETRIEVE_MAX_PAGE_SIZE
_API_PAGE_MAX = 200
_API_DECAY_RATE_MIN = 0.01
_API_DECAY_RATE_MAX = 0.5
_API_URGENCY_MIN = 1.0
_API_URGENCY_MAX = 10.0
_API_P_MAGNITUDE_MIN = 50.0
_API_P_MAGNITUDE_MAX = 100.0
_API_INTENT_TAGS_MIN = 3
# companion Signature.SOURCE_CHOICES
_API_SOURCES = frozenset(
    {
        "azus_chat",
        "junior_clipboard",
        "slack",
        "manual",
        "docvault",
        "operator_accumulation",
        "autonomous_web",
        "companion_app",
    }
)
_API_DEFAULT_SOURCE = "azus_chat"
# companion_api.pdm.signature_mutations.PATCHABLE_FIELDS — only these may appear
# in PATCH / batch-update. Client-derived fields not on the allowlist (e.g.
# effective_spike) are stripped; the API recomputes them server-side.
_API_PATCHABLE_FIELDS = frozenset(
    {
        "compressed_fact",
        "p_magnitude",
        "t_persistence",
        "intent_tags",
        "question_regime",
        "source",
        "phase_privilege",
        "retrieval_count",
        "last_retrieved",
        "t_deadline",
        "t_event_at",
        "urgency_rate",
        "decay_rate",
        "is_deleted",
        "is_complete",
        "validation_prediction_total",
        "validation_prediction_correct",
    }
)
_API_PATCH_DATETIME_FIELDS = frozenset(
    {"last_retrieved", "created_at", "t_deadline", "t_event_at"}
)


def _sanitise_patch_fields(fields: dict[str, Any]) -> dict[str, Any]:
    """Keep only Companion PATCH-allowlisted keys; ISO-serialise datetimes.

    Dropping client-derived fields (e.g. ``effective_spike``) is intentional:
    the API recomputes spike when ``p_magnitude`` / ``t_persistence`` /
    ``phase_privilege`` change. V-counters are patchable and are kept.
    """
    body = dict(fields)
    body.pop("user", None)
    body.pop("id", None)
    dropped = [k for k in body if k not in _API_PATCHABLE_FIELDS]
    if dropped:
        logger.debug(
            "[PDM-Cloud] stripping non-PATCH fields from update: %s",
            sorted(dropped),
        )
        for k in dropped:
            body.pop(k, None)
    for key in _API_PATCH_DATETIME_FIELDS:
        if key in body and isinstance(body[key], datetime):
            body[key] = body[key].isoformat()
    return body


class CloudDriver(BaseStorage):
    """
    HTTP-backed PDM storage driver targeting the AZUS Companion API.

    All network calls use httpx (sync).  Timeout is configurable.
    On 401 responses, automatically attempts token refresh if JWTAuth
    was constructed with a refresh_token.

    Args:
        auth:     JWTAuth instance carrying the user's access token.
        base_url: Base URL of the AZUS Companion API.
        timeout:  Request timeout in seconds.
        user:     Username to scope all requests (overrides token claim).
    """

    def __init__(
        self,
        auth: JWTAuth,
        base_url: str = DEFAULT_CLOUD_URL,
        timeout: float = 15.0,
        user: str = "default",
    ) -> None:
        try:
            import httpx  # noqa: F401 — guard for optional dep
        except ImportError as exc:
            raise ImportError(
                "httpx is required for cloud mode. Install it: pip install httpx"
            ) from exc
        self._auth = auth
        self._base_url = base_url.rstrip("/")
        self._timeout = timeout
        self._user = user

    # ------------------------------------------------------------------
    # BaseStorage
    # ------------------------------------------------------------------

    def save(self, sig: SignatureRecord) -> str:
        """POST /api/v1/pdm/ingest — create a new signature in the cloud."""
        payload = self._record_to_payload(sig)
        resp = self._post("/api/v1/pdm/ingest", payload)
        data = resp.json()
        cloud_id = data.get("id") or data.get("signature_id") or sig.id
        logger.debug("[PDM-Cloud] Saved signature %s", cloud_id)
        return str(cloud_id)

    def save_batch(
        self, sigs: builtins.list[SignatureRecord]
    ) -> builtins.list[SaveBatchResult]:
        """
        Bulk create via POST /api/v1/pdm/ingest/batch.

        Semantics mirror SQLite/Postgres:
        - empty input → []
        - per-index SaveBatchResult with id or error (partial success)
        - client pre-validation errors without HTTP
        - bulk route errors (including 404) propagate as CloudStorageError 
        """
        if not sigs:
            return []

        results: list[SaveBatchResult] = [
            SaveBatchResult(index=i, id=None) for i in range(len(sigs))
        ]
        # Build payloads first so invalid rows never hit the network, and
        # keep valid ones in request order for index alignment.
        pending: list[tuple[int, dict]] = []
        for index, sig in enumerate(sigs):
            try:
                pending.append((index, self._record_to_payload(sig)))
            except Exception as exc:
                results[index] = SaveBatchResult(
                    index=index, id=None, error=str(exc)
                )

        if not pending:
            return results

        for start in range(0, len(pending), _API_BATCH_MAX):
            chunk = pending[start : start + _API_BATCH_MAX]
            items = [payload for _, payload in chunk]
            resp = self._post(
                "/api/v1/pdm/ingest/batch",
                {"items": items},
            )
            data = resp.json() if resp.content else {}
            api_results = data.get("results") if isinstance(data, dict) else None
            if not isinstance(api_results, list):
                raise CloudStorageError(
                    "Batch ingest response missing 'results' list",
                    path="/api/v1/pdm/ingest/batch",
                    status_code=getattr(resp, "status_code", None),
                )
            for entry in api_results:
                if not isinstance(entry, dict):
                    continue
                local_i = entry.get("index")
                if not isinstance(local_i, int) or not (
                    0 <= local_i < len(chunk)
                ):
                    continue
                orig_index = chunk[local_i][0]
                err = entry.get("error")
                cid = entry.get("id")
                if err:
                    results[orig_index] = SaveBatchResult(
                        index=orig_index, id=None, error=str(err)
                    )
                elif cid:
                    results[orig_index] = SaveBatchResult(
                        index=orig_index, id=str(cid)
                    )
                else:
                    results[orig_index] = SaveBatchResult(
                        index=orig_index,
                        id=None,
                        error="Batch ingest returned no id",
                    )

        return results

    def get(self, memory_id: str, user: str = "default") -> SignatureRecord | None:
        """
        GET /api/v1/pdm/signatures/<id>

        Returns:
            SignatureRecord, or None if the cloud returns 404.

        Raises:
            CloudStorageError: On network / auth / non-404 HTTP failures.
        """
        try:
            resp = self._get(f"/api/v1/pdm/signatures/{memory_id}")
            rec = self._payload_to_record(resp.json())
            if self._record_deleted(rec):
                return None
            return rec
        except CloudNotFoundError:
            return None

    def get_many(
        self,
        ids: builtins.list[str],
        user: str = "default",
    ) -> dict[str, SignatureRecord]:
        """
        Bulk-fetch via POST /api/v1/pdm/signatures/batch-get.

        Same contract as SQLite/Postgres: dict keyed by id; missing ids
        omitted. Bulk route errors (including 404) raise.
        """
        if not ids:
            return {}

        unique_ids = list(dict.fromkeys(str(i) for i in ids if i))
        result: dict[str, SignatureRecord] = {}

        for start in range(0, len(unique_ids), _API_BATCH_MAX):
            chunk = unique_ids[start : start + _API_BATCH_MAX]
            resp = self._post(
                "/api/v1/pdm/signatures/batch-get",
                {"ids": chunk},
            )
            data = resp.json() if resp.content else {}
            if not isinstance(data, dict):
                raise CloudStorageError(
                    f"Unexpected batch-get body type: {type(data).__name__}",
                    path="/api/v1/pdm/signatures/batch-get",
                )
            items = data.get("signatures")
            if not isinstance(items, list):
                raise CloudStorageError(
                    "Batch-get response missing 'signatures' list",
                    path="/api/v1/pdm/signatures/batch-get",
                )
            for item in items:
                if not isinstance(item, dict):
                    continue
                rec = self._payload_to_record(item)
                if self._record_deleted(rec):
                    continue
                result[rec.id] = rec

        return result

    def find_by_idempotency_key(
        self,
        idempotency_key: str,
        user: str = "default",
    ) -> SignatureRecord | None:
        """
        GET /api/v1/pdm/signatures/by-idempotency-key?key=...

        Server-side lookup. Missing key → None.
        """
        key = (idempotency_key or "").strip()
        if not key:
            return None
        try:
            resp = self._get(
                "/api/v1/pdm/signatures/by-idempotency-key",
                params={"key": key},
            )
            data = resp.json() if resp.content else {}
            if not isinstance(data, dict) or not data.get("id"):
                return None
            rec = self._payload_to_record(data)
            if self._record_deleted(rec):
                return None
            return rec
        except CloudNotFoundError:
            return None
        except CloudStorageError as exc:
            if exc.status_code == 404:
                return None
            raise

    def find_by_hash(
        self, text_hash: str, user: str = "default"
    ) -> SignatureRecord | None:
        """
        GET /api/v1/pdm/signatures/by-hash?hash=...

        Server-side content-hash lookup for Memory.save(dedupe=True).
        Missing / invalid hash → None. Avoids list-scan BaseStorage default.
        """
        digest = (text_hash or "").strip().lower()
        if not digest:
            return None
        try:
            resp = self._get(
                "/api/v1/pdm/signatures/by-hash",
                params={"hash": digest},
            )
            data = resp.json() if resp.content else {}
            if not isinstance(data, dict) or not data.get("id"):
                return None
            rec = self._payload_to_record(data)
            if self._record_deleted(rec):
                return None
            return rec
        except CloudNotFoundError:
            return None
        except CloudStorageError as exc:
            if exc.status_code == 404:
                return None
            raise

    def ping(self) -> bool:
        try:
            self._get("/api/v1/pdm/retrieve", params={"limit": 1, "min_p": 0})
            return True
        except Exception as exc:
            logger.warning("[PDM-Cloud] ping failed: %s", exc)
            return False

    def update(self, memory_id: str, user: str = "default", **fields) -> None:
        """PATCH /api/v1/pdm/signatures/<id> — raises on failure.

        Non-PATCH fields (e.g. ``effective_spike``) are dropped; the API
        recomputes derived values from accepted inputs.
        """
        body = _sanitise_patch_fields(fields)
        if not body:
            return
        self._patch(f"/api/v1/pdm/signatures/{memory_id}", body)

    def update_batch(
        self,
        updates: builtins.list[tuple[str, dict]],
        user: str = "default",
    ) -> builtins.list[UpdateBatchResult]:
        """
        Bulk PATCH via POST /api/v1/pdm/signatures/batch-update.

        Per-index UpdateBatchResult with id + optional error, like local drivers.
        Bulk route errors (including 404) raise. Payload keys outside Companion
        PATCH allowlist are stripped (same as ``update``).
        """
        if not updates:
            return []

        results: list[UpdateBatchResult] = [
            UpdateBatchResult(index=i, id=memory_id)
            for i, (memory_id, _) in enumerate(updates)
        ]

        prepared: list[tuple[int, str, dict]] = []
        for index, (memory_id, fields) in enumerate(updates):
            try:
                body = _sanitise_patch_fields(fields)
                if not body:
                    # Nothing left after stripping derived-only updates.
                    continue
                prepared.append((index, memory_id, body))
            except Exception as exc:
                results[index] = UpdateBatchResult(
                    index=index, id=memory_id, error=str(exc)
                )

        if not prepared:
            return results

        for start in range(0, len(prepared), _API_BATCH_MAX):
            chunk = prepared[start : start + _API_BATCH_MAX]
            payload = {
                "updates": [
                    {"id": memory_id, **fields}
                    for _, memory_id, fields in chunk
                ]
            }
            resp = self._post(
                "/api/v1/pdm/signatures/batch-update",
                payload,
            )
            data = resp.json() if resp.content else {}
            api_results = data.get("results") if isinstance(data, dict) else None
            if not isinstance(api_results, list):
                raise CloudStorageError(
                    "Batch-update response missing 'results' list",
                    path="/api/v1/pdm/signatures/batch-update",
                )
            for entry in api_results:
                if not isinstance(entry, dict):
                    continue
                local_i = entry.get("index")
                if not isinstance(local_i, int) or not (
                    0 <= local_i < len(chunk)
                ):
                    continue
                orig_index = chunk[local_i][0]
                mid = chunk[local_i][1]
                err = entry.get("error")
                if err:
                    results[orig_index] = UpdateBatchResult(
                        index=orig_index, id=mid, error=str(err)
                    )
                else:
                    results[orig_index] = UpdateBatchResult(
                        index=orig_index, id=str(entry.get("id") or mid)
                    )

        return results

    def delete(self, memory_id: str, user: str = "default") -> None:
        """
        Soft-delete a signature: PATCH /api/v1/pdm/signatures/<id>
        with ``{"is_deleted": true}``.

        Permanent removal is ``hard_delete()`` → DELETE the same path.
        """
        self.update(memory_id, user=user, is_deleted=True)

    def hard_delete(self, memory_id: str, user: str = "default") -> None:
        """DELETE /api/v1/pdm/signatures/<id> — permanent removal."""
        self._delete(f"/api/v1/pdm/signatures/{memory_id}")

    def list(
        self,
        user: str = "default",
        limit: int = 100,
        min_pressure: float = 0.0,
        drawer: str | None = None,
        cursor_id: str | None = None,
        include_deleted: bool = False,
        tag_any: builtins.list[str] | tuple[str, ...] | None = None,
    ) -> builtins.list[SignatureRecord]:
        """
        GET /api/v1/pdm/signatures — keyset list, mirrors Postgres/SQLite.

        Storage scan endpoint (no retrieve side effects). Pages transparently
        when ``limit`` exceeds ``_API_PAGE_MAX`` (server max page size).
        Optional ``tag_any`` is filtered client-side (API has no tag param yet).
        """
        if limit <= 0:
            return []

        tags = {t.strip().lower() for t in (tag_any or ()) if t and str(t).strip()}
        # Over-fetch when tag-filtering so callers can still fill ``limit`` matches.
        need = limit if not tags else min(max(limit * 5, limit), 2_000)

        records: builtins.list[SignatureRecord] = []
        next_cursor = cursor_id
        path = "/api/v1/pdm/signatures"

        while len(records) < need:
            page_limit = min(need - len(records), _API_PAGE_MAX)
            params: dict = {"limit": page_limit, "min_p": min_pressure}
            if drawer:
                params["drawer"] = drawer
            if next_cursor:
                params["cursor_id"] = next_cursor
            if include_deleted:
                params["include_deleted"] = "true"

            resp = self._get(path, params=params)
            data = resp.json()
            if not isinstance(data, dict):
                raise CloudStorageError(
                    f"Unexpected list body type: {type(data).__name__}",
                    path=path,
                )
            items = data.get("signatures")
            if not isinstance(items, list):
                raise CloudStorageError(
                    "List response missing 'signatures' list",
                    path=path,
                )

            page_records = [
                self._payload_to_record(item)
                for item in items
                if isinstance(item, dict)
            ]
            raw_count = len(page_records)
            if not include_deleted:
                page_records = [
                    rec for rec in page_records if not self._record_deleted(rec)
                ]
            if tags:
                page_records = [
                    rec
                    for rec in page_records
                    if {tag.lower() for tag in (rec.intent_tags or []) if tag} & tags
                ]

            if raw_count == 0:
                break

            if page_records:
                records.extend(page_records)

            next_cursor = data.get("next_cursor_id")
            if not next_cursor and page_records:
                next_cursor = page_records[-1].id
            if raw_count < page_limit:
                break
            if not next_cursor:
                break

        return records[:limit]

    def count(self, user: str = "default") -> int:
        """
        Live signature count from list ``pagination.total_count``.

        One cheap page request (size 1) — not ``len(list(...))``.
        """
        path = "/api/v1/pdm/signatures"
        params = {
            "limit": 1,
            "min_p": 0.0,
        }
        resp = self._get(path, params=params)
        data = resp.json()
        if not isinstance(data, dict):
            raise CloudStorageError(
                f"Unexpected list body type: {type(data).__name__}",
                path=path,
            )
        pagination = (
            data.get("pagination") if isinstance(data.get("pagination"), dict) else {}
        )
        try:
            return int(pagination.get("total_count") or 0)
        except (TypeError, ValueError) as exc:
            raise CloudStorageError(
                "List response missing valid pagination.total_count",
                path=path,
            ) from exc

    def list_drawers(self, user: str = "default") -> builtins.list[DrawerInfo]:
        """GET /api/v1/pdm/drawers — raises on failure."""
        resp = self._get("/api/v1/pdm/drawers")
        data = resp.json()
        if not isinstance(data, dict):
            raise CloudStorageError(
                f"Unexpected drawers body type: {type(data).__name__}",
                path="/api/v1/pdm/drawers",
            )
        items = data.get("drawers")
        if not isinstance(items, list):
            raise CloudStorageError(
                "Drawers response missing 'drawers' list",
                path="/api/v1/pdm/drawers",
            )
        return [
            DrawerInfo(
                domain=item.get("domain", ""),
                signature_count=item.get("signature_count", 0),
                avg_pressure=float(item.get("avg_pressure", 0.0)),
                description=item.get("description", ""),
            )
            for item in items
        ]

    def current_resolution(
        self,
        observer: str = "principal",
        target: str = "operator",
        domain: str = "*",
    ) -> Any:
        """
        Query RelationshipChannel resolution via Companion integrity profile.

        GET /api/v1/integrity/profile/?observer=&target=&domain=
        """
        from pdm_memory.models import RelationshipChannelResolution

        path = "/api/v1/integrity/profile/"
        params = {
            "observer": observer,
            "target": target,
            "domain": domain,
        }
        resp = self._get(path, params=params)
        data = resp.json()
        if not isinstance(data, dict):
            raise CloudStorageError(
                f"Unexpected integrity profile body type: {type(data).__name__}",
                path=path,
            )
        channel = data.get("relationship_channel")
        if not isinstance(channel, dict):
            raise CloudStorageError(
                "Integrity profile response missing 'relationship_channel' object",
                path=path,
            )
        return RelationshipChannelResolution.from_payload(channel)

    # ------------------------------------------------------------------
    # HTTP helpers
    # ------------------------------------------------------------------

    def _post(self, path: str, payload: dict):
        import httpx

        self._auth.ensure_fresh()
        try:
            resp = httpx.post(
                f"{self._base_url}{path}",
                json=payload,
                headers=self._auth.headers(),
                timeout=self._timeout,
            )
        except httpx.HTTPError as exc:
            raise CloudStorageError(f"Cloud POST failed: {exc}", path=path) from exc
        self._handle_auth_retry(resp, "POST", path, payload)
        # After auth retry the response object is mutated in place
        self._raise_for_status(resp, path)
        return resp

    def _get(self, path: str, params: dict | None = None):
        import httpx

        self._auth.ensure_fresh()
        try:
            resp = httpx.get(
                f"{self._base_url}{path}",
                params=params,
                headers=self._auth.headers(),
                timeout=self._timeout,
            )
        except httpx.HTTPError as exc:
            raise CloudStorageError(f"Cloud GET failed: {exc}", path=path) from exc
        self._handle_auth_retry(resp, "GET", path, params=params)
        self._raise_for_status(resp, path)
        return resp

    def _patch(self, path: str, payload: dict):
        import httpx

        self._auth.ensure_fresh()
        try:
            resp = httpx.patch(
                f"{self._base_url}{path}",
                json=payload,
                headers=self._auth.headers(),
                timeout=self._timeout,
            )
        except httpx.HTTPError as exc:
            raise CloudStorageError(f"Cloud PATCH failed: {exc}", path=path) from exc
        self._handle_auth_retry(resp, "PATCH", path, payload)
        self._raise_for_status(resp, path)
        return resp

    def _delete(self, path: str):
        import httpx

        self._auth.ensure_fresh()
        try:
            resp = httpx.delete(
                f"{self._base_url}{path}",
                headers=self._auth.headers(),
                timeout=self._timeout,
            )
        except httpx.HTTPError as exc:
            raise CloudStorageError(f"Cloud DELETE failed: {exc}", path=path) from exc
        self._handle_auth_retry(resp, "DELETE", path)
        self._raise_for_status(resp, path)
        return resp

    def _handle_auth_retry(
        self,
        resp,
        method: str,
        path: str,
        payload=None,
        params=None,
    ) -> None:
        """On 401, attempt one token refresh and retry in-place."""
        import httpx

        if resp.status_code != 401:
            return
        if not self._auth.refresh():
            return

        if method == "POST":
            resp2 = httpx.post(
                f"{self._base_url}{path}",
                json=payload,
                headers=self._auth.headers(),
                timeout=self._timeout,
            )
        elif method == "GET":
            resp2 = httpx.get(
                f"{self._base_url}{path}",
                params=params,
                headers=self._auth.headers(),
                timeout=self._timeout,
            )
        elif method == "PATCH":
            resp2 = httpx.patch(
                f"{self._base_url}{path}",
                json=payload,
                headers=self._auth.headers(),
                timeout=self._timeout,
            )
        elif method == "DELETE":
            resp2 = httpx.delete(
                f"{self._base_url}{path}",
                headers=self._auth.headers(),
                timeout=self._timeout,
            )
        else:
            return
        resp.__dict__.update(resp2.__dict__)

    @staticmethod
    def _raise_for_status(resp, path: str) -> None:
        status = getattr(resp, "status_code", None)
        if status == 404:
            raise CloudNotFoundError(
                f"Cloud resource not found: {path}",
                status_code=404,
                path=path,
            )
        if status is not None and status >= 400:
            detail = ""
            try:
                detail = f" body={resp.text[:300]}"
            except Exception as err:
                logger.debug("[PDM-Cloud] error reading body: %s", err)
            raise CloudStorageError(
                f"Cloud HTTP {status} for {path}{detail}",
                status_code=status,
                path=path,
            )

    # ------------------------------------------------------------------
    # Serialisation helpers
    # ------------------------------------------------------------------

    @staticmethod
    def _iso(dt: datetime | None) -> str | None:
        if dt is None:
            return None
        return dt.isoformat()

    @classmethod
    def _clamp_api_decay_rate(cls, decay_rate: float) -> float:
        """SDK legacy default 0.9 is invalid for Companion PDM-T ingest (max 0.5)."""
        if _API_DECAY_RATE_MIN <= decay_rate <= _API_DECAY_RATE_MAX:
            return decay_rate
        return 0.05

    @classmethod
    def _clamp_api_urgency(cls, urgency_rate: float) -> float:
        return max(_API_URGENCY_MIN, min(_API_URGENCY_MAX, float(urgency_rate)))

    @classmethod
    def _map_api_source(cls, source: str | None) -> str:
        """Map SDK source to a Companion SOURCE_CHOICES value."""
        if source and source in _API_SOURCES:
            return source
        # SDK legacy default "chat" and any unknown label → azus_chat
        return _API_DEFAULT_SOURCE

    @classmethod
    def _validate_ingest_record(cls, sig: SignatureRecord) -> None:
        """Fail before HTTP when payload cannot pass Companion ingest rules."""
        tags = list(sig.intent_tags or [])
        if len(tags) < _API_INTENT_TAGS_MIN:
            raise ValueError(
                f"Companion ingest requires at least {_API_INTENT_TAGS_MIN} "
                f"intent_tags (got {len(tags)})."
            )
        p = float(sig.p_magnitude)
        if p < _API_P_MAGNITUDE_MIN or p > _API_P_MAGNITUDE_MAX:
            raise ValueError(
                f"Companion ingest requires p_magnitude in "
                f"[{_API_P_MAGNITUDE_MIN}, {_API_P_MAGNITUDE_MAX}] (got {p})."
            )

    @classmethod
    def _record_to_payload(cls, sig: SignatureRecord) -> dict:
        """
        Full signature dump for ingest / sync.

        Includes fields the Companion ingest serializer accepts today
        (t_deadline, urgency_rate, decay_rate, …) plus SDK extras. Unknown
        keys are ignored by DRF; accepted keys must not be silently dropped
        on our side.

        Validates Companion invariants (tags count, p_magnitude range) and
        maps SDK source labels (e.g. chat) to azus_chat when needed.
        """
        cls._validate_ingest_record(sig)

        meta: dict[str, Any] = dict(sig.metadata or {})
        if sig.idempotency_key:
            meta["_idempotency_key"] = sig.idempotency_key
        # Preserve SDK-only fields that the public response serializer may omit
        meta["_pdm_sdk"] = {
            "client_id": sig.id,
            "domain": sig.domain,
            "validation_prediction_total": sig.validation_prediction_total,
            "validation_prediction_correct": sig.validation_prediction_correct,
            "retrieval_count": sig.retrieval_count,
            "last_retrieved": cls._iso(sig.last_retrieved),
            "created_at": cls._iso(sig.created_at),
            "effective_spike": sig.effective_spike,
            "decay_rate_sdk": sig.decay_rate,
            "t_event_at": cls._iso(sig.t_event_at),
            "source_sdk": sig.source,
        }

        payload = {
            "id": sig.id,
            "user": sig.user,
            "compressed_fact": sig.compressed_fact,
            "source": cls._map_api_source(sig.source),
            "p_magnitude": sig.p_magnitude,
            "t_persistence": sig.t_persistence,
            "phase_privilege": sig.phase_privilege,
            "effective_spike": sig.effective_spike,
            "intent_tags": list(sig.intent_tags or []),
            "question_regime": sig.question_regime,
            "domain": sig.domain,
            "drawer_domain": sig.drawer_domain,
            "retrieval_count": sig.retrieval_count,
            "last_retrieved": cls._iso(sig.last_retrieved),
            "created_at": cls._iso(sig.created_at),
            "validation_prediction_total": sig.validation_prediction_total,
            "validation_prediction_correct": sig.validation_prediction_correct,
            "decay_rate": cls._clamp_api_decay_rate(sig.decay_rate),
            "t_deadline": cls._iso(sig.t_deadline),
            "t_event_at": cls._iso(sig.t_event_at),
            "urgency_rate": cls._clamp_api_urgency(sig.urgency_rate),
            "metadata": meta,
            # Companion ingest alias some clients use
            "related_tickers": list(meta.get("related_tickers") or []),
        }
        if sig.idempotency_key:
            payload["idempotency_key"] = sig.idempotency_key
        return payload

    @staticmethod
    def _record_deleted(rec: SignatureRecord) -> bool:
        if rec.is_deleted:
            return True
        meta = rec.metadata or {}
        return bool(meta.get("_pdm_is_deleted"))

    @classmethod
    def _payload_to_record(cls, data: dict) -> SignatureRecord:
        tags = data.get("intent_tags") or []
        if isinstance(tags, str):
            try:
                tags = json.loads(tags)
            except (ValueError, json.JSONDecodeError):
                tags = []

        def _dt(val: Any) -> datetime | None:
            if not val:
                return None
            if isinstance(val, datetime):
                return val
            try:
                return datetime.fromisoformat(str(val).replace("Z", "+00:00"))
            except (TypeError, ValueError):
                return None

        meta = data.get("metadata") or {}
        if isinstance(meta, str):
            try:
                meta = json.loads(meta)
            except (ValueError, json.JSONDecodeError):
                meta = {}
        sdk_bag = meta.get("_pdm_sdk") if isinstance(meta, dict) else None
        if not isinstance(sdk_bag, dict):
            sdk_bag = {}

        record_id = str(data.get("id") or sdk_bag.get("client_id") or "")
        domain = (
            data.get("domain")
            or sdk_bag.get("domain")
            or data.get("drawer")
            or data.get("drawer_domain")
            or "insight"
        )

        kwargs: dict[str, Any] = {
            "user": str(data.get("user", "default")),
            "compressed_fact": data.get("compressed_fact", ""),
            "source": data.get("source", "chat"),
            "p_magnitude": float(data.get("p_magnitude", 50.0)),
            "t_persistence": float(data.get("t_persistence", 30.0)),
            "phase_privilege": float(data.get("phase_privilege", 1.0)),
            "effective_spike": (
                data.get("effective_spike")
                if data.get("effective_spike") is not None
                else sdk_bag.get("effective_spike")
            ),
            "intent_tags": tags,
            "question_regime": data.get("question_regime", "neutral"),
            "domain": str(domain),
            "drawer_domain": str(
                data.get("drawer_domain") or data.get("drawer") or "general"
            ),
            "retrieval_count": int(
                data.get("retrieval_count", sdk_bag.get("retrieval_count", 0)) or 0
            ),
            "last_retrieved": _dt(
                data.get("last_retrieved") or sdk_bag.get("last_retrieved")
            ),
            "created_at": _dt(data.get("created_at") or sdk_bag.get("created_at")),
            "validation_prediction_total": int(
                data.get(
                    "validation_prediction_total",
                    sdk_bag.get("validation_prediction_total", 0),
                )
                or 0
            ),
            "validation_prediction_correct": int(
                data.get(
                    "validation_prediction_correct",
                    sdk_bag.get("validation_prediction_correct", 0),
                )
                or 0
            ),
            "decay_rate": float(
                sdk_bag.get("decay_rate_sdk", data.get("decay_rate", 0.9)) or 0.9
            ),
            "t_deadline": _dt(data.get("t_deadline")),
            "t_event_at": _dt(
                data.get("t_event_at") or sdk_bag.get("t_event_at")
            ),
            "urgency_rate": float(data.get("urgency_rate", 2.0) or 2.0),
            "metadata": (
                {k: v for k, v in meta.items() if k != "_pdm_sdk"}
                if isinstance(meta, dict)
                else {}
            ),
            "is_deleted": bool(data.get("is_deleted"))
            or bool(isinstance(meta, dict) and meta.get("_pdm_is_deleted")),
            "idempotency_key": data.get("idempotency_key")
            or (meta.get("_idempotency_key") if isinstance(meta, dict) else None),
        }
        if record_id:
            kwargs["id"] = record_id
        return SignatureRecord(**kwargs)
