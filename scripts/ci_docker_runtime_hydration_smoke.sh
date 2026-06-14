#!/usr/bin/env bash
set -euo pipefail

IMAGE="${1:?image name required}"
REPO="${2:-${GITHUB_REPOSITORY:-tbelz/hpe-networking-central-mcp}}"

mkdir -p tmp

VOL="central-mcp-runtime-hydration-smoke-${GITHUB_RUN_ID:-local}-${GITHUB_RUN_ATTEMPT:-1}-$$"
OUT="tmp/runtime-hydration-smoke-stdout-$$.log"
ERR="tmp/runtime-hydration-smoke-stderr-$$.log"

cleanup() {
  docker volume rm -f "$VOL" >/dev/null || true
  rm -f "$OUT" "$ERR"
}
trap cleanup EXIT

docker volume create "$VOL" >/dev/null

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

cmd = [
    "docker", "run", "-i", "--rm",
    "-v", f"{volume}:/data",
    "-e", f"KNOWLEDGE_RELEASE_REPO={repo}",
    "-e", "MCP_KNOWLEDGE_PROJECTION=v2",
    "-e", "MCP_COMPILER_TOOLS=true",
    "-e", "MCP_RUNTIME_HYDRATION=true",
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


def send(method: str, params: dict | None = None) -> int | None:
    global next_id
    message: dict = {"jsonrpc": "2.0", "method": method}
    if params is not None:
        message["params"] = params
    if method != "notifications/initialized":
        message["id"] = next_id
        next_id += 1
    proc.stdin.write(json.dumps(message, separators=(",", ":")) + "\n")
    proc.stdin.flush()
    return message.get("id")


def read_response(msg_id: int, timeout: float = 180.0) -> dict:
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


def call_tool(name: str, arguments: dict, timeout: float = 120.0) -> str:
    msg_id = send("tools/call", {"name": name, "arguments": arguments})
    assert msg_id is not None
    result = assert_rpc_ok(read_response(msg_id, timeout=timeout), name)
    text = "\n".join(
        part.get("text", "") for part in result.get("content", [])
        if isinstance(part, dict)
    )
    if result.get("isError"):
        raise SystemExit(f"{name} returned MCP tool error:\n{text}")
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
        "clientInfo": {"name": "runtime-hydration-smoke", "version": "0.1"},
    },
)
assert init_id is not None
assert_rpc_ok(read_response(init_id, timeout=240.0), "initialize")
send("notifications/initialized")

list_id = send("tools/list", {})
assert list_id is not None
tool_result = assert_rpc_ok(read_response(list_id, timeout=60.0), "tools/list")
tools = {tool["name"] for tool in tool_result.get("tools", [])}
for required in {
    "get_runtime_hydration_status",
    "list_runtime_hydration_candidates",
    "plan_runtime_hydration",
    "hydrate_runtime_endpoint",
    "list_runtime_observations",
    "materialize_runtime_facts",
    "promote_runtime_entities",
}:
    if required not in tools:
        raise SystemExit(f"Runtime hydration tool missing from MCP surface: {required}")

status = parse_json_tool(
    "get_runtime_hydration_status",
    call_tool("get_runtime_hydration_status", {}),
)
if status.get("stage") != "entity_highways" or not status.get("graph_available"):
    raise SystemExit(f"Unexpected runtime hydration status: {status}")

candidates = parse_json_tool(
    "list_runtime_hydration_candidates",
    call_tool("list_runtime_hydration_candidates", {"search": "devices", "limit": 20}),
)
endpoint_ids = {row.get("endpoint_id") for row in candidates.get("candidates", [])}
target = "GET:/network-monitoring/v1/devices"
if target not in endpoint_ids:
    raise SystemExit(f"Expected {target} in device hydration candidates: {endpoint_ids}")

plan = parse_json_tool(
    "plan_runtime_hydration",
    call_tool(
        "plan_runtime_hydration",
        {"endpoint_id": target, "provider": "central"},
    ),
)
if plan.get("next_best_action", {}).get("action") != "configure_provider_or_use_existing_graph":
    raise SystemExit(f"Unexpected first-use offline hydration plan: {plan}")

proc.stdin.close()
try:
    status_code = proc.wait(timeout=20)
except subprocess.TimeoutExpired:
    proc.terminate()
    status_code = proc.wait(timeout=10)
if status_code != 0:
    raise SystemExit(
        f"runtime hydration smoke container exited with status {status_code}\n"
        f"--- stderr ---\n{err_file.read_text(encoding='utf-8', errors='replace')}\n"
        f"--- stdout ---\n{out_file.read_text(encoding='utf-8', errors='replace')}"
    )

print("runtime hydration docker smoke planned a real v2 endpoint over MCP stdio")
PY
