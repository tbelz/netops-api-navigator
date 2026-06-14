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
        self.executions: list[tuple[str, dict[str, Any] | None]] = []

    def query(
        self,
        cypher: str,
        params: dict[str, Any] | None = None,
        *,
        read_only: bool = True,
    ) -> list[dict[str, Any]]:
        self.queries.append((cypher, params, read_only))
        if "PRODUCED_OBSERVATION" in cypher and "HydrationRun" in cypher:
            if (params or {}).get("provider") != "central":
                return []
            if (params or {}).get("parameters_json") != '{"limit":"2","offset":"0"}':
                return []
            if (params or {}).get("scope_json") != "{}":
                return []
            return [
                {
                    "observation_id": "obs-1",
                    "run_id": "run-1",
                    "endpoint_id": "GET:/monitoring/v1/devices",
                    "provider": "central",
                    "observedAt": "2026-06-14T10:00:00Z",
                    "status": "success",
                    "contentType": "application/json",
                    "rootKind": "object",
                    "responseHash": "abc123",
                    "responseBytes": 200,
                    "itemCount": 1,
                    "schema_component_id": "central:schemas:DeviceList",
                    "staleAfterSeconds": 300,
                    "rawJson": '{"items":[]}',
                }
            ]
        if "MATCH (fact:RuntimeFact" in cypher and "OPTIONAL MATCH" in cypher:
            return [
                {
                    "fact_id": "fact-1",
                    "observation_id": "obs-1",
                    "object_id": "obj-1",
                    "endpoint_id": "GET:/monitoring/v1/devices",
                    "entityType": "DeviceList",
                    "identityKey": "serial=SN1",
                    "confidence": "high",
                    "materializedAt": "2026-06-14T10:01:00Z",
                    "attributesJson": '{"serial":"SN1"}',
                    "provenance_observation_id": "obs-1",
                    "provenance_object_id": "obj-1",
                    "provenance_run_id": "run-1",
                    "provenance_endpoint_id": "GET:/monitoring/v1/devices",
                    "provenance_endpoint_path": "/monitoring/v1/devices",
                }
            ]
        if "MATCH (fact:RuntimeFact" in cypher:
            return [
                {
                    "fact_id": "fact-1",
                    "observation_id": "obs-1",
                    "object_id": "obj-1",
                    "endpoint_id": "GET:/monitoring/v1/devices",
                    "entityType": "DeviceList",
                    "identityKey": "serial=SN1",
                    "confidence": "high",
                    "materializedAt": "2026-06-14T10:01:00Z",
                    "attributesJson": '{"serial":"SN1"}',
                }
            ]
        if (
            "MATCH (obs:RuntimeObservation {observation_id: $observation_id})" in cypher
            and (params or {}).get("observation_id") != "obs-1"
        ):
            return []
        if "MATCH (obs:RuntimeObservation" in cypher:
            return [
                {
                    "observation_id": "obs-1",
                    "run_id": "run-1",
                    "endpoint_id": "GET:/monitoring/v1/devices",
                    "provider": "central",
                    "observedAt": "2026-06-14T10:00:00Z",
                    "status": "success",
                    "contentType": "application/json",
                    "rootKind": "object",
                    "responseHash": "abc123",
                    "responseBytes": 200,
                    "itemCount": 1,
                    "schema_component_id": "central:schemas:DeviceList",
                    "staleAfterSeconds": 300,
                    "rawJson": '{"items":[]}',
                }
            ]
        if "OBSERVED_OBJECT_HAS_FIELD" in cypher:
            return [
                {
                    "field_id": "field-1",
                    "object_id": "obj-1",
                    "property_id": "central:schemas:Device#prop:serial",
                    "name": "serial",
                    "jsonPointer": "/items/0/serial",
                    "valueJson": '"SN1"',
                    "scalarType": "string",
                }
            ]
        if "OBSERVATION_HAS_OBJECT" in cypher and "RuntimeObservedObject" in cypher:
            return [
                {
                    "object_id": "obj-1",
                    "jsonPointer": "/items/0",
                    "schema_component_id": "central:schemas:DeviceList",
                    "itemIndex": 0,
                    "identityJson": '{"serial":"SN1"}',
                    "valueType": "object",
                    "rawJson": '{"serial":"SN1"}',
                }
            ]
        if (
            "ApiEndpoint" in cypher
            and "e.endpoint_id AS endpoint_id" in cypher
            and "c.spec_source = $spec_source" in cypher
            and (params or {}).get("spec_source") != "central"
        ):
            return []
        if "ApiEndpoint" in cypher and "e.endpoint_id AS endpoint_id" in cypher:
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

    def execute(
        self,
        cypher: str,
        params: dict[str, Any] | None = None,
    ) -> list[dict[str, Any]]:
        self.executions.append((cypher, params))
        return []


