# NetOps API Navigator

An MCP server that connects your AI assistant to **HPE Aruba Networking Central**
and the **HPE GreenLake Platform**. Explore APIs, inspect your network, and build
reusable automation from your MCP client.

Independent community project; not affiliated with, endorsed by, or supported by
Hewlett Packard Enterprise.

## What you can do

- **Find the right API:** search endpoints and inspect parameters, payloads, and
  relationships between API schemas, YANG paths, and CLI commands.
- **Query your network:** use authenticated Central and GreenLake API calls with
  validation against the API catalog.
- **Explore topology:** inspect sites, devices, groups, and links populated from
  your Central environment.
- **Reuse automation:** save Python scripts and, in connected mode, run them with
  authenticated API helpers.

API discovery works without Central credentials. Live requests, topology
synchronization, and script execution require a connection to your environment.

## Quickstart

You need [uv](https://docs.astral.sh/uv/getting-started/installation/) and an MCP
client that can run a local stdio server. The native installation targets Windows
x64, macOS 15+ on Apple Silicon, and Linux x64/ARM64 with glibc.
Docker and WSL are not required.

Already using Navigator? Read the [upgrade instructions](docs/ladybug-extension-upgrade.md#required-deployment-and-rollback-procedure)
before running a newer version against an existing database.

### 1. Install and check

```sh
uv tool install --python 3.12 --no-build netops-api-navigator
netops-api-navigator doctor --skip-credentials
```

`uv` can install Python 3.12 if needed. The first check downloads the API knowledge
database from GitHub; allow it to finish before starting your MCP client. A
successful check reports `"status": "ready"` and `"graph_available": true`.
No Central account is needed for this first run; leave Central credentials unset.

If the command is not found or the check fails, see
[installation troubleshooting](docs/getting-started.md#troubleshooting).

### 2. Add the server to your MCP client

For clients using the `mcpServers` JSON format, add this server entry to their MCP
configuration:

```json
{
  "mcpServers": {
    "netops-api-navigator": {
      "command": "netops-api-navigator",
      "args": ["--read-only"]
    }
  }
}
```

Start or reload the server in your client. For VS Code and Codex configuration,
see [client setup](docs/getting-started.md#mcp-clients). Use the
[shared local service](docs/getting-started.md#multiple-local-clients) if several
clients or chats need the same database.

### 3. Try a request

> Find the Central API for listing sites. Show its method, path, and required
> parameters without making a live request.

The assistant should search the API catalog and return an endpoint with its
schema details. You can also ask it to call `get_server_status` to check the
connection. In discovery mode, `central_connected` is `false`.

## Connect your network

Provide your Central **API base URL**, **client ID**, and **client secret** through
your client's environment or secret inputs. Follow the
[connection guide](docs/getting-started.md#connect-to-central) for a complete
example and optional GreenLake credentials.

Keep `--read-only` for initial live exploration. It blocks mutating cloud API
requests; local script and graph editing remain available. The
[workshop profile](docs/configuration.md#profiles-and-connectivity) also removes
user-authored script execution and direct graph writes.

Once connected, try: “List my sites and summarize their devices.”

## Documentation

| I want to… | Read |
|---|---|
| Set up a client, use Docker, or share a local server | [Getting started](docs/getting-started.md) |
| Choose a profile or configure the server | [Configuration](docs/configuration.md) |
| Look up tools and resources | [MCP reference](docs/mcp-reference.md) |
| Import Central APIs into an OpenAPI explorer | [OpenAPI downloads](docs/openapi-export.md#consumer-interface) |
| Run the guided VS Code workshop | [Workshop starter](workshop/README.md) |

See the [documentation index](docs/README.md) for architecture, development, and
maintenance guides.

## Help and contributions

Report problems in [GitHub Issues](https://github.com/tbelz/netops-api-navigator/issues).
Include your platform, installed version, and sanitized `doctor` output.
For code changes and local checks, read the [development guide](docs/DEVELOPMENT.md).

## License

[MIT](LICENSE). HPE product names describe compatibility; trademarks belong to
their respective owners.
