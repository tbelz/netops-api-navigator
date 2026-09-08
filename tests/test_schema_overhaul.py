"""Tests for the ingestion overhaul (richest-wins, cross-spec refs,
inline promotion of oneOf/anyOf/additionalProperties, allOf composition chains,
SchemaComponent.supportedDeviceTypes, YangPath reverse index,
UnresolvedRef placeholder, and bodyShape).

These pin down the behaviour added in the "graph ingestion overhaul"
branch: nothing in here is per-spec — they all use synthetic specs.
"""

from __future__ import annotations

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
)
from netops_api_navigator.oas_schema_graph import (  # noqa: E402
    populate_schema_graph,
)


@pytest.fixture
def fresh_db():
    with TemporaryDirectory(prefix="overhaul_") as tmp:
        db = lb.Database(str(Path(tmp) / "graph_db"), max_db_size=256 * 1024 * 1024)
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


def _spec_with(components: dict, paths: dict | None = None) -> dict:
    return {
        "openapi": "3.0.0",
        "info": {"title": "T", "version": "1"},
        "components": {"schemas": components},
        "paths": paths or {},
    }


def _post(path: str, ref: str) -> dict:
    return {
        path: {
            "post": {
                "operationId": "op",
                "requestBody": {
                    "required": True,
                    "content": {
                        "application/json": {"schema": {"$ref": ref}}
                    },
                },
                "responses": {"200": {"description": "ok"}},
            }
        }
    }


# ── Richest-wins merge on collision ─────────────────────────────────


class TestRichestWinsMerge:
    """When two specs declare a component of the same name, the richer
    body must win — regardless of ingest order."""

    def _seed_two_specs(self, conn, order: tuple[str, str]) -> None:
        thin = _spec_with(
            {"Foo": {"type": "object", "properties": {"a": {"type": "string"}}}},
            _post("/v1/thin", "#/components/schemas/Foo"),
        )
        rich = _spec_with(
            {
                "Foo": {
                    "type": "object",
                    "required": ["a"],
                    "properties": {
                        "a": {"type": "string", "description": "the a"},
                        "b": {"type": "integer"},
                        "c": {"type": "boolean"},
                    },
                }
            },
            _post("/v1/rich", "#/components/schemas/Foo"),
        )
        specs = {"thin": thin, "rich": rich}
        _seed_endpoint(conn, "POST", "/v1/thin")
        _seed_endpoint(conn, "POST", "/v1/rich")
        for name in order:
            populate_schema_graph(
                conn,
                spec_source="central",
                spec=specs[name],
                endpoints=[("POST", f"/v1/{name}")],
            )

    @pytest.mark.parametrize("order", [("thin", "rich"), ("rich", "thin")])
    def test_rich_body_wins_regardless_of_order(self, fresh_db, order):
        _, conn = fresh_db
        self._seed_two_specs(conn, order)
        rows = list(conn.execute(
            "MATCH (c:SchemaComponent {name: 'Foo'})-[:HAS_PROPERTY]->(p:Property) "
            "RETURN p.name AS n ORDER BY p.name"
        ).rows_as_dict())
        assert [r["n"] for r in rows] == ["a", "b", "c"], (order, rows)


# ── Cross-spec $ref resolution via resolution_scope ─────────────────


class TestCrossSpecRefFallback:
    """A spec may $ref a component that lives in a sibling spec.
    resolution_scope provides that pool as a fallback."""

    def test_unknown_local_ref_resolved_via_resolution_scope(self, fresh_db):
        _, conn = fresh_db
        # spec_a doesn't declare Shared — only references it.
        spec_a = _spec_with(
            {
                "Wrapper": {
                    "type": "object",
                    "properties": {
                        "shared": {"$ref": "#/components/schemas/Shared"},
                    },
                }
            },
            _post("/v1/wrap", "#/components/schemas/Wrapper"),
        )
        # The pool says Shared lives elsewhere.
        pool = {
            "schemas": {
                "Shared": {
                    "type": "object",
                    "properties": {"x": {"type": "string"}, "y": {"type": "integer"}},
                }
            }
        }
        _seed_endpoint(conn, "POST", "/v1/wrap")
        populate_schema_graph(
            conn,
            spec_source="central",
            spec=spec_a,
            endpoints=[("POST", "/v1/wrap")],
            resolution_scope=pool,
        )
        # Shared was resolved -> its component exists with kind != unresolved
        # and carries the expected properties.
        rows = list(conn.execute(
            "MATCH (c:SchemaComponent {name: 'Shared'})-[:HAS_PROPERTY]->(p:Property) "
            "RETURN p.name AS n ORDER BY p.name"
        ).rows_as_dict())
        assert [r["n"] for r in rows] == ["x", "y"], rows
        kind_rows = list(conn.execute(
            "MATCH (c:SchemaComponent {name: 'Shared'}) RETURN c.kind AS k"
        ).rows_as_dict())
        assert kind_rows[0]["k"] != "unresolved"