class UnavailableGraph:
    is_available = False


class FreshPlanningGraph(FakeHydrationGraph):
    def query(
        self,
        cypher: str,
        params: dict[str, Any] | None = None,
        *,
        read_only: bool = True,
    ) -> list[dict[str, Any]]:
        rows = super().query(cypher, params=params, read_only=read_only)
        if "RuntimeObservation" in cypher:
            for row in rows:
                row["observedAt"] = "2999-01-01T00:00:00Z"
                row["staleAfterSeconds"] = 3600
        return rows


class ParameterizedHydrationGraph(FakeHydrationGraph):
    def query(
        self,
        cypher: str,
        params: dict[str, Any] | None = None,
        *,
        read_only: bool = True,
    ) -> list[dict[str, Any]]:
        self.queries.append((cypher, params, read_only))
        if "PRODUCED_OBSERVATION" in cypher and "HydrationRun" in cypher:
            return []
        if "MATCH (fact:RuntimeFact" in cypher:
            return []
        if "MATCH (obs:RuntimeObservation" in cypher:
            return []
        if "ApiEndpoint" in cypher and "e.endpoint_id AS endpoint_id" in cypher:
            return [
                {
                    "endpoint_id": "GET:/monitoring/v1/devices/{serial}",
                    "method": "GET",
                    "path": "/monitoring/v1/devices/{serial}",
                    "summary": "Get device",
                    "operationId": "getDevice",
                    "category": "Monitoring",
                }
            ]
        if "HAS_PARAMETER" in cypher:
            return [
                {
                    "name": "serial",
                    "location": "path",
                    "required": True,
                    "type": "string",
                    "format": "",
                }
            ]
        if "HAS_RESPONSE" in cypher:
            return [
                {
                    "status": "200",
                    "content_type": "application/json",
                    "component_id": "central:schemas:Device",
                    "schema_name": "Device",
                    "body_shape": "object",
                    "schema_type": "object",
                }
            ]
        if "HAS_PROPERTY" in cypher:
            return [
                {
                    "name": "serial",
                    "type": "string",
                    "parent_component_id": "central:schemas:Device",
                    "property_id": "central:schemas:Device#prop:serial",
                }
            ]
        return []


class FakeAPIClient:
    def __init__(self) -> None:
        self.requests: list[dict[str, Any]] = []

    def _request(
        self,
        method: str,
        path: str,
        params: dict[str, str] | None = None,
        json_body: dict | None = None,
    ) -> dict[str, Any]:
        self.requests.append(
            {
                "method": method,
                "path": path,
                "params": params,
                "json_body": json_body,
            }
        )
        return {
            "items": [
                {"serial": "SN1", "macAddress": "aa:bb:cc:dd:ee:01", "status": "Up"},
                {"serial": "SN2", "macAddress": "aa:bb:cc:dd:ee:02", "status": "Down"},
            ]
        }


class LadybugHydrationGraph:
    is_available = True

    def __init__(self, conn) -> None:
        self.conn = conn

    def query(
        self,
        cypher: str,
        params: dict[str, Any] | None = None,
        *,
        read_only: bool = True,
    ) -> list[dict[str, Any]]:
        return list(self.conn.execute(cypher, parameters=params or {}).rows_as_dict())

    def execute(
        self,
        cypher: str,
        params: dict[str, Any] | None = None,
    ) -> list[dict[str, Any]]:
        return list(self.conn.execute(cypher, parameters=params or {}).rows_as_dict())


def _make_tools(
    graph_manager: object | None = None,
    central_client: object | None = None,
    greenlake_client: object | None = None,
    settings: Settings | None = None,
) -> dict[str, object]:
    mcp = FastMCP("test-runtime-hydration")
    register_runtime_hydration_tools(
        mcp,
        settings or Settings(runtime_hydration=True),
        graph_manager=graph_manager,
        central_client=central_client,
        greenlake_client=greenlake_client,
    )
    return {tool.name: tool.fn for tool in mcp._tool_manager._tools.values()}


