"""Tests for compiler projection materialization into typed L3 tables."""

from __future__ import annotations

from collections.abc import Iterator
from pathlib import Path
import shutil
import tempfile

import pytest
import real_ladybug as lb

from netops_api_navigator.compiler.ast_builder import build_ast_graph
from netops_api_navigator.compiler.catalog_identity import canonical_body_hash
from netops_api_navigator.compiler.projection_writer import (
    CompilerProjectionData,
    build_compiler_projection_database,
    collect_compiler_projection_graph,
)
from netops_api_navigator.compiler.semantic_builder import build_semantic_overlay

pytestmark = [pytest.mark.compiler, pytest.mark.unit]


@pytest.fixture
def repo_tmp_path() -> Iterator[Path]:
    repo_tmp = Path(__file__).resolve().parent.parent / "tmp"
    repo_tmp.mkdir(exist_ok=True)
    path = Path(tempfile.mkdtemp(prefix="test_compiler_projection_", dir=repo_tmp))
    try:
        yield path
    finally:
        shutil.rmtree(path, ignore_errors=True)


def test_projection_materializes_reusable_response_header_and_array_item_ref(
    repo_tmp_path: Path,
) -> None:
    spec = {
        "openapi": "3.0.3",
        "info": {"title": "Projection", "version": "1.0"},
        "paths": {
            "/move": {
                "post": {
                    "operationId": "moveDevices",
                    "parameters": [
                        {"$ref": "#/components/parameters/DeviceId"}
                    ],
                    "requestBody": {
                        "content": {
                            "application/json": {
                                "schema": {"$ref": "#/components/schemas/DeviceMove"}
                            }
                        }
                    },
                    "responses": {
                        "200": {
                            "description": "mixed schema and schema-less content",
                            "content": {
                                "application/json": {
                                    "schema": {"$ref": "#/components/schemas/Error"}
                                },
                                "*/*": {},
                            },
                        },
                        "204": {
                            "description": "accepted without schema",
                            "content": {"*/*": {}},
                        },
                        "429": {"$ref": "#/components/responses/TooManyRequests"}
                    },
                }
            }
        },
        "components": {
            "headers": {
                "XRateLimitLimitHeader": {
                    "description": "limit",
                    "schema": {"type": "integer"},
                }
            },
            "examples": {
                "RateLimitExample": {
                    "summary": "rate limited",
                    "value": {"error": "too many requests"},
                }
            },
            "parameters": {
                "DeviceId": {
                    "name": "device_id",
                    "in": "query",
                    "schema": {"$ref": "#/components/schemas/ScopeIds"},
                }
            },
            "responses": {
                "TooManyRequests": {
                    "description": "rate limited",
                    "headers": {
                        "X-RateLimit-Limit": {
                            "$ref": "#/components/headers/XRateLimitLimitHeader"
                        }
                    },
                    "content": {
                        "application/json": {
                            "schema": {"$ref": "#/components/schemas/Error"}
                        }
                    },
                }
            },
            "schemas": {
                "DeviceMove": {
                    "type": "object",
                    "properties": {
                        "items": {
                            "type": "array",
                            "minItems": 1,
                            "x-key": ["scopeId"],
                            "items": {"$ref": "#/components/schemas/ScopeIds"},
                        }
                    },
                },
                "Error": {
                    "type": "object",
                    "properties": {
                        "message": {
                            "type": "string",
                            "default": "unknown",
                            "pattern": "^[a-z]+$",
                            "minLength": 1,
                            "maxLength": 64,
                            "x-enumDescriptions": {"unknown": "Unknown error"},
                        },
                        "code": {
                            "type": "integer",
                            "minimum": 400,
                            "maximum": 599,
                        },
                    },
                },
                "ScopeIds": {"type": "string"},
            },
        },
    }
    ast = build_ast_graph(spec, source="central/projection")
    semantic = build_semantic_overlay(ast)
    db_path = repo_tmp_path / "knowledge_db_compiler"
    stats = build_compiler_projection_database(db_path, [ast], [semantic])

    assert stats["node_kind_counts"]["SchemaComponent"] >= 5
    db = lb.Database(str(db_path), max_db_size=256 * 1024 * 1024)
    conn = lb.Connection(db)
    try:
        reusable_rows = list(
            conn.execute(
                """
                MATCH (component:SchemaComponent)
                WHERE component.component_id IN [
                  'central:examples:RateLimitExample',
                  'central:headers:XRateLimitLimitHeader',
                  'central:responses:TooManyRequests'
                ]
                RETURN component.component_id AS component_id,
                       component.section AS section,
                       component.name AS name
                ORDER BY component.component_id
                """
            ).rows_as_dict()
        )
        assert reusable_rows == [
            {
                "component_id": "central:examples:RateLimitExample",
                "section": "examples",
                "name": "RateLimitExample",
            },
            {
                "component_id": "central:headers:XRateLimitLimitHeader",
                "section": "headers",
                "name": "XRateLimitLimitHeader",
            },
            {
                "component_id": "central:responses:TooManyRequests",
                "section": "responses",
                "name": "TooManyRequests",
            },
        ]

        item_type_rows = list(
            conn.execute(
                """
                MATCH (schema:SchemaComponent {component_id: 'central:schemas:DeviceMove'})
                      -[:HAS_PROPERTY]->(prop:Property {name: 'items'})
                      -[:PROPERTY_OF_TYPE]->(target:SchemaComponent)
                RETURN target.component_id AS target_component_id
                """
            ).rows_as_dict()
        )
        assert item_type_rows == [
            {"target_component_id": "central:schemas:ScopeIds"}
        ]

        item_schema_rows = list(
            conn.execute(
                """
                MATCH (schema:SchemaComponent {component_id: 'central:schemas:DeviceMove'})
                      -[:HAS_PROPERTY]->(prop:Property {name: 'items'})
                      -[:HAS_ITEM_SCHEMA]->(target:SchemaComponent)
                RETURN target.component_id AS target_component_id
                """
            ).rows_as_dict()
        )
        assert item_schema_rows == [
            {"target_component_id": "central:schemas:ScopeIds"}
        ]

        constraint_rows = list(
            conn.execute(
                """
                MATCH (:SchemaComponent {component_id: 'central:schemas:Error'})
                      -[:HAS_PROPERTY]->(prop:Property)
                RETURN prop.name AS name,
                       prop.pattern AS pattern,
                       prop.defaultValue AS defaultValue,
                       prop.minimum AS minimum,
                       prop.maximum AS maximum,
                       prop.minLength AS minLength,
                       prop.maxLength AS maxLength,
                       prop.enumDescriptionsJson AS enumDescriptionsJson,
                       prop.constraintsJson AS constraintsJson
                ORDER BY prop.name
                """
            ).rows_as_dict()
        )
        assert constraint_rows == [
            {
                "name": "code",
                "pattern": "",
                "defaultValue": "",
                "minimum": 400.0,
                "maximum": 599.0,
                "minLength": None,
                "maxLength": None,
                "enumDescriptionsJson": "",
                "constraintsJson": '{"maximum":599,"minimum":400}',
            },
            {
                "name": "message",
                "pattern": "^[a-z]+$",
                "defaultValue": '"unknown"',
                "minimum": None,
                "maximum": None,
                "minLength": 1,
                "maxLength": 64,
                "enumDescriptionsJson": '{"unknown":"Unknown error"}',
                "constraintsJson": (
                    '{"default":"unknown","maxLength":64,"minLength":1,'
                    '"pattern":"^[a-z]+$","x-enumDescriptions":'
                    '{"unknown":"Unknown error"}}'
                ),
            },
        ]

        array_key_rows = list(
            conn.execute(
                """
                MATCH (:SchemaComponent {component_id: 'central:schemas:DeviceMove'})
                      -[:HAS_PROPERTY]->(prop:Property {name: 'items'})
                RETURN prop.constraintsJson AS constraintsJson
                """
            ).rows_as_dict()
        )
        assert array_key_rows == [
            {
                "constraintsJson": '{"minItems":1,"x-key":["scopeId"]}',
            }
        ]

        parameter_type_rows = list(
            conn.execute(
                """
                MATCH (:ApiEndpoint {endpoint_id: 'POST:/move'})
                      -[:HAS_PARAMETER]->(param:Parameter {name: 'device_id'})
                      -[:PARAMETER_REFERENCES]->(target:SchemaComponent)
                RETURN target.component_id AS target_component_id
                """
            ).rows_as_dict()
        )
        assert parameter_type_rows == [
            {"target_component_id": "central:schemas:ScopeIds"}
        ]

        response_rows = list(
            conn.execute(
                """
                MATCH (:ApiEndpoint {endpoint_id: 'POST:/move'})
                      -[:HAS_RESPONSE]->(response:Response)
                RETURN response.status AS status,
                       response.content_type AS content_type,
                       response.root_component_ref AS root_component_ref
                ORDER BY response.status, response.content_type
                """
            ).rows_as_dict()
        )
        assert {
            (row["status"], row["content_type"], row["root_component_ref"])
            for row in response_rows
        } == {
            ("200", "*/*", ""),
            ("200", "application/json", "central:schemas:Error"),
            ("204", "*/*", ""),
            ("429", "application/json", "central:schemas:Error"),
        }

        provenance_rows = list(
            conn.execute(
                """
                MATCH (provenance:CompilerProjectionMap)
                WHERE provenance.row_id = 'central:headers:XRateLimitLimitHeader'
                   OR provenance.row_id = 'central:schemas:DeviceMove#prop:items'
                RETURN provenance.table_name AS table_name,
                       provenance.row_id AS row_id,
                       provenance.semantic_id AS semantic_id,
                       provenance.ast_node_id AS ast_node_id,
                       provenance.json_pointer AS json_pointer
                ORDER BY provenance.row_id
                """
            ).rows_as_dict()
        )
        assert provenance_rows == [
            {
                "table_name": "SchemaComponent",
                "row_id": "central:headers:XRateLimitLimitHeader",
                "semantic_id": "",
                "ast_node_id": ast.spec_id + "#/components/headers/XRateLimitLimitHeader",
                "json_pointer": "/components/headers/XRateLimitLimitHeader",
            },
            {
                "table_name": "Property",
                "row_id": "central:schemas:DeviceMove#prop:items",
                "semantic_id": next(
                    node.semantic_id
                    for node in semantic.nodes
                    if node.kind == "Property" and node.name == "items"
                ),
                "ast_node_id": ast.spec_id + "#/components/schemas/DeviceMove/properties/items",
                "json_pointer": "/components/schemas/DeviceMove/properties/items",
            },
        ]
    finally:
        db.close()


