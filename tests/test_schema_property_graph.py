"""Tests for the property-level API schema subgraph (Phase 2C of ADR 009).

These tests pin down the Property node, vendor-extension extraction
(``x-supportedDeviceType``, ``x-path``, all other ``x-*`` preserved as
JSON), allOf flattening into ``HAS_PROPERTY`` edges with provenance,
and ``COMPOSED_OF`` edges between components.

The synthetic spec mimics the actual Aruba Central NTP profile shape:
a top-level ``NtpprofileSchema`` that is purely an ``allOf`` over
several modular config components, each of which carries
``x-supportedDeviceType`` and friends on the leaves.
"""

from __future__ import annotations

import copy
import json
import sys
from pathlib import Path
from tempfile import TemporaryDirectory

import pytest

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


# ── Synthetic NTP-style spec ─────────────────────────────────────────


def _make_ntp_like_spec() -> dict:
    """An NTP-flavoured spec that exercises allOf flattening + x-* extensions.

    Structure::

        NtpprofileSchema
          allOf:
            $ref AuthenticationConfig   (properties: authenticate, key-value)
            $ref ServerConfig           (properties: server)
            inline { properties: name, vrf }
    """
    auth = {
        "type": "object",
        "description": "Authentication config",
        "properties": {
            "authenticate": {
                "type": "boolean",
                "description": "Enable auth",
                "x-supportedDeviceType": ["Gateway", "Switch CX"],
            },
            "key-value": {
                "type": "string",
                "description": "Auth secret",
                "x-supportedDeviceType": ["Switch CX"],
                "x-path": "/ac-ntp:ntp/ac-ntp:auth/ac-ntp:key-value",
                "x-typeDescription": "Secret string",
            },
        },
    }
    server = {
        "type": "object",
        "description": "Server config",
        "properties": {
            "server": {
                "type": "string",
                "description": "NTP server hostname",
                "x-supportedDeviceType": ["Switch PVOS", "Gateway", "Switch CX"],
            }
        },
    }
    ntp_profile = {
        "allOf": [
            {"$ref": "#/components/schemas/AuthenticationConfig"},
            {"$ref": "#/components/schemas/ServerConfig"},
            {
                "properties": {
                    "name": {
                        "type": "string",
                        "description": "Profile name",
                        "x-supportedDeviceType": [
                            "Switch PVOS",
                            "Gateway",
                            "Switch CX",
                        ],
                    },
                    "vrf": {
                        "type": "string",
                        "description": "Client VRF",
                        "x-supportedDeviceType": ["Switch CX"],
                        "x-path": "/ac-vrf:vrfs/ac-vrf:vrf/ac-vrf:name",
                    },
                }
            },
        ],
    }
    return {
        "openapi": "3.0.0",
        "info": {"title": "NTP API"},
        "components": {
            "schemas": {
                "NtpprofileSchema": copy.deepcopy(ntp_profile),
                "AuthenticationConfig": copy.deepcopy(auth),
                "ServerConfig": copy.deepcopy(server),
            }
        },
        "paths": {
            "/v1/ntp": {
                "post": {
                    "summary": "Create NTP profile",
                    "operationId": "createNtp",
                    "requestBody": {
                        "required": True,
                        "content": {
                            "application/json": {
                                "schema": {"$ref": "#/components/schemas/NtpprofileSchema"}
                            }
                        },
                    },
                    "responses": {
                        "201": {
                            "description": "created",
                            "content": {
                                "application/json": {
                                    "schema": {"$ref": "#/components/schemas/NtpprofileSchema"}
                                }
                            },
                        }
                    },
                }
            }
        },
    }


@pytest.fixture
def fresh_db():
    with TemporaryDirectory(prefix="phase2c_") as tmp:
        db_path = Path(tmp) / "graph_db"
        db = lb.Database(str(db_path), max_db_size=256 * 1024 * 1024)
        conn = lb.Connection(db)
        for ddl in NODE_TABLES + KNOWLEDGE_NODE_TABLES + REL_TABLES + KNOWLEDGE_REL_TABLES:
            conn.execute(ddl.strip())
        yield db, conn