def _bootstrap_hydration_conn(conn) -> None:
    for ddl in (
        NODE_TABLES
        + KNOWLEDGE_NODE_TABLES
        + HYDRATION_NODE_TABLES
        + REL_TABLES
        + KNOWLEDGE_REL_TABLES
        + HYDRATION_REL_TABLES
    ):
        conn.execute(ddl.strip())


def _seed_hydratable_endpoint(conn) -> None:
    conn.execute(
        "CREATE (:ApiEndpoint {endpoint_id: 'GET:/monitoring/v1/devices', "
        "method: 'GET', path: '/monitoring/v1/devices', summary: '', "
        "description: '', operationId: '', category: 'Monitoring', "
        "deprecated: false, parameters: '', requestBody: '', responses: ''})"
    )
    conn.execute(
        "CREATE (:Response {response_id: 'response-1', "
        "endpoint_id: 'GET:/monitoring/v1/devices', status: '200', "
        "content_type: 'application/json', "
        "root_component_ref: 'central:schemas:DeviceList'})"
    )
    conn.execute(
        "CREATE (:SchemaComponent {component_id: 'central:schemas:DeviceList', "
        "spec_source: 'central', section: 'schemas', name: 'DeviceList', "
        "type: 'object', kind: 'object', bodyShape: 'object'})"
    )
    conn.execute(
        "CREATE (:SchemaComponent {component_id: 'central:schemas:Device', "
        "spec_source: 'central', section: 'schemas', name: 'Device', "
        "type: 'object', kind: 'object', bodyShape: 'object'})"
    )
    conn.execute(
        "CREATE (:Property {property_id: 'central:schemas:DeviceList#prop:items', "
        "parent_component_id: 'central:schemas:DeviceList', name: 'items', "
        "type: 'array', required: false})"
    )
    conn.execute(
        "CREATE (:Property {property_id: 'central:schemas:DeviceList#prop:serial', "
        "parent_component_id: 'central:schemas:DeviceList', name: 'serial', "
        "type: 'string', required: false})"
    )
    conn.execute(
        "CREATE (:Property {property_id: 'central:schemas:Device#prop:serial', "
        "parent_component_id: 'central:schemas:Device', name: 'serial', "
        "type: 'string', required: false})"
    )
    conn.execute(
        "MATCH (endpoint:ApiEndpoint {endpoint_id: 'GET:/monitoring/v1/devices'}), "
        "(response:Response {response_id: 'response-1'}), "
        "(schema:SchemaComponent {component_id: 'central:schemas:DeviceList'}), "
        "(device:SchemaComponent {component_id: 'central:schemas:Device'}), "
        "(items:Property {property_id: 'central:schemas:DeviceList#prop:items'}), "
        "(root_serial:Property {property_id: 'central:schemas:DeviceList#prop:serial'}), "
        "(device_serial:Property {property_id: 'central:schemas:Device#prop:serial'}) "
        "CREATE (endpoint)-[:HAS_RESPONSE]->(response), "
        "(response)-[:RESPONSE_REFERENCES]->(schema), "
        "(schema)-[:HAS_PROPERTY]->(items), "
        "(schema)-[:HAS_PROPERTY]->(root_serial), "
        "(items)-[:HAS_ITEM_SCHEMA]->(device), "
        "(device)-[:HAS_PROPERTY]->(device_serial)"
    )


def test_runtime_hydration_shell_tool_surface() -> None:
    tools = _make_tools()

    assert set(tools) == {
        "get_runtime_hydration_status",
        "list_runtime_hydration_candidates",
        "hydrate_runtime_endpoint",
        "list_runtime_observations",
        "get_runtime_observation",
        "get_runtime_hydration_state",
        "list_runtime_hydration_providers",
        "plan_runtime_hydration",
        "materialize_runtime_facts",
        "list_runtime_facts",
        "get_runtime_fact",
    }


