# © 2026 Westfield Innovations LLC. Patent Pending.
# U.S. App. No. 19/739,419 | 63/953,563 | 63/953,842
# MODIFICATION PROHIBITED. USE AS SHIPPED.

"""
JWT Auth Handler — Task 2.2

Manages Bearer token authentication for cloud storage requests.
Optionally handles refresh-token flows automatically on 401.

Usage:
    auth = JWTAuth(token="eyJ...", refresh_token="eyJ...", refresh_url="https://api.azus.ai/api/v1/accounts/token/refresh/")
    headers = auth.headers()
"""

from __future__ import annotations

import logging
import time

logger = logging.getLogger(__name__)


class JWTAuth:
    """
    JWT Bearer token handler.

    Args:
        token:         JWT access token (required).
        refresh_token: Optional refresh token for automatic renewal.
        refresh_url:   URL to POST refresh token to (companion_api endpoint).
        expire_buffer: Refresh this many seconds before expiry (default 60s).
    """

    def __init__(
        self,
        token: str,
        refresh_token: str | None = None,
        refresh_url: str | None = None,
        expire_buffer: int = 60,
    ) -> None:
        self._token = token
        self._refresh_token = refresh_token
        self._refresh_url = refresh_url
        self._expire_buffer = expire_buffer
        self._expires_at: float | None = self._decode_exp(token)

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    @property
    def token(self) -> str:
        return self._token

    def headers(self) -> dict[str, str]:
        """Return Authorization headers for an HTTP request."""
        return {"Authorization": f"Bearer {self._token}"}

    def refresh(self) -> bool:
        """
        Attempt to refresh the access token using the refresh token.

        Returns True on success, False if refresh not possible or failed.
        """
        if not self._refresh_token or not self._refresh_url:
            return False
        try:
            import httpx
            resp = httpx.post(
                self._refresh_url,
                json={"refresh": self._refresh_token},
                timeout=10,
            )
            resp.raise_for_status()
            data = resp.json()
            new_token = data.get("access")
            if new_token:
                self._token = new_token
                self._expires_at = self._decode_exp(new_token)
                logger.debug("[JWTAuth] Token refreshed successfully")
                return True
        except Exception as e:
            logger.warning("[JWTAuth] Token refresh failed: %s", e)
        return False

    def is_expired(self) -> bool:
        """Return True if token is expired or within expire_buffer seconds of expiry."""
        if self._expires_at is None:
            return False  # Unknown expiry — assume valid
        return time.time() >= (self._expires_at - self._expire_buffer)

    def ensure_fresh(self) -> None:
        """Refresh token if expired. Raises RuntimeError if refresh fails."""
        if self.is_expired() and not self.refresh():
            raise RuntimeError(
                "PDM cloud auth: access token is expired and could not be refreshed. "
                "Please provide a new token via Memory(token=...)."
            )

    # ------------------------------------------------------------------
    # Private
    # ------------------------------------------------------------------

    @staticmethod
    def _decode_exp(token: str) -> float | None:
        """Decode the 'exp' claim from a JWT without validating the signature."""
        try:
            import base64
            import json
            parts = token.split(".")
            if len(parts) != 3:
                return None
            payload = parts[1]
            # Add padding
            payload += "=" * (4 - len(payload) % 4)
            decoded = base64.urlsafe_b64decode(payload)
            claims = json.loads(decoded)
            return float(claims.get("exp", 0)) or None
        except Exception:
            return None