def _seed_endpoint(conn, method: str, path: str) -> str:
    eid = f"{method}:{path}"
    conn.execute(
        "CREATE (e:ApiEndpoint {endpoint_id: $eid, method: $m, path: $p, "
        "summary: '', description: '', operationId: '', category: '', "
        "deprecated: false, parameters: '', requestBody: '', responses: ''})",
        parameters={"eid": eid, "m": method, "p": path},
    )
    return eid


def _seed(conn) -> None:
    from netops_api_navigator.oas_schema_graph import populate_schema_graph

    spec = _make_ntp_like_spec()
    _seed_endpoint(conn, "POST", "/v1/ntp")
    populate_schema_graph(
        conn,
        spec_source="central",
        spec=spec,
        endpoints=[("POST", "/v1/ntp")],
    )


# ── DDL surface ──────────────────────────────────────────────────────


class TestPropertyDDL:
    def test_property_node_table_registered(self):
        assert "Property" in set(get_node_tables())

    def test_new_rel_tables_registered(self):
        rels = set(get_rel_tables())
        assert "HAS_PROPERTY" in rels
        assert "PROPERTY_OF_TYPE" in rels
        assert "COMPOSED_OF" in rels


# ── Population ───────────────────────────────────────────────────────


class TestPopulateProperties:
    def test_components_have_their_own_properties(self, fresh_db):
        _, conn = fresh_db
        _seed(conn)
        rows = list(conn.execute(
            "MATCH (c:SchemaComponent {name: 'AuthenticationConfig'})-[:HAS_PROPERTY]->(p:Property) "
            "RETURN p.name AS name ORDER BY p.name"
        ).rows_as_dict())
        names = [r["name"] for r in rows]
        assert names == ["authenticate", "key-value"]

    def test_property_carries_supported_device_types(self, fresh_db):
        _, conn = fresh_db
        _seed(conn)
        rows = list(conn.execute(
            "MATCH (c:SchemaComponent {name: 'AuthenticationConfig'})-[:HAS_PROPERTY]->(p:Property {name: 'key-value'}) "
            "RETURN p.supportedDeviceTypes AS sdt"
        ).rows_as_dict())
        assert rows
        assert rows[0]["sdt"] == ["Switch CX"]

    def test_property_carries_yang_path(self, fresh_db):
        _, conn = fresh_db
        _seed(conn)
        rows = list(conn.execute(
            "MATCH (c:SchemaComponent {name: 'AuthenticationConfig'})-[:HAS_PROPERTY]->(p:Property {name: 'key-value'}) "
            "RETURN p.yangPath AS yp"
        ).rows_as_dict())
        assert rows
        assert rows[0]["yp"] == "/ac-ntp:ntp/ac-ntp:auth/ac-ntp:key-value"

    def test_extensions_json_preserves_other_x_keys(self, fresh_db):
        from netops_api_navigator.oas_schema_graph import decode_json_blob

        _, conn = fresh_db
        _seed(conn)
        rows = list(conn.execute(
            "MATCH (c:SchemaComponent {name: 'AuthenticationConfig'})-[:HAS_PROPERTY]->(p:Property {name: 'key-value'}) "
            "RETURN p.extensionsJson AS ext"
        ).rows_as_dict())
        assert rows
        ext = json.loads(decode_json_blob(rows[0]["ext"])) if rows[0]["ext"] else {}
        assert ext.get("x-typeDescription") == "Secret string"
        # Also includes the typed-extracted ones for completeness:
        assert ext.get("x-supportedDeviceType") == ["Switch CX"]
        assert ext.get("x-path") == "/ac-ntp:ntp/ac-ntp:auth/ac-ntp:key-value"

    def test_property_description_is_extracted(self, fresh_db):
        _, conn = fresh_db
        _seed(conn)
        rows = list(conn.execute(
            "MATCH (c:SchemaComponent {name: 'AuthenticationConfig'})-[:HAS_PROPERTY]->(p:Property {name: 'authenticate'}) "
            "RETURN p.description AS d, p.type AS t"
        ).rows_as_dict())
        assert rows
        assert rows[0]["d"] == "Enable auth"
        assert rows[0]["t"] == "boolean"



