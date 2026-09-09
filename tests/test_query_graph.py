"""Tests for the ``query_graph`` MCP tool ergonomics (Phase 2B of ADR 009).

Cover:
- pass-through of parameterised Cypher via the new ``parameters`` JSON arg,
- soft row cap (default 200) — truncation envelope,
- hard row cap (default 2000) — raised as ToolError,
- empty / invalid input rejection,
- error hint enrichment via the schema.
"""

from __future__ import annotations

import json

import pytest
from mcp.server.fastmcp import FastMCP
from mcp.server.fastmcp.exceptions import ToolError

from netops_api_navigator.config import Settings
from netops_api_navigator.graph.manager import GraphManager
from netops_api_navigator.tools.graph import register_graph_tools


@pytest.fixture
def gm(tmp_path_factory):
    db_path = tmp_path_factory.mktemp("qg") / "test.db"
    gm = GraphManager(db_path)
    gm.initialize()
    return gm


def _make_query_tool(gm):
    settings = Settings(
        central_base_url="https://x",
        central_client_id="cid",
        central_client_secret="csec",
        read_only=True,
    )
    mcp = FastMCP("test-qg")
    register_graph_tools(mcp, settings, gm)
    tools = {t.name: t.fn for t in mcp._tool_manager._tools.values()}
    return tools["query_graph"]


def _make_n_endpoints(gm: GraphManager, n: int) -> None:
    for i in range(n):
        gm.execute(
            "CREATE (e:ApiEndpoint {"
            "  endpoint_id: $eid,"
            "  method: 'GET', path: $path,"
            "  summary: '', description: '',"
            "  operationId: '', category: 'cat',"
            "  deprecated: false,"
            "  parameters: '[]', requestBody: '', responses: ''"
            "})",
            {"eid": f"GET:/p/{i}", "path": f"/p/{i}"},
        )


class TestQueryGraphParameters:
    def test_accepts_json_parameters(self, gm):
        _make_n_endpoints(gm, 3)
        qg = _make_query_tool(gm)
        out = qg(
            cypher="MATCH (e:ApiEndpoint) WHERE e.path = $p RETURN e.path AS path",
            parameters=json.dumps({"p": "/p/1"}),
        )
        rows = json.loads(out)
        # Either bare list or envelope-with-rows; bare list expected for small results.
        assert isinstance(rows, list), f"expected bare list for small result, got {type(rows)}"
        assert rows == [{"path": "/p/1"}]

    def test_default_parameters_is_empty(self, gm):
        _make_n_endpoints(gm, 1)
        qg = _make_query_tool(gm)
        out = qg(cypher="MATCH (e:ApiEndpoint) RETURN count(e) AS n")
        rows = json.loads(out)
        assert rows == [{"n": 1}]

    def test_invalid_parameters_json_raises(self, gm):
        qg = _make_query_tool(gm)
        with pytest.raises(ToolError, match="Invalid JSON"):
            qg(cypher="MATCH (e) RETURN e", parameters="{not-json")


class TestQueryGraphCaps:
    def test_under_soft_cap_returns_bare_list(self, gm):
        qg = _make_query_tool(gm)
        out = qg(cypher="UNWIND range(1, 10) AS i RETURN i")
        rows = json.loads(out)
        assert isinstance(rows, list)
        assert len(rows) == 10

    def test_over_soft_cap_returns_truncation_envelope(self, gm):
        qg = _make_query_tool(gm)
        out = qg(cypher="UNWIND range(1, 250) AS i RETURN i")
        result = json.loads(out)
        assert isinstance(result, dict)
        assert result.get("truncated") is True
        assert result.get("cap") == 200
        assert "rows" in result
        assert len(result["rows"]) == 200
        assert "warning" in result

    def test_over_hard_cap_raises(self, gm):
        qg = _make_query_tool(gm)
        with pytest.raises(ToolError, match="hard cap|exceeds"):
            qg(cypher="UNWIND range(1, 2100) AS i RETURN i")


