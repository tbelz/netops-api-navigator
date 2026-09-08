"""Content-addressed reuse for persisted compiler artifacts."""

from __future__ import annotations

import copy
import hashlib
import json
from pathlib import Path
from typing import Any

from .frontend import clean_spec

_ARTIFACT_CACHE_VERSION = 1


def compiler_artifact_identity(
    specs: list[dict[str, Any]],
    *,
    repo_root: Path,
) -> dict[str, str | int]:
    """Return the exact corpus and implementation identity for compiler output."""
    corpus = _corpus_fingerprint(specs)
    implementation = _implementation_fingerprint(repo_root)
    identity = hashlib.sha256(f"{corpus}:{implementation}".encode()).hexdigest()
    external_ref_count = sum(_external_ref_count(spec) for spec in specs)
    return {
        "version": _ARTIFACT_CACHE_VERSION,
        "identity": identity,
        "corpus_fingerprint": corpus,
        "implementation_fingerprint": implementation,
        "external_ref_count": external_ref_count,
    }


def load_reusable_compiler_stats(
    manifest_path: Path | None,
    *,
    ast_db_path: Path,
    compiler_projection_db_path: Path,
    identity: dict[str, str | int],
) -> dict[str, Any] | None:
    """Return prior AST stats only when identity and both artifacts match."""
    if (
        manifest_path is None
        or identity.get("external_ref_count", 0) != 0
        or not manifest_path.is_file()
        or not _is_ladybug_database_file(ast_db_path)
        or not _is_ladybug_database_file(compiler_projection_db_path)
    ):
        return None
    try:
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None
    prior_stats = manifest.get("ast")
    if not isinstance(prior_stats, dict):
        return None
    prior_cache = prior_stats.get("artifact_cache")
    if not isinstance(prior_cache, dict):
        return None
    if any(prior_cache.get(key) != value for key, value in identity.items()):
        return None

    stats = copy.deepcopy(prior_stats)
    source_timings = prior_cache.get("source_timings_seconds")
    if not isinstance(source_timings, dict):
        source_timings = stats.get("timings_seconds")
    stats["artifact_cache"] = {
        **identity,
        "reuse_hit": True,
        "source_manifest": manifest_path.name,
        "source_timings_seconds": source_timings if isinstance(source_timings, dict) else {},
    }
    task1_cache = stats.get("task1_cache")
    if isinstance(task1_cache, dict):
        task1_cache["hit_count"] = 0
        task1_cache["miss_count"] = 0
        task1_cache["skipped_via_artifact_reuse"] = True
    stats["db_path"] = ast_db_path.name
    compiler_stats = stats.get("compiler_projection")
    if isinstance(compiler_stats, dict):
        compiler_stats["db_path"] = compiler_projection_db_path.name
    return stats


def _is_ladybug_database_file(path: Path) -> bool:
    """Return whether a restored file has the shape of a LadybugDB artifact."""
    try:
        return path.is_file() and path.stat().st_size > 0
    except OSError:
        return False


def _corpus_fingerprint(specs: list[dict[str, Any]]) -> str:
    digest = hashlib.sha256()
    record_digests: list[bytes] = []
    for spec in specs:
        record = {
            "source": spec.get("_spec_source", ""),
            "spec": clean_spec(spec),
        }
        serialized = json.dumps(
            record,
            sort_keys=True,
            separators=(",", ":"),
        ).encode("utf-8")
        record_digests.append(hashlib.sha256(serialized).digest())
    for record_digest in sorted(record_digests):
        digest.update(record_digest)
    return digest.hexdigest()


def _implementation_fingerprint(repo_root: Path) -> str:
    digest = hashlib.sha256()
    digest.update(f"artifact-cache-v{_ARTIFACT_CACHE_VERSION}\n".encode())
    compiler_dir = repo_root / "src" / "netops_api_navigator" / "compiler"
    paths = sorted(compiler_dir.rglob("*.py"))
    paths.extend(
        path
        for path in (repo_root / "scripts" / "build_knowledge_db.py", repo_root / "uv.lock")
        if path.is_file()
    )
    for path in paths:
        digest.update(path.relative_to(repo_root).as_posix().encode())
        digest.update(b"\0")
        digest.update(path.read_bytes())
        digest.update(b"\0")
    return digest.hexdigest()


def _external_ref_count(value: Any) -> int:
    if isinstance(value, dict):
        count = int(
            isinstance(value.get("$ref"), str)
            and not value["$ref"].startswith("#")
        )
        return count + sum(_external_ref_count(child) for child in value.values())
    if isinstance(value, list):
        return sum(_external_ref_count(child) for child in value)
    return 0
