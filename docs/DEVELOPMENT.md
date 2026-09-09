# Graph Schema & Build Pipeline

## Overview

The graph schema uses a single bootstrap DDL layer:

1. **Bootstrap DDL** ([src/netops_api_navigator/graph/schema.py](src/netops_api_navigator/graph/schema.py)) — Static node/relationship tables for the core domain model (Org, Site, Device, ConfigProfile, etc.) and knowledge layer (ApiEndpoint, ApiCategory, DocSection, Script).

## Runtime modes

The server has two startup modes:

- **Connected** — `CENTRAL_BASE_URL` / `CENTRAL_CLIENT_ID` /
  `CENTRAL_CLIENT_SECRET` are set (via env vars or the equivalent
  `--central-url` / `--client-id` / `--client-secret` CLI args). The
  OAuth2 token is validated at startup, the auto-run seeds populate the
  domain graph, and the live-API tools (`call_central_api`,
  `call_greenlake_api`, `execute_script`) are registered.
- **Discovery-only** — no Central credentials configured. The server
  boots without contacting Central, skips the auto-run seeds, and only
  registers the graph query/write tools and the script-CRUD tools.
  Useful for local UI work and for review sessions where the agent
  drafts API calls / scripts that the user runs later in a connected
  workspace. Locally: `uv run netops-api-navigator` with an empty
  env.

## Graph query tools (ADR-012)

The graph surface is exposed through **five `query_*` tools** that share
one Cypher executor, the same row caps, and the same byte caps. They
differ only in their docstrings (canned patterns) so clients with
limited per-tool description budgets can pull in only the docs they
need:

| Tool | Use when |
|------|----------|
| `query_graph` | Generic escape hatch. Use when none of the focused aliases fit. |
| `query_api_schema` | Walking the OpenAPI surface — endpoints, components, properties, `COMPOSED_OF`, `PROPERTY_OF_TYPE`, `supportedDeviceTypes` filter. |
| `query_fts` | Full-text search via `CALL QUERY_FTS_INDEX(...)` over `api_fts`, `doc_fts`, `script_fts`, `property_fts`, `device_fts`, `site_fts`, `config_fts`. |
| `query_topology` | Live network — Org / SiteCollection / Site / Device / DeviceGroup / UnmanagedDevice and their `HAS_*` / `CONNECTED_TO` / `LINKED_TO` edges. |
| `query_yang` | `YangPath`, `PROPERTY_AT_YANG`, `CONFIGURES_YANG` provenance. |

All five are read-only and idempotent. Writes go through `write_graph`.

### FTS indexes

Built by `_create_fts_indexes` in `scripts/build_knowledge_db.py`. Use
them via `CALL QUERY_FTS_INDEX('<table>', '<index>', '<query>') YIELD
node, score` and then chain a `MATCH` in the same Cypher block to hop
from hits to whatever you actually want.

| Table | Index | Indexed columns | Notes |
|-------|-------|-----------------|-------|
| `ApiEndpoint` | `api_fts` | `summary, description, path, operationId` | Endpoint discovery. |
| `DocSection` | `doc_fts` | `title, content` | OpenAPI Markdown docs. |
| `Script` | `script_fts` | `filename, description` | Built-in scripts. |
| `Property` | `property_fts` | `name, description, yangPath` | Field-level discovery ("vrf binding", "ntp server"). Hops to owning component via `HAS_PROPERTY` then to endpoints via `BODY_REFERENCES` / `COMPOSED_OF*`. `enumValues` is a `STRING[]` and is intentionally not FTS-indexed (Kuzu limitation); use `$val IN p.enumValues`. |
| `Device` | `device_fts` | runtime | Populated by live seed. |
| `Site` | `site_fts` | runtime | Populated by live seed. |
| Config nodes | `config_fts` | runtime | Populated by live seed. |

### Batch mode on read tools

Every `query_*` tool accepts an optional `queries: list[dict]`
argument. Each item is a `{cypher, parameters?, label?}` dict.
When `queries` is set the single-call `cypher` / `parameters`
arguments are ignored (mirrors `call_central_api(calls=...)`).

