"""Build-script tests for the L1 OpenAPI AST artifact."""

from __future__ import annotations

import importlib.util
import json
import shutil
import subprocess
import sys
import tarfile
import tempfile
from collections.abc import Iterator
from pathlib import Path

import pytest
import real_ladybug as lb
import yaml

from netops_api_navigator.compiler.ast_builder import UnknownKeywordError
from netops_api_navigator.compiler.frontend import (
    ResolutionFailure,
    ResolutionResult,
    ResolvedSpec,
    resolve_spec,
)

pytestmark = [pytest.mark.compiler, pytest.mark.unit]


@pytest.fixture
def repo_tmp_path() -> Iterator[Path]:
    repo_tmp = Path(__file__).resolve().parent.parent / "tmp"
    repo_tmp.mkdir(exist_ok=True)
    path = Path(tempfile.mkdtemp(prefix="test_build_ast_", dir=repo_tmp))
    try:
        yield path
    finally:
        shutil.rmtree(path, ignore_errors=True)


def _load_build_module():
    repo_root = Path(__file__).resolve().parent.parent
    spec = importlib.util.spec_from_file_location(
        "build_knowledge_db",
        repo_root / "scripts" / "build_knowledge_db.py",
    )
    mod = importlib.util.module_from_spec(spec)  # type: ignore[arg-type]
    spec.loader.exec_module(mod)  # type: ignore[union-attr]
    return mod


def _oas_spec() -> dict:
    return {
        "openapi": "3.0.3",
        "info": {"title": "Pets", "version": "1.0"},
        "paths": {
            "/pets": {
                "get": {
                    "operationId": "listPets",
                    "parameters": [
                        {
                            "name": "limit",
                            "in": "query",
                            "schema": {"type": "integer"},
                        }
                    ],
                    "requestBody": {
                        "content": {
                            "application/json": {
                                "schema": {
                                    "$ref": "#/components/schemas/Pet",
                                }
                            }
                        }
                    },
                    "responses": {
                        "200": {
                            "description": "ok",
                            "content": {
                                "application/json": {
                                    "schema": {
                                        "$ref": "#/components/schemas/Pet",
                                    }
                                }
                            },
                        }
                    },
                }
            }
        },
        "components": {
            "schemas": {
                "Pet": {
                    "type": "object",
                    "properties": {"id": {"type": "string"}},
                }
            }
        },
    }


def _resolved() -> ResolvedSpec:
    outcome = resolve_spec(_oas_spec(), source="unit/pets")
    assert isinstance(outcome, ResolvedSpec), getattr(outcome, "error", None)
    return outcome


def test_remove_build_path_handles_ladybug_file_or_directory(repo_tmp_path: Path) -> None:
    mod = _load_build_module()
    file_path = repo_tmp_path / "file_db"
    file_path.write_text("db", encoding="utf-8")
    directory_path = repo_tmp_path / "directory_db"
    directory_path.mkdir()
    (directory_path / "db.lbd").write_text("db", encoding="utf-8")

    mod._remove_build_path(file_path)
    mod._remove_build_path(directory_path)

    assert not file_path.exists()
    assert not directory_path.exists()


