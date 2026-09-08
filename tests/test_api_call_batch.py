"""Tests for batch mode + 404 suggester on call_central_api / call_greenlake_api."""

from __future__ import annotations

import json
import sys
from pathlib import Path
from unittest.mock import patch

import pytest
from mcp.server.fastmcp import FastMCP
from mcp.server.fastmcp.exceptions import ToolError

sys.path.insert(0, str(Path(__file__).parent.parent / "src"))

from netops_api_navigator.central_client import CentralAPIError, CentralClient
from netops_api_navigator.config import Settings
from netops_api_navigator.graph.manager import GraphManager
from netops_api_navigator.tools.api_call import register_api_call_tools


# ── fixtures ────────────────────────────────────────────────────────


@pytest.fixture
def gm_with_endpoints(tmp_path_factory):
    """Real GraphManager seeded with a handful of ApiEndpoint nodes."""
    db_path = tmp_path_factory.mktemp("apicall") / "test.db"
    gm = GraphManager(db_path)
    gm.initialize()
    seeded = [
        ("GET", "/network-monitoring/v1/devices"),
        ("GET", "/network-monitoring/v1/aps"),
        ("GET", "/network-monitoring/v1/gateways"),
        ("GET", "/network-monitoring/v1/clients"),
        ("GET", "/network-monitoring/v1/switches"),
    ]
    for method, path in seeded:
        gm.execute(
            "CREATE (e:ApiEndpoint {"
            "  endpoint_id: $eid,"
            "  method: $m, path: $p,"
            "  summary: '', description: '',"
            "  operationId: '', category: 'Monitoring',"
            "  deprecated: false,"
            "  parameters: '[]', requestBody: '', responses: ''"
            "})",
            {"eid": f"{method}:{path}", "m": method, "p": path},
        )
    return gm


def _make_tool(settings: Settings, gm: GraphManager | None = None):
    client = CentralClient(
        settings.central_base_url,
        settings.central_client_id,
        settings.central_client_secret,
    )
    mcp = FastMCP("test-batch")
    register_api_call_tools(mcp, settings, client, gm)
    tools = {t.name: t.fn for t in mcp._tool_manager._tools.values()}
    return tools["call_central_api"], client


def _strip_warning_header(out: str) -> str:
    """Single-call envelope may be prefixed with a warning block before the JSON."""
    idx = out.find("{")
    return out[idx:] if idx > 0 else out


def _settings(read_only: bool = False) -> Settings:
    return Settings(
        central_base_url="https://x",
        central_client_id="cid",
        central_client_secret="csec",
        read_only=read_only,
    )


# ── single-call envelope ────────────────────────────────────────────


class TestSingleCallEnvelope:
    def test_single_call_envelope_shape(self):
        tool, client = _make_tool(_settings())
        with patch.object(client, "_request", return_value={"items": [1, 2]}):
            out = tool(path="some/path")
        env = json.loads(_strip_warning_header(out))
        assert env["status"] == 200
        assert env["request"] == {"method": "GET", "path": "some/path"}
        assert env["response"] == {"items": [1, 2]}


# ── batch happy path & shape ────────────────────────────────────────


class TestBatchAggregation:
    def test_batch_runs_all_calls_and_aggregates(self):
        tool, client = _make_tool(_settings())
        with patch.object(client, "_request", side_effect=[{"a": 1}, {"b": 2}, {"c": 3}]):
            out = tool(calls=[
                {"path": "p/1"},
                {"path": "p/2"},
                {"path": "p/3"},
            ])
        env = json.loads(_strip_warning_header(out))
        assert env["batch"] is True
        assert env["total"] == 3
        assert env["ok"] == 3
        assert env["failed"] == 0
        assert [r["response"] for r in env["results"]] == [{"a": 1}, {"b": 2}, {"c": 3}]
        for r in env["results"]:
            assert r["request"]["batch_item"] is True

    def test_batch_continues_on_individual_failure(self):
        tool, client = _make_tool(_settings())
        with patch.object(
            client,
            "_request",
            side_effect=[
                {"ok": True},
                CentralAPIError(500, message="nope"),
                {"ok": True},
            ],
        ):
            out = tool(calls=[
                {"path": "p/1"},
                {"path": "p/2"},
                {"path": "p/3"},
            ])
        env = json.loads(_strip_warning_header(out))
        assert env["total"] == 3
        assert env["ok"] == 2
        assert env["failed"] == 1
        assert env["results"][1]["ok"] is False
        assert env["results"][1]["status"] == 500


# ── batch validation / caps ─────────────────────────────────────────


class TestBatchValidation:
    def test_over_cap_rejected(self):
        tool, _ = _make_tool(_settings())
        with pytest.raises(ToolError, match="batch cap is 25"):
            tool(calls=[{"path": f"p/{i}"} for i in range(26)])

    def test_empty_calls_rejected(self):
        tool, _ = _make_tool(_settings())
        with pytest.raises(ToolError, match="at least one"):
            tool(calls=[])

    def test_malformed_item_rejected(self):
        tool, _ = _make_tool(_settings())
        with pytest.raises(ToolError, match="missing a non-empty `path`"):
            tool(calls=[{"path": ""}])

    def test_unknown_method_rejected(self):
        tool, _ = _make_tool(_settings())
        with pytest.raises(ToolError, match="GET/POST"):
            tool(calls=[{"path": "p/1", "method": "OPTIONS"}])


# ── READ_ONLY enforcement per item ──────────────────────────────────


class TestBatchReadOnly:
    def test_read_only_blocks_writes_per_item(self):
        tool, client = _make_tool(_settings(read_only=True))
        with patch.object(client, "_request", return_value={"ok": True}) as mock_req:
            out = tool(calls=[
                {"path": "p/1"},
                {"path": "p/2", "method": "POST", "body": {"x": 1}},
            ])
        env = json.loads(_strip_warning_header(out))
        assert env["ok"] == 1
        assert env["failed"] == 1
        assert env["results"][1]["ok"] is False
        assert "READ_ONLY" in env["results"][1]["errors"][0].upper()
        # First call dispatched, second blocked before reaching client.
        assert mock_req.call_count == 1


# ── 404 path suggester ──────────────────────────────────────────────


class TestNotFoundSuggester:
    def test_single_call_404_includes_suggestions(self, gm_with_endpoints):
        tool, client = _make_tool(_settings(), gm=gm_with_endpoints)
        with patch.object(
            client,
            "_request",
            side_effect=CentralAPIError(404, message="not found"),
        ):
            with pytest.raises(ToolError) as exc:
                tool(path="network-monitoring/v1/device-inventory-xyz")
        msg = str(exc.value)
        assert "Did you mean" in msg
        # Should mention at least one of the seeded monitoring endpoints.
        assert "/network-monitoring/v1/" in msg

    def test_batch_404_includes_suggestions(self, gm_with_endpoints):
        tool, client = _make_tool(_settings(), gm=gm_with_endpoints)
        with patch.object(
            client,
            "_request",
            side_effect=CentralAPIError(404, message="not found"),
        ):
            out = tool(calls=[{"path": "network-monitoring/v1/devices-typo"}])
        env = json.loads(_strip_warning_header(out))
        assert env["failed"] == 1
        err = env["results"][0]["errors"][0]
        assert "Did you mean" in err
