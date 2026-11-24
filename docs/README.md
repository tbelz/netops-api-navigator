# Documentation

Start with the [project README](../README.md) to install Navigator and try your
first API discovery request.

## Use Navigator

| Task | Guide |
|---|---|
| Install, connect a client or Central account, use Docker, troubleshoot startup | [Getting started](getting-started.md) |
| Select profiles, credentials, paths, knowledge snapshots, and optional features | [Configuration](configuration.md) |
| Look up MCP tools, resources, prompts, and the discovery sequence | [MCP reference](mcp-reference.md) |
| Download Central OpenAPI definitions for an explorer | [OpenAPI exports](openapi-export.md#consumer-interface) |
| Set up the guided VS Code environment | [Workshop starter](../workshop/README.md) |
| Upgrade an existing database or plan a rollback | [Ladybug upgrade](ladybug-extension-upgrade.md#required-deployment-and-rollback-procedure) |

## Develop and maintain

| Task | Guide |
|---|---|
| Understand components, data flow, and database ownership | [Technical overview](TECHNICAL_OVERVIEW.md) |
| Change the code, work on graph ingestion, and run tests | [Development](DEVELOPMENT.md) |
| Release packages or investigate workflow alerts | [Maintenance](maintenance.md) |
| Understand OpenAPI transformation and publication | [Export implementation](openapi-export.md#transformation-boundary) |

## Design and investigation records

These explain decisions, proposals, or investigations. They are not the reference
for which features a particular installed release exposes.

- [Architecture decisions](adr/)
- [Compiler tooling proposal](compiler-mcp-tooling-proposal.md)
- [Runtime hydration roadmap](runtime-hydration-roadmap.md)
- [Graph restart investigation](graph-restart-investigation.md)

Documentation describes this source revision unless a release is named. Check
`netops-api-navigator --version` and the relevant
[release notes](https://github.com/tbelz/netops-api-navigator/releases) when using
an installed package. Published packages and the `main` container can differ.