@pytest.mark.timeout(90)
def test_build_ast_artifact_writes_queryable_ladybug_db(repo_tmp_path: Path) -> None:
    mod = _load_build_module()
    ast_db_path = repo_tmp_path / "knowledge_db_ast"
    compiler_db_path = repo_tmp_path / "knowledge_db_compiler"
    ast_db_path.mkdir()
    compiler_db_path.mkdir()
    (ast_db_path / "stale.txt").write_text("old", encoding="utf-8")
    (compiler_db_path / "stale.txt").write_text("old", encoding="utf-8")

    stats = mod._build_ast_artifact(
        ast_db_path,
        [_resolved()],
        task1_failures=[
            ResolutionFailure(
                source="unit/bad",
                title="Bad",
                error="strict validation failed",
                error_type="validation",
            ),
            ResolutionFailure(
                source="unit/broken",
                title="Broken",
                error="ref resolution failed",
                error_type="resolution",
            ),
        ],
        compiler_projection_db_path=compiler_db_path,
    )

    assert stats["enabled"] is True
    assert stats["db_path"] == "knowledge_db_ast"
    assert stats["graph_batch_size"] == 256
    assert stats["raw_spec_count"] == 3
    assert stats["task1_resolved_count"] == 1
    assert stats["spec_count"] == 1
    assert stats["task1_failed_count"] == 2
    assert stats["degraded"]["candidate_count"] == 2
    assert stats["degraded"]["compiled_count"] == 0
    assert stats["degraded"]["failed_count"] == 2
    assert stats["task1_failures"] == [
        {
            "source": "unit/bad",
            "title": "Bad",
            "error_type": "validation",
            "error": "strict validation failed",
        },
        {
            "source": "unit/broken",
            "title": "Broken",
            "error_type": "resolution",
            "error": "ref resolution failed",
        },
    ]
    assert stats["node_count"] > 0
    assert stats["child_edge_count"] > 0
    assert stats["ref_edge_count"] == 2
    assert stats["semantic"]["graph_count"] == 1
    assert stats["semantic"]["node_count"] > 0
    assert stats["semantic"]["edge_count"] > 0
    assert stats["semantic"]["derived_from_ast_edge_count"] > 0
    assert set(stats["semantic"]["rule_packs"]) == {
        "semantic.identity.v1",
        "semantic.structural.v1",
    }
    assert stats["semantic"]["metrics"]["node_kind_counts"]["ApiEndpoint"] == 1
    assert stats["semantic"]["metrics"]["node_kind_counts"]["ModelEntity"] == 3
    assert stats["semantic"]["metrics"]["node_kind_counts"]["Parameter"] == 1
    assert stats["semantic"]["metrics"]["node_kind_counts"]["RequestBody"] == 1
    assert stats["semantic"]["metrics"]["node_kind_counts"]["Response"] == 1
    assert stats["semantic"]["metrics"]["edge_kind_counts"]["BODY_REFERENCES"] == 1
    assert stats["semantic"]["metrics"]["edge_kind_counts"]["ACCEPTS_MODEL"] == 1
    assert stats["semantic"]["metrics"]["edge_kind_counts"]["HAS_PARAMETER"] == 1
    assert stats["semantic"]["metrics"]["edge_kind_counts"]["HAS_REQUEST_BODY"] == 1
    assert stats["semantic"]["metrics"]["edge_kind_counts"]["HAS_RESPONSE"] == 1
    assert stats["semantic"]["metrics"]["edge_kind_counts"]["RESPONSE_REFERENCES"] == 1
    assert stats["semantic"]["metrics"]["edge_kind_counts"]["REPRESENTS_MODEL"] == 3
    assert stats["semantic"]["metrics"]["edge_kind_counts"]["RETURNS_MODEL"] == 1
    assert stats["semantic"]["metrics"]["edge_kind_counts"]["RETURNS_SCHEMA"] == 1
    assert stats["semantic"]["metrics"]["carry_through"] == {
        "raw_spec_count": 3,
        "task1_resolved_count": 1,
        "task1_failed_count": 2,
        "degraded_compiled_count": 0,
        "degraded_failed_count": 2,
        "ast_graph_count": 1,
        "semantic_graph_count": 1,
        "resolved_to_ast_ratio": 1.0,
        "raw_to_semantic_ratio": 0.3333,
    }
    assert stats["semantic"]["metrics"]["coverage"]["endpoints_with_parameters"] == {
        "count": 1,
        "total": 1,
        "ratio": 1.0,
    }
    assert stats["semantic"]["metrics"]["coverage"]["endpoints_with_request_bodies"] == {
        "count": 1,
        "total": 1,
        "ratio": 1.0,
    }
    assert stats["semantic"]["metrics"]["coverage"]["endpoints_with_responses"] == {
        "count": 1,
        "total": 1,
        "ratio": 1.0,
    }
    assert stats["semantic"]["metrics"]["coverage"]["endpoints_returning_schema"] == {
        "count": 1,
        "total": 1,
        "ratio": 1.0,
    }
    assert stats["semantic"]["metrics"]["coverage"]["endpoints_with_any_model_edge"] == {
        "count": 1,
        "total": 1,
        "ratio": 1.0,
    }
    assert stats["semantic"]["metrics"]["coverage"]["schemas_representing_model"] == {
        "count": 2,
        "total": 2,
        "ratio": 1.0,
    }
    assert stats["semantic"]["metrics"]["coverage"]["semantic_nodes_with_ast_provenance"][
        "ratio"
    ] == 1.0
    assert stats["compiler_projection"]["enabled"] is True
    assert stats["compiler_projection"]["db_path"] == "knowledge_db_compiler"
    assert stats["compiler_projection"]["node_kind_counts"]["ApiEndpoint"] == 1
    assert stats["compiler_projection"]["node_kind_counts"]["SchemaComponent"] == 2
    assert stats["compiler_projection"]["node_kind_counts"]["Property"] == 1
    assert stats["compiler_projection"]["edge_kind_counts"]["HAS_REQUEST_BODY"] == 1
    assert stats["compiler_projection"]["edge_kind_counts"]["BODY_REFERENCES"] == 1
    assert stats["compiler_projection"]["provenance_count"] > 0
    assert stats["compiler_projection"]["catalog_identity"] == {
        "base_identity_count": 1,
        "variant_identity_count": 1,
        "conflicting_named_identity_count": 0,
        "identical_named_identity_merge_count": 0,
    }
    assert set(stats["timings_seconds"]) == {
        "compile",
        "ast_write",
        "semantic_write",
        "projection_collect",
        "projection_write",
        "compiler_total",
    }
    assert all(value >= 0 for value in stats["timings_seconds"].values())
    assert not (ast_db_path / "stale.txt").exists()
    assert not (compiler_db_path / "stale.txt").exists()
    json.dumps({"ast": stats})

    db = lb.Database(str(ast_db_path), max_db_size=256 * 1024 * 1024)
    conn = lb.Connection(db)
    try:
        root_rows = list(
            conn.execute(
                """
                MATCH (s:OasSpec)-[:HAS_AST_ROOT]->(root:OasAstNode)
                RETURN s.source AS source, root.kind AS root_kind
                """
            ).rows_as_dict()
        )
        assert root_rows == [{"source": "unit/pets", "root_kind": "Document"}]

        child_count = list(
            conn.execute("MATCH ()-[r:AST_CHILD]->() RETURN COUNT(r) AS n").rows_as_dict()
        )[0]["n"]
        assert child_count == stats["child_edge_count"]

        ref_rows = list(
            conn.execute(
                """
                MATCH (r:OasAstNode)-[edge:AST_REF_TARGET]->(target:OasAstNode)
                RETURN r.key AS key, edge.ref AS ref, target.kind AS target_kind
                """
            ).rows_as_dict()
        )
        assert ref_rows == [
            {
                "key": "$ref",
                "ref": "#/components/schemas/Pet",
                "target_kind": "Schema",
            },
            {
                "key": "$ref",
                "ref": "#/components/schemas/Pet",
                "target_kind": "Schema",
            },
        ]
        semantic_rows = list(
            conn.execute(
                """
                MATCH (e:SemanticNode {kind: 'ApiEndpoint'})
                      -[edge:SEMANTIC_EDGE]->(schema:SemanticNode {kind: 'SchemaComponent'})
                RETURN e.name AS endpoint, edge.kind AS edge_kind, schema.name AS schema
                """
            ).rows_as_dict()
        )
        assert {
            (row["endpoint"], row["edge_kind"], row["schema"])
            for row in semantic_rows
        } == {
            ("GET /pets", "ACCEPTS_SCHEMA", "Pet"),
            ("GET /pets", "RETURNS_SCHEMA", "Pet"),
        }
        traversal_rows = list(
            conn.execute(
                """
                MATCH (e:SemanticNode {kind: 'ApiEndpoint'})
                      -[:SEMANTIC_EDGE]->(body:SemanticNode {kind: 'RequestBody'})
                      -[edge:SEMANTIC_EDGE]->(schema:SemanticNode {kind: 'SchemaComponent'})
                WHERE edge.kind = 'BODY_REFERENCES'
                RETURN e.name AS endpoint, body.kind AS body_kind, schema.name AS schema
                """
            ).rows_as_dict()
        )
        assert traversal_rows == [
            {
                "endpoint": "GET /pets",
                "body_kind": "RequestBody",
                "schema": "Pet",
            }
        ]
        parameter_rows = list(
            conn.execute(
                """
                MATCH (e:SemanticNode {kind: 'ApiEndpoint'})
                      -[edge:SEMANTIC_EDGE]->(p:SemanticNode {kind: 'Parameter'})
                WHERE edge.kind = 'HAS_PARAMETER'
                RETURN e.name AS endpoint, p.name AS parameter
                """
            ).rows_as_dict()
        )
        assert parameter_rows == [{"endpoint": "GET /pets", "parameter": "limit"}]
        response_rows = list(
            conn.execute(
                """
                MATCH (e:SemanticNode {kind: 'ApiEndpoint'})
                      -[:SEMANTIC_EDGE]->(response:SemanticNode {kind: 'Response'})
                      -[edge:SEMANTIC_EDGE]->(schema:SemanticNode {kind: 'SchemaComponent'})
                WHERE edge.kind = 'RESPONSE_REFERENCES'
                RETURN e.name AS endpoint, response.kind AS response_kind, schema.name AS schema
                """
            ).rows_as_dict()
        )
        assert response_rows == [
            {
                "endpoint": "GET /pets",
                "response_kind": "Response",
                "schema": "Pet",
            }
        ]
        model_rows = list(
            conn.execute(
                """
                MATCH (e:SemanticNode {kind: 'ApiEndpoint'})
                      -[edge:SEMANTIC_EDGE]->(model:SemanticNode {kind: 'ModelEntity'})
                WHERE edge.kind IN ['ACCEPTS_MODEL', 'RETURNS_MODEL']
                RETURN e.name AS endpoint, edge.kind AS edge_kind, model.name AS model
                ORDER BY edge.kind
                """
            ).rows_as_dict()
        )
        assert model_rows == [
            {
                "endpoint": "GET /pets",
                "edge_kind": "ACCEPTS_MODEL",
                "model": "Pet",
            },
            {
                "endpoint": "GET /pets",
                "edge_kind": "RETURNS_MODEL",
                "model": "Pet",
            },
        ]
        semantic_node_count = list(
            conn.execute("MATCH (n:SemanticNode) RETURN COUNT(n) AS n").rows_as_dict()
        )[0]["n"]
        assert semantic_node_count == stats["semantic"]["node_count"]

        semantic_edge_count = list(
            conn.execute("MATCH ()-[r:SEMANTIC_EDGE]->() RETURN COUNT(r) AS n").rows_as_dict()
        )[0]["n"]
        assert semantic_edge_count == stats["semantic"]["edge_count"]

        provenance_rows = list(
            conn.execute(
                """
                MATCH (:SemanticNode)-[edge:SEMANTIC_DERIVED_FROM]->(:OasAstNode)
                RETURN COUNT(edge) AS n
                """
            ).rows_as_dict()
        )
        assert provenance_rows[0]["n"] == stats["semantic"]["derived_from_ast_edge_count"]
    finally:
        db.close()