class TestQueryGraphRejection:
    def test_empty_cypher_raises(self, gm):
        qg = _make_query_tool(gm)
        with pytest.raises(ToolError, match="empty"):
            qg(cypher="   ")


# ── Freshness signaling (volatile-field detection) ──────────────────


def _insert_device_fresh(gm, serial: str) -> None:
    gm.execute(
        "CREATE (d:Device {serial: $s, name: $s, status: 'Up', "
        "lastSyncedAt: current_timestamp()})",
        {"s": serial},
    )


def _insert_device_stale(gm, serial: str) -> None:
    gm.execute(
        "CREATE (d:Device {serial: $s, name: $s, status: 'Up', "
        "lastSyncedAt: timestamp('2020-01-01 00:00:00')})",
        {"s": serial},
    )


def _insert_device_unstamped(gm, serial: str) -> None:
    gm.execute(
        "CREATE (d:Device {serial: $s, name: $s, status: 'Up'})",
        {"s": serial},
    )


class TestQueryGraphFreshness:
    def test_fresh_volatile_projection_returns_bare_list(self, gm):
        _insert_device_fresh(gm, "FRESH1")
        qg = _make_query_tool(gm)
        out = qg(
            cypher="MATCH (d:Device) RETURN d.status AS `d.status`, "
                   "d.lastSyncedAt AS `d.lastSyncedAt`",
        )
        parsed = json.loads(out)
        assert isinstance(parsed, list), f"expected bare list, got {parsed!r}"
        assert len(parsed) == 1

    def test_stale_volatile_projection_wraps_with_warnings(self, gm):
        _insert_device_stale(gm, "STALE1")
        qg = _make_query_tool(gm)
        out = qg(
            cypher="MATCH (d:Device) RETURN d.status AS `d.status`, "
                   "d.lastSyncedAt AS `d.lastSyncedAt`",
        )
        parsed = json.loads(out)
        assert isinstance(parsed, dict)
        assert "rows" in parsed
        assert "freshness_warnings" in parsed
        warns = parsed["freshness_warnings"]
        assert len(warns) == 1
        w = warns[0]
        assert w["node_label"] == "Device"
        assert "status" in w["volatile_fields_in_result"]
        assert w["lastSyncedAt_present"] is True
        assert w["max_age_seconds"] is not None and w["max_age_seconds"] > 0
        assert w["rows_affected"] >= 1

    def test_no_volatile_projection_returns_bare_list(self, gm):
        _insert_device_stale(gm, "STALE2")
        qg = _make_query_tool(gm)
        # Project only stable fields — name + serial. No warning expected.
        out = qg(cypher="MATCH (d:Device) RETURN d.name AS `d.name`")
        parsed = json.loads(out)
        assert isinstance(parsed, list)

    def test_unstamped_node_with_volatile_field_warns(self, gm):
        _insert_device_unstamped(gm, "NOSTAMP")
        qg = _make_query_tool(gm)
        out = qg(
            cypher="MATCH (d:Device) RETURN d.status AS `d.status`",
        )
        parsed = json.loads(out)
        assert isinstance(parsed, dict)
        warns = parsed["freshness_warnings"]
        assert warns and warns[0]["lastSyncedAt_present"] is False

    def test_threshold_zero_disables_warnings(self, gm, monkeypatch):
        _insert_device_stale(gm, "STALE3")
        monkeypatch.setenv("MCP_GRAPH_STALE_THRESHOLD_SECONDS", "0")
        qg = _make_query_tool(gm)
        out = qg(
            cypher="MATCH (d:Device) RETURN d.status AS `d.status`, "
                   "d.lastSyncedAt AS `d.lastSyncedAt`",
        )
        parsed = json.loads(out)
        assert isinstance(parsed, list), (
            f"threshold=0 should disable freshness wrapping, got {type(parsed)}"
        )


# ── Byte caps (per-cell, per-response) ──────────────────────────────


