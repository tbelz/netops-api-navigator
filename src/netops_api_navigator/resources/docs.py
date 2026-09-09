"""MCP Resources - documentation for agent context."""

from __future__ import annotations

import json
from pathlib import Path
from typing import TYPE_CHECKING

import structlog

from ..config import Settings

if TYPE_CHECKING:
    from ..graph.manager import GraphManager

logger = structlog.get_logger("resources.docs")


def register_api_catalog_resource(mcp, settings: Settings, graph_manager: "GraphManager"):
    """Register the API endpoint catalog as a fetchable MCP resource.

    Claude Desktop silently drops the MCP server ``instructions`` field, so
    the API tree that is also embedded there is unreachable in that client.
    Exposing the same tree as an explicit resource lets the agent fetch it
    with a single tool call and place it directly in the context window.
    """
    from ..api_tree import render_path_tree

    def _render_catalog() -> str:
        if graph_manager is None or not graph_manager.is_available:
            return (
                "API endpoint catalog is currently unavailable — "
                "graph database not initialized. Try again shortly."
            )
        try:
            rows = graph_manager.query(
                "MATCH (e:ApiEndpoint) "
                "RETURN e.method AS method, e.path AS path, "
                "e.category AS category, e.deprecated AS deprecated",
                read_only=True,
            )
        except Exception:
            logger.exception("api_endpoint_catalog_query_failed")
            return (
                "API endpoint catalog is currently unavailable. "
                "Try again shortly; see server logs for details."
            )
        return render_path_tree(rows, read_only=settings.read_only)

    @mcp.resource("api://endpoint-catalog")
    def api_endpoint_catalog() -> str:
        """Full API Endpoint Catalog — every available METHOD /path for Central and GreenLake.

        Read this resource **before** calling ``call_central_api()`` or
        ``call_greenlake_api()`` to find the correct METHOD and exact path.
        The catalog is grouped by API category with nested path-tree
        indentation. A trailing ``!`` marks deprecated endpoints.

        If you cannot see the API Endpoint Catalog in the system
        instructions, reading this resource is the authoritative fallback.
        Guessing API paths without consulting the catalog has a near-zero
        chance of success.
        """
        return _render_catalog()

    # Alias under the docs:// scheme used by every other documentation
    # resource. Some MCP clients (notably Claude Code builds) hide
    # resources whose URI scheme they don't recognise; exposing the same
    # content under docs:// guarantees the catalog is reachable from any
    # spec-compliant client.
    @mcp.resource("docs://endpoint-catalog")
    def docs_endpoint_catalog() -> str:
        """Alias for ``api://endpoint-catalog`` — full API Endpoint Catalog.

        Identical content to ``api://endpoint-catalog``; provided under the
        ``docs://`` scheme for clients that filter unknown schemes from
        their resource picker.
        """
        return _render_catalog()


