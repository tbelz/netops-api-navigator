"""Opt-in runtime hydration MCP tools.

Runtime hydration starts as a safe planning and introspection surface. The
first enabled tools do not call Central APIs. They expose the feature flag,
generic observation schema readiness, and deterministic read-endpoint
classification over the existing API graph. Later PRs can reuse this contract
to execute bounded GET hydration and persist observations.
"""

from __future__ import annotations

import json
from typing import TYPE_CHECKING, Any

from mcp.types import ToolAnnotations

from ..config import Settings
from ..graph.schema import HYDRATION_NODE_TABLES, HYDRATION_REL_TABLES

if TYPE_CHECKING:
    from ..graph.manager import GraphManager

_MAX_CANDIDATE_LIMIT = 100
_DEFAULT_CANDIDATE_LIMIT = 25
_IDENTITY_FIELD_NAMES = (
    "id",
    "uuid",
    "uid",
    "serial",
    "serialNumber",
    "serial_number",
    "mac",
    "macaddr",
    "macAddress",
    "name",
    "scopeId",
    "siteId",
    "deviceGroupId",
)


def register_runtime_hydration_tools(
    mcp,
    settings: Settings,
    graph_manager: "GraphManager | None" = None,
) -> None:
    """Register opt-in runtime hydration tools."""

    gm = graph_manager

    @mcp.tool(
        annotations=ToolAnnotations(readOnlyHint=True, idempotentHint=True, openWorldHint=False),
    )
    def get_runtime_hydration_status() -> str:
        """Report runtime hydration feature status.

        Runtime hydration is opt-in. This foundation stage can classify
        read-capable endpoints and has graph schema for future observations,
        but it does not execute live hydration calls yet.
        """
        return json.dumps(
            {
                "enabled": settings.runtime_hydration,
                "stage": "foundation",
                "capabilities": [
                    "status",
                    "list_read_hydration_candidates",
                    "generic_observation_schema",
                ],
                "implemented": {
                    "generic_endpoint_hydration": False,
                    "observation_persistence_schema": True,
                    "observation_persistence_runtime": False,
                    "materialization": False,
                },
                "graph_available": bool(gm is not None and getattr(gm, "is_available", False)),
                "schema": {
                    "node_tables": _ddl_table_names(HYDRATION_NODE_TABLES),
                    "relationship_tables": _ddl_table_names(HYDRATION_REL_TABLES),
                },
                "roadmap": "docs/runtime-hydration-roadmap.md",
                "message": (
                    "Runtime hydration foundation is enabled. This stage can "
                    "classify read endpoints but does not call live APIs yet."
                ),
            },
            indent=2,
        )

    @mcp.tool(
        annotations=ToolAnnotations(readOnlyHint=True, idempotentHint=True, openWorldHint=False),
    )
    def list_runtime_hydration_candidates(search: str = "", limit: int = 25) -> str:
        """List GET endpoints that may be hydratable later.

        This is a planning tool only. It reads the API graph, classifies
        endpoint shape, parameters, response root, pagination hints, and
        identity hints. It never calls Central or writes observations.
        """
        if gm is None or not getattr(gm, "is_available", False):
            return json.dumps(
                {
                    "candidates": [],
                    "total": 0,
                    "error": "Graph database is unavailable.",
                },
                indent=2,
            )

        safe_limit = _clamp_limit(limit)
        endpoint_rows = _query_candidate_endpoints(gm, search, safe_limit)
        candidates = []
        for endpoint in endpoint_rows:
            endpoint_id = endpoint.get("endpoint_id") or ""
            parameters = _query_endpoint_parameters(gm, endpoint_id)
            responses = _query_endpoint_responses(gm, endpoint_id)
            identity_hints = _query_identity_hints(gm, responses)
            candidates.append(
                classify_hydration_candidate(
                    endpoint=endpoint,
                    parameters=parameters,
                    responses=responses,
                    identity_hints=identity_hints,
                )
            )

        return json.dumps(
            {
                "candidates": candidates,
                "total": len(candidates),
                "limit": safe_limit,
                "search": search,
                "note": (
                    "Candidates are read-only planning metadata. Future "
                    "hydration execution must still validate parameters, "
                    "pagination, cost, and identity before persisting data."
                ),
            },
            indent=2,
            default=str,
        )


