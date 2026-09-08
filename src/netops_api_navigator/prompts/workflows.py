"""MCP Prompts - guided workflow templates."""

from __future__ import annotations

from typing import TYPE_CHECKING

import structlog

if TYPE_CHECKING:
    from ..graph.manager import GraphManager

logger = structlog.get_logger("prompts.workflows")


def register_prompts(mcp, graph_manager: GraphManager):
    """Register workflow prompts with the MCP server."""

    @mcp.prompt()
    def analyze_inventory() -> str:
        """Guide: analyze the current network hierarchy, devices, and identify issues."""
        return """You are analyzing the HPE Aruba Networking Central network using the configuration graph.

Follow this workflow:

1. **Read Graph Schema**: Read the graph://schema resource to understand the data model.

2. **Explore Hierarchy**: Use query_graph() to understand the structure:
   ```cypher
   MATCH (o:Org)-[:HAS_COLLECTION]->(sc:SiteCollection)-[:CONTAINS_SITE]->(s:Site)
   RETURN o.name AS org, sc.name AS collection, s.name AS site
   ```
   Also check standalone sites:
   ```cypher
   MATCH (o:Org)-[:HAS_SITE]->(s:Site)
   RETURN s.name AS site, s.city AS city
   ```

3. **Device Overview**: Query devices per site and their status:
   ```cypher
   MATCH (s:Site)-[:HAS_DEVICE]->(d:Device)
   RETURN s.name AS site, d.deviceType AS type, d.status AS status, count(d) AS count
   ORDER BY s.name
   ```

4. **Find Issues**: Check for offline devices:
   ```cypher
   MATCH (s:Site)-[:HAS_DEVICE]->(d:Device {status: 'OFFLINE'})
   RETURN s.name AS site, d.serial, d.name, d.model
   ```

5. **Deep Dive**: Use call_central_api() for live monitoring data:
   - Site health endpoints
   - AP/switch/gateway specific stats
   - Client connection counts

6. **Present Report**: Summarize findings:
   - Hierarchy overview (collections → sites → devices)
   - Overall health (% online)
   - Action items (offline devices, firmware inconsistencies)
   - Recommendations

Present data in tables where appropriate for readability.
"""

    @mcp.prompt()
    def troubleshoot_device(identifier: str) -> str:
        """Guide: troubleshoot a specific network device."""
        return f"""You are troubleshooting device "{identifier}" in HPE Aruba Networking Central.

Follow this workflow:

1. **Find Device in Graph**: Use query_graph() to locate the device and its context:
   ```cypher
   MATCH (s:Site)-[:HAS_DEVICE]->(d:Device)
   WHERE d.serial CONTAINS '{identifier}' OR d.name CONTAINS '{identifier}'
   RETURN d.serial, d.name, d.model, d.status, d.ipv4, d.firmware, s.name AS site
   ```

2. **Understand Context**: Check what else is at the same site:
   ```cypher
   MATCH (s:Site)-[:HAS_DEVICE]->(d:Device)
   WHERE s.name = '<site_from_step_1>'
   RETURN d.serial, d.name, d.deviceType, d.status
   ```

3. **Check Blast Radius**: If the device is in a collection, understand the hierarchy:
   ```cypher
   MATCH (sc:SiteCollection)-[:CONTAINS_SITE]->(s:Site)-[:HAS_DEVICE]->(d:Device {{serial: '{identifier}'}})
   RETURN sc.name AS collection, s.name AS site
   ```

4. **Live Diagnostics**: Use call_central_api() for real-time monitoring data.
   Find the relevant `METHOD /path` in the API endpoint catalog
   (`api://endpoint-catalog`), then use `query_graph` against
   `Parameter`/`RequestBody`/`Property` nodes for the field-by-field
   guide (see `graph://schema` for canned patterns).

5. **Check Script Library**: Call list_scripts(tag="troubleshooting") for existing diagnostic scripts.

6. **Present Findings**:
   - Device status and key attributes
   - Hierarchy context (collection → site → device)
   - Comparison with other devices at the same site
   - Recommended actions
"""

    @mcp.prompt()
    def analyze_config(scope: str = "") -> str:
        """Guide: analyze configuration across scopes using the Central API."""
        scope_clause = f' Focus on scope: "{scope}".' if scope else ""
        return f"""You are analyzing configuration policy in HPE Aruba Networking Central.{scope_clause}

Follow this workflow:

1. **Discover Config Categories**: Browse the API endpoint catalog
   (`api://endpoint-catalog` resource) for endpoints under
   `network-config/...`, then use `query_graph` against
   `Parameter`/`RequestBody`/`Response` nodes to understand the
   endpoint's parameters and body shape (see `graph://schema` for
   canned Cypher patterns).

2. **Understand the Hierarchy**: Query the graph for scope structure:
   ```cypher
   MATCH (o:Org)-[:HAS_SITE]->(s:Site)-[:HAS_DEVICE]->(d:Device)
   RETURN o.name AS org, s.name AS site, d.serial, d.name, d.deviceType
   ORDER BY s.name
   ```

3. **Read Effective Config per Device** — use the Central API:
   ```
   call_central_api(
       "network-config/v1alpha1/{{category}}",
       query_params={{"scopeId": "<device-serial>", "scopeType": "device",
                     "effective": "true", "detailed": "true"}}
   )
   ```
   The `detailed=true` response includes provenance annotations showing
   `scope_type` (GLOBAL/SITE/DEVICE), `scope_id`, and `scope_name` for
   each setting — telling you exactly where it comes from in the hierarchy.

4. **Blast Radius for Config Change**: Before modifying config at a scope:
   ```cypher
   MATCH (s:Site {{name: 'MySite'}})-[:HAS_DEVICE]->(d:Device)
   RETURN d.serial, d.name, d.deviceType, d.configStatus
   ```

5. **Present Report**:
   - Hierarchy overview (org → collections → sites → devices)
   - Per-device effective config (with inheritance source from API)
   - Anomalies: conflicting overrides, unexpected scope sources
   - Recommendations
"""

    @mcp.prompt()
    def write_script(task_description: str) -> str:
        """Guide: write a Python automation script for a given task.

        Provides the script-writing template and instructs the agent to discover
        API endpoints via the API endpoint catalog instead of embedding the full catalog.

        Args:
            task_description: What the script should accomplish.
        """
        return f"""You are writing a Python automation script for HPE Aruba Networking Central.

## Task
{task_description}

## Step 1 — Discover Endpoints

Before writing ANY code you MUST:
1. Find candidate `METHOD /path` combinations in the API endpoint catalog
   (`api://endpoint-catalog` resource).
2. Use `query_graph` against the `Parameter`/`RequestBody`/`SchemaComponent`/
   `Property` subgraph for each endpoint you plan to use — get exact
   parameter names, types, required-ness, request-body fields, and
   per-device support. See `graph://schema` for canned Cypher patterns.
3. A pre-flight validator runs on every `call_central_api` /
   `call_greenlake_api` and will reject calls with missing required
   parameters or body fields, inlining a schema summary so you can
   correct and retry.

NEVER guess or hardcode API paths — always discover them first.

## Script Template

```python
#!/usr/bin/env python3
\"\"\"<one-line description of what this script does>\"\"\"

import argparse, json, sys
from central_helpers import api, glp, graph
from central_helpers import CentralAPIError, NotFoundError

def main():
    parser = argparse.ArgumentParser()
    # parser.add_argument("--site", required=True, help="Site name")
    args = parser.parse_args()

    # --- your logic here ---

if __name__ == "__main__":
    main()
```

## Authentication

Scripts run as subprocesses with pre-authenticated helpers injected:
- `api` — CentralAPI instance. Use `api.get(path)`, `api.post(path, body)`, `api.put(path, body)`, `api.delete(path)`, `api.patch(path, body)`.
- `api.paginate(path)` — Auto-paginating GET that returns a flat list. Use for any list endpoint.
- `glp` — GreenLakeAPI instance. Same interface, hits `https://global.api.greenlake.hpe.com`.
- `graph` — GraphHelper for the LadybugDB graph DB. Use `graph.execute(cypher, params)` for writes, `graph.query(cypher)` for reads.
- `CentralAPIError`, `NotFoundError` — Exception classes for error handling.

**Do NOT** handle OAuth2, tokens, or base URLs — the helpers do that.

## Pagination

NEVER pass `limit` to individual API calls for collecting data.
Use `api.paginate(path)` which auto-detects cursor vs offset pagination:
```python
all_aps = api.paginate("/monitoring/v2/aps")
```

## Error Handling

```python
from central_helpers import CentralAPIError, NotFoundError
try:
    result = api.get(f"/monitoring/v1/aps/{{serial}}")
except NotFoundError:
    print(f"AP {{serial}} not found", file=sys.stderr)
    sys.exit(1)
except CentralAPIError as e:
    print(f"API error: {{e}}", file=sys.stderr)
    sys.exit(1)
```

## Output

- Print JSON to stdout for structured results: `json.dump(result, sys.stdout, indent=2)`
- Print diagnostics/progress to stderr: `print("Processing...", file=sys.stderr)`
- Exit 0 on success, non-zero on failure.

## Rules

1. Use ONLY endpoints discovered via the API endpoint catalog — NEVER guess API paths.
2. Use `api.paginate()` for any list/collection endpoint.
3. Always handle errors with try/except CentralAPIError.
4. Print results as JSON to stdout.
5. Keep scripts focused — one task per script.
"""
