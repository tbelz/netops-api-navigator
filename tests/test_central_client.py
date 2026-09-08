"""Unit tests for _http_core — auth lifecycle, retries, error handling.

Tests BaseHTTPClient's token management, 401/429 retry logic, and error
parsing without requiring live Central credentials.
"""

from __future__ import annotations

import json
import sys
import time
from datetime import datetime, timezone, timedelta
from pathlib import Path
from unittest.mock import MagicMock, patch, PropertyMock

import httpx
import pytest

sys.path.insert(0, str(Path(__file__).parent / "src"))

from netops_api_navigator._http_core import (
    BaseHTTPClient,
    CentralAPIError,
    AuthenticationError,
    NotFoundError,
    RateLimitError,
    parse_error_body,
    parse_retry_wait,
    detect_item_key,
)


# ── Fixtures ────────────────────────────────────────────────────────


@pytest.fixture
def client():
    """Create a BaseHTTPClient with fake credentials."""
    c = BaseHTTPClient(
        "https://api.example.com",
        client_id="test-id",
        client_secret="test-secret",
    )
    yield c
    c.close()


def _token_response(expires_in: int = 7200) -> httpx.Response:
    """Build a fake token response."""
    return httpx.Response(
        200,
        json={"access_token": "tok-123", "expires_in": expires_in},
        request=httpx.Request("POST", "https://sso.common.cloud.hpe.com/as/token.oauth2"),
    )


def _api_response(status: int = 200, body: dict | None = None, headers: dict | None = None) -> httpx.Response:
    """Build a fake API response."""
    return httpx.Response(
        status,
        json=body or {},
        headers=headers or {},
        request=httpx.Request("GET", "https://api.example.com/test"),
    )


# ── Token management ────────────────────────────────────────────────


class TestTokenManagement:
    """Tests for _ensure_token and token caching."""

    def test_initial_token_fetch(self, client):
        """First request should acquire a token."""
        with patch.object(client._http, "post", return_value=_token_response()) as mock_post:
            client._ensure_token()

        assert client._access_token == "tok-123"
        mock_post.assert_called_once()

    def test_cached_token_reused(self, client):
        """Second call should reuse the cached token."""
        with patch.object(client._http, "post", return_value=_token_response()) as mock_post:
            client._ensure_token()
            client._ensure_token()

        mock_post.assert_called_once()

    def test_token_refresh_on_expiry(self, client):
        """Token should be refreshed when expired."""
        with patch.object(client._http, "post", return_value=_token_response()):
            client._ensure_token()

        # Force expiry
        client._token_expires_at = time.time() - 10

        with patch.object(client._http, "post", return_value=_token_response()) as mock_post:
            client._ensure_token()

        mock_post.assert_called_once()

    def test_60s_buffer_before_expiry(self, client):
        """Token should be refreshed if within 60s of expiry."""
        resp = _token_response(expires_in=7200)
        with patch.object(client._http, "post", return_value=resp):
            client._ensure_token()

        # Should have set expiry to now + 7200 - 60 = now + 7140
        expected_min = time.time() + 7100
        expected_max = time.time() + 7180
        assert expected_min < client._token_expires_at < expected_max

    def test_validate_calls_ensure_token(self, client):
        """validate() should perform a token fetch."""
        with patch.object(client._http, "post", return_value=_token_response()):
            client.validate()

        assert client._access_token == "tok-123"


# ── 401 retry ────────────────────────────────────────────────────────


class TestRetry401:
    """Tests for 401 token-expiry retry logic."""

    def test_401_triggers_token_refresh_and_retry(self, client):
        """A 401 should clear the token, refresh, and retry once."""
        # Pre-populate token
        client._access_token = "old-tok"
        client._token_expires_at = time.time() + 3600

        resp_401 = _api_response(401, {"errorCode": "UNAUTH", "message": "expired"})
        resp_200 = _api_response(200, {"data": "ok"})
        token_resp = _token_response()

        call_count = 0

        def mock_request(method, url, **kwargs):
            nonlocal call_count
            call_count += 1
            if call_count == 1:
                return resp_401
            return resp_200

        with patch.object(client._http, "request", side_effect=mock_request), \
             patch.object(client._http, "post", return_value=token_resp):
            result = client.get("/test")

        assert result == {"data": "ok"}
        assert call_count == 2

    def test_401_no_infinite_retry(self, client):
        """A second 401 after retry should raise AuthenticationError."""
        client._access_token = "old-tok"
        client._token_expires_at = time.time() + 3600

        resp_401 = _api_response(401, {"errorCode": "UNAUTH", "message": "bad creds"})
        token_resp = _token_response()

        with patch.object(client._http, "request", return_value=resp_401), \
             patch.object(client._http, "post", return_value=token_resp):
            with pytest.raises(AuthenticationError) as exc_info:
                client.get("/test")

        assert exc_info.value.status_code == 401


# ── 429 rate limit ───────────────────────────────────────────────────


