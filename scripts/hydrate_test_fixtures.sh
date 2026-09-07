#!/usr/bin/env bash
# Download and extract the real Central OpenAPI spec cache for local
# testing. Reads the latest knowledge-db release artifact published by
# the .github/workflows/update-knowledge-db.yml pipeline.
#
# Requirements: `gh` CLI authenticated against this repo.
#
# After running, tests marked @pytest.mark.real_spec pick up the cache
# via the real_central_specs fixture in tests/conftest.py.

set -euo pipefail

cd "$(dirname "$0")/.."

DEST="tmp/test_fixtures"
mkdir -p "$DEST"

if ! command -v gh >/dev/null 2>&1; then
    echo "ERROR: gh CLI is required. Install from https://cli.github.com/" >&2
    exit 1
fi

TAG=$(gh release list --limit 100 --json tagName -q '[.[] | select(.tagName | startswith("knowledge-db-"))][0].tagName // ""')
if [ -z "$TAG" ]; then
    echo "ERROR: no knowledge-db-* release found." >&2
    exit 1
fi

echo "Downloading central-spec-cache.tar.gz from $TAG..."
if ! gh release download "$TAG" --pattern central-spec-cache.tar.gz --dir "$DEST" --clobber 2>/dev/null; then
    echo "ERROR: release $TAG does not contain central-spec-cache.tar.gz yet." >&2
    echo "       Either wait for the next scheduled knowledge-db build, or" >&2
    echo "       point CENTRAL_SPEC_CACHE at a local spec directory." >&2
    exit 1
fi

rm -rf "$DEST/central_spec_cache"
mkdir -p "$DEST/central_spec_cache"
tar xzf "$DEST/central-spec-cache.tar.gz" -C "$DEST/central_spec_cache" --strip-components=1
COUNT=$(find "$DEST/central_spec_cache" -name '*.json' | wc -l)
echo "Hydrated $DEST/central_spec_cache/ with $COUNT spec files."
