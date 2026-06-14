# HPE Networking Central MCP Server

[![Build](https://github.com/tbelz/hpe-networking-central-mcp/actions/workflows/build-and-push.yml/badge.svg)](https://github.com/tbelz/hpe-networking-central-mcp/actions/workflows/build-and-push.yml)
[![Knowledge DB](https://github.com/tbelz/hpe-networking-central-mcp/actions/workflows/update-knowledge-db.yml/badge.svg)](https://github.com/tbelz/hpe-networking-central-mcp/actions/workflows/update-knowledge-db.yml)
[![License: MIT](https://img.shields.io/badge/License-MIT-yellow.svg)](LICENSE)
[![Python 3.12+](https://img.shields.io/badge/python-3.12%2B-blue.svg)](https://python.org)

MCP server for **HPE Aruba Networking Central** and the **HPE GreenLake Platform**.
It gives an MCP client a graph-backed API catalog, live Central/GreenLake API
calls when credentials are configured, and a reusable Python script library for
network automation workflows.

The current agent-facing discovery surface is graph-first. Routine endpoint,
schema, CLI/YANG, and topology lookup should use the focused graph aliases
(`query_fts`, `query_api_schema`, `query_yang`, `query_topology`) before falling
back to broad `query_graph` Cypher. Compiler artifacts can be enabled for
provenance and release-health diagnostics, but they are not a separate endpoint
discovery tool surface.

## What It Provides

- OpenAPI discovery for Central and GreenLake from a pre-built LadybugDB graph.
- Focused Cypher tools for API/schema traversal, full-text search, topology, and
  YANG/CLI reverse lookup.
- Authenticated Central and GreenLake API tools in connected mode.
- Stateless pre-flight API validation against the graph before live API calls.
- A script library with seed scripts, editable saved scripts, and optional
  execution with `central_helpers` injected.
- Discovery-only and read-only modes for safer review and audit workflows.
- Optional compiler/v2 projection support for smoke testing, provenance, and
  graph-health diagnostics.
- Optional runtime-hydration foundation for graph-backed planning of future
  live-state observations.

## Architecture

```text
MCP client
  |
  | stdio JSON-RPC
  v
FastMCP server
  |
  |-- graph tools
  |     query_fts, query_api_schema, query_yang, query_topology
  |     query_graph, write_graph, get_raw_schema
  |
  |-- live API tools, only with credentials
  |     call_central_api, call_greenlake_api
  |
  |-- script tools
  |     list_scripts, get_script_content, save_script
  |     execute_script, only with credentials
  |
  |-- optional compiler tools
  |     get_openapi_source_detail, get_compiler_graph_health
  |
  |-- optional runtime hydration tools
  |     get_runtime_hydration_status, list_runtime_hydration_candidates
  |
  |-- resources
        api://endpoint-catalog, docs://endpoint-catalog
        graph://schema, graph://seed-status
        docs://central/overview, docs://script-writing-guide
        docs://config-workflows, docs://vsg/list, docs://vsg/{section_id}
        script://seeds
```

The graph has three main layers:

- Knowledge layer: build-time API and documentation graph nodes such as
  `ApiEndpoint`, `Parameter`, `RequestBody`, `Response`, `SchemaComponent`,
  `Property`, `YangPath`, `CliCommand`, `DocSection`, and `Script`.
- Domain layer: runtime network state such as `Org`, `SiteCollection`, `Site`,
  `Device`, `DeviceGroup`, and topology edges populated by seed scripts.
- Runtime hydration layer: opt-in provenance nodes such as `HydrationRun`,
  `RuntimeObservation`, `RuntimeObservedObject`, `RuntimeObservedField`, and
  `RuntimeFact` that attach future live observations back to the API graph.

## Quick Start

Prerequisites:

- Docker for the published image path.
- HPE Aruba Networking Central credentials for connected mode.
- Optional GreenLake Platform credentials, otherwise GreenLake uses the Central
  credentials when possible.

### Discovery-Only Docker Profile

Use this first when you want API discovery and script authoring without granting
network credentials. Live API tools and script execution are intentionally not
registered in this mode.

```json
{
  "mcpServers": {
    "hpe-networking-central-mcp-discovery": {
      "command": "docker",
      "args": [
        "run", "-i", "--rm", "--pull", "always",
        "-v", "central-scripts:/scripts/library",
        "ghcr.io/tbelz/hpe-networking-central-mcp:main"
      ]
    }
  }
}
```

### Connected Docker Profile

Add credentials to enable `call_central_api`, `call_greenlake_api`, seed-script
startup, and `execute_script`.

```json
{
  "mcpServers": {
    "hpe-networking-central-mcp": {
      "command": "docker",
      "args": [
        "run", "-i", "--rm", "--pull", "always",
        "-v", "central-scripts:/scripts/library",
        "ghcr.io/tbelz/hpe-networking-central-mcp:main",
        "--central-url", "https://apigw-YOUR_CLUSTER.central.arubanetworks.com",
        "--client-id", "REPLACE_WITH_YOUR_CENTRAL_CLIENT_ID",
        "--client-secret", "REPLACE_WITH_YOUR_CENTRAL_CLIENT_SECRET",
        "--glp-client-id", "REPLACE_WITH_YOUR_GLP_CLIENT_ID",
        "--glp-client-secret", "REPLACE_WITH_YOUR_GLP_CLIENT_SECRET"
      ]
    }
  }
}
```

Add `--read-only` after the credentials to keep live API access network-side
read-only:

```json
"--read-only"
```

Claude Desktop reads this shape from
`~/Library/Application Support/Claude/claude_desktop_config.json` on macOS and
`%APPDATA%\Claude\claude_desktop_config.json` on Windows. Claude Code uses the
same `mcpServers` schema in `~/.config/claude-code/config.json`.

CLI arguments are visible to local process/container inspection tools. On shared
workstations, prefer an env file or platform secret store.

## Configuration

You can pass the same settings through Docker `-e` flags, an `--env-file`, or
the CLI flags shown above.

```env
CENTRAL_BASE_URL=https://apigw-YOUR_CLUSTER.central.arubanetworks.com
CENTRAL_CLIENT_ID=your_client_id
CENTRAL_CLIENT_SECRET=your_client_secret
GREENLAKE_CLIENT_ID=your_glp_client_id
GREENLAKE_CLIENT_SECRET=your_glp_client_secret
```

| Variable | Required | Default | Description |
| --- | --- | --- | --- |
| `CENTRAL_BASE_URL` | Connected mode | none | Central API base URL |
| `CENTRAL_CLIENT_ID` | Connected mode | none | Central OAuth2 client ID |
| `CENTRAL_CLIENT_SECRET` | Connected mode | none | Central OAuth2 client secret |
| `GREENLAKE_CLIENT_ID` | No | Central client ID | GreenLake OAuth2 client ID |
| `GREENLAKE_CLIENT_SECRET` | No | Central client secret | GreenLake OAuth2 client secret |
| `GLP_BASE_URL` | No | `https://global.api.greenlake.hpe.com` | GreenLake API base URL |
| `GLP_INCLUDED_SLUGS` | No | default set | Comma-separated GreenLake service slugs, or `*` for all |
| `READ_ONLY` | No | `false` | Refuse mutating Central/GreenLake HTTP methods |
| `KNOWLEDGE_RELEASE_REPO` | No | none | GitHub repo (`owner/name`) to download knowledge DB releases from |
| `GRAPH_DB_PATH` | No | `/data/graph_db` | Runtime LadybugDB graph path |
| `MCP_KNOWLEDGE_PROJECTION` | No | `legacy` | Runtime projection: `legacy`, `v2`, or `compiler` |
| `MCP_COMPILER_TOOLS` | No | `false` | Register compiler provenance and health tools |
| `MCP_RUNTIME_HYDRATION` | No | `false` | Register the opt-in runtime hydration foundation |
| `MCP_COMPILER_DB_PATH` | No | sibling `knowledge_db_compiler` | Compiler projection sidecar path |
| `MCP_COMPILER_AST_DB_PATH` | No | sibling `knowledge_db_ast` | Compiler AST sidecar path |
| `SCRIPT_LIBRARY_PATH` | No | `/scripts/library` | Script library mount |
| `INVENTORY_CACHE_TTL` | No | `300` | Runtime inventory cache TTL in seconds |

The published Docker image sets
`KNOWLEDGE_RELEASE_REPO=tbelz/hpe-networking-central-mcp`, so normal container
starts download the latest released knowledge DB automatically. Local
non-Docker runs leave it empty unless you set it.

Partial Central credentials are treated as a configuration error. Provide all of
`CENTRAL_BASE_URL`, `CENTRAL_CLIENT_ID`, and `CENTRAL_CLIENT_SECRET` for
connected mode, or omit all three for discovery-only mode.

## Runtime Modes

### Discovery-Only

No Central credentials are configured. The server exposes the knowledge graph,
documentation resources, `write_graph`, and script CRUD tools. It does not
register `call_central_api`, `call_greenlake_api`, or `execute_script`.

### Connected

Central credentials are configured and validated during startup. The server
registers live API calls, script execution, and runtime seed execution. GreenLake
tools are registered when effective GreenLake credentials validate.

### Read-Only

Set `READ_ONLY=true` or pass `--read-only`. The server rejects `POST`, `PUT`,
`PATCH`, and `DELETE` through live API tools and through script helpers. Mutating
endpoints are filtered out of `api://endpoint-catalog`. Local graph writes,
script saves, and script execution remain available, so this is an agent
guardrail rather than a sandbox for untrusted script authors.

### Compiler / v2 Smoke

Recent PRs moved v2 API discovery back onto the shared graph aliases and removed
the parallel compiler endpoint/context discovery tools. Use this profile to load
the compiler/v2 runtime graph and enable the remaining compiler diagnostics:

```json
{
  "mcpServers": {
    "hpe-networking-central-mcp-v2-smoke": {
      "command": "docker",
      "args": [
        "run", "-i", "--rm", "--pull", "always",
        "-v", "central-mcp-v2-data:/data",
        "-v", "central-scripts:/scripts/library",
        "-e", "KNOWLEDGE_RELEASE_REPO=tbelz/hpe-networking-central-mcp",
        "-e", "MCP_KNOWLEDGE_PROJECTION=v2",
        "-e", "MCP_COMPILER_TOOLS=true",
        "ghcr.io/tbelz/hpe-networking-central-mcp:main"
      ]
    }
  }
}
```

`MCP_COMPILER_TOOLS=true` adds only:

- `get_openapi_source_detail`
- `get_compiler_graph_health`

It does not add `find_api_endpoints`, `get_api_endpoint_context`, or
`get_api_schema_context`; those tools were removed. Use `query_fts`,
`query_api_schema`, and `query_yang` for normal discovery.

### Runtime Hydration Foundation

Set `MCP_RUNTIME_HYDRATION=true` to register the opt-in runtime hydration
foundation. This adds status and candidate-planning tools plus a bounded
GET-only `hydrate_runtime_endpoint` executor that calls a selected endpoint and
persists raw runtime observations with provenance back to the API graph.
Runtime observations can be inspected with `list_runtime_observations` and
`get_runtime_observation`, and clear-identity observations can be promoted into
generic `RuntimeFact` nodes with `materialize_runtime_facts`. Typed runtime
highways remain a roadmap item tracked in
[docs/runtime-hydration-roadmap.md](docs/runtime-hydration-roadmap.md).

## Tool Surface

### Graph And Discovery Tools

| Tool | Mode | Purpose |
| --- | --- | --- |
| `query_fts` | Always | Full-text search over endpoint, property, doc, script, and runtime indexes. Use this for keyword-first discovery. |
| `query_api_schema` | Always | Focused Cypher over endpoints, parameters, request/response bodies, schema components, properties, and API/YANG edges. |
| `query_yang` | Always | YANG path, CLI command, and config-profile reverse lookup. |
| `query_topology` | Always | Runtime topology graph queries over orgs, sites, devices, groups, and neighbor edges. |
| `query_graph` | Always | Broad read-only Cypher escape hatch for cross-domain graph queries. |
| `get_raw_schema` | Always | Fetch raw OpenAPI JSON for known `SchemaComponent` IDs when graph fields are not enough. |
| `write_graph` | Always | Local graph writes for enrichment and script metadata. |

The read tools support batch mode with `queries=[...]`. Responses are capped to
keep MCP payloads manageable; oversized cells return truncation envelopes with
next-step hints.

### Live API Tools

| Tool | Mode | Purpose |
| --- | --- | --- |
| `call_central_api` | Connected | Authenticated Central REST call with graph-backed pre-flight validation. |
| `call_greenlake_api` | Connected plus GLP credentials | Authenticated GreenLake Platform REST call with validation. |

### Script Tools

| Tool | Mode | Purpose |
| --- | --- | --- |
| `list_scripts` | Always | List saved and seed scripts, optionally by tag. |
| `get_script_content` | Always | Read a script from the library. |
| `save_script` | Always | Save or update a reusable Python script. |
| `execute_script` | Connected | Run a script with Central/GreenLake helpers injected. |

### Compiler Tools

| Tool | Mode | Purpose |
| --- | --- | --- |
| `get_openapi_source_detail` | `MCP_COMPILER_TOOLS=true` | Resolve a compiler projection row back to projection data, provenance, AST metadata, and raw OpenAPI source. |
| `get_compiler_graph_health` | `MCP_COMPILER_TOOLS=true` | Run bounded traversal-health samples against compiler artifacts. |

### Runtime Hydration Tools

| Tool | Mode | Purpose |
| --- | --- | --- |
| `get_runtime_hydration_status` | `MCP_RUNTIME_HYDRATION=true` | Report the opt-in hydration stage, graph availability, schema tables, and not-yet-implemented runtime capabilities. |
| `list_runtime_hydration_candidates` | `MCP_RUNTIME_HYDRATION=true` | List GET endpoints that can be planned for future generic hydration from the compiled API graph. |
| `hydrate_runtime_endpoint` | `MCP_RUNTIME_HYDRATION=true` plus credentials | Execute one bounded GET hydration and persist raw observation/provenance nodes. |
| `list_runtime_observations` | `MCP_RUNTIME_HYDRATION=true` | List previously persisted runtime observations without calling live APIs. |
| `get_runtime_observation` | `MCP_RUNTIME_HYDRATION=true` | Fetch one observation with observed objects and fields. |
| `get_runtime_hydration_state` | `MCP_RUNTIME_HYDRATION=true` | Report whether hydrated state for an endpoint is missing, fresh, stale, or unknown. |
| `materialize_runtime_facts` | `MCP_RUNTIME_HYDRATION=true` | Promote observed objects with clear identity into generic `RuntimeFact` nodes. |
| `list_runtime_facts` | `MCP_RUNTIME_HYDRATION=true` | List materialized facts by endpoint, entity type, or identity key. |
| `get_runtime_fact` | `MCP_RUNTIME_HYDRATION=true` | Fetch one materialized fact with provenance back to observation, run, and endpoint. |

## Recommended Discovery Flow

1. Read `api://endpoint-catalog` or `docs://endpoint-catalog` for a path-tree
   overview when the endpoint family is already obvious.
2. Use `query_fts` when starting from a keyword such as a feature name, field,
   config concept, or CLI term.
3. Use `query_api_schema` to inspect exact parameters, request bodies, response
   schemas, `COMPOSED_OF`, `PROPERTY_OF_TYPE`, and `HAS_ITEM_SCHEMA` traversal.
4. Use `query_yang` when mapping YANG paths, CLI commands, or config-profile
   concepts back to API endpoints and schema properties.
5. Use `get_raw_schema` only for a known component when the structured graph
   omits source detail that you need.
6. In connected mode, call `call_central_api` or `call_greenlake_api` after
   validating the method, path, parameters, and body shape.

## Knowledge DB Startup

When `KNOWLEDGE_RELEASE_REPO` is set, the server downloads the latest released
knowledge artifact before opening the graph. `MCP_KNOWLEDGE_PROJECTION=legacy`
uses `knowledge_db.tar.gz`; `v2` or `compiler` uses
`knowledge_db_compiler.tar.gz`.

The local manifest records the release tag, selected artifact, archive member,
and projection so the server does not confuse same-release legacy and v2
installs. If GitHub is unavailable but a local DB exists, startup keeps using
the local copy. If a persisted graph fails to open with recoverable Ladybug/WAL
errors, startup forces one fresh download and retries.

## Development

Use `uv` for local Python commands.

```bash
uv sync
uv run hpe-networking-central-mcp
```

Fast local test loops:

```bash
bash scripts/dev_test.sh
bash scripts/test_changed.sh
```

The full suite is slower and is normally left to CI or broader validation:

```bash
uv run pytest
```

Build the Docker image locally:

```bash
docker build -t hpe-networking-central-mcp .
```

See [docs/DEVELOPMENT.md](docs/DEVELOPMENT.md) for build-pipeline details and
test-marker guidance. Architecture decisions live in [docs/adr/](docs/adr/).

## License

MIT
