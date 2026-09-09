#!/usr/bin/env bash
# Path-aware test selector. Runs the tests relevant to changed files.
#
# Strategy:
#   1. Diff working tree + index + commits ahead of origin/main.
#   2. Map source paths -> test patterns (best-effort heuristic).
#   3. Run that subset (excluding slow / live / real_spec by default).
#   4. If no mapping matches, fall back to scripts/dev_test.sh.
#
# Override the test base set:
#   TEST_MARKERS="not slow and not live_api" scripts/test_changed.sh

set -euo pipefail

cd "$(dirname "$0")/.."

DEFAULT_MARKERS="not slow and not real_spec and not integration and not live_api"
MARKERS="${TEST_MARKERS:-$DEFAULT_MARKERS}"

# Collect changed files vs origin/main (fall back to HEAD if no remote)
if git rev-parse --verify --quiet origin/main >/dev/null; then
    BASE="origin/main"
else
    BASE="HEAD"
fi

CHANGED=$(git diff --name-only "$BASE"...HEAD 2>/dev/null || true)
CHANGED="$CHANGED
$(git diff --name-only 2>/dev/null || true)
$(git diff --cached --name-only 2>/dev/null || true)"
CHANGED=$(echo "$CHANGED" | sort -u | grep -v '^$' || true)

if [ -z "$CHANGED" ]; then
    echo "No changed files detected; running default fast loop."
    exec scripts/dev_test.sh
fi

echo "Changed files:"
echo "$CHANGED" | sed 's/^/  /'
echo

# Path -> test glob mappings. Add new mappings as the codebase grows.
declare -a TESTS=()

add_tests() {
    local pattern="$1"
    if compgen -G "$pattern" >/dev/null 2>&1; then
        for f in $pattern; do
            TESTS+=("$f")
        done
    fi
}

while IFS= read -r f; do
    case "$f" in
        src/netops_api_navigator/compiler/*)
            add_tests "tests/test_compiler_*.py"
            add_tests "tests/test_projection_parity.py"
            ;;
        src/netops_api_navigator/graph/*)
            add_tests "tests/test_query_graph.py"
            add_tests "tests/test_schema_*.py"
            add_tests "tests/test_seed_integration.py"
            ;;
        src/netops_api_navigator/tools/api_call*.py | src/netops_api_navigator/_http_core.py)
            add_tests "tests/test_api_call_*.py"
            ;;
        src/netops_api_navigator/tools/scripts*.py)
            add_tests "tests/test_scripts_tool.py"
            add_tests "tests/test_execution.py"
            ;;
        src/netops_api_navigator/oas_*.py | src/netops_api_navigator/api_tree.py)
            add_tests "tests/test_oas_normalize.py"
            add_tests "tests/test_api_tree.py"
            add_tests "tests/test_endpoint_catalog_resource.py"
            ;;
        src/netops_api_navigator/knowledge_db.py)
            add_tests "tests/test_knowledge_db.py"
            ;;
        scripts/build_knowledge_db.py)
            add_tests "tests/test_knowledge_db.py"
            add_tests "tests/test_build_knowledge_db_ast.py"
            ;;
        scripts/report_compiler_traversal.py)
            add_tests "tests/test_compiler_traversal_report.py"
            ;;
        scripts/build_workshop_bundle.py)
            add_tests "tests/test_build_workshop_bundle.py"
            ;;
        scripts/verify_pypi_release.py)
            add_tests "tests/test_verify_pypi_release.py"
            ;;
        src/netops_api_navigator/central_client.py)
            add_tests "tests/test_central_client.py"
            add_tests "tests/test_paginate.py"
            ;;
        src/netops_api_navigator/server.py | src/netops_api_navigator/instructions.py)
            add_tests "tests/test_server_imports.py"
            add_tests "tests/test_instructions_catalog.py"
            ;;
        src/netops_api_navigator/seeds/*)
            add_tests "tests/test_monitoring_seed.py"
            add_tests "tests/test_seed_integration.py"
            ;;
        pyproject.toml | uv.lock | tests/conftest.py)
            # Foundational changes — full fast loop (must precede tests/* glob)
            echo "Foundational change detected ($f); running full fast loop."
            exec bash scripts/dev_test.sh
            ;;
        tests/*)
            # Only add test files that still exist (guard against renames/deletes)
            if [ -f "$f" ]; then
                TESTS+=("$f")
            fi
            ;;
    esac
done <<< "$CHANGED"

# Dedupe
if [ "${#TESTS[@]}" -eq 0 ]; then
    echo "No mapped tests for these changes; running full fast loop."
    exec bash scripts/dev_test.sh
fi

UNIQUE_TESTS=$(printf '%s\n' "${TESTS[@]}" | sort -u | tr '\n' ' ')
echo "Running targeted tests:"
echo "  $UNIQUE_TESTS"
echo "Markers: $MARKERS"
echo

exec uv run pytest -m "$MARKERS" -x --ff --timeout=60 -q $UNIQUE_TESTS
