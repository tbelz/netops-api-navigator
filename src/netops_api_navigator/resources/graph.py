"""MCP Resource — graph schema description for agent context."""

from __future__ import annotations

import json
from typing import Callable

import structlog

from ..graph.manager import GraphManager

logger = structlog.get_logger("resources.graph")


def register_graph_resources(mcp, graph: GraphManager, get_seed_status: Callable[[], dict]):
    """Register graph-related resources with the MCP server."""

    @mcp.resource("graph://schema")
    def graph_schema() -> str:
        """Schema of the Central configuration graph — node types, relationships, and example Cypher queries."""
        return graph.get_schema_description()

    @mcp.resource("graph://seed-status")
    def seed_status() -> str:
        """Startup seed execution status — shows which seeds ran, succeeded, or failed with error details."""
        status = get_seed_status()
        if not status:
            return json.dumps({
                "status": "pending",
                "message": "Seeds have not started yet or are still running.",
            }, indent=2)
        return json.dumps(status, indent=2)