# ── readOnly extraction (Phase 2D-1) ────────────────────────────────


class TestReadOnlyExtraction:
    def _seed_with_readonly(self, conn) -> None:
        from netops_api_navigator.oas_schema_graph import populate_schema_graph

        spec = {
            "openapi": "3.0.0",
            "info": {"title": "RO API"},
            "components": {
                "schemas": {
                    "RoSchema": {
                        "type": "object",
                        "properties": {
                            "id": {"type": "string", "readOnly": True},
                            "name": {"type": "string"},
                            "createdAt": {"type": "string", "readOnly": True},
                        },
                    }
                }
            },
            "paths": {
                "/v1/ro": {
                    "post": {
                        "operationId": "createRo",
                        "requestBody": {
                            "required": True,
                            "content": {
                                "application/json": {
                                    "schema": {"$ref": "#/components/schemas/RoSchema"}
                                }
                            },
                        },
                        "responses": {"200": {"description": "ok"}},
                    }
                }
            },
        }
        _seed_endpoint(conn, "POST", "/v1/ro")
        populate_schema_graph(
            conn,
            spec_source="central",
            spec=spec,
            endpoints=[("POST", "/v1/ro")],
        )

    def test_readOnly_true_when_set(self, fresh_db):
        _, conn = fresh_db
        self._seed_with_readonly(conn)
        rows = list(conn.execute(
            "MATCH (:SchemaComponent {name: 'RoSchema'})-[:HAS_PROPERTY]->(p:Property) "
            "RETURN p.name AS n, p.readOnly AS ro ORDER BY p.name"
        ).rows_as_dict())
        by_name = {r["n"]: r["ro"] for r in rows}
        assert by_name == {"createdAt": True, "id": True, "name": False}

    def test_filter_writable_properties_via_cypher(self, fresh_db):
        """Headline: 'which fields can I include in a POST body?'"""
        _, conn = fresh_db
        self._seed_with_readonly(conn)
        rows = list(conn.execute(
            "MATCH (:SchemaComponent {name: 'RoSchema'})-[:HAS_PROPERTY]->(p:Property) "
            "WHERE p.readOnly = false "
            "RETURN p.name AS n"
        ).rows_as_dict())
        assert [r["n"] for r in rows] == ["name"]


# ── allOf flattening ────────────────────────────────────────────────


