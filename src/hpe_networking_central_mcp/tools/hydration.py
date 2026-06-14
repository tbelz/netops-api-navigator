"""Opt-in runtime hydration MCP shell.

This module intentionally starts as a small introspection surface. The roadmap
in ``docs/runtime-hydration-roadmap.md`` defines the later generic endpoint
hydration substrate; this first tool only proves the feature flag and gives
agents an honest status payload when the shell is enabled.
"""

from __future__ import annotations

import json

from mcp.types import ToolAnnotations

from ..config import Settings


def register_runtime_hydration_tools(mcp, settings: Settings) -> None:
    """Register opt-in runtime hydration status tools."""

    @mcp.tool(
        annotations=ToolAnnotations(readOnlyHint=True, idempotentHint=True, openWorldHint=False),
    )
    def get_runtime_hydration_status() -> str:
        """Report runtime hydration feature status.

        Runtime hydration is an opt-in roadmap feature. In this shell stage the
        server only reports status; generic endpoint hydration and observation
        persistence are not implemented yet.
        """
        return json.dumps(
            {
                "enabled": settings.runtime_hydration,
                "stage": "flag-shell",
                "capabilities": [],
                "implemented": {
                    "generic_endpoint_hydration": False,
                    "observation_persistence": False,
                    "materialization": False,
                },
                "roadmap": "docs/runtime-hydration-roadmap.md",
                "message": (
                    "Runtime hydration shell is enabled. This stage registers "
                    "status introspection only; no API hydration is available yet."
                ),
            },
            indent=2,
        )
