# Getting started

[Documentation index](README.md)

Use the [README quickstart](../README.md#quickstart) for a first native installation
and discovery request. This guide covers client-specific setup, live credentials,
Docker, and startup problems.

## Native installation

Install [uv](https://docs.astral.sh/uv/getting-started/installation/), then follow
the quickstart. The native package targets Windows x64, macOS 15+ on Apple Silicon,
and Linux x64/ARM64 with glibc. Use uv-managed Python 3.12 as selected by the
quickstart's `--managed-python` flag; no repository checkout is required. On Windows,
the pinned engine needs the OpenSSL libraries included in that Python distribution.

The install command selects the current PyPI release. To reproduce a particular
release, append `==VERSION` to the package name using a version listed in
[Releases](https://github.com/tbelz/netops-api-navigator/releases). For a source
checkout, use the [development setup](DEVELOPMENT.md#local-setup).

A fresh start requires network access for Python/package installation and the
GitHub knowledge release. Packages from 0.3.3 onward include the database extensions.
Discovery mode avoids Central API calls; it does not imply a fully offline install.
Existing local graph data is reused on later starts.

Before upgrading an existing installation, stop its server processes and follow
the [database backup and rollback procedure](ladybug-extension-upgrade.md#required-deployment-and-rollback-procedure).
`doctor` also opens the database and can trigger an engine storage migration.

## MCP clients

The executable is `netops-api-navigator`; stdio is the default transport. A client
launches and owns that process. Do not add Docker's `-t` option to a stdio setup.

### Clients using mcpServers

Use the JSON configuration in the [README](../README.md#2-add-the-server-to-your-mcp-client).
Merge its server entry into your existing configuration. Configuration file
locations depend on the client; use its MCP settings rather than assuming all
clients share a file format or path.

### VS Code

Create `.vscode/mcp.json` in your workspace, or use **MCP: Open User Configuration**:

```json
{
  "servers": {
    "netops-api-navigator": {
      "type": "stdio",
      "command": "netops-api-navigator",
      "args": ["--read-only"]
    }
  }
}
```

Start the server from VS Code's MCP controls. See the
[VS Code MCP guide](https://code.visualstudio.com/docs/agent-customization/mcp-servers)
for client configuration and tool permissions. The
[workshop starter](../workshop/README.md) supplies a separate configuration with
credential prompts and the restricted workshop profile.

### Codex

In your Codex MCP configuration:

```toml
[mcp_servers.netops_api_navigator]
command = "netops-api-navigator"
args = ["--read-only"]
```

See the [Codex MCP guide](https://developers.openai.com/codex/mcp) for configuration
locations and environment settings. If the client starts a new process for every
chat, use the shared HTTP service below instead of letting processes compete for
one database.

## Connect to Central

Obtain your Central API client ID and client secret. Copy the exact account's
Base URL from **Central → Menu → API Gateway → REST API**. A URL for a different
cluster or tenant can cause authorization failures even with valid credentials.

Set all three variables in the environment used to launch the server:

| Variable | Value |
|---|---|
| `CENTRAL_BASE_URL` | Your account's Central API base URL |
| `CENTRAL_CLIENT_ID` | Central API client ID |
| `CENTRAL_CLIENT_SECRET` | Central API client secret |

For VS Code, this complete configuration prompts for the values instead of
putting secrets in a file:

```json
{
  "inputs": [
    {"type": "promptString", "id": "central-url", "description": "Central API base URL"},
    {"type": "promptString", "id": "central-client-id", "description": "Central client ID"},
    {"type": "promptString", "id": "central-client-secret", "description": "Central client secret", "password": true}
  ],
  "servers": {
    "netops-api-navigator": {
      "type": "stdio",
      "command": "netops-api-navigator",
      "args": ["--read-only"],
      "env": {
        "CENTRAL_BASE_URL": "${input:central-url}",
        "CENTRAL_CLIENT_ID": "${input:central-client-id}",
        "CENTRAL_CLIENT_SECRET": "${input:central-client-secret}"
      }
    }
  }
}
```

Run this in the local VS Code extension host so its interactive inputs can be
resolved. For other clients, use their environment/secret settings. A `.env` file
is not automatically loaded by Navigator. Avoid passing secrets as CLI arguments
on shared systems because process inspection can expose them.

Restart the server after changing credentials. Ask the assistant to call
`get_server_status`: expect `central_connected: true`. This confirms token
validation, not permission to every API. Background topology synchronization may
still be running; inspect `topology_sync` and `graph://seed-status` for completion
or endpoint-level failures.

In the full profile, GreenLake uses Central credentials as a fallback. Set
`GREENLAKE_CLIENT_ID` and `GREENLAKE_CLIENT_SECRET` when GreenLake needs a different
client. Failed GreenLake authentication leaves Central available. Workshop mode
omits GreenLake tools. See [profiles and connectivity](configuration.md#profiles-and-connectivity)
for the exact differences, including the limits of `--read-only`.

## Docker

Use this entry in a client with the `mcpServers` JSON format:

```json
{
  "mcpServers": {
    "netops-api-navigator": {
      "command": "docker",
      "args": [
        "run", "-i", "--rm", "--pull", "always",
        "-v", "navigator-data:/data",
        "-v", "navigator-scripts:/scripts/library",
        "ghcr.io/tbelz/netops-api-navigator:main",
        "--read-only"
      ]
    }
  }
}
```

The volumes retain the local graph and saved scripts between container runs.
This example uses the image built from `main`, which can be newer than PyPI.
For reproducible deployments, select an image digest from the
[container package](https://github.com/tbelz/netops-api-navigator/pkgs/container/netops-api-navigator).

To connect Central, create a protected local environment file with the three
variables above. Insert `"--env-file", "/absolute/path/to/navigator.env"` before
the image name in `args`. Do not commit that file. See
[configuration](configuration.md) for additional settings.

## Multiple local clients

The current source supports an authenticated Streamable HTTP service. Before
using it with a released package, confirm that `netops-api-navigator --help`
lists `--transport`; otherwise install a release that includes it or use a
[source checkout](DEVELOPMENT.md#local-setup).

Each stdio process owns its database. For several chats or clients, start one
persistent server and connect all clients to it:

```sh
netops-api-navigator serve --transport streamable-http --port 8765 --read-only
```

Before starting it, set `MCP_HTTP_TOKEN` to a randomly generated secret of at
least 32 printable ASCII characters without spaces. Supply the usual Central
credentials and data paths to the service as needed. Keep its token in a secret
store or protected environment, available to both service and clients.

The service binds to `127.0.0.1` and serves `/mcp`. For Codex, replace the stdio
entry with:

```toml
[mcp_servers.netops_api_navigator]
url = "http://127.0.0.1:8765/mcp"
bearer_token_env_var = "MCP_HTTP_TOKEN"
```

Start the service before clients, optionally through your OS service manager.
All sessions share its credentials, tools, and graph. Closing a client session
does not close the service. This is a local shared-account service, without
per-user authorization or a public remote endpoint. Missing or incorrect bearer
tokens receive HTTP 401.

Stop existing stdio owners gracefully before using their graph with the service.
Do not run both transports against the same database. Verify a graph query from
each client after switching. For independent graphs, give each process a separate
`GRAPH_DB_PATH` in its own directory.

## Troubleshooting

| Symptom | Next step |
|---|---|
| Executable not found | Run `uv tool update-shell`, reopen the terminal/client, or set the client's `command` to the executable's absolute path. `uv tool dir --bin` shows its directory. |
| Windows reports a missing Ladybug C API library | Reinstall with `uv tool install --reinstall --managed-python --python 3.12 --no-build netops-api-navigator`, then run doctor. The Python.org runtime has different OpenSSL DLL names. |
| `doctor` reports an extension error | Check the [extension and upgrade guide](ladybug-extension-upgrade.md). This is an engine/install failure, not a Central credential error. Include the engine version and sanitized diagnostic in an issue. |
| First start cannot obtain a knowledge database | Check GitHub access and the configured release/digest. Run `doctor --skip-credentials` outside the MCP client to see the error. |
| Partial Central credentials | Supply all three Central variables, or unset all three for discovery mode. |
| Token succeeds but an endpoint returns 403 | Check the account-specific base URL and the client's workspace/role permissions. |
| Topology is incomplete | Inspect `graph://seed-status`; successful OAuth does not guarantee access to every topology endpoint. |
| Graph lock held by another process | Stop the other owner, use the shared service, or select a separate graph directory. Do not delete database sidecars. |
| Corruption or incompatible database format | Stop graph owners and follow the [restore procedure](configuration.md#knowledge-database-lifecycle) and [upgrade guide](ladybug-extension-upgrade.md). |

When filing an [issue](https://github.com/tbelz/netops-api-navigator/issues), include
`netops-api-navigator --version`, OS/architecture, and sanitized `doctor` output.
Never include client secrets, bearer tokens, or sensitive inventory.