def test_projection_collection_requires_finalized_catalog_identity_registry() -> None:
    spec = {
        "openapi": "3.0.3",
        "info": {"title": "Guard", "version": "1.0"},
        "paths": {},
        "components": {"schemas": {"Shared": {"type": "object"}}},
    }
    ast = build_ast_graph(spec, source="central/guard")

    with pytest.raises(RuntimeError, match="catalog_identities must be finalized"):
        collect_compiler_projection_graph(
            CompilerProjectionData(),
            ast,
            build_semantic_overlay(ast),
        )


def test_projection_uses_lineage_ids_for_inline_schema_branches(
    repo_tmp_path: Path,
) -> None:
    spec = {
        "openapi": "3.0.3",
        "info": {"title": "Inline identities", "version": "1.0"},
        "paths": {},
        "components": {
            "schemas": {
                "Base": {
                    "type": "object",
                    "properties": {"base": {"type": "string"}},
                },
                "Root": {
                    "allOf": [
                        {"$ref": "#/components/schemas/Base"},
                        {
                            "type": "object",
                            "properties": {
                                "branch": {"type": "string"},
                                "branchItems": {
                                    "type": "array",
                                    "items": {
                                        "properties": {
                                            "value": {"type": "string"}
                                        }
                                    },
                                },
                            },
                        },
                    ],
                    "properties": {
                        "items": {
                            "type": "object",
                            "properties": {"id": {"type": "string"}},
                        },
                        "nested": {
                            "type": "object",
                            "allOf": [{"$ref": "#/components/schemas/Base"}],
                            "properties": {"name": {"type": "string"}},
                        },
                        "entries": {
                            "type": "array",
                            "items": {
                                "type": "object",
                                "properties": {"id": {"type": "string"}},
                            },
                        },
                        "choice": {
                            "oneOf": [
                                {"$ref": "#/components/schemas/Base"},
                                {"type": "string"},
                            ]
                        },
                    },
                },
            }
        },
    }
    ast = build_ast_graph(spec, source="central/inline-identities")
    semantic = build_semantic_overlay(ast)
    db_path = repo_tmp_path / "knowledge_db_compiler_inline"
    build_compiler_projection_database(db_path, [ast], [semantic])

    db = lb.Database(str(db_path), max_db_size=256 * 1024 * 1024)
    conn = lb.Connection(db)
    try:
        component_ids = {
            row["component_id"]
            for row in conn.execute(
                "MATCH (component:SchemaComponent) "
                "RETURN component.component_id AS component_id"
            ).rows_as_dict()
        }
        assert {
            "central:schemas:Base",
            "central:schemas:Root",
            "central:schemas:Root#allOf:1",
            "central:schemas:Root#allOf:1#prop:branchItems#items",
            "central:schemas:Root#prop:choice#union",
            "central:schemas:Root#prop:choice#union#oneOf:1",
            "central:schemas:Root#prop:entries#items",
            "central:schemas:Root#prop:items#object",
            "central:schemas:Root#prop:nested#object",
        }.issubset(component_ids)
        assert not any(component_id.startswith("central:inline:") for component_id in component_ids)
        assert "central:schemas:Root#prop:nested#union" not in component_ids

        composition = {
            (row["source"], row["target"])
            for row in conn.execute(
                """
                MATCH (source:SchemaComponent)-[:COMPOSED_OF]->(target:SchemaComponent)
                RETURN source.component_id AS source, target.component_id AS target
                """
            ).rows_as_dict()
        }
        assert ("central:schemas:Root", "central:schemas:Base") in composition
        assert (
            "central:schemas:Root",
            "central:schemas:Root#allOf:1",
        ) in composition
        assert (
            "central:schemas:Root#prop:choice#union",
            "central:schemas:Base",
        ) in composition
        assert (
            "central:schemas:Root#prop:nested#object",
            "central:schemas:Base",
        ) in composition
    finally:
        db.close()


