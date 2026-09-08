"""Tests for Task 2: lossless OpenAPI AST graph generation."""

from __future__ import annotations

import copy
import json
from pathlib import Path
from tempfile import TemporaryDirectory

import pytest
import real_ladybug as lb

from netops_api_navigator.compiler.ast_builder import (
    UnknownKeywordError,
    build_ast_from_resolved,
    build_ast_graph,
    reconstruct_spec,
)
from netops_api_navigator.compiler.ast_schema import apply_ast_schema
from netops_api_navigator.compiler.ast_writer import write_ast_graph
from netops_api_navigator.compiler.frontend import ResolvedSpec, clean_spec, resolve_spec

pytestmark = pytest.mark.compiler

_FIXTURE_DIR = Path(__file__).resolve().parent / "fixtures" / "oas" / "real_excerpts"


def _oas30_spec() -> dict:
    return {
        "openapi": "3.0.3",
        "info": {"title": "Pets", "version": "1.0"},
        "paths": {
            "/pets": {
                "get": {
                    "operationId": "listPets",
                    "parameters": [
                        {
                            "name": "limit",
                            "in": "query",
                            "schema": {"type": "integer", "default": 10},
                        }
                    ],
                    "responses": {
                        "200": {
                            "description": "ok",
                            "content": {
                                "application/json": {
                                    "schema": {
                                        "type": "array",
                                        "items": {"$ref": "#/components/schemas/Pet"},
                                    }
                                }
                            },
                        }
                    },
                }
            }
        },
        "components": {
            "schemas": {
                "Pet": {
                    "type": "object",
                    "required": ["id"],
                    "properties": {
                        "id": {"type": "string"},
                        "x-device-field": {
                            "type": "string",
                            "description": "A real property whose name starts with x-.",
                        },
                    },
                    "x-central-note": {"source": "fixture"},
                }
            }
        },
    }


def _oas31_spec() -> dict:
    return {
        "openapi": "3.1.0",
        "info": {"title": "Audit", "version": "1.0"},
        "jsonSchemaDialect": "https://json-schema.org/draft/2020-12/schema",
        "paths": {
            "/audits/{id}": {
                "parameters": [
                    {
                        "name": "id",
                        "in": "path",
                        "required": True,
                        "schema": {"type": "string"},
                    }
                ],
                "get": {
                    "operationId": "getAudit",
                    "responses": {
                        "200": {
                            "description": "ok",
                            "content": {
                                "application/json": {
                                    "schema": {"$ref": "#/components/schemas/Audit"}
                                }
                            },
                        }
                    },
                },
            }
        },
        "components": {
            "schemas": {
                "Audit": {
                    "type": "object",
                    "$defs": {
                        "AuditId": {"type": "string", "format": "uuid"},
                    },
                    "properties": {
                        "id": {"$ref": "#/components/schemas/Audit/$defs/AuditId"}
                    },
                }
            }
        },
    }


def _review_edge_spec() -> dict:
    return {
        "openapi": "3.0.3",
        "info": {"title": "Review edges", "version": "1.0"},
        "servers": [
            {
                "url": "https://{tenant}.example.test",
                "variables": {
                    "tenant": {
                        "default": "central",
                        "description": "Tenant slug",
                    }
                },
            }
        ],
        "paths": {
            "/pets": {
                "$ref": "#/components/pathItems/PetsPath",
                "get": {
                    "operationId": "listPets",
                    "responses": {
                        "200": {
                            "description": "ok",
                            "links": {
                                "nextPage": {
                                    "operationId": "listPets",
                                    "parameters": {"cursor": "$response.body#/next"},
                                }
                            },
                        }
                    },
                    "callbacks": {
                        "onEvent": {
                            "{$request.body#/callbackUrl}": {
                                "post": {
                                    "operationId": "petEvent",
                                    "responses": {"204": {"description": "ok"}},
                                }
                            }
                        }
                    },
                },
            }
        },
        "components": {
            "pathItems": {
                "PetsPath": {
                    "parameters": [
                        {
                            "name": "tenant",
                            "in": "query",
                            "schema": {"type": "string"},
                        }
                    ]
                }
            },
            "securitySchemes": {
                "oauth": {
                    "type": "oauth2",
                    "flows": {
                        "clientCredentials": {
                            "tokenUrl": "https://auth.example.test/token",
                            "scopes": {"read:pets": "Read pets"},
                        }
                    },
                }
            },
            "links": {
                "PetLink": {
                    "operationId": "getPet",
                    "parameters": {"id": "$response.body#/id"},
                }
            },
            "callbacks": {
                "PetEvent": {
                    "{$request.body#/callbackUrl}": {
                        "post": {
                            "operationId": "petEventComponent",
                            "responses": {"204": {"description": "ok"}},
                        }
                    }
                }
            },
        },
    }


