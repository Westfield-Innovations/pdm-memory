"""
Cloud Storage Driver — Task 2.1

Implements BaseStorage by making HTTP requests to the AZUS Companion API
instead of reading/writing a local file.

Requires: httpx (listed as a main dependency in pyproject.toml).

Usage:
    from pdm_memory.storage.cloud_driver import CloudDriver
    from pdm_memory.auth import JWTAuth

    auth = JWTAuth(token="eyJ...")
    driver = CloudDriver(auth=auth, base_url="https://api.azus.ai")

    # Or via Memory:
    mem = Memory(store="cloud", token="eyJ...", cloud_url="https://api.azus.ai")
"""

from __future__ import annotations

import json
import logging
from typing import List, Optional

from pdm_memory.auth.jwt_handler import JWTAuth
from pdm_memory.core.signature import DrawerInfo, SignatureRecord
from pdm_memory.storage.base import BaseStorage

logger = logging.getLogger(__name__)

# Default AZUS Companion API base URL
DEFAULT_CLOUD_URL = "https://api.azus.ai"


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
        except ImportError:
            raise ImportError(
                "httpx is required for cloud mode. Install it: pip install httpx"
            )
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
        # Cloud may assign a different ID
        cloud_id = data.get("id") or data.get("signature_id") or sig.id
        logger.debug("[PDM-Cloud] Saved signature %s", cloud_id)
        return str(cloud_id)

    def get(self, memory_id: str, user: str = "default") -> Optional[SignatureRecord]:
        """GET /api/v1/pdm/signatures/<id>/"""
        try:
            resp = self._get(f"/api/v1/pdm/signatures/{memory_id}/")
            return self._payload_to_record(resp.json())
        except Exception as e:
            logger.warning("[PDM-Cloud] get(%s) failed: %s", memory_id, e)
            return None

    def update(self, memory_id: str, **fields) -> None:
        """PATCH /api/v1/pdm/signatures/<id>/"""
        try:
            self._patch(f"/api/v1/pdm/signatures/{memory_id}/", fields)
        except Exception as e:
            logger.warning("[PDM-Cloud] update(%s) failed: %s", memory_id, e)

    def delete(self, memory_id: str, user: str = "default") -> None:
        """DELETE /api/v1/pdm/signatures/<id>/"""
        try:
            import httpx
            self._auth.ensure_fresh()
            resp = httpx.delete(
                f"{self._base_url}/api/v1/pdm/signatures/{memory_id}/",
                headers=self._auth.headers(),
                timeout=self._timeout,
            )
            resp.raise_for_status()
        except Exception as e:
            logger.warning("[PDM-Cloud] delete(%s) failed: %s", memory_id, e)

    def list(
        self,
        user: str = "default",
        limit: int = 100,
        min_pressure: float = 0.0,
        drawer: Optional[str] = None,
    ) -> List[SignatureRecord]:
        """GET /api/v1/pdm/retrieve — list signatures with filters."""
        params: dict = {"limit": limit, "min_pressure": min_pressure}
        if drawer:
            params["drawer"] = drawer
        try:
            resp = self._get("/api/v1/pdm/retrieve", params=params)
            data = resp.json()
            items = data if isinstance(data, list) else data.get("results", [])
            return [self._payload_to_record(item) for item in items]
        except Exception as e:
            logger.warning("[PDM-Cloud] list() failed: %s", e)
            return []

    def list_drawers(self, user: str = "default") -> List[DrawerInfo]:
        """GET /api/v1/pdm/drawers"""
        try:
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
        except Exception as e:
            logger.warning("[PDM-Cloud] list_drawers() failed: %s", e)
            return []

    # ------------------------------------------------------------------
    # HTTP helpers
    # ------------------------------------------------------------------

    def _post(self, path: str, payload: dict):
        import httpx
        self._auth.ensure_fresh()
        resp = httpx.post(
            f"{self._base_url}{path}",
            json=payload,
            headers=self._auth.headers(),
            timeout=self._timeout,
        )
        self._handle_auth_retry(resp, "POST", path, payload)
        resp.raise_for_status()
        return resp

    def _get(self, path: str, params: Optional[dict] = None):
        import httpx
        self._auth.ensure_fresh()
        resp = httpx.get(
            f"{self._base_url}{path}",
            params=params,
            headers=self._auth.headers(),
            timeout=self._timeout,
        )
        self._handle_auth_retry(resp, "GET", path)
        resp.raise_for_status()
        return resp

    def _patch(self, path: str, payload: dict):
        import httpx
        self._auth.ensure_fresh()
        resp = httpx.patch(
            f"{self._base_url}{path}",
            json=payload,
            headers=self._auth.headers(),
            timeout=self._timeout,
        )
        resp.raise_for_status()
        return resp

    def _handle_auth_retry(self, resp, method: str, path: str, payload=None) -> None:
        """On 401, attempt one token refresh and retry."""
        import httpx
        if resp.status_code == 401:
            if self._auth.refresh():
                if method == "POST":
                    resp2 = httpx.post(
                        f"{self._base_url}{path}",
                        json=payload,
                        headers=self._auth.headers(),
                        timeout=self._timeout,
                    )
                    resp.__dict__.update(resp2.__dict__)
                elif method == "GET":
                    resp2 = httpx.get(
                        f"{self._base_url}{path}",
                        headers=self._auth.headers(),
                        timeout=self._timeout,
                    )
                    resp.__dict__.update(resp2.__dict__)

    # ------------------------------------------------------------------
    # Serialisation helpers
    # ------------------------------------------------------------------

    @staticmethod
    def _record_to_payload(sig: SignatureRecord) -> dict:
        return {
            "compressed_fact": sig.compressed_fact,
            "source": sig.source,
            "p_magnitude": sig.p_magnitude,
            "t_persistence": sig.t_persistence,
            "phase_privilege": sig.phase_privilege,
            "intent_tags": sig.intent_tags,
            "question_regime": sig.question_regime,
            "drawer_domain": sig.drawer_domain,
            "metadata": sig.metadata,
        }

    @staticmethod
    def _payload_to_record(data: dict) -> SignatureRecord:
        from datetime import datetime
        tags = data.get("intent_tags") or []
        if isinstance(tags, str):
            try:
                tags = json.loads(tags)
            except Exception:
                tags = []

        def _dt(val):
            if not val:
                return None
            try:
                return datetime.fromisoformat(str(val))
            except Exception:
                return None

        return SignatureRecord(
            id=str(data.get("id", "")),
            user=str(data.get("user", "default")),
            compressed_fact=data.get("compressed_fact", ""),
            source=data.get("source", "chat"),
            p_magnitude=float(data.get("p_magnitude", 50.0)),
            t_persistence=float(data.get("t_persistence", 30.0)),
            phase_privilege=float(data.get("phase_privilege", 1.0)),
            effective_spike=data.get("effective_spike"),
            intent_tags=tags,
            question_regime=data.get("question_regime", "neutral"),
            domain=data.get("domain", "insight"),
            drawer_domain=data.get("drawer_domain") or data.get("drawer", "general"),
            retrieval_count=int(data.get("retrieval_count", 0)),
            last_retrieved=_dt(data.get("last_retrieved")),
            created_at=_dt(data.get("created_at")),
            validation_prediction_total=int(data.get("validation_prediction_total", 0)),
            validation_prediction_correct=int(data.get("validation_prediction_correct", 0)),
            metadata=data.get("metadata") or {},
        )
