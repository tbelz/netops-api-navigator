"""Tests for the opt-in runtime hydration foundation."""

from __future__ import annotations

import json
from pathlib import Path
from tempfile import TemporaryDirectory
from typing import Any

import pytest
import real_ladybug as lb
from mcp.server.fastmcp import FastMCP

from hpe_networking_central_mcp.config import Settings
from hpe_networking_central_mcp.graph.schema import (
    HYDRATION_NODE_TABLES,
    HYDRATION_REL_TABLES,
    KNOWLEDGE_NODE_TABLES,
    KNOWLEDGE_REL_TABLES,
    NODE_TABLES,
    REL_TABLES,
    get_node_properties,
    get_node_tables,
    get_rel_tables,
    get_rel_tables_with_endpoints,
)
from hpe_networking_central_mcp.tools.hydration import (
    classify_hydration_candidate,
    register_runtime_hydration_tools,
)

pytestmark = pytest.mark.unit


class FakeHydrationGraph:
    """Small query fixture for graph-backed candidate discovery."""

    is_available = True

    def __init__(self) -> None:
        self.queries: list[tuple[str, dict[str, Any] | None, bool]] = []

    def query(
        self,
        cypher: str,
        params: dict[str, Any] | None = None,
        *,
        read_only: bool = True,
    ) -> list[dict[str, Any]]:
        self.queries.append((cypher, params, read_only))
        if "MATCH (e:ApiEndpoint)" in cypher:
            return [
                {
                    "endpoint_id": "GET:/monitoring/v1/devices",
                    "method": "GET",
                    "path": "/monitoring/v1/devices",
                    "summary": "List devices",
                    "operationId": "listDevices",
                    "category": "Monitoring",
                }
            ]
        if "HAS_PARAMETER" in cypher:
            return [
                {
                    "name": "limit",
                    "location": "query",
                    "required": False,
                    "type": "integer",
                    "format": "",
                },
                {
                    "name": "offset",
                    "location": "query",
                    "required": False,
                    "type": "integer",
                    "format": "",
                },
            ]
        if "HAS_RESPONSE" in cypher:
            return [
                {
                    "status": "200",
                    "content_type": "application/json",
                    "component_id": "central:schemas:DeviceList",
                    "schema_name": "DeviceList",
                    "body_shape": "array",
                    "schema_type": "array",
                }
            ]
        if "HAS_PROPERTY" in cypher:
            return [
                {
                    "name": "serial",
                    "type": "string",
                    "parent_component_id": "central:schemas:Device",
                    "property_id": "central:schemas:Device#prop:serial",
                },
                {
                    "name": "macAddress",
                    "type": "string",
                    "parent_component_id": "central:schemas:Device",
                    "property_id": "central:schemas:Device#prop:macAddress",
                },
            ]
        return []


class UnavailableGraph:
    is_available = False


def _make_tools(graph_manager: object | None = None) -> dict[str, object]:
    mcp = FastMCP("test-runtime-hydration")
    register_runtime_hydration_tools(
        mcp,
        Settings(runtime_hydration=True),
        graph_manager=graph_manager,
    )
    return {tool.name: tool.fn for tool in mcp._tool_manager._tools.values()}


def test_runtime_hydration_shell_tool_surface() -> None:
    tools = _make_tools()

    assert set(tools) == {
        "get_runtime_hydration_status",
        "list_runtime_hydration_candidates",
    }


def test_runtime_hydration_status_is_honest_about_unimplemented_capabilities() -> None:
    tools = _make_tools(graph_manager=FakeHydrationGraph())

    parsed = json.loads(tools["get_runtime_hydration_status"]())

    assert parsed["enabled"] is True
    assert parsed["stage"] == "foundation"
    assert parsed["capabilities"] == [
        "status",
        "list_read_hydration_candidates",
        "generic_observation_schema",
    ]
    assert parsed["implemented"] == {
        "generic_endpoint_hydration": False,
        "observation_persistence_schema": True,
        "observation_persistence_runtime": False,
        "materialization": False,
    }
    assert parsed["graph_available"] is True
    assert "HydrationRun" in parsed["schema"]["node_tables"]
    assert "RuntimeObservation" in parsed["schema"]["node_tables"]
    assert "CALLED_API" in parsed["schema"]["relationship_tables"]


def test_list_runtime_hydration_candidates_reads_graph_without_hydrating() -> None:
    graph = FakeHydrationGraph()
    tools = _make_tools(graph_manager=graph)

    parsed = json.loads(
        tools["list_runtime_hydration_candidates"](search="device", limit=250)
    )

    assert parsed["total"] == 1
    assert parsed["limit"] == 100
    candidate = parsed["candidates"][0]
    assert candidate["endpoint_id"] == "GET:/monitoring/v1/devices"
    assert candidate["hydration_candidate"] is True
    assert candidate["execution_shape"] == "collection"
    assert candidate["pagination"] == {
        "style": "offset",
        "confidence": "medium",
        "parameters": ["limit", "offset"],
    }
    assert candidate["identity"]["status"] == "candidate"
    assert candidate["identity"]["fields"] == ["serial", "macAddress"]
    assert candidate["confidence"] == "high"
    assert all(read_only is True for _, _, read_only in graph.queries)
    assert not any("CREATE " in cypher or "MERGE " in cypher for cypher, _, _ in graph.queries)


