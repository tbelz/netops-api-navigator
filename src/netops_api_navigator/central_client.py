"""Lightweight HTTP client for HPE Aruba Networking Central API.

Handles OAuth2 client-credentials authentication and token refresh.
Delegates to ``_http_core`` for shared auth/request logic.
"""

from __future__ import annotations

import structlog

from ._http_core import (  # noqa: F401 — re-exported for backward compat
    BaseHTTPClient,
    CentralAPIError,
    AuthenticationError,
    RateLimitError,
    NotFoundError,
    parse_error_body,
    parse_retry_wait,
)

logger = structlog.get_logger("central_client")


class CentralClient(BaseHTTPClient):
    """HTTP client for HPE Aruba Networking Central API."""

    def __init__(self, base_url: str, client_id: str, client_secret: str) -> None:
        super().__init__(base_url, client_id, client_secret, logger=logger)


class GreenLakeClient(BaseHTTPClient):
    """HTTP client for HPE GreenLake Platform API."""

    def __init__(self, base_url: str, client_id: str, client_secret: str) -> None:
        super().__init__(base_url, client_id, client_secret, logger=logger)


# Backward compatibility alias
BaseAPIClient = BaseHTTPClient
