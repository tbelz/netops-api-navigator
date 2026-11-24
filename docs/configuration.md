# Configuration

[Documentation index](README.md) · [Connection examples](getting-started.md)

This reference follows the current source. Use `netops-api-navigator --help` to
check options in your installed release. Settings come from the server process's
environment; CLI options override their corresponding variables. Navigator does
not automatically load `.env` files. Docker's `--env-file` is handled by Docker.

## Profiles and connectivity

The profile and credential availability are separate choices:

| Configuration | Available behavior |
|---|---|
| `full`, no Central credentials | API/schema discovery, local graph queries/writes, and script storage; no live API tools, automatic network seeds, or script execution. |
| `full`, valid Central credentials | Adds live Central calls, background seeds, and script execution. GreenLake calls are available when effective GreenLake credentials validate. |
| `full` with `--read-only` or `READ_ONLY=true` | Blocks mutating cloud API methods through API tools and script helpers. Local graph writes, script saves, and script execution remain available. |
| `workshop`, no Central credentials | Graph discovery, schema/catalog resources, and status. User script tools and direct graph writes are absent. |
| `workshop`, valid Central credentials | Adds GET-only Central calls, bounded pagination, and two trusted topology seeds. GreenLake, user script execution, compiler tools, hydration tools, and prompts remain absent. |

Use `MCP_PROFILE=workshop` or `--profile workshop` for the restricted profile. It
always enables network read-only mode. Its bundled topology seeds can still
update the local graph; the restriction is on user-controlled writes/execution.

In the full profile, read-only mode rejects `POST`, `PUT`, `PATCH`, and `DELETE`
through supported API calls and helpers, with authentication exceptions handled
internally. It also filters mutating endpoints from the endpoint catalog.
It is not an execution sandbox for untrusted Python scripts.

## Credentials

| Variable | Default | CLI equivalent / meaning |
|---|---|---|
| `CENTRAL_BASE_URL` | unset | `--central-url`; account-specific Central API URL |
| `CENTRAL_CLIENT_ID` | unset | `--client-id` |
| `CENTRAL_CLIENT_SECRET` | unset | `--client-secret` |
| `GREENLAKE_CLIENT_ID` | Central client ID | `--glp-client-id`; `GLP_CLIENT_ID` is a fallback alias when the canonical variable is unset |
| `GREENLAKE_CLIENT_SECRET` | Central client secret | `--glp-client-secret`; `GLP_CLIENT_SECRET` is a fallback alias when the canonical variable is unset |
| `GLP_BASE_URL` | `https://global.api.greenlake.hpe.com` | GreenLake API base URL |
| `GLP_INCLUDED_SLUGS` | unset | Parsed settings field, currently not wired into the default build/runtime provider. It does not filter an installed catalog. |

Central requires all three values together. Partial credentials fail startup.
No Central credentials means discovery-only mode, even if separate GreenLake
credentials are set. Central authentication failure stops startup; GreenLake
validation failure disables GreenLake while preserving Central access.

## Runtime and paths

| Variable | Default | Meaning |
|---|---|---|
| `MCP_PROFILE` | `full` | `full` or `workshop`; CLI: `--profile` |
| `READ_ONLY` | `false` | Cloud API write guard; CLI: `--read-only` |
| `GRAPH_DB_PATH` | per-user application data / `graph_db` | Persistent runtime database; one owning process |
| `SCRIPT_LIBRARY_PATH` | per-user application data / `scripts` | Saved scripts and metadata |
| `DOCS_PATH` | per-user application data / `docs` | Documentation path setting |
| `SPEC_CACHE_DIR` | per-user application cache / `specs` | Spec cache path setting |
| `INVENTORY_CACHE_TTL` | `300` | Inventory cache TTL setting in seconds |
| `MCP_HTTP_TOKEN` | unset | Required bearer token for the local HTTP transport |

