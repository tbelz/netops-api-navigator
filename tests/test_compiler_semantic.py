"""Tests for Task 3A: semantic overlay generation from the L1 AST."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from netops_api_navigator.compiler.ast_builder import build_ast_graph
from netops_api_navigator.compiler.frontend import clean_spec
from netops_api_navigator.compiler.semantic_builder import (
    IDENTITY_RULE_PACK_ID,
    STRUCTURAL_RULE_PACK_ID,
    SemanticEdge,
    SemanticGraph,
    SemanticNode,
    build_semantic_overlay,
    _internal_ref_pointer,
)
from netops_api_navigator.compiler.semantic_metrics import (
    compute_semantic_metrics,
    merge_semantic_metrics,
)

pytestmark = [pytest.mark.compiler, pytest.mark.unit]

_FIXTURE_DIR = Path(__file__).resolve().parent / "fixtures" / "oas" / "real_excerpts"


def _semantic_spec() -> dict:
    return {
        "openapi": "3.0.3",
        "info": {"title": "Semantic", "version": "1.0"},
        "paths": {
            "/ntp": {
                "parameters": [
                    {
                        "name": "device_id",
                        "in": "query",
                        "required": False,
                        "schema": {"type": "string"},
                    }
                ],
                "post": {
                    "operationId": "setNtp",
                    "summary": "Configure NTP",
                    "parameters": [
                        {"$ref": "#/components/parameters/SiteParam"},
                    ],
                    "x-cliParam": {
                        "commandName": "ntp server",
                        "commandUse": "configuration",
                        "paramKeys": [{"key": "server"}],
                    },
                    "requestBody": {"$ref": "#/components/requestBodies/NtpBody"},
                    "responses": {
                        "200": {
                            "description": "ok",
                            "content": {
                                "application/json": {
                                    "schema": {"$ref": "#/components/schemas/NtpResponse"}
                                },
                                "application/problem+json": {
                                    "schema": {"$ref": "#/components/schemas/NtpError"}
                                }
                            },
                        },
                        "204": {"description": "No content"},
                    },
                }
            }
        },
        "components": {
            "parameters": {
                "SiteParam": {
                    "name": "site_id",
                    "in": "query",
                    "required": False,
                    "schema": {"$ref": "#/components/schemas/SiteId"},
                }
            },
            "requestBodies": {
                "NtpBody": {
                    "required": True,
                    "content": {
                        "application/json": {
                            "schema": {"$ref": "#/components/schemas/NtpProfile"}
                        }
                    },
                }
            },
            "schemas": {
                "SiteId": {"type": "string", "format": "uuid"},
                "BaseConfig": {
                    "type": "object",
                    "properties": {
                        "enabled": {
                            "type": "boolean",
                            "x-path": "/ac-ntp:ntp/ac-ntp:enabled",
                        }
                    },
                },
                "NtpProfile": {
                    "allOf": [
                        {"$ref": "#/components/schemas/BaseConfig"},
                        {
                            "type": "object",
                            "required": ["server"],
                            "properties": {
                                "server": {
                                    "type": "string",
                                    "description": "NTP server address",
                                    "x-supportedDeviceType": ["Switch CX"],
                                    "x-path": "/ac-ntp:ntp/ac-ntp:server",
                                }
                            },
                        },
                    ]
                },
                "NtpResponse": {
                    "type": "object",
                    "properties": {"id": {"type": "string"}},
                },
                "NtpError": {
                    "type": "object",
                    "properties": {"message": {"type": "string"}},
                },
            }
        },
    }


def _semantic_spec_with_untyped_endpoint() -> dict:
    spec = _semantic_spec()
    spec["paths"]["/health"] = {
        "get": {
            "operationId": "health",
            "responses": {"204": {"description": "No content"}},
        }
    }
    return spec


def test_semantic_overlay_builds_agent_highways() -> None:
    ast_graph = build_ast_graph(_semantic_spec(), source="unit/semantic")
    semantic = build_semantic_overlay(ast_graph)

    assert semantic.rule_packs == (STRUCTURAL_RULE_PACK_ID, IDENTITY_RULE_PACK_ID)
    nodes_by_kind = {}
    for node in semantic.nodes:
        nodes_by_kind.setdefault(node.kind, []).append(node)
    assert {node.name for node in nodes_by_kind["ApiEndpoint"]} == {"POST /ntp"}
    assert {node.name for node in nodes_by_kind["CliCommand"]} == {"ntp server"}
    assert {node.name for node in nodes_by_kind["Parameter"]} == {
        "device_id",
        "site_id",
    }
    assert len(nodes_by_kind["RequestBody"]) == 1
    assert len(nodes_by_kind["Response"]) == 3
    assert {node.name for node in nodes_by_kind["YangPath"]} == {
        "/ac-ntp:ntp/ac-ntp:enabled",
        "/ac-ntp:ntp/ac-ntp:server",
    }
    assert len(nodes_by_kind["ModelEntity"]) >= len(nodes_by_kind["SchemaComponent"])

    node_by_id = {node.semantic_id: node for node in semantic.nodes}
    identity_by_id = {
        node.semantic_id: json.loads(node.summary_json)["identityKey"]
        for node in semantic.nodes
        if node.kind == "ModelEntity"
    }
    assert all(identity_by_id.values())
    model_keys = set(identity_by_id.values())
    edge_tuples = {
        (
            node_by_id[edge.source_id].name,
            edge.kind,
            node_by_id[edge.target_id].name,
        )
        for edge in semantic.edges
    }
    response_by_media = {
        json.loads(node.summary_json)["contentType"]: node
        for node in nodes_by_kind["Response"]
        if json.loads(node.summary_json)["status"] == "200"
    }
    response_reference_edges = {
        (
            json.loads(node_by_id[edge.source_id].summary_json)["contentType"],
            node_by_id[edge.target_id].name,
        )
        for edge in semantic.edges
        if edge.kind == "RESPONSE_REFERENCES"
        and node_by_id[edge.source_id].kind == "Response"
    }
    model_edge_tuples = {
        (
            identity_by_id.get(edge.source_id, node_by_id[edge.source_id].name),
            edge.kind,
            identity_by_id.get(edge.target_id, node_by_id[edge.target_id].name),
        )
        for edge in semantic.edges
    }
    assert ("POST /ntp", "HAS_PARAMETER", "device_id") in edge_tuples
    assert ("POST /ntp", "HAS_PARAMETER", "site_id") in edge_tuples
    assert ("site_id", "PARAMETER_REFERENCES", "SiteId") in edge_tuples
    assert ("POST /ntp", "HAS_REQUEST_BODY", "POST /ntp requestBody") in edge_tuples
    assert ("POST /ntp requestBody", "BODY_REFERENCES", "NtpProfile") in edge_tuples
    assert set(response_by_media) == {"application/json", "application/problem+json"}
    assert ("POST /ntp", "HAS_RESPONSE", "POST /ntp 200") in edge_tuples
    assert ("POST /ntp", "HAS_RESPONSE", "POST /ntp 204") in edge_tuples
    assert ("application/json", "NtpResponse") in response_reference_edges
    assert ("application/problem+json", "NtpError") in response_reference_edges
    assert ("POST /ntp", "ACCEPTS_SCHEMA", "NtpProfile") in edge_tuples
    assert ("POST /ntp", "RETURNS_SCHEMA", "NtpResponse") in edge_tuples
    assert ("POST /ntp", "RETURNS_SCHEMA", "NtpError") in edge_tuples
    assert ("POST /ntp", "HAS_CLI_COMMAND", "ntp server") in edge_tuples
    assert ("POST /ntp", "CONFIGURES_YANG", "/ac-ntp:ntp/ac-ntp:server") in edge_tuples
    assert "schema:ntpprofile" in model_keys
    assert "yang:/ac-ntp:ntp/ac-ntp:server" in model_keys
    assert ("POST /ntp", "ACCEPTS_MODEL", "schema:ntpprofile") in model_edge_tuples
    assert ("POST /ntp", "RETURNS_MODEL", "schema:ntpresponse") in model_edge_tuples
    assert (
        "schema:ntpprofile",
        "MODEL_COMPOSED_OF",
        "schema-pointer:/components/schemas/NtpProfile/allOf/1",
    ) in model_edge_tuples
    assert (
        "schema-pointer:/components/schemas/NtpProfile/allOf/1",
        "MODEL_HAS_PROPERTY",
        "yang:/ac-ntp:ntp/ac-ntp:server",
    ) in model_edge_tuples
    assert (
        "yang:/ac-ntp:ntp/ac-ntp:server",
        "MODEL_AT_YANG",
        "/ac-ntp:ntp/ac-ntp:server",
    ) in model_edge_tuples
    assert (
        "POST /ntp",
        "CONFIGURES_MODEL",
        "yang:/ac-ntp:ntp/ac-ntp:server",
    ) in model_edge_tuples
    # Transparent composition refs connect directly to their named target.
    assert ("NtpProfile", "COMPOSED_OF", "BaseConfig") in edge_tuples
    assert ("server", "PROPERTY_AT_YANG", "/ac-ntp:ntp/ac-ntp:server") in edge_tuples

    server = next(node for node in nodes_by_kind["Property"] if node.name == "server")
    summary = json.loads(server.summary_json)
    assert summary["required"] is True
    assert summary["x-supportedDeviceType"] == ["Switch CX"]
    assert server.ast_node_id
    assert any(edge.semantic_id == server.semantic_id for edge in semantic.derived_edges)


def test_semantic_overlay_classifies_additional_properties_schema_as_map() -> None:
    ast_graph = build_ast_graph(
        {
            "openapi": "3.0.3",
            "info": {"title": "Map schema", "version": "1.0"},
            "paths": {},
            "components": {
                "schemas": {
                    "Metadata": {
                        "additionalProperties": {"type": "string"},
                    }
                }
            },
        },
        source="unit/map-schema",
    )

    semantic = build_semantic_overlay(ast_graph)
    metadata = next(node for node in semantic.nodes if node.name == "Metadata")
    summary = json.loads(metadata.summary_json)

    assert summary["bodyShape"] == "map"
    assert summary["kind"] == "map"


def test_semantic_overlay_links_array_property_to_named_item_schema() -> None:
    ast_graph = build_ast_graph(
        {
            "openapi": "3.0.3",
            "info": {"title": "Array item ref", "version": "1.0"},
            "paths": {},
            "components": {
                "schemas": {
                    "ScopeIds": {
                        "type": "string",
                    },
                    "DeviceMove": {
                        "type": "object",
                        "properties": {
                            "items": {
                                "type": "array",
                                "minItems": 1,
                                "maxItems": 20,
                                "x-key": ["id"],
                                "items": {"$ref": "#/components/schemas/ScopeIds"},
                            }
                        },
                    },
                }
            },
        },
        source="unit/array-items-ref",
    )

    semantic = build_semantic_overlay(ast_graph)
    node_by_id = {node.semantic_id: node for node in semantic.nodes}
    edge_tuples = {
        (
            node_by_id[edge.source_id].name,
            edge.kind,
            node_by_id[edge.target_id].name,
        )
        for edge in semantic.edges
    }

    assert ("items", "PROPERTY_OF_TYPE", "ScopeIds") in edge_tuples
    assert ("items", "HAS_ITEM_SCHEMA", "ScopeIds") in edge_tuples

    item_property = next(
        node for node in semantic.nodes if node.kind == "Property" and node.name == "items"
    )
    item_summary = json.loads(item_property.summary_json)
    assert item_summary["constraints"] == {
        "maxItems": 20,
        "minItems": 1,
        "x-key": ["id"],
    }


def test_semantic_overlay_resolves_transparent_schema_wrappers() -> None:
    ast_graph = build_ast_graph(
        {
            "openapi": "3.0.3",
            "info": {"title": "Transparent refs", "version": "1.0"},
            "paths": {},
            "components": {
                "schemas": {
                    "Leaf": {"type": "string"},
                    "Base": {
                        "type": "object",
                        "properties": {"name": {"type": "string"}},
                    },
                    "Composite": {
                        "allOf": [
                            {"$ref": "#/components/schemas/Base"},
                            {
                                "type": "object",
                                "properties": {
                                    "leaf": {"$ref": "#/components/schemas/Leaf"}
                                },
                            },
                        ]
                    },
                    "Map": {
                        "type": "object",
                        "additionalProperties": {
                            "$ref": "#/components/schemas/Leaf"
                        },
                    },
                }
            },
        },
        source="unit/transparent-refs",
    )

    semantic = build_semantic_overlay(ast_graph)
    node_by_id = {node.semantic_id: node for node in semantic.nodes}
    edge_tuples = {
        (
            node_by_id[edge.source_id].name,
            edge.kind,
            node_by_id[edge.target_id].name,
        )
        for edge in semantic.edges
    }
    schema_pointers = {
        node.json_pointer
        for node in semantic.nodes
        if node.kind == "SchemaComponent"
    }

    assert ("Composite", "COMPOSED_OF", "Base") in edge_tuples
    assert ("Map", "HAS_VALUE_SCHEMA", "Leaf") in edge_tuples
    assert ("leaf", "PROPERTY_OF_TYPE", "Leaf") in edge_tuples
    assert "/components/schemas/Composite/allOf/0" not in schema_pointers
    assert "/components/schemas/Composite/allOf/1/properties/leaf" not in schema_pointers
    assert "/components/schemas/Map/additionalProperties" not in schema_pointers


def test_semantic_overlay_merges_reused_model_entity_summary() -> None:
    ast_graph = build_ast_graph(
        {
            "openapi": "3.0.3",
            "info": {"title": "Shared YANG model", "version": "1.0"},
            "paths": {},
            "components": {
                "schemas": {
                    "SharedLeaf": {
                        "type": "string",
                        "x-path": "/ac-test:test/ac-test:leaf",
                    },
                    "Container": {
                        "type": "object",
                        "required": ["leaf"],
                        "properties": {
                            "leaf": {
                                "type": "string",
                                "x-key": ["name"],
                                "x-path": "/ac-test:test/ac-test:leaf",
                            }
                        },
                    },
                }
            },
        },
        source="unit/shared-yang-model",
    )

    semantic = build_semantic_overlay(ast_graph)
    model_summaries = [
        json.loads(node.summary_json)
        for node in semantic.nodes
        if node.kind == "ModelEntity"
        and json.loads(node.summary_json)["identityKey"]
        == "yang:/ac-test:test/ac-test:leaf"
    ]

    assert len(model_summaries) == 1
    summary = model_summaries[0]
    assert set(summary["identityTypes"]) == {"schema", "property"}
    assert set(summary["sourceKinds"]) == {"SchemaComponent", "Property"}
    assert summary["required"] is True
    assert summary["keyFields"] == ["name"]
    assert summary["parentIdentityKey"] == "schema:container"


def test_operation_parameter_overrides_path_item_parameter() -> None:
    spec = _semantic_spec()
    operation = spec["paths"]["/ntp"]["post"]
    spec["paths"]["/ntp"]["parameters"] = [
        {
            "name": "site_id",
            "in": "query",
            "schema": {"type": "string"},
        }
    ]
    operation["parameters"] = [
        {
            "name": "site_id",
            "in": "query",
            "schema": {"$ref": "#/components/schemas/SiteId"},
        }
    ]

    semantic = build_semantic_overlay(build_ast_graph(spec, source="unit/override"))
    site_param = next(
        node
        for node in semantic.nodes
        if node.kind == "Parameter" and node.name == "site_id"
    )
    summary = json.loads(site_param.summary_json)

    assert summary["targetPointer"] == "/components/schemas/SiteId"
    assert summary["schemaPointer"] == "/paths/~1ntp/post/parameters/0/schema"


def test_internal_ref_pointer_preserves_escaped_json_pointer_tokens() -> None:
    assert _internal_ref_pointer("#/paths/~1pets/get") == "/paths/~1pets/get"
    assert _internal_ref_pointer("#") == ""


def test_semantic_metrics_report_catalog_coverage_ratios() -> None:
    ast_graph = build_ast_graph(_semantic_spec_with_untyped_endpoint(), source="unit/semantic")
    semantic = build_semantic_overlay(ast_graph)
    metrics = compute_semantic_metrics([semantic])

    assert metrics["node_kind_counts"]["ApiEndpoint"] == 2
    assert metrics["node_kind_counts"]["ModelEntity"] == 11
    assert metrics["node_kind_counts"]["Parameter"] == 2
    assert metrics["node_kind_counts"]["RequestBody"] == 1
    assert metrics["node_kind_counts"]["Response"] == 4
    assert metrics["edge_kind_counts"]["ACCEPTS_SCHEMA"] == 1
    assert metrics["edge_kind_counts"]["ACCEPTS_MODEL"] == 1
    assert metrics["edge_kind_counts"]["BODY_REFERENCES"] == 1
    assert metrics["edge_kind_counts"]["CONFIGURES_MODEL"] == 2
    assert metrics["edge_kind_counts"]["HAS_PARAMETER"] == 2
    assert metrics["edge_kind_counts"]["HAS_REQUEST_BODY"] == 1
    assert metrics["edge_kind_counts"]["HAS_RESPONSE"] == 4
    assert metrics["edge_kind_counts"]["RESPONSE_REFERENCES"] == 2
    assert metrics["edge_kind_counts"]["REPRESENTS_MODEL"] == 11
    assert metrics["edge_kind_counts"]["RETURNS_MODEL"] == 2
    assert metrics["edge_kind_counts"]["RETURNS_SCHEMA"] == 2
    assert metrics["coverage"]["endpoints_with_parameters"] == {
        "count": 1,
        "total": 2,
        "ratio": 0.5,
    }
    assert metrics["coverage"]["endpoints_with_request_bodies"] == {
        "count": 1,
        "total": 2,
        "ratio": 0.5,
    }
    assert metrics["coverage"]["endpoints_with_responses"] == {
        "count": 2,
        "total": 2,
        "ratio": 1.0,
    }
    assert metrics["coverage"]["endpoints_accepting_schema"] == {
        "count": 1,
        "total": 2,
        "ratio": 0.5,
    }
    assert metrics["coverage"]["endpoints_returning_schema"] == {
        "count": 1,
        "total": 2,
        "ratio": 0.5,
    }
    assert metrics["coverage"]["endpoints_with_any_schema_edge"] == {
        "count": 1,
        "total": 2,
        "ratio": 0.5,
    }
    assert metrics["coverage"]["endpoints_with_any_model_edge"] == {
        "count": 1,
        "total": 2,
        "ratio": 0.5,
    }
    assert metrics["coverage"]["schemas_representing_model"] == {
        "count": 7,
        "total": 7,
        "ratio": 1.0,
    }
    assert metrics["coverage"]["properties_representing_model"] == {
        "count": 4,
        "total": 4,
        "ratio": 1.0,
    }
    assert metrics["coverage"]["endpoints_configuring_model"] == {
        "count": 1,
        "total": 2,
        "ratio": 0.5,
    }
    assert metrics["coverage"]["semantic_nodes_with_ast_provenance"]["ratio"] == 1.0


def test_semantic_metrics_count_configures_model_as_any_model_edge() -> None:
    graph = SemanticGraph(
        spec_id="unit/config-only",
        nodes=[
            SemanticNode(
                semantic_id="endpoint",
                spec_id="unit/config-only",
                kind="ApiEndpoint",
                name="POST /config",
                ast_node_id="",
                json_pointer="/paths/~1config/post",
                stable_key="endpoint:POST:/config",
                summary_json="{}",
            ),
            SemanticNode(
                semantic_id="model",
                spec_id="unit/config-only",
                kind="ModelEntity",
                name="leaf",
                ast_node_id="",
                json_pointer="/components/schemas/Config/properties/leaf",
                stable_key="model:yang:/ac-test:test/ac-test:leaf",
                summary_json='{"identityKey":"yang:/ac-test:test/ac-test:leaf"}',
            ),
        ],
        edges=[
            SemanticEdge(
                source_id="endpoint",
                target_id="model",
                kind="CONFIGURES_MODEL",
                rule_id=f"{IDENTITY_RULE_PACK_ID}.operation.yangModel",
                evidence_json="{}",
            )
        ],
    )

    metrics = compute_semantic_metrics([graph])

    assert metrics["coverage"]["endpoints_with_any_model_edge"] == {
        "count": 1,
        "total": 1,
        "ratio": 1.0,
    }
    assert metrics["coverage"]["endpoints_configuring_model"] == {
        "count": 1,
        "total": 1,
        "ratio": 1.0,
    }


def test_merge_semantic_metrics_matches_single_catalog_report() -> None:
    first = build_semantic_overlay(
        build_ast_graph(_semantic_spec(), source="unit/semantic-first")
    )
    second = build_semantic_overlay(
        build_ast_graph(_semantic_spec_with_untyped_endpoint(), source="unit/semantic-second")
    )

    merged = merge_semantic_metrics(
        [compute_semantic_metrics([first]), compute_semantic_metrics([second])]
    )

    assert merged == compute_semantic_metrics([first, second])


@pytest.mark.parametrize(
    "fixture",
    sorted(_FIXTURE_DIR.glob("*.json")),
    ids=lambda p: p.stem,
)
def test_real_spec_excerpt_builds_semantic_overlay(fixture: Path) -> None:
    spec = clean_spec(json.loads(fixture.read_text(encoding="utf-8")))
    ast_graph = build_ast_graph(spec, source=f"fixture/{fixture.name}")
    semantic = build_semantic_overlay(ast_graph)
    assert semantic.nodes
    assert any(node.kind == "ApiEndpoint" for node in semantic.nodes)
    assert all(
        edge.rule_id.startswith((STRUCTURAL_RULE_PACK_ID, IDENTITY_RULE_PACK_ID))
        for edge in semantic.edges
    )
