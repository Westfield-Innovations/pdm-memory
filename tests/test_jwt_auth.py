"""
Comprehensive tests for JWTAuth and PDM Memory Cloud Authentication.

HOW USERS OBTAIN AND USE THE JWT KEYS:

1. Authenticate with credentials:
   POST /api/v1/accounts/auth/login/
   Payload:
   {
       "email": "user@example.com",
       "password": "your_password"
   }

   Response:
   {
       "access": "<JWT_ACCESS_TOKEN>",     # Expiry typically 5-15 mins
       "refresh": "<JWT_REFRESH_TOKEN>",   # Expiry typically 1-7 days
       "user": { ... }
   }

2. Initialize PDM SDK in Cloud Mode:
   from pdm_memory import Memory

   mem = Memory(
       store="cloud",
       token="<JWT_ACCESS_TOKEN>",
       refresh_token="<JWT_REFRESH_TOKEN>",
       cloud_url="http://localhost:8000"
   )

   # The SDK will use the access token as a Bearer header.
   # If a 401 Unauthorized is returned, or the token is about to expire,
   # it automatically calls:
   # POST http://localhost:8000/api/v1/accounts/token/refresh/ with {"refresh": refresh_token}
   # to retrieve a new access token and retries the request seamlessly.
"""

import time
import base64
import json
import pytest
from unittest.mock import MagicMock, patch
from pdm_memory.auth.jwt_handler import JWTAuth
from pdm_memory.storage.cloud_driver import CloudDriver

def make_mock_jwt(expire_time: float) -> str:
    """Helper to generate a mock JWT token with a specific expiration timestamp."""
    header = base64.urlsafe_b64encode(json.dumps({"alg": "HS256", "typ": "JWT"}).encode()).decode().rstrip("=")
    payload = base64.urlsafe_b64encode(json.dumps({"exp": expire_time, "user_id": 123}).encode()).decode().rstrip("=")
    signature = "mock_sig"
    return f"{header}.{payload}.{signature}"

class TestJWTAuthUnit:
    def test_jwt_auth_init_and_decode(self):
        """Test that JWTAuth successfully parses expiration from a valid JWT payload."""
        expiry = int(time.time()) + 3600  # expires in 1 hour
        token = make_mock_jwt(expiry)
        
        auth = JWTAuth(token=token)
        assert auth.token == token
        assert auth._expires_at == expiry
        assert not auth.is_expired()

    def test_jwt_auth_expired(self):
        """Test that is_expired returns True when the token is past expiry (or within buffer)."""
        now = time.time()
        
        # Already expired
        expired_token = make_mock_jwt(now - 100)
        auth1 = JWTAuth(token=expired_token)
        assert auth1.is_expired() is True
        
        # Within the default 60s buffer
        expiring_token = make_mock_jwt(now + 30)
        auth2 = JWTAuth(token=expiring_token, expire_buffer=60)
        assert auth2.is_expired() is True

    def test_headers_structure(self):
        """Test headers return format contains Authorization Bearer."""
        token = make_mock_jwt(time.time() + 1000)
        auth = JWTAuth(token=token)
        assert auth.headers() == {"Authorization": f"Bearer {token}"}

    @patch("httpx.post")
    def test_refresh_success(self, mock_post):
        """Test successful automatic token refresh."""
        old_token = make_mock_jwt(time.time() - 10)
        new_expiry = time.time() + 3600
        new_token = make_mock_jwt(new_expiry)
        
        # Mock response from token refresh endpoint
        mock_response = MagicMock()
        mock_response.status_code = 200
        mock_response.json.return_value = {"access": new_token}
        mock_post.return_value = mock_response
        
        auth = JWTAuth(
            token=old_token,
            refresh_token="mock_refresh",
            refresh_url="http://localhost:8000/api/v1/accounts/token/refresh/"
        )
        
        # Verify it can refresh
        success = auth.refresh()
        assert success is True
        assert auth.token == new_token
        assert auth._expires_at == pytest.approx(new_expiry, abs=1)
        mock_post.assert_called_once_with(
            "http://localhost:8000/api/v1/accounts/token/refresh/",
            json={"refresh": "mock_refresh"},
            timeout=10
        )

    def test_refresh_noop_without_credentials(self):
        """Test that refresh immediately returns False if refresh token/url is missing."""
        token = make_mock_jwt(time.time())
        auth = JWTAuth(token=token)
        assert auth.refresh() is False

    @patch("httpx.post")
    def test_ensure_fresh_raises_on_failure(self, mock_post):
        """Test that ensure_fresh raises a RuntimeError if the token is expired and refresh fails."""
        expired_token = make_mock_jwt(time.time() - 10)
        
        # Mock a failed refresh response
        import httpx
        mock_response = MagicMock()
        mock_response.status_code = 400
        mock_response.raise_for_status.side_effect = httpx.HTTPStatusError(
            "Bad Request", request=MagicMock(), response=mock_response
        )
        mock_post.return_value = mock_response
        
        auth = JWTAuth(
            token=expired_token,
            refresh_token="mock_refresh",
            refresh_url="http://localhost:8000/api/v1/accounts/token/refresh/"
        )
        
        with pytest.raises(RuntimeError) as exc_info:
            auth.ensure_fresh()
        assert "PDM cloud auth: access token is expired" in str(exc_info.value)

class TestCloudDriverAuthIntegration:
    @patch("httpx.post")
    @patch("httpx.get")
    def test_cloud_driver_auto_retry_on_401(self, mock_get, mock_post):
        """Test that CloudDriver retries the HTTP call if a 401 is returned, after refreshing the token."""
        old_token = make_mock_jwt(time.time() + 1000)
        new_token = make_mock_jwt(time.time() + 3600)
        
        # Mock the refresh call
        mock_refresh_resp = MagicMock()
        mock_refresh_resp.status_code = 200
        mock_refresh_resp.json.return_value = {"access": new_token}
        mock_post.return_value = mock_refresh_resp
        
        # Mock the GET call - first time returns 401, second time returns 200
        mock_get_401 = MagicMock()
        mock_get_401.status_code = 401
        
        mock_get_200 = MagicMock()
        mock_get_200.status_code = 200
        mock_get_200.json.return_value = {"results": []}
        
        # Mock side effects: first 401, then 200
        mock_get.side_effect = [mock_get_401, mock_get_200]
        
        auth = JWTAuth(
            token=old_token,
            refresh_token="mock_refresh",
            refresh_url="http://localhost:8000/api/v1/accounts/token/refresh/"
        )
        driver = CloudDriver(auth=auth, base_url="http://localhost:8000")
        
        # Call list
        results = driver.list()
        
        # Assertions
        assert results == []
        assert auth.token == new_token  # successfully updated
        assert mock_get.call_count == 2
        # Check authorization headers in the second call
        headers_first = mock_get.call_args_list[0][1]["headers"]
        headers_second = mock_get.call_args_list[1][1]["headers"]
        assert headers_first == {"Authorization": f"Bearer {old_token}"}
        assert headers_second == {"Authorization": f"Bearer {new_token}"}