def test_runtime_hydration_status_is_honest_about_unimplemented_capabilities() -> None:
    tools = _make_tools(graph_manager=FakeHydrationGraph())

    parsed = json.loads(tools["get_runtime_hydration_status"]())

    assert parsed["enabled"] is True
    assert parsed["stage"] == "planning"
    assert parsed["capabilities"] == [
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
    ]
    assert parsed["implemented"] == {
        "generic_endpoint_hydration": True,
        "observation_persistence_schema": True,
        "observation_persistence_runtime": True,
        "materialization": True,
        "provider_readiness": True,
        "planning_helpers": True,
    }
    assert parsed["graph_available"] is True
    assert parsed["clients_available"]["central"] is False
    assert parsed["providers"][0]["provider"] == "central"
    assert parsed["providers"][0]["can_hydrate"] is False
    assert "HydrationRun" in parsed["schema"]["node_tables"]
    assert "RuntimeObservation" in parsed["schema"]["node_tables"]
    assert "CALLED_API" in parsed["schema"]["relationship_tables"]


def test_list_runtime_hydration_providers_reports_explicit_boundaries() -> None:
    tools = _make_tools(
        graph_manager=FakeHydrationGraph(),
        central_client=FakeAPIClient(),
        greenlake_client=FakeAPIClient(),
        settings=Settings(
            runtime_hydration=True,
            central_base_url="https://central.example.test",
            central_client_id="central-id",
            central_client_secret="central-secret",
            glp_client_id="glp-id",
            glp_client_secret="glp-secret",
        ),
    )

    parsed = json.loads(tools["list_runtime_hydration_providers"]())

    assert parsed["default_provider"] == "central"
    providers = {row["provider"]: row for row in parsed["providers"]}
    assert providers["central"]["aliases"] == ["central", "aruba", "aruba-central"]
    assert providers["central"]["configured"] is True
    assert providers["central"]["can_hydrate"] is True
    assert providers["greenlake"]["aliases"] == ["greenlake", "glp", "hpe-greenlake"]
    assert providers["greenlake"]["configured"] is True
    assert providers["greenlake"]["can_hydrate"] is True


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


def test_plan_runtime_hydration_prefers_fresh_materialized_facts() -> None:
    graph = FreshPlanningGraph()
    tools = _make_tools(graph_manager=graph, central_client=FakeAPIClient())

    parsed = json.loads(
        tools["plan_runtime_hydration"](
            question="Which devices are monitored?",
            search="device",
            query_params={"limit": "2", "offset": "0"},
        )
    )

    assert parsed["provider"] == "central"
    assert parsed["provider_ready"]["can_hydrate"] is True
    assert parsed["total"] == 1
    plan = parsed["plans"][0]
    assert plan["endpoint"]["endpoint_id"] == "GET:/monitoring/v1/devices"
    assert plan["candidate"]["execution_shape"] == "collection"
    assert plan["latest_observation"]["freshness"]["state"] == "fresh"
    assert plan["materialized_fact_count"] == 1
    assert plan["recommended_action"]["action"] == "use_materialized_facts"
    assert parsed["next_best_action"]["action"] == "use_materialized_facts"
    assert plan["hydrate_call"]["args"]["query_params"] == {"limit": "2", "offset": "0"}
    assert plan["materialize_call"]["args"] == {"observation_id": "obs-1"}
    scoped_queries = [params for cypher, params, _ in graph.queries if "PRODUCED_OBSERVATION" in cypher]
    assert scoped_queries
    assert scoped_queries[-1]["parameters_json"] == '{"limit":"2","offset":"0"}'
    assert scoped_queries[-1]["scope_json"] == "{}"
    assert all(read_only is True for _, _, read_only in graph.queries)
    assert graph.executions == []


def test_plan_runtime_hydration_does_not_reuse_different_parameter_scope() -> None:
    graph = FreshPlanningGraph()
    tools = _make_tools(graph_manager=graph, central_client=FakeAPIClient())

    parsed = json.loads(
        tools["plan_runtime_hydration"](
            endpoint_id="GET:/monitoring/v1/devices",
            query_params={"limit": "2", "offset": "50"},
        )
    )

    plan = parsed["plans"][0]
    assert plan["latest_observation"] is None
    assert plan["materialized_fact_count"] == 0
    assert plan["recommended_action"]["action"] == "hydrate_endpoint"
    assert plan["hydrate_call"]["args"]["query_params"] == {"limit": "2", "offset": "50"}
    scoped_queries = [params for cypher, params, _ in graph.queries if "PRODUCED_OBSERVATION" in cypher]
    assert scoped_queries[-1]["parameters_json"] == '{"limit":"2","offset":"50"}'