def test_reconstructs_cleaned_oas30_spec_losslessly() -> None:
    spec = _oas30_spec()
    graph = build_ast_graph(spec, source="unit/pets")
    assert reconstruct_spec(graph) == spec


def test_reconstructs_cleaned_oas31_spec_losslessly() -> None:
    spec = _oas31_spec()
    graph = build_ast_graph(spec, source="unit/audit")
    assert reconstruct_spec(graph) == spec


def test_ref_node_is_preserved_and_linked_to_target() -> None:
    graph = build_ast_graph(_oas30_spec(), source="unit/pets")
    ref_nodes = [
        n for n in graph.nodes
        if n.key == "$ref" and json.loads(n.scalar_json) == "#/components/schemas/Pet"
    ]
    assert len(ref_nodes) == 1
    assert ref_nodes[0].kind == "Constraint"

    targets = {e.ref_node_id: e.target_node_id for e in graph.ref_edges}
    assert ref_nodes[0].node_id in targets
    target = next(n for n in graph.nodes if n.node_id == targets[ref_nodes[0].node_id])
    assert target.kind == "Schema"
    assert target.json_pointer == "/components/schemas/Pet"


def test_dynamic_property_names_do_not_count_as_unknown_or_extension() -> None:
    graph = build_ast_graph(_oas30_spec(), source="unit/pets")
    props = [
        n for n in graph.nodes
        if n.kind == "Property" and n.json_pointer.endswith("/properties/x-device-field")
    ]
    assert len(props) == 1
    assert props[0].key == "x-device-field"
    assert props[0].is_extension is False


def test_nested_schema_roles_survive_composition_constraint_containers() -> None:
    spec = _oas30_spec()
    spec["components"]["schemas"]["Pet"] = {
        "allOf": [
            {
                "properties": {
                    "modules": {
                        "type": "array",
                        "items": {
                            "properties": {
                                "severity": {"type": "string"},
                            }
                        },
                    }
                }
            }
        ]
    }

    graph = build_ast_graph(spec, source="unit/nested-schema-roles")
    nodes = {node.json_pointer: node for node in graph.nodes}
    base = "/components/schemas/Pet/allOf/0/properties/modules"

    assert nodes["/components/schemas/Pet/allOf/0"].kind == "Schema"
    assert nodes[base].kind == "Property"
    assert nodes[f"{base}/items"].kind == "Items"
    assert json.loads(nodes[f"{base}/items"].raw_json)["properties"] == {
        "severity": {"type": "string"}
    }


def test_unknown_fixed_keyword_fails_loudly() -> None:
    spec = _oas30_spec()
    spec["paths"]["/pets"]["get"]["madeUpKeyword"] = True
    with pytest.raises(UnknownKeywordError) as exc:
        build_ast_graph(spec, source="unit/broken")
    assert exc.value.parent_kind == "Operation"
    assert exc.value.key == "madeUpKeyword"
    assert exc.value.pointer == "/paths/~1pets/get"


def test_vendor_extension_is_preserved_as_extension_node() -> None:
    graph = build_ast_graph(_oas30_spec(), source="unit/pets")
    nodes = [n for n in graph.nodes if n.key == "x-central-note"]
    assert len(nodes) == 1
    assert nodes[0].kind == "Extension"
    assert nodes[0].is_extension is True
    assert json.loads(nodes[0].raw_json) == {"source": "fixture"}


def test_persisted_raw_json_avoids_recursive_structural_duplication() -> None:
    graph = build_ast_graph(_oas30_spec(), source="unit/compact-raw")
    by_pointer = {node.json_pointer: node for node in graph.nodes}

    assert by_pointer[""].raw_json == ""
    assert by_pointer["/components"].raw_json == ""
    assert by_pointer["/components/schemas"].raw_json == ""
    assert by_pointer["/components/schemas/Pet"].raw_json
    assert by_pointer["/components/schemas/Pet/properties/id"].raw_json
    assert by_pointer[
        "/components/schemas/Pet/x-central-note"
    ].raw_json == '{"source":"fixture"}'
    assert reconstruct_spec(graph) == _oas30_spec()


def test_example_value_array_items_are_not_validated_as_schema_keywords() -> None:
    spec = _oas30_spec()
    media = spec["paths"]["/pets"]["get"]["responses"]["200"]["content"]["application/json"]
    media["examples"] = {
        "sample": {
            "summary": "Example array",
            "value": {
                "items": [
                    {
                        "stage": "ready",
                        "clientName": "client-1",
                    }
                ]
            },
        }
    }

    graph = build_ast_graph(spec, source="unit/examples")
    by_pointer = {n.json_pointer: n.kind for n in graph.nodes}
    assert (
        by_pointer[
            "/paths/~1pets/get/responses/200/content/application~1json/examples/sample/value/items/0"
        ]
        == "Scalar"
    )
    assert reconstruct_spec(graph) == spec


