# Ladybug extension distribution and storage upgrade

## Dependency choice

Navigator 0.3.3 pins `ladybug==0.19.1` and bundles the required `fts` and `algo`
extensions for ABI `v0.19.0`. The engine wheel hashes are in `uv.lock`; binary
provenance, sizes and SHA-256 hashes are in
`src/netops_api_navigator/extension_bundle/manifest.json`.

The previous 0.3.2 image pinned 0.17.1, which requested the v0.17.0 extension
ABI. On 2026-11-24, the exact reported image digest
`sha256:b8c929c4364b4c84cbd198d1fa06551deb558d108db6a1e7241800c1b091ac8c`
was reproduced under linux/amd64, without credentials or a volume. Both upstream
extension downloads returned 404. The published knowledge graph already contained
FTS indexes, so startup's seed-script DELETE failed before MCP initialization.
A search fallback to CONTAINS cannot make indexed-table writes safe without FTS.
This repeated the October disappearance of the v0.15.0/v0.16.0 binaries.

The fix removes extension downloads from runtime. Every supported distribution
contains the exact same verified binaries; a local-path LOAD is the only loading
mechanism. A temporary in-memory preflight verifies both libraries before any
persistent graph is opened for writes. Missing, damaged or unloadable extensions
stop startup and doctor with a repair message. No database is reset as recovery.
A verified local knowledge graph is still required for fully offline discovery;
a first knowledge download and connected Central calls still need networking.

Authoritative sources:

- [Official Python distribution](https://pypi.org/project/ladybug/0.19.1/).
- [Official local extension loading](https://docs.ladybugdb.com/extensions/).
- [0.19.1 storage versions](https://github.com/LadybugDB/ladybug/blob/v0.19.1/src/include/storage/storage_version_info.h).
- [Archived extension bundle](https://github.com/tbelz/netops-api-navigator/releases/tag/ladybug-extensions-v0.19.0-r1).

## Distribution and source development

The supported bundle targets are Linux x64 and ARM64 with glibc, macOS ARM64
(macOS 15+ for the pinned engine wheels), and Windows x64. Unsupported targets,
including musl and Windows ARM64, receive an explicit diagnostic. Adding a target
requires corresponding pinned binaries and a real native qualification job.

Wheels, source distributions and the workshop archive include all four targets.
Docker uses the same package resources and installs its dependencies from frozen
lockfile constraints. A build hook rejects incomplete distributions, including
rebuilds of an sdist. The engine and extension versions must be changed together.

For a fresh source checkout, stage the archived bundle before testing or building:

```bash
uv run --no-project python scripts/prepare_extensions.py
uv sync --frozen --extra test
uv run python scripts/smoke_ladybug_extensions.py --directory tmp/extension-smoke
```

The smoke directory must not already exist. The preparation command downloads
only the pinned project archive, checks the archive and individual binaries, and
stages them under the gitignored package resource directories. It does not modify
an existing Ladybug user cache. A pre-downloaded archive can be supplied with
`--archive /path/to/ladybug-extensions-v0.19.0-r1.zip`. Published packages need no
preparation step and perform no extension downloads after installation.

The archive is a snapshot of official upstream binaries, with the upstream license
included in the package. Never replace an archived asset in place. Any change to a
binary requires a new archive revision, updated checksums, review and all platform
gates. Existing cached libraries on a maintainer's machine are not qualification.

## Required deployment and rollback procedure

**A normal writable open with 0.19.1 upgrades older supported graphs to storage
version 43.** This includes Navigator startup and doctor. The cloud `READ_ONLY`
setting does not make local databases read-only. The previous 0.17.1 engine uses
storage version 41 and cannot read a graph upgraded to 43.

1. Stop every owner of the graph, including shared HTTP services and standalone
   clients, before the first new-engine start.
2. Preserve a consistent, protected copy of the complete graph directory: graph,
   manifest, WAL and shadow sidecars. Include compiler/AST files stored elsewhere.
   Never copy an actively written database; keep corruption evidence separately.
3. Install the matching new distribution. Run doctor, graph and FTS queries, then
   the actual client handshake. A shared HTTP deployment still has one graph owner.
4. To roll back, stop the new owner and preserve its complete directory separately.
   Restore the entire pre-upgrade directory with matching manifests and sidecars,
   then start the old runtime. Changes made after the backup are not in that backup.
   Do not point 0.17.1 at a version-43 graph or mix files from different snapshots.

Rollback restores the old data format. It does not restore the retired upstream
extension service: an old runtime still requires its original matching extension
cache to perform indexed writes. Keep that cache with the rollback environment.

New builds publish `knowledge-lbug43-*` snapshots. The old `knowledge-db-*`
release family is preserved, so existing clients cannot select an incompatible
format-43 snapshot. New clients prefer the new family and can bootstrap from
legacy snapshots until a new snapshot exists. Explicit KNOWLEDGE_RELEASE_TAG
pins remain authoritative. The manifest records engine, storage and extension ABI
versions; persisted compiler-cache identities include all three. Older clients
should be upgraded: their release-list window is bounded and their extension
server dependency remains broken.

## Compatibility qualification

The regression gates cover real FTS queries and PageRank, two graph open/close
cycles, indexed seed synchronization, preservation of user scripts and run metadata,
and rejection of missing or corrupt libraries before graph or sidecar changes.
Native CI runs the exact packaged wheel on Windows x64, macOS ARM64 and Linux x64.
Docker CI tests both Linux architectures with `--network none`, including MCP
initialize/tools-list, FTS, algo and a second server startup. Failed handshakes
are failures, not warnings. Full compiler and hydration smoke gates remain enabled.

Local qualification on 2026-11-24 used disposable copies only:

- Storage-40 legacy graph: 1,863 ApiEndpoint nodes retained; real FTS/PageRank and
  Script CREATE/DELETE passed before and after reopening under 0.19.1.
- `knowledge-db-20261118-043523` storage-41 graph: 1,931 ApiEndpoint nodes retained
  over two complete server lifecycles; FTS for alerts returned 13 matches and a
  user script with run metadata survived seed synchronization.
- The storage-41 backup SHA-256 was
  `bc92fe72703f7033430d77f3df9869d158f47e2c55109c2cc69163e32e9cd9dd`.
  Its source bytes were unchanged while the writable copy became storage 43.

A connected Central check additionally requires valid credentials. Local credentials
found during this investigation returned OAuth HTTP 401, so offline qualification
does not claim connected API validation. Do not paste secrets into logs or issues.