class TestQueryGraphByteCaps:
    def test_per_cell_cap_truncates_large_string_value(self, gm, monkeypatch):
        monkeypatch.setenv("MCP_GRAPH_PER_CELL_BYTES", "200")
        qg = _make_query_tool(gm)
        big = "x" * 1000
        out = qg(
            cypher="UNWIND [$s] AS v RETURN v",
            parameters=json.dumps({"s": big}),
        )
        parsed = json.loads(out)
        assert isinstance(parsed, list)
        cell = parsed[0]["v"]
        assert isinstance(cell, dict)
        assert cell.get("_truncated") is True
        assert cell.get("size_bytes") == 1000
        assert len(cell.get("preview", "")) <= 200
        assert "hint" in cell

    def test_per_cell_cap_leaves_small_values_alone(self, gm, monkeypatch):
        monkeypatch.setenv("MCP_GRAPH_PER_CELL_BYTES", "200")
        qg = _make_query_tool(gm)
        out = qg(
            cypher="UNWIND [$s] AS v RETURN v",
            parameters=json.dumps({"s": "small"}),
        )
        parsed = json.loads(out)
        assert parsed == [{"v": "small"}]

    def test_per_response_byte_cap_returns_envelope(self, gm, monkeypatch):
        # Force a very small response cap so a small bare list trips it.
        monkeypatch.setenv("MCP_GRAPH_PER_RESPONSE_BYTES", "100")
        qg = _make_query_tool(gm)
        out = qg(cypher="UNWIND range(1, 50) AS i RETURN i")
        parsed = json.loads(out)
        assert isinstance(parsed, dict)
        assert parsed.get("truncated") is True
        assert parsed.get("reason") == "response_byte_cap"
        assert parsed.get("cap_bytes") == 100
        assert "rows" in parsed
        assert "warning" in parsed
        # We dropped at least one row.
        assert parsed.get("rows_dropped", 0) >= 1

    def test_response_byte_cap_respects_env_default(self, gm):
        # No env override → default 50_000; a small result must pass through.
        qg = _make_query_tool(gm)
        out = qg(cypher="UNWIND range(1, 5) AS i RETURN i")
        parsed = json.loads(out)
        assert isinstance(parsed, list)
        assert len(parsed) == 5

    def test_invalid_env_falls_back_to_default(self, gm, monkeypatch):
        monkeypatch.setenv("MCP_GRAPH_PER_CELL_BYTES", "not-a-number")
        monkeypatch.setenv("MCP_GRAPH_PER_RESPONSE_BYTES", "0")
        qg = _make_query_tool(gm)
        out = qg(cypher="UNWIND range(1, 3) AS i RETURN i")
        parsed = json.loads(out)
        # Defaults still apply: small result is a bare list.
        assert isinstance(parsed, list)
        assert len(parsed) == 3


# ── get_raw_schema tool ─────────────────────────────────────────────


def _make_tools(gm):
    settings = Settings(
        central_base_url="https://x",
        central_client_id="cid",
        central_client_secret="csec",
        read_only=True,
    )
    mcp = FastMCP("test-grs")
    register_graph_tools(mcp, settings, gm)
    return {t.name: t.fn for t in mcp._tool_manager._tools.values()}


def _insert_schema_component(gm, cid: str, body_json: str, name: str = "TestComp") -> None:
    gm.execute(
        "CREATE (c:SchemaComponent {"
        "  component_id: $cid, spec_source: 'test', section: 'schemas',"
        "  name: $name, type: 'object', kind: 'object',"
        "  bodyShape: 'object', required: [], enumValues: [],"
        "  supportedDeviceTypes: [], bodyJson: $body"
        "})",
        {"cid": cid, "name": name, "body": body_json},
    )