def test_projection_keeps_distinct_same_name_component_variants(
    repo_tmp_path: Path,
) -> None:
    rich_spec = {
        "openapi": "3.0.3",
        "info": {"title": "Rich", "version": "1.0"},
        "paths": {},
        "components": {
            "schemas": {
                "Shared": {
                    "type": "object",
                    "properties": {"name": {"type": "string"}},
                }
            }
        },
    }
    stub_spec = {
        "openapi": "3.0.3",
        "info": {"title": "Stub", "version": "1.0"},
        "paths": {},
        "components": {"schemas": {"Shared": {"type": "object"}}},
    }
    rich_ast = build_ast_graph(rich_spec, source="central/rich")
    stub_ast = build_ast_graph(stub_spec, source="central/stub")
    db_path = repo_tmp_path / "knowledge_db_compiler_richest"
    build_compiler_projection_database(
        db_path,
        [rich_ast, stub_ast],
        [build_semantic_overlay(rich_ast), build_semantic_overlay(stub_ast)],
    )

    db = lb.Database(str(db_path), max_db_size=256 * 1024 * 1024)
    conn = lb.Connection(db)
    try:
        rich_id = "central:schemas:Shared"
        stub_id = "central:schemas:Shared@" + canonical_body_hash(
            stub_spec["components"]["schemas"]["Shared"]
        )[:12]
        rows = list(
            conn.execute(
                """
                MATCH (component:SchemaComponent)
                WHERE component.component_id IN [$rich_id, $stub_id]
                OPTIONAL MATCH (component)-[:HAS_PROPERTY]->(prop:Property)
                WITH component, COUNT(prop) AS property_count
                MATCH (provenance:CompilerProjectionMap {
                  row_id: component.component_id,
                  table_name: 'SchemaComponent'
                })
                RETURN component.component_id AS component_id,
                       component.bodyJson AS body_json,
                       property_count,
                       provenance.source AS source,
                       provenance.json_pointer AS json_pointer
                ORDER BY component.component_id
                """,
                parameters={"rich_id": rich_id, "stub_id": stub_id},
            ).rows_as_dict()
        )
        assert {
            row["component_id"]: row
            for row in rows
        } == {
            rich_id: {
                "component_id": rich_id,
                "body_json": '{"type":"object","properties":{"name":{"type":"string"}}}',
                "property_count": 1,
                "source": "central/rich",
                "json_pointer": "/components/schemas/Shared",
            },
            stub_id: {
                "component_id": stub_id,
                "body_json": '{"type":"object"}',
                "property_count": 0,
                "source": "central/stub",
                "json_pointer": "/components/schemas/Shared",
            },
        }
    finally:
        db.close()


