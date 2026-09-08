"""Unit tests for tools/execution.py — script execution, env building, security.

Mocks subprocess.run to avoid actually executing scripts.
"""

from __future__ import annotations

import json
import os
import sys
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

sys.path.insert(0, str(Path(__file__).parent / "src"))

from netops_api_navigator.config import Settings
from netops_api_navigator.tools.execution import (
    _build_env,
    _run_script,
    EXECUTION_TIMEOUT,
)


# ── Fixtures ────────────────────────────────────────────────────────


@pytest.fixture
def settings(tmp_path):
    """Settings with a temp script library containing a test script."""
    lib = tmp_path / "library"
    lib.mkdir()
    (lib / "hello.py").write_text('print("hello world")', encoding="utf-8")
    (lib / "hello.meta.json").write_text('{}', encoding="utf-8")
    return Settings(
        central_base_url="https://api.example.com",
        central_client_id="cid",
        central_client_secret="csec",
        glp_client_id="glp-cid",
        glp_client_secret="glp-csec",
        glp_base_url="https://glp.example.com",
        script_library_path=lib,
        graph_db_path=tmp_path / "graph.db",
        graph_ipc_socket=tmp_path / "graph.sock",
    )


# ── _build_env ──────────────────────────────────────────────────────


class TestBuildEnv:
    """Test environment variable construction for script subprocesses."""

    def test_central_credentials_in_env(self, settings):
        env = _build_env(settings)
        assert env["CENTRAL_BASE_URL"] == "https://api.example.com"
        assert env["CENTRAL_CLIENT_ID"] == "cid"
        assert env["CENTRAL_CLIENT_SECRET"] == "csec"

    def test_glp_credentials_in_env(self, settings):
        env = _build_env(settings)
        assert env["GLP_CLIENT_ID"] == "glp-cid"
        assert env["GLP_CLIENT_SECRET"] == "glp-csec"
        assert env["GLP_BASE_URL"] == "https://glp.example.com"

    def test_greenlake_aliases(self, settings):
        env = _build_env(settings)
        assert env["GREENLAKE_CLIENT_ID"] == "glp-cid"
        assert env["GREENLAKE_CLIENT_SECRET"] == "glp-csec"

    def test_graph_paths_in_env(self, settings):
        ipc_env = {
            "GRAPH_IPC_HOST": "127.0.0.1",
            "GRAPH_IPC_PORT": "54321",
            "GRAPH_IPC_TOKEN": "test-token",
        }
        env = _build_env(settings, ipc_env=ipc_env)
        assert "graph.db" in env["GRAPH_DB_PATH"]
        assert env["GRAPH_IPC_HOST"] == "127.0.0.1"
        assert env["GRAPH_IPC_PORT"] == "54321"
        assert env["GRAPH_IPC_TOKEN"] == "test-token"
        assert "GRAPH_IPC_SOCKET" not in env

    def test_stale_vars_removed(self, settings):
        """Generic vars like BASE_URL should be cleaned from env."""
        os.environ["BASE_URL"] = "stale"
        try:
            env = _build_env(settings)
            assert "BASE_URL" not in env
        finally:
            del os.environ["BASE_URL"]


# ── _run_script ──────────────────────────────────────────────────────


class TestRunScript:
    """Test script execution logic."""

    def test_successful_execution(self, settings):
        mock_result = MagicMock()
        mock_result.returncode = 0
        mock_result.stdout = "hello world\n"
        mock_result.stderr = ""

        with patch("netops_api_navigator.tools.execution.subprocess.run", return_value=mock_result):
            result = json.loads(_run_script(settings, "hello.py"))

        assert result["exit_code"] == 0
        assert "hello world" in result["stdout"]
        assert "duration_seconds" in result

    def test_script_not_found(self, settings):
        result = json.loads(_run_script(settings, "nonexistent.py"))
        assert "error" in result
        assert "not found" in result["error"].lower()

    def test_non_py_file_rejected(self, settings):
        (settings.script_library_path / "bad.sh").write_text("#!/bin/sh", encoding="utf-8")
        result = json.loads(_run_script(settings, "bad.sh"))
        assert "error" in result
        assert ".py" in result["error"]

    def test_path_traversal_blocked(self, settings):
        result = json.loads(_run_script(settings, "../../../etc/passwd"))
        # Should be caught either by filename validation or path resolution
        assert "error" in result

    def test_parameters_passed_as_cli_args(self, settings):
        mock_result = MagicMock()
        mock_result.returncode = 0
        mock_result.stdout = ""
        mock_result.stderr = ""

        with patch("netops_api_navigator.tools.execution.subprocess.run", return_value=mock_result) as mock_run:
            _run_script(settings, "hello.py", {"site": "NYC", "device": "SW01"})

        cmd = mock_run.call_args[0][0]
        assert cmd[0] == sys.executable
        assert "--site" in cmd
        assert "NYC" in cmd
        assert "--device" in cmd
        assert "SW01" in cmd

    def test_timeout_handling(self, settings):
        import subprocess

        with patch(
            "netops_api_navigator.tools.execution.subprocess.run",
            side_effect=subprocess.TimeoutExpired(cmd="python3 hello.py", timeout=300),
        ):
            result = json.loads(_run_script(settings, "hello.py"))

        assert "error" in result
        assert "timed out" in result["error"].lower()

    def test_stdout_truncation(self, settings):
        mock_result = MagicMock()
        mock_result.returncode = 0
        mock_result.stdout = "x" * 20000
        mock_result.stderr = ""

        with patch("netops_api_navigator.tools.execution.subprocess.run", return_value=mock_result):
            result = json.loads(_run_script(settings, "hello.py"))

        assert result["truncated"] is True
        assert len(result["stdout"]) == 10000

    def test_execution_uses_lib_as_cwd(self, settings):
        mock_result = MagicMock()
        mock_result.returncode = 0
        mock_result.stdout = ""
        mock_result.stderr = ""

        with patch("netops_api_navigator.tools.execution.subprocess.run", return_value=mock_result) as mock_run:
            _run_script(settings, "hello.py")

        call_kwargs = mock_run.call_args[1]
        assert call_kwargs["cwd"] == str(settings.script_library_path)
        assert call_kwargs["timeout"] == EXECUTION_TIMEOUT


# ── execute_script tool (registered) ─────────────────────────────────


class TestExecuteScriptTool:
    """Test the registered execute_script MCP tool."""

    def test_tool_registered(self, settings):
        from mcp.server.fastmcp import FastMCP
        from netops_api_navigator.tools.execution import register_execution_tools

        mcp = FastMCP("test")
        register_execution_tools(mcp, settings)

        tool_names = [t.name for t in mcp._tool_manager._tools.values()]
        assert "execute_script" in tool_names
