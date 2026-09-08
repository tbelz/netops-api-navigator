"""Tests for the API schema subgraph (Phase 2A of ADR 009).

Covers the new node + relationship tables that decompose per-endpoint
JSON blobs into queryable Cypher entities, and the build-time populate
helper that fills them from a normalised OpenAPI spec.

Tests use a synthetic OAS spec for fast feedback. A separate
integration test (test_schema_graph_real_db.py) verifies the same
shapes against the real spec cache.
"""

from __future__ import annotations

import copy
import hashlib
import json
import sys
from pathlib import Path
from tempfile import TemporaryDirectory

import pytest

# Ensure src is importable without editable install.
_SRC_DIR = Path(__file__).resolve().parent.parent / "src"
if str(_SRC_DIR) not in sys.path:
    sys.path.insert(0, str(_SRC_DIR))

import real_ladybug as lb  # noqa: E402

from netops_api_navigator.graph.schema import (  # noqa: E402
    KNOWLEDGE_NODE_TABLES,
    KNOWLEDGE_REL_TABLES,
    NODE_TABLES,
    REL_TABLES,
    get_node_tables,
    get_rel_tables,
)


# ── Helpers ──────────────────────────────────────────────────────────


def _make_synthetic_spec() -> dict:
    """Spec with two endpoints sharing a referenced component graph.

    Layout::

        POST /v1/widgets   body=Widget    -> properties.rule -> Rule
                                          -> properties.tags -> array of TagEnum
        GET  /v1/widgets/{id}             returns Widget
    """
    rule = {
        "type": "object",
        "required": ["src", "dst"],
        "properties": {
            "src": {"type": "string", "description": "source"},
            "dst": {"type": "string"},
            "action": {"type": "string", "enum": ["allow", "deny"]},
        },
    }
    widget = {
        "type": "object",
        "required": ["name", "rule"],
        "properties": {
            "name": {"type": "string"},
            "rule": {"$ref": "#/components/schemas/Rule"},
            "tags": {
                "type": "array",
                "items": {"$ref": "#/components/schemas/TagEnum"},
            },
        },
    }
    tag_enum = {"type": "string", "enum": ["red", "green", "blue"]}
    return {
        "openapi": "3.0.0",
        "info": {"title": "Widgets API"},
        "components": {
            "schemas": {
                "Widget": copy.deepcopy(widget),
                "Rule": copy.deepcopy(rule),
                "TagEnum": copy.deepcopy(tag_enum),
            }
        },
        "paths": {
            "/v1/widgets": {
                "post": {
                    "summary": "Create a widget",
                    "operationId": "createWidget",
                    "tags": ["widgets"],
                    "parameters": [
                        {
                            "name": "scopeId",
                            "in": "query",
                            "required": True,
                            "schema": {"type": "string"},
                        },
                        {
                            "name": "filter",
                            "in": "query",
                            "required": False,
                            "description": "OData filter expression",
                            "schema": {"type": "string"},
                        },
                    ],
                    "requestBody": {
                        "required": True,
                        "content": {
                            "application/json": {
                                "schema": {"$ref": "#/components/schemas/Widget"}
                            }
                        },
                    },
                    "responses": {
                        "201": {
                            "description": "created",
                            "content": {
                                "application/json": {
                                    "schema": {"$ref": "#/components/schemas/Widget"}
                                }
                            },
                        },
                        "400": {"description": "bad"},
                    },
                }
            },
            "/v1/widgets/{id}": {
                "get": {
                    "summary": "Get a widget",
                    "operationId": "getWidget",
                    "tags": ["widgets"],
                    "parameters": [
                        {
                            "name": "id",
                            "in": "path",
                            "required": True,
                            "schema": {"type": "string"},
                        }
                    ],
                    "responses": {
                        "200": {
                            "content": {
                                "application/json": {
                                    "schema": {"$ref": "#/components/schemas/Widget"}
                                }
                            }
                        }
                    },
                },
            },
        },
    }


@pytest.fixture
def fresh_db():
    """Per-test temp LadybugDB with the full knowledge DDL applied."""
    with TemporaryDirectory(prefix="schema_graph_test_") as tmp:
        db_path = Path(tmp) / "graph_db"
        db = lb.Database(str(db_path), max_db_size=256 * 1024 * 1024)
        conn = lb.Connection(db)
        for ddl in NODE_TABLES + KNOWLEDGE_NODE_TABLES + REL_TABLES + KNOWLEDGE_REL_TABLES:
            conn.execute(ddl.strip())
        yield db, conn


# ── DDL surface tests ───────────────────────────────────────────────


