"""Safe runtime status tool."""

from __future__ import annotations

import json
import platform
import sys
from collections.abc import Callable

from mcp.types import ToolAnnotations

from ..config import Settings


def register_status_tool(
    mcp,
    settings: Settings,
    *,
    graph_available: Callable[[], bool],
    connected: bool,
) -> None:
    """Register a status endpoint that never exposes credentials or tokens."""

    @mcp.tool(
        annotations=ToolAnnotations(
            readOnlyHint=True,
            idempotentHint=True,
            openWorldHint=False,
        ),
    )
    def get_server_status() -> str:
        """Return the local MCP profile, platform, and readiness state.

        Credentials, access tokens, process environments, and full filesystem
        paths are intentionally excluded from this response.
        """
        return json.dumps(
            {
                "status": "ready" if graph_available() else "degraded",
                "profile": settings.profile,
                "read_only": settings.read_only,
                "central_connected": connected,
                "knowledge_projection": settings.knowledge_projection,
                "platform": platform.system(),
                "architecture": platform.machine(),
                "python": (
                    f"{sys.version_info.major}.{sys.version_info.minor}."
                    f"{sys.version_info.micro}"
                ),
            },
            indent=2,
        )
