"""End-to-end smoke test of the OAS→graph ingestion pipeline against
real (trimmed) HPE Aruba Central and HPE GreenLake spec excerpts.

The fixtures in ``tests/fixtures/oas/real_excerpts/`` were copied from
the live spec_cache under ``build/`` and represent the shapes that
shipped past the previous (synthetic-only) suite: allOf+supportedDeviceType
chains, oneOf service definitions, simple RPC bodies, and a GLP audit-log
spec with cross-ref objects. They are NOT hand-edited; the test asserts
the post-flush graph satisfies every invariant in
:mod:`netops_api_navigator.graph.invariants`.

This is the bug-class catcher mandated by ADR-011: when the
ingestion code regresses, this test fails BEFORE the build script ever
runs against the real spec_cache (which takes ~25 min).
"""
from __future__ import annotations

import json
import sys
from pathlib import Path
from tempfile import TemporaryDirectory

import pytest

pytestmark = pytest.mark.oas_ingest

_SRC_DIR = Path(__file__).resolve().parent.parent / "src"
if str(_SRC_DIR) not in sys.path:
    sys.path.insert(0, str(_SRC_DIR))

import real_ladybug as lb  # noqa: E402

from netops_api_navigator.graph.invariants import (  # noqa: E402
    assert_graph_invariants,
    format_report,
)
from netops_api_navigator.graph.schema import (  # noqa: E402
    KNOWLEDGE_NODE_TABLES,
    KNOWLEDGE_REL_TABLES,
    NODE_TABLES,
    REL_TABLES,
)
from netops_api_navigator.oas_normalize import normalize as normalize_spec  # noqa: E402
from netops_api_navigator.oas_schema_graph import (  # noqa: E402
    collect_into_batch,
    flush_batch,
    new_batch,
    query_existing_eids,
)

_FIXTURE_DIR = Path(__file__).resolve().parent / "fixtures" / "oas" / "real_excerpts"


def _all_fixtures() -> list[Path]:
    return sorted(_FIXTURE_DIR.glob("*.json"))


def _spec_endpoints(spec: dict) -> list[tuple[str, str]]:
    out: list[tuple[str, str]] = []
    for path, ops in (spec.get("paths") or {}).items():
        if not isinstance(ops, dict):
            continue
        for method in ("get", "post", "put", "patch", "delete", "head", "options"):
            if method in ops:
                out.append((method.upper(), path))
    return out


def _spec_source(fixture: Path) -> str:
    return "glp" if fixture.name.startswith("glp_") else "central"


def _seed_endpoint(conn, method: str, path: str) -> None:
    conn.execute(
        "CREATE (e:ApiEndpoint {endpoint_id: $eid, method: $m, path: $p, "
        "summary: '', description: '', operationId: '', category: '', "
        "deprecated: false, parameters: '', requestBody: '', responses: ''})",
        parameters={"eid": f"{method}:{path}", "m": method, "p": path},
    )


@pytest.fixture
def fresh_db():
    with TemporaryDirectory(prefix="ingest_smoke_") as tmp:
        db = lb.Database(str(Path(tmp) / "graph_db"), max_db_size=256 * 1024 * 1024)
        conn = lb.Connection(db)
        for ddl in NODE_TABLES + KNOWLEDGE_NODE_TABLES + REL_TABLES + KNOWLEDGE_REL_TABLES:
            conn.execute(ddl.strip())
        yield db, conn


def test_real_spec_corpus_present():
    """Guards against accidental deletion of the fixture corpus."""
    fixtures = _all_fixtures()
    assert len(fixtures) >= 4, (
        f"expected ≥4 real-spec fixtures in {_FIXTURE_DIR}, found {len(fixtures)}"
    )
    # Must have both providers represented.
    providers = {_spec_source(f) for f in fixtures}
    assert providers >= {"central", "glp"}, providers


@pytest.mark.parametrize(
    "fixture",
    _all_fixtures(),
    ids=lambda p: p.stem,
)
def test_real_spec_fixture_passes_invariants(fresh_db, fixture):
    """Per-fixture smoke: each real-spec excerpt must ingest cleanly
    and pass every invariant after a flush.
    """
    _, conn = fresh_db
    spec = normalize_spec(json.loads(fixture.read_text(encoding="utf-8")))
    endpoints = _spec_endpoints(spec)
    assert endpoints, f"{fixture.name} has no operations — bad fixture"

    for m, p in endpoints:
        _seed_endpoint(conn, m, p)

    batch = new_batch()
    eids = query_existing_eids(conn, [f"{m}:{p}" for m, p in endpoints])
    collect_into_batch(
        batch,
        spec_source=_spec_source(fixture),
        spec=spec,
        endpoints=endpoints,
        existing_eids=eids,
    )
    flush_batch(conn, batch)

    violations = assert_graph_invariants(conn, strict=False)
    assert not violations, "\n" + format_report(violations)


def test_full_corpus_in_one_batch_passes_invariants(fresh_db):
    """Cross-fixture batch: catches richest-wins / cross-spec ref bugs
    that only surface when multiple specs share a SchemaComponent name
    (e.g. Error400) and ingest into ONE batch.
    """
    _, conn = fresh_db

    fixtures = _all_fixtures()
    # Pass A: build provider-wide resolution pool of every spec's
    # components so cross-spec $refs resolve.
    central_pool: dict = {"schemas": {}, "parameters": {}, "responses": {}, "requestBodies": {}}
    glp_pool: dict = {"schemas": {}, "parameters": {}, "responses": {}, "requestBodies": {}}
    parsed: list[tuple[Path, dict, list[tuple[str, str]]]] = []
    for fx in fixtures:
        spec = normalize_spec(json.loads(fx.read_text(encoding="utf-8")))
        eps = _spec_endpoints(spec)
        for m, p in eps:
            _seed_endpoint(conn, m, p)
        pool = glp_pool if _spec_source(fx) == "glp" else central_pool
        for bucket, items in (spec.get("components") or {}).items():
            if isinstance(items, dict):
                pool.setdefault(bucket, {}).update(items)
        parsed.append((fx, spec, eps))

    # Pass B: single shared batch.
    batch = new_batch()
    all_eids = [f"{m}:{p}" for _, _, eps in parsed for m, p in eps]
    eids = query_existing_eids(conn, all_eids)
    for fx, spec, eps in parsed:
        collect_into_batch(
            batch,
            spec_source=_spec_source(fx),
            spec=spec,
            endpoints=eps,
            existing_eids=eids,
            resolution_scope=glp_pool if _spec_source(fx) == "glp" else central_pool,
        )
    flush_batch(conn, batch)

    violations = assert_graph_invariants(conn, strict=False)
    assert not violations, "\n" + format_report(violations)

    # Sanity: at least one named SchemaComponent with HAS_PROPERTY edges.
    rows = list(conn.execute(
        "MATCH (c:SchemaComponent)-[:HAS_PROPERTY]->(:Property) "
        "WHERE NOT c.component_id CONTAINS '#' "
        "RETURN count(DISTINCT c) AS n"
    ).rows_as_dict())
    assert rows[0]["n"] >= 1, "expected ≥1 named component with properties"
