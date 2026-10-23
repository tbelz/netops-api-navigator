# Releases and downloads

## Choose a download

| Release family | Purpose | Where to start |
| --- | --- | --- |
| `vMAJOR.MINOR.PATCH` | Installable MCP server | [PyPI](https://pypi.org/project/netops-api-navigator/) or the [latest software release](https://github.com/tbelz/netops-api-navigator/releases/latest) |
| `knowledge-db-*` | Prebuilt API/documentation graph and build caches | Downloaded automatically for new runtime graphs; see [graph startup](USER_GUIDE.md#knowledge-db-startup) |
| `openapi-*` | Self-contained Central API schema exports | [Stable current URLs](openapi-export.md#consumer-interface), or a dated immutable snapshot |

Software releases own the **Latest** designation. Data snapshot dates are
independent of the Python package version; they do not require reinstalling the
server. GitHub lists all three families together. Existing tags and download
URLs remain available.

For a normal installation, use `uv tool install` from the README. The workshop
ZIP contains a VS Code starter, the project wheel, locked constraints and
dependency wheels for Windows x64, macOS Apple Silicon and Linux x64. It avoids
PyPI access after download; acquiring Python can still require network access.
Separate wheel/source downloads support manual installation. `constraints.txt`
records the release dependencies, and `SHA256SUMS` covers the downloadable files.

## Prepare a software release

1. Update the package version in `pyproject.toml` and the README installation
   example. Check the user-facing upgrade instructions, especially graph storage
   compatibility and rollback requirements.
2. Add `docs/releases/<version>.md` with nonempty `## Highlights` and
   `## Upgrade notes` sections. State explicitly when no migration is required.
   Keep the earlier files so the renderer can link to the previous software tag.
3. Preview the complete release body:

   ```sh
   uv run python scripts/render_release_notes.py --tag v0.3.2 --output tmp/release-notes.md
   ```

4. Run `bash scripts/dev_test.sh` and let the PR package checks pass before
   merging. Push the matching `vMAJOR.MINOR.PATCH` tag when ready to publish.

The Python Package workflow validates the tag, version and curated notes before
publishing. It builds one distribution set, locks runtime constraints, assembles
the workshop ZIP and checksums, and smoke-tests the exact wheel on all three
supported platforms with `doctor` and an MCP initialize/tool-list handshake.
It publishes to TestPyPI, verifies file digests and runs the TestPyPI wheel,
then publishes to PyPI through Trusted Publishing. PyPI digests and installations
on all three platforms must pass before the GitHub software release is created.

The rendered notes travel with the build artifact, but are excluded from release
assets and checksums. The pipeline adds version-specific PyPI/install links,
download explanations and a software-tag changelog link. Retrying the release
job uploads the same asset names and reapplies the same title and notes.
PyPI/TestPyPI environments and their Trusted Publishers must remain configured.
Container publication remains a separate workflow.

To repair historical release presentation, render that version's curated notes
with `--version <version> --tag v<version>` and edit the GitHub release body only.
Preserve tags, distribution bytes,
checksums and any storage-upgrade warnings. Editing repository documentation does
not update an already-published PyPI description; that requires a new package
release. Documentation-only cleanup does not itself require a new version tag.

## Automated data publication

The daily workflow runs at 04:00 UTC and can also be dispatched manually. It
retains the existing Knowledge DB health and change gates, artifact names and
runtime selection behavior. Data releases use `make_latest: false`.

For OpenAPI, every successful run validates and publishes the complete current
file set and fresh provenance report to Pages. A dated GitHub snapshot is created
only when the SHA-256 of at least one of the three API files differs from the
last `openapi-*` release. Report dates and workflow URLs alone do not create
another release. The first successful publication creates a snapshot.

Release lookup paginates across the mixed release history. Failed comparison,
missing prior assets or invalid checksums stop publication rather than treating
an error as a content change. Export failure leaves the prior Pages deployment
intact. The current publication can therefore have a newer verification date
than the last immutable snapshot.

Check the Actions run summary/logs for the snapshot decision and the
[public report](https://tbelz.github.io/netops-api-navigator/openapi/latest/export-report.json)
for freshness. Export diagnostics are retained as Actions artifacts. See the
[export policy](openapi-export.md) for source validation and publication checks.

## Repository discovery

The README links the PyPI version badge to the package. The repository's About
website points to PyPI; GitHub Packages does not list external PyPI packages.
Project metadata links back to documentation, source, issues and releases.