def test_projection_component_variant_ids_are_input_order_independent(
    repo_tmp_path: Path,
) -> None:
    alpha_spec = {
        "openapi": "3.0.3",
        "info": {"title": "Alpha", "version": "1.0"},
        "paths": {},
        "components": {
            "schemas": {
                "Shared": {
                    "type": "object",
                    "properties": {
                        "alpha": {"type": "string"},
                        "alphaExtra": {"type": "boolean"},
                    },
                }
            }
        },
    }
    beta_spec = {
        "openapi": "3.0.3",
        "info": {"title": "Beta", "version": "1.0"},
        "paths": {},
        "components": {
            "schemas": {
                "Shared": {
                    "type": "object",
                    "properties": {"beta": {"type": "integer"}},
                }
            }
        },
    }
    alpha_ast = build_ast_graph(alpha_spec, source="central/alpha")
    beta_ast = build_ast_graph(beta_spec, source="central/beta")

    def component_ids(db_path: Path, asts: list) -> set[str]:
        build_compiler_projection_database(
            db_path,
            asts,
            [build_semantic_overlay(ast) for ast in asts],
        )
        db = lb.Database(str(db_path), max_db_size=256 * 1024 * 1024)
        try:
            return {
                row["component_id"]
                for row in lb.Connection(db)
                .execute(
                    """
                    MATCH (component:SchemaComponent)
                    RETURN component.component_id AS component_id
                    """
                )
                .rows_as_dict()
            }
        finally:
            db.close()

    forward_ids = component_ids(
        repo_tmp_path / "knowledge_db_compiler_forward",
        [alpha_ast, beta_ast],
    )
    reverse_ids = component_ids(
        repo_tmp_path / "knowledge_db_compiler_reverse",
        [beta_ast, alpha_ast],
    )

    assert forward_ids == reverse_ids
    assert forward_ids == {
        "central:schemas:Shared",
        "central:schemas:Shared@" + canonical_body_hash(
            beta_spec["components"]["schemas"]["Shared"]
        )[:12],
    }


