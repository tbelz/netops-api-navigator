"""Opt-in runtime hydration MCP tools.

Runtime hydration is a generic observation and fact layer on top of the API
graph. It can classify GET endpoints, execute bounded read hydration when live
clients are available, persist raw observations with provenance, and
materialize generic runtime facts when observed identity is clear.
"""

from __future__ import annotations

import hashlib
import json
import re
import time
import uuid
from datetime import datetime, timezone
from typing import TYPE_CHECKING, Any

from mcp.server.fastmcp.exceptions import ToolError
from mcp.types import ToolAnnotations

from ..central_client import CentralAPIError
from ..config import Settings
from ..graph.schema import HYDRATION_NODE_TABLES, HYDRATION_REL_TABLES

if TYPE_CHECKING:
    from ..central_client import BaseAPIClient
    from ..graph.manager import GraphManager

_MAX_CANDIDATE_LIMIT = 100
_DEFAULT_CANDIDATE_LIMIT = 25
_MAX_PLAN_LIMIT = 20
_DEFAULT_PLAN_LIMIT = 5
_MAX_OBSERVATION_LIMIT = 100
_DEFAULT_OBSERVATION_LIMIT = 20
_MAX_OBJECT_LIMIT = 100
_DEFAULT_OBJECT_LIMIT = 25
_MAX_FIELD_LIMIT = 500
_DEFAULT_FIELD_LIMIT = 200
_MAX_FACT_LIMIT = 500
_DEFAULT_FACT_LIMIT = 50
_MAX_MATERIALIZATION_OBJECT_LIMIT = 5_000
_DEFAULT_MATERIALIZATION_OBJECT_LIMIT = 1_000
_DEFAULT_STALE_AFTER_SECONDS = 300
_MAX_STALE_AFTER_SECONDS = 7 * 24 * 60 * 60
_DEFAULT_MAX_RESPONSE_BYTES = 1_000_000
_MAX_RESPONSE_BYTES = 5_000_000
_PATH_PARAM_RE = re.compile(r"{([^}/]+)}")
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
_PROVIDER_DEFINITIONS = {
    "central": {
        "provider": "central",
        "label": "HPE Aruba Networking Central",
        "aliases": ["central", "aruba", "aruba-central"],
        "base_url_setting": "CENTRAL_BASE_URL",
    },
    "greenlake": {
        "provider": "greenlake",
        "label": "HPE GreenLake Platform",
        "aliases": ["greenlake", "glp", "hpe-greenlake"],
        "base_url_setting": "GLP_BASE_URL",
    },
}