class TestAllOfFlattening:
    def test_composed_of_edges_recorded(self, fresh_db):
        _, conn = fresh_db
        _seed(conn)
        rows = list(conn.execute(
            "MATCH (a:SchemaComponent {name: 'NtpprofileSchema'})-[r:COMPOSED_OF]->(b:SchemaComponent) "
            "RETURN b.name AS name, r.kind AS kind ORDER BY b.name"
        ).rows_as_dict())
        names = sorted({(r["name"], r["kind"]) for r in rows})
        assert ("AuthenticationConfig", "allOf") in names
        assert ("ServerConfig", "allOf") in names

    def test_allOf_leaves_reachable_via_composed_of_walk(self, fresh_db):
        """NtpprofileSchema is allOf [Auth, Server, inline{name,vrf}].

        Properties live only on their declaring component. The canonical
        agent query walks ``COMPOSED_OF*0..N -> HAS_PROPERTY`` to gather
        every leaf, including the inline-promoted synthetic branch."""
        _, conn = fresh_db
        _seed(conn)
        rows = list(conn.execute(
            "MATCH (root:SchemaComponent {name: 'NtpprofileSchema'})"
            "-[:COMPOSED_OF*0..5]->(c:SchemaComponent)"
            "-[:HAS_PROPERTY]->(p:Property) "
            "RETURN DISTINCT p.name AS name ORDER BY name"
        ).rows_as_dict())
        names = sorted(r["name"] for r in rows)
        assert names == sorted(["authenticate", "key-value", "server", "name", "vrf"])

    def test_leaves_are_declared_on_their_branch_component(self, fresh_db):
        """Each leaf surfaces from the branch that declares it; no copies
        on the parent. Replaces the old ``inheritedFrom`` provenance column."""
        _, conn = fresh_db
        _seed(conn)
        rows = list(conn.execute(
            "MATCH (root:SchemaComponent {name: 'NtpprofileSchema'})"
            "-[:COMPOSED_OF*0..5]->(c:SchemaComponent)"
            "-[:HAS_PROPERTY]->(p:Property) "
            "RETURN p.name AS name, c.name AS declaredOn"
        ).rows_as_dict())
        by_name = {r["name"]: r["declaredOn"] for r in rows}
        assert by_name["authenticate"] == "AuthenticationConfig"
        assert by_name["key-value"] == "AuthenticationConfig"
        assert by_name["server"] == "ServerConfig"
        # Inline allOf branch is promoted to a synthetic component whose
        # name starts with the parent name.
        assert by_name["name"].startswith("NtpprofileSchema")
        assert by_name["vrf"].startswith("NtpprofileSchema")
        # The parent itself declares no fields directly.
        assert "NtpprofileSchema" not in set(by_name.values())

    def test_filter_by_supported_device_type_works_in_cypher(self, fresh_db):
        """The headline use case: 'show me the NTP fields valid for Switch CX'."""
        _, conn = fresh_db
        _seed(conn)
        rows = list(conn.execute(
            "MATCH (e:ApiEndpoint {endpoint_id: 'POST:/v1/ntp'})"
            "-[:HAS_REQUEST_BODY]->(:RequestBody)-[:BODY_REFERENCES]->"
            "(root:SchemaComponent) "
            "MATCH (root)-[:COMPOSED_OF*0..5]->(c:SchemaComponent)"
            "-[:HAS_PROPERTY]->(p:Property) "
            "WHERE 'Switch CX' IN p.supportedDeviceTypes "
            "RETURN p.name AS name ORDER BY p.name"
        ).rows_as_dict())
        names = sorted(r["name"] for r in rows)
        # Every leaf in our spec was supported on Switch CX.
        assert names == sorted(
            ["authenticate", "key-value", "server", "name", "vrf"]
        )


# ── Eviction integrity (richer-wins replacement) ─────────────────────


