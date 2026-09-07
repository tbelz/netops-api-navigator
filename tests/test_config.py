"""Tests for environment-backed settings parsing."""

from __future__ import annotations

from pathlib import Path

import pytest

from hpe_networking_central_mcp.config import Settings, load_settings

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


def test_native_defaults_are_user_scoped_and_enable_knowledge_download(monkeypatch):
    for name in (
        "GRAPH_DB_PATH",
        "SCRIPT_LIBRARY_PATH",
        "DOCS_PATH",
        "SPEC_CACHE_DIR",
        "KNOWLEDGE_RELEASE_REPO",
    ):
        monkeypatch.delenv(name, raising=False)

    settings = load_settings()

    assert not str(settings.graph_db_path).startswith("/data/")
    assert not str(settings.script_library_path).startswith("/scripts/")
    assert settings.knowledge_release_repo == "tbelz/hpe-networking-central-mcp"


def test_workshop_profile_forces_fail_closed_settings(monkeypatch):
    monkeypatch.setenv("MCP_PROFILE", "workshop")
    monkeypatch.setenv("READ_ONLY", "false")
    monkeypatch.setenv("MCP_COMPILER_TOOLS", "true")
    monkeypatch.setenv("MCP_RUNTIME_HYDRATION", "true")

    settings = load_settings()

    assert settings.workshop_mode is True
    assert settings.read_only is True
    assert settings.compiler_tools is False
    assert settings.runtime_hydration is False


def test_knowledge_release_pin_is_loaded(monkeypatch):
    monkeypatch.setenv("KNOWLEDGE_RELEASE_TAG", "knowledge-db-pinned")
    monkeypatch.setenv("KNOWLEDGE_ASSET_SHA256", "a" * 64)

    settings = load_settings()

    assert settings.knowledge_release_tag == "knowledge-db-pinned"
    assert settings.knowledge_asset_sha256 == "a" * 64


def test_knowledge_digest_is_normalized_and_validated():
    settings = Settings(knowledge_asset_sha256=f"sha256:{'A' * 64}")
    assert settings.knowledge_asset_sha256 == "a" * 64

    with pytest.raises(ValueError, match="64-character"):
        Settings(knowledge_asset_sha256="not-a-digest")
