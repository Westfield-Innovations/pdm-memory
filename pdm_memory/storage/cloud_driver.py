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
from pdm_memory.storage.base import BaseStorage
from pdm_memory.storage.errors import CloudNotFoundError, CloudStorageError

logger = logging.getLogger(__name__)

# Default AZUS Companion API base URL
DEFAULT_CLOUD_URL = "https://api.azus.ai"

# Ingest API clamps (companion SignatureIngestSerializer)
_API_DECAY_RATE_MIN = 0.01
_API_DECAY_RATE_MAX = 0.5
_API_URGENCY_MIN = 1.0
_API_URGENCY_MAX = 10.0


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

    def get(self, memory_id: str, user: str = "default") -> SignatureRecord | None:
        """
        GET /api/v1/pdm/signatures/<id>/

        Returns:
            SignatureRecord, or None if the cloud returns 404.

        Raises:
            CloudStorageError: On network / auth / non-404 HTTP failures.
        """
        try:
            resp = self._get(f"/api/v1/pdm/signatures/{memory_id}/")
            rec = self._payload_to_record(resp.json())
            if self._record_deleted(rec):
                return None
            return rec
        except CloudNotFoundError:
            return None

    def find_by_idempotency_key(
        self,
        idempotency_key: str,
        user: str = "default",
    ) -> SignatureRecord | None:
        key = idempotency_key.strip()
        if not key:
            return None
        for rec in self.list(user=user, limit=500):
            if rec.idempotency_key == key:
                return rec
        return None

    def ping(self) -> bool:
        try:
            self._get("/api/v1/pdm/retrieve", params={"limit": 1, "min_pressure": 0})
            return True
        except Exception as exc:
            logger.warning("[PDM-Cloud] ping failed: %s", exc)
            return False

    def update(self, memory_id: str, user: str = "default", **fields) -> None:
        """PATCH /api/v1/pdm/signatures/<id>/ — raises on failure."""
        fields = dict(fields)
        fields.pop("user", None)
        fields.pop("id", None)
        # Serialise datetimes for JSON
        for key in ("last_retrieved", "created_at", "t_deadline"):
            if key in fields and isinstance(fields[key], datetime):
                fields[key] = fields[key].isoformat()
        self._patch(f"/api/v1/pdm/signatures/{memory_id}/", fields)

    def delete(self, memory_id: str, user: str = "default") -> None:
        """Soft-delete via metadata flag (Companion API has no is_deleted column yet)."""
        rec = self.get(memory_id, user=user)
        if rec is None:
            raise CloudNotFoundError(
                f"Cloud resource not found: /api/v1/pdm/signatures/{memory_id}/",
                status_code=404,
                path=f"/api/v1/pdm/signatures/{memory_id}/",
            )
        meta = dict(rec.metadata or {})
        meta["_pdm_is_deleted"] = True
        self.update(memory_id, user=user, metadata=meta)

    def hard_delete(self, memory_id: str, user: str = "default") -> None:
        """DELETE /api/v1/pdm/signatures/<id>/ — permanent removal."""
        import httpx

        self._auth.ensure_fresh()
        path = f"/api/v1/pdm/signatures/{memory_id}/"
        try:
            resp = httpx.delete(
                f"{self._base_url}{path}",
                headers=self._auth.headers(),
                timeout=self._timeout,
            )
        except httpx.HTTPError as exc:
            raise CloudStorageError(
                f"Cloud DELETE failed: {exc}", path=path
            ) from exc
        self._raise_for_status(resp, path)

    def list(
        self,
        user: str = "default",
        limit: int = 100,
        min_pressure: float = 0.0,
        drawer: str | None = None,
        cursor_id: str | None = None,
        include_deleted: bool = False,
    ) -> builtins.list[SignatureRecord]:
        """GET /api/v1/pdm/retrieve — client-side keyset when API has no cursor param."""
        fetch_limit = limit if cursor_id is None else max(limit * 4, 200)
        params: dict = {"limit": fetch_limit, "min_pressure": min_pressure}
        if drawer:
            params["drawer"] = drawer
        resp = self._get("/api/v1/pdm/retrieve", params=params)
        data = resp.json()
        items = data if isinstance(data, list) else data.get("results", [])
        records = [self._payload_to_record(item) for item in items]
        if not include_deleted:
            records = [rec for rec in records if not self._record_deleted(rec)]
        records.sort(key=lambda rec: (rec.p_magnitude, rec.id))
        records.reverse()

        if cursor_id:
            cursor = next((rec for rec in records if rec.id == cursor_id), None)
            if cursor is None:
                cursor = self.get(cursor_id, user=user)
            if cursor is not None:
                records = [
                    rec
                    for rec in records
                    if rec.p_magnitude < cursor.p_magnitude
                    or (rec.p_magnitude == cursor.p_magnitude and rec.id < cursor_id)
                ]

        return records[:limit]

    def list_drawers(self, user: str = "default") -> builtins.list[DrawerInfo]:
        """GET /api/v1/pdm/drawers — raises on failure."""
        resp = self._get("/api/v1/pdm/drawers")
        items = resp.json()
        if not isinstance(items, list):
            items = items.get("results", [])
        return [
            DrawerInfo(
                domain=item.get("domain", ""),
                signature_count=item.get("signature_count", 0),
                avg_pressure=float(item.get("avg_pressure", 0.0)),
                description=item.get("description", ""),
            )
            for item in items
        ]

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
            except Exception:
                pass
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
    def _record_to_payload(cls, sig: SignatureRecord) -> dict:
        """
        Full signature dump for ingest / sync.

        Includes fields the Companion ingest serializer accepts today
        (t_deadline, urgency_rate, decay_rate, …) plus SDK extras. Unknown
        keys are ignored by DRF; accepted keys must not be silently dropped
        on our side.
        """
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
        }

        return {
            "id": sig.id,
            "user": sig.user,
            "compressed_fact": sig.compressed_fact,
            "source": sig.source,
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
            "urgency_rate": cls._clamp_api_urgency(sig.urgency_rate),
            "metadata": meta,
            # Companion ingest alias some clients use
            "related_tickers": list(meta.get("related_tickers") or []),
        }

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