class TestRetry429:
    """Tests for 429 rate-limit retry logic."""

    def test_429_retry_after_header(self, client):
        """Should wait per Retry-After and retry."""
        client._access_token = "tok"
        client._token_expires_at = time.time() + 3600

        resp_429 = _api_response(429, {}, headers={"Retry-After": "1"})
        resp_200 = _api_response(200, {"ok": True})

        call_count = 0

        def mock_request(method, url, **kwargs):
            nonlocal call_count
            call_count += 1
            if call_count == 1:
                return resp_429
            return resp_200

        with patch.object(client._http, "request", side_effect=mock_request), \
             patch("netops_api_navigator._http_core.time.sleep") as mock_sleep:
            result = client.get("/test")

        assert result == {"ok": True}
        mock_sleep.assert_called_once_with(1.0)

    def test_429_exceeds_cap_raises(self, client):
        """Wait time exceeding MAX_RATE_LIMIT_WAIT should raise immediately."""
        client._access_token = "tok"
        client._token_expires_at = time.time() + 3600

        resp_429 = _api_response(429, {"errorCode": "RATE_LIMIT"}, headers={"Retry-After": "120"})

        with patch.object(client._http, "request", return_value=resp_429):
            with pytest.raises(RateLimitError) as exc_info:
                client.get("/test")

        assert "exceeds" in exc_info.value.message

    def test_429_persistent_raises(self, client):
        """Two successive 429s should raise RateLimitError."""
        client._access_token = "tok"
        client._token_expires_at = time.time() + 3600

        resp_429 = _api_response(429, {"errorCode": "RL"}, headers={"Retry-After": "1"})

        with patch.object(client._http, "request", return_value=resp_429), \
             patch("netops_api_navigator._http_core.time.sleep"):
            with pytest.raises(RateLimitError):
                client.get("/test")


# ── Error handling ───────────────────────────────────────────────────


class TestErrorHandling:
    """Tests for structured error responses."""

    def test_404_raises_not_found(self, client):
        client._access_token = "tok"
        client._token_expires_at = time.time() + 3600

        resp = _api_response(404, {"errorCode": "NOT_FOUND", "message": "no such thing"})
        with patch.object(client._http, "request", return_value=resp):
            with pytest.raises(NotFoundError) as exc_info:
                client.get("/missing")

        assert exc_info.value.status_code == 404
        assert exc_info.value.error_code == "NOT_FOUND"

    def test_500_raises_central_api_error(self, client):
        client._access_token = "tok"
        client._token_expires_at = time.time() + 3600

        resp = _api_response(500, {"errorCode": "INTERNAL", "message": "oops"})
        with patch.object(client._http, "request", return_value=resp):
            with pytest.raises(CentralAPIError) as exc_info:
                client.get("/broken")

        assert exc_info.value.status_code == 500


# ── parse_error_body / parse_retry_wait ──────────────────────────────


class TestParseHelpers:
    """Tests for response parsing utility functions."""

    def test_parse_error_body_valid_json(self):
        resp = _api_response(400, {"errorCode": "BAD", "message": "bad"})
        body = parse_error_body(resp)
        assert body["errorCode"] == "BAD"

    def test_parse_error_body_invalid_json(self):
        resp = httpx.Response(
            400,
            content=b"not json",
            headers={"content-type": "text/plain"},
            request=httpx.Request("GET", "https://api.example.com/x"),
        )
        body = parse_error_body(resp)
        assert body == {}

    def test_parse_retry_wait_retry_after(self):
        resp = _api_response(429, {}, headers={"Retry-After": "10"})
        assert parse_retry_wait(resp) == 10.0

    def test_parse_retry_wait_min_1s(self):
        resp = _api_response(429, {}, headers={"Retry-After": "0.1"})
        assert parse_retry_wait(resp) == 1.0

    def test_parse_retry_wait_x_ratelimit_reset(self):
        future = datetime.now(timezone.utc) + timedelta(seconds=30)
        resp = _api_response(429, {}, headers={"X-RateLimit-Reset": future.isoformat()})
        wait = parse_retry_wait(resp)
        assert 25 < wait < 35

    def test_parse_retry_wait_default(self):
        resp = _api_response(429, {})
        assert parse_retry_wait(resp) == 5.0


# ── detect_item_key ──────────────────────────────────────────────────


class TestDetectItemKey:
    """Tests for paginated response item key detection."""

    def test_standard_items_key(self):
        assert detect_item_key({"items": [1, 2], "total": 2}) == "items"

    def test_custom_list_key(self):
        assert detect_item_key({"devices": [1, 2], "total": 2}) == "devices"

    def test_errors_excluded(self):
        assert detect_item_key({"errors": ["e1"]}) is None

    def test_no_list(self):
        assert detect_item_key({"message": "ok"}) is None


# ── HTTP verb delegation ─────────────────────────────────────────────


class TestHTTPVerbs:
    """Test that HTTP verb methods delegate correctly."""

    @pytest.mark.parametrize("verb,method_name", [
        ("GET", "get"),
        ("POST", "post"),
        ("PUT", "put"),
        ("PATCH", "patch"),
        ("DELETE", "delete"),
    ])
    def test_verb_delegates_to_request(self, client, verb, method_name):
        client._access_token = "tok"
        client._token_expires_at = time.time() + 3600

        resp = _api_response(200, {"ok": True})
        with patch.object(client._http, "request", return_value=resp) as mock_req:
            method = getattr(client, method_name)
            if method_name in ("post", "put", "patch"):
                result = method("/test", json_body={"x": 1})
            else:
                result = method("/test")

        assert result == {"ok": True}
        assert mock_req.call_args[0][0] == verb