class TestGetRawSchema:
    def test_returns_bodyjson_blob(self, gm):
        _insert_schema_component(gm, "test:schemas:Foo", "RAW_BODY_PAYLOAD", "Foo")
        tools = _make_tools(gm)
        out = tools["get_raw_schema"](component_id="test:schemas:Foo")
        parsed = json.loads(out)
        assert parsed["component_id"] == "test:schemas:Foo"
        assert parsed["name"] == "Foo"
        assert parsed["section"] == "schemas"
        assert parsed["bodyShape"] == "object"
        assert parsed["bodyJson"] == "RAW_BODY_PAYLOAD"

    def test_unknown_component_raises(self, gm):
        tools = _make_tools(gm)
        with pytest.raises(ToolError, match="No SchemaComponent"):
            tools["get_raw_schema"](component_id="does:not:exist")

    def test_empty_component_id_raises(self, gm):
        tools = _make_tools(gm)
        with pytest.raises(ToolError, match="empty"):
            tools["get_raw_schema"](component_id="  ")

    def test_oversize_blob_returns_hint_envelope(self, gm, monkeypatch):
        big = "a" * 5000
        _insert_schema_component(gm, "test:schemas:Big", big, "Big")
        monkeypatch.setenv("MCP_GRAPH_RAW_SCHEMA_MAX_BYTES", "1000")
        tools = _make_tools(gm)
        out = tools["get_raw_schema"](component_id="test:schemas:Big")
        parsed = json.loads(out)
        assert "error" in parsed
        assert parsed["component_id"] == "test:schemas:Big"
        assert parsed["size_bytes"] > 1000
        assert parsed["cap_bytes"] == 1000
        assert "hint" in parsed
        assert "bodyJson" not in parsed


# ── Tool aliasing (query_api_schema / query_fts / query_topology / query_runtime / query_yang) ──


class TestQueryAliases:
    ALIASES = (
        "query_graph",
        "query_api_schema",
        "query_fts",
        "query_topology",
        "query_runtime",
        "query_yang",
    )

    def test_all_aliases_registered(self, gm):
        tools = _make_tools(gm)
        for name in self.ALIASES:
            assert name in tools, f"missing tool alias {name}"

    def test_each_alias_executes_simple_query(self, gm):
        tools = _make_tools(gm)
        for alias in self.ALIASES:
            out = tools[alias](cypher="RETURN 1 AS n")
            assert json.loads(out) == [{"n": 1}], f"{alias} failed RETURN 1"

    def test_each_alias_respects_byte_caps(self, gm, monkeypatch):
        monkeypatch.setenv("MCP_GRAPH_PER_CELL_BYTES", "100")
        tools = _make_tools(gm)
        big = "x" * 500
        for alias in self.ALIASES:
            out = tools[alias](
                cypher="UNWIND [$s] AS v RETURN v",
                parameters=json.dumps({"s": big}),
            )
            cell = json.loads(out)[0]["v"]
            assert isinstance(cell, dict) and cell.get("_truncated") is True, (
                f"{alias} did not apply per-cell cap"
            )

    def test_each_alias_empty_cypher_raises(self, gm):
        tools = _make_tools(gm)
        for alias in self.ALIASES:
            with pytest.raises(ToolError, match="empty"):
                tools[alias](cypher="  ")

    def test_each_alias_docstring_within_budget(self, gm):
        # Each focused docstring should stay under ~4000 chars so clients
        # that cap tool descriptions around 4 KB don't truncate guidance.
        tools = _make_tools(gm)
        for alias in self.ALIASES:
            doc = tools[alias].__doc__ or ""
            assert len(doc) < 4000, (
                f"{alias} docstring is {len(doc)} chars — keep it under 4000 "
                "so tool-description-capping clients don't truncate guidance."
            )

    def test_query_graph_points_runtime_reads_to_runtime_alias(self, gm):
        tools = _make_tools(gm)
        doc = tools["query_graph"].__doc__ or ""
        assert "query_runtime" in doc
        assert "hydrated runtime state" in (tools["query_runtime"].__doc__ or "")


