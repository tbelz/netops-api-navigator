# Central OpenAPI export

## Consumer interface

The current publication lives at
`https://tbelz.github.io/netops-api-navigator/openapi/latest/`:

- `central-openapi.json`: combined Central API, preferring confirmed stable successors.
- `central-monitoring-openapi.json`: the MRT source, including token generation.
- `central-config-openapi.json`: the Config source.
- `export-report.json`: source timestamp and health, file provenance, correction
  records, workflow/commit provenance, validation results and SHA-256 checksums.

Import JSON directly or supply its URL to an OpenAPI 3.1-capable explorer. GitHub
Pages serves the files publicly with JSON content type and cross-origin access.
The exporter does not call the APIs. Running requests from an explorer requires
the appropriate API credentials and permissions; upstream API CORS behavior is
separate from loading these public schema files.

The three definitions are self-contained; internal `$ref`s are retained, including
cycles. This avoids expanding repeated or recursive data models. The two area
exports partition the operations in the combined export. Output order and schema
bytes are deterministic for identical source inputs and exporter behavior.
`info.version` fingerprints source inputs and the selection policy; it is not an
upstream API version.
Original document metadata is available in each operation's `x-source-document`.

## Stable API preference

Exports omit a deprecated alpha operation only when all of these checks pass:

- The same scrape contains a non-deprecated stable operation with the same HTTP
  method and resource path, changing only a version segment such as `v1alpha1` to `v1`.
- Both operations belong to the same Central source area and use the same servers.
- The alpha description explicitly says to use that stable path instead.

The stable definition is preserved unchanged, including its own parameters,
authentication, examples and responses. Paths are never upgraded by renaming.
Alpha operations without a confirmed successor remain available. Renamed resources,
different major versions and ambiguous replacements are retained for explicit review.

The report lists `selection_policy` and, for each output, `source_operations`,
`superseded_alpha_operations` and `replacements` with both source files, paths and
operation IDs. Every input is validated before this selection, including replaced
alpha inputs. Output coverage must equal input coverage minus exactly those recorded
replacements. A retained reference or link to a removed operation blocks publication.

The scrape date describes when HPE's public documentation was retrieved. It does
not certify that those upstream documents reflect the latest product release.

## Transformation boundary

This exporter is independent of the existing compiler frontend and consumes the
same run's raw Central cache. The original cache and graph artifacts are not
modified. The exporter:

1. Checks that MRT and Config are fresh and successful, with file counts matching
   the manifest. Cached fallbacks, HTTP failures and discovery errors block it.
   Pages without an embedded OpenAPI document are explicitly accounted for in
   the source health report.
2. Corrects unambiguous primitive default types at schema positions only. It
   preserves example/default payload objects, vendor extensions and property
   names such as `_id`. Every correction is reported.
3. Converts OpenAPI 3.0 nullable and exclusive-bound semantics to OpenAPI 3.1.
   A dangling root security requirement is removed only when every operation
   explicitly overrides it; effective authentication is never guessed.
4. Namespaces components per source and rewrites typed references, security
   requirements and discriminator mappings. It deduplicates identical definitions
   after their references have been scoped, repeating bottom-up as needed.
   Cyclic equivalents can remain separate. Distinct credential names remain
   distinct, even when their security-scheme bodies match.
5. Preserves effective operation servers, security and parameter overrides.
   Generates a deterministic operation ID only when the source omitted one.
6. Selects confirmed stable successors using the policy above, then checks all local
   references, operation coverage after recorded replacements and OpenAPI validity
   before writing any candidate publication.

Unknown reference scopes, external references, ambiguous discriminators,
conflicting operations/operation IDs, and invalid documents fail the export.
There is no partial-publication mode. A source document is never silently omitted.
No remote validator or resolver is needed to construct the files.

## Local verification

Install the repository's compiler/test dependencies, then hydrate the real cache:

```sh
uv sync --frozen --extra test
bash scripts/hydrate_test_fixtures.sh
```

Download `manifest.json` from the same `knowledge-db-*` release as the cache into
`tmp/openapi-manifest.json`. Use a **new** output directory for each attempt:

```sh
uv run python scripts/build_openapi_export.py \
  --cache-dir tmp/test_fixtures/central_spec_cache \
  --manifest tmp/openapi-manifest.json \
  --output-dir tmp/openapi-candidate \
  --diagnostics tmp/openapi-diagnostics.json

uv run pytest tests/test_openapi_export.py -m 'not real_spec'
uv run pytest tests/test_openapi_export.py -m real_spec
```

All outputs are validated in memory before a staged directory is promoted to
the requested candidate path. Existing output directories are refused, so an
unsuccessful local attempt cannot overwrite an earlier publication. Diagnostics
are written separately and include the error on failure.

## Publication

The existing daily Knowledge DB workflow passes its raw cache and manifest to a
separate exporter job. That job uploads the full validated file set only on
success; diagnostics are retained for failed runs too. The publishing job has an
explicit dependency on exporter success and runs only for `main`.

GitHub Pages must be enabled with **GitHub Actions** as the build source. The
`github-pages` environment is restricted to `main`. Publication requires the
standard workflow permissions `contents: write`, `pages: write` and
`id-token: write`; it requires no additional secret or external hosting account.

Each successful run creates an immutable
`openapi-YYYYMMDD-RUN_ID-RUN_ATTEMPT` release with `make_latest: false`, then deploys
the complete Pages artifact. Serializing the daily workflow prevents overlapping
runs from replacing a newer publication with an older one. No changes to the
existing knowledge release selection or raw archive format are required.

The publishing job checks all public files, their JSON content type, CORS headers
and checksums. It retries briefly for Pages/CDN propagation. A failed exporter
leaves the last Pages deployment intact. Inspect Actions and the retained
diagnostics artifact when a publication stops updating.

After enabling Pages and merging the workflow, dispatch **Update Knowledge
Database** on `main` for the first publication. Verify public access with:

```sh
uv run --no-project python scripts/verify_openapi_publication.py \
  --base-url https://tbelz.github.io/netops-api-navigator/openapi/latest/ \
  --report tmp/openapi-candidate/export-report.json
```

The local report must come from the same run as the published files. A schema
import does not prove that live API calls will succeed; the explorer smoke check
loads definitions and representative operations without invoking API endpoints.