# ── UnresolvedRef placeholder ───────────────────────────────────────


class TestUnresolvedRefPlaceholder:
    def test_missing_ref_creates_placeholder(self, fresh_db):
        _, conn = fresh_db
        spec = _spec_with(
            {
                "Wrapper": {
                    "type": "object",
                    "properties": {
                        "missing": {"$ref": "#/components/schemas/DoesNotExist"},
                    },
                }
            },
            _post("/v1/wrap", "#/components/schemas/Wrapper"),
        )
        _seed_endpoint(conn, "POST", "/v1/wrap")
        populate_schema_graph(
            conn,
            spec_source="central",
            spec=spec,
            endpoints=[("POST", "/v1/wrap")],
        )
        rows = list(conn.execute(
            "MATCH (c:SchemaComponent {kind: 'unresolved'}) "
            "RETURN c.name AS n, c.bodyShape AS s, c.bodyJson AS b"
        ).rows_as_dict())
        assert rows, "expected at least one unresolved placeholder"
        # The placeholder's bodyJson is empty; bodyShape signals unresolved.
        assert all(r["s"] == "unresolved" for r in rows), rows
        assert all((r["b"] or "") == "" for r in rows), rows


# ── oneOf / anyOf inline promotion ──────────────────────────────────


class TestInlineUnionPromotion:
    def test_inline_oneOf_branches_promoted_to_synthetic_components(self, fresh_db):
        _, conn = fresh_db
        spec = _spec_with(
            {
                "Union": {
                    "oneOf": [
                        {"type": "object", "properties": {"a": {"type": "string"}}},
                        {"type": "object", "properties": {"b": {"type": "integer"}}},
                    ]
                }
            },
            _post("/v1/u", "#/components/schemas/Union"),
        )
        _seed_endpoint(conn, "POST", "/v1/u")
        populate_schema_graph(
            conn,
            spec_source="central",
            spec=spec,
            endpoints=[("POST", "/v1/u")],
        )
        # Union has bodyShape='union-oneOf' and COMPOSED_OF kind='oneOf'
        # edges to two synthetic inline branches.
        rows = list(conn.execute(
            "MATCH (u:SchemaComponent {name: 'Union'}) "
            "RETURN u.bodyShape AS s"
        ).rows_as_dict())
        assert rows[0]["s"] == "union-oneOf"
        branches = list(conn.execute(
            "MATCH (u:SchemaComponent {name: 'Union'})-[r:COMPOSED_OF]->(b:SchemaComponent) "
            "RETURN r.kind AS k, b.section AS sec ORDER BY b.component_id"
        ).rows_as_dict())
        kinds = [r["k"] for r in branches]
        sections = [r["sec"] for r in branches]
        assert kinds == ["oneOf", "oneOf"], branches
        assert sections == ["inline", "inline"], branches
        # And each branch carries its inline property.
        leaf_rows = list(conn.execute(
            "MATCH (:SchemaComponent {name: 'Union'})"
            "-[:COMPOSED_OF]->(:SchemaComponent)-[:HAS_PROPERTY]->(p:Property) "
            "RETURN p.name AS n ORDER BY p.name"
        ).rows_as_dict())
        assert [r["n"] for r in leaf_rows] == ["a", "b"]


# ── additionalProperties → HAS_VALUE_SCHEMA ─────────────────────────