def test_schema_property_named_value_under_content_remains_schema() -> None:
    spec = _oas30_spec()
    schema = (
        spec["paths"]["/pets"]["get"]["responses"]["200"]["content"]["application/json"][
            "schema"
        ]
    )
    schema.clear()
    schema.update(
        {
            "type": "object",
            "properties": {
                "value": {
                    "type": "array",
                    "items": {
                        "type": "object",
                        "properties": {
                            "name": {"type": "string"},
                        },
                    },
                }
            },
        }
    )

    graph = build_ast_graph(spec, source="unit/schema-value")
    by_pointer = {n.json_pointer: n.kind for n in graph.nodes}

    assert (
        by_pointer[
            "/paths/~1pets/get/responses/200/content/application~1json/schema/properties/value/items"
        ]
        == "Items"
    )
    assert (
        by_pointer[
            "/paths/~1pets/get/responses/200/content/application~1json/schema/properties/value/items/properties/name"
        ]
        == "Property"
    )
    assert reconstruct_spec(graph) == spec


def test_boolean_default_coercion_preserves_invalid_strings() -> None:
    spec = {
        "openapi": "3.0.3",
        "info": {"title": "Defaults", "version": "1"},
        "paths": {},
        "components": {
            "schemas": {
                "Flags": {
                    "type": "object",
                    "properties": {
                        "yes": {"type": "boolean", "default": "true"},
                        "no": {"type": "boolean", "default": "false"},
                        "bad": {"type": "boolean", "default": "maybe"},
                    },
                }
            }
        },
    }
    cleaned = clean_spec(spec)
    props = cleaned["components"]["schemas"]["Flags"]["properties"]
    assert props["yes"]["default"] is True
    assert props["no"]["default"] is False
    assert props["bad"]["default"] == "maybe"


def test_fixed_object_kinds_are_reached_and_validated() -> None:
    graph = build_ast_graph(_review_edge_spec(), source="unit/review-edges")
    by_pointer = {n.json_pointer: n.kind for n in graph.nodes}
    assert by_pointer["/servers/0"] == "Server"
    assert by_pointer["/servers/0/variables/tenant"] == "ServerVariable"
    assert by_pointer["/components/securitySchemes/oauth/flows"] == "OAuthFlows"
    assert by_pointer["/components/securitySchemes/oauth/flows/clientCredentials"] == "OAuthFlow"
    assert by_pointer["/paths/~1pets/get/responses/200/links/nextPage"] == "Link"
    assert by_pointer["/components/links/PetLink"] == "Link"
    assert by_pointer["/components/callbacks/PetEvent"] == "Callback"
    assert (
        by_pointer[
            "/components/callbacks/PetEvent/{$request.body#~1callbackUrl}"
        ]
        == "PathItem"
    )


@pytest.mark.parametrize(
    ("mutate", "parent_kind", "key"),
    [
        (
            lambda s: s["servers"][0].update({"bogusServerKey": True}),
            "Server",
            "bogusServerKey",
        ),
        (
            lambda s: s["components"]["links"]["PetLink"].update({"bogusLinkKey": True}),
            "Link",
            "bogusLinkKey",
        ),
        (
            lambda s: s["components"]["securitySchemes"]["oauth"]["flows"][
                "clientCredentials"
            ].update({"bogusFlowKey": True}),
            "OAuthFlow",
            "bogusFlowKey",
        ),
    ],
)
def test_unknown_keys_fail_in_reached_fixed_object_kinds(mutate, parent_kind, key) -> None:
    spec = _review_edge_spec()
    mutate(spec)
    with pytest.raises(UnknownKeywordError) as exc:
        build_ast_graph(spec, source="unit/review-broken")
    assert exc.value.parent_kind == parent_kind
    assert exc.value.key == key


def test_task1_result_keeps_cleaned_raw_spec_for_ast_input() -> None:
    raw = _oas30_spec()
    raw["_id"] = "readme-internal"
    outcome = resolve_spec(raw, source="unit/resolved")
    assert isinstance(outcome, ResolvedSpec), getattr(outcome, "error", None)
    assert "_id" not in outcome.raw_spec
    assert any("$ref" in json.dumps(v) for v in outcome.raw_spec["paths"].values())
    assert "$ref" not in json.dumps(outcome.spec)

    graph = build_ast_from_resolved(outcome)
    assert reconstruct_spec(graph) == outcome.raw_spec