def classify_hydration_candidate(
    *,
    endpoint: dict[str, Any],
    parameters: list[dict[str, Any]],
    responses: list[dict[str, Any]],
    identity_hints: list[dict[str, Any]],
) -> dict[str, Any]:
    """Return deterministic hydration planning metadata for one GET endpoint."""
    method = str(endpoint.get("method") or "").upper()
    path = str(endpoint.get("path") or "")
    required_params = [
        _param_summary(row) for row in parameters if _truthy(row.get("required"))
    ]
    path_params = [
        _param_summary(row) for row in parameters
        if str(row.get("location") or "").lower() == "path"
    ]
    query_params = [
        _param_summary(row) for row in parameters
        if str(row.get("location") or "").lower() == "query"
    ]
    response_root = _preferred_response_root(responses)
    pagination = _classify_pagination(parameters)
    identity = _classify_identity(identity_hints)
    shape = _classify_execution_shape(
        path=path,
        path_params=path_params,
        query_params=query_params,
        response_root=response_root,
        pagination=pagination,
    )
    blockers = []
    if method != "GET":
        blockers.append("not_get")
    if path_params:
        blockers.append("requires_path_parameters")
    if not response_root:
        blockers.append("no_response_schema")

    return {
        "endpoint_id": endpoint.get("endpoint_id") or f"{method}:{path}",
        "method": method,
        "path": path,
        "summary": endpoint.get("summary") or "",
        "operationId": endpoint.get("operationId") or "",
        "category": endpoint.get("category") or "",
        "hydration_candidate": method == "GET",
        "execution_shape": shape,
        "required_parameters": required_params,
        "path_parameters": path_params,
        "query_parameters": query_params,
        "pagination": pagination,
        "response_root": response_root,
        "identity": identity,
        "blockers": blockers,
        "confidence": _candidate_confidence(
            method=method,
            blockers=blockers,
            pagination=pagination,
            identity=identity,
        ),
    }


def _query_candidate_endpoints(
    graph_manager: "GraphManager",
    search: str,
    limit: int,
) -> list[dict[str, Any]]:
    search_clause = ""
    params: dict[str, Any] = {}
    if search.strip():
        search_clause = (
            "AND (toLower(e.path) CONTAINS toLower($search) "
            "OR toLower(coalesce(e.summary, '')) CONTAINS toLower($search) "
            "OR toLower(coalesce(e.operationId, '')) CONTAINS toLower($search)) "
        )
        params["search"] = search.strip()
    return graph_manager.query(
        "MATCH (e:ApiEndpoint) "
        "WHERE e.method = 'GET' "
        f"{search_clause}"
        "RETURN e.endpoint_id AS endpoint_id, e.method AS method, "
        "e.path AS path, e.summary AS summary, e.operationId AS operationId, "
        "e.category AS category "
        f"ORDER BY e.path LIMIT {limit}",
        params=params,
        read_only=True,
    )


def _query_endpoint_parameters(
    graph_manager: "GraphManager",
    endpoint_id: str,
) -> list[dict[str, Any]]:
    if not endpoint_id:
        return []
    return graph_manager.query(
        "MATCH (:ApiEndpoint {endpoint_id: $endpoint_id})-[:HAS_PARAMETER]->(p:Parameter) "
        "RETURN p.name AS name, p.location AS location, p.required AS required, "
        "p.type AS type, p.format AS format "
        "ORDER BY p.location, p.required DESC, p.name LIMIT 80",
        params={"endpoint_id": endpoint_id},
        read_only=True,
    )


def _query_endpoint_responses(
    graph_manager: "GraphManager",
    endpoint_id: str,
) -> list[dict[str, Any]]:
    if not endpoint_id:
        return []
    return graph_manager.query(
        "MATCH (:ApiEndpoint {endpoint_id: $endpoint_id})-[:HAS_RESPONSE]->(r:Response) "
        "OPTIONAL MATCH (r)-[:RESPONSE_REFERENCES]->(c:SchemaComponent) "
        "RETURN r.status AS status, r.content_type AS content_type, "
        "c.component_id AS component_id, c.name AS schema_name, "
        "c.bodyShape AS body_shape, c.type AS schema_type "
        "ORDER BY r.status LIMIT 20",
        params={"endpoint_id": endpoint_id},
        read_only=True,
    )


def _query_identity_hints(
    graph_manager: "GraphManager",
    responses: list[dict[str, Any]],
) -> list[dict[str, Any]]:
    root = _preferred_response_root(responses)
    component_id = root.get("component_id") if root else None
    if not component_id:
        return []
    identity_names = "[" + ", ".join(f"'{name}'" for name in _IDENTITY_FIELD_NAMES) + "]"
    try:
        return graph_manager.query(
            "MATCH (root:SchemaComponent {component_id: $component_id}) "
            "MATCH (root)-[:COMPOSED_OF*0..5]->(c:SchemaComponent)-[:HAS_PROPERTY]->(p:Property) "
            f"WHERE p.name IN {identity_names} "
            "RETURN DISTINCT p.name AS name, p.type AS type, "
            "p.parent_component_id AS parent_component_id, "
            "p.property_id AS property_id "
            "ORDER BY p.name LIMIT 20",
            params={"component_id": component_id},
            read_only=True,
        )
    except Exception:
        return []