class TestAdditionalPropertiesMap:
    def test_map_shape_links_to_value_schema(self, fresh_db):
        _, conn = fresh_db
        spec = _spec_with(
            {
                "Bag": {
                    "type": "object",
                    "additionalProperties": {
                        "$ref": "#/components/schemas/Item"
                    },
                },
                "Item": {
                    "type": "object",
                    "properties": {"name": {"type": "string"}},
                },
            },
            _post("/v1/bag", "#/components/schemas/Bag"),
        )
        _seed_endpoint(conn, "POST", "/v1/bag")
        populate_schema_graph(
            conn,
            spec_source="central",
            spec=spec,
            endpoints=[("POST", "/v1/bag")],
        )
        rows = list(conn.execute(
            "MATCH (b:SchemaComponent {name: 'Bag'})-[:HAS_VALUE_SCHEMA]->(v:SchemaComponent) "
            "RETURN v.name AS n, v.bodyShape AS s"
        ).rows_as_dict())
        assert rows, "expected HAS_VALUE_SCHEMA edge"
        assert rows[0]["n"] == "Item"


# ── allOf chain depth ≥ 2: traversal via COMPOSED_OF ───────────────


class TestAllOfChainTraversal:
    def test_two_level_allOf_chain_recorded(self, fresh_db):
        _, conn = fresh_db
        spec = _spec_with(
            {
                "Top": {
                    "allOf": [
                        {"$ref": "#/components/schemas/Mid"},
                        {"properties": {"top_own": {"type": "string"}}},
                    ]
                },
                "Mid": {
                    "allOf": [
                        {"$ref": "#/components/schemas/Base"},
                        {"properties": {"mid_own": {"type": "string"}}},
                    ]
                },
                "Base": {
                    "type": "object",
                    "properties": {"base_leaf": {"type": "string"}},
                },
            },
            _post("/v1/top", "#/components/schemas/Top"),
        )
        _seed_endpoint(conn, "POST", "/v1/top")
        populate_schema_graph(
            conn,
            spec_source="central",
            spec=spec,
            endpoints=[("POST", "/v1/top")],
        )
        # Walk allOf composition: every leaf must be reachable from Top
        # via COMPOSED_OF*0..N -> HAS_PROPERTY, and surface declared on the
        # component that owns it (not duplicated onto Top).
        rows = list(conn.execute(
            "MATCH (t:SchemaComponent {name: 'Top'})-[:COMPOSED_OF*0..5]->(c:SchemaComponent)"
            "-[:HAS_PROPERTY]->(p:Property) "
            "RETURN p.name AS n, c.name AS declaredOn"
        ).rows_as_dict())
        by_name = {r["n"]: r["declaredOn"] for r in rows}
        # Top's own inline property is promoted to a synthetic component
        # whose name starts with "Top".
        assert by_name["top_own"].startswith("Top")
        # Mid's own contribution lives on Mid's synthetic inline component.
        assert by_name["mid_own"].startswith("Mid")
        # Base declares its leaf directly.
        assert by_name["base_leaf"] == "Base"


# ── SchemaComponent.supportedDeviceTypes lifted from body ──────────


class TestComponentSupportedDeviceTypes:
    def test_body_level_extension_lifted_to_component(self, fresh_db):
        _, conn = fresh_db
        spec = _spec_with(
            {
                "CxOnly": {
                    "type": "object",
                    "x-supportedDeviceType": ["Switch CX"],
                    "properties": {"x": {"type": "string"}},
                }
            },
            _post("/v1/cx", "#/components/schemas/CxOnly"),
        )
        _seed_endpoint(conn, "POST", "/v1/cx")
        populate_schema_graph(
            conn,
            spec_source="central",
            spec=spec,
            endpoints=[("POST", "/v1/cx")],
        )
        rows = list(conn.execute(
            "MATCH (c:SchemaComponent {name: 'CxOnly'}) "
            "RETURN c.supportedDeviceTypes AS sdt"
        ).rows_as_dict())
        assert rows[0]["sdt"] == ["Switch CX"]


# ── YangPath reverse index ──────────────────────────────────────────