def register_resources(mcp, settings: Settings, graph_manager: GraphManager | None = None):
    """Register documentation resources with the MCP server."""

    @mcp.resource("docs://central/overview")
    def central_overview() -> str:
        """Overview of the Central and GreenLake API surface - what's available and how to use it."""
        return _read_doc(settings.docs_path / "central" / "overview.md",
                         fallback=CENTRAL_API_OVERVIEW)

    @mcp.resource("docs://script-writing-guide")
    def script_writing_guide() -> str:
        """Guide for writing automation scripts that the MCP server can execute."""
        return SCRIPT_WRITING_GUIDE

    @mcp.resource("docs://config-workflows")
    def config_workflows() -> str:
        """Central hierarchy, scope IDs, and configuration workflow patterns."""
        return CONFIG_WORKFLOWS

    @mcp.resource("script://seeds")
    def seed_scripts() -> str:
        """Pre-built seed scripts available in the automation library."""
        import json as _json
        seeds_dir = Path(__file__).parent.parent / "seeds"
        entries = []
        for meta_file in sorted(seeds_dir.glob("*.meta.json")):
            meta = _json.loads(meta_file.read_text(encoding="utf-8"))
            script_name = meta_file.stem + ".py"  # foo.meta → foo.py
            entries.append(
                f"### {script_name}\n"
                f"{meta.get('description', 'No description')}\n\n"
                f"**Tags:** {', '.join(meta.get('tags', []))}\n\n"
                f"**Parameters:**\n"
                + "\n".join(
                    f"- `--{p['name']}` ({p.get('type','str')})"
                    f"{' [required]' if p.get('required') else ''}"
                    f" — {p.get('description','')}"
                    for p in meta.get("parameters", [])
                )
            )
        if not entries:
            return "No seed scripts available."
        return "# Seed Scripts\n\nPre-built reusable scripts. Use `list_scripts()` to see them, `execute_script()` to run.\n\n" + "\n\n".join(entries)

    # ── Dynamic VSG documentation resources ───────────────────────────

    @mcp.resource("docs://vsg/list")
    def vsg_doc_list() -> str:
        """List all available VSG documentation sections. Use section_id values to read full content via docs://vsg/{section_id}."""
        gm = graph_manager
        if gm is None or not gm.is_available:
            return json.dumps({"error": "Graph database not available."})
        try:
            rows = gm.query(
                "MATCH (d:DocSection) WHERE d.source = 'vsg-central' "
                "RETURN d.section_id, d.title, d.url ORDER BY d.section_id",
                read_only=True,
            )
            sections = [
                {"section_id": r["d.section_id"], "title": r["d.title"], "url": r["d.url"]}
                for r in rows
            ]
            return json.dumps({"total": len(sections), "sections": sections}, indent=2)
        except Exception as exc:
            logger.debug("vsg_list_failed", error=str(exc))
            return json.dumps({"error": "No VSG documentation available.", "detail": str(exc)})

    @mcp.resource("docs://vsg/{section_id}")
    def vsg_doc_section(section_id: str) -> str:
        """Read full content of a VSG documentation section by section_id."""
        gm = graph_manager
        if gm is None or not gm.is_available:
            return "Documentation not available — graph database not initialized."
        try:
            rows = gm.query(
                "MATCH (d:DocSection {section_id: $sid}) "
                "RETURN d.title, d.content, d.url",
                {"sid": section_id},
                read_only=True,
            )
            if not rows:
                return f"No documentation section found with id '{section_id}'.\n\nUse docs://vsg/list to see available sections."
            r = rows[0]
            title = r.get("d.title", "")
            content = r.get("d.content", "")
            url = r.get("d.url", "")
            return f"# {title}\n\n{content}\n\n---\nSource: {url}"
        except Exception as exc:
            logger.debug("vsg_section_failed", section_id=section_id, error=str(exc))
            return f"Error retrieving section '{section_id}': {exc}"


def _read_doc(path: Path, fallback: str = "Documentation not available.") -> str:
    """Read a documentation file, returning fallback if not found."""
    if path.exists():
        return path.read_text(encoding="utf-8")
    return fallback


# --- Embedded fallback documentation ---