class TestEvictionPreservesGraphIntegrity:
    """Regression tests for the orphaned-Property bug.

    When a SchemaComponent is replaced by a richer body, the eviction
    helper must drop ALL buffered rows owned by the component AND by its
    inline descendants (e.g. ``{cid}#allOf:0``). A previous version only
    filtered direct-child properties (``{cid}#prop:*``) and edges with
    ``a == cid``, leaving inline-descendant HAS_PROPERTY edges in the
    buffer whose source SchemaComponent had been wiped. ``COPY`` then
    silently dropped those edges (foreign-key violation under
    ``ignore_errors=true``), orphaning ~42% of Property nodes on a real
    rebuild.
    """

    @staticmethod
    def _stub_spec() -> dict:
        """First definition of ``CommonProfile`` — allOf with ONE inline
        branch. Emits Properties whose ids carry the inline-child prefix
        (``CommonProfile#allOf:0#prop:*``), which the buggy eviction
        failed to clear from the dedup sets when the richer body
        arrived.
        """
        return {
            "openapi": "3.0.0",
            "info": {"title": "Stub"},
            "components": {
                "schemas": {
                    "CommonProfile": {
                        "allOf": [
                            {
                                "type": "object",
                                "properties": {"id": {"type": "string"}},
                            }
                        ],
                    }
                }
            },
            "paths": {
                "/v1/stub": {
                    "get": {
                        "operationId": "getStub",
                        "responses": {
                            "200": {
                                "description": "ok",
                                "content": {
                                    "application/json": {
                                        "schema": {
                                            "$ref": "#/components/schemas/CommonProfile"
                                        }
                                    }
                                },
                            }
                        },
                    }
                }
            },
        }

    @staticmethod
    def _rich_spec() -> dict:
        """Richer second definition that adds inline allOf branches with
        their own properties — exercises the inline-descendant eviction
        path that the bug missed.
        """
        return {
            "openapi": "3.0.0",
            "info": {"title": "Rich"},
            "components": {
                "schemas": {
                    "CommonProfile": {
                        "allOf": [
                            {
                                "type": "object",
                                "properties": {
                                    "id": {"type": "string"},
                                    "label": {"type": "string"},
                                },
                            },
                            {
                                "type": "object",
                                "properties": {
                                    "tags": {"type": "string"},
                                    "owner": {"type": "string"},
                                },
                            },
                        ],
                    }
                }
            },
            "paths": {
                "/v1/rich": {
                    "get": {
                        "operationId": "getRich",
                        "responses": {
                            "200": {
                                "description": "ok",
                                "content": {
                                    "application/json": {
                                        "schema": {
                                            "$ref": "#/components/schemas/CommonProfile"
                                        }
                                    }
                                },
                            }
                        },
                    }
                }
            },
        }

    def _seed_both(self, conn) -> None:
        from netops_api_navigator.oas_schema_graph import populate_schema_graph

        _seed_endpoint(conn, "GET", "/v1/stub")
        populate_schema_graph(
            conn,
            spec_source="central",
            spec=self._stub_spec(),
            endpoints=[("GET", "/v1/stub")],
        )
        _seed_endpoint(conn, "GET", "/v1/rich")
        populate_schema_graph(
            conn,
            spec_source="central",
            spec=self._rich_spec(),
            endpoints=[("GET", "/v1/rich")],
        )

    def test_no_orphaned_properties_after_richest_wins_replacement(self, fresh_db):
        """INV-8 in spirit: every Property must have at least one
        incoming HAS_PROPERTY edge.
        """
        _, conn = fresh_db
        self._seed_both(conn)
        rows = list(conn.execute(
            "MATCH (p:Property) "
            "WHERE NOT EXISTS { MATCH (:SchemaComponent)-[:HAS_PROPERTY]->(p) } "
            "RETURN p.property_id AS pid LIMIT 25"
        ).rows_as_dict())
        assert rows == [], (
            f"Found {len(rows)} orphaned Property nodes after eviction: "
            f"{[r['pid'] for r in rows]}"
        )

    def test_richer_inline_branches_are_reachable(self, fresh_db):
        """Walking from the richer-wins component must surface every
        leaf declared on its inline allOf branches."""
        _, conn = fresh_db
        self._seed_both(conn)
        rows = list(conn.execute(
            "MATCH (root:SchemaComponent {name: 'CommonProfile'})"
            "-[:COMPOSED_OF*0..5]->(c:SchemaComponent)"
            "-[:HAS_PROPERTY]->(p:Property) "
            "RETURN DISTINCT p.name AS name ORDER BY name"
        ).rows_as_dict())
        names = sorted(r["name"] for r in rows)
        assert names == sorted(["id", "label", "tags", "owner"])


# ── Inline schemas (no $ref) materialised as synthetic components ───


def _make_inline_servers_spec() -> dict:
    """Mimics NTP-style ``servers`` array with inline item schema (no $ref).

    The shape that previously stranded sub-properties inside ``bodyJson``::

        NtpServers
          properties:
            servers:
              type: array
              items:                      # <-- inline, NO $ref
                type: object
                properties:
                  address: string
                  prefer:  boolean
            tags:
              type: array
              items:                      # inline scalar items must NOT
                type: string              # mint a synthetic component
            location:
              type: object                # inline object property
              properties:
                lat: number
                lon: number
    """
    components = {
        "schemas": {
            "NtpServers": {
                "type": "object",
                "properties": {
                    "servers": {
                        "type": "array",
                        "items": {
                            "type": "object",
                            "required": ["address"],
                            "properties": {
                                "address": {
                                    "type": "string",
                                    "description": "NTP server hostname",
                                    "x-supportedDeviceType": ["Switch CX"],
                                },
                                "prefer": {
                                    "type": "boolean",
                                    "description": "Mark as preferred",
                                },
                            },
                        },
                    },
                    "tags": {
                        "type": "array",
                        "items": {"type": "string"},
                    },
                    "location": {
                        "type": "object",
                        "properties": {
                            "lat": {"type": "number"},
                            "lon": {"type": "number"},
                        },
                    },
                },
            }
        }
    }
    return {
        "openapi": "3.0.0",
        "info": {"title": "NTP", "version": "1"},
        "components": components,
        "paths": {
            "/v1/ntp-servers": {
                "post": {
                    "operationId": "putServers",
                    "requestBody": {
                        "required": True,
                        "content": {
                            "application/json": {
                                "schema": {"$ref": "#/components/schemas/NtpServers"}
                            }
                        },
                    },
                    "responses": {"200": {"description": "ok"}},
                }
            }
        },
    }