@pytest.mark.timeout(90)
def test_build_ast_artifact_carries_strict_failure_as_marked_degraded_graph(
    repo_tmp_path: Path,
) -> None:
    mod = _load_build_module()
    invalid = _oas_spec()
    invalid.pop("info")
    failure = resolve_spec(invalid, source="unit/degraded")
    assert isinstance(failure, ResolutionFailure)
    assert failure.raw_spec == invalid

    ast_db_path = repo_tmp_path / "knowledge_db_ast"
    compiler_db_path = repo_tmp_path / "knowledge_db_compiler"
    stats = mod._build_ast_artifact(
        ast_db_path,
        [],
        task1_failures=[failure],
        compiler_projection_db_path=compiler_db_path,
    )

    assert stats["task1_resolved_count"] == 0
    assert stats["task1_failed_count"] == 1
    assert stats["spec_count"] == 1
    assert stats["degraded"] == {
        "candidate_count": 1,
        "compiled_count": 1,
        "failed_count": 0,
        "failures": [],
    }
    assert stats["semantic"]["metrics"]["carry_through"] == {
        "raw_spec_count": 1,
        "task1_resolved_count": 0,
        "task1_failed_count": 1,
        "degraded_compiled_count": 1,
        "degraded_failed_count": 0,
        "ast_graph_count": 1,
        "semantic_graph_count": 1,
        "resolved_to_ast_ratio": 0.0,
        "raw_to_semantic_ratio": 1.0,
    }

    ast_db = lb.Database(str(ast_db_path), max_db_size=256 * 1024 * 1024)
    compiler_db = lb.Database(str(compiler_db_path), max_db_size=256 * 1024 * 1024)
    try:
        ast_rows = list(
            lb.Connection(ast_db).execute(
                """
                MATCH (spec:OasSpec)
                RETURN spec.source AS source,
                       spec.ingestion_status AS status,
                       spec.ingestion_error_type AS error_type
                """
            ).rows_as_dict()
        )
        assert ast_rows == [{
            "source": "unit/degraded",
            "status": "degraded",
            "error_type": "validation",
        }]

        provenance_rows = list(
            lb.Connection(compiler_db).execute(
                """
                MATCH (row:CompilerProjectionMap {table_name: 'ApiEndpoint'})
                RETURN row.ingestion_status AS status,
                       row.ingestion_error_type AS error_type
                """
            ).rows_as_dict()
        )
        assert provenance_rows == [{
            "status": "degraded",
            "error_type": "validation",
        }]
    finally:
        compiler_db.close()
        ast_db.close()

    compiler_db = lb.Database(str(compiler_db_path), max_db_size=256 * 1024 * 1024)
    compiler_conn = lb.Connection(compiler_db)
    try:
        typed_rows = list(
            compiler_conn.execute(
                """
                MATCH (e:ApiEndpoint {method: 'GET', path: '/pets'})
                      -[:HAS_REQUEST_BODY]->(body:RequestBody)
                      -[:BODY_REFERENCES]->(schema:SchemaComponent)
                      -[:HAS_PROPERTY]->(prop:Property)
                RETURN e.endpoint_id AS endpoint,
                       body.content_type AS content_type,
                       schema.name AS schema,
                       prop.name AS property
                """
            ).rows_as_dict()
        )
        assert typed_rows == [
            {
                "endpoint": "GET:/pets",
                "content_type": "application/json",
                "schema": "Pet",
                "property": "id",
            }
        ]
        response_rows = list(
            compiler_conn.execute(
                """
                MATCH (:ApiEndpoint {method: 'GET', path: '/pets'})
                      -[:HAS_RESPONSE]->(response:Response)
                      -[:RESPONSE_REFERENCES]->(schema:SchemaComponent)
                RETURN response.status AS status,
                       response.content_type AS content_type,
                       schema.component_id AS component
                """
            ).rows_as_dict()
        )
        assert response_rows == [
            {
                "status": "200",
                "content_type": "application/json",
                "component": "unit:schemas:Pet",
            }
        ]
    finally:
        compiler_db.close()