def test_plan_runtime_hydration_explains_missing_path_parameters() -> None:
    graph = ParameterizedHydrationGraph()
    tools = _make_tools(graph_manager=graph, central_client=FakeAPIClient())

    parsed = json.loads(
        tools["plan_runtime_hydration"](
            endpoint_id="GET:/monitoring/v1/devices/{serial}",
            provider="aruba-central",
        )
    )

    plan = parsed["plans"][0]
    assert parsed["provider"] == "central"
    assert plan["candidate"]["execution_shape"] == "parameterized_lookup"
    assert plan["candidate"]["blockers"] == []
    assert plan["recommended_action"]["action"] == "ask_for_parameters"
    assert "serial" in "\n".join(plan["validation_errors"])
    assert plan["hydrate_call"]["args"]["endpoint_id"] == "GET:/monitoring/v1/devices/{serial}"
    assert graph.executions == []


def test_plan_runtime_hydration_reports_missing_provider_for_unhydrated_endpoint() -> None:
    graph = ParameterizedHydrationGraph()
    tools = _make_tools(graph_manager=graph)

    parsed = json.loads(
        tools["plan_runtime_hydration"](
            endpoint_id="GET:/monitoring/v1/devices/{serial}",
            provider="glp",
            path_params={"serial": "SN1"},
        )
    )

    plan = parsed["plans"][0]
    assert parsed["provider"] == "greenlake"
    assert parsed["provider_ready"]["can_hydrate"] is False
    assert plan["endpoint"]["provider"] == "central"
    assert plan["validation_errors"] == []
    assert plan["recommended_action"]["action"] == "select_matching_provider"
    assert plan["hydrate_call"] is None
    assert parsed["next_best_action"]["action"] == "select_matching_provider"
    assert graph.executions == []


def test_plan_runtime_hydration_filters_search_results_by_provider() -> None:
    graph = FakeHydrationGraph()
    tools = _make_tools(graph_manager=graph, greenlake_client=FakeAPIClient())

    parsed = json.loads(
        tools["plan_runtime_hydration"](
            search="device",
            provider="greenlake",
        )
    )

    assert parsed["provider"] == "greenlake"
    assert parsed["plans"] == []
    assert parsed["next_best_action"]["action"] == "search_api_graph"
    provider_queries = [params for cypher, params, _ in graph.queries if "c.spec_source = $spec_source" in cypher]
    assert provider_queries
    assert provider_queries[0]["spec_source"] == "glp"
    assert graph.executions == []


def test_hydrate_runtime_endpoint_persists_generic_observation() -> None:
    graph = FakeHydrationGraph()
    client = FakeAPIClient()
    tools = _make_tools(graph_manager=graph, central_client=client)

    parsed = json.loads(
        tools["hydrate_runtime_endpoint"](
            endpoint_id="GET:/monitoring/v1/devices",
            query_params={"limit": "2", "offset": "0"},
            stale_after_seconds=60,
        )
    )

    assert parsed["ok"] is True
    assert parsed["endpoint_id"] == "GET:/monitoring/v1/devices"
    assert parsed["item_count"] == 2
    assert parsed["observed_object_count"] == 2
    assert parsed["stale_after_seconds"] == 60
    assert client.requests == [
        {
            "method": "GET",
            "path": "monitoring/v1/devices",
            "params": {"limit": "2", "offset": "0"},
            "json_body": None,
        }
    ]
    executed_cypher = "\n".join(cypher for cypher, _ in graph.executions)
    assert "CREATE (:HydrationRun" in executed_cypher
    assert "CREATE (:RuntimeObservation" in executed_cypher
    assert "CREATE (:RuntimeObservedObject" in executed_cypher
    assert "CREATE (:RuntimeObservedField" in executed_cypher
    assert "CALLED_API" in executed_cypher
    assert "PRODUCED_OBSERVATION" in executed_cypher


