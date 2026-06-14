"""Tests for environment-backed settings parsing."""

from __future__ import annotations

from pathlib import Path

import pytest

from hpe_networking_central_mcp.config import load_settings

pytestmark = pytest.mark.unit


def test_compiler_tool_settings_default_to_disabled_sidecars(monkeypatch, tmp_path):
    graph_db_path = tmp_path / "graph_db"
    monkeypatch.setenv("GRAPH_DB_PATH", str(graph_db_path))
    monkeypatch.delenv("MCP_COMPILER_TOOLS", raising=False)
    monkeypatch.delenv("MCP_RUNTIME_HYDRATION", raising=False)
    monkeypatch.delenv("MCP_COMPILER_DB_PATH", raising=False)
    monkeypatch.delenv("MCP_COMPILER_AST_DB_PATH", raising=False)
    monkeypatch.delenv("MCP_KNOWLEDGE_PROJECTION", raising=False)
    monkeypatch.delenv("KNOWLEDGE_PROJECTION", raising=False)

    settings = load_settings()

    assert settings.compiler_tools is False
    assert settings.runtime_hydration is False
    assert settings.compiler_db_path == tmp_path / "knowledge_db_compiler"
    assert settings.compiler_ast_db_path == tmp_path / "knowledge_db_ast"


def test_v2_projection_keeps_compiler_db_as_sidecar_by_default(monkeypatch, tmp_path):
    graph_db_path = tmp_path / "graph_db"
    monkeypatch.setenv("GRAPH_DB_PATH", str(graph_db_path))
    monkeypatch.setenv("MCP_KNOWLEDGE_PROJECTION", "v2")
    monkeypatch.delenv("MCP_COMPILER_DB_PATH", raising=False)

    settings = load_settings()

    assert settings.knowledge_projection == "v2"
    assert settings.compiler_db_path == tmp_path / "knowledge_db_compiler"


def test_compiler_tool_settings_accept_explicit_overrides(monkeypatch, tmp_path):
    monkeypatch.setenv("MCP_COMPILER_TOOLS", "true")
    monkeypatch.setenv("MCP_COMPILER_DB_PATH", str(tmp_path / "compiler"))
    monkeypatch.setenv("MCP_COMPILER_AST_DB_PATH", str(tmp_path / "ast"))

    settings = load_settings()

    assert settings.compiler_tools is True
    assert settings.compiler_db_path == Path(tmp_path / "compiler")
    assert settings.compiler_ast_db_path == Path(tmp_path / "ast")


def test_runtime_hydration_defaults_to_disabled(monkeypatch):
    monkeypatch.delenv("MCP_RUNTIME_HYDRATION", raising=False)

    settings = load_settings()

    assert settings.runtime_hydration is False


def test_runtime_hydration_accepts_truthy_flag(monkeypatch):
    monkeypatch.setenv("MCP_RUNTIME_HYDRATION", "true")

    settings = load_settings()

    assert settings.runtime_hydration is True
