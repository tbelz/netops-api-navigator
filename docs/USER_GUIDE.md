# User guide

[Project overview](../README.md) · [Tool reference](TECHNICAL_OVERVIEW.md#tool-surface) · [OpenAPI downloads](openapi-export.md)

Installation, configuration and operating procedures for native and Docker deployments.

## Native Python Installation (Windows x64, Apple Silicon, and Linux x64)

This is the production path for local use without Docker or WSL. It runs the
server directly on the host and stores data in the operating system's normal
per-user application directories.

Install `uv` with `winget install --id=astral-sh.uv -e` on Windows,
`brew install uv` on macOS, or the
[documented installer](https://docs.astral.sh/uv/getting-started/installation/)
on Linux, then install the exact release from PyPI:

**Before upgrading an existing graph**, read the
[backup and rollback procedure](ladybug-extension-upgrade.md). Version 0.3.2
upgrades older graph storage on its first writable open, including `doctor`.

```text
uv tool install --python 3.12 --no-build netops-api-navigator==0.3.2
netops-api-navigator doctor --profile workshop --skip-credentials
```

`uv` installs Python 3.12 when it is not already available. The GitHub release
contains the wheel, locked `constraints.txt`, checksums, source archive, and a
ready-to-open VS Code workshop bundle with all supported-platform dependency
wheels. The bundle does not need PyPI access after download. Separate `.msi`,
`.exe`, or `.pkg` installers are intentionally not required.

The starter configuration is documented in [`workshop/README.md`](../workshop/README.md).
It prompts for credentials through VS Code password inputs and launches the
strict GET-only workshop profile.

## Discovery-Only Docker Profile

Use this first when you want API discovery and script authoring without granting
network credentials. Live API tools and script execution are intentionally not
registered in this mode.

```json
{
  "mcpServers": {
    "netops-api-navigator-discovery": {
      "command": "docker",
      "args": [
        "run", "-i", "--rm", "--pull", "always",
        "-v", "central-scripts:/scripts/library",
        "ghcr.io/tbelz/netops-api-navigator:main"
      ]
    }
  }
}
```

## Connected Docker Profile

Add credentials to enable `call_central_api`, `call_greenlake_api`, seed-script
startup, and `execute_script`.

```json
{
  "mcpServers": {
    "netops-api-navigator": {
      "command": "docker",
      "args": [
        "run", "-i", "--rm", "--pull", "always",
        "-v", "central-scripts:/scripts/library",
        "ghcr.io/tbelz/netops-api-navigator:main",
        "--central-url", "https://de1.api.central.arubanetworks.com",
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
`%APPDATA%\Claude\claude_desktop_config.json` on Windows. For Claude Code, use
its [MCP setup instructions](https://code.claude.com/docs/en/mcp).

CLI arguments are visible to local process/container inspection tools. On shared
workstations, prefer an env file or platform secret store.

## Configuration

You can pass the same settings through the host environment, Docker `-e` flags,
an `--env-file`, or the CLI flags shown above.

```env
CENTRAL_BASE_URL=https://de1.api.central.arubanetworks.com
CENTRAL_CLIENT_ID=your_client_id
CENTRAL_CLIENT_SECRET=your_client_secret
GREENLAKE_CLIENT_ID=your_glp_client_id
GREENLAKE_CLIENT_SECRET=your_glp_client_secret
```

Copy the exact account-specific Base URL from **Central → Menu → API Gateway →
REST API**. Do not reuse a Lab or another tenant's cluster URL; a valid token
sent to the wrong Central cluster can still be rejected with HTTP 403.

| Variable | Required | Default | Description |
| --- | --- | --- | --- |
| `CENTRAL_BASE_URL` | Connected mode | none | Central API base URL |
| `CENTRAL_CLIENT_ID` | Connected mode | none | Central OAuth2 client ID |
| `CENTRAL_CLIENT_SECRET` | Connected mode | none | Central OAuth2 client secret |
| `GREENLAKE_CLIENT_ID` | No | Central client ID | GreenLake OAuth2 client ID |
| `GREENLAKE_CLIENT_SECRET` | No | Central client secret | GreenLake OAuth2 client secret |
| `GLP_BASE_URL` | No | `https://global.api.greenlake.hpe.com` | GreenLake API base URL |
| `GLP_INCLUDED_SLUGS` | No | default set | Comma-separated GreenLake service slugs, or `*` for all |
| `MCP_PROFILE` | No | `full` | `full` or fail-closed `workshop` profile |
| `READ_ONLY` | No | `false` | Refuse mutating Central/GreenLake HTTP methods |
| `KNOWLEDGE_RELEASE_REPO` | No | this repository | GitHub repo (`owner/name`) to download knowledge DB releases from |
| `KNOWLEDGE_RELEASE_TAG` | No | newest `knowledge-db-*` release | Immutable Knowledge DB release pin |
| `KNOWLEDGE_ASSET_SHA256` | No | GitHub asset digest | Optional explicit archive digest |
| `GRAPH_DB_PATH` | No | per-user application data | Runtime LadybugDB graph path |
| `MCP_KNOWLEDGE_PROJECTION` | No | `legacy` | Runtime projection: `legacy`, `v2`, or `compiler` |
| `MCP_COMPILER_TOOLS` | No | `false` | Register compiler provenance and health tools |
| `MCP_RUNTIME_HYDRATION` | No | `false` | Register the opt-in runtime hydration foundation |
| `MCP_COMPILER_DB_PATH` | No | sibling `knowledge_db_compiler` | Compiler projection sidecar path |
| `MCP_COMPILER_AST_DB_PATH` | No | sibling `knowledge_db_ast` | Compiler AST sidecar path |
| `SCRIPT_LIBRARY_PATH` | No | per-user application data | Script library path |
| `INVENTORY_CACHE_TTL` | No | `300` | Runtime inventory cache TTL in seconds |

Native and Docker starts download the newest Knowledge DB release automatically
when the runtime graph does not yet exist. Existing runtime graphs are reused.
Set `KNOWLEDGE_RELEASE_TAG` for an immutable workshop snapshot. Downloads use
the SHA-256 digest reported by GitHub and are staged before installation.

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

### Workshop

Set `MCP_PROFILE=workshop` or pass `--profile workshop` for the smallest,
fail-closed surface intended for guided onboarding. The profile always enables
network read-only mode and exposes graph/API discovery, live topology queries,
a secret-free status tool, and—when valid Central credentials are present—GET-only
Central API calls plus bounded collection pagination. Two trusted package seeds
populate sites, devices, groups, and L2 links in the local graph at startup.
User-authored script creation/execution, direct local graph writes, GreenLake,
runtime hydration, compiler tools, and prompts remain unavailable.

Run `netops-api-navigator doctor --profile workshop` before connecting an
MCP client. Add `--skip-credentials` to validate only the local installation.
The prebuilt [`workshop/`](../workshop/) starter is included as a ZIP in tagged
GitHub releases.

`get_server_status.central_connected=true` confirms OAuth token issuance only;
Central still authorizes each API endpoint for the token's workspace and user
role. Use `topology_sync` and `graph://seed-status` to detect endpoint-level
403 errors or partial topology refreshes.

### Compiler / v2 Smoke

Recent PRs moved v2 API discovery back onto the shared graph aliases and removed
the parallel compiler endpoint/context discovery tools. Use this profile to load
the compiler/v2 runtime graph and enable the remaining compiler diagnostics:

```json
{
  "mcpServers": {
    "netops-api-navigator-v2-smoke": {
      "command": "docker",
      "args": [
        "run", "-i", "--rm", "--pull", "always",
        "-v", "netops-api-navigator-v2-data:/data",
        "-v", "central-scripts:/scripts/library",
        "-e", "KNOWLEDGE_RELEASE_REPO=tbelz/netops-api-navigator",
        "-e", "MCP_KNOWLEDGE_PROJECTION=v2",
        "-e", "MCP_COMPILER_TOOLS=true",
        "ghcr.io/tbelz/netops-api-navigator:main"
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
generic `RuntimeFact` nodes with `materialize_runtime_facts`. Agents can also
ask `plan_runtime_hydration` for a read-only plan that combines endpoint
capability, supplied parameters, provider readiness, existing observations,
freshness, and materialized facts before making live calls. Clear-identity
facts can be rolled up with `promote_runtime_entities` into generic
`RuntimeEntity` highways keyed by provider, entity type, and identity key. This
keeps the common path fast without requiring one hand-written table per Central
domain.

## Knowledge DB Startup

When `KNOWLEDGE_RELEASE_REPO` is set, the server downloads the latest released
knowledge artifact before opening a new graph. Existing runtime graphs are not
automatically refreshed, because they also contain local state. `MCP_KNOWLEDGE_PROJECTION=legacy`
uses `knowledge_db.tar.gz`; `v2` or `compiler` uses
`knowledge_db_compiler.tar.gz`.

The local manifest records the release tag, selected artifact, archive member,
and projection so the server does not confuse same-release legacy and v2
installs. If GitHub is unavailable but a local DB exists, startup keeps using
the local copy. WAL corruption and short page reads stop startup with a diagnostic;
the server never deletes the database or its sidecars to hide the error.

For an explicit restore or knowledge upgrade, stop every process using the graph,
preserve the entire graph directory (including `.wal`, `.shadow`, and `manifest.json`),
and select a new empty `GRAPH_DB_PATH` in a separate directory. The next start installs
a verified snapshot there. This restores knowledge only; local graph changes must be
recreated or recovered separately. Do not remove only the WAL or mix an old WAL with
a replacement database.

On STDIO EOF or SIGTERM, the server cancels automatic seed scripts, drains graph IPC
requests, and releases the database. Native statements already executing must finish
before close; SIGKILL or a client grace period shorter than that work can still prevent
cleanup. See [the restart investigation](graph-restart-investigation.md).

## Multiple local chats sharing one graph

A STDIO client owns its server process. If a desktop starts a separate process
for each chat, those processes cannot all open the same Ladybug graph. A second
owner fails at startup; opening the graph read-only does not bypass an active
writer. Do not delete the database or WAL to resolve ownership errors.

Use one persistent local service for multiple chats:

```bash
# Supply MCP_HTTP_TOKEN from a protected environment/secret store; generate it
# once with secrets.token_urlsafe(32), and share it only with trusted local clients.
netops-api-navigator serve --transport streamable-http --port 8765
```

The service requires `MCP_HTTP_TOKEN` (at least 32 printable ASCII characters,
without spaces), binds only to `127.0.0.1`, and serves `/mcp`. Configure its usual
Central credentials, graph and script-library paths in the service environment.
Run exactly one instance under your OS service manager. Start it before connecting
clients. Each client session uses the same account, tools and persistent graph;
closing a chat ends that session without closing the database. This mode does not
provide per-user authorization or expose a public remote ChatGPT endpoint.

For a local Codex client, replace the STDIO `command`/`args` configuration with:

```toml
[mcp_servers.arubacentral]
url = "http://127.0.0.1:8765/mcp"
bearer_token_env_var = "MCP_HTTP_TOKEN"
```

The token must also be available in the **desktop client's** environment, not just
the service environment. See the [official MCP configuration documentation](https://developers.openai.com/codex/mcp).
Keep the token out of source control. Missing or incorrect tokens receive HTTP 401.
STDIO remains the default for a single client.

For an existing installation, first arrange a graceful stop of the current graph
owner, preserve any existing corruption evidence, and then start the shared
service against the same persistent graph. Never start both transports as writers
of that graph. Verify initialization and graph queries in two concurrent native
chats before resuming work. Service shutdown drains HTTP sessions and then follows
the same seed/IPC/graph cleanup path as STDIO.

## Before upgrading an existing Ladybug graph

The runtime now pins the official `ladybug==0.17.1` distribution to use available
FTS/algo extensions. **Its first writable open upgrades older graphs from storage
format 40 to 41, including startup or `doctor`; the old engine cannot reopen them.**
Stop all graph owners and preserve the complete graph directory, manifest and
sidecars before starting this version. Rollback requires restoring that backup,
not merely reinstalling the older Python package. See the
[upgrade qualification and rollback procedure](ladybug-extension-upgrade.md).
