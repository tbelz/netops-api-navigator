"""Regression test: the top-level server module must import cleanly.

The bug this guards against (caught manually 2026-04-25 in production):

    NameError: name 'shutil' is not defined

was introduced when ``shutil`` was removed from ``server.py`` during the
``knowledge_db`` extraction, even though ``server.py`` still calls
``shutil.copy2`` to seed ``central_helpers.py`` and ``_http_core.py``
into the script library at import time. No existing test imported
``server`` end-to-end (tests construct FastMCP locally and register
individual tool packs), so the broken module-level code path was only
discovered when the MCP server was launched by Claude Desktop.

This test imports ``hpe_networking_central_mcp.server`` with credentials
set and the network-touching boundaries stubbed, so any future
``NameError`` / ``ImportError`` introduced at module import time fails
the suite immediately.
"""

from __future__ import annotations

import importlib
import sys

import pytest

pytestmark = pytest.mark.unit


def test_server_module_imports_cleanly(monkeypatch, tmp_path):
    """Importing ``server`` must not raise — exercises every module-level statement."""
    for var in (
        "MCP_COMPILER_TOOLS",
        "MCP_COMPILER_DB_PATH",
        "MCP_COMPILER_AST_DB_PATH",
        "MCP_RUNTIME_HYDRATION",
        "MCP_KNOWLEDGE_PROJECTION",
        "KNOWLEDGE_PROJECTION",
    ):
        monkeypatch.delenv(var, raising=False)

    # Force the credential gate open with throwaway values.
    monkeypatch.setenv("CENTRAL_BASE_URL", "https://example.invalid")
    monkeypatch.setenv("CENTRAL_CLIENT_ID", "test")
    monkeypatch.setenv("CENTRAL_CLIENT_SECRET", "test")
    monkeypatch.setenv("SCRIPT_LIBRARY_PATH", str(tmp_path / "scripts"))
    monkeypatch.setenv("GRAPH_DB_PATH", str(tmp_path / "graph.db"))
    monkeypatch.setenv("KNOWLEDGE_RELEASE_REPO", "")  # skip download path

    # Stub out the OAuth2 round-trip in CentralClient.validate so the
    # import does not require network access.
    from hpe_networking_central_mcp import central_client as cc

    monkeypatch.setattr(cc.CentralClient, "validate", lambda self: None)
    monkeypatch.setattr(cc.GreenLakeClient, "validate", lambda self: None)

    # Ensure a fresh import — server.py runs all its setup at module scope.
    # Stop any IPC server from a previous import before evicting the module
    # to avoid leaking background threads into subsequent tests.
    _prev = sys.modules.get("hpe_networking_central_mcp.server")
    if _prev is not None:
        _ipc = getattr(_prev, "ipc_server", None)
        if _ipc is not None:
            _ipc.stop()
    sys.modules.pop("hpe_networking_central_mcp.server", None)

    module = importlib.import_module("hpe_networking_central_mcp.server")

    # Sanity: the names server.py is expected to export are bound.
    assert hasattr(module, "mcp")
    assert hasattr(module, "settings")
    assert hasattr(module, "logger")
    assert module._offline_mode is False
    assert module.client is not None


def test_server_module_imports_offline(monkeypatch, tmp_path):
    """Without credentials, the server must boot in discovery-only mode.

    Guards against regressions in the credential gate: prior to the
    discovery-only mode the server module called ``sys.exit(1)`` at import
    time when no credentials were configured, which made the offline
    deployment story impossible. The expected behaviour is now:

      * module imports cleanly (no ``SystemExit``);
      * ``_offline_mode`` is True and ``client`` is None;
      * the connected-only tools (``call_central_api``, ``call_greenlake_api``,
        ``execute_script``) are not registered on the FastMCP instance.
    """
    for var in (
        "CENTRAL_BASE_URL",
        "CENTRAL_CLIENT_ID",
        "CENTRAL_CLIENT_SECRET",
        "GREENLAKE_CLIENT_ID",
        "GREENLAKE_CLIENT_SECRET",
        "READ_ONLY",
        "MCP_COMPILER_TOOLS",
        "MCP_COMPILER_DB_PATH",
        "MCP_COMPILER_AST_DB_PATH",
        "MCP_RUNTIME_HYDRATION",
        "MCP_KNOWLEDGE_PROJECTION",
        "KNOWLEDGE_PROJECTION",
    ):
        monkeypatch.delenv(var, raising=False)
    monkeypatch.setenv("SCRIPT_LIBRARY_PATH", str(tmp_path / "scripts"))
    monkeypatch.setenv("GRAPH_DB_PATH", str(tmp_path / "graph.db"))
    monkeypatch.setenv("KNOWLEDGE_RELEASE_REPO", "")  # skip download path

    # Stop any IPC server from a previous import before evicting the module.
    _prev = sys.modules.get("hpe_networking_central_mcp.server")
    if _prev is not None:
        _ipc = getattr(_prev, "ipc_server", None)
        if _ipc is not None:
            _ipc.stop()
    sys.modules.pop("hpe_networking_central_mcp.server", None)

    module = importlib.import_module("hpe_networking_central_mcp.server")

    assert module._offline_mode is True
    assert module.client is None
    assert module.glp_client is None

    # FastMCP stores registered tools on the underlying tool manager; the
    # exact attribute name has been stable across recent FastMCP releases
    # but we look it up defensively so this test does not become a tight
    # coupling.
    tool_mgr = getattr(module.mcp, "_tool_manager", None)
    assert tool_mgr is not None, "FastMCP changed tool-manager attribute name"
    tool_names = {t.name for t in tool_mgr._tools.values()}
    for connected_only in ("call_central_api", "call_greenlake_api", "execute_script"):
        assert connected_only not in tool_names, (
            f"{connected_only} must not be registered in discovery-only mode"
        )
    # Discovery tools must still be present.
    for must_have in ("query_graph", "write_graph", "list_scripts", "save_script"):
        assert must_have in tool_names, f"{must_have} missing in discovery-only mode"
    for compiler_only in (
        "find_api_endpoints",
        "get_api_endpoint_context",
        "get_api_schema_context",
        "get_openapi_source_detail",
        "get_compiler_graph_health",
    ):
        assert compiler_only not in tool_names
    for hydration_tool in (
        "get_runtime_hydration_status",
        "list_runtime_hydration_candidates",
        "hydrate_runtime_graph",
        "hydrate_runtime_endpoint",
        "list_runtime_observations",
        "get_runtime_observation",
        "get_runtime_hydration_state",
        "list_runtime_hydration_providers",
        "plan_runtime_hydration",
        "materialize_runtime_facts",
        "list_runtime_facts",
        "get_runtime_fact",
        "promote_runtime_entities",
        "list_runtime_entities",
        "get_runtime_entity",
    ):
        assert hydration_tool not in tool_names


