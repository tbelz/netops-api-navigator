# Ladybug extension distribution and storage upgrade

## Dependency choice

Navigator pins the official `ladybug==0.17.1` Python distribution and imports
`ladybug`. Its wheel/source hashes are recorded in `uv.lock`. This replaces
`real-ladybug==0.15.3`; the old distribution has no later releases.

Authoritative sources:

- [Current Python installation instructions](https://docs.ladybugdb.com/installation/)
  name the `ladybug` distribution.
- [Official PyPI release](https://pypi.org/project/ladybug/0.17.1/) links to the
  Ladybug project and its Python binding repository.
- [Official extension installation](https://docs.ladybugdb.com/extensions/)
  uses `INSTALL` followed by `LOAD EXTENSION`; Navigator continues using that
  mechanism, without a third-party mirror or manually substituted binaries.
- [0.17.1 storage-version declarations](https://github.com/LadybugDB/ladybug/blob/v0.17.1/src/include/storage/storage_version_info.h)
  recognize storage version 40 and introduce version 41.

On 2026-10-15, clean `real-ladybug` 0.15.3 installs failed because the official
`v0.15.0/<platform>/fts/libfts.lbug_extension` and algo artifacts returned 404.
A clean `ladybug` 0.15.4 install still requested the same unavailable extension
ABI. The v0.16.0 artifacts also returned 404. The official v0.17.0 extension ABI
used by 0.17.1 was available. The patch release 0.17.1 is pinned rather than
allowing an unqualified upgrade to the latest engine.

The documented `ghcr.io/ladybugdb/extension-repo` image was considered. Its
registry exposed only a `latest` tag. Inspecting its manifest established its
identity, but a bounded archive download did not complete, so no compatibility
claim or binary reuse is made for that image. Cached old libraries on a developer
machine are not a fresh-install qualification.

## Required deployment and rollback procedure

**A normal writable open of a version-40 database upgrades it to version 41,
even without an explicit query that changes data.** This includes Navigator
startup and `doctor`. Navigator's `READ_ONLY` setting governs cloud API writes;
it does not make its local database read-only. A direct engine read-only open
retained version 40 in the isolated probe, but Navigator does not use that mode.

1. Gracefully stop every owner of the graph before the first new-engine start.
2. Preserve a consistent, protected copy of the entire pre-upgrade graph directory,
   including its manifest and any WAL/shadow sidecars. Preserve the original
   corruption evidence separately. Include separate compiler/AST graph files if
   configured outside that directory. Do not copy an actively written database.
3. Start the new runtime against the intended persistent graph, and validate
   graph queries, FTS, and the actual native clients. Shared HTTP deployments
   must still have exactly one service owning that graph.
4. To roll back, stop the new owner and preserve its complete directory separately.
   Restore the entire consistent pre-upgrade directory and matching manifests/
   sidecars, then start the old runtime. Do not point 0.15.3 at an upgraded graph,
   mix sidecars from different snapshots, or overwrite an active owner. Changes
   made after the backup are not present in a restored backup.

The old engine rejects version 41 with a database-version mismatch. No automatic
reset, download-over-existing-graph, or downgrade is introduced by this change.

## Compatibility qualification

Isolated macOS ARM64 probes used a copy of the verified
`knowledge-db-20261015-043156` legacy graph. Its original SHA-256 remained
`7bbd799162e5143d6d40ecfac674c339b091b923e52310ef91f797106413e352`.
The source was never opened for writes. With an empty extension cache:

- The engine and the full Navigator factory opened the old knowledge graph.
- Schema inspection and queries retained all 1,863 `ApiEndpoint` rows.
- Real FTS returned 21 matches for `alerts notifications`.
- A PageRank projection exercised the algo extension on a disposable sentinel.
- Two Navigator factory/close cycles retained the sentinel and endpoint count.
- A normal writable open alone changed the copied file header from 40 to 41;
  the old engine then refused it. A separate read-only open kept version 40.
- The existing concurrent HTTP session test passed with the pinned engine,
  including disconnect/reconnect, shutdown, and persisted-data reopening.

The compiler's existing `index` property must be quoted in DDL and Cypher
expressions because it is reserved by this engine. Names and graph semantics
remain unchanged. Existing compiler traversal/build tests cover those paths.

`scripts/smoke_ladybug_extensions.py` adds a disposable, empty-cache FTS and
PageRank check to the exact-wheel native CI on Windows, macOS, and Linux. It
checks both initial use and reopening. Docker's existing pinned-knowledge,
schema, FTS and compiler smoke gates remain required and unweakened. These
checks qualify their tested artifacts/platforms, not every historical user graph
or future availability of the upstream extension server.