def test_schema_property_named_properties_does_not_shadow_keyword_context() -> None:
    spec = _oas30_spec()
    spec["components"]["schemas"]["PropertyCollision"] = {
        "type": "object",
        "properties": {
            "properties": {
                "type": "object",
                "properties": {
                    "name": {"type": "string"},
                },
            },
        },
    }

    graph = build_ast_graph(spec, source="unit/property-collision")
    by_pointer = {node.json_pointer: node.kind for node in graph.nodes}

    assert by_pointer[
        "/components/schemas/PropertyCollision/properties/properties"
    ] == "Property"
    assert by_pointer[
        "/components/schemas/PropertyCollision/properties/properties/properties"
    ] == "Scalar"
    assert by_pointer[
        "/components/schemas/PropertyCollision/properties/properties/properties/name"
    ] == "Property"
    assert reconstruct_spec(graph) == spec


def test_writer_persists_ast_graph_to_ladybug() -> None:
    graph = build_ast_graph(_oas30_spec(), source="unit/pets")
    with TemporaryDirectory(prefix="ast_writer_") as tmp:
        db = lb.Database(str(Path(tmp) / "ast_db"), max_db_size=256 * 1024 * 1024)
        conn = lb.Connection(db)
        try:
            apply_ast_schema(conn)
            write_ast_graph(conn, graph)
            counts = list(
                conn.execute(
                    """
                    MATCH (s:OasSpec)-[:HAS_AST_ROOT]->(root:OasAstNode)
                    RETURN s.source AS source, root.kind AS root_kind
                    """
                ).rows_as_dict()
            )
            assert counts == [{"source": "unit/pets", "root_kind": "Document"}]
            child_rows = list(
                conn.execute(
                    """
                    MATCH (root:OasAstNode)-[edge:AST_CHILD]->(child:OasAstNode)
                    WHERE root.kind = 'Document' AND child.key = 'info'
                    RETURN edge.key AS edge_key, edge.index AS edge_index,
                           child.key AS child_key, child.index AS child_index
                    """
                ).rows_as_dict()
            )
            assert child_rows == [
                {
                    "edge_key": "info",
                    "edge_index": None,
                    "child_key": "info",
                    "child_index": None,
                }
            ]
            ref_rows = list(
                conn.execute(
                    """
                    MATCH (r:OasAstNode)-[edge:AST_REF_TARGET]->(target:OasAstNode)
                    RETURN r.key AS ref_key, edge.ref AS ref, target.jsonPointer AS target
                    """
                ).rows_as_dict()
            )
            assert ref_rows == [
                {
                    "ref_key": "$ref",
                    "ref": "#/components/schemas/Pet",
                    "target": "/components/schemas/Pet",
                }
            ]
        finally:
            db.close()


@pytest.mark.parametrize(
    "fixture",
    sorted(_FIXTURE_DIR.glob("*.json")),
    ids=lambda p: p.stem,
)
def test_real_spec_excerpt_builds_lossless_ast(fixture: Path) -> None:
    spec = clean_spec(json.loads(fixture.read_text(encoding="utf-8")))
    graph = build_ast_graph(spec, source=f"fixture/{fixture.name}")
    assert reconstruct_spec(graph) == spec
    assert graph.nodes


@pytest.mark.real_spec
def test_real_central_stride_sample_builds_ast(real_central_specs: list[Path]) -> None:
    sample = real_central_specs[::64]
    assert sample, "expected at least one real Central spec"
    built = 0
    for path in sample[:25]:
        spec = json.loads(path.read_text(encoding="utf-8"))
        # Remove ReadMe internal metadata in the same way Task 1 does.
        outcome = resolve_spec(copy.deepcopy(spec), source=f"central/{path.name}")
        if not isinstance(outcome, ResolvedSpec):
            continue
        graph = build_ast_from_resolved(outcome)
        assert reconstruct_spec(graph) == outcome.raw_spec
        built += 1
    assert built > 0, "expected at least one sampled real spec to resolve and build an AST"


@pytest.mark.real_spec
def test_real_floor_map_property_named_properties_builds_ast(
    real_central_specs: list[Path],
) -> None:
    path = next(
        (candidate for candidate in real_central_specs if candidate.name == "getfloormapv1.json"),
        None,
    )
    if path is None:
        pytest.skip("Hydrated corpus does not contain getfloormapv1.json")
    outcome = resolve_spec(
        json.loads(path.read_text(encoding="utf-8")),
        source=f"central/{path.name}",
    )
    assert isinstance(outcome, ResolvedSpec), getattr(outcome, "error", None)
    graph = build_ast_from_resolved(outcome)
    assert reconstruct_spec(graph) == outcome.raw_spec