class TestSchemaDDL:
    def test_new_node_tables_registered(self):
        tables = set(get_node_tables())
        for required in (
            "Parameter",
            "RequestBody",
            "Response",
            "SchemaComponent",
        ):
            assert required in tables, f"{required} missing from KNOWLEDGE_NODE_TABLES"

    def test_new_rel_tables_registered(self):
        rels = set(get_rel_tables())
        for required in (
            "HAS_PARAMETER",
            "HAS_REQUEST_BODY",
            "HAS_RESPONSE",
            "BODY_REFERENCES",
            "RESPONSE_REFERENCES",
            "REFERENCES",
        ):
            assert required in rels, f"{required} missing from KNOWLEDGE_REL_TABLES"

    def test_full_ddl_applies_cleanly(self, fresh_db):
        # Fixture itself raises if DDL is malformed.
        db, conn = fresh_db
        # Smoke: each new table is queryable (returns empty result without error).
        for tbl in (
            "Parameter",
            "RequestBody",
            "Response",
            "SchemaComponent",
        ):
            rows = list(conn.execute(f"MATCH (n:{tbl}) RETURN COUNT(n) AS c").rows_as_dict())
            assert rows[0]["c"] == 0


# ── Populate helper tests ──────────────────────────────────────────


def _seed_endpoint_node(conn: lb.Connection, method: str, path: str) -> str:
    """Insert the minimum ApiEndpoint row that the schema-graph populator needs."""
    eid = f"{method}:{path}"
    conn.execute(
        "CREATE (e:ApiEndpoint {endpoint_id: $eid, method: $m, path: $p, "
        "summary: '', description: '', operationId: '', category: '', "
        "deprecated: false, parameters: '', requestBody: '', responses: ''})",
        parameters={"eid": eid, "m": method, "p": path},
    )
    return eid