def _preferred_response_root(responses: list[dict[str, Any]]) -> dict[str, Any]:
    roots = []
    for row in responses:
        status = str(row.get("status") or "")
        component_id = row.get("component_id") or ""
        if not component_id:
            continue
        roots.append(
            {
                "status": status,
                "content_type": row.get("content_type") or "",
                "component_id": component_id,
                "schema_name": row.get("schema_name") or "",
                "body_shape": row.get("body_shape") or "",
                "schema_type": row.get("schema_type") or "",
            }
        )
    for root in roots:
        if root["status"].startswith("2"):
            return root
    return roots[0] if roots else {}


def _classify_execution_shape(
    *,
    path: str,
    path_params: list[dict[str, Any]],
    query_params: list[dict[str, Any]],
    response_root: dict[str, Any],
    pagination: dict[str, Any],
) -> str:
    if path_params or "{" in path:
        return "parameterized_lookup"
    if pagination["style"] != "none_detected":
        return "collection"
    root_shape = (response_root.get("body_shape") or response_root.get("schema_type") or "").lower()
    if root_shape in {"array", "map"}:
        return "collection"
    query_names = {str(p.get("name") or "").lower() for p in query_params}
    if {"limit", "offset", "next", "cursor"} & query_names:
        return "collection"
    return "single_or_unknown"


def _classify_pagination(parameters: list[dict[str, Any]]) -> dict[str, Any]:
    query_names = {
        str(row.get("name") or "").lower()
        for row in parameters
        if str(row.get("location") or "").lower() == "query"
    }
    if {"offset", "limit"} <= query_names:
        return {"style": "offset", "confidence": "medium", "parameters": ["limit", "offset"]}
    if {"next", "limit"} <= query_names:
        return {"style": "cursor", "confidence": "medium", "parameters": ["limit", "next"]}
    if "cursor" in query_names:
        return {"style": "cursor", "confidence": "low", "parameters": ["cursor"]}
    if "limit" in query_names:
        return {"style": "bounded_list", "confidence": "low", "parameters": ["limit"]}
    return {"style": "none_detected", "confidence": "low", "parameters": []}


def _classify_identity(identity_hints: list[dict[str, Any]]) -> dict[str, Any]:
    names = [str(row.get("name") or "") for row in identity_hints if row.get("name")]
    priority = [
        "id",
        "uuid",
        "serial",
        "serialNumber",
        "serial_number",
        "mac",
        "macaddr",
        "macAddress",
        "scopeId",
        "name",
    ]
    ordered = sorted(set(names), key=lambda name: priority.index(name) if name in priority else 999)
    if not ordered:
        return {"status": "unknown", "fields": [], "confidence": "low"}
    high_confidence_fields = {"id", "uuid", "serial", "serialNumber", "mac", "macaddr"}
    confidence = "high" if ordered[0] in high_confidence_fields else "medium"
    return {"status": "candidate", "fields": ordered, "confidence": confidence}


def _candidate_confidence(
    *,
    method: str,
    blockers: list[str],
    pagination: dict[str, Any],
    identity: dict[str, Any],
) -> str:
    if method != "GET":
        return "unsupported"
    serious = set(blockers) - {"requires_path_parameters"}
    if serious:
        return "low"
    if identity.get("confidence") == "high" and pagination.get("confidence") in {"medium", "high"}:
        return "high"
    if identity.get("status") == "candidate":
        return "medium"
    return "low"


def _param_summary(row: dict[str, Any]) -> dict[str, Any]:
    return {
        "name": row.get("name") or "",
        "location": row.get("location") or "",
        "required": bool(row.get("required") or False),
        "type": row.get("type") or "",
        "format": row.get("format") or "",
    }


def _truthy(value: Any) -> bool:
    return bool(value is True or str(value).lower() == "true")


def _clamp_limit(limit: int) -> int:
    try:
        parsed = int(limit)
    except (TypeError, ValueError):
        return _DEFAULT_CANDIDATE_LIMIT
    if parsed <= 0:
        return _DEFAULT_CANDIDATE_LIMIT
    return min(parsed, _MAX_CANDIDATE_LIMIT)


def _ddl_table_names(ddls: list[str]) -> list[str]:
    names = []
    for ddl in ddls:
        words = ddl.replace("(", " ").split()
        if "EXISTS" in words:
            idx = words.index("EXISTS")
            if idx + 1 < len(words):
                names.append(words[idx + 1])
    return names