| Env var | Default | What it caps |
|---------|---------|--------------|
| `MCP_GRAPH_BATCH_MAX_ITEMS` | `25` | Maximum items per batch. Over-cap calls fail fast with a `ToolError`. |
| `MCP_GRAPH_BATCH_RESPONSE_BYTES` | `200000` | Aggregate byte cap on the whole batch envelope. Trailing items are dropped and `truncated: true, kept_items: N` is set. Per-item caps still apply. |

Batches run sequentially, continue-on-error, and return
`{batch: true, total, ok, failed, results: [...]}`. Each item's
envelope is `{ok: bool, label, result | error}`.

### Response caps and `get_raw_schema`

Two byte caps protect agent context budgets:

| Env var | Default | What it caps |
|---------|---------|--------------|
| `MCP_GRAPH_PER_CELL_BYTES` | `4096` | Per-cell string size. Larger cells are replaced with a `{"_truncated": true, "preview": ..., "size_bytes": N, "hint": "..."}` envelope. |
| `MCP_GRAPH_PER_RESPONSE_BYTES` | `50000` | Total response size after row capping. Over-cap responses come back as `{"truncated": true, "reason": "response_byte_cap", ...}` with as many rows as fit. |
| `MCP_GRAPH_RAW_SCHEMA_MAX_BYTES` | `200000` | Hard cap on `get_raw_schema(component_id)`. Above this the call is refused with a hint to walk `COMPOSED_OF*0..5` → `HAS_PROPERTY` instead of pulling the blob. |

`get_raw_schema(component_id)` is the escape hatch for the per-cell
envelope: it returns the raw `bodyJson` string for a single
`SchemaComponent`, subject to the hard cap above.

### `component_id` convention

Every `SchemaComponent.component_id` follows
`<provider>:<section>:<Name>`:

| Shape | Example | Meaning |
|-------|---------|---------|
| `<provider>:<section>:<Name>` | `central:schemas:VlanInterface` | Top-level named component from a spec. |
| `...:<Name>#allOf:N` | `central:schemas:VlanInterface#allOf:1` | Inline allOf branch promoted to a synthetic component. |
| `...:<Name>#oneOf:N` / `#anyOf:N` | — | Inline union branch. |
| `...:<Name>#prop:<field>#items` | `central:schemas:VlanInterface#prop:vlan_ids#items` | Inline array-item shape. |
| `...:<Name>#additionalProperties` | — | Inline map value shape. |

Look one up by name when you only know the friendly name:

```cypher
MATCH (c:SchemaComponent {name: $n})
RETURN c.component_id
```

### Invariants (build-time guards)

`graph/invariants.py` exposes `assert_graph_invariants(conn, strict=...)`,
run by `[3d/6] Auditing graph invariants` in `build_knowledge_db.py`
and by `tests/test_real_spec_ingest_smoke.py`. Each check raises
`InvariantViolation(invariant, detail, sample)` on failure. INV-8
(`no_orphaned_properties`) catches `Property` rows that are not
reachable from any `SchemaComponent` via `HAS_PROPERTY` — the failure
mode behind ADR-013's eviction fix.

### `supportedDeviceTypes` semantic

`Property.supportedDeviceTypes` is `NULL` when the property carries no
`x-supportedDeviceType` vendor extension (i.e. it applies to **all**
device types). An empty list (`[]`) is reserved for "explicitly
restricted to no device types" and should not occur in healthy graphs.
The canonical device-type filter is therefore:

```cypher
WHERE $deviceType = ''
   OR p.supportedDeviceTypes IS NULL
   OR size(p.supportedDeviceTypes) = 0
   OR $deviceType IN p.supportedDeviceTypes
```

The `size(...) = 0` clause is a one-release safety net for graphs built
against the pre-ADR-012 populator.

## Key Modules

| Module | Purpose |
|--------|---------|
| `graph/manager.py` | Loads bootstrap DDL, provides `get_schema_description()` via live catalog introspection |
| `graph/schema.py` | Bootstrap DDL constants (`NODE_TABLES`, `REL_TABLES`, etc.) and helper functions |
| `seeds/populate_monitoring.py` | On-demand seed: ports, radios, clients (auto_run=false) |