class TestPopulateSchemaGraph:
    """Tests for ``populate_schema_graph`` — the build-time helper that
    decomposes a normalised OAS spec into Parameter / RequestBody /
    Response / SchemaComponent rows."""

    def test_helper_is_importable(self):
        from netops_api_navigator.oas_schema_graph import populate_schema_graph  # noqa: F401

    def test_populates_parameter_rows(self, fresh_db):
        from netops_api_navigator.oas_schema_graph import populate_schema_graph

        db, conn = fresh_db
        spec = _make_synthetic_spec()
        _seed_endpoint_node(conn, "POST", "/v1/widgets")
        _seed_endpoint_node(conn, "GET", "/v1/widgets/{id}")

        populate_schema_graph(
            conn,
            spec_source="central",
            spec=spec,
            endpoints=[("POST", "/v1/widgets"), ("GET", "/v1/widgets/{id}")],
        )

        rows = list(conn.execute(
            "MATCH (e:ApiEndpoint)-[:HAS_PARAMETER]->(p:Parameter) "
            "WHERE e.endpoint_id = 'POST:/v1/widgets' "
            "RETURN p.name AS name, p.location AS loc, p.required AS req"
        ).rows_as_dict())
        names = {r["name"] for r in rows}
        assert names == {"scopeId", "filter"}
        scope = next(r for r in rows if r["name"] == "scopeId")
        assert scope["loc"] == "query"
        assert scope["req"] is True

    def test_parameter_description_is_persisted(self, fresh_db):
        """Phase 2E-2: Parameter.description must be extracted so glossary
        retirement doesn't lose human-readable parameter docs."""
        from netops_api_navigator.oas_schema_graph import populate_schema_graph

        db, conn = fresh_db
        spec = _make_synthetic_spec()
        _seed_endpoint_node(conn, "POST", "/v1/widgets")

        populate_schema_graph(
            conn,
            spec_source="central",
            spec=spec,
            endpoints=[("POST", "/v1/widgets")],
        )

        rows = list(conn.execute(
            "MATCH (e:ApiEndpoint)-[:HAS_PARAMETER]->(p:Parameter) "
            "WHERE e.endpoint_id = 'POST:/v1/widgets' "
            "RETURN p.name AS name, p.description AS description"
        ).rows_as_dict())
        by_name = {r["name"]: r["description"] for r in rows}
        assert by_name["filter"] == "OData filter expression"
        # `scopeId` had no description in the spec — empty string, not null.
        assert by_name["scopeId"] == ""

    def test_populates_schema_components_with_named_ids(self, fresh_db):
        from netops_api_navigator.oas_schema_graph import populate_schema_graph

        db, conn = fresh_db
        spec = _make_synthetic_spec()
        _seed_endpoint_node(conn, "POST", "/v1/widgets")

        populate_schema_graph(
            conn,
            spec_source="central",
            spec=spec,
            endpoints=[("POST", "/v1/widgets")],
        )

        rows = list(conn.execute(
            "MATCH (c:SchemaComponent) RETURN c.component_id AS id, c.name AS name, "
            "c.section AS section, c.spec_source AS src ORDER BY c.name"
        ).rows_as_dict())
        names = {r["name"] for r in rows}
        # Widget + Rule + TagEnum are all referenced from the request body.
        assert {"Widget", "Rule", "TagEnum"} <= names
        widget = next(r for r in rows if r["name"] == "Widget")
        assert widget["id"] == "central:schemas:Widget"
        assert widget["section"] == "schemas"
        assert widget["src"] == "central"

    def test_request_body_links_to_root_component(self, fresh_db):
        from netops_api_navigator.oas_schema_graph import populate_schema_graph

        db, conn = fresh_db
        spec = _make_synthetic_spec()
        _seed_endpoint_node(conn, "POST", "/v1/widgets")

        populate_schema_graph(
            conn,
            spec_source="central",
            spec=spec,
            endpoints=[("POST", "/v1/widgets")],
        )

        rows = list(conn.execute(
            "MATCH (e:ApiEndpoint)-[:HAS_REQUEST_BODY]->(rb:RequestBody)"
            "-[:BODY_REFERENCES]->(c:SchemaComponent) "
            "WHERE e.endpoint_id = 'POST:/v1/widgets' "
            "RETURN c.name AS name, rb.required AS req, rb.content_type AS ct"
        ).rows_as_dict())
        assert len(rows) == 1
        assert rows[0]["name"] == "Widget"
        assert rows[0]["req"] is True
        assert rows[0]["ct"] == "application/json"

    def test_references_carry_via_property(self, fresh_db):
        from netops_api_navigator.oas_schema_graph import populate_schema_graph

        db, conn = fresh_db
        spec = _make_synthetic_spec()
        _seed_endpoint_node(conn, "POST", "/v1/widgets")

        populate_schema_graph(
            conn,
            spec_source="central",
            spec=spec,
            endpoints=[("POST", "/v1/widgets")],
        )

        # Widget -[REFERENCES via=properties]-> Rule
        # Widget -[REFERENCES via=items]-> TagEnum
        rows = list(conn.execute(
            "MATCH (a:SchemaComponent {name: 'Widget'})-[r:REFERENCES]->(b:SchemaComponent) "
            "RETURN b.name AS name, r.via AS via ORDER BY b.name"
        ).rows_as_dict())
        by_name = {r["name"]: r["via"] for r in rows}
        assert by_name.get("Rule") == "properties"
        assert by_name.get("TagEnum") == "items"

    def test_response_links_to_root_component(self, fresh_db):
        from netops_api_navigator.oas_schema_graph import populate_schema_graph

        db, conn = fresh_db
        spec = _make_synthetic_spec()
        _seed_endpoint_node(conn, "POST", "/v1/widgets")
        _seed_endpoint_node(conn, "GET", "/v1/widgets/{id}")

        populate_schema_graph(
            conn,
            spec_source="central",
            spec=spec,
            endpoints=[("POST", "/v1/widgets"), ("GET", "/v1/widgets/{id}")],
        )

        rows = list(conn.execute(
            "MATCH (e:ApiEndpoint {endpoint_id: 'GET:/v1/widgets/{id}'})"
            "-[:HAS_RESPONSE]->(r:Response)-[:RESPONSE_REFERENCES]->(c:SchemaComponent) "
            "RETURN r.status AS status, c.name AS name"
        ).rows_as_dict())
        assert any(r["status"] == "200" and r["name"] == "Widget" for r in rows)

    def test_idempotent_population(self, fresh_db):
        """Running populate twice must not double-insert rows."""
        from netops_api_navigator.oas_schema_graph import populate_schema_graph

        db, conn = fresh_db
        spec = _make_synthetic_spec()
        _seed_endpoint_node(conn, "POST", "/v1/widgets")

        for _ in range(2):
            populate_schema_graph(
                conn,
                spec_source="central",
                spec=spec,
                endpoints=[("POST", "/v1/widgets")],
            )

        rows = list(conn.execute(
            "MATCH (c:SchemaComponent {name: 'Widget'}) RETURN COUNT(c) AS c"
        ).rows_as_dict())
        assert rows[0]["c"] == 1
        rows = list(conn.execute(
            "MATCH (e:ApiEndpoint)-[:HAS_PARAMETER]->(p:Parameter) "
            "WHERE e.endpoint_id = 'POST:/v1/widgets' RETURN COUNT(p) AS c"
        ).rows_as_dict())
        assert rows[0]["c"] == 2  # scopeId + filter, not 4
