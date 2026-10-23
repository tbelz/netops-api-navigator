# NetOps API Navigator

[![PyPI](https://img.shields.io/pypi/v/netops-api-navigator)](https://pypi.org/project/netops-api-navigator/)
[![CI](https://github.com/tbelz/netops-api-navigator/actions/workflows/tests.yml/badge.svg)](https://github.com/tbelz/netops-api-navigator/actions/workflows/tests.yml)
[![Python 3.12+](https://img.shields.io/badge/python-3.12%2B-blue.svg)](https://www.python.org/)
[![License: MIT](https://img.shields.io/badge/License-MIT-yellow.svg)](https://github.com/tbelz/netops-api-navigator/blob/main/LICENSE)

An MCP server for **HPE Aruba Networking Central** and **HPE GreenLake Platform**.
Give your MCP client a searchable API graph, live API access and reusable Python
scripts for network automation.

## What it provides

- **API discovery:** search endpoints, schemas and YANG/CLI references in a
  prebuilt graph, including without Central credentials.
- **Live API calls:** authenticate to Central and GreenLake, with graph-backed
  validation before requests.
- **Network context:** query sites, devices, groups and network topology.
- **Reusable automation:** create and run Python scripts, or use the restricted
  GET-only workshop profile for guided onboarding.

<a id="quick-start"></a>
<a id="native-python-installation-windows-x64-apple-silicon-and-linux-x64"></a>
<a id="before-upgrading-an-existing-ladybug-graph"></a>

## Install

Requires [uv](https://docs.astral.sh/uv/getting-started/installation/) and
Python 3.12 or newer. `uv` can install Python 3.12 for you. Release installation
is tested on Windows x64, macOS Apple Silicon and Linux x64.

**Upgrading an existing graph?** Back up its complete directory, manifest and
sidecars first. Version 0.3.2 upgrades Ladybug storage format 40 to 41 on its
first writable open, including `doctor`. The old engine cannot reopen it;
rollback requires restoring the backup. Follow the
[upgrade procedure](https://github.com/tbelz/netops-api-navigator/blob/main/docs/ladybug-extension-upgrade.md).

```text
uv tool install --python 3.12 --no-build netops-api-navigator==0.3.2
netops-api-navigator doctor --profile workshop --skip-credentials
```

`doctor` checks the installation without Central credentials. On first start,
the server downloads its knowledge snapshot from GitHub. Existing graphs are
reused; network access to GitHub is needed for the initial snapshot.

For a prepared VS Code setup, download the workshop ZIP from the
[latest software release](https://github.com/tbelz/netops-api-navigator/releases/latest)
and follow the
[workshop guide](https://github.com/tbelz/netops-api-navigator/blob/main/workshop/README.md).
It includes dependency wheels for the supported platforms.

<a id="discovery-only"></a>

## Connect an MCP client

For clients using `mcpServers`, add this native STDIO configuration:

```json
{
  "mcpServers": {
    "netops-api-navigator": {
      "command": "netops-api-navigator",
      "args": ["--profile", "workshop"]
    }
  }
}
```

Ensure the client can find the command on its `PATH`. Without credentials, this
profile exposes API and graph discovery. Try: “Find the Central API for listing
sites and show its required parameters.”

For live Central access, supply `CENTRAL_BASE_URL`, `CENTRAL_CLIENT_ID` and
`CENTRAL_CLIENT_SECRET` through the client's environment or secret inputs.
Copy your account's exact Base URL from **Central → Menu → API Gateway → REST API**.
The workshop profile permits Central GET requests and trusted topology seeds;
it disables user-authored scripts, direct graph writes and GreenLake tools.

See the
[user guide](https://github.com/tbelz/netops-api-navigator/blob/main/docs/USER_GUIDE.md)
for connected mode, Docker, other profiles and multiple local chats sharing one
graph. Full mode permits mutations and Python script execution; use it with
trusted clients and credentials appropriate to the task.

<a id="configuration"></a>
<a id="runtime-modes"></a>
<a id="connected"></a><a id="read-only"></a><a id="workshop"></a>
<a id="discovery-only-docker-profile"></a><a id="connected-docker-profile"></a>
<a id="multiple-local-chats-sharing-one-graph"></a><a id="knowledge-db-startup"></a>

## Documentation

| Guide | Contents |
| --- | --- |
| [User guide](https://github.com/tbelz/netops-api-navigator/blob/main/docs/USER_GUIDE.md) | Installation, Docker, configuration, profiles, shared HTTP service and graph backups |
| [Workshop](https://github.com/tbelz/netops-api-navigator/blob/main/workshop/README.md) | Prepared VS Code setup with local credential prompts |
| [Tool reference](https://github.com/tbelz/netops-api-navigator/blob/main/docs/TECHNICAL_OVERVIEW.md#tool-surface) | Discovery, live API, script and optional diagnostic tools |
| [Technical overview](https://github.com/tbelz/netops-api-navigator/blob/main/docs/TECHNICAL_OVERVIEW.md) | Architecture, data model and component contracts |
| [Development](https://github.com/tbelz/netops-api-navigator/blob/main/docs/DEVELOPMENT.md) | Local setup, test commands and build pipeline |
| [Release process](https://github.com/tbelz/netops-api-navigator/blob/main/docs/RELEASING.md) | Software releases, downloads and automated data snapshots |

<a id="openapi-files-for-explorers-and-api-tools"></a>

## OpenAPI downloads

Use the validated
[Central OpenAPI JSON](https://tbelz.github.io/netops-api-navigator/openapi/latest/central-openapi.json)
in an OpenAPI 3.1-compatible explorer without installing the MCP server.
[Monitoring and configuration subsets](https://github.com/tbelz/netops-api-navigator/blob/main/docs/openapi-export.md#consumer-interface)
are also available. These exports cover Central; GreenLake is not included.

The pipeline checks upstream documentation daily. See the
[publication report](https://tbelz.github.io/netops-api-navigator/openapi/latest/export-report.json)
for source freshness and checksums. Failed validation leaves the last successful
publication available.

<a id="python-releases"></a>

## Releases

Install software from [PyPI](https://pypi.org/project/netops-api-navigator/).
[Software releases](https://github.com/tbelz/netops-api-navigator/releases/latest)
include upgrade notes, the workshop ZIP, Python distributions and checksums.

The same repository also hosts automated `knowledge-db-*` graph snapshots and
`openapi-*` schema snapshots. They are data updates with their own dates;
software releases use `v*` tags and own GitHub's **Latest** designation.
See the [release guide](https://github.com/tbelz/netops-api-navigator/blob/main/docs/RELEASING.md)
for download choices and publication details.

<a id="architecture"></a><a id="tool-surface"></a>
<a id="graph-and-discovery-tools"></a><a id="live-api-tools"></a><a id="script-tools"></a>
<a id="compiler-tools"></a><a id="runtime-hydration-tools"></a>
<a id="recommended-discovery-flow"></a><a id="compiler--v2-smoke"></a>
<a id="runtime-hydration-foundation"></a>

Detailed architecture, tool contracts and experimental features are documented
in the [technical reference](https://github.com/tbelz/netops-api-navigator/blob/main/docs/TECHNICAL_OVERVIEW.md#tool-surface)
and [runtime profiles](https://github.com/tbelz/netops-api-navigator/blob/main/docs/USER_GUIDE.md#runtime-modes).

## Development

Use `uv sync` to set up a checkout and `uv run netops-api-navigator` to run it.
Start with the [development guide](https://github.com/tbelz/netops-api-navigator/blob/main/docs/DEVELOPMENT.md)
for tests and contributions, and [SECURITY.md](https://github.com/tbelz/netops-api-navigator/blob/main/SECURITY.md)
for reporting vulnerabilities.

## License

[MIT](https://github.com/tbelz/netops-api-navigator/blob/main/LICENSE).

NetOps API Navigator is an independent community project. It is not affiliated
with, sponsored by, endorsed by, or supported by Hewlett Packard Enterprise.
HPE Aruba Networking Central and HPE GreenLake Platform are referenced solely
to describe compatibility. All trademarks belong to their respective owners.
