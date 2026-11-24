# Development

[Documentation index](README.md) · [Architecture](TECHNICAL_OVERVIEW.md) · [Maintenance](maintenance.md)

## Local setup

Use Python 3.12 or newer through uv. From the repository root, stage the verified
extension bundle before installing the project:

```sh
uv run --no-project python scripts/prepare_extensions.py
uv sync --frozen --extra test
uv run netops-api-navigator --help
```

See the [engine distribution guide](ladybug-extension-upgrade.md) for bundle
verification, supported platforms, and extension smoke checks.

For an isolated discovery runtime, select paths under the gitignored `tmp/`
directory before starting the server. The following example is for a POSIX shell;
set the same environment variables in PowerShell on Windows:

```sh
mkdir -p tmp/dev-runtime
GRAPH_DB_PATH="$PWD/tmp/dev-runtime/graph" \
SCRIPT_LIBRARY_PATH="$PWD/tmp/dev-runtime/scripts" \
SPEC_CACHE_DIR="$PWD/tmp/dev-runtime/specs" \
uv run netops-api-navigator --read-only
```

Leave Central credentials unset for discovery-only work. A first run downloads
knowledge. Do not point experimental runs at your normal runtime database.
See [configuration](configuration.md) for profiles,
pins, and source-versus-release differences.

Before editing, follow [AGENTS.md](../AGENTS.md), including its rules for long
commands and repo-local scratch/log files. Documentation changes belong in the
existing page for their audience; keep the README's entry path short.

## Overview

The graph schema uses a single bootstrap DDL layer:

1. **Bootstrap DDL** ([src/netops_api_navigator/graph/schema.py](../src/netops_api_navigator/graph/schema.py)) — Static node/relationship tables for the core domain model (Org, Site, Device, ConfigProfile, etc.) and knowledge layer (ApiEndpoint, ApiCategory, DocSection, Script).

## Graph query tools (ADR-012)

Use the [MCP reference](mcp-reference.md#graph-and-discovery) for tool
availability and query/batch behavior. The focused aliases share an executor;
write operations use `write_graph` in the full profile. `query_runtime` provides
an additional view of the optional runtime data layers.

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

### Batching and response limits

The [MCP reference](mcp-reference.md#graph-and-discovery) owns input behavior;
[configuration](configuration.md#graph-response-limits) owns the limits and their
defaults. Keep implementations and those references aligned when changing caps
or truncation envelopes.

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
and by `tests/test_real_spec_ingest_smoke.py`. Checks return
`InvariantViolation` records; strict mode raises `InvariantViolationError`
containing the collected violations. INV-8
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
- Use `uv run` for Python commands so tests use the repository environment.

## Testing

Run the fast subset before every push. The full suite remains the CI gate; it
is not the per-edit feedback loop. Keep long runs in one background terminal,
redirect logs into `tmp/`, and poll the existing run instead of starting it again.

In a checkout without `.env`, define the Central variables as empty for test
collection, matching CI. Some helper imports require the keys to exist even when
live tests are excluded. In a POSIX shell:

```sh
CENTRAL_BASE_URL= CENTRAL_CLIENT_ID= CENTRAL_CLIENT_SECRET= bash scripts/dev_test.sh
```

The pytest suite is organised by **markers** (declared in
[pyproject.toml](../pyproject.toml)):

| Marker       | What it covers                                          | Credentials needed |
|--------------|---------------------------------------------------------|--------------------|
| `unit`       | Fast in-process checks; some use a local graph fixture | No                 |
| `integration`| Integration scenarios; collection requires Central credentials | Yes |
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
bash scripts/dev_test.sh

# Tests selected from changed source files
bash scripts/test_changed.sh

# Full suite (skips live_api without .env)
uv run pytest

# Live API contract / seed tests (requires .env with creds)
uv run pytest -m live_api

# With coverage
uv run pytest --cov --cov-report=term-missing
```

### Real-spec ingestion fixtures (ADR-011)

Tests marked `real_spec` need the downloaded Central cache. Hydrate it once:

```sh
bash scripts/hydrate_test_fixtures.sh
```

The cache is stored in `tmp/test_fixtures/central_spec_cache/`. This is separate
from the small committed excerpts below.

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

`scripts/build_knowledge_db.py` runs the checks defined in
`graph/invariants.py` after the schema subgraph is populated. Its audit flags:

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

The smoke scripts under `scripts/` exercise the built image and are not invoked
by pytest. Build an image first, then pass its tag to the relevant script:

```sh
docker build -t netops-api-navigator:test .
bash scripts/ci_docker_v2_smoke.sh netops-api-navigator:test
bash scripts/ci_docker_runtime_hydration_smoke.sh netops-api-navigator:test
```

These checks use temporary Docker volumes and download knowledge artifacts.

### Standalone smoke scripts

Two former tests that were really utility runners now live in
`scripts/`:

- `scripts/smoke_oas_e2e.py` — scrapes the Central OAS docs end-to-end.
- `scripts/smoke_graph_live.py` — drives the live populate-graph path.

They are not collected by pytest.