def test_list_runtime_hydration_candidates_reports_unavailable_graph() -> None:
    tools = _make_tools(graph_manager=UnavailableGraph())

    parsed = json.loads(tools["list_runtime_hydration_candidates"]())

    assert parsed == {
        "candidates": [],
        "total": 0,
        "error": "Graph database is unavailable.",
    }


def test_classifier_marks_parameterized_get_as_needing_scope() -> None:
    candidate = classify_hydration_candidate(
        endpoint={
            "endpoint_id": "GET:/monitoring/v1/devices/{serial}",
            "method": "GET",
            "path": "/monitoring/v1/devices/{serial}",
            "summary": "Get device",
            "operationId": "getDevice",
            "category": "Monitoring",
        },
        parameters=[
            {
                "name": "serial",
                "location": "path",
                "required": True,
                "type": "string",
                "format": "",
            }
        ],
        responses=[
            {
                "status": "200",
                "content_type": "application/json",
                "component_id": "central:schemas:Device",
                "schema_name": "Device",
                "body_shape": "object",
                "schema_type": "object",
            }
        ],
        identity_hints=[{"name": "serial", "type": "string"}],
    )

    assert candidate["hydration_candidate"] is True
    assert candidate["execution_shape"] == "parameterized_lookup"
    assert candidate["blockers"] == ["requires_path_parameters"]
    assert candidate["required_parameters"] == [
        {
            "name": "serial",
            "location": "path",
            "required": True,
            "type": "string",
            "format": "",
        }
    ]
    assert candidate["confidence"] == "medium"


def test_runtime_hydration_schema_is_registered_for_introspection() -> None:
    node_tables = set(get_node_tables())
    rel_tables = set(get_rel_tables())
    rel_endpoints = set(get_rel_tables_with_endpoints())
    node_props = get_node_properties()

    for table in (
        "HydrationRun",
        "RuntimeObservation",
        "RuntimeObservedObject",
        "RuntimeObservedField",
        "RuntimeFact",
    ):
        assert table in node_tables

    for rel in (
        "CALLED_API",
        "PRODUCED_OBSERVATION",
        "OBSERVATION_HAS_OBJECT",
        "OBSERVED_OBJECT_HAS_FIELD",
        "OBSERVED_FIELD_PROPERTY",
        "OBSERVATION_MATERIALIZED_FACT",
    ):
        assert rel in rel_tables

    assert ("CALLED_API", "HydrationRun", "ApiEndpoint") in rel_endpoints
    assert (
        "OBSERVED_FIELD_PROPERTY",
        "RuntimeObservedField",
        "Property",
    ) in rel_endpoints
    assert "rawJson" in node_props["RuntimeObservation"]
    assert "identityJson" in node_props["RuntimeObservedObject"]
    assert "attributesJson" in node_props["RuntimeFact"]


def test_runtime_hydration_schema_bootstraps_in_ladybug() -> None:
    with TemporaryDirectory(prefix="runtime_hydration_schema_") as tmp:
        db = lb.Database(str(Path(tmp) / "graph_db"), max_db_size=256 * 1024 * 1024)
        conn = lb.Connection(db)
        for ddl in (
            NODE_TABLES
            + KNOWLEDGE_NODE_TABLES
            + HYDRATION_NODE_TABLES
            + REL_TABLES
            + KNOWLEDGE_REL_TABLES
            + HYDRATION_REL_TABLES
        ):
            conn.execute(ddl.strip())

        conn.execute(
            "CREATE (:ApiEndpoint {endpoint_id: 'GET:/monitoring/v1/devices', "
            "method: 'GET', path: '/monitoring/v1/devices', summary: '', "
            "description: '', operationId: '', category: 'Monitoring', "
            "deprecated: false, parameters: '', requestBody: '', responses: ''})"
        )
        conn.execute(
            "CREATE (:HydrationRun {run_id: 'run-1', provider: 'central', "
            "endpoint_id: 'GET:/monitoring/v1/devices', method: 'GET', "
            "path: '/monitoring/v1/devices', status: 'planned'})"
        )
        conn.execute(
            "CREATE (:RuntimeObservation {observation_id: 'obs-1', run_id: 'run-1', "
            "endpoint_id: 'GET:/monitoring/v1/devices', provider: 'central', "
            "status: 'ok', rootKind: 'array', rawJson: '[]'})"
        )
        conn.execute(
            "MATCH (run:HydrationRun {run_id: 'run-1'}), "
            "(endpoint:ApiEndpoint {endpoint_id: 'GET:/monitoring/v1/devices'}), "
            "(obs:RuntimeObservation {observation_id: 'obs-1'}) "
            "CREATE (run)-[:CALLED_API]->(endpoint), "
            "(run)-[:PRODUCED_OBSERVATION]->(obs)"
        )

        rows = list(
            conn.execute(
                "MATCH (:HydrationRun)-[:PRODUCED_OBSERVATION]->"
                "(:RuntimeObservation) RETURN COUNT(*) AS n"
            ).rows_as_dict()
        )
        assert rows == [{"n": 1}]
