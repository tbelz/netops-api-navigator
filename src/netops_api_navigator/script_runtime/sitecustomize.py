"""Auto-loaded by Python at interpreter startup when this directory is on
PYTHONPATH. Installed by ``tools.execution._build_env`` only when the
MCP server is in READ_ONLY mode.

Monkey-patches ``httpx.Client`` and ``httpx.AsyncClient`` to refuse any
non-GET / non-HEAD / non-OPTIONS request. This catches scripts that
bypass ``central_helpers`` and use raw httpx with the OAuth credentials
present in the environment.

This is *defence in depth*, not a security boundary — see the package
docstring in ``script_runtime/__init__.py``.
"""

from __future__ import annotations

import os

_TRUTHY = {"1", "true", "yes", "on"}
_SAFE_METHODS = {"GET", "HEAD", "OPTIONS"}

# URLs that are always allowed regardless of method, because they are
# required infrastructure even for read-only workflows.  The HPE SSO
# token endpoint must be reachable so that ``central_helpers`` can
# obtain a bearer token before issuing any GET requests.
_SAFE_URLS = {
    "https://sso.common.cloud.hpe.com/as/token.oauth2",
}


def _read_only() -> bool:
    return os.environ.get("READ_ONLY", "").strip().lower() in _TRUTHY


def _install_httpx_guard() -> None:
    try:
        import httpx
    except Exception:  # httpx may not be importable in some environments
        return

    original_send = httpx.Client.send
    original_async_send = httpx.AsyncClient.send

    def _guarded_send(self, request, *args, **kwargs):  # type: ignore[no-redef]
        method = (request.method or "").upper()
        url = str(request.url)
        if _read_only() and method not in _SAFE_METHODS and url not in _SAFE_URLS:
            raise httpx.HTTPError(
                f"READ_ONLY mode: refusing {method} {request.url}. "
                "The MCP server is in READ_ONLY mode; mutating HTTP "
                "requests are not permitted from scripts."
            )
        return original_send(self, request, *args, **kwargs)

    async def _guarded_async_send(self, request, *args, **kwargs):  # type: ignore[no-redef]
        method = (request.method or "").upper()
        url = str(request.url)
        if _read_only() and method not in _SAFE_METHODS and url not in _SAFE_URLS:
            raise httpx.HTTPError(
                f"READ_ONLY mode: refusing {method} {request.url}. "
                "The MCP server is in READ_ONLY mode; mutating HTTP "
                "requests are not permitted from scripts."
            )
        return await original_async_send(self, request, *args, **kwargs)

    httpx.Client.send = _guarded_send  # type: ignore[assignment]
    httpx.AsyncClient.send = _guarded_async_send  # type: ignore[assignment]


if _read_only():
    _install_httpx_guard()
