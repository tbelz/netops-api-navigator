"""Cross-platform graph IPC tests."""

from __future__ import annotations

import json
import socket

import pytest

from hpe_networking_central_mcp.central_helpers import GraphHelper
from hpe_networking_central_mcp.graph.ipc_server import GraphIPCServer

pytestmark = pytest.mark.unit


class _FakeGraphManager:
    def query(self, cypher, params=None, read_only=True):
        return [{"operation": "query", "cypher": cypher, "params": params or {}}]

    def execute(self, cypher, params=None):
        return [{"operation": "execute", "cypher": cypher, "params": params or {}}]


def test_loopback_ipc_round_trip(monkeypatch):
    server = GraphIPCServer(_FakeGraphManager())
    server.start()
    try:
        for key, value in server.environment.items():
            monkeypatch.setenv(key, value)
        helper = GraphHelper()
        rows = helper.query("MATCH (n) RETURN n", {"limit": 1})
        assert rows == [
            {
                "operation": "query",
                "cypher": "MATCH (n) RETURN n",
                "params": {"limit": 1},
            }
        ]
        assert server.host == "127.0.0.1"
        assert server.port > 0
    finally:
        server.stop()


def test_loopback_ipc_rejects_wrong_token():
    server = GraphIPCServer(_FakeGraphManager())
    server.start()
    try:
        with socket.create_connection((server.host, server.port), timeout=5) as connection:
            request = json.dumps(
                {
                    "id": 1,
                    "token": "wrong-token",
                    "method": "query",
                    "cypher": "RETURN 1",
                }
            )
            connection.sendall((request + "\n").encode())
            response = b""
            while not response.endswith(b"\n"):
                response += connection.recv(4096)
        payload = json.loads(response)
        assert payload["id"] == 1
        assert "Unauthorized" in payload["error"]
    finally:
        server.stop()


def test_graph_helper_refuses_non_loopback_host(monkeypatch):
    monkeypatch.setenv("GRAPH_IPC_HOST", "192.0.2.1")
    monkeypatch.setenv("GRAPH_IPC_PORT", "1234")
    monkeypatch.setenv("GRAPH_IPC_TOKEN", "token")

    with pytest.raises(RuntimeError, match="loopback"):
        GraphHelper().query("RETURN 1")
