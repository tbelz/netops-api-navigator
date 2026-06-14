"""Tests for the opt-in runtime hydration shell."""

from __future__ import annotations

import json

import pytest
from mcp.server.fastmcp import FastMCP

from hpe_networking_central_mcp.config import Settings
from hpe_networking_central_mcp.tools.hydration import register_runtime_hydration_tools

pytestmark = pytest.mark.unit


def _make_tools() -> dict[str, object]:
    mcp = FastMCP("test-runtime-hydration")
    register_runtime_hydration_tools(mcp, Settings(runtime_hydration=True))
    return {tool.name: tool.fn for tool in mcp._tool_manager._tools.values()}


def test_runtime_hydration_shell_tool_surface() -> None:
    tools = _make_tools()

    assert set(tools) == {"get_runtime_hydration_status"}


def test_runtime_hydration_status_is_honest_about_unimplemented_capabilities() -> None:
    tools = _make_tools()

    parsed = json.loads(tools["get_runtime_hydration_status"]())

    assert parsed["enabled"] is True
    assert parsed["stage"] == "flag-shell"
    assert parsed["capabilities"] == []
    assert parsed["implemented"] == {
        "generic_endpoint_hydration": False,
        "observation_persistence": False,
        "materialization": False,
    }