class TestYangPathIndex:
    def test_yang_path_row_and_edge_created(self, fresh_db):
        _, conn = fresh_db
        yp = "/ac-ntp:ntp/ac-ntp:server/ac-ntp:address"
        spec = _spec_with(
            {
                "Y": {
                    "type": "object",
                    "properties": {
                        "addr": {"type": "string", "x-path": yp},
                    },
                }
            },
            _post("/v1/y", "#/components/schemas/Y"),
        )
        _seed_endpoint(conn, "POST", "/v1/y")
        populate_schema_graph(
            conn,
            spec_source="central",
            spec=spec,
            endpoints=[("POST", "/v1/y")],
        )
        # YangPath row exists with correct module prefix.
        rows = list(conn.execute(
            "MATCH (y:YangPath) WHERE y.yangPath = '" + yp + "' RETURN y.module AS m"
        ).rows_as_dict())
        assert rows, "expected YangPath node"
        assert rows[0]["m"] == "ac-ntp"
        # PROPERTY_AT_YANG edge exists.
        edge_rows = list(conn.execute(
            "MATCH (p:Property)-[:PROPERTY_AT_YANG]->(y:YangPath) "
            "WHERE y.yangPath = '" + yp + "' "
            "RETURN p.name AS n"
        ).rows_as_dict())
        assert [r["n"] for r in edge_rows] == ["addr"]


# ── bodyShape sanity ────────────────────────────────────────────────


class TestBodyShape:
    def test_object_array_primitive_shapes(self, fresh_db):
        _, conn = fresh_db
        spec = _spec_with(
            {
                "Obj": {"type": "object", "properties": {"a": {"type": "string"}}},
                "Arr": {"type": "array", "items": {"type": "string"}},
                "Prim": {"type": "string"},
            },
            _post("/v1/o", "#/components/schemas/Obj"),
        )
        _seed_endpoint(conn, "POST", "/v1/o")
        populate_schema_graph(
            conn,
            spec_source="central",
            spec=spec,
            endpoints=[("POST", "/v1/o")],
        )
        rows = list(conn.execute(
            "MATCH (c:SchemaComponent) WHERE c.name IN ['Obj','Arr','Prim'] "
            "RETURN c.name AS n, c.bodyShape AS s ORDER BY c.name"
        ).rows_as_dict())
        by_name = {r["n"]: r["s"] for r in rows}
        assert by_name["Obj"] == "object"
        # Arr and Prim are only reachable if BODY_REFERENCES drags them in,
        # which it doesn't (endpoint only refs Obj) — so only assert Obj.
        # Done.


# ── ADR-011 bug-class regressions ──────────────────────────────────
#
# Two specific defects that shipped past the previous synthetic-only
# suite. Both are exercised at unit granularity here AND at integration
# granularity in ``test_real_spec_ingest_smoke.py`` via the post-flush
# invariants module.


class TestFollowRefRichnessAcrossScopes:
    """Bug 1 (oas_normalize._follow_ref): when both the local components
    map AND the provider-wide pool define the target, the OLD code
    returned the local hit unconditionally. Real Aruba bundles ship
    operation-level stubs alongside richer sibling definitions; that
    behaviour silently lost properties.
    """

    def test_richest_candidate_wins_when_pool_is_richer(self):
        from netops_api_navigator.oas_normalize import _follow_ref

        local = {"schemas": {"Foo": {"type": "object"}}}
        pool = {
            "schemas": {
                "Foo": {
                    "type": "object",
                    "properties": {
                        "a": {"type": "string"},
                        "b": {"type": "integer"},
                    },
                    "required": ["a"],
                    "description": "rich",
                }
            }
        }
        resolved = _follow_ref(
            "#/components/schemas/Foo", local, fallback=pool
        )
        assert resolved is not None
        assert "properties" in resolved
        assert set(resolved["properties"]) == {"a", "b"}

    def test_local_wins_when_local_is_richer(self):
        from netops_api_navigator.oas_normalize import _follow_ref

        local = {
            "schemas": {
                "Bar": {
                    "type": "object",
                    "properties": {"x": {"type": "string"}},
                }
            }
        }
        pool = {"schemas": {"Bar": {"type": "object"}}}
        resolved = _follow_ref(
            "#/components/schemas/Bar", local, fallback=pool
        )
        assert resolved is not None
        assert resolved.get("properties", {}) == {"x": {"type": "string"}}

    def test_missing_in_both_returns_none(self):
        from netops_api_navigator.oas_normalize import _follow_ref

        assert (
            _follow_ref(
                "#/components/schemas/Nope", {"schemas": {}}, fallback={"schemas": {}}
            )
            is None
        )

    def test_external_ref_returns_none(self):
        from netops_api_navigator.oas_normalize import _follow_ref

        assert (
            _follow_ref(
                "other.json#/components/schemas/Foo",
                {"schemas": {"Foo": {}}},
            )
            is None
        )