def _seed_inline(conn) -> None:
    from netops_api_navigator.oas_schema_graph import populate_schema_graph

    spec = _make_inline_servers_spec()
    _seed_endpoint(conn, "POST", "/v1/ntp-servers")
    populate_schema_graph(
        conn,
        spec_source="central",
        spec=spec,
        endpoints=[("POST", "/v1/ntp-servers")],
    )


class TestInlineSchemaMaterialisation:
    """Inline ``items`` / ``properties`` (no ``$ref``) must appear in the
    property subgraph, not be stranded inside opaque ``bodyJson``."""

    def test_inline_array_items_become_synthetic_component(self, fresh_db):
        _, conn = fresh_db
        _seed_inline(conn)
        rows = list(conn.execute(
            "MATCH (sc:SchemaComponent {name: 'NtpServers'})"
            "-[:HAS_PROPERTY]->(p:Property {name: 'servers'})"
            "-[:PROPERTY_OF_TYPE]->(items:SchemaComponent) "
            "RETURN items.component_id AS cid, items.section AS section"
        ).rows_as_dict())
        assert len(rows) == 1, rows
        assert rows[0]["cid"].endswith("#items"), rows[0]
        assert rows[0]["section"] == "inline"

    def test_inline_array_item_subproperties_are_extracted(self, fresh_db):
        _, conn = fresh_db
        _seed_inline(conn)
        rows = list(conn.execute(
            "MATCH (sc:SchemaComponent {name: 'NtpServers'})"
            "-[:HAS_PROPERTY]->(:Property {name: 'servers'})"
            "-[:PROPERTY_OF_TYPE]->(:SchemaComponent)"
            "-[:HAS_PROPERTY]->(child:Property) "
            "RETURN child.name AS name, child.type AS type, child.required AS req "
            "ORDER BY child.name"
        ).rows_as_dict())
        names = [(r["name"], r["type"], bool(r["req"])) for r in rows]
        assert names == [
            ("address", "string", True),
            ("prefer", "boolean", False),
        ]

    def test_inline_array_item_extensions_preserved(self, fresh_db):
        _, conn = fresh_db
        _seed_inline(conn)
        rows = list(conn.execute(
            "MATCH (:Property {name: 'servers'})"
            "-[:PROPERTY_OF_TYPE]->(:SchemaComponent)"
            "-[:HAS_PROPERTY]->(p:Property {name: 'address'}) "
            "RETURN p.supportedDeviceTypes AS sdt"
        ).rows_as_dict())
        assert rows[0]["sdt"] == ["Switch CX"]

    def test_inline_object_property_becomes_synthetic_component(self, fresh_db):
        _, conn = fresh_db
        _seed_inline(conn)
        rows = list(conn.execute(
            "MATCH (:SchemaComponent {name: 'NtpServers'})"
            "-[:HAS_PROPERTY]->(p:Property {name: 'location'})"
            "-[:PROPERTY_OF_TYPE]->(obj:SchemaComponent)"
            "-[:HAS_PROPERTY]->(child:Property) "
            "RETURN child.name AS name ORDER BY child.name"
        ).rows_as_dict())
        assert [r["name"] for r in rows] == ["lat", "lon"]

    def test_inline_scalar_array_items_do_not_mint_component(self, fresh_db):
        # `tags: [string]` has no nested structure to extract.
        _, conn = fresh_db
        _seed_inline(conn)
        rows = list(conn.execute(
            "MATCH (:SchemaComponent {name: 'NtpServers'})"
            "-[:HAS_PROPERTY]->(p:Property {name: 'tags'})"
            "-[:PROPERTY_OF_TYPE]->(c:SchemaComponent) "
            "RETURN c.component_id AS cid"
        ).rows_as_dict())
        assert rows == []

    def test_synthetic_component_id_is_deterministic(self, fresh_db):
        # Build the graph twice in the same DB; second run must dedupe and
        # not raise primary-key violations on the synthetic SchemaComponent.
        _, conn = fresh_db
        _seed_inline(conn)
        # Re-running populate against the same endpoint set must be a no-op
        # for the synthetic node — second insert would crash on PK collision
        # if the id were not deterministic.
        from netops_api_navigator.oas_schema_graph import populate_schema_graph
        populate_schema_graph(
            conn,
            spec_source="central",
            spec=_make_inline_servers_spec(),
            endpoints=[("POST", "/v1/ntp-servers")],
        )
        rows = list(conn.execute(
            "MATCH (c:SchemaComponent) WHERE c.section = 'inline' "
            "RETURN count(c) AS n"
        ).rows_as_dict())
        # exactly two synthetic components: servers#items and location#object
        assert rows[0]["n"] == 2

    def test_endpoint_to_inline_subproperty_traversal(self, fresh_db):
        """End-to-end: starting from the endpoint, can the agent reach the
        inline `servers[].address` field via pure graph traversal?"""
        _, conn = fresh_db
        _seed_inline(conn)
        rows = list(conn.execute(
            "MATCH (e:ApiEndpoint {endpoint_id: 'POST:/v1/ntp-servers'})"
            "-[:HAS_REQUEST_BODY]->(:RequestBody)-[:BODY_REFERENCES]->"
            "(:SchemaComponent)-[:HAS_PROPERTY]->(:Property {name: 'servers'})"
            "-[:PROPERTY_OF_TYPE]->(:SchemaComponent)"
            "-[:HAS_PROPERTY]->(p:Property) "
            "RETURN p.name AS name ORDER BY p.name"
        ).rows_as_dict())
        assert [r["name"] for r in rows] == ["address", "prefer"]