class TestQueryBatchMode:
    """``queries=[...]`` batch dispatch shared by all read aliases."""

    ALIASES = (
        "query_graph",
        "query_api_schema",
        "query_fts",
        "query_topology",
        "query_runtime",
        "query_yang",
    )

    def test_batch_executes_in_order(self, gm):
        qg = _make_query_tool(gm)
        out = qg(queries=[
            {"cypher": "RETURN 1 AS n", "label": "one"},
            {"cypher": "RETURN 2 AS n", "label": "two"},
            {"cypher": "RETURN 3 AS n"},
        ])
        env = json.loads(out)
        assert env["batch"] is True
        assert env["total"] == 3
        assert env["ok"] == 3
        assert env["failed"] == 0
        labels = [r["label"] for r in env["results"]]
        assert labels == ["one", "two", None]
        values = [r["result"][0]["n"] for r in env["results"]]
        assert values == [1, 2, 3]

    def test_batch_continue_on_error(self, gm):
        qg = _make_query_tool(gm)
        out = qg(queries=[
            {"cypher": "RETURN 1 AS n"},
            {"cypher": "   ", "label": "blank"},          # empty cypher
            {"cypher": "RETURN 2 AS n", "label": "after"},
        ])
        env = json.loads(out)
        assert env["total"] == 3
        assert env["ok"] == 2
        assert env["failed"] == 1
        assert env["results"][0]["ok"] is True
        assert env["results"][1]["ok"] is False
        assert "empty" in env["results"][1]["error"].lower()
        assert env["results"][2]["ok"] is True
        assert env["results"][2]["label"] == "after"

    def test_batch_size_cap_enforced(self, gm, monkeypatch):
        monkeypatch.setenv("MCP_GRAPH_BATCH_MAX_ITEMS", "3")
        qg = _make_query_tool(gm)
        with pytest.raises(ToolError, match="per-batch cap"):
            qg(queries=[{"cypher": "RETURN 1 AS n"}] * 4)

    def test_batch_accepts_dict_parameters(self, gm):
        qg = _make_query_tool(gm)
        out = qg(queries=[
            {"cypher": "RETURN $x AS x", "parameters": {"x": 7}},
            {"cypher": "RETURN $x AS x", "parameters": json.dumps({"x": 8})},
        ])
        env = json.loads(out)
        assert env["ok"] == 2
        assert env["results"][0]["result"] == [{"x": 7}]
        assert env["results"][1]["result"] == [{"x": 8}]

    def test_batch_empty_list_raises(self, gm):
        qg = _make_query_tool(gm)
        with pytest.raises(ToolError, match="empty"):
            qg(queries=[])

    def test_batch_total_response_cap_drops_trailing_items(self, gm, monkeypatch):
        # Tiny aggregate cap forces tail-trimming. Each item still respects
        # its own per-response cap.
        monkeypatch.setenv("MCP_GRAPH_BATCH_RESPONSE_BYTES", "500")
        qg = _make_query_tool(gm)
        out = qg(queries=[
            {"cypher": "UNWIND range(1, 10) AS i RETURN i", "label": f"q{n}"}
            for n in range(5)
        ])
        env = json.loads(out)
        assert env.get("truncated") is True
        assert env["kept_items"] < 5
        assert len(env["results"]) == env["kept_items"]
        # Order preserved: kept items are the leading prefix.
        labels = [r["label"] for r in env["results"]]
        assert labels == [f"q{n}" for n in range(len(labels))]

    @pytest.mark.parametrize("alias", ALIASES)
    def test_each_alias_supports_batch(self, gm, alias):
        tools = _make_tools(gm)
        out = tools[alias](queries=[
            {"cypher": "RETURN 1 AS n", "label": "a"},
            {"cypher": "RETURN 2 AS n", "label": "b"},
        ])
        env = json.loads(out)
        assert env["batch"] is True
        assert env["ok"] == 2
        assert [r["label"] for r in env["results"]] == ["a", "b"]