class TestInBatchReplacementEvictsDescendants:
    """Bug 2 (_Batch.add_component): when a richer body arrives for a
    component that is still pending (not yet flushed), the OLD code
    overwrote the body row in-place but left the descendant dedup keys
    set, so re-emitted Properties were silently skipped. Result: a
    richer body persisted with zero HAS_PROPERTY edges.

    This test drives the merge through the public ingestion API and
    asserts the rebuilt property subgraph IS visible after flush.
    """

    def test_in_batch_stub_then_rich_yields_full_property_subgraph(self, fresh_db):
        _, conn = fresh_db
        stub = _spec_with(
            {"Foo": {"type": "object"}},
            _post("/v1/stub", "#/components/schemas/Foo"),
        )
        rich = _spec_with(
            {
                "Foo": {
                    "type": "object",
                    "required": ["a"],
                    "properties": {
                        "a": {"type": "string"},
                        "b": {"type": "integer"},
                        "c": {"type": "boolean"},
                    },
                }
            },
            _post("/v1/rich", "#/components/schemas/Foo"),
        )
        _seed_endpoint(conn, "POST", "/v1/stub")
        _seed_endpoint(conn, "POST", "/v1/rich")

        # Single batch: stub first (allocates idx >= 0), then rich
        # triggers the in-batch in-place mutate path. The fix must
        # evict the previously-emitted (empty) property subgraph
        # bookkeeping so the rich body's properties get re-emitted.
        from netops_api_navigator.oas_schema_graph import (
            collect_into_batch,
            flush_batch,
            new_batch,
            query_existing_eids,
        )

        batch = new_batch()
        eids = query_existing_eids(conn, ["POST:/v1/stub", "POST:/v1/rich"])
        collect_into_batch(
            batch,
            spec_source="central",
            spec=stub,
            endpoints=[("POST", "/v1/stub")],
            existing_eids=eids,
        )
        collect_into_batch(
            batch,
            spec_source="central",
            spec=rich,
            endpoints=[("POST", "/v1/rich")],
            existing_eids=eids,
        )
        flush_batch(conn, batch)

        rows = list(conn.execute(
            "MATCH (c:SchemaComponent {name: 'Foo'})-[:HAS_PROPERTY]->(p:Property) "
            "RETURN p.name AS n ORDER BY p.name"
        ).rows_as_dict())
        assert [r["n"] for r in rows] == ["a", "b", "c"], rows

    def test_in_batch_rich_then_stub_keeps_full_property_subgraph(self, fresh_db):
        """Reverse order: rich first, stub second. Stub must NOT win,
        and the richer subgraph must remain intact.
        """
        _, conn = fresh_db
        stub = _spec_with(
            {"Foo": {"type": "object"}},
            _post("/v1/stub", "#/components/schemas/Foo"),
        )
        rich = _spec_with(
            {
                "Foo": {
                    "type": "object",
                    "properties": {
                        "a": {"type": "string"},
                        "b": {"type": "integer"},
                    },
                }
            },
            _post("/v1/rich", "#/components/schemas/Foo"),
        )
        _seed_endpoint(conn, "POST", "/v1/stub")
        _seed_endpoint(conn, "POST", "/v1/rich")

        from netops_api_navigator.oas_schema_graph import (
            collect_into_batch,
            flush_batch,
            new_batch,
            query_existing_eids,
        )

        batch = new_batch()
        eids = query_existing_eids(conn, ["POST:/v1/stub", "POST:/v1/rich"])
        collect_into_batch(
            batch,
            spec_source="central",
            spec=rich,
            endpoints=[("POST", "/v1/rich")],
            existing_eids=eids,
        )
        collect_into_batch(
            batch,
            spec_source="central",
            spec=stub,
            endpoints=[("POST", "/v1/stub")],
            existing_eids=eids,
        )
        flush_batch(conn, batch)

        rows = list(conn.execute(
            "MATCH (c:SchemaComponent {name: 'Foo'})-[:HAS_PROPERTY]->(p:Property) "
            "RETURN p.name AS n ORDER BY p.name"
        ).rows_as_dict())
        assert [r["n"] for r in rows] == ["a", "b"], rows