def test_projection_merges_identical_component_rows_with_multiple_provenance(
    repo_tmp_path: Path,
) -> None:
    spec = {
        "openapi": "3.0.3",
        "info": {"title": "One", "version": "1.0"},
        "paths": {},
        "components": {
            "schemas": {
                "Shared": {
                    "type": "object",
                    "properties": {"id": {"type": "string"}},
                }
            }
        },
    }
    first_ast = build_ast_graph(spec, source="central/one")
    second_spec = {
        **spec,
        "info": {"title": "Two", "version": "1.0"},
    }
    second_ast = build_ast_graph(second_spec, source="central/two")
    db_path = repo_tmp_path / "knowledge_db_compiler_identical"
    stats = build_compiler_projection_database(
        db_path,
        [first_ast, second_ast],
        [build_semantic_overlay(first_ast), build_semantic_overlay(second_ast)],
    )

    db = lb.Database(str(db_path), max_db_size=256 * 1024 * 1024)
    conn = lb.Connection(db)
    try:
        assert stats["catalog_identity"]["conflicting_named_identity_count"] == 0
        assert stats["catalog_identity"]["identical_named_identity_merge_count"] >= 1
        component_rows = list(
            conn.execute(
                """
                MATCH (component:SchemaComponent {component_id: 'central:schemas:Shared'})
                RETURN component.component_id AS component_id,
                       component.bodyJson AS body_json
                """
            ).rows_as_dict()
        )
        assert component_rows == [
            {
                "component_id": "central:schemas:Shared",
                "body_json": '{"type":"object","properties":{"id":{"type":"string"}}}',
            }
        ]

        provenance_rows = list(
            conn.execute(
                """
                MATCH (provenance:CompilerProjectionMap {
                  row_id: 'central:schemas:Shared',
                  table_name: 'SchemaComponent'
                })
                RETURN provenance.source AS source,
                       provenance.json_pointer AS json_pointer
                ORDER BY provenance.source
                """
            ).rows_as_dict()
        )
        assert provenance_rows == [
            {
                "source": "central/one",
                "json_pointer": "/components/schemas/Shared",
            },
            {
                "source": "central/two",
                "json_pointer": "/components/schemas/Shared",
            }
        ]
    finally:
        db.close()
