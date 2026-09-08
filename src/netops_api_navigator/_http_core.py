"""Shared HTTP client core — error hierarchy, token management, request logic.

This module contains the authentication and HTTP request logic shared between
``central_client.py`` (main server process) and ``central_helpers.py``
(script subprocess).  Both import from here to avoid code duplication.

This file is also copied into the script library directory at startup so that
``central_helpers.py`` can import it in subprocess context.
"""

from __future__ import annotations

import os
import threading
import time
from datetime import datetime, timezone

import httpx

TOKEN_URL = "https://sso.common.cloud.hpe.com/as/token.oauth2"

_READ_ONLY_TRUTHY = {"1", "true", "yes", "on"}


def _read_only_enabled() -> bool:
    """Return True if the READ_ONLY env var is set to a truthy value."""
    return os.environ.get("READ_ONLY", "").strip().lower() in _READ_ONLY_TRUTHY


# ── Error hierarchy ──────────────────────────────────────────────────


class CentralAPIError(Exception):
    """Base exception for Central API errors with structured error details."""

    def __init__(self, status_code: int, error_code: str = "", message: str = "", debug_id: str = ""):
        self.status_code = status_code
        self.error_code = error_code
        self.message = message
        self.debug_id = debug_id
        super().__init__(
            f"[{status_code}] {error_code}: {message}" if error_code else f"[{status_code}] {message}"
        )


class AuthenticationError(CentralAPIError):
    """401/403 authentication or authorization failure."""


class RateLimitError(CentralAPIError):
    """429 rate limit exceeded (after retry exhaustion)."""


class NotFoundError(CentralAPIError):
    """404 resource not found."""


class PaginationError(CentralAPIError):
    """Error during paginated fetch."""


# ── Response parsing helpers ─────────────────────────────────────────


def parse_error_body(resp: httpx.Response) -> dict:
    """Try to parse the standard Central error JSON body."""
    try:
        return resp.json()
    except Exception:
        return {}


def parse_retry_wait(resp: httpx.Response) -> float:
    """Extract wait time from rate-limit response headers."""
    retry_after = resp.headers.get("Retry-After")
    if retry_after:
        try:
            return max(float(retry_after), 1.0)
        except ValueError:
            pass

    reset = resp.headers.get("X-RateLimit-Reset")
    if reset:
        try:
            reset_time = datetime.fromisoformat(reset)
            if reset_time.tzinfo is None:
                reset_time = reset_time.replace(tzinfo=timezone.utc)
            wait = (reset_time - datetime.now(timezone.utc)).total_seconds()
            return max(wait, 1.0)
        except (ValueError, TypeError):
            pass

    return 5.0  # default fallback


def detect_item_key(resp: dict) -> str | None:
    """Detect the key containing the items array in a paginated response."""
    if "items" in resp and isinstance(resp["items"], list):
        return "items"
    for key, value in resp.items():
        if isinstance(value, list) and key not in ("errors",):
            return key
    return None


# ── Base API client ──────────────────────────────────────────────────


