# Maintenance

[Documentation index](README.md) · [Development](DEVELOPMENT.md)

This guide is for repository maintainers managing publication and workflow
alerts. Users installing the server should start with [Getting started](getting-started.md).

## Workflows

| Workflow | Responsibility |
|---|---|
| [Tests](../.github/workflows/tests.yml) | Repository test gate |
| [Python Package](../.github/workflows/python-package.yml) | Build, native package checks, and tagged Python releases |
| [Build and Push Container Image](../.github/workflows/build-and-push.yml) | Container build and publication |
| [Update Knowledge Database](../.github/workflows/update-knowledge-db.yml) | Scrape API documentation, build knowledge artifacts, and publish validated OpenAPI exports |
| [Workflow Health](../.github/workflows/workflow-health.yml) | Check production workflow and publication health, maintain alert issues |

Inspect runs in [GitHub Actions](https://github.com/tbelz/netops-api-navigator/actions).
Workflow YAML and the associated scripts define the executable behavior.

## Knowledge and OpenAPI publication

The knowledge workflow is scheduled daily at 04:00 UTC and supports manual
execution. GitHub may start scheduled jobs later. The build checks source health,
graph invariants, and compiler coverage before publishing knowledge artifacts.
Incomplete compilation, provider failures, and major endpoint loss block
knowledge publication.

Validated Central OpenAPI exports are published separately from the raw spec
archives and graph artifacts. A failed export leaves the previous public files
in place. The export report records source time, provenance, corrections, and
checksums; its date describes the scrape, not guaranteed product freshness.
See [OpenAPI publication](openapi-export.md#publication) for the transformation
contract, Pages configuration, and verification commands.

## Workflow health and notifications

The health monitor checks production workflows, release completeness, compiler
coverage, and the public OpenAPI files' integrity/accessibility. It runs after
relevant non-PR workflow completions, every six hours, and on manual dispatch.
A successful refresh/public schema update more than 36 hours overdue is reported
as stale.

Find current findings in
[health alert issues](https://github.com/tbelz/netops-api-navigator/issues?q=is%3Aissue+is%3Aopen+label%3Aworkflow-health-alert).
The monitor assigns and mentions the repository owner by default; set repository
variable `WORKFLOW_ALERT_ASSIGNEE` to override the recipient. Changed findings and
recovery update the issue; unchanged findings do not produce repeated comments.

Optional VSG failures and compiler/legacy parity gaps are non-blocking warnings.
They remain visible in the issue and run summary without failing the monitor or
sending repeated workflow-failure emails. A successful monitor run means there
are no blocking errors; it can coexist with an open warning issue. Actual workflow
failures, stale/invalid exports, incomplete compilation, and monitor errors still
fail the health check. Classification lives in
[monitor_workflow_health.py](../scripts/monitor_workflow_health.py) and
[check_knowledge_health.py](../scripts/check_knowledge_health.py).

For email delivery, enable **Email** for **Participating, @mentions and custom**
in [GitHub notification settings](https://github.com/settings/notifications).
Native Actions notifications have separate email/on-GitHub settings, including
failure-only notifications. Scheduled-run notifications follow GitHub's responsible
actor rules; the assigned alert issue targets the configured maintainer.
Repository workflows cannot control account email preferences or delivery, and
an Actions-based monitor cannot notify during a GitHub-wide Actions outage.

When investigating, inspect the alert's failed check and linked run first. For
knowledge/export failures, inspect retained diagnostics and the release/report
from that run before retrying publication. Do not interpret a cached successful
export as proof that the newest scrape succeeded.

## Python releases

The package workflow builds a wheel, source distribution, locked runtime
constraints, checksums, dependency wheelhouses, and a VS Code workshop archive.
It smoke-tests the installed wheel on Windows x64, macOS Apple Silicon, and Linux,
including `doctor` and an MCP stdio initialize/tool-list handshake.

A `v*` version tag enters the publication chain: TestPyPI publication and
verification, PyPI publication and digest verification, a PyPI installation smoke,
and creation/repair of the GitHub release. The workflow uses Trusted Publishing;
configure the corresponding PyPI/TestPyPI projects, repository environments, and
publisher identities before a first release. Inspect the workflow for the exact
required gates and permissions.

Before tagging, verify that package metadata and the package's reported version
agree, the supported-platform checks pass, and any storage migration has a tested
backup/rollback procedure. Keep version-specific fixes and migration details in
release notes and the appropriate guide. The README describes the entry path.

The workshop builder renders the packaged wheel filename into its MCP template.
Changes to the pinned knowledge release/digest belong in that template, with a
matching artifact check. Its accompanying README must work in the extracted
archive as well as in the repository.

## Documentation maintenance

The root README owns the brief project introduction and first-run example.
Use the [documentation index](README.md) to choose the existing guide for detailed
changes, and follow the five documentation rules in [AGENTS.md](../AGENTS.md#documentation).
Update the affected explanation rather than appending a task history. Preserve
useful links when moving content; mark proposals as proposals and keep release
availability distinct from the current source tree.
