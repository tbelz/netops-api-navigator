"""Unit tests for tools/scripts.py — script library management.

Uses an in-memory LadybugDB graph. Tests save_script, list_scripts,
get_script_content, validate_filename, Cypher escape helpers, and
sync_seeds_to_graph.
"""

from __future__ import annotations

import json
import sys
import time
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

sys.path.insert(0, str(Path(__file__).parent / "src"))

from netops_api_navigator.graph.manager import GraphManager
from netops_api_navigator.config import Settings
from netops_api_navigator.tools.scripts import (
    _cypher_escape,
    _cypher_string_list,
    _validate_filename,
    sync_seeds_to_graph,
)


# ── Fixtures ────────────────────────────────────────────────────────


@pytest.fixture(scope="module")
def gm(tmp_path_factory):
    """Create an in-memory GraphManager with schema."""
    db_path = tmp_path_factory.mktemp("scripts_db") / "test.db"
    gm = GraphManager(db_path)
    gm.initialize()
    return gm


@pytest.fixture
def settings(tmp_path):
    """Settings with temp script library."""
    lib = tmp_path / "library"
    lib.mkdir()
    return Settings(
        central_base_url="https://test.example.com",
        central_client_id="cid",
        central_client_secret="csec",
        script_library_path=lib,
    )


@pytest.fixture
def tools(gm, settings):
    """Register script tools and return tool function dict."""
    from mcp.server.fastmcp import FastMCP
    from netops_api_navigator.tools.scripts import register_script_tools

    mcp = FastMCP("test")
    register_script_tools(mcp, settings, gm)

    tool_map = {}
    for tool in mcp._tool_manager._tools.values():
        tool_map[tool.name] = tool.fn
    return tool_map


# ── Helper functions ─────────────────────────────────────────────────


class TestCypherEscape:
    """Test _cypher_escape string escaping."""

    def test_single_quote(self):
        assert _cypher_escape("it's") == "it\\'s"

    def test_backslash(self):
        assert _cypher_escape("a\\b") == "a\\\\b"

    def test_combined(self):
        assert _cypher_escape("it's a\\b") == "it\\'s a\\\\b"

    def test_empty_string(self):
        assert _cypher_escape("") == ""


class TestCypherStringList:
    """Test _cypher_string_list literal builder."""

    def test_empty_list(self):
        result = _cypher_string_list([])
        assert result == "CAST([] AS STRING[])"

    def test_single_item(self):
        result = _cypher_string_list(["foo"])
        assert result == "['foo']"

    def test_multiple_items(self):
        result = _cypher_string_list(["a", "b", "c"])
        assert result == "['a', 'b', 'c']"

    def test_escapes_quotes(self):
        result = _cypher_string_list(["it's"])
        assert result == "['it\\'s']"


class TestValidateFilename:
    """Test _validate_filename validation."""

    def test_valid_filename(self):
        assert _validate_filename("my_script.py") is None

    def test_valid_with_hyphens(self):
        assert _validate_filename("my-script.py") is None

    def test_empty(self):
        assert _validate_filename("") is not None

    def test_no_py_extension(self):
        assert _validate_filename("script.sh") is not None

    def test_path_separator(self):
        assert _validate_filename("../evil.py") is not None

    def test_backslash_path(self):
        assert _validate_filename("..\\evil.py") is not None

    def test_special_chars(self):
        assert _validate_filename("sc ript.py") is not None


# ── save_script / list_scripts / get_script_content ──────────────────


