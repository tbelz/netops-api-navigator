"""Searchable index over scraped OpenAPI specs.

Builds a flat list of ``EndpointEntry`` objects from parsed OpenAPI specs,
supports keyword search (compact results) and full endpoint detail with
resolved ``$ref`` schemas.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Any


@dataclass
class ParamEntry:
    name: str
    location: str  # "query", "path", "header"
    required: bool
    schema: dict
    description: str = ""


@dataclass
class ResponseEntry:
    status: str  # "200", "201", "4XX", etc.
    description: str = ""
    schema: dict | None = None


@dataclass
class EndpointEntry:
    """Full detail for a single API endpoint."""

    method: str
    path: str
    summary: str
    description: str
    operation_id: str
    tags: list[str]
    category: str  # OAS info.title
    deprecated: bool
    parameters: list[ParamEntry] = field(default_factory=list)
    request_body: dict | None = None  # resolved schema
    responses: list[ResponseEntry] = field(default_factory=list)


@dataclass
class CompactEntry:
    """Lightweight summary for search result lists."""

    method: str
    path: str
    summary: str
    category: str
    deprecated: bool
    operation_id: str


class OASIndex:
    """Searchable index over a collection of OpenAPI specs."""

    def __init__(self) -> None:
        self._entries: list[EndpointEntry] = []
        self._categories: dict[str, int] = {}  # category → endpoint count

    @property
    def total_endpoints(self) -> int:
        return len(self._entries)

    @property
    def categories(self) -> dict[str, int]:
        return dict(self._categories)

    # ----- Build index --------------------------------------------------------

    def build(self, specs: list[dict]) -> None:
        """Build index from a list of parsed OpenAPI spec dicts."""
        self._entries.clear()
        self._categories.clear()

        for spec in specs:
            title = spec.get("info", {}).get("title", "Unknown")
            components = spec.get("components", {})
            paths = spec.get("paths", {})

            count = 0
            for path, path_item in paths.items():
                for method in ("get", "post", "put", "patch", "delete", "head", "options"):
                    operation = path_item.get(method)
                    if not operation:
                        continue

                    entry = self._parse_operation(
                        method=method.upper(),
                        path=path,
                        operation=operation,
                        category=title,
                        components=components,
                    )
                    self._entries.append(entry)
                    count += 1

            if count:
                self._categories[title] = self._categories.get(title, 0) + count

    def _parse_operation(
        self,
        method: str,
        path: str,
        operation: dict,
        category: str,
        components: dict,
    ) -> EndpointEntry:
        params = []
        for p in operation.get("parameters", []):
            p = _resolve_refs(p, components)
            params.append(ParamEntry(
                name=p.get("name", ""),
                location=p.get("in", ""),
                required=p.get("required", False),
                schema=_resolve_refs(p.get("schema", {}), components),
                description=p.get("description", ""),
            ))

        # Request body
        req_body = None
        rb = operation.get("requestBody")
        if rb:
            rb = _resolve_refs(rb, components)
            content = rb.get("content", {})
            for media_type in ("application/json", "application/merge-patch+json"):
                if media_type in content:
                    schema = content[media_type].get("schema", {})
                    req_body = _resolve_refs(schema, components)
                    break
            if req_body is None and content:
                first_ct = next(iter(content.values()))
                req_body = _resolve_refs(first_ct.get("schema", {}), components)

        # Responses
        responses = []
        for status, resp_obj in operation.get("responses", {}).items():
            resp_obj = _resolve_refs(resp_obj, components)
            resp_schema = None
            resp_content = resp_obj.get("content", {})
            if "application/json" in resp_content:
                resp_schema = _resolve_refs(
                    resp_content["application/json"].get("schema", {}), components
                )
            responses.append(ResponseEntry(
                status=str(status),
                description=resp_obj.get("description", ""),
                schema=resp_schema,
            ))

        return EndpointEntry(
            method=method,
            path=path,
            summary=operation.get("summary", ""),
            description=operation.get("description", ""),
            operation_id=operation.get("operationId", ""),
            tags=operation.get("tags", []),
            category=category,
            deprecated=operation.get("deprecated", False),
            parameters=params,
            request_body=req_body,
            responses=responses,
        )

    # ----- Search (compact) ---------------------------------------------------

    def search(
        self,
        query: str,
        *,
        include_deprecated: bool = False,
        limit: int = 40,
    ) -> list[CompactEntry]:
        """Search endpoints by keyword. Returns compact results."""
        terms = query.strip().lower().split()
        if not terms:
            return []

        results: list[tuple[int, CompactEntry]] = []
        for entry in self._entries:
            if not include_deprecated and entry.deprecated:
                continue
            score = _match_score(entry, terms)
            if score > 0:
                results.append((score, CompactEntry(
                    method=entry.method,
                    path=entry.path,
                    summary=entry.summary,
                    category=entry.category,
                    deprecated=entry.deprecated,
                    operation_id=entry.operation_id,
                )))

        results.sort(key=lambda x: -x[0])
        return [r[1] for r in results[:limit]]

    # ----- Detail (full resolved) ---------------------------------------------

    def get_detail(
        self,
        method: str,
        path: str,
    ) -> EndpointEntry | None:
        """Get full endpoint detail by exact method + path."""
        method_up = method.upper()
        for entry in self._entries:
            if entry.method == method_up and entry.path == path:
                return entry
        return None

    def get_detail_by_operation_id(self, operation_id: str) -> EndpointEntry | None:
        """Get full endpoint detail by operationId."""
        for entry in self._entries:
            if entry.operation_id == operation_id:
                return entry
        return None

    # ----- List categories ----------------------------------------------------

    def list_categories(self) -> dict[str, int]:
        """Return {category_title: endpoint_count}."""
        return dict(self._categories)


# ----- $ref resolution -------------------------------------------------------

_MAX_REF_DEPTH = 15


def _resolve_refs(obj: Any, components: dict, *, _depth: int = 0) -> Any:
    """Recursively resolve $ref pointers within an OpenAPI schema.

    Expands references inline up to ``_MAX_REF_DEPTH`` levels to prevent
    infinite recursion on circular schemas.
    """
    if _depth > _MAX_REF_DEPTH:
        return {"$circular_ref": True}

    if isinstance(obj, dict):
        ref = obj.get("$ref")
        if ref and isinstance(ref, str):
            resolved = _follow_ref(ref, components)
            if resolved is not None:
                return _resolve_refs(resolved, components, _depth=_depth + 1)
            return obj  # unresolvable ref — keep as-is

        # Recurse into all dict values
        return {k: _resolve_refs(v, components, _depth=_depth) for k, v in obj.items()}

    if isinstance(obj, list):
        return [_resolve_refs(item, components, _depth=_depth) for item in obj]

    return obj


def _follow_ref(ref: str, components: dict) -> Any | None:
    """Resolve a ``#/components/...`` reference."""
    if not ref.startswith("#/components/"):
        return None
    parts = ref.removeprefix("#/components/").split("/")
    node: Any = components
    for part in parts:
        if isinstance(node, dict):
            node = node.get(part)
        else:
            return None
    return node


# ----- Scoring ----------------------------------------------------------------


def _match_score(entry: EndpointEntry, terms: list[str]) -> int:
    """Score an endpoint against search terms. Higher = better match."""
    # Build searchable text fields with weights
    fields = [
        (entry.path.lower(), 3),
        (entry.summary.lower(), 2),
        (entry.operation_id.lower(), 2),
        (entry.description.lower(), 1),
        (" ".join(entry.tags).lower(), 2),
        (entry.category.lower(), 1),
    ]

    total = 0
    for term in terms:
        term_score = 0
        for text, weight in fields:
            if term in text:
                term_score += weight
        if term_score == 0:
            return 0  # all terms must match
        total += term_score

    return total