Native data/cache roots are chosen by `platformdirs` for `netops-api-navigator`.
Use `doctor` to inspect resolved paths. Docker sets explicit paths under `/data`
and `/scripts`; see its [volume example](getting-started.md#docker).
Use a separate parent directory for each independent runtime graph, since its
manifest and optional compiler artifacts live alongside it.

Boolean settings accept `1`, `true`, `yes`, and `on`, ignoring case.
The HTTP transport is selected with `--transport streamable-http`; stdio is the
default. `--port` defaults to `8765`. HTTP binds only to loopback and requires a
token of at least 32 printable ASCII characters without spaces.
See [multiple local clients](getting-started.md#multiple-local-clients).

`GRAPH_IPC_SOCKET` is retained for compatibility in settings; script IPC uses
server-generated loopback TCP credentials, not that socket path. Do not configure
`GRAPH_IPC_HOST`, `GRAPH_IPC_PORT`, or `GRAPH_IPC_TOKEN` manually for scripts.

## Knowledge database lifecycle

| Variable | Default | Meaning |
|---|---|---|
| `KNOWLEDGE_RELEASE_REPO` | `tbelz/netops-api-navigator` | Repository supplying knowledge releases; an explicitly empty value disables release downloads for development |
| `KNOWLEDGE_RELEASE_TAG` | newest compatible knowledge release for a new graph | Immutable release pin; CLI: `--knowledge-release-tag` |
| `KNOWLEDGE_ASSET_SHA256` | GitHub asset digest | Optional explicit SHA-256 pin; CLI: `--knowledge-sha256` |
| `MCP_KNOWLEDGE_PROJECTION` | `legacy` | `legacy` or `v2`; `compiler` aliases `v2`. `KNOWLEDGE_PROJECTION` is a fallback variable. |

A new runtime graph is installed from a downloaded, digest-verified artifact.
`legacy` selects `knowledge_db.tar.gz`; `v2` selects
`knowledge_db_compiler.tar.gz`. The manifest records release and projection
identity. Existing runtime graphs are reused, because they can contain local
state; daily upstream publications do not automatically replace them. Explicit
pins are checked at startup rather than silently ignored.
The installed runtime determines the compatible release family; storage-format
transitions are documented in the [engine upgrade guide](ladybug-extension-upgrade.md).

If a download fails and a usable local graph exists, the server can continue
with it. Without a usable graph, startup fails. The graph's knowledge schema
version must match the runtime. Corruption stops startup with a diagnostic;
the server does not delete the graph or WAL to make the error disappear.

For a restore or knowledge upgrade:

1. Stop every process using the graph.
2. Preserve the complete graph directory, including its manifest and any `.wal`
   or `.shadow` sidecars. Keep corruption evidence when investigating a failure.
3. Choose a new empty `GRAPH_DB_PATH` in a separate directory and the desired
   knowledge release; start the server to install a verified snapshot.
4. Recreate or recover local state separately. A knowledge download does not
   restore user scripts or runtime graph edits.

Do not remove only a WAL file or mix sidecars with another database. For engine
upgrades, also follow the [storage migration and rollback procedure](ladybug-extension-upgrade.md#required-deployment-and-rollback-procedure).

## Optional compiler and hydration features

These features are opt-in in the full profile. Workshop mode disables them.

| Variable | Default | Meaning |
|---|---|---|
| `MCP_COMPILER_TOOLS` | `false` | Enable source-provenance and compiler-health tools |
| `MCP_COMPILER_DB_PATH` | sibling `knowledge_db_compiler` | Compiler projection artifact |
| `MCP_COMPILER_AST_DB_PATH` | sibling `knowledge_db_ast` | Source AST artifact |
| `MCP_COMPILER_RESPONSE_BYTES` | `200000` | Compiler response byte cap |
| `MCP_RUNTIME_HYDRATION` | `false` | Enable runtime observation, fact, and entity tools |

Selecting a v2 runtime projection and enabling compiler diagnostics are separate
settings. Routine API discovery still uses the graph tools. Enabling compiler
tools downloads the compiler sidecars. Hydration uses the shared runtime graph;
its live GET execution requires the relevant provider client. Stored observations
and planning can be inspected without live access. See the
[MCP reference](mcp-reference.md#optional-features) for the tool groups.

## Graph response limits

| Variable | Default | Meaning |
|---|---|---|
| `MCP_GRAPH_PER_CELL_BYTES` | `4096` | Oversized strings become truncation envelopes |
| `MCP_GRAPH_PER_RESPONSE_BYTES` | `50000` | Total single-query response budget |
| `MCP_GRAPH_RAW_SCHEMA_MAX_BYTES` | `200000` | Maximum raw schema payload |
| `MCP_GRAPH_BATCH_MAX_ITEMS` | `25` | Maximum items per batch |
| `MCP_GRAPH_BATCH_RESPONSE_BYTES` | `200000` | Aggregate batch response budget |
| `MCP_GRAPH_STALE_THRESHOLD_SECONDS` | `900` | Age threshold for warnings on volatile graph fields |

Use focused queries and pagination before increasing limits. The
[graph reference](mcp-reference.md#graph-and-discovery) explains batch and
truncation behavior. Source defaults live in
[config.py](../src/netops_api_navigator/config.py),
[graph tools](../src/netops_api_navigator/tools/graph.py), and
[compiler tools](../src/netops_api_navigator/tools/compiler.py).
