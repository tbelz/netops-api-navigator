"""Tool-description length guard.

Claude's tool catalogue rejects descriptions longer than 2100 characters
(measured after ``inspect.cleandoc``, which is what FastMCP uses to render
docstrings into the ``Tool.description`` field). Exceeding the cap causes
the entire MCP server to be silently dropped from the agent's tool list —
no error surfaces to the user.

This test enforces the cap on every registered tool so that an over-long
docstring fails CI before it ever ships.
"""

from __future__ import annotations

import inspect

import pytest

pytestmark = pytest.mark.unit

DESCRIPTION_CHAR_LIMIT = 2100


def _load_server_module(monkeypatch, tmp_path):
    from netops_api_navigator.config import Settings
    from netops_api_navigator.server import create_server

    return create_server(
        Settings(
            central_base_url="https://example.invalid",
            central_client_id="test",
            central_client_secret="test",
            script_library_path=tmp_path / "scripts",
            graph_db_path=tmp_path / "graph.db",
            docs_path=tmp_path / "docs",
            spec_cache_path=tmp_path / "cache",
            knowledge_release_repo="",
        ),
        validate_credentials=False,
        start_background=False,
    )


def test_all_tool_descriptions_under_limit(monkeypatch, tmp_path):
    """Every registered FastMCP tool must have a description ≤2100 chars."""
    runtime = _load_server_module(monkeypatch, tmp_path)
    try:
        tool_mgr = getattr(runtime.mcp, "_tool_manager", None)
        assert tool_mgr is not None, "FastMCP changed tool-manager attribute name"

        over_budget: list[tuple[str, int]] = []
        for tool in tool_mgr._tools.values():
            desc = (tool.description or "").strip()
            # FastMCP renders the docstring through inspect.cleandoc when
            # building the Tool object; mirror that here for parity in case a
            # future release stops trimming.
            cleaned = inspect.cleandoc(desc)
            if len(cleaned) > DESCRIPTION_CHAR_LIMIT:
                over_budget.append((tool.name, len(cleaned)))
    finally:
        runtime.close()

    assert not over_budget, (
        "The following tool descriptions exceed the "
        f"{DESCRIPTION_CHAR_LIMIT}-char cap (Claude silently drops the "
        "whole server when this is breached):\n"
        + "\n".join(f"  - {name}: {n} chars" for name, n in over_budget)
    )
