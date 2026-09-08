"""Render the API endpoint catalog as a category-grouped path-tree.

The tree is embedded in the MCP server's system instructions so the agent
always sees the full set of available endpoints without needing to call a
search tool first. Common path prefixes are collapsed into nested indentation
to keep the representation compact (~14–16k tokens for ~2,100 endpoints).

Example output (excerpt):

    ## Monitoring (314)
      network-monitoring/
        v1/
          aps  [GET]
            {serial-number}  [GET]
              cpu-utilization-trends  [GET]
              ports  [GET]
                {port-index}/
                  crc-trends  [GET]

Deprecated endpoints are marked with a trailing ``!`` after the methods.
"""

from __future__ import annotations

from collections import defaultdict
from typing import Iterable

# Endpoint dict keys expected — matches the column names returned by the
# graph query in server.py.
_K_METHOD = "method"
_K_PATH = "path"
_K_CATEGORY = "category"
_K_DEPRECATED = "deprecated"


def _split_path(path: str) -> list[str]:
    """Split an API path into segments, dropping empty parts from leading '/'."""
    return [p for p in path.split("/") if p]


def _render_category_tree(endpoints: list[dict]) -> list[str]:
    """Render a path-tree for the endpoints of a single category.

    Endpoints sharing path prefixes are folded into a nested indentation
    structure. Each leaf line shows the path segment followed by a
    bracketed list of HTTP methods (and a ``!`` suffix if any of them is
    deprecated).
    """
    # Collect all (methods, deprecated-flag) per path
    path_methods: dict[str, list[str]] = defaultdict(list)
    path_deprecated: dict[str, bool] = {}
    for ep in endpoints:
        p = ep[_K_PATH]
        m = ep[_K_METHOD].upper()
        path_methods[p].append(m)
        # Path counts as deprecated if ANY method on it is deprecated
        path_deprecated[p] = path_deprecated.get(p, False) or bool(ep.get(_K_DEPRECATED))

    lines: list[str] = []
    printed: set[str] = set()  # keys = "/".join(parts[:depth+1]) already emitted
    prev_parts: list[str] = []

    for path in sorted(path_methods):
        parts = _split_path(path)
        if not parts:
            # Defensive: skip pathological empty paths
            continue

        methods = "|".join(sorted(set(path_methods[path])))
        dep = "!" if path_deprecated[path] else ""

        # Find common prefix depth with previous path
        common = 0
        for a, b in zip(prev_parts, parts):
            if a == b:
                common += 1
            else:
                break

        for depth, part in enumerate(parts):
            key = "/".join(parts[: depth + 1])
            indent = "  " * depth
            is_leaf = depth == len(parts) - 1

            if depth < common and key in printed and not is_leaf:
                # Branch already printed in a previous iteration — skip
                continue

            if is_leaf:
                # Always emit the leaf even if a same-named branch was
                # printed before (e.g. the leaf has methods to show)
                lines.append(f"{indent}{part}  [{methods}]{dep}")
            else:
                if key not in printed:
                    lines.append(f"{indent}{part}/")
            printed.add(key)

        prev_parts = parts

    return lines


def render_path_tree(
    endpoints: Iterable[dict],
    *,
    read_only: bool = False,
) -> str:
    """Render the full API catalog as a category-grouped path-tree.

    Args:
        endpoints: Iterable of endpoint dicts with keys ``method``, ``path``,
            ``category``, and optional ``deprecated``.
        read_only: When True, omit endpoints whose method is not ``GET``.
            Categories that become empty as a result are dropped.

    Returns:
        A multi-line string ready to embed into the system instructions.
        Returns a short fallback message when ``endpoints`` is empty.
    """
    eps = list(endpoints)
    if read_only:
        eps = [e for e in eps if e[_K_METHOD].upper() == "GET"]

    if not eps:
        return (
            "_API endpoint catalog is currently unavailable._\n"
            "Check the server startup logs for `api_tree_query_failed`. If you "
            "already know a `METHOD /path`, you can still use `query_graph` "
            "against the ApiEndpoint/Parameter/Property subgraph (see "
            "`graph://schema`)."
        )

    by_cat: dict[str, list[dict]] = defaultdict(list)
    for ep in eps:
        cat = ep.get(_K_CATEGORY) or "(uncategorized)"
        by_cat[cat].append(ep)

    out: list[str] = [
        f"# API Endpoint Catalog ({len(eps)} endpoints, {len(by_cat)} categories)",
        "",
        "Format: `path/` (branch) or `segment  [METHODS]` (endpoint).",
        "A trailing `!` after the method list marks deprecated endpoints.",
        "Use `query_graph` against `ApiEndpoint`/`Parameter`/`RequestBody`/`Property` nodes for endpoint details — see `graph://schema` for canned patterns.",
        "",
    ]

    for cat in sorted(by_cat):
        cat_eps = by_cat[cat]
        out.append(f"## {cat} ({len(cat_eps)})")
        out.extend(_render_category_tree(cat_eps))
        out.append("")

    return "\n".join(out).rstrip() + "\n"