def test_server_registers_compiler_tools_when_enabled(monkeypatch, tmp_path):
    """Compiler provenance/health tools are opt-in and register without creds."""
    for var in (
        "CENTRAL_BASE_URL",
        "CENTRAL_CLIENT_ID",
        "CENTRAL_CLIENT_SECRET",
        "GREENLAKE_CLIENT_ID",
        "GREENLAKE_CLIENT_SECRET",
        "READ_ONLY",
    ):
        monkeypatch.delenv(var, raising=False)
    monkeypatch.setenv("SCRIPT_LIBRARY_PATH", str(tmp_path / "scripts"))
    monkeypatch.setenv("GRAPH_DB_PATH", str(tmp_path / "graph.db"))
    monkeypatch.setenv("KNOWLEDGE_RELEASE_REPO", "")
    monkeypatch.setenv("MCP_COMPILER_TOOLS", "true")
    monkeypatch.setenv("MCP_COMPILER_DB_PATH", str(tmp_path / "knowledge_db_compiler"))
    monkeypatch.setenv("MCP_COMPILER_AST_DB_PATH", str(tmp_path / "knowledge_db_ast"))

    _prev = sys.modules.get("hpe_networking_central_mcp.server")
    if _prev is not None:
        _ipc = getattr(_prev, "ipc_server", None)
        if _ipc is not None:
            _ipc.stop()
    sys.modules.pop("hpe_networking_central_mcp.server", None)

    module = importlib.import_module("hpe_networking_central_mcp.server")

    tool_mgr = getattr(module.mcp, "_tool_manager", None)
    assert tool_mgr is not None, "FastMCP changed tool-manager attribute name"
    tool_names = {t.name for t in tool_mgr._tools.values()}
    for compiler_tool in ("get_openapi_source_detail", "get_compiler_graph_health"):
        assert compiler_tool in tool_names
    for removed_tool in (
        "find_api_endpoints",
        "get_api_endpoint_context",
        "get_api_schema_context",
    ):
        assert removed_tool not in tool_names


def test_server_registers_runtime_hydration_foundation_when_enabled(monkeypatch, tmp_path):
    """Runtime hydration is opt-in and starts as a read-only foundation."""
    for var in (
        "CENTRAL_BASE_URL",
        "CENTRAL_CLIENT_ID",
        "CENTRAL_CLIENT_SECRET",
        "GREENLAKE_CLIENT_ID",
        "GREENLAKE_CLIENT_SECRET",
        "READ_ONLY",
        "MCP_COMPILER_TOOLS",
    ):
        monkeypatch.delenv(var, raising=False)
    monkeypatch.setenv("SCRIPT_LIBRARY_PATH", str(tmp_path / "scripts"))
    monkeypatch.setenv("GRAPH_DB_PATH", str(tmp_path / "graph.db"))
    monkeypatch.setenv("KNOWLEDGE_RELEASE_REPO", "")
    monkeypatch.setenv("MCP_RUNTIME_HYDRATION", "true")

    _prev = sys.modules.get("hpe_networking_central_mcp.server")
    if _prev is not None:
        _ipc = getattr(_prev, "ipc_server", None)
        if _ipc is not None:
            _ipc.stop()
    sys.modules.pop("hpe_networking_central_mcp.server", None)

    module = importlib.import_module("hpe_networking_central_mcp.server")

    tool_mgr = getattr(module.mcp, "_tool_manager", None)
    assert tool_mgr is not None, "FastMCP changed tool-manager attribute name"
    tool_names = {t.name for t in tool_mgr._tools.values()}
    assert "get_runtime_hydration_status" in tool_names
    assert "list_runtime_hydration_candidates" in tool_names
    assert "hydrate_runtime_graph" in tool_names
    assert "hydrate_runtime_endpoint" in tool_names
    assert "list_runtime_observations" in tool_names
    assert "get_runtime_observation" in tool_names
    assert "get_runtime_hydration_state" in tool_names
    assert "list_runtime_hydration_providers" in tool_names
    assert "plan_runtime_hydration" in tool_names
    assert "materialize_runtime_facts" in tool_names
    assert "list_runtime_facts" in tool_names
    assert "get_runtime_fact" in tool_names
    assert "promote_runtime_entities" in tool_names
    assert "list_runtime_entities" in tool_names
    assert "get_runtime_entity" in tool_names
