"""Regression tests guarding the API endpoint catalog embedded in MCP instructions.

Some MCP clients (notably some Claude Code builds) silently drop the server's
``instructions`` field. This test suite asserts the *server-side* contract:

1. ``build_instructions(api_tree=...)`` actually appends the tree to the text.
2. ``render_path_tree`` produces a non-empty, recognisable catalog when fed
   real-shaped rows, and a clearly-marked fallback when fed nothing — so a
   silent failure mode (empty catalog with no warning) is impossible.
3. The base instructions reference ``api://endpoint-catalog`` so an agent
   whose client dropped ``instructions`` still has a documented escape hatch
   via the resource (added on main in commit d792566).

These are pure unit tests — no graph DB, no network, no credentials.
"""

from __future__ import annotations

import pytest

from netops_api_navigator.api_tree import render_path_tree
from netops_api_navigator.instructions import (
    _API_TREE_HEADER,
    _BASE_INSTRUCTIONS,
    _READONLY_BANNER,
    build_instructions,
)

pytestmark = pytest.mark.unit


# --- build_instructions ----------------------------------------------------


def test_build_instructions_embeds_api_tree_verbatim():
    marker = "==DUMMY_TREE_MARKER_DO_NOT_FILTER=="
    out = build_instructions(read_only=False, api_tree=marker)
    assert marker in out, "build_instructions must embed the api_tree string verbatim"


def test_build_instructions_includes_separator_when_tree_given():
    out = build_instructions(read_only=False, api_tree="x")
    assert _API_TREE_HEADER in out


def test_build_instructions_no_tree_omits_separator():
    out = build_instructions(read_only=False, api_tree=None)
    assert _API_TREE_HEADER not in out


def test_build_instructions_empty_tree_omits_separator():
    # Falsy api_tree (empty string) must not insert an empty separator
    out = build_instructions(read_only=False, api_tree="")
    assert _API_TREE_HEADER not in out


def test_build_instructions_readonly_prepends_banner():
    out = build_instructions(read_only=True, api_tree="x")
    assert out.startswith(_READONLY_BANNER)
    assert "x" in out  # tree still present


def test_build_instructions_references_catalog_resource():
    # Defence in depth: even if a client drops `instructions`, the base
    # text mentions the resource fallback. Don't let that disappear.
    assert "api://endpoint-catalog" in _BASE_INSTRUCTIONS


# --- render_path_tree ------------------------------------------------------


_SAMPLE_ROWS = [
    {
        "method": "GET",
        "path": "network-monitoring/v1/aps",
        "category": "Monitoring",
        "deprecated": False,
    },
    {
        "method": "POST",
        "path": "network-config/v1alpha1/named-vlan",
        "category": "VLANs & Networks",
        "deprecated": False,
    },
    {
        "method": "DELETE",
        "path": "network-config/v1alpha1/named-vlan/{name}",
        "category": "VLANs & Networks",
        "deprecated": False,
    },
    {
        "method": "GET",
        "path": "network-monitoring/v1alpha1/aps",
        "category": "Monitoring",
        "deprecated": True,
    },
]


def test_render_path_tree_emits_header_with_counts():
    out = render_path_tree(_SAMPLE_ROWS, read_only=False)
    assert "API Endpoint Catalog" in out
    assert "endpoints" in out
    assert "categories" in out


def test_render_path_tree_lists_methods_and_paths():
    out = render_path_tree(_SAMPLE_ROWS, read_only=False)
    assert "Monitoring" in out
    assert "VLANs & Networks" in out
    # Every method should show up somewhere
    assert "GET" in out
    assert "POST" in out
    assert "DELETE" in out


def test_render_path_tree_readonly_filters_mutating_methods():
    out = render_path_tree(_SAMPLE_ROWS, read_only=True)
    assert "POST" not in out
    assert "DELETE" not in out
    # GETs must remain
    assert "GET" in out


def test_render_path_tree_empty_returns_explicit_fallback():
    # CRITICAL: empty input must produce an OBSERVABLE fallback, not silently
    # empty output. Otherwise a broken DB query would yield blank instructions
    # and the operator would never know.
    out = render_path_tree([], read_only=False)
    assert out.strip(), "fallback must not be empty whitespace"
    assert "unavailable" in out.lower() or "no endpoints" in out.lower(), (
        f"render_path_tree([]) must clearly signal absence; got: {out!r}"
    )
