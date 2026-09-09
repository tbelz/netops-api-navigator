#!/usr/bin/env python3
"""Start an installed MCP command and validate its workshop stdio contract."""

from __future__ import annotations

import argparse
import json
import subprocess
import sys
import tomllib
from pathlib import Path

EXPECTED_OFFLINE_WORKSHOP_TOOLS = {
    "get_raw_schema",
    "get_server_status",
    "query_api_schema",
    "query_fts",
    "query_graph",
    "query_topology",
    "query_yang",
}


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("command", nargs=argparse.REMAINDER)
    args = parser.parse_args()
    command = args.command
    if command and command[0] == "--":
        command = command[1:]
    if not command:
        parser.error("pass the installed server command after --")

    requests = [
        {
            "jsonrpc": "2.0",
            "method": "initialize",
            "id": 1,
            "params": {
                "protocolVersion": "2024-11-05",
                "capabilities": {},
                "clientInfo": {"name": "native-package-smoke", "version": "1"},
            },
        },
        {"jsonrpc": "2.0", "method": "notifications/initialized"},
        {"jsonrpc": "2.0", "method": "tools/list", "id": 2, "params": {}},
    ]
    payload = "".join(json.dumps(request) + "\n" for request in requests)
    completed = subprocess.run(
        command,
        input=payload,
        text=True,
        capture_output=True,
        timeout=60,
    )
    if completed.returncode != 0:
        print(completed.stderr, file=sys.stderr)
        return completed.returncode

    responses = []
    for line in completed.stdout.splitlines():
        try:
            responses.append(json.loads(line))
        except json.JSONDecodeError:
            continue
    initialize = next((item for item in responses if item.get("id") == 1), None)
    tools_response = next((item for item in responses if item.get("id") == 2), None)
    if initialize is None or tools_response is None:
        print(f"Missing MCP responses. stdout={completed.stdout!r}", file=sys.stderr)
        return 1
    server_name = initialize.get("result", {}).get("serverInfo", {}).get("name")
    if server_name != "netops-api-navigator":
        print(f"Unexpected server name: {server_name!r}", file=sys.stderr)
        return 1
    server_version = initialize.get("result", {}).get("serverInfo", {}).get("version")
    pyproject_path = Path(__file__).resolve().parents[1] / "pyproject.toml"
    expected_version = tomllib.loads(pyproject_path.read_text(encoding="utf-8"))["project"][
        "version"
    ]
    if server_version != expected_version:
        print(
            f"Unexpected server version: {server_version!r}; expected {expected_version!r}",
            file=sys.stderr,
        )
        return 1
    tools = {tool["name"] for tool in tools_response.get("result", {}).get("tools", [])}
    if tools != EXPECTED_OFFLINE_WORKSHOP_TOOLS:
        print(
            f"Unexpected workshop tools. expected={sorted(EXPECTED_OFFLINE_WORKSHOP_TOOLS)} "
            f"actual={sorted(tools)}",
            file=sys.stderr,
        )
        return 1
    print(
        json.dumps(
            {"server": server_name, "version": server_version, "tools": sorted(tools)},
            indent=2,
        )
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
