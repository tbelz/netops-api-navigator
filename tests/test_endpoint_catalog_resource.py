"""Regression tests for the ``api://endpoint-catalog`` MCP resource.

Some MCP clients (notably Claude Code) drop the server's ``instructions``
field, so the catalog must also be reachable via a fetchable resource.
Main introduced ``register_api_catalog_resource`` for that purpose; these
tests pin the contract:

1. The resource is listed under its exact ``api://endpoint-catalog`` URI.
2. Reading it returns the rendered catalog text from ``render_path_tree``.
3. When the graph DB is unavailable the resource still resolves with a
   user-readable message instead of raising — so the client sees the
   resource exist either way.
4. ``READ_ONLY=True`` causes the catalog text to filter out mutating
   methods, matching the read-only filter applied to instructions.

Pure unit tests; no real graph DB, no network.
"""

from __future__ import annotations

import asyncio
from dataclasses import dataclass
from typing import Any

import pytest
from mcp.server.fastmcp import FastMCP

from netops_api_navigator.config import Settings
from netops_api_navigator.resources.docs import register_api_catalog_resource

pytestmark = pytest.mark.unit


_SAMPLE_ROWS = [
    {"method": "GET", "path": "monitoring/v1/devices", "category": "Monitoring", "deprecated": False},
    {"method": "POST", "path": "monitoring/v1/devices", "category": "Monitoring", "deprecated": False},
    {"method": "DELETE", "path": "configuration/v1/groups/{group}", "category": "Configuration", "deprecated": False},
    {"method": "GET", "path": "configuration/v1/legacy", "category": "Configuration", "deprecated": True},
]


@dataclass
class _StubGraphManager:
    """Minimal graph-manager stand-in that returns a canned row set."""

    rows: list[dict[str, Any]]
    available: bool = True
    raise_on_query: Exception | None = None

    @property
    def is_available(self) -> bool:
        return self.available

    def query(self, *_args: Any, **_kwargs: Any) -> list[dict[str, Any]]:
        if self.raise_on_query is not None:
            raise self.raise_on_query
        return self.rows


def _build_server(graph_manager: _StubGraphManager | None, *, read_only: bool = False) -> FastMCP:
    mcp = FastMCP("test-endpoint-catalog")
    settings = Settings(read_only=read_only)
    register_api_catalog_resource(mcp, settings, graph_manager)
    return mcp


def _read(mcp: FastMCP, uri: str) -> str:
    contents = asyncio.run(mcp.read_resource(uri))
    # FastMCP returns an iterable of ReadResourceContents; concatenate text.
    return "".join(c.content for c in contents if isinstance(c.content, str))


def test_resource_is_listed_under_exact_uri():
    mcp = _build_server(_StubGraphManager(rows=_SAMPLE_ROWS))

    resources = asyncio.run(mcp.list_resources())
    uris = [str(r.uri) for r in resources]

    assert "api://endpoint-catalog" in uris, (
        f"api://endpoint-catalog must be exposed as an MCP resource so clients "
        f"that drop the instructions field can still fetch it. Saw: {uris}"
    )


def test_docs_scheme_alias_is_listed():
    """Claude Code (and similar) may filter unknown URI schemes from the
    resource picker, so the catalog is also published under the docs://
    scheme used by every other documentation resource."""
    mcp = _build_server(_StubGraphManager(rows=_SAMPLE_ROWS))

    resources = asyncio.run(mcp.list_resources())
    uris = [str(r.uri) for r in resources]

    assert "docs://endpoint-catalog" in uris, (
        f"docs://endpoint-catalog alias must be registered for clients that "
        f"hide non-standard URI schemes. Saw: {uris}"
    )


def test_docs_alias_returns_same_content_as_api_uri():
    mcp = _build_server(_StubGraphManager(rows=_SAMPLE_ROWS))

    api_text = _read(mcp, "api://endpoint-catalog")
    docs_text = _read(mcp, "docs://endpoint-catalog")

    assert api_text == docs_text
    assert "API Endpoint Catalog" in docs_text


def test_resource_advertises_text_mime_type():
    mcp = _build_server(_StubGraphManager(rows=_SAMPLE_ROWS))

    resources = asyncio.run(mcp.list_resources())
    catalog = next(r for r in resources if str(r.uri) == "api://endpoint-catalog")

    assert catalog.mimeType == "text/plain"


def test_resource_has_non_empty_description():
    """Without a description, some clients hide the resource from the picker."""
    mcp = _build_server(_StubGraphManager(rows=_SAMPLE_ROWS))

    resources = asyncio.run(mcp.list_resources())
    catalog = next(r for r in resources if str(r.uri) == "api://endpoint-catalog")

    assert catalog.description and catalog.description.strip(), (
        "api://endpoint-catalog must carry a description so MCP clients can "
        "render it in their resource pickers."
    )


def test_reading_resource_returns_rendered_catalog():
    mcp = _build_server(_StubGraphManager(rows=_SAMPLE_ROWS))

    text = _read(mcp, "api://endpoint-catalog")

    # Header from render_path_tree
    assert "API Endpoint Catalog" in text
    # Categories present
    assert "## Monitoring" in text
    assert "## Configuration" in text
    # Methods listed
    assert "[GET|POST]" in text or ("[GET]" in text and "[POST]" in text)
    # Deprecated marker preserved
    assert "!" in text


def test_reading_resource_when_graph_unavailable_returns_message_not_error():
    mcp = _build_server(_StubGraphManager(rows=[], available=False))

    text = _read(mcp, "api://endpoint-catalog")

    assert "unavailable" in text.lower()


def test_reading_resource_when_graph_manager_is_none_returns_message():
    mcp = _build_server(None)

    text = _read(mcp, "api://endpoint-catalog")

    assert "unavailable" in text.lower()


def test_reading_resource_when_query_raises_returns_error_message():
    boom = RuntimeError("ladybug connection lost")
    mcp = _build_server(_StubGraphManager(rows=[], raise_on_query=boom))

    text = _read(mcp, "api://endpoint-catalog")

    assert "unavailable" in text.lower()
    # Internal exception text must NOT leak to the resource consumer.
    assert "ladybug connection lost" not in text


def test_read_only_filters_mutating_methods_from_catalog():
    mcp = _build_server(_StubGraphManager(rows=_SAMPLE_ROWS), read_only=True)

    text = _read(mcp, "api://endpoint-catalog")

    # GET endpoint must remain (rendered as nested path tree)
    assert "monitoring/" in text and "devices  [GET]" in text
    # POST + DELETE entries must be filtered out
    assert "POST" not in text
    assert "DELETE" not in text
