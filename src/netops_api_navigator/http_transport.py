"""Authenticated local HTTP transport for multiple clients of one runtime."""

from __future__ import annotations

import secrets

from starlette.responses import Response


class LocalBearerAuth:
    """Protect every HTTP request, including session creation and deletion."""

    def __init__(self, app, token: str):
        self.app = app
        self.expected = f"Bearer {token}".encode("ascii")

    async def __call__(self, scope, receive, send):
        if scope["type"] == "http":
            values = [value for key, value in scope["headers"] if key.lower() == b"authorization"]
            if len(values) != 1 or not secrets.compare_digest(values[0], self.expected):
                await Response(status_code=401, headers={"WWW-Authenticate": "Bearer"})(
                    scope, receive, send
                )
                return
        await self.app(scope, receive, send)


def serve_http(mcp, *, port: int, token: str) -> None:
    """Serve independent MCP sessions without transferring graph ownership."""
    import uvicorn

    app = LocalBearerAuth(mcp.streamable_http_app(), token)
    uvicorn.run(
        app, host="127.0.0.1", port=port, access_log=False,
        timeout_graceful_shutdown=5,
    )