class TestScriptCRUD:
    """Test save, list, and get operations."""

    def test_save_and_list(self, tools, gm):
        # Save
        result = json.loads(tools["save_script"](
            filename="test_hello.py",
            content='print("hello")',
            description="A test script",
            tags=["test"],
        ))
        assert result["status"] == "saved"
        assert result["filename"] == "test_hello.py"

        # List
        result = json.loads(tools["list_scripts"]())
        filenames = [s["filename"] for s in result["scripts"]]
        assert "test_hello.py" in filenames

    def test_get_script_content(self, tools):
        result = json.loads(tools["get_script_content"](filename="test_hello.py"))
        assert result["filename"] == "test_hello.py"
        assert 'print("hello")' in result["content"]

    def test_get_script_not_found(self, tools):
        result = json.loads(tools["get_script_content"](filename="no_such.py"))
        assert "error" in result

    def test_list_by_tag(self, tools):
        result = json.loads(tools["list_scripts"](tag="test"))
        assert all("test" in s["tags"] for s in result["scripts"])

    def test_save_overwrite_preserves_run_meta(self, tools, gm):
        """Overwriting a script should preserve last_run metadata."""
        # Set some run metadata
        gm.execute(
            "MATCH (s:Script {filename: 'test_hello.py'}) "
            "SET s.last_run = '2025-01-01T00:00:00Z', s.last_exit_code = 0"
        )

        # Overwrite the script
        tools["save_script"](
            filename="test_hello.py",
            content='print("updated")',
            description="Updated script",
            tags=["test", "updated"],
        )

        # Verify run metadata preserved
        rows = gm.query(
            "MATCH (s:Script {filename: 'test_hello.py'}) "
            "RETURN s.last_run, s.last_exit_code",
            read_only=True,
        )
        assert rows[0]["s.last_run"] == "2025-01-01T00:00:00Z"
        assert rows[0]["s.last_exit_code"] == 0

    def test_save_invalid_filename(self, tools):
        result = json.loads(tools["save_script"](
            filename="../evil.py",
            content="import os",
            description="bad",
            tags=[],
        ))
        assert "error" in result

    def test_save_writes_to_disk(self, tools, settings):
        tools["save_script"](
            filename="disk_test.py",
            content='print("disk")',
            description="Disk test",
            tags=[],
        )
        assert (settings.script_library_path / "disk_test.py").exists()


# ── sync_seeds_to_graph ─────────────────────────────────────────────


class TestSyncSeeds:
    """Test sync_seeds_to_graph copies seeds into graph and disk."""

    def test_sync_copies_seeds(self, gm, tmp_path):
        seeds_dir = tmp_path / "seeds"
        seeds_dir.mkdir()
        lib_dir = tmp_path / "lib"

        # Create a seed file
        seed = seeds_dir / "my_seed.py"
        seed.write_text('print("seed")', encoding="utf-8")

        # Create metadata
        meta = seeds_dir / "my_seed.meta.json"
        meta.write_text(json.dumps({
            "description": "A test seed",
            "tags": ["test"],
            "parameters": [{"name": "site", "type": "string", "description": "Site name"}],
        }), encoding="utf-8")

        # Also create __init__.py which should be skipped entirely
        (seeds_dir / "__init__.py").write_text("", encoding="utf-8")

        sync_seeds_to_graph(gm, seeds_dir, lib_dir)

        # Verify disk copy (only non-__init__ .py files)
        assert (lib_dir / "my_seed.py").exists()
        assert not (lib_dir / "__init__.py").exists()

        # Verify graph node
        rows = gm.query(
            "MATCH (s:Script {filename: 'my_seed.py'}) RETURN s.description, s.tags",
            read_only=True,
        )
        assert len(rows) == 1
        assert rows[0]["s.description"] == "A test seed"

    def test_sync_skips_private_modules(self, gm, tmp_path):
        seeds_dir = tmp_path / "seeds2"
        seeds_dir.mkdir()
        lib_dir = tmp_path / "lib2"

        # Create a private helper
        (seeds_dir / "_helpers.py").write_text("# private", encoding="utf-8")

        sync_seeds_to_graph(gm, seeds_dir, lib_dir)

        # Should be on disk but NOT in graph
        assert (lib_dir / "_helpers.py").exists()
        rows = gm.query(
            "MATCH (s:Script {filename: '_helpers.py'}) RETURN s",
            read_only=True,
        )
        assert len(rows) == 0