def test_hydrate_runtime_endpoint_persists_to_ladybug_graph() -> None:
    with TemporaryDirectory(prefix="runtime_hydration_executor_") as tmp:
        db = lb.Database(str(Path(tmp) / "graph_db"), max_db_size=256 * 1024 * 1024)
        conn = lb.Connection(db)
        _bootstrap_hydration_conn(conn)
        _seed_hydratable_endpoint(conn)
        graph = LadybugHydrationGraph(conn)
        tools = _make_tools(graph_manager=graph, central_client=FakeAPIClient())

        hydrated = json.loads(
            tools["hydrate_runtime_endpoint"](
                endpoint_id="GET:/monitoring/v1/devices",
                query_params={"limit": "2"},
            )
        )

        assert hydrated["ok"] is True
        listed = json.loads(
            tools["list_runtime_observations"](
                endpoint_id="GET:/monitoring/v1/devices",
                include_raw=True,
            )
        )
        assert listed["total"] == 1
        observation_id = listed["observations"][0]["observation_id"]
        detail = json.loads(
            tools["get_runtime_observation"](
                observation_id=observation_id,
                include_raw=True,
            )
        )
        assert detail["observation"]["responseHash"] == hydrated["response_hash"]
        assert len(detail["objects"]) == 2
        assert {field["name"] for field in detail["fields"]} >= {"serial"}
        assert any(field["property_id"] for field in detail["fields"])

        materialized = json.loads(
            tools["materialize_runtime_facts"](observation_id=observation_id)
        )
        assert materialized["materialized_count"] == 2
        fact_id = materialized["materialized"][0]["fact_id"]
        fact_detail = json.loads(tools["get_runtime_fact"](fact_id=fact_id))
        assert fact_detail["fact"]["identityKey"].startswith("serial=SN")
        assert fact_detail["fact"]["entityType"] == "Device"
        assert fact_detail["fact"]["confidence"] == "high"
        assert fact_detail["provenance"]["observation_id"] == observation_id
        assert fact_detail["provenance"]["run_id"] == hydrated["run_id"]
        assert fact_detail["provenance"]["endpoint_id"] == "GET:/monitoring/v1/devices"

        rows = list(
            conn.execute(
                "MATCH (:RuntimeFact)-[:FACT_FROM_RUN]->(:HydrationRun) "
                "RETURN COUNT(*) AS n"
            ).rows_as_dict()
        )
        assert rows == [{"n": 2}]


def test_hydrate_runtime_endpoint_requires_live_client() -> None:
    tools = _make_tools(graph_manager=FakeHydrationGraph())

    with pytest.raises(Exception, match="Central credentials are not configured"):
        tools["hydrate_runtime_endpoint"](endpoint_id="GET:/monitoring/v1/devices")


def test_runtime_observation_lookup_tools_hide_raw_by_default() -> None:
    graph = FakeHydrationGraph()
    tools = _make_tools(graph_manager=graph)

    listed = json.loads(
        tools["list_runtime_observations"](
            endpoint_id="GET:/monitoring/v1/devices",
            limit=5,
        )
    )
    assert listed["total"] == 1
    assert listed["observations"][0]["observation_id"] == "obs-1"
    assert "rawJson" not in listed["observations"][0]

    detail = json.loads(tools["get_runtime_observation"](observation_id="obs-1"))
    assert detail["observation"]["observation_id"] == "obs-1"
    assert detail["objects"][0]["identityJson"] == '{"serial":"SN1"}'
    assert detail["fields"][0]["name"] == "serial"
    assert "rawJson" not in detail["observation"]
    assert "rawJson" not in detail["objects"][0]


def test_runtime_hydration_state_reports_missing_and_existing_state() -> None:
    missing_tools = _make_tools(graph_manager=UnavailableGraph())
    with pytest.raises(Exception, match="Graph database is unavailable"):
        missing_tools["get_runtime_hydration_state"](endpoint_id="GET:/monitoring/v1/devices")

    tools = _make_tools(graph_manager=FakeHydrationGraph())
    parsed = json.loads(
        tools["get_runtime_hydration_state"](endpoint_id="GET:/monitoring/v1/devices")
    )

    assert parsed["endpoint_id"] == "GET:/monitoring/v1/devices"
    assert parsed["state"] in {"fresh", "stale", "unknown"}
    assert parsed["latest_observation"]["freshness"]["state"] == parsed["state"]
    assert "rawJson" not in parsed["latest_observation"]


def test_runtime_observation_filters_normalize_provider_aliases() -> None:
    graph = FakeHydrationGraph()
    tools = _make_tools(graph_manager=graph)

    json.loads(
        tools["list_runtime_observations"](
            endpoint_id="GET:/monitoring/v1/devices",
            provider="aruba-central",
        )
    )
    json.loads(
        tools["get_runtime_hydration_state"](
            endpoint_id="GET:/monitoring/v1/devices",
            provider="aruba-central",
        )
    )
    json.loads(
        tools["materialize_runtime_facts"](
            endpoint_id="GET:/monitoring/v1/devices",
            provider="aruba-central",
        )
    )

    provider_params = [
        params["provider"]
        for cypher, params, _ in graph.queries
        if params and "provider" in params and "RuntimeObservation" in cypher
    ]
    assert provider_params
    assert set(provider_params) == {"central"}