def register_runtime_hydration_tools(
    mcp,
    settings: Settings,
    graph_manager: "GraphManager | None" = None,
    central_client: "BaseAPIClient | None" = None,
    greenlake_client: "BaseAPIClient | None" = None,
) -> None:
    """Register opt-in runtime hydration tools."""

    gm = graph_manager
    central = central_client
    greenlake = greenlake_client

    @mcp.tool(
        annotations=ToolAnnotations(readOnlyHint=True, idempotentHint=True, openWorldHint=False),
    )
    def get_runtime_hydration_status() -> str:
        """Report runtime hydration feature status.

        Runtime hydration is opt-in. The current stage can classify GET
        endpoints, persist observations, report freshness, and materialize
        generic facts. It does not promote typed runtime highways yet.
        """
        return json.dumps(
            {
                "enabled": settings.runtime_hydration,
                "stage": "planning",
                "capabilities": [
                    "status",
                    "list_read_hydration_candidates",
                    "generic_observation_schema",
                    "hydrate_read_endpoint",
                    "list_runtime_observations",
                    "get_runtime_observation",
                    "get_runtime_hydration_state",
                    "list_runtime_hydration_providers",
                    "plan_runtime_hydration",
                    "materialize_runtime_facts",
                    "list_runtime_facts",
                    "get_runtime_fact",
                ],
                "implemented": {
                    "generic_endpoint_hydration": True,
                    "observation_persistence_schema": True,
                    "observation_persistence_runtime": True,
                    "materialization": True,
                    "provider_readiness": True,
                    "planning_helpers": True,
                },
                "graph_available": bool(gm is not None and getattr(gm, "is_available", False)),
                "clients_available": {
                    "central": central is not None,
                    "greenlake": greenlake is not None,
                },
                "providers": _provider_readiness(settings, central, greenlake),
                "schema": {
                    "node_tables": _ddl_table_names(HYDRATION_NODE_TABLES),
                    "relationship_tables": _ddl_table_names(HYDRATION_REL_TABLES),
                },
                "roadmap": "docs/runtime-hydration-roadmap.md",
                "message": (
                    "Runtime hydration is enabled. This stage can classify "
                    "GET endpoints, hydrate a bounded read endpoint, and "
                    "persist raw runtime observations."
                ),
            },
            indent=2,
        )

    @mcp.tool(
        annotations=ToolAnnotations(readOnlyHint=True, idempotentHint=True, openWorldHint=False),
    )
    def list_runtime_hydration_providers() -> str:
        """List runtime hydration provider boundaries and client readiness."""
        return json.dumps(
            {
                "providers": _provider_readiness(settings, central, greenlake),
                "default_provider": "central",
                "note": (
                    "Provider selection is explicit. Core observation, freshness, "
                    "and fact tables are shared across providers."
                ),
            },
            indent=2,
            default=str,
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

    @mcp.tool(
        annotations=ToolAnnotations(readOnlyHint=True, idempotentHint=True, openWorldHint=False),
    )
    def plan_runtime_hydration(
        question: str = "",
        endpoint_id: str = "",
        path: str = "",
        search: str = "",
        provider: str = "central",
        query_params: dict[str, str] | None = None,
        path_params: dict[str, str] | None = None,
        freshness_max_age_seconds: int = _DEFAULT_STALE_AFTER_SECONDS,
        limit: int = _DEFAULT_PLAN_LIMIT,
    ) -> str:
        """Plan useful runtime hydration work without calling live APIs.

        This advisory helper combines endpoint classification, supplied
        parameters, provider readiness, existing observation freshness, and
        materialized facts. It explains whether an agent should use existing
        graph data, materialize existing observations, hydrate a read endpoint,
        or ask for missing parameters first.
        """
        graph = _require_graph(gm)
        provider_key = _normalise_provider(provider)
        provider_status = _provider_readiness_by_key(settings, central, greenlake, provider_key)
        safe_limit = _clamp_limit_with_max(limit, _DEFAULT_PLAN_LIMIT, _MAX_PLAN_LIMIT)
        params = query_params or {}
        scope = path_params or {}
        freshness_target = _clamp_int(
            freshness_max_age_seconds,
            default=_DEFAULT_STALE_AFTER_SECONDS,
            minimum=0,
            maximum=_MAX_STALE_AFTER_SECONDS,
        )

        endpoints = _planning_endpoints(
            graph,
            endpoint_id=endpoint_id,
            path=path,
            search=search or question,
            limit=safe_limit,
        )
        plans = []
        for endpoint in endpoints:
            plan = _build_endpoint_hydration_plan(
                graph=graph,
                endpoint=endpoint,
                provider_key=provider_key,
                provider_status=provider_status,
                query_params=params,
                path_params=scope,
                freshness_target=freshness_target,
            )
            plans.append(plan)

        return json.dumps(
            {
                "question": question,
                "search": search or question,
                "provider": provider_key,
                "provider_ready": provider_status,
                "plans": plans,
                "total": len(plans),
                "next_best_action": _next_best_plan_action(plans),
                "note": (
                    "This tool is advisory and read-only. It does not call live "
                    "APIs or write graph data."
                ),
            },
            indent=2,
            default=str,
        )

    @mcp.tool(
        annotations=ToolAnnotations(
            readOnlyHint=False,
            idempotentHint=False,
            openWorldHint=True,
        ),
    )
    def hydrate_runtime_endpoint(
        endpoint_id: str = "",
        path: str = "",
        provider: str = "central",
        query_params: dict[str, str] | None = None,
        path_params: dict[str, str] | None = None,
        stale_after_seconds: int = _DEFAULT_STALE_AFTER_SECONDS,
        max_response_bytes: int = _DEFAULT_MAX_RESPONSE_BYTES,
    ) -> str:
        """Hydrate one bounded GET endpoint into runtime observation nodes.

        This is the first generic executor. It supports GET endpoints only,
        requires any path/query parameters to be supplied explicitly, calls the
        selected provider client, and persists raw response/provenance data
        back into the graph. It does not materialize typed runtime facts yet.
        """
        graph = _require_graph(gm)
        endpoint = _resolve_endpoint(graph, endpoint_id=endpoint_id, path=path)
        method = str(endpoint.get("method") or "").upper()
        if method != "GET":
            raise ToolError("Runtime hydration currently supports GET endpoints only.")

        provider_key = _normalise_provider(provider)
        api_client = _select_api_client(provider_key, central, greenlake)
        params = query_params or {}
        path_values = path_params or {}
        parameter_rows = _query_endpoint_parameters(graph, endpoint["endpoint_id"])
        validation_errors = _validate_hydration_inputs(
            endpoint=endpoint,
            parameters=parameter_rows,
            query_params=params,
            path_params=path_values,
        )
        if validation_errors:
            raise ToolError(
                "Runtime hydration rejected by endpoint parameter validation:\n"
                + "\n".join(f"  - {err}" for err in validation_errors)
            )

        actual_path = _apply_path_params(endpoint["path"], path_values)
        responses = _query_endpoint_responses(graph, endpoint["endpoint_id"])
        response_root = _preferred_response_root(responses)
        property_map = _query_response_property_map(graph, response_root)
        stale_after = _clamp_int(
            stale_after_seconds,
            default=_DEFAULT_STALE_AFTER_SECONDS,
            minimum=0,
            maximum=_MAX_STALE_AFTER_SECONDS,
        )
        response_cap = _clamp_int(
            max_response_bytes,
            default=_DEFAULT_MAX_RESPONSE_BYTES,
            minimum=10_000,
            maximum=_MAX_RESPONSE_BYTES,
        )

        request_payload = {
            "provider": provider_key,
            "endpoint_id": endpoint["endpoint_id"],
            "method": "GET",
            "path": actual_path,
            "query_params": params,
            "path_params": path_values,
        }
        started = time.perf_counter()
        try:
            response = api_client._request("GET", actual_path.lstrip("/"), params=params or None)
        except CentralAPIError as exc:
            duration_ms = int((time.perf_counter() - started) * 1000)
            run_id = _persist_hydration_failure(
                graph=graph,
                endpoint=endpoint,
                provider=provider_key,
                request_payload=request_payload,
                duration_ms=duration_ms,
                error=f"[{exc.status_code}] {exc.message}",
            )
            return json.dumps(
                {
                    "ok": False,
                    "run_id": run_id,
                    "endpoint_id": endpoint["endpoint_id"],
                    "status": exc.status_code,
                    "error": exc.message,
                },
                indent=2,
            )

        duration_ms = int((time.perf_counter() - started) * 1000)
        raw_json = _stable_json(response)
        response_bytes = len(raw_json.encode("utf-8"))
        response_hash = _sha256(raw_json)
        if response_bytes > response_cap:
            run_id = _persist_hydration_failure(
                graph=graph,
                endpoint=endpoint,
                provider=provider_key,
                request_payload=request_payload,
                duration_ms=duration_ms,
                error=(
                    f"Response was {response_bytes} bytes, exceeding "
                    f"max_response_bytes={response_cap}."
                ),
                response_hash=response_hash,
                response_bytes=response_bytes,
            )
            return json.dumps(
                {
                    "ok": False,
                    "run_id": run_id,
                    "endpoint_id": endpoint["endpoint_id"],
                    "response_hash": response_hash,
                    "response_bytes": response_bytes,
                    "error": "response_too_large",
                },
                indent=2,
            )

        observed_objects = _extract_observed_objects(response, property_map)
        run_id, observation_id = _persist_hydration_success(
            graph=graph,
            endpoint=endpoint,
            provider=provider_key,
            request_payload=request_payload,
            duration_ms=duration_ms,
            response=response,
            raw_json=raw_json,
            response_hash=response_hash,
            response_bytes=response_bytes,
            response_root=response_root,
            stale_after_seconds=stale_after,
            observed_objects=observed_objects,
        )
        return json.dumps(
            {
                "ok": True,
                "run_id": run_id,
                "observation_id": observation_id,
                "endpoint_id": endpoint["endpoint_id"],
                "provider": provider_key,
                "method": "GET",
                "path": actual_path,
                "response_hash": response_hash,
                "response_bytes": response_bytes,
                "root_kind": _value_kind(response),
                "item_count": _item_count(response),
                "observed_object_count": len(observed_objects),
                "stale_after_seconds": stale_after,
            },
            indent=2,
        )

    @mcp.tool(
        annotations=ToolAnnotations(readOnlyHint=True, idempotentHint=True, openWorldHint=False),
    )
    def list_runtime_observations(
        endpoint_id: str = "",
        provider: str = "",
        limit: int = _DEFAULT_OBSERVATION_LIMIT,
        include_raw: bool = False,
    ) -> str:
        """List persisted runtime observations without calling live APIs."""
        graph = _require_graph(gm)
        rows = _query_runtime_observations(
            graph,
            endpoint_id=endpoint_id,
            provider=provider,
            limit=_clamp_limit_with_max(limit, _DEFAULT_OBSERVATION_LIMIT, _MAX_OBSERVATION_LIMIT),
        )
        if not include_raw:
            for row in rows:
                row.pop("rawJson", None)
        rows = [_annotate_observation_freshness(row) for row in rows]
        return json.dumps({"observations": rows, "total": len(rows)}, indent=2, default=str)

    @mcp.tool(
        annotations=ToolAnnotations(readOnlyHint=True, idempotentHint=True, openWorldHint=False),
    )
    def get_runtime_observation(
        observation_id: str = "",
        include_raw: bool = False,
        object_limit: int = _DEFAULT_OBJECT_LIMIT,
        field_limit: int = _DEFAULT_FIELD_LIMIT,
    ) -> str:
        """Fetch one runtime observation with observed objects and fields."""
        graph = _require_graph(gm)
        clean_id = observation_id.strip()
        if not clean_id:
            raise ToolError("observation_id is required.")
        observation = _query_runtime_observation(graph, clean_id)
        if not observation:
            raise ToolError(f"Runtime observation not found: {clean_id}")
        objects = _query_runtime_observed_objects(
            graph,
            clean_id,
            limit=_clamp_limit_with_max(object_limit, _DEFAULT_OBJECT_LIMIT, _MAX_OBJECT_LIMIT),
        )
        fields = _query_runtime_observed_fields(
            graph,
            clean_id,
            limit=_clamp_limit_with_max(field_limit, _DEFAULT_FIELD_LIMIT, _MAX_FIELD_LIMIT),
        )
        if not include_raw:
            observation.pop("rawJson", None)
            for obj in objects:
                obj.pop("rawJson", None)
        observation = _annotate_observation_freshness(observation)
        return json.dumps(
            {
                "observation": observation,
                "objects": objects,
                "fields": fields,
            },
            indent=2,
            default=str,
        )

    @mcp.tool(
        annotations=ToolAnnotations(readOnlyHint=True, idempotentHint=True, openWorldHint=False),
    )
    def get_runtime_hydration_state(
        endpoint_id: str = "",
        provider: str = "",
    ) -> str:
        """Report whether hydrated state for an endpoint is missing, fresh, or stale."""
        graph = _require_graph(gm)
        if not endpoint_id.strip():
            raise ToolError("endpoint_id is required.")
        rows = _query_runtime_observations(
            graph,
            endpoint_id=endpoint_id.strip(),
            provider=provider,
            limit=1,
        )
        if not rows:
            return json.dumps(
                {
                    "endpoint_id": endpoint_id.strip(),
                    "provider": provider,
                    "state": "missing",
                    "latest_observation": None,
                    "message": "No runtime observation has been persisted for this endpoint.",
                },
                indent=2,
            )
        latest = _annotate_observation_freshness(rows[0])
        latest.pop("rawJson", None)
        return json.dumps(
            {
                "endpoint_id": endpoint_id.strip(),
                "provider": provider,
                "state": latest["freshness"]["state"],
                "latest_observation": latest,
            },
            indent=2,
            default=str,
        )

    @mcp.tool(
        annotations=ToolAnnotations(
            readOnlyHint=False,
            idempotentHint=True,
            openWorldHint=False,
        ),
    )
    def materialize_runtime_facts(
        observation_id: str = "",
        endpoint_id: str = "",
        provider: str = "",
        limit: int = _DEFAULT_OBSERVATION_LIMIT,
        object_limit: int = _DEFAULT_MATERIALIZATION_OBJECT_LIMIT,
        object_offset: int = 0,
    ) -> str:
        """Materialize generic RuntimeFact nodes when observed identity is clear.

        This does not create Central-specific typed tables. It promotes
        observed objects with clear identity hints into generic facts and
        preserves edges back to the observation, observed object, hydration run,
        and source API endpoint.
        """
        graph = _require_graph(gm)
        if observation_id.strip():
            observation = _query_runtime_observation(graph, observation_id.strip())
            if not observation:
                raise ToolError(f"Runtime observation not found: {observation_id.strip()}")
            observations = [observation]
        else:
            observations = _query_runtime_observations(
                graph,
                endpoint_id=endpoint_id,
                provider=provider,
                limit=_clamp_limit_with_max(
                    limit,
                    _DEFAULT_OBSERVATION_LIMIT,
                    _MAX_OBSERVATION_LIMIT,
                ),
            )

        clean_object_limit = _clamp_limit_with_max(
            object_limit,
            _DEFAULT_MATERIALIZATION_OBJECT_LIMIT,
            _MAX_MATERIALIZATION_OBJECT_LIMIT,
        )
        clean_object_offset = max(0, _safe_int(object_offset))
        materialized: list[dict[str, Any]] = []
        skipped: list[dict[str, Any]] = []
        observations_with_more: list[dict[str, Any]] = []
        for observation in observations:
            if not observation:
                continue
            objects = _query_runtime_observed_objects(
                graph,
                observation["observation_id"],
                limit=clean_object_limit + 1,
                offset=clean_object_offset,
            )
            has_more = len(objects) > clean_object_limit
            if has_more:
                observations_with_more.append(
                    {
                        "observation_id": observation["observation_id"],
                        "next_object_offset": clean_object_offset + clean_object_limit,
                    }
                )
            for obj in objects[:clean_object_limit]:
                fact, reason = _materialize_fact_from_object(graph, observation, obj)
                if fact:
                    materialized.append(fact)
                else:
                    skipped.append(
                        {
                            "observation_id": observation.get("observation_id"),
                            "object_id": obj.get("object_id"),
                            "reason": reason,
                        }
                    )

        return json.dumps(
            {
                "materialized": materialized,
                "materialized_count": len(materialized),
                "skipped": skipped,
                "skipped_count": len(skipped),
                "object_limit": clean_object_limit,
                "object_offset": clean_object_offset,
                "has_more_objects": bool(observations_with_more),
                "next_object_offsets": observations_with_more,
            },
            indent=2,
            default=str,
        )

    @mcp.tool(
        annotations=ToolAnnotations(readOnlyHint=True, idempotentHint=True, openWorldHint=False),
    )
    def list_runtime_facts(
        endpoint_id: str = "",
        entity_type: str = "",
        identity_key: str = "",
        limit: int = _DEFAULT_FACT_LIMIT,
        include_attributes: bool = False,
    ) -> str:
        """List materialized generic runtime facts without calling live APIs."""
        graph = _require_graph(gm)
        facts = _query_runtime_facts(
            graph,
            endpoint_id=endpoint_id,
            entity_type=entity_type,
            identity_key=identity_key,
            limit=_clamp_limit_with_max(limit, _DEFAULT_FACT_LIMIT, _MAX_FACT_LIMIT),
        )
        if not include_attributes:
            for fact in facts:
                fact.pop("attributesJson", None)
        return json.dumps({"facts": facts, "total": len(facts)}, indent=2, default=str)

    @mcp.tool(
        annotations=ToolAnnotations(readOnlyHint=True, idempotentHint=True, openWorldHint=False),
    )
    def get_runtime_fact(fact_id: str = "", include_attributes: bool = False) -> str:
        """Fetch one materialized fact with provenance."""
        graph = _require_graph(gm)
        clean_id = fact_id.strip()
        if not clean_id:
            raise ToolError("fact_id is required.")
        fact = _query_runtime_fact(graph, clean_id)
        if not fact:
            raise ToolError(f"Runtime fact not found: {clean_id}")
        if not include_attributes:
            fact["fact"].pop("attributesJson", None)
        return json.dumps(fact, indent=2, default=str)


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
        "coalesce(c.component_id, r.root_component_ref) AS component_id, "
        "coalesce(c.name, r.root_component_ref) AS schema_name, "
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
    queries = [
        "MATCH (root:SchemaComponent {component_id: $component_id})"
        "-[:HAS_PROPERTY]->(p:Property) "
        f"WHERE p.name IN {identity_names} "
        "RETURN DISTINCT p.name AS name, p.type AS type, "
        "p.parent_component_id AS parent_component_id, "
        "p.property_id AS property_id "
        "ORDER BY name LIMIT 20",
        "MATCH (root:SchemaComponent {component_id: $component_id}) "
        "MATCH (root)-[:COMPOSED_OF*0..5]->(c:SchemaComponent)-[:HAS_PROPERTY]->(p:Property) "
        f"WHERE p.name IN {identity_names} "
        "RETURN DISTINCT p.name AS name, p.type AS type, "
        "p.parent_component_id AS parent_component_id, "
        "p.property_id AS property_id "
        "ORDER BY name LIMIT 20",
    ]
    rows: list[dict[str, Any]] = []
    seen: set[str] = set()
    for query in queries:
        try:
            for row in graph_manager.query(
                query,
                params={"component_id": component_id},
                read_only=True,
            ):
                property_id = str(row.get("property_id") or row.get("name") or "")
                if property_id and property_id not in seen:
                    seen.add(property_id)
                    rows.append(row)
        except Exception:
            continue
    return rows


def _provider_readiness(
    settings: Settings,
    central_client: "BaseAPIClient | None",
    greenlake_client: "BaseAPIClient | None",
) -> list[dict[str, Any]]:
    return [
        _provider_readiness_by_key(settings, central_client, greenlake_client, "central"),
        _provider_readiness_by_key(settings, central_client, greenlake_client, "greenlake"),
    ]


def _provider_readiness_by_key(
    settings: Settings,
    central_client: "BaseAPIClient | None",
    greenlake_client: "BaseAPIClient | None",
    provider_key: str,
) -> dict[str, Any]:
    provider_key = _normalise_provider(provider_key)
    definition = _PROVIDER_DEFINITIONS[provider_key]
    if provider_key == "central":
        configured = settings.has_credentials
        client_available = central_client is not None
        base_url = settings.central_base_url
    else:
        configured = settings.has_glp_credentials
        client_available = greenlake_client is not None
        base_url = settings.glp_base_url
    return {
        **definition,
        "configured": configured,
        "client_available": client_available,
        "can_hydrate": client_available,
        "base_url": base_url,
    }


def _normalise_provider(provider: str) -> str:
    provider_key = (provider or "central").strip().lower()
    for canonical, definition in _PROVIDER_DEFINITIONS.items():
        if provider_key in definition["aliases"]:
            return canonical
    allowed = ", ".join(sorted(_PROVIDER_DEFINITIONS))
    raise ToolError(f"provider must be one of: {allowed}.")


def _planning_endpoints(
    graph_manager: "GraphManager",
    *,
    endpoint_id: str,
    path: str,
    search: str,
    limit: int,
) -> list[dict[str, Any]]:
    if endpoint_id.strip() or path.strip():
        return [_resolve_endpoint(graph_manager, endpoint_id=endpoint_id, path=path)]
    return _query_candidate_endpoints(graph_manager, search, limit)


def _build_endpoint_hydration_plan(
    *,
    graph: "GraphManager",
    endpoint: dict[str, Any],
    provider_key: str,
    provider_status: dict[str, Any],
    query_params: dict[str, Any],
    path_params: dict[str, Any],
    freshness_target: int,
) -> dict[str, Any]:
    endpoint_id = str(endpoint.get("endpoint_id") or "")
    parameters = _query_endpoint_parameters(graph, endpoint_id)
    responses = _query_endpoint_responses(graph, endpoint_id)
    identity_hints = _query_identity_hints(graph, responses)
    candidate = classify_hydration_candidate(
        endpoint=endpoint,
        parameters=parameters,
        responses=responses,
        identity_hints=identity_hints,
    )
    validation_errors = _validate_hydration_inputs(
        endpoint=endpoint,
        parameters=parameters,
        query_params=query_params,
        path_params=path_params,
    )
    observations = _query_runtime_observations(
        graph,
        endpoint_id=endpoint_id,
        provider=provider_key,
        limit=1,
    )
    latest_observation = _summarise_latest_observation(observations)
    facts = _query_runtime_facts(
        graph,
        endpoint_id=endpoint_id,
        entity_type="",
        identity_key="",
        limit=5,
    )
    for fact in facts:
        fact.pop("attributesJson", None)
    action = _plan_action(
        candidate=candidate,
        validation_errors=validation_errors,
        provider_status=provider_status,
        latest_observation=latest_observation,
        fact_count=len(facts),
        freshness_target=freshness_target,
    )
    return {
        "endpoint": {
            "endpoint_id": endpoint_id,
            "method": endpoint.get("method") or "",
            "path": endpoint.get("path") or "",
            "summary": endpoint.get("summary") or "",
            "operationId": endpoint.get("operationId") or "",
            "category": endpoint.get("category") or "",
        },
        "candidate": candidate,
        "provider": provider_key,
        "provider_ready": provider_status["can_hydrate"],
        "supplied": {
            "query_params": sorted(query_params.keys()),
            "path_params": sorted(path_params.keys()),
        },
        "validation_errors": validation_errors,
        "latest_observation": latest_observation,
        "materialized_fact_count": len(facts),
        "sample_facts": facts,
        "recommended_action": action,
        "hydrate_call": _hydrate_call_plan(
            candidate=candidate,
            provider_key=provider_key,
            query_params=query_params,
            path_params=path_params,
        ),
        "materialize_call": _materialize_call_plan(latest_observation),
    }


def _summarise_latest_observation(rows: list[dict[str, Any]]) -> dict[str, Any] | None:
    if not rows:
        return None
    latest = _annotate_observation_freshness(rows[0])
    latest.pop("rawJson", None)
    return latest


def _plan_action(
    *,
    candidate: dict[str, Any],
    validation_errors: list[str],
    provider_status: dict[str, Any],
    latest_observation: dict[str, Any] | None,
    fact_count: int,
    freshness_target: int,
) -> dict[str, Any]:
    if candidate["blockers"]:
        return {
            "action": "unsupported",
            "reason": "Endpoint is not currently supported by generic read hydration.",
            "blockers": candidate["blockers"],
        }
    if validation_errors:
        return {
            "action": "ask_for_parameters",
            "reason": "Hydration requires additional supplied parameters.",
            "missing_or_invalid": validation_errors,
        }
    if not provider_status["can_hydrate"] and latest_observation is None:
        return {
            "action": "configure_provider_or_use_existing_graph",
            "reason": "No hydrated state exists and the selected provider client is unavailable.",
            "provider": provider_status["provider"],
        }
    if latest_observation is None:
        return {
            "action": "hydrate_endpoint",
            "reason": "No runtime observation exists for this endpoint/provider.",
        }

    freshness = latest_observation.get("freshness") or {}
    age_seconds = freshness.get("age_seconds")
    state = freshness.get("state") or "unknown"
    stale_by_target = (
        isinstance(age_seconds, int)
        and freshness_target >= 0
        and age_seconds >= freshness_target
    )
    if state == "fresh" and not stale_by_target and fact_count:
        return {
            "action": "use_materialized_facts",
            "reason": "Fresh materialized facts already exist.",
        }
    if state == "fresh" and not stale_by_target:
        return {
            "action": "materialize_existing_observation",
            "reason": "A fresh observation exists but no materialized facts were found.",
        }
    if not provider_status["can_hydrate"]:
        return {
            "action": "use_stale_observation_or_configure_provider",
            "reason": "Existing hydrated state is stale or unknown and the provider client is unavailable.",
            "freshness": freshness,
        }
    return {
        "action": "refresh_hydration",
        "reason": "Existing hydrated state is stale, unknown, or older than the requested freshness target.",
        "freshness": freshness,
    }


def _hydrate_call_plan(
    *,
    candidate: dict[str, Any],
    provider_key: str,
    query_params: dict[str, Any],
    path_params: dict[str, Any],
) -> dict[str, Any]:
    return {
        "tool": "hydrate_runtime_endpoint",
        "args": {
            "endpoint_id": candidate["endpoint_id"],
            "provider": provider_key,
            "query_params": query_params,
            "path_params": path_params,
        },
    }


def _materialize_call_plan(latest_observation: dict[str, Any] | None) -> dict[str, Any] | None:
    if not latest_observation:
        return None
    observation_id = latest_observation.get("observation_id") or ""
    if not observation_id:
        return None
    return {
        "tool": "materialize_runtime_facts",
        "args": {"observation_id": observation_id},
    }


def _next_best_plan_action(plans: list[dict[str, Any]]) -> dict[str, Any]:
    if not plans:
        return {
            "action": "search_api_graph",
            "reason": "No hydratable GET endpoints matched the request.",
        }
    priority = [
        "use_materialized_facts",
        "materialize_existing_observation",
        "hydrate_endpoint",
        "refresh_hydration",
        "ask_for_parameters",
        "use_stale_observation_or_configure_provider",
        "configure_provider_or_use_existing_graph",
        "unsupported",
    ]
    by_action = {
        (plan.get("recommended_action") or {}).get("action"): plan
        for plan in plans
    }
    for action in priority:
        plan = by_action.get(action)
        if plan:
            return {
                "action": action,
                "endpoint_id": plan["endpoint"]["endpoint_id"],
                "reason": plan["recommended_action"].get("reason", ""),
            }
    first = plans[0]
    recommended = first.get("recommended_action") or {}
    return {
        "action": recommended.get("action", "inspect_plan"),
        "endpoint_id": first["endpoint"]["endpoint_id"],
        "reason": recommended.get("reason", ""),
    }


def _require_graph(graph_manager: "GraphManager | None") -> "GraphManager":
    if graph_manager is None or not getattr(graph_manager, "is_available", False):
        raise ToolError("Graph database is unavailable.")
    return graph_manager


def _resolve_endpoint(
    graph_manager: "GraphManager",
    *,
    endpoint_id: str,
    path: str,
) -> dict[str, Any]:
    clean_endpoint_id = endpoint_id.strip()
    clean_path = _normalise_graph_path(path)
    if clean_endpoint_id:
        rows = graph_manager.query(
            "MATCH (e:ApiEndpoint {endpoint_id: $endpoint_id}) "
            "RETURN e.endpoint_id AS endpoint_id, e.method AS method, "
            "e.path AS path, e.summary AS summary, e.operationId AS operationId, "
            "e.category AS category LIMIT 1",
            params={"endpoint_id": clean_endpoint_id},
            read_only=True,
        )
    elif clean_path:
        rows = graph_manager.query(
            "MATCH (e:ApiEndpoint {method: 'GET', path: $path}) "
            "RETURN e.endpoint_id AS endpoint_id, e.method AS method, "
            "e.path AS path, e.summary AS summary, e.operationId AS operationId, "
            "e.category AS category LIMIT 1",
            params={"path": clean_path},
            read_only=True,
        )
    else:
        raise ToolError("Provide endpoint_id or path.")

    if not rows:
        target = clean_endpoint_id or f"GET:{clean_path}"
        raise ToolError(f"API endpoint not found in graph: {target}")
    row = rows[0]
    if not row.get("endpoint_id"):
        row["endpoint_id"] = f"{str(row.get('method') or '').upper()}:{row.get('path')}"
    return row


def _select_api_client(
    provider: str,
    central_client: "BaseAPIClient | None",
    greenlake_client: "BaseAPIClient | None",
) -> "BaseAPIClient":
    provider_key = _normalise_provider(provider)
    if provider_key == "central":
        if central_client is None:
            raise ToolError(
                "Central credentials are not configured; runtime hydration can "
                "classify endpoints but cannot call live Central APIs."
            )
        return central_client
    if greenlake_client is None:
        raise ToolError("GreenLake credentials are not configured.")
    return greenlake_client


def _validate_hydration_inputs(
    *,
    endpoint: dict[str, Any],
    parameters: list[dict[str, Any]],
    query_params: dict[str, Any],
    path_params: dict[str, Any],
) -> list[str]:
    errors: list[str] = []
    path = str(endpoint.get("path") or "")
    placeholders = set(_PATH_PARAM_RE.findall(path))
    for name in sorted(placeholders):
        if name not in path_params or path_params.get(name) in (None, ""):
            errors.append(f"Missing required path parameter: {name!r}.")

    supplied_query = set(query_params.keys())
    for row in parameters:
        if not _truthy(row.get("required")):
            continue
        name = str(row.get("name") or "")
        location = str(row.get("location") or "").lower()
        if not name:
            continue
        if location == "query" and name not in supplied_query:
            errors.append(f"Missing required query parameter: {name!r}.")
        elif location == "path" and name not in path_params:
            errors.append(f"Missing required path parameter: {name!r}.")
        elif location not in {"query", "path"}:
            errors.append(
                f"Required {location or 'unknown'} parameter {name!r} is not "
                "supported by generic runtime hydration yet."
            )
    return sorted(set(errors))


def _apply_path_params(path: str, path_params: dict[str, Any]) -> str:
    def replace(match: re.Match[str]) -> str:
        name = match.group(1)
        return str(path_params.get(name, match.group(0))).strip("/")

    return _PATH_PARAM_RE.sub(replace, path)


def _query_response_property_map(
    graph_manager: "GraphManager",
    response_root: dict[str, Any],
) -> dict[str, dict[str, Any]]:
    component_id = response_root.get("component_id") if response_root else None
    if not component_id:
        return {}
    queries = [
        "MATCH (root:SchemaComponent {component_id: $component_id})"
        "-[:HAS_PROPERTY]->(p:Property) "
        "RETURN DISTINCT p.name AS name, p.type AS type, "
        "p.property_id AS property_id, p.parent_component_id AS parent_component_id "
        "ORDER BY name LIMIT 500",
        "MATCH (root:SchemaComponent {component_id: $component_id}) "
        "MATCH (root)-[:COMPOSED_OF*0..5]->(c:SchemaComponent)-[:HAS_PROPERTY]->(p:Property) "
        "RETURN DISTINCT p.name AS name, p.type AS type, "
        "p.property_id AS property_id, p.parent_component_id AS parent_component_id "
        "ORDER BY name LIMIT 500",
    ]
    properties: dict[str, dict[str, Any]] = {}
    for query in queries:
        try:
            rows = graph_manager.query(
                query,
                params={"component_id": component_id},
                read_only=True,
            )
        except Exception:
            continue
        for row in rows:
            name = str(row.get("name") or "")
            if name and name not in properties:
                properties[name] = row
    return properties


def _persist_hydration_failure(
    *,
    graph: "GraphManager",
    endpoint: dict[str, Any],
    provider: str,
    request_payload: dict[str, Any],
    duration_ms: int,
    error: str,
    response_hash: str = "",
    response_bytes: int = 0,
) -> str:
    run_id = _new_run_id()
    graph.execute(
        "CREATE (:HydrationRun {run_id: $run_id, provider: $provider, "
        "endpoint_id: $endpoint_id, method: 'GET', path: $path, "
        "parametersJson: $parameters_json, scopeJson: $scope_json, "
        "status: 'error', startedAt: current_timestamp(), "
        "finishedAt: current_timestamp(), durationMs: $duration_ms, "
        "requestHash: $request_hash, responseHash: $response_hash, "
        "responseBytes: $response_bytes, itemCount: 0, "
        "paginationStyle: 'single_call', error: $error})",
        {
            "run_id": run_id,
            "provider": provider,
            "endpoint_id": endpoint["endpoint_id"],
            "path": request_payload.get("path") or endpoint.get("path") or "",
            "parameters_json": _stable_json(request_payload.get("query_params") or {}),
            "scope_json": _stable_json(request_payload.get("path_params") or {}),
            "duration_ms": duration_ms,
            "request_hash": _sha256(_stable_json(request_payload)),
            "response_hash": response_hash,
            "response_bytes": response_bytes,
            "error": error[:1000],
        },
    )
    _link_run_to_endpoint(graph, run_id, endpoint["endpoint_id"])
    return run_id


def _persist_hydration_success(
    *,
    graph: "GraphManager",
    endpoint: dict[str, Any],
    provider: str,
    request_payload: dict[str, Any],
    duration_ms: int,
    response: Any,
    raw_json: str,
    response_hash: str,
    response_bytes: int,
    response_root: dict[str, Any],
    stale_after_seconds: int,
    observed_objects: list[dict[str, Any]],
) -> tuple[str, str]:
    run_id = _new_run_id()
    observation_id = f"{run_id}:observation"
    component_id = response_root.get("component_id") or ""
    item_count = _item_count(response)
    graph.execute(
        "CREATE (:HydrationRun {run_id: $run_id, provider: $provider, "
        "endpoint_id: $endpoint_id, method: 'GET', path: $path, "
        "parametersJson: $parameters_json, scopeJson: $scope_json, "
        "status: 'success', startedAt: current_timestamp(), "
        "finishedAt: current_timestamp(), durationMs: $duration_ms, "
        "requestHash: $request_hash, responseHash: $response_hash, "
        "responseBytes: $response_bytes, itemCount: $item_count, "
        "paginationStyle: 'single_call', error: ''})",
        {
            "run_id": run_id,
            "provider": provider,
            "endpoint_id": endpoint["endpoint_id"],
            "path": request_payload.get("path") or endpoint.get("path") or "",
            "parameters_json": _stable_json(request_payload.get("query_params") or {}),
            "scope_json": _stable_json(request_payload.get("path_params") or {}),
            "duration_ms": duration_ms,
            "request_hash": _sha256(_stable_json(request_payload)),
            "response_hash": response_hash,
            "response_bytes": response_bytes,
            "item_count": item_count,
        },
    )
    graph.execute(
        "CREATE (:RuntimeObservation {observation_id: $observation_id, "
        "run_id: $run_id, endpoint_id: $endpoint_id, provider: $provider, "
        "observedAt: current_timestamp(), status: 'success', "
        "contentType: 'application/json', rootKind: $root_kind, "
        "rawJson: $raw_json, responseHash: $response_hash, "
        "responseBytes: $response_bytes, itemCount: $item_count, "
        "schema_component_id: $schema_component_id, "
        "staleAfterSeconds: $stale_after_seconds})",
        {
            "observation_id": observation_id,
            "run_id": run_id,
            "endpoint_id": endpoint["endpoint_id"],
            "provider": provider,
            "root_kind": _value_kind(response),
            "raw_json": raw_json,
            "response_hash": response_hash,
            "response_bytes": response_bytes,
            "item_count": item_count,
            "schema_component_id": component_id,
            "stale_after_seconds": stale_after_seconds,
        },
    )
    _link_run_to_endpoint(graph, run_id, endpoint["endpoint_id"])
    graph.execute(
        "MATCH (run:HydrationRun {run_id: $run_id}), "
        "(obs:RuntimeObservation {observation_id: $observation_id}) "
        "MERGE (run)-[:PRODUCED_OBSERVATION]->(obs)",
        {"run_id": run_id, "observation_id": observation_id},
    )
    if component_id:
        graph.execute(
            "MATCH (obs:RuntimeObservation {observation_id: $observation_id}), "
            "(schema:SchemaComponent {component_id: $component_id}) "
            "MERGE (obs)-[:OBSERVATION_OF_SCHEMA]->(schema)",
            {"observation_id": observation_id, "component_id": component_id},
        )
    for index, obj in enumerate(observed_objects):
        _persist_observed_object(
            graph=graph,
            observation_id=observation_id,
            endpoint_id=endpoint["endpoint_id"],
            component_id=component_id,
            index=index,
            observed=obj,
        )
    return run_id, observation_id


def _link_run_to_endpoint(graph: "GraphManager", run_id: str, endpoint_id: str) -> None:
    graph.execute(
        "MATCH (run:HydrationRun {run_id: $run_id}), "
        "(endpoint:ApiEndpoint {endpoint_id: $endpoint_id}) "
        "MERGE (run)-[:CALLED_API]->(endpoint)",
        {"run_id": run_id, "endpoint_id": endpoint_id},
    )


def _persist_observed_object(
    *,
    graph: "GraphManager",
    observation_id: str,
    endpoint_id: str,
    component_id: str,
    index: int,
    observed: dict[str, Any],
) -> None:
    object_id = f"{observation_id}:object:{index}"
    object_component_id = _resolve_observed_object_component(graph, component_id, observed)
    graph.execute(
        "CREATE (:RuntimeObservedObject {object_id: $object_id, "
        "observation_id: $observation_id, endpoint_id: $endpoint_id, "
        "jsonPointer: $json_pointer, schema_component_id: $schema_component_id, "
        "itemIndex: $item_index, identityJson: $identity_json, "
        "valueType: $value_type, rawJson: $raw_json})",
        {
            "object_id": object_id,
            "observation_id": observation_id,
            "endpoint_id": endpoint_id,
            "json_pointer": observed["jsonPointer"],
            "schema_component_id": object_component_id,
            "item_index": observed.get("itemIndex", -1),
            "identity_json": observed["identityJson"],
            "value_type": observed["valueType"],
            "raw_json": observed["rawJson"],
        },
    )
    graph.execute(
        "MATCH (obs:RuntimeObservation {observation_id: $observation_id}), "
        "(obj:RuntimeObservedObject {object_id: $object_id}) "
        "MERGE (obs)-[:OBSERVATION_HAS_OBJECT]->(obj)",
        {"observation_id": observation_id, "object_id": object_id},
    )
    if object_component_id:
        graph.execute(
            "MATCH (obj:RuntimeObservedObject {object_id: $object_id}), "
            "(schema:SchemaComponent {component_id: $component_id}) "
            "MERGE (obj)-[:OBSERVED_OBJECT_SCHEMA]->(schema)",
            {"object_id": object_id, "component_id": object_component_id},
        )
    for field_index, field in enumerate(observed["fields"]):
        _persist_observed_field(
            graph=graph,
            object_id=object_id,
            observation_id=observation_id,
            endpoint_id=endpoint_id,
            field_index=field_index,
            field=field,
        )


def _resolve_observed_object_component(
    graph: "GraphManager",
    component_id: str,
    observed: dict[str, Any],
) -> str:
    if not component_id:
        return ""
    item_key = _observed_item_key(observed)
    if not item_key:
        return component_id
    try:
        rows = graph.query(
            "MATCH (root:SchemaComponent {component_id: $component_id})"
            "-[:HAS_PROPERTY]->(prop:Property {name: $item_key}) "
            "OPTIONAL MATCH (prop)-[:HAS_ITEM_SCHEMA]->(item:SchemaComponent) "
            "OPTIONAL MATCH (prop)-[:PROPERTY_OF_TYPE]->(typed:SchemaComponent) "
            "RETURN coalesce(item.component_id, typed.component_id, '') AS component_id "
            "LIMIT 1",
            params={"component_id": component_id, "item_key": item_key},
            read_only=True,
        )
    except Exception:
        return component_id
    resolved = str((rows[0] if rows else {}).get("component_id") or "")
    return resolved or component_id


def _observed_item_key(observed: dict[str, Any]) -> str:
    if _safe_int(observed.get("itemIndex")) < 0:
        return ""
    pointer = str(observed.get("jsonPointer") or "")
    parts = [part for part in pointer.split("/") if part]
    return parts[0] if len(parts) >= 2 else ""


def _persist_observed_field(
    *,
    graph: "GraphManager",
    object_id: str,
    observation_id: str,
    endpoint_id: str,
    field_index: int,
    field: dict[str, Any],
) -> None:
    field_id = f"{object_id}:field:{field_index}"
    graph.execute(
        "CREATE (:RuntimeObservedField {field_id: $field_id, "
        "object_id: $object_id, observation_id: $observation_id, "
        "endpoint_id: $endpoint_id, property_id: $property_id, "
        "name: $name, jsonPointer: $json_pointer, valueJson: $value_json, "
        "scalarType: $scalar_type})",
        {
            "field_id": field_id,
            "object_id": object_id,
            "observation_id": observation_id,
            "endpoint_id": endpoint_id,
            "property_id": field.get("property_id") or "",
            "name": field["name"],
            "json_pointer": field["jsonPointer"],
            "value_json": field["valueJson"],
            "scalar_type": field["scalarType"],
        },
    )
    graph.execute(
        "MATCH (obj:RuntimeObservedObject {object_id: $object_id}), "
        "(field:RuntimeObservedField {field_id: $field_id}) "
        "MERGE (obj)-[:OBSERVED_OBJECT_HAS_FIELD]->(field)",
        {"object_id": object_id, "field_id": field_id},
    )
    if field.get("property_id"):
        graph.execute(
            "MATCH (field:RuntimeObservedField {field_id: $field_id}), "
            "(prop:Property {property_id: $property_id}) "
            "MERGE (field)-[:OBSERVED_FIELD_PROPERTY]->(prop)",
            {"field_id": field_id, "property_id": field["property_id"]},
        )


def _extract_observed_objects(
    response: Any,
    property_map: dict[str, dict[str, Any]],
) -> list[dict[str, Any]]:
    if isinstance(response, list):
        return [
            _observed_object(item, f"/{idx}", idx, property_map)
            for idx, item in enumerate(response)
        ]
    if isinstance(response, dict):
        item_key = _detect_item_key(response)
        if item_key:
            return [
                _observed_object(item, f"/{item_key}/{idx}", idx, property_map)
                for idx, item in enumerate(response[item_key])
            ]
        return [_observed_object(response, "", -1, property_map)]
    return [_observed_object(response, "", -1, property_map)]


def _observed_object(
    value: Any,
    pointer: str,
    item_index: int,
    property_map: dict[str, dict[str, Any]],
) -> dict[str, Any]:
    fields = []
    if isinstance(value, dict):
        for name, field_value in sorted(value.items()):
            prop = property_map.get(name, {})
            fields.append(
                {
                    "name": name,
                    "jsonPointer": _join_pointer(pointer, name),
                    "valueJson": _stable_json(field_value),
                    "scalarType": _value_kind(field_value),
                    "property_id": prop.get("property_id") or "",
                }
            )
    return {
        "jsonPointer": pointer,
        "itemIndex": item_index,
        "identityJson": _stable_json(_extract_identity(value)),
        "valueType": _value_kind(value),
        "rawJson": _stable_json(value),
        "fields": fields,
    }


def _query_runtime_observations(
    graph_manager: "GraphManager",
    *,
    endpoint_id: str,
    provider: str,
    limit: int,
) -> list[dict[str, Any]]:
    where = []
    params: dict[str, Any] = {}
    if endpoint_id.strip():
        where.append("obs.endpoint_id = $endpoint_id")
        params["endpoint_id"] = endpoint_id.strip()
    if provider.strip():
        where.append("obs.provider = $provider")
        params["provider"] = provider.strip()
    where_clause = "WHERE " + " AND ".join(where) + " " if where else ""
    return graph_manager.query(
        "MATCH (obs:RuntimeObservation) "
        f"{where_clause}"
        "RETURN obs.observation_id AS observation_id, obs.run_id AS run_id, "
        "obs.endpoint_id AS endpoint_id, obs.provider AS provider, "
        "obs.observedAt AS observedAt, obs.status AS status, "
        "obs.rootKind AS rootKind, obs.responseHash AS responseHash, "
        "obs.responseBytes AS responseBytes, obs.itemCount AS itemCount, "
        "obs.schema_component_id AS schema_component_id, "
        "obs.staleAfterSeconds AS staleAfterSeconds, obs.rawJson AS rawJson "
        f"ORDER BY obs.observedAt DESC LIMIT {limit}",
        params=params,
        read_only=True,
    )


def _query_runtime_observation(
    graph_manager: "GraphManager",
    observation_id: str,
) -> dict[str, Any]:
    rows = graph_manager.query(
        "MATCH (obs:RuntimeObservation {observation_id: $observation_id}) "
        "RETURN obs.observation_id AS observation_id, obs.run_id AS run_id, "
        "obs.endpoint_id AS endpoint_id, obs.provider AS provider, "
        "obs.observedAt AS observedAt, obs.status AS status, "
        "obs.contentType AS contentType, obs.rootKind AS rootKind, "
        "obs.responseHash AS responseHash, obs.responseBytes AS responseBytes, "
        "obs.itemCount AS itemCount, obs.schema_component_id AS schema_component_id, "
        "obs.staleAfterSeconds AS staleAfterSeconds, obs.rawJson AS rawJson "
        "LIMIT 1",
        params={"observation_id": observation_id},
        read_only=True,
    )
    return rows[0] if rows else {}


def _query_runtime_observed_objects(
    graph_manager: "GraphManager",
    observation_id: str,
    limit: int,
    offset: int = 0,
) -> list[dict[str, Any]]:
    return graph_manager.query(
        "MATCH (:RuntimeObservation {observation_id: $observation_id})"
        "-[:OBSERVATION_HAS_OBJECT]->(obj:RuntimeObservedObject) "
        "RETURN obj.object_id AS object_id, obj.jsonPointer AS jsonPointer, "
        "obj.schema_component_id AS schema_component_id, obj.itemIndex AS itemIndex, "
        "obj.identityJson AS identityJson, obj.valueType AS valueType, "
        "obj.rawJson AS rawJson "
        f"ORDER BY obj.itemIndex, obj.jsonPointer SKIP {max(0, offset)} LIMIT {limit}",
        params={"observation_id": observation_id},
        read_only=True,
    )


def _query_runtime_observed_fields(
    graph_manager: "GraphManager",
    observation_id: str,
    limit: int,
) -> list[dict[str, Any]]:
    return graph_manager.query(
        "MATCH (:RuntimeObservation {observation_id: $observation_id})"
        "-[:OBSERVATION_HAS_OBJECT]->(:RuntimeObservedObject)"
        "-[:OBSERVED_OBJECT_HAS_FIELD]->(field:RuntimeObservedField) "
        "RETURN field.field_id AS field_id, field.object_id AS object_id, "
        "field.property_id AS property_id, field.name AS name, "
        "field.jsonPointer AS jsonPointer, field.valueJson AS valueJson, "
        "field.scalarType AS scalarType "
        f"ORDER BY field.jsonPointer LIMIT {limit}",
        params={"observation_id": observation_id},
        read_only=True,
    )


def _materialize_fact_from_object(
    graph: "GraphManager",
    observation: dict[str, Any],
    obj: dict[str, Any],
) -> tuple[dict[str, Any] | None, str]:
    identity = _safe_json_object(obj.get("identityJson"))
    if not identity:
        return None, "identity_unknown"

    attributes = _safe_json_object(obj.get("rawJson"))
    if not attributes:
        return None, "attributes_not_object"

    identity_key, identity_field = _identity_key(identity)
    if not identity_key:
        return None, "identity_ambiguous"

    entity_type = _derive_entity_type(observation, obj)
    fact_id = _fact_id(
        endpoint_id=str(observation.get("endpoint_id") or obj.get("endpoint_id") or ""),
        entity_type=entity_type,
        identity_key=identity_key,
        observation_id=str(observation.get("observation_id") or ""),
        object_id=str(obj.get("object_id") or ""),
    )
    confidence = _fact_confidence(identity_field, attributes)
    graph.execute(
        "MERGE (fact:RuntimeFact {fact_id: $fact_id}) "
        "SET fact.observation_id = $observation_id, fact.object_id = $object_id, "
        "fact.endpoint_id = $endpoint_id, fact.entityType = $entity_type, "
        "fact.identityKey = $identity_key, fact.attributesJson = $attributes_json, "
        "fact.confidence = $confidence, fact.materializedAt = current_timestamp()",
        {
            "fact_id": fact_id,
            "observation_id": observation.get("observation_id") or "",
            "object_id": obj.get("object_id") or "",
            "endpoint_id": observation.get("endpoint_id") or obj.get("endpoint_id") or "",
            "entity_type": entity_type,
            "identity_key": identity_key,
            "attributes_json": _stable_json(attributes),
            "confidence": confidence,
        },
    )
    _link_fact_provenance(
        graph=graph,
        fact_id=fact_id,
        observation_id=str(observation.get("observation_id") or ""),
        object_id=str(obj.get("object_id") or ""),
        run_id=str(observation.get("run_id") or ""),
        endpoint_id=str(observation.get("endpoint_id") or obj.get("endpoint_id") or ""),
    )
    return (
        {
            "fact_id": fact_id,
            "observation_id": observation.get("observation_id") or "",
            "object_id": obj.get("object_id") or "",
            "endpoint_id": observation.get("endpoint_id") or obj.get("endpoint_id") or "",
            "entityType": entity_type,
            "identityKey": identity_key,
            "identityField": identity_field,
            "confidence": confidence,
        },
        "",
    )


def _link_fact_provenance(
    *,
    graph: "GraphManager",
    fact_id: str,
    observation_id: str,
    object_id: str,
    run_id: str,
    endpoint_id: str,
) -> None:
    graph.execute(
        "MATCH (obs:RuntimeObservation {observation_id: $observation_id}), "
        "(fact:RuntimeFact {fact_id: $fact_id}) "
        "MERGE (obs)-[:OBSERVATION_MATERIALIZED_FACT]->(fact)",
        {"observation_id": observation_id, "fact_id": fact_id},
    )
    graph.execute(
        "MATCH (fact:RuntimeFact {fact_id: $fact_id}), "
        "(obj:RuntimeObservedObject {object_id: $object_id}) "
        "MERGE (fact)-[:FACT_FROM_OBJECT]->(obj)",
        {"fact_id": fact_id, "object_id": object_id},
    )
    if run_id:
        graph.execute(
            "MATCH (fact:RuntimeFact {fact_id: $fact_id}), "
            "(run:HydrationRun {run_id: $run_id}) "
            "MERGE (fact)-[:FACT_FROM_RUN]->(run)",
            {"fact_id": fact_id, "run_id": run_id},
        )
    if endpoint_id:
        graph.execute(
            "MATCH (fact:RuntimeFact {fact_id: $fact_id}), "
            "(endpoint:ApiEndpoint {endpoint_id: $endpoint_id}) "
            "MERGE (fact)-[:FACT_FROM_API]->(endpoint)",
            {"fact_id": fact_id, "endpoint_id": endpoint_id},
        )


def _query_runtime_facts(
    graph_manager: "GraphManager",
    *,
    endpoint_id: str,
    entity_type: str,
    identity_key: str,
    limit: int,
) -> list[dict[str, Any]]:
    where = []
    params: dict[str, Any] = {}
    if endpoint_id.strip():
        where.append("fact.endpoint_id = $endpoint_id")
        params["endpoint_id"] = endpoint_id.strip()
    if entity_type.strip():
        where.append("fact.entityType = $entity_type")
        params["entity_type"] = entity_type.strip()
    if identity_key.strip():
        where.append("fact.identityKey = $identity_key")
        params["identity_key"] = identity_key.strip()
    where_clause = "WHERE " + " AND ".join(where) + " " if where else ""
    return graph_manager.query(
        "MATCH (fact:RuntimeFact) "
        f"{where_clause}"
        "RETURN fact.fact_id AS fact_id, fact.observation_id AS observation_id, "
        "fact.object_id AS object_id, fact.endpoint_id AS endpoint_id, "
        "fact.entityType AS entityType, fact.identityKey AS identityKey, "
        "fact.confidence AS confidence, fact.materializedAt AS materializedAt, "
        "fact.attributesJson AS attributesJson "
        f"ORDER BY fact.materializedAt DESC LIMIT {limit}",
        params=params,
        read_only=True,
    )


def _query_runtime_fact(
    graph_manager: "GraphManager",
    fact_id: str,
) -> dict[str, Any]:
    rows = graph_manager.query(
        "MATCH (fact:RuntimeFact {fact_id: $fact_id}) "
        "OPTIONAL MATCH (obs:RuntimeObservation)-[:OBSERVATION_MATERIALIZED_FACT]->(fact) "
        "OPTIONAL MATCH (fact)-[:FACT_FROM_OBJECT]->(obj:RuntimeObservedObject) "
        "OPTIONAL MATCH (fact)-[:FACT_FROM_RUN]->(run:HydrationRun) "
        "OPTIONAL MATCH (fact)-[:FACT_FROM_API]->(endpoint:ApiEndpoint) "
        "RETURN fact.fact_id AS fact_id, fact.observation_id AS observation_id, "
        "fact.object_id AS object_id, fact.endpoint_id AS endpoint_id, "
        "fact.entityType AS entityType, fact.identityKey AS identityKey, "
        "fact.confidence AS confidence, fact.materializedAt AS materializedAt, "
        "fact.attributesJson AS attributesJson, "
        "obs.observation_id AS provenance_observation_id, "
        "obj.object_id AS provenance_object_id, "
        "run.run_id AS provenance_run_id, "
        "endpoint.endpoint_id AS provenance_endpoint_id, "
        "endpoint.path AS provenance_endpoint_path "
        "LIMIT 1",
        params={"fact_id": fact_id},
        read_only=True,
    )
    if not rows:
        return {}
    row = rows[0]
    fact = {
        key: row.get(key)
        for key in (
            "fact_id",
            "observation_id",
            "object_id",
            "endpoint_id",
            "entityType",
            "identityKey",
            "confidence",
            "materializedAt",
            "attributesJson",
        )
    }
    return {
        "fact": fact,
        "provenance": {
            "observation_id": row.get("provenance_observation_id") or row.get("observation_id"),
            "object_id": row.get("provenance_object_id") or row.get("object_id"),
            "run_id": row.get("provenance_run_id"),
            "endpoint_id": row.get("provenance_endpoint_id") or row.get("endpoint_id"),
            "endpoint_path": row.get("provenance_endpoint_path"),
        },
    }


def _annotate_observation_freshness(row: dict[str, Any]) -> dict[str, Any]:
    annotated = dict(row)
    observed_at = _coerce_datetime(row.get("observedAt"))
    stale_after = _safe_int(row.get("staleAfterSeconds"))
    if observed_at is None:
        state = "unknown"
        age_seconds = None
        stale_at = None
    else:
        age_seconds = max(0, int((datetime.now(timezone.utc) - observed_at).total_seconds()))
        stale_at_dt = observed_at if stale_after <= 0 else observed_at.timestamp() + stale_after
        stale_at = (
            observed_at.isoformat()
            if stale_after <= 0
            else datetime.fromtimestamp(stale_at_dt, timezone.utc).isoformat()
        )
        state = "stale" if stale_after <= 0 or age_seconds >= stale_after else "fresh"
    annotated["freshness"] = {
        "state": state,
        "age_seconds": age_seconds,
        "stale_after_seconds": stale_after,
        "stale_at": stale_at,
    }
    return annotated


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


def _normalise_graph_path(path: str) -> str:
    clean = (path or "").strip()
    if not clean:
        return ""
    return clean if clean.startswith("/") else f"/{clean}"


def _new_run_id() -> str:
    return f"hydration:{uuid.uuid4()}"


def _stable_json(value: Any) -> str:
    return json.dumps(value, sort_keys=True, separators=(",", ":"), default=str)


def _sha256(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


def _value_kind(value: Any) -> str:
    if value is None:
        return "null"
    if isinstance(value, bool):
        return "boolean"
    if isinstance(value, int | float):
        return "number"
    if isinstance(value, str):
        return "string"
    if isinstance(value, list):
        return "array"
    if isinstance(value, dict):
        return "object"
    return type(value).__name__


def _detect_item_key(value: dict[str, Any]) -> str:
    if isinstance(value.get("items"), list):
        return "items"
    best_key = ""
    best_len = 0
    for key, candidate in value.items():
        if not isinstance(candidate, list) or key == "errors":
            continue
        if len(candidate) > best_len:
            best_key = str(key)
            best_len = len(candidate)
    return best_key


def _item_count(value: Any) -> int:
    if isinstance(value, list):
        return len(value)
    if isinstance(value, dict):
        item_key = _detect_item_key(value)
        if item_key:
            return len(value[item_key])
        return 1
    return 1


def _extract_identity(value: Any) -> dict[str, Any]:
    if not isinstance(value, dict):
        return {}
    identity = {}
    lower_lookup = {str(key).lower(): key for key in value.keys()}
    for name in _IDENTITY_FIELD_NAMES:
        key = value.get(name)
        if key not in (None, ""):
            identity[name] = key
            continue
        actual_key = lower_lookup.get(name.lower())
        if actual_key and value.get(actual_key) not in (None, ""):
            identity[str(actual_key)] = value[actual_key]
    return identity


def _identity_key(identity: dict[str, Any]) -> tuple[str, str]:
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
        "siteId",
        "deviceGroupId",
        "name",
    ]
    lower_to_actual = {key.lower(): key for key in identity.keys()}
    for field in priority:
        actual = lower_to_actual.get(field.lower())
        if actual and identity.get(actual) not in (None, ""):
            return f"{actual}={identity[actual]}", actual
    if len(identity) == 1:
        key, value = next(iter(identity.items()))
        return f"{key}={value}", str(key)
    if identity:
        return "identityHash=" + _sha256(_stable_json(identity))[:16], "identityHash"
    return "", ""


def _derive_entity_type(observation: dict[str, Any], obj: dict[str, Any]) -> str:
    schema_id = str(
        obj.get("schema_component_id")
        or observation.get("schema_component_id")
        or ""
    )
    if schema_id:
        tail = schema_id.rsplit(":", 1)[-1].split("#", 1)[0]
        if tail:
            return tail
    endpoint_id = str(observation.get("endpoint_id") or obj.get("endpoint_id") or "")
    if endpoint_id:
        path = endpoint_id.split(":", 1)[-1].strip("/")
        segment = path.rsplit("/", 1)[-1]
        if segment:
            return segment
    return "RuntimeObject"


def _fact_id(
    *,
    endpoint_id: str,
    entity_type: str,
    identity_key: str,
    observation_id: str,
    object_id: str,
) -> str:
    payload = {
        "endpoint_id": endpoint_id,
        "entity_type": entity_type,
        "identity_key": identity_key,
        "observation_id": observation_id,
        "object_id": object_id,
    }
    return "fact:" + _sha256(_stable_json(payload))


def _fact_confidence(identity_field: str, attributes: dict[str, Any]) -> str:
    high = {"id", "uuid", "serial", "serialNumber", "mac", "macaddr", "macAddress"}
    medium = {"serial_number", "scopeId", "siteId", "deviceGroupId", "name"}
    if identity_field in high:
        return "high"
    if identity_field in medium:
        return "medium"
    if len(attributes) >= 2:
        return "medium"
    return "low"


def _safe_json_object(value: Any) -> dict[str, Any]:
    if isinstance(value, dict):
        return value
    if not isinstance(value, str) or not value.strip():
        return {}
    try:
        parsed = json.loads(value)
    except (TypeError, ValueError):
        parsed = _parse_ladybug_map_string(value)
    return parsed if isinstance(parsed, dict) else {}


def _parse_ladybug_map_string(value: str) -> dict[str, Any]:
    """Parse Ladybug's flat map string representation as a best-effort fallback."""
    text = value.strip()
    if not (text.startswith("{") and text.endswith("}")):
        return {}
    body = text[1:-1].strip()
    if not body:
        return {}

    parsed: dict[str, Any] = {}
    for item in _split_top_level_map_items(body):
        if ":" not in item:
            return {}
        key, raw = item.split(":", 1)
        key = key.strip().strip("\"'")
        if not key:
            return {}
        parsed[key] = _parse_ladybug_scalar(raw.strip())
    return parsed


def _split_top_level_map_items(value: str) -> list[str]:
    items: list[str] = []
    start = 0
    depth = 0
    quote = ""
    for idx, char in enumerate(value):
        if quote:
            if char == quote:
                quote = ""
            continue
        if char in {"'", '"'}:
            quote = char
        elif char in "{[":
            depth += 1
        elif char in "}]":
            depth = max(0, depth - 1)
        elif char == "," and depth == 0:
            items.append(value[start:idx].strip())
            start = idx + 1
    tail = value[start:].strip()
    if tail:
        items.append(tail)
    return items


def _parse_ladybug_scalar(value: str) -> Any:
    clean = value.strip()
    if clean.startswith("{") and clean.endswith("}"):
        nested = _parse_ladybug_map_string(clean)
        if nested:
            return nested
    if clean.startswith("[") and clean.endswith("]"):
        return clean
    lowered = clean.lower()
    if lowered == "null":
        return None
    if lowered == "true":
        return True
    if lowered == "false":
        return False
    try:
        if "." in clean:
            return float(clean)
        return int(clean)
    except ValueError:
        return clean.strip("\"'")


def _coerce_datetime(value: Any) -> datetime | None:
    if isinstance(value, datetime):
        return value if value.tzinfo else value.replace(tzinfo=timezone.utc)
    if isinstance(value, str) and value.strip():
        text = value.strip().replace("Z", "+00:00")
        try:
            parsed = datetime.fromisoformat(text)
        except ValueError:
            return None
        return parsed if parsed.tzinfo else parsed.replace(tzinfo=timezone.utc)
    return None


def _safe_int(value: Any) -> int:
    try:
        return int(value)
    except (TypeError, ValueError):
        return 0


def _join_pointer(base: str, token: str) -> str:
    escaped = token.replace("~", "~0").replace("/", "~1")
    return f"{base}/{escaped}" if base else f"/{escaped}"


def _clamp_int(value: int, *, default: int, minimum: int, maximum: int) -> int:
    try:
        parsed = int(value)
    except (TypeError, ValueError):
        return default
    return max(minimum, min(parsed, maximum))


def _clamp_limit(limit: int) -> int:
    try:
        parsed = int(limit)
    except (TypeError, ValueError):
        return _DEFAULT_CANDIDATE_LIMIT
    if parsed <= 0:
        return _DEFAULT_CANDIDATE_LIMIT
    return min(parsed, _MAX_CANDIDATE_LIMIT)


def _clamp_limit_with_max(limit: int, default: int, maximum: int) -> int:
    try:
        parsed = int(limit)
    except (TypeError, ValueError):
        return default
    if parsed <= 0:
        return default
    return min(parsed, maximum)


def _ddl_table_names(ddls: list[str]) -> list[str]:
    names = []
    for ddl in ddls:
        words = ddl.replace("(", " ").split()
        if "EXISTS" in words:
            idx = words.index("EXISTS")
            if idx + 1 < len(words):
                names.append(words[idx + 1])
    return names