# ── EC1: absent x-supportedDeviceType lifts to NULL ─────────────────


def _make_spec_with_unannotated_property() -> dict:
    """Spec where one property has NO x-supportedDeviceType extension.

    Used to assert that absent ⇒ NULL (not empty list), so consumers
    can filter with ``p.supportedDeviceTypes IS NULL OR ...`` to mean
    "applies to all device types".
    """
    return {
        "openapi": "3.0.0",
        "info": {"title": "EC1 fixture"},
        "components": {
            "schemas": {
                "MixedConfig": {
                    "type": "object",
                    "properties": {
                        "annotated": {
                            "type": "string",
                            "x-supportedDeviceType": ["Switch CX"],
                        },
                        "unannotated": {
                            "type": "string",
                        },
                    },
                }
            }
        },
        "paths": {
            "/v1/mixed": {
                "post": {
                    "operationId": "postMixed",
                    "requestBody": {
                        "required": True,
                        "content": {
                            "application/json": {
                                "schema": {"$ref": "#/components/schemas/MixedConfig"}
                            }
                        },
                    },
                    "responses": {"200": {"description": "ok"}},
                }
            }
        },
    }


class TestSupportedDeviceTypesNullSemantic:
    def test_property_without_extension_is_null(self, fresh_db):
        _, conn = fresh_db
        _seed_endpoint(conn, "POST", "/v1/mixed")
        from netops_api_navigator.oas_schema_graph import populate_schema_graph
        populate_schema_graph(
            conn,
            spec_source="central",
            spec=_make_spec_with_unannotated_property(),
            endpoints=[("POST", "/v1/mixed")],
        )
        rows = list(conn.execute(
            "MATCH (:SchemaComponent {name: 'MixedConfig'})"
            "-[:HAS_PROPERTY]->(p:Property {name: 'unannotated'}) "
            "RETURN p.supportedDeviceTypes AS sdt"
        ).rows_as_dict())
        assert rows, "unannotated property should still exist"
        assert rows[0]["sdt"] is None, (
            "absent x-supportedDeviceType should lift to NULL, "
            f"got {rows[0]['sdt']!r}"
        )

    def test_property_with_extension_keeps_list(self, fresh_db):
        _, conn = fresh_db
        _seed_endpoint(conn, "POST", "/v1/mixed")
        from netops_api_navigator.oas_schema_graph import populate_schema_graph
        populate_schema_graph(
            conn,
            spec_source="central",
            spec=_make_spec_with_unannotated_property(),
            endpoints=[("POST", "/v1/mixed")],
        )
        rows = list(conn.execute(
            "MATCH (:SchemaComponent {name: 'MixedConfig'})"
            "-[:HAS_PROPERTY]->(p:Property {name: 'annotated'}) "
            "RETURN p.supportedDeviceTypes AS sdt"
        ).rows_as_dict())
        assert rows[0]["sdt"] == ["Switch CX"]

    def test_canonical_is_null_filter_returns_all_devices_properties(self, fresh_db):
        """The EC1 fix: ``IS NULL OR $deviceType IN ...`` returns both
        the device-specific annotated row AND the unannotated row that
        "applies to every device"."""
        _, conn = fresh_db
        _seed_endpoint(conn, "POST", "/v1/mixed")
        from netops_api_navigator.oas_schema_graph import populate_schema_graph
        populate_schema_graph(
            conn,
            spec_source="central",
            spec=_make_spec_with_unannotated_property(),
            endpoints=[("POST", "/v1/mixed")],
        )
        rows = list(conn.execute(
            "MATCH (e:ApiEndpoint {endpoint_id: 'POST:/v1/mixed'})"
            "-[:HAS_REQUEST_BODY]->(:RequestBody)-[:BODY_REFERENCES]->"
            "(root:SchemaComponent) "
            "MATCH (root)-[:COMPOSED_OF*0..5]->(c:SchemaComponent)"
            "-[:HAS_PROPERTY]->(p:Property) "
            "WHERE p.supportedDeviceTypes IS NULL "
            "   OR size(p.supportedDeviceTypes) = 0 "
            "   OR $dt IN p.supportedDeviceTypes "
            "RETURN p.name AS name ORDER BY p.name",
            parameters={"dt": "Switch CX"},
        ).rows_as_dict())
        names = sorted(r["name"] for r in rows)
        assert names == ["annotated", "unannotated"]


