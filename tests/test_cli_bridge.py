"""Phase C: OAS-native CLI bridge.

Vendor extensions ``x-cliParam`` and ``x-path`` in the Aruba Central
OpenAPI specs already encode the CLI ↔ API mapping. This test seeds a
small synthetic spec carrying both extensions and verifies that:

  1. The ``CliCommand`` node is populated from ``x-cliParam`` and wired
     to the originating ``ApiEndpoint`` via ``HAS_CLI_COMMAND``.
  2. The ``YangModule`` node is derived from the ``x-path`` prefix and
     wired to the ``YangPath`` via ``IN_MODULE``.
  3. The canned CLI-bridge recipe from the ``query_yang`` docstring
     returns at least one row joining ``Property → YangPath → YangModule
     → ApiEndpoint → CliCommand``.
  4. The new invariants INV-13 and INV-14 pass on the populated DB.
"""

from __future__ import annotations

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


def _make_cli_yang_spec() -> dict:
    return {
        "openapi": "3.0.0",
        "info": {"title": "cli-bridge fixture"},
        "components": {
            "schemas": {
                "NtpServerConfig": {
                    "type": "object",
                    "properties": {
                        "server_ip": {
                            "type": "string",
                            "x-path": "/ac-ntp:ntp/servers/server/server-ip",
                            "description": "NTP server IP address",
                        },
                    },
                }
            }
        },
        "paths": {
            "/v1/ntp/server": {
                "post": {
                    "operationId": "createNtpServer",
                    "summary": "Create NTP server",
                    "x-cliParam": {
                        "commandName": "createNtpServerCmd",
                        "commandUse": "ntp-server",
                        "parentCommand": "createNtpCmd",
                        "pathToPrint": "/ntp/server/%s/",
                        "paramKeys": [{"key": "server_ip"}],
                    },
                    "requestBody": {
                        "required": True,
                        "content": {
                            "application/json": {
                                "schema": {"$ref": "#/components/schemas/NtpServerConfig"}
                            }
                        },
                    },
                    "responses": {"201": {"description": "created"}},
                }
            }
        },
    }


@pytest.fixture
def fresh_db():
    with TemporaryDirectory(prefix="cli_bridge_") as tmp:
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


def _seed(conn) -> None:
    from netops_api_navigator.oas_schema_graph import populate_schema_graph

    _seed_endpoint(conn, "POST", "/v1/ntp/server")
    populate_schema_graph(
        conn,
        spec_source="central",
        spec=_make_cli_yang_spec(),
        endpoints=[("POST", "/v1/ntp/server")],
    )


def _rows(conn, cypher: str) -> list[dict]:
    return list(conn.execute(cypher).rows_as_dict())


class TestCliBridgePopulator:
    def test_cli_command_node_is_emitted(self, fresh_db):
        _, conn = fresh_db
        _seed(conn)
        rows = _rows(
            conn,
            "MATCH (c:CliCommand) RETURN c.command_id AS cid, "
            "c.commandName AS name, c.commandUse AS use, "
            "c.parentCommand AS parent, c.pathToPrint AS p2p, "
            "c.paramKeys AS keys",
        )
        assert len(rows) == 1, rows
        r = rows[0]
        assert r["cid"] == "POST:/v1/ntp/server::createNtpServerCmd"
        assert r["name"] == "createNtpServerCmd"
        assert r["use"] == "ntp-server"
        assert r["parent"] == "createNtpCmd"
        assert r["p2p"] == "/ntp/server/%s/"
        assert r["keys"] == ["server_ip"]

    def test_has_cli_command_edge_wired_to_endpoint(self, fresh_db):
        _, conn = fresh_db
        _seed(conn)
        rows = _rows(
            conn,
            "MATCH (e:ApiEndpoint)-[:HAS_CLI_COMMAND]->(c:CliCommand) "
            "RETURN e.endpoint_id AS eid, c.commandName AS name",
        )
        assert rows == [
            {"eid": "POST:/v1/ntp/server", "name": "createNtpServerCmd"}
        ]

    def test_yang_module_node_and_in_module_edge(self, fresh_db):
        _, conn = fresh_db
        _seed(conn)
        modules = _rows(conn, "MATCH (m:YangModule) RETURN m.module AS m")
        assert {r["m"] for r in modules} == {"ac-ntp"}
        in_mod = _rows(
            conn,
            "MATCH (y:YangPath)-[:IN_MODULE]->(m:YangModule) "
            "RETURN y.yangPath AS yp, m.module AS m",
        )
        assert in_mod == [
            {"yp": "/ac-ntp:ntp/servers/server/server-ip", "m": "ac-ntp"}
        ]


class TestCliBridgeRecipe:
    def test_canonical_cli_bridge_join_returns_row(self, fresh_db):
        _, conn = fresh_db
        _seed(conn)
        # Mirrors the canned recipe from the query_yang docstring, but
        # without FTS (the test DB is not indexed) — drives the same
        # join chain Property → YangPath → YangModule → ApiEndpoint →
        # CliCommand.
        rows = _rows(
            conn,
            """
            MATCH (p:Property {name: 'server_ip'})
                  -[:PROPERTY_AT_YANG]->(yp:YangPath)
                  -[:IN_MODULE]->(m:YangModule)
            MATCH (rb:RequestBody)-[:BODY_REFERENCES]->(:SchemaComponent)
                  -[:HAS_PROPERTY]->(p)
            MATCH (e:ApiEndpoint)-[:HAS_REQUEST_BODY]->(rb)
            MATCH (e)-[:HAS_CLI_COMMAND]->(cli:CliCommand)
            RETURN p.name AS prop, m.module AS module,
                   e.method AS method, e.path AS path,
                   cli.commandName AS cli_name, cli.pathToPrint AS p2p
            """,
        )
        assert rows == [
            {
                "prop": "server_ip",
                "module": "ac-ntp",
                "method": "POST",
                "path": "/v1/ntp/server",
                "cli_name": "createNtpServerCmd",
                "p2p": "/ntp/server/%s/",
            }
        ]


class TestCliBridgeInvariants:
    def test_inv13_and_inv14_pass(self, fresh_db):
        from netops_api_navigator.graph.invariants import (
            check_cli_command_has_single_endpoint,
            check_yang_path_has_module_edge,
        )

        _, conn = fresh_db
        _seed(conn)
        assert check_cli_command_has_single_endpoint(conn) is None
        assert check_yang_path_has_module_edge(conn) is None