def test_materialize_runtime_facts_writes_generic_facts_with_provenance() -> None:
    graph = FakeHydrationGraph()
    tools = _make_tools(graph_manager=graph)

    parsed = json.loads(tools["materialize_runtime_facts"](observation_id="obs-1"))

    assert parsed["materialized_count"] == 1
    assert parsed["object_limit"] == 1000
    assert parsed["has_more_objects"] is False
    fact = parsed["materialized"][0]
    assert fact["endpoint_id"] == "GET:/monitoring/v1/devices"
    assert fact["identityKey"] == "serial=SN1"
    assert fact["confidence"] == "high"
    executed_cypher = "\n".join(cypher for cypher, _ in graph.executions)
    assert "MERGE (fact:RuntimeFact" in executed_cypher
    assert "OBSERVATION_MATERIALIZED_FACT" in executed_cypher
    assert "FACT_FROM_OBJECT" in executed_cypher
    assert "FACT_FROM_RUN" in executed_cypher
    assert "FACT_FROM_API" in executed_cypher
    object_queries = [cypher for cypher, _, _ in graph.queries if "OBSERVATION_HAS_OBJECT" in cypher]
    assert object_queries
    assert "SKIP 0 LIMIT 1001" in object_queries[-1]


def test_materialize_runtime_facts_raises_for_unknown_explicit_observation() -> None:
    tools = _make_tools(graph_manager=FakeHydrationGraph())

    with pytest.raises(Exception, match="Runtime observation not found: missing"):
        tools["materialize_runtime_facts"](observation_id="missing")


def test_runtime_fact_lookup_tools_hide_attributes_by_default() -> None:
    tools = _make_tools(graph_manager=FakeHydrationGraph())

    listed = json.loads(
        tools["list_runtime_facts"](endpoint_id="GET:/monitoring/v1/devices")
    )
    assert listed["total"] == 1
    assert listed["facts"][0]["fact_id"] == "fact-1"
    assert "attributesJson" not in listed["facts"][0]

    detail = json.loads(tools["get_runtime_fact"](fact_id="fact-1"))
    assert detail["fact"]["fact_id"] == "fact-1"
    assert "attributesJson" not in detail["fact"]
    assert detail["provenance"]["observation_id"] == "obs-1"
    assert detail["provenance"]["run_id"] == "run-1"
    assert detail["provenance"]["endpoint_id"] == "GET:/monitoring/v1/devices"

    detail_with_attributes = json.loads(
        tools["get_runtime_fact"](fact_id="fact-1", include_attributes=True)
    )
    assert detail_with_attributes["fact"]["attributesJson"] == '{"serial":"SN1"}'


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
    assert candidate["blockers"] == []
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
        "FACT_FROM_OBJECT",
        "FACT_FROM_RUN",
        "FACT_FROM_API",
    ):
        assert rel in rel_tables

    assert ("CALLED_API", "HydrationRun", "ApiEndpoint") in rel_endpoints
    assert (
        "OBSERVED_FIELD_PROPERTY",
        "RuntimeObservedField",
        "Property",
    ) in rel_endpoints
    assert ("FACT_FROM_RUN", "RuntimeFact", "HydrationRun") in rel_endpoints
    assert ("FACT_FROM_API", "RuntimeFact", "ApiEndpoint") in rel_endpoints
    assert ("FACT_FROM_OBJECT", "RuntimeFact", "RuntimeObservedObject") in rel_endpoints
    assert "rawJson" in node_props["RuntimeObservation"]
    assert "identityJson" in node_props["RuntimeObservedObject"]
    assert "attributesJson" in node_props["RuntimeFact"]


def test_runtime_hydration_schema_bootstraps_in_ladybug() -> None:
    with TemporaryDirectory(prefix="runtime_hydration_schema_") as tmp:
        db = lb.Database(str(Path(tmp) / "graph_db"), max_db_size=256 * 1024 * 1024)
        conn = lb.Connection(db)
        _bootstrap_hydration_conn(conn)

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
