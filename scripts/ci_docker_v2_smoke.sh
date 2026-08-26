#!/usr/bin/env bash
set -euo pipefail

IMAGE="${1:?image name required}"
REPO="${2:-${GITHUB_REPOSITORY:-tbelz/hpe-networking-central-mcp}}"

mkdir -p tmp

VOL="central-mcp-v2-smoke-${GITHUB_RUN_ID:-local}-${GITHUB_RUN_ATTEMPT:-1}-$$"
OUT="tmp/v2-smoke-stdout-$$.log"
ERR="tmp/v2-smoke-stderr-$$.log"

cleanup() {
  docker volume rm -f "$VOL" >/dev/null || true
  rm -f "$OUT" "$ERR"
}
trap cleanup EXIT

docker volume create "$VOL" >/dev/null

LATEST_KNOWLEDGE_TAG="$(python3 - "$REPO" <<'PY'
import json
import sys
import urllib.request

repo = sys.argv[1]
with urllib.request.urlopen(
    f"https://api.github.com/repos/{repo}/releases/latest",
    timeout=15,
) as response:
    print(json.load(response)["tag_name"])
PY
)"

docker run --rm --entrypoint sh -v "$VOL:/data" "$IMAGE" -c "
  cat > /data/manifest.json <<EOF
{\"release_tag\":\"$LATEST_KNOWLEDGE_TAG\",\"schema_version\":10,\"knowledge_asset\":\"knowledge_db_compiler.tar.gz\",\"knowledge_archive_member\":\"knowledge_db_compiler\",\"knowledge_projection\":\"v2\"}
EOF
  printf 'not a ladybug database' > /data/graph_db
  printf 'stale corrupt wal' > /data/graph_db.wal
"

python3 - "$IMAGE" "$REPO" "$VOL" "$OUT" "$ERR" <<'PY'
from __future__ import annotations

import json
import queue
import subprocess
import sys
import threading
import time
from pathlib import Path

image, repo, volume, out_path, err_path = sys.argv[1:]
out_file = Path(out_path)
err_file = Path(err_path)
out_file.touch()
err_file.touch()

cmd = [
    "docker", "run", "-i", "--rm",
    "-v", f"{volume}:/data",
    "-e", f"KNOWLEDGE_RELEASE_REPO={repo}",
    "-e", "MCP_KNOWLEDGE_PROJECTION=v2",
    "-e", "MCP_COMPILER_TOOLS=true",
    image,
]

proc = subprocess.Popen(
    cmd,
    stdin=subprocess.PIPE,
    stdout=subprocess.PIPE,
    stderr=subprocess.PIPE,
    text=True,
    bufsize=1,
)
assert proc.stdin is not None
assert proc.stdout is not None
assert proc.stderr is not None

stdout_lines: "queue.Queue[str]" = queue.Queue()
stderr_lines: list[str] = []


def _pump_stdout() -> None:
    for line in proc.stdout:
        with out_file.open("a", encoding="utf-8") as fh:
            fh.write(line)
        stdout_lines.put(line)


def _pump_stderr() -> None:
    for line in proc.stderr:
        with err_file.open("a", encoding="utf-8") as fh:
            fh.write(line)
        stderr_lines.append(line)


threading.Thread(target=_pump_stdout, daemon=True).start()
threading.Thread(target=_pump_stderr, daemon=True).start()

next_id = 1


def send(method: str, params: dict | None = None, *, msg_id: int | None = None) -> int | None:
    global next_id
    message: dict = {"jsonrpc": "2.0", "method": method}
    if params is not None:
        message["params"] = params
    if msg_id is not None:
        message["id"] = msg_id
    elif method != "notifications/initialized":
        message["id"] = next_id
        next_id += 1
    proc.stdin.write(json.dumps(message, separators=(",", ":")) + "\n")
    proc.stdin.flush()
    return message.get("id")


def read_response(msg_id: int, timeout: float = 90.0) -> dict:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if proc.poll() is not None and stdout_lines.empty():
            break
        try:
            line = stdout_lines.get(timeout=0.2)
        except queue.Empty:
            continue
        try:
            message = json.loads(line)
        except json.JSONDecodeError:
            continue
        if message.get("id") == msg_id:
            return message
    stderr = "".join(stderr_lines) or err_file.read_text(encoding="utf-8", errors="replace")
    raise SystemExit(
        f"Timed out waiting for JSON-RPC response id={msg_id}\n"
        f"--- stderr ---\n{stderr}\n--- stdout ---\n"
        f"{out_file.read_text(encoding='utf-8', errors='replace')}"
    )


def assert_rpc_ok(message: dict, label: str) -> dict:
    if "error" in message:
        raise SystemExit(f"{label} returned JSON-RPC error: {message['error']}")
    result = message.get("result")
    if not isinstance(result, dict):
        raise SystemExit(f"{label} returned malformed result: {message}")
    return result


def call_tool(name: str, arguments: dict, timeout: float = 90.0) -> str:
    msg_id = send("tools/call", {"name": name, "arguments": arguments})
    assert msg_id is not None
    result = assert_rpc_ok(read_response(msg_id, timeout=timeout), name)
    if result.get("isError"):
        text = "\n".join(
            part.get("text", "") for part in result.get("content", [])
            if isinstance(part, dict)
        )
        raise SystemExit(f"{name} returned MCP tool error:\n{text}")
    text = "\n".join(
        part.get("text", "") for part in result.get("content", [])
        if isinstance(part, dict)
    )
    if not text:
        raise SystemExit(f"{name} returned no text content")
    return text


def parse_json_tool(name: str, text: str):
    try:
        return json.loads(text)
    except json.JSONDecodeError as exc:
        raise SystemExit(f"{name} did not return JSON text: {exc}\n{text[:1000]}") from exc


init_id = send(
    "initialize",
    {
        "protocolVersion": "2024-11-05",
        "capabilities": {},
        "clientInfo": {"name": "v2-smoke", "version": "0.1"},
    },
)
assert init_id is not None
assert_rpc_ok(read_response(init_id, timeout=240.0), "initialize")
send("notifications/initialized")

list_id = send("tools/list", {})
assert list_id is not None
tool_result = assert_rpc_ok(read_response(list_id, timeout=60.0), "tools/list")
tools = {tool["name"] for tool in tool_result.get("tools", [])}

expected_tools = {
    "query_graph",
    "query_api_schema",
    "query_fts",
    "query_topology",
    "query_runtime",
    "query_yang",
    "get_raw_schema",
    "write_graph",
    "list_scripts",
    "get_script_content",
    "save_script",
    "get_openapi_source_detail",
    "get_compiler_graph_health",
}
removed_tools = {
    "find_api_endpoints",
    "get_api_endpoint_context",
    "get_api_schema_context",
}
connected_only_tools = {
    "call_central_api",
    "call_greenlake_api",
    "execute_script",
}

if tools != expected_tools:
    raise SystemExit(
        "Discovery-only v2 smoke tool surface changed without a smoke call update.\n"
        f"Expected: {sorted(expected_tools)}\n"
        f"Actual:   {sorted(tools)}"
    )
unexpected = tools & (removed_tools | connected_only_tools)
if unexpected:
    raise SystemExit(f"Unexpected tools registered in discovery-only v2 smoke: {sorted(unexpected)}")

component_rows = parse_json_tool(
    "query_graph",
    call_tool(
        "query_graph",
        {
            "cypher": (
                "MATCH (c:SchemaComponent) "
                "RETURN c.component_id AS component_id "
                "ORDER BY c.component_id LIMIT 1"
            ),
            "parameters": "{}",
        },
    ),
)
if not component_rows or not component_rows[0].get("component_id"):
    raise SystemExit(f"query_graph did not return a SchemaComponent id: {component_rows}")
component_id = component_rows[0]["component_id"]

parse_json_tool(
    "query_api_schema",
    call_tool(
        "query_api_schema",
        {
            "queries": [
                {
                    "label": "endpoint-sample",
                    "cypher": (
                        "MATCH (e:ApiEndpoint) "
                        "RETURN e.method AS method, e.path AS path, e.summary AS summary "
                        "ORDER BY e.path LIMIT 3"
                    ),
                    "parameters": {},
                },
                {
                    "label": "body-field-sample",
                    "cypher": (
                        "MATCH (e:ApiEndpoint)-[:HAS_REQUEST_BODY]->(:RequestBody)"
                        "-[:BODY_REFERENCES]->(root:SchemaComponent) "
                        "MATCH (root)-[:COMPOSED_OF*0..5]->(c:SchemaComponent)"
                        "-[:HAS_PROPERTY]->(p:Property) "
                        "RETURN e.method AS method, e.path AS path, c.name AS declaredOn, "
                        "p.name AS property, p.type AS type "
                        "LIMIT 10"
                    ),
                    "parameters": {},
                },
            ],
        },
    ),
)

parse_json_tool(
    "query_fts",
    call_tool(
        "query_fts",
        {
            "cypher": (
                "CALL QUERY_FTS_INDEX('ApiEndpoint','api_fts','alerts notifications') "
                "YIELD node, score "
                "RETURN node.method AS method, node.path AS path, "
                "node.summary AS summary, score "
                "ORDER BY score DESC LIMIT 10"
            ),
            "parameters": "{}",
        },
    ),
)

parse_json_tool(
    "query_topology",
    call_tool(
        "query_topology",
        {
            "cypher": "MATCH (d:Device) RETURN COUNT(d) AS devices",
            "parameters": "{}",
        },
    ),
)

parse_json_tool(
    "query_yang",
    call_tool(
        "query_yang",
        {
            "cypher": (
                "MATCH (e:ApiEndpoint)-[:HAS_CLI_COMMAND]->(cli:CliCommand) "
                "WHERE toLower(coalesce(cli.commandName, '')) CONTAINS toLower($keyword) "
                "   OR toLower(coalesce(cli.commandUse, '')) CONTAINS toLower($keyword) "
                "   OR toLower(coalesce(cli.parentCommand, '')) CONTAINS toLower($keyword) "
                "RETURN e.method AS method, e.path AS path, e.summary AS summary, "
                "cli.commandName AS commandName, cli.commandUse AS commandUse, "
                "cli.parentCommand AS parentCommand, cli.pathToPrint AS pathToPrint, "
                "cli.paramKeys AS paramKeys "
                "LIMIT 25"
            ),
            "parameters": "{\"keyword\":\"profile\"}",
        },
    ),
)

parse_json_tool(
    "get_raw_schema",
    call_tool("get_raw_schema", {"component_id": component_id}),
)

parse_json_tool(
    "write_graph",
    call_tool(
        "write_graph",
        {
            "cypher": (
                "MERGE (s:Script {filename: 'ci_smoke_write_graph.py'}) "
                "SET s.description = 'CI smoke write_graph marker', "
                "s.content = '', s.parameters = '[]', s.created_at = 'ci-smoke'"
            ),
            "parameters": "{}",
        },
    ),
)

parse_json_tool("list_scripts", call_tool("list_scripts", {}))

parse_json_tool(
    "save_script",
    call_tool(
        "save_script",
        {
            "filename": "ci_smoke_saved.py",
            "content": "print('hello from v2 smoke')\n",
            "description": "CI v2 smoke saved script",
            "tags": ["ci", "smoke"],
            "parameters": [],
            "execute": False,
        },
    ),
)

parse_json_tool(
    "get_script_content",
    call_tool("get_script_content", {"filename": "ci_smoke_saved.py"}),
)

parse_json_tool(
    "get_openapi_source_detail",
    call_tool(
        "get_openapi_source_detail",
        {"table_name": "SchemaComponent", "row_id": component_id},
        timeout=120.0,
    ),
)

health = parse_json_tool(
    "get_compiler_graph_health",
    call_tool(
        "get_compiler_graph_health",
        {"endpoint_limit": 5, "schema_limit": 5},
        timeout=120.0,
    ),
)
if health.get("failure_count", 0) != 0:
    raise SystemExit(f"get_compiler_graph_health reported failures: {health}")

proc.stdin.close()
try:
    status = proc.wait(timeout=20)
except subprocess.TimeoutExpired:
    proc.terminate()
    status = proc.wait(timeout=10)
if status != 0:
    raise SystemExit(
        f"v2 smoke container exited with status {status}\n"
        f"--- stderr ---\n{err_file.read_text(encoding='utf-8', errors='replace')}\n"
        f"--- stdout ---\n{out_file.read_text(encoding='utf-8', errors='replace')}"
    )

stderr = "".join(stderr_lines) or err_file.read_text(encoding="utf-8", errors="replace")
if "knowledge_db_refresh_forced" not in stderr:
    raise SystemExit(f"Expected forced DB refresh recovery log was not emitted\n{stderr}")
if "Corrupted wal file" in stderr.split("knowledge_db_reinstall_recovered")[-1]:
    raise SystemExit(f"WAL corruption persisted after recovery\n{stderr}")

print(
    "v2 discovery smoke recovered persisted DB and called every registered "
    "discovery-only tool"
)
PY