# ── Warning logs ─────────────────────────────────────────────────────


class TestPopulatorWarningLogs:
    """Best-effort failures during population must emit structured warnings
    instead of being silently swallowed."""

    def test_warning_logged_when_requestbody_has_no_schema(self, fresh_db):
        """A requestBody with content but no resolvable schema must log a
        ``requestbody_without_schema_root`` warning so operators can spot
        malformed specs without diffing the DB."""
        import structlog

        _, conn = fresh_db
        _seed_endpoint(conn, "POST", "/v1/no-schema")
        spec = {
            "openapi": "3.0.0",
            "info": {"title": "x"},
            "paths": {
                "/v1/no-schema": {
                    "post": {
                        "requestBody": {
                            "required": True,
                            "content": {
                                "application/json": {
                                    # no `schema` key on purpose
                                },
                            },
                        },
                        "responses": {"200": {"description": "ok"}},
                    }
                }
            },
        }
        from netops_api_navigator.oas_schema_graph import populate_schema_graph
        with structlog.testing.capture_logs() as cap:
            populate_schema_graph(
                conn,
                spec_source="central",
                spec=spec,
                endpoints=[("POST", "/v1/no-schema")],
            )
        events = [e for e in cap if e.get("event") == "requestbody_without_schema_root"]
        assert events, f"expected requestbody_without_schema_root warning, got {cap!r}"
        ev = events[0]
        assert ev["endpoint_id"] == "POST:/v1/no-schema"
        assert ev["method"] == "POST"
        assert ev["path"] == "/v1/no-schema"
        assert "application/json" in ev["content_types"]
        assert ev["log_level"] == "warning"