CENTRAL_API_OVERVIEW = """\
# HPE Aruba Networking Central & GreenLake — API Overview

This MCP server provides authenticated access to two API platforms and an in-memory
configuration graph for structural navigation.

## Configuration Graph (via `query_graph`)

A file-backed LadybugDB graph models the Central configuration hierarchy:
Org → SiteCollection → Site → Device, DeviceGroup → Device.

Read the **graph://schema** resource for the full schema, relationships, and example
Cypher queries. Use `query_graph(cypher)` for structural questions (hierarchy navigation,
blast-radius analysis, cross-site comparison, device lookup).

The graph is populated and enriched by seed scripts at startup.
Scripts can write directly to the graph using `from central_helpers import graph`.

## 1. Aruba Central APIs (via `call_central_api`)

Base URL: configured via `CENTRAL_BASE_URL` (e.g. `https://internal.api.central.arubanetworks.com`).

### Monitoring (network-monitoring/v1alpha1/)
- **devices** — list, filter, inspect monitored devices (switches, APs, gateways)
- **aps** — AP-specific monitoring, CPU/memory/PoE stats
- **gateways** — gateway monitoring, interfaces, tunnels
- **sites** — site health and per-site device health
- **clients** — wireless/wired client monitoring, trends

### Configuration (network-config/v1alpha1/)
- **Profiles** — 100+ config profile types: VLANs, WLANs, DHCP, routing (OSPF, BGP),
  ACLs, AAA, NTP, DNS, SNMP, and more
- CRUD pattern: GET/POST/PATCH/DELETE on `/network-config/v1alpha1/{type}[/{name}]`

## 2. GreenLake Platform APIs (via `call_greenlake_api`)

Base URL: `https://global.api.greenlake.hpe.com`

- **Device Management** (`/devices/v1/`) — add, view, manage devices in your workspace
- **Subscriptions** — license/subscription management and assignment
- **Service Catalog** — provision service managers (e.g. assign devices to Central)
- **Locations** — site/location management at the GreenLake platform level
- **Authorization, Tags, Workspaces, Audit Logs** — and many more

## API Discovery

All endpoints from both platforms are indexed in a single unified catalog.

1. `api://endpoint-catalog` resource — the authoritative `METHOD /path`
   listing for both platforms, grouped by category. GreenLake categories
   appear as "HPE GreenLake APIs for ...".
2. `query_graph(cypher)` — the source of truth for parameter and body
   details. Run Cypher against `ApiEndpoint`, `Parameter`, `RequestBody`,
   `Response`, `SchemaComponent`, and `Property` nodes. `allOf` branches
   are flattened at seed time, so a single `HAS_PROPERTY` traversal
   returns every inherited leaf property. Read `graph://schema` for the
   full subgraph and canned Cypher patterns.

Every `call_central_api` / `call_greenlake_api` invocation is pre-flight
validated against the graph. Missing required query parameters and
missing required top-level body fields (POST only) are rejected with a
structured error containing a schema summary; unknown body keys appear
as warnings on a successful response.

## Authentication

Both platforms use OAuth2 client-credentials via `https://sso.common.cloud.hpe.com/as/token.oauth2`.
Token management is fully automatic — in tools and in scripts.
"""