## Build Pipeline Flow

`scripts/build_knowledge_db.py` runs on GitHub Actions:

1. Sync OpenAPI references from Aruba Central docs
2. Apply bootstrap DDL to knowledge DB
3. Index and populate API endpoints
4. Populate seed scripts
5. Create FTS indexes

## LadybugDB Quirks

- `CALL show_tables() RETURN * WHERE type = 'NODE'` is **invalid** — WHERE doesn't work with CALL. Filter in Python after `rows_as_dict()`.
- Always use `IF NOT EXISTS` in DDL for idempotent schema application.
- Use `python3.12` for tests (python3/3.10 lacks `real_ladybug`).

## Testing

The pytest suite is organised by **markers** (declared in
[pyproject.toml](../pyproject.toml)):

| Marker       | What it covers                                          | Credentials needed |
|--------------|---------------------------------------------------------|--------------------|
| `unit`       | Pure-Python logic, no network, no graph DB              | No                 |
| `integration`| Local subprocess / IPC / seed startup against a temp DB | No (some)          |
| `live_api`   | Real Central / GreenLake API calls                      | **Yes**            |
| `slow`       | Anything that takes more than a few seconds             | n/a                |

Tests marked `integration` or `live_api` are **auto-skipped** when
`CENTRAL_BASE_URL`, `CENTRAL_CLIENT_ID`, and `CENTRAL_CLIENT_SECRET`
are not set in the environment (see `pytest_collection_modifyitems` in
[tests/conftest.py](../tests/conftest.py)). The shell-script aliases
`BASE_URL` / `CLIENT_ID` / `CLIENT_SECRET` from older docs are also
accepted and back-filled to the canonical names.

```bash
# Install test extras
uv sync --extra test

# Fast feedback loop (no creds required)
uv run pytest -m "unit and not slow"

# Full suite (skips live_api without .env)
uv run pytest

# Live API contract / seed tests (requires .env with creds)
uv run pytest -m live_api

# With coverage
uv run pytest --cov --cov-report=term-missing
```

### Real-spec ingestion fixtures (ADR-011)

The OAS → graph ingestion code is regression-tested against real
HPE Aruba Central / GreenLake spec excerpts at
`tests/fixtures/oas/real_excerpts/` rather than handwritten
synthetic OpenAPI. To refresh the corpus:

```bash
# After a fresh build with the current upstream specs:
cp build/spec_cache/central/Config/<file>.json tests/fixtures/oas/real_excerpts/central_config_<slug>.json
cp build/spec_cache/glp/<file>.json            tests/fixtures/oas/real_excerpts/glp_<slug>.json
```

Do NOT hand-edit these fixtures — they are verbatim by policy.
`tests/test_real_spec_ingest_smoke.py` runs every fixture through the
full pipeline and asserts the post-flush invariants
(`src/netops_api_navigator/graph/invariants.py`) hold.

### Build-time invariants gate

`scripts/build_knowledge_db.py` now runs the four invariants from
`graph/invariants.py` after the schema subgraph is populated. Two new
flags:

- `--strict` — turn violations into a non-zero exit (CI default).
- `--no-invariants` — skip the audit entirely (rarely useful).

Locally:

```bash
uv run python scripts/build_knowledge_db.py            # warn only
uv run python scripts/build_knowledge_db.py --strict   # fail on violation
```

See [docs/adr/011-real-spec-ingestion-invariants.md](adr/011-real-spec-ingestion-invariants.md)
for the rationale.

### Docker E2E layer

The shell scripts at the repo root (`test_all.sh`, `test_mcp.sh`,
`test_inventory.sh`, etc.) exercise the built Docker image end-to-end
and are **not** invoked by `pytest`. Build the image first with
`docker build -t netops-api-navigator:test .` and then run them
manually.

### Standalone smoke scripts

Two former tests that were really utility runners now live in
`scripts/`:

- `scripts/smoke_oas_e2e.py` — scrapes the Central OAS docs end-to-end.
- `scripts/smoke_graph_live.py` — drives the live populate-graph path.

They are not collected by pytest.

