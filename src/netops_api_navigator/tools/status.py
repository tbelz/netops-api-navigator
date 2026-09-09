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
    seed_status: Callable[[], dict] | None = None,
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
        paths are intentionally excluded from this response. A true
        ``central_connected`` means OAuth token validation succeeded; Central
        still authorizes each API endpoint independently.
        """
        raw_jobs = seed_status() if seed_status is not None else {}
        jobs = raw_jobs.copy() if isinstance(raw_jobs, dict) else {}
        job_states = {
            str(name): {
                key: details[key]
                for key in (
                    "status",
                    "exit_code",
                    "queued_at",
                    "started_at",
                    "finished_at",
                )
                if key in details
            }
            for name, details in jobs.items()
            if isinstance(details, dict)
        }
        states = {details.get("status") for details in job_states.values()}
        if not settings.has_credentials:
            topology_state = "unavailable"
        elif not job_states or "running" in states:
            topology_state = "syncing"
        elif states & {"failed", "error", "partial"}:
            topology_state = "degraded"
        elif states == {"success"}:
            topology_state = "ready"
        else:
            topology_state = "pending"
        return json.dumps(
            {
                "status": "ready" if graph_available() else "degraded",
                "profile": settings.profile,
                "read_only": settings.read_only,
                "central_connected": connected,
                "central_authentication": (
                    "token_validated"
                    if connected
                    else "not_validated" if settings.has_credentials else "not_configured"
                ),
                "topology_sync": {
                    "status": topology_state,
                    "jobs": job_states,
                },
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