SCRIPT_WRITING_GUIDE = """\
# Script Writing Guide

## When to Write a Script vs Use call_central_api / call_greenlake_api

**Use direct API tools** for:
- Single API calls (GET, POST, PATCH, DELETE)
- Quick lookups: device status, site health, config profiles
- One-off writes: create a VLAN, delete a profile

**Write a script** for:
- Multi-step workflows (create site → assign devices → set persona)
- Complex logic with conditionals, loops, or error handling
- Operations that need rollback on failure
- Batch operations across many devices or sites
- Workflows that span both Central and GreenLake APIs

## How Scripts Work

Scripts are Python files executed by the MCP server. The server injects
credentials as environment variables and provides `central_helpers.py` with
pre-authenticated API clients. No OAuth2 boilerplate needed.

## IMPORTANT: Discover endpoints first

Before writing any script, you MUST:
1. Find candidate `METHOD /path` combinations in the API endpoint catalog
   (`api://endpoint-catalog` resource).
2. Use `query_graph` against the `Parameter`/`RequestBody`/`SchemaComponent`/
   `Property` subgraph to get parameter names, types, required-ness, body
   fields, and per-device support. See `graph://schema` for canned
   Cypher patterns.

A pre-flight validator runs on every `call_central_api` /
`call_greenlake_api` and will reject calls with missing required
parameters or body fields. Never guess or hardcode API paths.

## Template

```python
#!/usr/bin/env python3
\"\"\"Description of what this script does.\"\"\"

import argparse
import json
import sys

from central_helpers import api, glp, graph


def main():
    parser = argparse.ArgumentParser(description="Script description")
    parser.add_argument("--param1", required=True, help="Description")
    args = parser.parse_args()

    # Use api.paginate() for collection endpoints (auto-handles cursor/offset pagination)
    all_items = api.paginate("<path-from-api-catalog>")

    # Use api.get() only for single-item lookups
    single_item = api.get("<path-from-api-catalog>/ITEM_ID")

    print(json.dumps({"status": "success", "count": len(all_items)}))


if __name__ == "__main__":
    main()
```

## API Helper Reference

### Central: `from central_helpers import api`

- `api.get(path, params=None)` → dict — **Single-item lookups only** (e.g., get one device by serial). Never use for fetching collections.
- `api.post(path, json_body=None, params=None)` → dict
- `api.patch(path, json_body=None, params=None)` → dict
- `api.put(path, json_body=None, params=None)` → dict
- `api.delete(path, params=None)` → dict
- `api.paginate(path, params=None, max_pages=50, page_size=100)` → list[dict] — **The ONLY safe way to fetch collections.** Auto-detects cursor vs offset pagination. Never use `api.get()` with a `limit` parameter for fetching multiple items.

### GreenLake: `from central_helpers import glp`

Same methods as `api` above, but targeting `https://global.api.greenlake.hpe.com`.

### Graph Database: `from central_helpers import graph`

Scripts can read from and write to the shared LadybugDB graph database.

- `graph.query(cypher, params=None)` → list[dict] — Read-only Cypher query.
- `graph.execute(cypher, params=None)` → list[dict] — Read-write Cypher query (CREATE, MERGE, SET, DELETE).

```python
from central_helpers import api, graph

# Read existing graph data
sites = graph.query("MATCH (s:Site) RETURN s.scopeId, s.name")

# Enrich the graph with data from APIs
for site in sites:
    health = api.get(f"network-monitoring/v1alpha1/sites/{site['s.scopeId']}/health")
    graph.execute(
        "MATCH (s:Site {scopeId: $sid}) SET s.health = $h",
        {"sid": site["s.scopeId"], "h": health.get("overall", "unknown")},
    )
```

The graph database is file-backed and shared between the MCP server and scripts.
Changes made by scripts are immediately visible to `query_graph()`.

### Error Handling

```python
from central_helpers import api, CentralAPIError, NotFoundError, AuthenticationError

try:
    device = api.get("<discovered-path>/SERIAL123")
except NotFoundError:
    print("Device not found", file=sys.stderr)
except CentralAPIError as e:
    print(f"Error [{e.status_code}]: {e.message}", file=sys.stderr)
```

Error classes: `CentralAPIError` (base), `AuthenticationError` (401/403),
`RateLimitError` (429), `NotFoundError` (404), `PaginationError`.

All methods handle token refresh and 401 retry automatically.
Rate-limited requests (429) are retried once after the server-specified wait.

## Environment Variables Available in Scripts

- `CENTRAL_BASE_URL` — Central API base URL
- `CENTRAL_CLIENT_ID` / `CENTRAL_CLIENT_SECRET` — Central OAuth2 credentials
- `GREENLAKE_CLIENT_ID` / `GREENLAKE_CLIENT_SECRET` — GreenLake OAuth2 credentials
- `GLP_BASE_URL` — GreenLake API base URL (default: https://global.api.greenlake.hpe.com)
- `GRAPH_DB_PATH` — Path to the shared file-backed LadybugDB graph database

**Scripts should NEVER:**
- Manage OAuth2 tokens directly
- Import httpx or requests for API calls
- Hardcode credentials or base URLs

## NetworkX for Topology Analysis

The `networkx` library is available for graph/topology analysis in scripts.
Import it directly — it is pre-installed in the MCP server environment.

```python
import networkx as nx
from central_helpers import api

# Build a NetworkX graph from LadybugDB topology data or from the topology API
G = nx.Graph()

# Example: fetch topology for a site and build a graph
topo = api.get(f"network-monitoring/v1/topology/{site_id}")
for device in topo.get("devices", []):
    G.add_node(device["serial"], name=device.get("name", ""), type=device.get("type", ""))
for link in topo.get("links", []):
    G.add_edge(link["from"], link["to"], speed=link.get("speed", 0),
               health=link.get("health", ""))

# Standard NetworkX analysis
print(f"Nodes: {G.number_of_nodes()}, Edges: {G.number_of_edges()}")
print(f"Connected: {nx.is_connected(G)}")
if nx.is_connected(G):
    print(f"Diameter: {nx.diameter(G)}")
    bridges = list(nx.bridges(G))
    print(f"Single points of failure (bridges): {bridges}")
```
"""

