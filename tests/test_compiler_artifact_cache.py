"""Tests for content-addressed compiler artifact reuse."""

from __future__ import annotations

import copy
import json
import shutil
import tempfile
from collections.abc import Iterator
from pathlib import Path

import pytest

from netops_api_navigator.compiler.artifact_cache import (
    compiler_artifact_identity,
    load_reusable_compiler_stats,
)

pytestmark = [pytest.mark.compiler, pytest.mark.unit]


@pytest.fixture
def repo_tmp_path() -> Iterator[Path]:
    repo_tmp = Path(__file__).resolve().parent.parent / "tmp"
    repo_tmp.mkdir(exist_ok=True)
    path = Path(tempfile.mkdtemp(prefix="test_compiler_artifact_cache_", dir=repo_tmp))
    try:
        yield path
    finally:
        shutil.rmtree(path, ignore_errors=True)


def _fake_repo(root: Path) -> None:
    compiler_dir = root / "src" / "netops_api_navigator" / "compiler"
    compiler_dir.mkdir(parents=True)
    (compiler_dir / "builder.py").write_text("VERSION = 1\n", encoding="utf-8")
    scripts_dir = root / "scripts"
    scripts_dir.mkdir()
    (scripts_dir / "build_knowledge_db.py").write_text("BUILD = 1\n", encoding="utf-8")
    (root / "uv.lock").write_text("lock = 1\n", encoding="utf-8")


def _spec() -> dict:
    return {
        "openapi": "3.0.3",
        "info": {"title": "Pets", "version": "1.0"},
        "paths": {},
        "_spec_source": "central",
    }


def test_artifact_identity_changes_with_corpus_source_or_implementation(
    repo_tmp_path: Path,
) -> None:
    repo_root = repo_tmp_path / "repo"
    _fake_repo(repo_root)
    second_spec = _spec()
    second_spec["_spec_source"] = "glp"
    specs = [_spec(), second_spec]

    original = compiler_artifact_identity(specs, repo_root=repo_root)
    assert compiler_artifact_identity(copy.deepcopy(specs), repo_root=repo_root) == original
    assert compiler_artifact_identity(list(reversed(specs)), repo_root=repo_root) == original

    changed_spec = copy.deepcopy(specs)
    changed_spec[0]["paths"]["/pets"] = {}
    assert compiler_artifact_identity(changed_spec, repo_root=repo_root) != original

    changed_source = copy.deepcopy(specs)
    changed_source[0]["_spec_source"] = "glp"
    assert compiler_artifact_identity(changed_source, repo_root=repo_root) != original

    (repo_root / "src/netops_api_navigator/compiler/builder.py").write_text(
        "VERSION = 2\n",
        encoding="utf-8",
    )
    assert compiler_artifact_identity(specs, repo_root=repo_root) != original

    nested_dir = repo_root / "src/netops_api_navigator/compiler/rules"
    nested_dir.mkdir()
    nested_file = nested_dir / "yang.py"
    nested_file.write_text("VERSION = 1\n", encoding="utf-8")
    with_nested = compiler_artifact_identity(specs, repo_root=repo_root)
    nested_file.write_text("VERSION = 2\n", encoding="utf-8")
    assert compiler_artifact_identity(specs, repo_root=repo_root) != with_nested


def test_reusable_stats_require_exact_identity_and_both_artifacts(
    repo_tmp_path: Path,
) -> None:
    repo_root = repo_tmp_path / "repo"
    _fake_repo(repo_root)
    identity = compiler_artifact_identity([_spec()], repo_root=repo_root)
    ast_db_path = repo_tmp_path / "knowledge_db_ast"
    compiler_db_path = repo_tmp_path / "knowledge_db_compiler"
    ast_db_path.write_bytes(b"ast database")
    compiler_db_path.write_bytes(b"compiler database")
    manifest_path = repo_tmp_path / "manifest.json"
    manifest_path.write_text(
        json.dumps({
            "ast": {
                "spec_count": 1,
                "db_path": "old_ast",
                "compiler_projection": {"db_path": "old_compiler", "node_count": 2},
                "timings_seconds": {"compiler_total": 12.5},
                "task1_cache": {"hit_count": 4, "miss_count": 2},
                "artifact_cache": identity,
            }
        }),
        encoding="utf-8",
    )

    stats = load_reusable_compiler_stats(
        manifest_path,
        ast_db_path=ast_db_path,
        compiler_projection_db_path=compiler_db_path,
        identity=identity,
    )

    assert stats is not None
    assert stats["db_path"] == "knowledge_db_ast"
    assert stats["compiler_projection"]["db_path"] == "knowledge_db_compiler"
    assert stats["artifact_cache"]["reuse_hit"] is True
    assert stats["artifact_cache"]["source_timings_seconds"] == {
        "compiler_total": 12.5
    }
    assert stats["task1_cache"] == {
        "hit_count": 0,
        "miss_count": 0,
        "skipped_via_artifact_reuse": True,
    }

    mismatch = {**identity, "identity": "different"}
    assert load_reusable_compiler_stats(
        manifest_path,
        ast_db_path=ast_db_path,
        compiler_projection_db_path=compiler_db_path,
        identity=mismatch,
    ) is None

    ast_db_path.unlink()
    assert load_reusable_compiler_stats(
        manifest_path,
        ast_db_path=ast_db_path,
        compiler_projection_db_path=compiler_db_path,
        identity=identity,
    ) is None

    ast_db_path.mkdir()
    assert load_reusable_compiler_stats(
        manifest_path,
        ast_db_path=ast_db_path,
        compiler_projection_db_path=compiler_db_path,
        identity=identity,
    ) is None

    ast_db_path.rmdir()
    ast_db_path.write_bytes(b"ast database")
    compiler_db_path.write_bytes(b"")
    assert load_reusable_compiler_stats(
        manifest_path,
        ast_db_path=ast_db_path,
        compiler_projection_db_path=compiler_db_path,
        identity=identity,
    ) is None


def test_reusable_stats_reject_malformed_manifest(repo_tmp_path: Path) -> None:
    ast_db_path = repo_tmp_path / "knowledge_db_ast"
    compiler_db_path = repo_tmp_path / "knowledge_db_compiler"
    ast_db_path.mkdir()
    compiler_db_path.mkdir()
    manifest_path = repo_tmp_path / "manifest.json"
    manifest_path.write_text("{bad", encoding="utf-8")

    assert load_reusable_compiler_stats(
        manifest_path,
        ast_db_path=ast_db_path,
        compiler_projection_db_path=compiler_db_path,
        identity={"version": 1, "identity": "x"},
    ) is None


def test_external_refs_disable_artifact_reuse(repo_tmp_path: Path) -> None:
    repo_root = repo_tmp_path / "repo"
    _fake_repo(repo_root)
    spec = _spec()
    spec["components"] = {
        "schemas": {"Pet": {"$ref": "shared.yaml#/components/schemas/Pet"}}
    }
    identity = compiler_artifact_identity([spec], repo_root=repo_root)
    ast_db_path = repo_tmp_path / "knowledge_db_ast"
    compiler_db_path = repo_tmp_path / "knowledge_db_compiler"
    ast_db_path.mkdir()
    compiler_db_path.mkdir()
    manifest_path = repo_tmp_path / "manifest.json"
    manifest_path.write_text(
        json.dumps({"ast": {"artifact_cache": identity}}),
        encoding="utf-8",
    )

    assert identity["external_ref_count"] == 1
    assert load_reusable_compiler_stats(
        manifest_path,
        ast_db_path=ast_db_path,
        compiler_projection_db_path=compiler_db_path,
        identity=identity,
    ) is None
