# HPE Networking Central MCP — Technical Overview

> Version: `0.3.0` · Python `≥ 3.12` · Transport: `stdio`

This document is the single authoritative reference for anyone who needs to understand
how the MCP server is built, what contracts its components expose to one another, and how
data flows through the system at runtime.

---

## Table of Contents

1. [High-Level Architecture](#1-high-level-architecture)
2. [Repository Layout](#2-repository-layout)
3. [Startup Sequence](#3-startup-sequence)
4. [Component Reference](#4-component-reference)
   - 4.1 [Configuration (`config.py`)](#41-configuration-configpy)
   - 4.2 [Authentication & HTTP (`central_client.py`, `_http_core.py`)](#42-authentication--http-central_clientpy-_http_corepy)
   - 4.3 [Knowledge DB (`knowledge_db.py`)](#43-knowledge-db-knowledge_dbpy)
   - 4.4 [Graph Layer (`graph/`)](#44-graph-layer-graph)
   - 4.5 [OAS Normalisation & Schema-Graph Ingestion (`oas_normalize.py`, `oas_schema_graph.py`)](#45-oas-normalisation--schema-graph-ingestion)
   - 4.6 [API Catalog (`api_tree.py`, `oas_index.py`)](#46-api-catalog)
   - 4.7 [MCP Tools (`tools/`)](#47-mcp-tools-tools)
   - 4.8 [Script Runtime (`central_helpers.py`, `_http_core.py`)](#48-script-runtime)
   - 4.9 [Seed Scripts (`seeds/`)](#49-seed-scripts-seeds)
   - 4.10 [MCP Resources (`resources/`)](#410-mcp-resources-resources)
   - 4.11 [MCP Prompts (`prompts/`)](#411-mcp-prompts-prompts)
   - 4.12 [Server Instructions (`instructions.py`)](#412-server-instructions-instructionspy)
5. [Graph Data Model](#5-graph-data-model)
6. [IPC Protocol (Graph Access from Scripts)](#6-ipc-protocol-graph-access-from-scripts)
7. [Build Pipeline](#7-build-pipeline)
8. [Deployment & Configuration](#8-deployment--configuration)
9. [Read-Only Mode](#9-read-only-mode)
10. [Error Handling Strategy](#10-error-handling-strategy)
11. [Testing Strategy](#11-testing-strategy)
12. [Key Design Decisions](#12-key-design-decisions)

---

## 1. High-Level Architecture

```
┌─────────────────────────────────────────────────────────────────────┐
│                     MCP Client (e.g. Claude Code)                   │
└────────────────────────────────┬────────────────────────────────────┘
                                 │  stdio (JSON-RPC / MCP protocol)
┌────────────────────────────────▼────────────────────────────────────┐
│                    FastMCP Server  (server.py)                       │
│  ┌──────────────┐  ┌──────────────┐  ┌─────────────┐  ┌──────────┐ │
│  │   MCP Tools  │  │ MCP Resources│  │ MCP Prompts │  │  System  │ │
│  │  (tools/)    │  │ (resources/) │  │ (prompts/)  │  │ Instruct.│ │
│  └──────┬───────┘  └──────┬───────┘  └─────────────┘  └──────────┘ │
│         │                 │                                          │
│  ┌──────▼───────────────────────────────────────────────────┐       │
│  │                 Graph Layer  (graph/)                     │       │
│  │  GraphManager ── LadybugDB (real_ladybug) ── schema.py   │       │
│  │  GraphIPCServer ── authenticated loopback TCP            │       │
│  └───────────────────────────────────────────────────────────┘       │
│         │                                                            │
│  ┌──────▼──────┐   ┌──────────────────┐   ┌──────────────────────┐  │
│  │  Central    │   │ Knowledge DB      │   │  Seed Scripts        │  │
│  │  Client     │   │ (downloaded from  │   │  (seeds/ — auto-run  │  │
│  │  OAuth2/    │   │  GitHub Release)  │   │   + on-demand)       │  │
│  │  httpx      │   └──────────────────┘   └──────────────────────┘  │
│  └──────┬──────┘                                                     │
└─────────┼───────────────────────────────────────────────────────────┘
          │  HTTPS (OAuth2 + REST)
  ┌───────▼──────────┐    ┌──────────────────────────────┐
  │  HPE Aruba       │    │  HPE GreenLake Platform       │
  │  Networking      │    │  (global.api.greenlake.hpe.com│
  │  Central         │    │  – optional)                  │
  └──────────────────┘    └──────────────────────────────┘
```

The server exposes three surfaces to MCP clients:

| Surface | Purpose |
|---------|---------|
| **Tools** | Callable functions (API calls, graph queries, script management) |
| **Resources** | Read-only structured content (`graph://schema`, `api://endpoint-catalog`, `docs://…`) |
| **Prompts** | Guided workflow templates injected as pre-filled messages |

---

## 2. Repository Layout

```
src/hpe_networking_central_mcp/
├── server.py               # Entry point — wires all components, starts FastMCP
├── config.py               # Settings dataclass, loaded from environment
├── instructions.py         # Builds MCP system-instruction string
├── logging.py              # Structured JSON logging via structlog
│
├── central_client.py       # CentralClient / GreenLakeClient (OAuth2 + httpx)
├── _http_core.py           # BaseHTTPClient + error hierarchy (also copied to script library)
├── central_helpers.py      # Pre-auth helper singletons for script subprocesses
│
├── knowledge_db.py         # Downloads knowledge DB tar.gz from GitHub release
├── api_tree.py             # Renders API endpoint catalog as a compact path-tree
├── oas_normalize.py        # OpenAPI normalisation + skeleton/glossary projections
├── oas_schema_graph.py     # Decomposes OAS spec into the schema property-graph
├── oas_index.py            # In-memory search index over ApiEndpoint rows (legacy)
├── spec_provider.py        # SpecProvider protocol + registry
├── oas_scraper.py          # ReadMe.io scraper for Aruba Central OAS specs
├── glp_spec_provider.py    # GreenLake developer portal spec fetcher
│
├── graph/
│   ├── schema.py           # DDL constants (NODE_TABLES, REL_TABLES, etc.)
│   ├── manager.py          # GraphManager — DB lifecycle, query/execute API
│   ├── ipc_server.py       # Authenticated loopback IPC for script subprocesses
│   └── invariants.py       # Post-flush invariants for ingestion correctness
│
├── tools/
│   ├── api_call.py         # call_central_api, call_greenlake_api
│   ├── api_call_validation.py  # Path + body pre-flight validation
│   ├── graph.py            # query_graph, write_graph
│   ├── scripts.py          # list_scripts, save_script
│   └── execution.py        # execute_script
│
├── resources/
│   ├── docs.py             # docs://central/overview, docs://script-writing-guide, api://endpoint-catalog
│   └── graph.py            # graph://schema, graph://seed-status
│
├── prompts/
│   └── workflows.py        # analyze_inventory, troubleshoot_device, analyze_config, write_script
│
└── seeds/                  # Bundled seed scripts + metadata
    ├── populate_base_graph.py   # Bootstrap domain hierarchy (auto_run=true)
    ├── enrich_topology.py       # CONNECTED_TO / LINKED_TO edges (auto_run=true, depends_on: populate_base_graph)
    ├── populate_config_policy.py
    ├── populate_monitoring.py   # Ports, Radios, Clients (auto_run=false)
    ├── onboard_device.py
    └── analyze_topology.py
```

Build-time tooling lives in `scripts/` and is not packaged into the wheel.

---

## 3. Startup Sequence

```
server.py  main() → load_settings() → create_server()
    │
    ├── Validate Central credential completeness
    ├── CentralClient.validate()     → OAuth2 token check when credentials exist
    │
    ├── download_knowledge_db()      → select knowledge-db-* release, verify SHA-256,
    │                                  stage and atomically install
    ├── _check_knowledge_pin()       → enforce optional release/digest pin
    ├── GraphManager.initialize()    → open / create LadybugDB, apply bootstrap DDL + migrations
    ├── GraphManager.create_fts_indexes()
    │
    ├── _check_knowledge_schema_version()  → compare manifest.json schema_version (currently 10)
    │
    ├── _load_api_tree()             → MATCH ApiEndpoint → render_path_tree() → embed in instructions
    │
    ├── FastMCP("hpe-networking-central-mcp", instructions=…)
    ├── GraphIPCServer.start()       → authenticated loopback TCP in full connected mode
    │
    ├── GreenLakeClient.validate()   → optional, degrades gracefully
    ├── Full profile: copy/sync script runtime and seeds
    ├── Workshop profile: omit writes, scripts, GreenLake, compiler/hydration and seed jobs
    │
    ├── Register tools, resources, prompts
    │
    ├── threading.Thread(_bg_auto_run_seeds)  → topological seed execution in background
    │
    └── FastMCP.run()                → stdio MCP server (blocking)
```

### Schema-version gate

`manifest.json` (shipped inside the knowledge DB tar.gz) carries a `schema_version`
integer. The server requires **version 10**. A mismatch raises `StartupError`
before tool registration to prevent serving stale graph data.

---

## 4. Component Reference

### 4.1 Configuration (`config.py`)

```python
@dataclass(frozen=True)
class Settings:
    profile: str                 # full or fail-closed workshop
    central_base_url: str
    central_client_id: str
    central_client_secret: str
    glp_client_id: str          # falls back to central_client_id
    glp_client_secret: str      # falls back to central_client_secret
    glp_base_url: str           # default: https://global.api.greenlake.hpe.com
    script_library_path: Path   # default: per-user application data
    graph_db_path: Path         # default: per-user application data
    spec_cache_path: Path       # default: per-user application cache
    inventory_cache_ttl: int    # default: 300 s
    glp_included_slugs: str     # comma-separated or "*" for all
    knowledge_release_repo: str # owner/repo for knowledge DB download
    knowledge_release_tag: str  # optional immutable release pin
    knowledge_asset_sha256: str # optional immutable digest pin
    read_only: bool             # refuses mutating API calls
```

Loaded from environment variables via `load_settings()` and optionally overlaid
by the server CLI before construction.
The object is `frozen` — settings never change at runtime.

**Derived properties:**

| Property | Logic |
|---|---|
| `has_credentials` | All three Central fields non-empty |
| `has_glp_credentials` | Effective GLP id + secret non-empty |
| `effective_glp_client_id` | `glp_client_id or central_client_id` |
| `parsed_glp_included_slugs` | `None` (= all) or `set[str]` |

---

### 4.2 Authentication & HTTP (`central_client.py`, `_http_core.py`)

#### Class hierarchy

```
BaseHTTPClient  (_http_core.py)
├── CentralClient (central_client.py)   — Central API
└── GreenLakeClient (central_client.py) — HPE GreenLake platform API
```

#### `BaseHTTPClient`

| Aspect | Detail |
|--------|--------|
| Token URL | `{base_url}/as/token.oauth2` (Central), `https://sso.common.cloud.hpe.com/…` (GLP) |
| Grant type | `client_credentials` |
| Refresh | 60 s before expiry |
| Thread safety | `threading.Lock()` around token acquisition |
| Retry — 401 | Reset token, retry once |
| Retry — 429 | Parse `Retry-After` / `X-RateLimit-Reset`, wait ≤ 60 s, retry once |
| HTTP library | `httpx` (sync) |

#### Error hierarchy

```
CentralAPIError(status_code, error_code, message, debug_id)
├── AuthenticationError  (401 / 403)
├── RateLimitError       (429 after retry)
├── NotFoundError        (404)
└── PaginationError      (unexpected pagination shape)
```

#### `validate()`

Performs a lightweight token fetch. Called at startup; any exception causes `sys.exit(1)`.

---

### 4.3 Knowledge DB (`knowledge_db.py`)

At startup, `download_knowledge_db(repo, db_path)` checks the local `manifest.json`
for a `release_tag` and compares it with the latest GitHub release. If they differ
(or no local copy exists), it downloads `knowledge_db.tar.gz` from the release assets,
extracts it to `db_path`, and stamps the new `release_tag` into the manifest.

Timeouts: 8 s for the release-info API call, 120 s for the download, to avoid
blocking the MCP client's `initialize` budget.

The knowledge DB is built offline (see §7) and contains:
- All `ApiEndpoint` rows and the full schema property-graph (`Parameter`, `RequestBody`,
  `Response`, `SchemaComponent`, `Property`, …)
- `DocSection` rows (script-writing guide, overview docs)
- Bundled `Script` rows (seed scripts)
- `ApiCategory` grouping rows

---

### 4.4 Graph Layer (`graph/`)

#### `GraphManager`

| Method | Description |
|--------|-------------|
| `initialize()` | Open / create LadybugDB, apply all DDL, run column migrations, load extensions |
| `create_fts_indexes()` | Create full-text search index; graceful degradation if FTS unavailable |
| `query(cypher, params, read_only)` | Execute Cypher; when `read_only=True` rejects write keywords |
| `execute(cypher, params)` | Execute without write-keyword guard (used by seeds and IPC server) |
| `get_schema_description()` | Live introspection: node tables, rel tables, row counts, example queries |

**Thread safety:** `threading.Lock()` serialises all connection access.
A single `lb.Database` is kept open; `lb.Connection` objects are created per operation.

**Write-keyword guard** (enforced in `query()` when `read_only=True`):
`CREATE | DELETE | DETACH | SET | REMOVE | MERGE | DROP | ALTER | COPY | INSERT | LOAD | INSTALL`

#### `GraphIPCServer`

Exposes the `GraphManager` to authorized script subprocesses over an ephemeral
TCP port bound exclusively to `127.0.0.1`.

**Protocol:** newline-delimited JSON.

```json
// Request
{"id": 1, "token": "<capability>", "method": "query"|"execute", "cypher": "MATCH …", "params": {}}

// Success response
{"id": 1, "result": [{…}, …]}

// Error response
{"id": 1, "error": "message string"}
```

One thread is used per connection (`socketserver.ThreadingMixIn`). A random
capability token is created on every server start and passed only to child
scripts together with the selected loopback port.

#### `graph/schema.py` — DDL constants

Schema is split into logical groups, all applied at `GraphManager.initialize()`:

| Constant | Tables |
|---|---|
| `NODE_TABLES` | `Org`, `SiteCollection`, `Site`, `DeviceGroup`, `Device`, `UnmanagedDevice` |
| `KNOWLEDGE_NODE_TABLES` | `ApiEndpoint`, `ApiCategory`, `DocSection`, `Script`, `Parameter`, `RequestBody`, `Response`, `SchemaComponent`, `Property` |
| `REL_TABLES` | Core hierarchy: `HAS_COLLECTION`, `HAS_SITE`, `CONTAINS_SITE`, `HAS_DEVICE`, `IN_GROUP` |
| `KNOWLEDGE_REL_TABLES` | `BELONGS_TO_CATEGORY`, `HAS_PARAMETER`, `HAS_REQUEST_BODY`, `HAS_RESPONSE`, `BODY_REFERENCES`, `RESPONSE_REFERENCES`, `HAS_PROPERTY`, `PROPERTY_OF_TYPE`, `COMPOSED_OF`, `REFERENCES` |
| `TOPOLOGY_REL_TABLES` | `CONNECTED_TO` (Device→Device), `LINKED_TO` (Device→UnmanagedDevice) |
| `POLICY_REL_TABLES` | `ORG_ASSIGNS_CONFIG`, `COLLECTION_ASSIGNS_CONFIG`, `SITE_ASSIGNS_CONFIG`, `GROUP_ASSIGNS_CONFIG` |

`ALTER_ADD_LAST_SYNCED_AT` holds idempotent `ALTER TABLE … ADD` statements for the
`lastSyncedAt TIMESTAMP` column added in schema version 9.

#### `graph/invariants.py`

Four post-flush invariants run by the build pipeline after schema-graph ingestion:

| Invariant | Check |
|---|---|
| `INV-1` | Every named `SchemaComponent` with a non-empty object body has ≥ 1 `HAS_PROPERTY` / `COMPOSED_OF` / `HAS_VALUE_SCHEMA` edge |
| `INV-2` | Every `ApiEndpoint` row has ≥ 1 reachable `Parameter` or `RequestBody` (where the spec defines them) |
| `INV-3` | No orphaned `Property` nodes (must trace back to a `SchemaComponent`) |
| `INV-4` | No duplicate `(component_id, spec_source)` pairs |

In strict mode (`--strict`), violations exit non-zero (CI default).

---

### 4.5 OAS Normalisation & Schema-Graph Ingestion

#### `oas_normalize.py`

**`normalize(spec)`** — idempotent transforms on a raw OpenAPI 3.x dict:
- Promotes repeated inline schemas to `components/schemas`
- Rewrites use-sites to `$ref`

**`project_skeleton(spec, method, path)`** — structure-only projection:
- Parameters, request body shape, first success + error response shell
- Strips all human-readable prose (`description`, `title`, `example`, …)
- Includes `$components_index` with minimal type/enum/required/child_refs hints

**`project_glossary(spec, method, path)`** — prose-only complement:
- Returns only the stripped keys from skeleton
- Includes minimum structural scaffold to reach the prose

The two projections share a single `_SKELETON_STRIP_KEYS` constant so they
never drift — adding a key atomically moves it from skeleton to glossary.

#### `oas_schema_graph.py`

Build-time helper that decomposes a normalised spec into the schema property-graph:

```
ApiEndpoint ──HAS_PARAMETER──▶ Parameter
ApiEndpoint ──HAS_REQUEST_BODY──▶ RequestBody ──BODY_REFERENCES──▶ SchemaComponent
ApiEndpoint ──HAS_RESPONSE──▶ Response ──RESPONSE_REFERENCES──▶ SchemaComponent
SchemaComponent ──HAS_PROPERTY──▶ Property
SchemaComponent ──COMPOSED_OF──▶ SchemaComponent   (allOf / oneOf / anyOf)
Property ──PROPERTY_OF_TYPE──▶ SchemaComponent
SchemaComponent ──REFERENCES──▶ SchemaComponent    (inline $ref)
```

Bulk-loads via PyArrow `COPY FROM` for performance; deduplication is done in Python
before the COPY so re-running on the same spec is idempotent.

---

### 4.6 API Catalog

#### `api_tree.py`

Renders all `ApiEndpoint` rows from the graph into a compact category-grouped
path-tree that is embedded in the MCP system instructions:

```
## Monitoring (314)
  network-monitoring/
    v1/
      aps  [GET]
        {serial-number}  [GET]
```

Deprecated endpoints are marked with a trailing `!`. In read-only mode, non-GET
methods are hidden. The tree is built once at startup (`_load_api_tree()`) from a
live graph query; it uses ≈ 14–16k tokens for ~2,100 endpoints.

#### `oas_index.py`

In-memory search index (legacy, used by older `search_api_catalog` flow):
- `OASIndex`: builds from `EndpointEntry` list
- Multi-term keyword scoring: `path×3`, `summary×2`, `operation_id×2`, `tags×2`, `description×1`
- AND logic: all terms must match; returns up to 40 results

---

### 4.7 MCP Tools (`tools/`)

#### `tools/api_call.py`

| Tool | Signature | Notes |
|------|-----------|-------|
| `call_central_api` | `(path, method="GET", query_params, body)` | Validates path (no `..`, non-empty), rejects body on GET |
| `call_greenlake_api` | `(path, method="GET", query_params, body)` | Same validation; unavailable if GLP credentials missing |

Validation is delegated to `tools/api_call_validation.py`. Both tools surface
`CentralAPIError` as MCP `ToolError`.

#### `tools/graph.py`

| Tool | Signature | Notes |
|------|-----------|-------|
| `query_graph` | `(cypher: str)` | Read-only Cypher; context-aware error hints + freshness warnings |
| `write_graph` | `(cypher: str, parameters: dict)` | Allows MERGE/CREATE/SET/DELETE; no DDL |

**Freshness warnings** (`_scan_freshness`): checks `lastSyncedAt` on volatile node fields
(e.g. `Device.status`, `Device.firmware`) against a configurable threshold
(`MCP_GRAPH_STALE_THRESHOLD_SECONDS`, default 900 s) and appends a structured warning
to the result when data may be stale.

**Error hints** (`_build_error_hint`): parses Kuzu error messages to surface
property names valid for the queried label, or the full compact schema on
table/relationship errors.

#### `tools/scripts.py`

| Tool | Signature | Notes |
|------|-----------|-------|
| `list_scripts` | `(tag: str \| None)` | Reads `.meta.json` for each `.py` in library; optional tag filter |
| `save_script` | `(filename, content, description, tags, parameters)` | Validates name (alphanumeric + `_-`), no path traversal, `.py` suffix; preserves `last_run` on overwrite |

#### `tools/execution.py`

| Tool | Signature | Notes |
|------|-----------|-------|
| `execute_script` | `(filename, parameters: dict[str,str])` | Path-traversal guard; 5-min timeout; env injection; updates `.meta.json` |

**Environment injected into subprocesses:**

```
CENTRAL_BASE_URL, CENTRAL_CLIENT_ID, CENTRAL_CLIENT_SECRET
GLP_CLIENT_ID, GLP_CLIENT_SECRET, GLP_BASE_URL
GRAPH_IPC_HOST, GRAPH_IPC_PORT, GRAPH_IPC_TOKEN
  ← authenticated loopback endpoint, injected only into child scripts
```

**Output truncation:** 10 KB stdout, 5 KB stderr.

---

### 4.8 Script Runtime

Scripts in the library import two helper modules copied from the package at startup:

| Module | Source | Purpose |
|--------|--------|---------|
| `_http_core.py` | `src/…/_http_core.py` | `BaseHTTPClient` + error hierarchy (no external deps beyond stdlib) |
| `central_helpers.py` | `src/…/central_helpers.py` | `CentralAPI`, `GreenLakeAPI`, `GraphClient` singletons |

**`central_helpers` API:**

```python
from central_helpers import api, glp, graph

# HTTP API calls
api.get(path, params={})
api.post(path, json_body={})
api.paginate(path, params={}, max_pages=50, page_size=100)

# GreenLake (optional)
glp.paginate("devices/v1/devices")

# Graph DB (via IPC — no file lock conflicts)
graph.query("MATCH (d:Device) RETURN d.serial")
graph.execute("MERGE (n:Device {serial: $s}) SET n.name = $n", {"s": "SN1", "n": "SW1"})
```

`api.paginate()` auto-detects cursor-based (`next` token, MRT APIs) vs offset-based
(`offset` integer, Config APIs) pagination.

---

### 4.9 Seed Scripts (`seeds/`)

Each seed has a companion `.meta.json`:

```json
{
  "description": "…",
  "tags": ["graph", "topology"],
  "auto_run": true,
  "depends_on": ["populate_base_graph.py"],
  "parameters": {}
}
```

**Auto-run seeds** are executed on startup in dependency order (topological sort via
`graphlib.TopologicalSorter`) in a daemon thread (`_bg_auto_run_seeds`).
The `graph://seed-status` resource exposes per-seed outcome.

| Seed | `auto_run` | Depends on |
|------|-----------|-----------|
| `populate_base_graph.py` | ✅ | — |
| `enrich_topology.py` | ✅ | `populate_base_graph.py` |
| `populate_config_policy.py` | ❌ | — |
| `populate_monitoring.py` | ❌ | — |
| `onboard_device.py` | ❌ | — |
| `analyze_topology.py` | ❌ | — |

After each seed run, the `Script` graph node is updated with `last_run` and
`last_exit_code` via `_update_script_node()`.

---

### 4.10 MCP Resources (`resources/`)

| URI | Handler | Description |
|-----|---------|-------------|
| `graph://schema` | `GraphManager.get_schema_description()` | Live introspection: node tables, rel tables, row counts, hierarchy diagram, example Cypher |
| `graph://seed-status` | `_seed_status` dict | Per-seed execution status (started_at, finished_at, exit_code, error) |
| `api://endpoint-catalog` | Built from `ApiEndpoint` graph rows | Full category-grouped path-tree of all endpoints (same source as system instructions) |
| `docs://central/overview` | Static text | Central + GreenLake platform overview |
| `docs://script-writing-guide` | Static text | Script template, helper reference, error handling |

---

### 4.11 MCP Prompts (`prompts/`)

| Prompt | Parameters | Purpose |
|--------|-----------|---------|
| `analyze_inventory()` | — | Guided workflow: explore hierarchy → device health → live API → report |
| `troubleshoot_device(identifier)` | `identifier: str` | Graph lookup → site context → blast radius → live diagnostics → script library |
| `analyze_config(scope)` | `scope: str` | Config precedence walk → effective config via API (`effective=true&detailed=true`) |
| `write_script(task)` | `task: str` | Guided automation script generation: API discovery → pagination → error handling |

Prompts inject structured decision trees as pre-filled assistant messages.

---

### 4.12 Server Instructions (`instructions.py`)

`build_instructions(read_only, api_tree)` constructs the MCP system prompt that is
sent to the client on connection. It embeds:

- Operational rules (API discovery workflow, tool-invocation patterns)
- The full API endpoint path-tree (built from live graph data at startup)
- Config-model explanation (Global → SiteCollection → Site → DeviceGroup → Device precedence)
- Pagination guidance (`api.paginate()` in scripts vs. `call_central_api` for single lookups)
- Error-handling class names
- GreenLake access notes

In read-only mode, the instructions suppress mutation-related guidance.

---

## 5. Graph Data Model

### Domain Layer

```
Org {scopeId, name}
  │─[HAS_COLLECTION]──▶ SiteCollection {scopeId, name, siteCount, deviceCount, lastSyncedAt}
  │                          │─[CONTAINS_SITE]──▶ Site
  │─[HAS_SITE]──────────────▶ Site {scopeId, name, address, city, country, deviceCount, …, lastSyncedAt}
                                │─[HAS_DEVICE]──▶ Device {serial, name, mac, model, deviceType,
                                                          status, ipv4, firmware, persona,
                                                          deviceFunction, siteId, …, lastSyncedAt}
                                                      │─[CONNECTED_TO]──▶ Device
                                                      │─[LINKED_TO]────▶ UnmanagedDevice

DeviceGroup {scopeId, name, deviceCount, lastSyncedAt}
  │─[IN_GROUP] ◀── Device   (cross-cutting, not hierarchical)
```

### Knowledge Layer

```
ApiEndpoint {endpoint_id, method, path, summary, category, deprecated, …}
  │─[BELONGS_TO_CATEGORY]──▶ ApiCategory {name, endpointCount, sourceProvider}
  │─[HAS_PARAMETER]────────▶ Parameter {name, location, required, type, enumValues, …}
  │─[HAS_REQUEST_BODY]─────▶ RequestBody ──[BODY_REFERENCES]──▶ SchemaComponent
  │─[HAS_RESPONSE]──────────▶ Response ───[RESPONSE_REFERENCES]▶ SchemaComponent

SchemaComponent {component_id, name, type, kind, bodyShape, required[], enumValues[], …}
  │─[HAS_PROPERTY]──────────▶ Property {name, type, required, enumValues[], …}
  │─[COMPOSED_OF]───────────▶ SchemaComponent   (allOf / oneOf / anyOf)
  │─[HAS_VALUE_SCHEMA]──────▶ SchemaComponent   (array item schema)

Property ──[PROPERTY_OF_TYPE]──▶ SchemaComponent

DocSection {section_id, title, content, source, url}

Script {filename, description, tags[], content, parameters, created_at, last_run, last_exit_code}
```

### Configuration Layer (on-demand)

```
ConfigProfile {name, scope, category, …}
  ◀─[ORG_ASSIGNS_CONFIG]─── Org
  ◀─[COLLECTION_ASSIGNS_CONFIG]─── SiteCollection
  ◀─[SITE_ASSIGNS_CONFIG]─── Site
  ◀─[GROUP_ASSIGNS_CONFIG]─── DeviceGroup
```

**Config precedence (high → low):** Device > DeviceGroup > Site > SiteCollection > Org.
The graph stores scope *assignments* only; effective (resolved) values are always
fetched via the Central API with `effective=true&detailed=true`.

### Volatile fields & freshness

Fields with `lastSyncedAt` staleness tracking:

| Node | Volatile fields |
|------|----------------|
| `Device` | `status`, `firmware`, `configStatus`, `ipv4` |
| `Site` | `deviceCount` |
| `SiteCollection` | `deviceCount`, `siteCount` |
| `DeviceGroup` | `deviceCount` |

---

## 6. IPC Protocol (Graph Access from Scripts)

Scripts executed by `execute_script` access the graph through the IPC server rather
than opening LadybugDB directly (which would conflict with the server's open file handle).

```
Script subprocess
  │
  │  from central_helpers import graph
  │  graph.query("MATCH (d:Device) …")
  │
  └─── GraphClient (in central_helpers.py)
         │  connect to 127.0.0.1:GRAPH_IPC_PORT
         └─── token-authenticated TCP ──▶ GraphIPCServer (in server process)
                                          │
                                          └─── GraphManager.query() / .execute()
```

`GraphClient` in `central_helpers.py` accepts only the loopback host, connects
using `GRAPH_IPC_HOST` and `GRAPH_IPC_PORT`, and authenticates every JSON request
with the per-process capability in `GRAPH_IPC_TOKEN`. These values are created
at server start and injected only into authorized script subprocesses. The
transport works on Windows and POSIX hosts without platform-specific sockets.

---

## 7. Build Pipeline

`scripts/build_knowledge_db.py` runs on GitHub Actions to produce the offline
knowledge DB shipped as a GitHub Release asset:

```
1. Scrape/fetch OpenAPI specs
   ├── oas_scraper.py  → Aruba Central ReadMe.io portal
   └── glp_spec_provider.py → GreenLake developer portal

2. oas_normalize.normalize(spec)  — deduplicate inline schemas

3. Apply bootstrap DDL to a fresh LadybugDB

4. Populate ApiEndpoint + ApiCategory rows

5. oas_schema_graph.populate_schema_graph()
   — Parameter, RequestBody, Response, SchemaComponent, Property nodes + edges

6. Run graph/invariants.py (4 invariants)
   — --strict exits non-zero on violation (CI gate)

7. Populate DocSection rows (docs, script-writing guide)

8. Populate Script rows (bundled seed scripts)

9. Create FTS indexes

10. Write manifest.json  {schema_version: 9, release_tag: …}

11. tar.gz → upload to GitHub Release
```

**Local build:**

```bash
uv run python scripts/build_knowledge_db.py            # warn-only mode
uv run python scripts/build_knowledge_db.py --strict   # CI mode
```

**Refreshing real-spec test fixtures** (do not hand-edit):

```bash
cp build/spec_cache/central/Config/<slug>.json tests/fixtures/oas/real_excerpts/central_config_<slug>.json
cp build/spec_cache/glp/<slug>.json            tests/fixtures/oas/real_excerpts/glp_<slug>.json
```

---

## 8. Deployment & Configuration

### Environment Variables

| Variable | Required | Default | Description |
|---|---|---|---|
| `CENTRAL_BASE_URL` | ✅ | — | `https://internal.api.central.arubanetworks.com` |
| `CENTRAL_CLIENT_ID` | ✅ | — | OAuth2 client ID |
| `CENTRAL_CLIENT_SECRET` | ✅ | — | OAuth2 client secret |
| `GREENLAKE_CLIENT_ID` / `GLP_CLIENT_ID` | ❌ | Falls back to Central | GreenLake client ID |
| `GREENLAKE_CLIENT_SECRET` / `GLP_CLIENT_SECRET` | ❌ | Falls back to Central | GreenLake client secret |
| `GLP_BASE_URL` | ❌ | `https://global.api.greenlake.hpe.com` | GreenLake base URL |
| `MCP_PROFILE` | ❌ | `full` | Runtime surface: `full` or fail-closed `workshop` |
| `SCRIPT_LIBRARY_PATH` | ❌ | per-user app data | Writable directory for user scripts |
| `GRAPH_DB_PATH` | ❌ | per-user app data | LadybugDB file path |
| `SPEC_CACHE_DIR` | ❌ | per-user cache | OpenAPI/compiler download cache |
| `KNOWLEDGE_RELEASE_REPO` | ❌ | this repository | `owner/repo` for knowledge DB download |
| `KNOWLEDGE_RELEASE_TAG` | ❌ | newest `knowledge-db-*` | Immutable knowledge snapshot pin |
| `KNOWLEDGE_ASSET_SHA256` | ❌ | GitHub asset digest | Optional explicit SHA-256 pin |
| `INVENTORY_CACHE_TTL` | ❌ | `300` | Seconds to cache inventory in memory |
| `GLP_INCLUDED_SLUGS` | ❌ | `*` | Comma-separated GreenLake service slugs |
| `READ_ONLY` | ❌ | `false` | Block mutating API calls |
| `MCP_GRAPH_STALE_THRESHOLD_SECONDS` | ❌ | `900` | Freshness warning threshold |

### Native Python package

The supported Docker-free production path is the Python wheel built from this
repository. `uv` installs a managed Python 3.12 runtime and the exact locked
binary dependencies without requiring administrator rights, WSL, or Docker:

```bash
uv tool install --python 3.12 --no-build --constraints constraints.txt hpe-networking-central-mcp==0.3.0
hpe-networking-central-mcp doctor --profile workshop --skip-credentials
```

Pull requests build and install the wheel on Windows x64, macOS Apple Silicon,
and Linux, then exercise `doctor` and an MCP stdio handshake. Version tags
publish wheel/sdist to PyPI and attach constraints, checksums, and the workshop
starter ZIP to a GitHub release.

### Docker

A `Dockerfile` is provided. The image entrypoint is:

```bash
hpe-networking-central-mcp  # = python -m hpe_networking_central_mcp.server
```

### Claude Desktop / Claude Code

```json
{
  "mcpServers": {
    "hpe-networking-central-mcp": {
      "command": "uvx",
      "args": ["hpe-networking-central-mcp"],
      "env": {
        "CENTRAL_BASE_URL": "...",
        "CENTRAL_CLIENT_ID": "...",
        "CENTRAL_CLIENT_SECRET": "..."
      }
    }
  }
}
```

---

## 9. Read-Only Mode

When `READ_ONLY=true`:

- `call_central_api` and `call_greenlake_api` reject non-GET methods with a `ToolError`.
- The API endpoint path-tree embedded in system instructions omits non-GET methods.
- The `api://endpoint-catalog` resource also filters to GET-only.
- Local graph writes (`write_graph`) and script CRUD / execution remain available.

`MCP_PROFILE=workshop` is stricter than general read-only mode. It permanently
enables read-only operation and omits local graph writes, script CRUD/execution,
GreenLake, compiler/hydration tools, prompts, and automatic seed jobs. Connected
workshop sessions expose Central GET calls only.

---

## 10. Error Handling Strategy

| Layer | Mechanism |
|-------|-----------|
| OAuth2 / network | `BaseHTTPClient._request()` — retry on 401 (once) and 429 (once, with backoff); structured `CentralAPIError` subclasses |
| MCP tool surface | `CentralAPIError` → `mcp.ToolError` in `tools/api_call.py`; Cypher errors → hints in `tools/graph.py` |
| Script execution | Subprocess stderr captured; `.meta.json` updated with `last_exit_code`; output truncated (10 KB / 5 KB) |
| Startup | `sys.exit(1)` on missing/invalid credentials; `SystemExit` on schema version mismatch |
| Seeds | Per-seed failure recorded in `_seed_status`; remaining seeds continue |
| Build invariants | `InvariantViolationError` (strict) or structured list (warn-only) |

---

## 11. Testing Strategy

Tests are organised by marker (declared in `pyproject.toml`):

| Marker | Scope | Credentials needed |
|--------|-------|--------------------|
| `unit` | Pure Python, no network, no graph DB | No |
| `integration` | Local subprocess / IPC / temp DB | No (most) |
| `live_api` | Real Central / GreenLake API calls | Yes |
| `slow` | Takes more than a few seconds | — |
| `oas_ingest` | OAS → graph ingestion smoke (real spec excerpts) | No |

`live_api` and `integration` tests auto-skip when `CENTRAL_BASE_URL` /
`CENTRAL_CLIENT_ID` / `CENTRAL_CLIENT_SECRET` are absent (see `tests/conftest.py`).

```bash
uv run pytest -m "unit and not slow"          # fast feedback, no creds
uv run pytest                                 # full suite (skips live_api)
uv run pytest -m live_api                     # needs .env with creds
uv run pytest --cov --cov-report=term-missing
```

Real OAS spec excerpts live in `tests/fixtures/oas/real_excerpts/` and are tested
by `tests/test_real_spec_ingest_smoke.py` against `graph/invariants.py`.

---

## 12. Key Design Decisions

| Decision | Rationale |
|---|---|
| **No `pycentral` dependency** | Avoids a heavy SDK; minimal `httpx` stack keeps the image small and reduces supply-chain surface |
| **LadybugDB (Kuzu) as graph engine** | Native property-graph with Cypher; file-backed so scripts can read/write without a separate DB process |
| **IPC instead of direct DB access from scripts** | Scripts run in subprocesses; LadybugDB file handles cannot be safely shared across processes without coordination — the IPC server serialises access |
| **Pre-authenticated helper modules** | Credentials injected via environment; scripts import `central_helpers` with zero OAuth2 boilerplate |
| **Knowledge DB as GitHub Release asset** | Separates the expensive offline build (OAS scraping, schema-graph ingestion) from runtime startup; server boots in seconds |
| **Schema version gate** | Prevents a stale knowledge DB from being served silently after a schema evolution |
| **Schema subgraph (ADR 009)** | Decomposes per-endpoint OAS blobs into queryable Cypher entities so `query_graph` can serve as the primary API-discovery surface instead of raw JSON search |
| **Skeleton / glossary split (ADR 007)** | One tool call for structure (99% of cases), a second for prose (ambiguous fields) — avoids sending megabytes of description text on every detail call |
| **Auto-run seeds in topological order** | Seeds declare `depends_on`; `graphlib.TopologicalSorter` ensures correct execution order without hardcoding |
| **Freshness warnings on volatile fields** | `lastSyncedAt` on domain nodes enables the tool to warn agents when graph data may be stale without requiring a live API call |
| **Read-only mode** | Enables safe demonstration / auditing environments where Central mutations must be prevented |