CONFIG_WORKFLOWS = """\
# Central Hierarchy & Configuration Workflows

## Hierarchy Scopes (top → bottom)

Central uses hierarchical scopes for configuration. Higher-scope config is
inherited by all child scopes; lower-scope config takes precedence.

| Level           | Description                                    | Precedence |
|-----------------|------------------------------------------------|------------|
| Library         | Template profiles assignable to any scope      | Lowest     |
| Global (Org)    | Encapsulates all collections, sites, devices   | Low        |
| Site Collection | Optional grouping of sites                     | Medium     |
| Site            | Network site with devices                      | High       |
| Device          | Individual device — overrides all above         | Highest    |

**Device Groups** cut across the hierarchy — they group devices from any site
for shared configuration. A device can belong to only one group.

## Scope IDs

Every config API operation requires a `scopeId` identifying the target scope.
Scope IDs are available in the configuration graph:

- **Site**: `MATCH (s:Site) RETURN s.scopeId, s.name`
- **SiteCollection**: `MATCH (sc:SiteCollection) RETURN sc.scopeId, sc.name`
- **DeviceGroup**: `MATCH (dg:DeviceGroup) RETURN dg.scopeId, dg.name`
- **Device**: `MATCH (d:Device) RETURN d.serial, d.name` (serial = device scope ID)
- **Org/Global**: `MATCH (o:Org) RETURN o.scopeId`

## Configuration API Pattern

Config endpoints are under `network-config/v1alpha1/`. They require `scopeId`
and `scopeType` query parameters.

### Read config at a scope
```
GET network-config/v1alpha1/{category}
    ?scopeId=<id>&scopeType=<site|device|collection|org>
```

Add `effective=true` for merged inherited config. Add `detailed=true` for
source annotations showing which scope each setting comes from.

### Effective Config (API-first)

For **effective (resolved) config per device**, use the Central API — it is
the single source of truth:

```
GET network-config/v1alpha1/{category}
    ?scopeId=<device-serial>&scopeType=device&effective=true&detailed=true
```

The `detailed=true` response includes `@.aruba-annotation:scope_device_function`
provenance annotations — a JSON array where each entry contains:
- `scope_type`: GLOBAL, SITE, or DEVICE
- `scope_id`: the originating scope ID
- `scope_name`: human-readable scope name
- `device_function`: applicable device function (e.g., AP, CX)

This tells you exactly where each config setting comes from in the hierarchy.

### Write config
```
POST/PATCH network-config/v1alpha1/{category}
    ?scopeId=<id>&scopeType=<site|device|collection|org>
    body: { ... config payload ... }
```

## Blast Radius Check (Before Mutations)

Before applying config at a scope, check what devices will be affected:

```cypher
// Site-level blast radius
MATCH (s:Site {name: 'MySite'})-[:HAS_DEVICE]->(d:Device)
RETURN d.serial, d.name, d.deviceType, d.configStatus

// Collection-level blast radius
MATCH (sc:SiteCollection {name: 'MyCollection'})-[:CONTAINS_SITE]->(s:Site)-[:HAS_DEVICE]->(d:Device)
RETURN s.name AS site, d.serial, d.name, d.deviceType
```

## Config Sync Verification (After Mutations)

After applying config, verify sync status:

1. Check `configStatus` on affected devices:
   ```cypher
   MATCH (s:Site {name: 'MySite'})-[:HAS_DEVICE]->(d:Device)
   RETURN d.name, d.configStatus
   ```

2. Re-run the relevant seed script (e.g., `execute_script('populate_base_graph.py')`) to pull latest state.

3. Values: `synced` = config applied, `not_synced` = pending push, `failed` = error.

## Device Function & Persona

Devices have a `deviceFunction` (e.g., access, core, distribution) and
`persona` that determine which config categories apply. Pushing incompatible
config to a device will fail. Check a device's function before applying config:

```cypher
MATCH (d:Device {serial: 'SERIAL'})
RETURN d.persona, d.deviceFunction, d.deviceType
```
"""