class BaseHTTPClient:
    """OAuth2-authenticated HTTP client with token management.

    Handles token acquisition via client-credentials grant, automatic
    token refresh (60s before expiry), 401 retry, 429 rate-limit retry,
    and structured error parsing.
    """

    _MAX_RATE_LIMIT_WAIT = 60  # seconds

    def __init__(self, base_url: str, client_id: str, client_secret: str,
                 *, logger=None) -> None:
        self._base_url = base_url.rstrip("/")
        self._client_id = client_id
        self._client_secret = client_secret
        self._access_token: str = ""
        self._token_expires_at: float = 0.0
        self._lock = threading.Lock()
        self._http = httpx.Client(timeout=30.0)
        self._logger = logger

    def _log(self, msg: str, **kwargs) -> None:
        """Log a message if a logger is available, otherwise no-op."""
        if self._logger:
            self._logger.info(msg, **kwargs)

    def _log_warn(self, msg: str, **kwargs) -> None:
        if self._logger:
            self._logger.warning(msg, **kwargs)

    def _ensure_token(self) -> None:
        """Generate or refresh the OAuth2 access token if expired."""
        with self._lock:
            if self._access_token and time.time() < self._token_expires_at:
                return

            self._log("token_refresh_start")
            resp = self._http.post(
                TOKEN_URL,
                data={"grant_type": "client_credentials"},
                auth=(self._client_id, self._client_secret),
                headers={"Content-Type": "application/x-www-form-urlencoded"},
            )
            resp.raise_for_status()
            body = resp.json()
            self._access_token = body["access_token"]
            # Refresh 60s before expiry; default to 7200s if not provided
            expires_in = int(body.get("expires_in", 7200))
            self._token_expires_at = time.time() + expires_in - 60
            self._log("token_refresh_done", expires_in=expires_in)

    # -- public HTTP verbs -------------------------------------------------

    def get(self, path: str, params: dict | None = None) -> dict:
        return self._request("GET", path, params=params)

    def post(self, path: str, json_body: dict | None = None, params: dict | None = None) -> dict:
        return self._request("POST", path, params=params, json_body=json_body)

    def patch(self, path: str, json_body: dict | None = None, params: dict | None = None) -> dict:
        return self._request("PATCH", path, params=params, json_body=json_body)

    def put(self, path: str, json_body: dict | None = None, params: dict | None = None) -> dict:
        return self._request("PUT", path, params=params, json_body=json_body)

    def delete(self, path: str, params: dict | None = None) -> dict:
        return self._request("DELETE", path, params=params)

    # -- request core ------------------------------------------------------

    def _request(
        self,
        method: str,
        path: str,
        params: dict | None = None,
        json_body: dict | None = None,
        *,
        _retry: bool = True,
    ) -> dict:
        """Internal request method with auto-retry on 401 and 429."""
        # READ_ONLY enforcement: refuse any non-GET request when the
        # READ_ONLY env var is truthy. This is the single chokepoint that
        # covers both the server-side CentralClient/GreenLakeClient and
        # the script-side CentralAPI/GreenLakeAPI helpers.
        if method.upper() != "GET" and _read_only_enabled():
            raise CentralAPIError(
                403,
                "READ_ONLY",
                f"Server is in READ_ONLY mode; {method.upper()} not permitted.",
            )
        self._ensure_token()
        url = f"{self._base_url}/{path.lstrip('/')}"
        resp = self._http.request(
            method,
            url,
            params=params,
            json=json_body,
            headers={
                "Authorization": f"Bearer {self._access_token}",
                "Accept": "application/json",
            },
        )

        # 429 rate limit — retry once after waiting
        if resp.status_code == 429:
            wait = parse_retry_wait(resp)
            if wait > self._MAX_RATE_LIMIT_WAIT:
                err = parse_error_body(resp)
                raise RateLimitError(
                    429, err.get("errorCode", ""),
                    f"Rate limited, retry after {wait}s exceeds {self._MAX_RATE_LIMIT_WAIT}s cap",
                    err.get("debugId", ""),
                )
            self._log("rate_limited_retry", wait_seconds=wait)
            time.sleep(wait)
            resp = self._http.request(
                method, url,
                params=params,
                json=json_body,
                headers={"Authorization": f"Bearer {self._access_token}", "Accept": "application/json"},
            )
            if resp.status_code == 429:
                err = parse_error_body(resp)
                raise RateLimitError(429, err.get("errorCode", ""), "Rate limited after retry", err.get("debugId", ""))

        # Retry once on 401 (token may have been revoked server-side)
        if resp.status_code == 401 and _retry:
            self._log("token_expired_retry")
            with self._lock:
                self._access_token = ""
                self._token_expires_at = 0.0
            return self._request(method, path, params=params, json_body=json_body, _retry=False)

        # Structured error handling for non-2xx responses
        if resp.status_code >= 400:
            err = parse_error_body(resp)
            status = resp.status_code
            error_code = err.get("errorCode", "")
            message = err.get("message", resp.text[:200])
            debug_id = err.get("debugId", "")
            if status in (401, 403):
                raise AuthenticationError(status, error_code, message, debug_id)
            if status == 404:
                raise NotFoundError(status, error_code, message, debug_id)
            raise CentralAPIError(status, error_code, message, debug_id)

        return resp.json()

    def validate(self) -> None:
        """Test credentials by requesting an OAuth2 token."""
        self._ensure_token()

    def close(self) -> None:
        """Close the underlying HTTP client."""
        self._http.close()
