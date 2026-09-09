"""Unit tests for the path-tree renderer (api_tree.py)."""

from __future__ import annotations

from netops_api_navigator.api_tree import render_path_tree


def _ep(method: str, path: str, category: str = "Test", deprecated: bool = False) -> dict:
    return {
        "method": method,
        "path": path,
        "category": category,
        "deprecated": deprecated,
    }


# ── Empty / fallback ─────────────────────────────────────────────────


def test_empty_endpoints_returns_fallback():
    text = render_path_tree([])
    assert "unavailable" in text.lower()
    # Fallback must NOT steer callers toward retired tools.
    assert "unified_search" not in text
    assert "get_api_endpoint_detail" not in text
    assert "get_api_endpoint_glossary" not in text
    # Should mention the still-working alternative.
    assert "query_graph" in text


def test_read_only_filters_to_empty_returns_fallback():
    eps = [_ep("POST", "/x"), _ep("DELETE", "/y")]
    text = render_path_tree(eps, read_only=True)
    assert "unavailable" in text.lower()


# ── Basic structure ──────────────────────────────────────────────────


def test_renders_category_header_with_count():
    eps = [_ep("GET", "/foo"), _ep("GET", "/bar")]
    text = render_path_tree(eps)
    assert "## Test (2)" in text


def test_renders_top_summary_line():
    eps = [_ep("GET", "/foo", category="A"), _ep("GET", "/bar", category="B")]
    text = render_path_tree(eps)
    # 2 endpoints, 2 categories
    assert "API Endpoint Catalog (2 endpoints, 2 categories)" in text


def test_categories_sorted_alphabetically():
    eps = [
        _ep("GET", "/a", category="Zebra"),
        _ep("GET", "/b", category="Apple"),
        _ep("GET", "/c", category="Mango"),
    ]
    text = render_path_tree(eps)
    apple_pos = text.index("## Apple")
    mango_pos = text.index("## Mango")
    zebra_pos = text.index("## Zebra")
    assert apple_pos < mango_pos < zebra_pos


def test_uncategorized_fallback():
    eps = [_ep("GET", "/x", category="")]
    text = render_path_tree(eps)
    assert "(uncategorized)" in text


# ── Prefix dedup ─────────────────────────────────────────────────────


def test_common_prefix_collapses():
    eps = [
        _ep("GET", "/network-monitoring/v1/aps"),
        _ep("GET", "/network-monitoring/v1/aps/{serial}"),
        _ep("GET", "/network-monitoring/v1/aps/{serial}/ports"),
    ]
    text = render_path_tree(eps)
    # Each prefix segment should appear exactly once
    assert text.count("network-monitoring/") == 1
    assert text.count("v1/") == 1
    # 'aps' is BOTH a leaf (GET /aps) and a branch (GET /aps/{serial}).
    # The renderer prints the branch form once and the leaf form once.
    assert text.count("aps  [GET]") == 1
    assert "{serial}  [GET]" in text


def test_methods_aggregated_per_path():
    eps = [
        _ep("GET", "/items/{id}"),
        _ep("PUT", "/items/{id}"),
        _ep("DELETE", "/items/{id}"),
    ]
    text = render_path_tree(eps)
    # Methods appear sorted, joined by '|'
    assert "{id}  [DELETE|GET|PUT]" in text


def test_deprecated_marked_with_bang():
    eps = [_ep("GET", "/old", deprecated=True)]
    text = render_path_tree(eps)
    assert "[GET]!" in text


def test_deprecated_propagates_when_any_method_deprecated():
    eps = [
        _ep("GET", "/items"),  # not deprecated
        _ep("POST", "/items", deprecated=True),
    ]
    text = render_path_tree(eps)
    # path counts as deprecated because at least one method is
    assert "[GET|POST]!" in text


# ── Read-only filtering ──────────────────────────────────────────────


def test_read_only_excludes_non_get():
    eps = [
        _ep("GET", "/safe"),
        _ep("POST", "/danger"),
        _ep("DELETE", "/danger"),
    ]
    text = render_path_tree(eps, read_only=True)
    assert "/safe" in text or "safe  [GET]" in text
    assert "danger" not in text


def test_read_only_drops_empty_categories():
    eps = [
        _ep("GET", "/a", category="Reads"),
        _ep("POST", "/b", category="Writes"),
    ]
    text = render_path_tree(eps, read_only=True)
    assert "## Reads" in text
    assert "## Writes" not in text


# ── Sorting within category ──────────────────────────────────────────


def test_paths_sorted_lexicographically_within_category():
    eps = [
        _ep("GET", "/zoo"),
        _ep("GET", "/apple"),
        _ep("GET", "/mango"),
    ]
    text = render_path_tree(eps)
    apple_pos = text.index("apple")
    mango_pos = text.index("mango")
    zoo_pos = text.index("zoo")
    assert apple_pos < mango_pos < zoo_pos


# ── Trailing newline / shape ─────────────────────────────────────────


def test_output_ends_with_single_newline():
    eps = [_ep("GET", "/x")]
    text = render_path_tree(eps)
    assert text.endswith("\n")
    assert not text.endswith("\n\n")


# ── Mixed scenario (sanity) ──────────────────────────────────────────


def test_mixed_realistic_scenario():
    eps = [
        _ep("GET", "/network-monitoring/v1/aps", category="Monitoring"),
        _ep("GET", "/network-monitoring/v1/aps/{serial}", category="Monitoring"),
        _ep("DELETE", "/network-config/v1alpha1/vlans/{id}", category="VLANs"),
        _ep("GET", "/network-config/v1alpha1/vlans/{id}", category="VLANs"),
        _ep("PUT", "/network-config/v1alpha1/vlans/{id}", category="VLANs", deprecated=True),
    ]
    text = render_path_tree(eps)
    assert "## Monitoring (2)" in text
    assert "## VLANs (3)" in text
    assert "{serial}  [GET]" in text
    assert "{id}  [DELETE|GET|PUT]!" in text  # deprecated propagated