def test_build_ast_artifact_fails_when_resolved_spec_cannot_build(
    repo_tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    mod = _load_build_module()

    def fail_build(_resolved_spec):
        raise UnknownKeywordError(
            source="unit/pets",
            pointer="",
            parent_kind="Document",
            key="madeUpKeyword",
        )

    monkeypatch.setattr(mod, "build_ast_from_resolved", fail_build)

    with pytest.raises(UnknownKeywordError):
        mod._build_ast_artifact(
            repo_tmp_path / "knowledge_db_ast",
            [_resolved()],
            task1_failures=[],
        )


def test_build_ast_artifact_reports_uncompilable_degraded_spec(
    repo_tmp_path: Path,
) -> None:
    mod = _load_build_module()
    failure = ResolutionFailure(
        source="unit/unknown-keyword",
        title="Unknown keyword",
        error="strict validation failed",
        error_type="validation",
        raw_spec={
            "openapi": "3.0.3",
            "info": {"title": "Unknown keyword", "version": "1.0"},
            "paths": {},
            "madeUpKeyword": True,
        },
    )

    stats = mod._build_ast_artifact(
        repo_tmp_path / "knowledge_db_ast",
        [],
        task1_failures=[failure],
    )

    assert stats["spec_count"] == 0
    assert stats["degraded"]["candidate_count"] == 1
    assert stats["degraded"]["compiled_count"] == 0
    assert stats["degraded"]["failed_count"] == 1
    assert stats["degraded"]["failures"][0]["source"] == "unit/unknown-keyword"
    assert stats["degraded"]["failures"][0]["compiler_error_type"] == (
        "UnknownKeywordError"
    )
    assert "madeUpKeyword" in stats["degraded"]["failures"][0]["compiler_error"]


def test_build_ast_artifact_distinguishes_empty_from_missing_degraded_spec(
    repo_tmp_path: Path,
) -> None:
    mod = _load_build_module()
    empty = ResolutionFailure(
        source="unit/empty",
        title="Empty",
        error="strict validation failed",
        error_type="validation",
        raw_spec={},
    )
    missing = ResolutionFailure(
        source="unit/missing",
        title="Missing",
        error="strict validation failed",
        error_type="validation",
    )

    stats = mod._build_ast_artifact(
        repo_tmp_path / "knowledge_db_ast",
        [],
        task1_failures=[empty, missing],
    )

    assert stats["degraded"]["compiled_count"] == 1
    assert stats["degraded"]["failed_count"] == 1
    assert stats["degraded"]["failures"][0]["source"] == "unit/missing"
    assert stats["degraded"]["failures"][0]["compiler_error_type"] == "MissingRawSpec"


def test_release_archives_keep_ast_tar_separate(repo_tmp_path: Path) -> None:
    mod = _load_build_module()
    db_path = repo_tmp_path / "knowledge_db"
    ast_db_path = repo_tmp_path / "knowledge_db_ast"
    compiler_db_path = repo_tmp_path / "knowledge_db_compiler"
    db_path.write_text("runtime", encoding="utf-8")
    ast_db_path.write_text("ast", encoding="utf-8")
    compiler_db_path.write_text("compiler", encoding="utf-8")
    manifest_path = repo_tmp_path / "manifest.json"
    manifest_path.write_text('{"version": "unit"}', encoding="utf-8")

    archives = mod._create_release_archives(
        repo_tmp_path,
        db_path=db_path,
        ast_db_path=ast_db_path,
        compiler_projection_db_path=compiler_db_path,
        manifest_path=manifest_path,
    )

    assert archives["knowledge_db"].name == "knowledge_db.tar.gz"
    assert archives["knowledge_db_compiler"].name == "knowledge_db_compiler.tar.gz"
    assert archives["knowledge_db_ast"].name == "knowledge_db_ast.tar.gz"

    with tarfile.open(archives["knowledge_db"], "r:gz") as tf:
        names = tf.getnames()
    assert "knowledge_db" in names
    assert "manifest.json" in names
    assert all(not name.startswith("knowledge_db_ast/") for name in names)
    assert all(not name.startswith("knowledge_db_compiler/") for name in names)

    with tarfile.open(archives["knowledge_db_compiler"], "r:gz") as tf:
        compiler_names = tf.getnames()
    assert compiler_names == ["knowledge_db_compiler"]
    assert "manifest.json" not in compiler_names

    with tarfile.open(archives["knowledge_db_ast"], "r:gz") as tf:
        ast_names = tf.getnames()
    assert ast_names == ["knowledge_db_ast"]
    assert "manifest.json" not in ast_names


def test_prepare_compiler_artifact_reuses_exact_prior_artifacts(
    repo_tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    mod = _load_build_module()
    repo_root = repo_tmp_path / "repo"
    repo_root.mkdir()
    specs = [_oas_spec()]
    identity = mod.compiler_artifact_identity(specs, repo_root=repo_root)
    ast_db_path = repo_tmp_path / "knowledge_db_ast"
    compiler_db_path = repo_tmp_path / "knowledge_db_compiler"
    ast_db_path.write_bytes(b"ast database")
    compiler_db_path.write_bytes(b"compiler database")
    manifest_path = repo_tmp_path / "prior_manifest.json"
    manifest_path.write_text(
        json.dumps({
            "ast": {
                "spec_count": 1,
                "node_count": 10,
                "child_edge_count": 9,
                "ref_edge_count": 0,
                "semantic": {"node_count": 2, "edge_count": 1},
                "degraded": {"candidate_count": 0, "compiled_count": 0, "failed_count": 0},
                "compiler_projection": {"node_count": 2, "edge_count": 1},
                "timings_seconds": {"compiler_total": 4.0},
                "artifact_cache": identity,
            }
        }),
        encoding="utf-8",
    )

    def unexpected_resolve(*_args, **_kwargs):
        raise AssertionError("exact compiler artifacts should skip Task 1")

    monkeypatch.setattr(mod, "resolve_specs", unexpected_resolve)

    stats = mod._prepare_compiler_artifact(
        specs,
        repo_root=repo_root,
        ast_db_path=ast_db_path,
        compiler_projection_db_path=compiler_db_path,
        task1_cache_path=repo_tmp_path / "compiler-task1-cache.json",
        reuse_manifest=manifest_path,
    )

    assert stats["artifact_cache"]["reuse_hit"] is True
    assert set(stats["timings_seconds"]) == {"compiler_reuse"}
    assert (repo_tmp_path / "compiler-task1-cache.json").is_file()


def test_prepare_compiler_artifact_rebuilds_on_reuse_miss(
    repo_tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    mod = _load_build_module()
    resolved = _resolved()
    task1 = ResolutionResult(resolved=[resolved], workers_used=1)
    monkeypatch.setattr(mod, "load_resolution_cache", lambda _path: {})
    monkeypatch.setattr(mod, "resolve_specs", lambda *_args, **_kwargs: task1)
    monkeypatch.setattr(mod, "write_resolution_cache", lambda *_args: None)
    monkeypatch.setattr(
        mod,
        "_build_ast_artifact",
        lambda *_args, **_kwargs: {
            "timings_seconds": {"compiler_total": 3.0},
            "compiler_projection": {},
        },
    )

    stats = mod._prepare_compiler_artifact(
        [_oas_spec()],
        repo_root=repo_tmp_path,
        ast_db_path=repo_tmp_path / "knowledge_db_ast",
        compiler_projection_db_path=repo_tmp_path / "knowledge_db_compiler",
        task1_cache_path=repo_tmp_path / "compiler-task1-cache.json",
        reuse_manifest=None,
    )

    assert stats["artifact_cache"]["reuse_hit"] is False
    assert stats["task1_cache"]["hit_count"] == 0
    assert stats["task1_cache"]["miss_count"] == 0
    assert stats["timings_seconds"]["compiler_pipeline_total"] >= 3.0


def test_release_workflow_restores_and_uses_compiler_artifacts() -> None:
    workflow_path = (
        Path(__file__).resolve().parent.parent
        / ".github"
        / "workflows"
        / "update-knowledge-db.yml"
    )
    workflow = yaml.safe_load(workflow_path.read_text(encoding="utf-8"))
    steps = workflow["jobs"]["build-knowledge-db"]["steps"]
    by_name = {step.get("name"): step for step in steps}

    prime = by_name["Prime content-identical compiler artifacts from previous release"][
        "run"
    ]
    assert "knowledge_db_ast.tar.gz" in prime
    assert "knowledge_db_compiler.tar.gz" in prime
    assert "manifest.json" in prime

    build = by_name["Build knowledge database"]["run"]
    assert "--compiler-reuse-manifest build/compiler_reuse/manifest.json" in build
    assert "--strict" in build
    assert "--strict-compiler" in build

    diff = by_name["Check for changes against latest release"]["run"]
    assert ".ast.artifact_cache.identity" in diff
    assert ".ast.artifact_cache.external_ref_count" in diff
    assert '[ "$NEW_EXTERNAL_REFS" = "0" ]' in diff
    assert ".ast // null" not in diff


def test_compiler_cutover_gates_pass_when_all_inputs_are_clean() -> None:
    mod = _load_build_module()

    gates = mod._compiler_cutover_gates(
        projection_parity={"all_legacy_effectively_covered": True},
        compiler_invariant_violations=[],
        compiler_traversal={"failure_count": 0},
    )

    assert gates == {
        "passed": True,
        "parity_passed": True,
        "invariants_passed": True,
        "traversal_passed": True,
        "default_flip_ready": True,
    }


def test_compiler_cutover_gates_report_effective_parity_gap_without_blocking_release() -> None:
    mod = _load_build_module()

    gates = mod._compiler_cutover_gates(
        projection_parity={"all_legacy_effectively_covered": False},
        compiler_invariant_violations=[],
        compiler_traversal={"failure_count": 0},
    )

    assert gates["passed"] is True
    assert gates["parity_passed"] is False
    assert gates["default_flip_ready"] is False
    assert mod._format_compiler_gate_failure(gates) == "unknown"


def test_compiler_cutover_gates_fail_for_invariant_violation() -> None:
    mod = _load_build_module()
    violation = mod.InvariantViolation(
        invariant="INV-X",
        detail="broken shape",
        sample=[{"component_id": "central:schemas:Broken"}],
    )

    gates = mod._compiler_cutover_gates(
        projection_parity={"all_legacy_effectively_covered": True},
        compiler_invariant_violations=[violation],
        compiler_traversal={"failure_count": 0},
    )

    assert gates["passed"] is False
    assert gates["invariants_passed"] is False
    assert gates["default_flip_ready"] is False
    assert mod._serialize_invariant_violations([violation]) == [
        {
            "invariant": "INV-X",
            "detail": "broken shape",
            "sample": [{"component_id": "central:schemas:Broken"}],
        }
    ]


def test_compiler_cutover_gates_fail_for_traversal_failure() -> None:
    mod = _load_build_module()

    gates = mod._compiler_cutover_gates(
        projection_parity={"all_legacy_effectively_covered": True},
        compiler_invariant_violations=[],
        compiler_traversal={"failure_count": 2},
    )

    assert gates["passed"] is False
    assert gates["traversal_passed"] is False
    assert gates["default_flip_ready"] is False
    assert mod._format_compiler_gate_failure(gates) == "traversal"


def test_sample_build_strict_compiler_writes_cutover_manifest(
    repo_tmp_path: Path,
) -> None:
    repo_root = Path(__file__).resolve().parent.parent
    central_dir = repo_tmp_path / "spec_cache" / "central"
    glp_dir = repo_tmp_path / "spec_cache" / "glp"
    central_dir.mkdir(parents=True)
    glp_dir.mkdir(parents=True)
    shutil.copy2(
        repo_root
        / "tests"
        / "fixtures"
        / "oas"
        / "real_excerpts"
        / "central_config_createairgroupservicedefinitionsservicebyid.json",
        central_dir / "central.json",
    )
    shutil.copy2(
        repo_root
        / "tests"
        / "fixtures"
        / "oas"
        / "real_excerpts"
        / "glp_audit_logs.json",
        glp_dir / "glp.json",
    )

    result = subprocess.run(
        [
            sys.executable,
            "scripts/build_knowledge_db.py",
            "--output-dir",
            str(repo_tmp_path),
            "--sample",
            "1",
            "--strict",
            "--strict-compiler",
            "--tar",
        ],
        cwd=repo_root,
        text=True,
        capture_output=True,
        timeout=30,
        check=False,
    )

    assert result.returncode == 0, result.stderr + "\n" + result.stdout
    manifest = json.loads((repo_tmp_path / "manifest.json").read_text())
    assert manifest["compiler_invariants"]["violation_count"] == 0
    assert manifest["compiler_traversal"]["failure_count"] == 0
    assert manifest["compiler_cutover_gates"] == {
        "passed": True,
        "parity_passed": True,
        "invariants_passed": True,
        "traversal_passed": True,
        "default_flip_ready": True,
    }
    assert (repo_tmp_path / "knowledge_db_compiler.tar.gz").exists()
    assert (repo_tmp_path / "knowledge_db_ast.tar.gz").exists()


@pytest.mark.real_spec
def test_real_central_stride_builds_ast_artifact(
    repo_tmp_path: Path,
    real_central_specs: list[Path],
) -> None:
    mod = _load_build_module()
    resolved: list[ResolvedSpec] = []
    for path in real_central_specs[::64][:25]:
        outcome = resolve_spec(
            json.loads(path.read_text(encoding="utf-8")),
            source=f"central/{path.name}",
        )
        if isinstance(outcome, ResolvedSpec):
            resolved.append(outcome)
        if len(resolved) >= 3:
            break

    assert resolved, "expected at least one sampled real spec to resolve"
    stats = mod._build_ast_artifact(
        repo_tmp_path / "knowledge_db_ast",
        resolved,
        task1_failures=[],
    )
    assert stats["spec_count"] == len(resolved)
    assert stats["node_count"] > 0
    assert stats["child_edge_count"] > 0
    assert stats["semantic"]["node_count"] > 0
    assert stats["semantic"]["edge_count"] > 0
