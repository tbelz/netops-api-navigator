"""Knowledge DB downloader.

Extracted from server.py so production startup and tests share one
implementation. Downloads the latest ``knowledge_db.tar.gz`` asset from
a GitHub release and extracts it to the configured database path.
"""

from __future__ import annotations

import hashlib
import json
import logging
import os
import shutil
import tarfile
import tempfile
import uuid
from pathlib import Path
from typing import Callable
from urllib.parse import quote

import httpx

_DEFAULT_LOGGER = logging.getLogger("hpe_networking_central_mcp.knowledge_db")

# How long to wait for the GitHub release-info API call. Kept short so a
# spotty network does not eat the MCP client's ``initialize`` budget on
# cold start — the typical Claude Code timeout is ~10s and the rest of
# startup (graph init + seed sync) also runs synchronously.
_RELEASE_INFO_TIMEOUT = 8.0
_DOWNLOAD_TIMEOUT = 120.0


def _stdlib_log(level: int):
    """Return a callable that accepts structlog-style **kwargs and forwards
    them to ``logging`` as an ``extra`` mapping (so stdlib won't reject them).
    """

    def _log(event: str, **kwargs) -> None:
        if kwargs:
            _DEFAULT_LOGGER.log(level, "%s %s", event, kwargs)
        else:
            _DEFAULT_LOGGER.log(level, event)

    return _log


def _read_local_manifest_state(manifest_path: Path) -> dict:
    """Return release/install metadata from the on-disk manifest, if any.

    The manifest written by :func:`_write_local_manifest` carries an explicit
    ``release_tag`` field set from the GitHub release ``tag_name``. Older
    manifests (built before the resume-on-restart fix) only carry
    ``version``, which the build script generates from the same timestamp
    template as the release tag, so we fall back to it for backward compat.
    """
    if not manifest_path.exists():
        return {}
    try:
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    except Exception:
        return {}
    tag = manifest.get("release_tag") or manifest.get("version")
    return {
        "release_tag": tag if isinstance(tag, str) and tag else None,
        "knowledge_asset": manifest.get("knowledge_asset"),
        "knowledge_archive_member": manifest.get("knowledge_archive_member"),
        "knowledge_projection": manifest.get("knowledge_projection"),
        "knowledge_asset_sha256": manifest.get("knowledge_asset_sha256"),
    }


def _read_local_release_tag(manifest_path: Path) -> str | None:
    """Return the GitHub release tag recorded in the on-disk manifest, if any."""
    state = _read_local_manifest_state(manifest_path)
    tag = state.get("release_tag")
    return tag if isinstance(tag, str) and tag else None


def _write_local_manifest(
    extracted_manifest: Path,
    dest_manifest: Path,
    *,
    release_tag: str | None,
    asset_name: str = "knowledge_db.tar.gz",
    archive_member: str = "knowledge_db",
    projection: str = "legacy",
    asset_sha256: str | None = None,
) -> None:
    """Copy the extracted manifest to ``dest_manifest`` and stamp the
    GitHub release ``tag_name`` so subsequent startups can short-circuit
    the download when the tag is already present locally.
    """
    try:
        manifest = json.loads(extracted_manifest.read_text(encoding="utf-8"))
    except Exception:
        # Manifest is malformed — copy verbatim so we don't lose the
        # original, but we won't be able to short-circuit next time.
        shutil.copy2(extracted_manifest, dest_manifest)
        return
    if release_tag:
        manifest["release_tag"] = release_tag
    manifest["knowledge_asset"] = asset_name
    manifest["knowledge_archive_member"] = archive_member
    manifest["knowledge_projection"] = projection
    if asset_sha256:
        manifest["knowledge_asset_sha256"] = asset_sha256
    dest_manifest.parent.mkdir(parents=True, exist_ok=True)
    temp_manifest = dest_manifest.with_name(f".{dest_manifest.name}.{uuid.uuid4().hex}.tmp")
    temp_manifest.write_text(json.dumps(manifest, indent=2), encoding="utf-8")
    os.replace(temp_manifest, dest_manifest)


def download_knowledge_db(
    repo: str,
    db_path: Path,
    *,
    asset_name: str = "knowledge_db.tar.gz",
    archive_member: str = "knowledge_db",
    projection: str = "legacy",
    manifest_name: str = "manifest.json",
    release_tag: str = "",
    expected_sha256: str = "",
    require_digest: bool = False,
    force: bool = False,
    logger: Callable | None = None,
) -> bool:
    """Download the latest knowledge DB tar.gz from a GitHub release.

    Parameters
    ----------
    repo : str
        ``owner/name`` of the GitHub repository hosting the release.
    db_path : Path
        Destination path for the extracted database (file or directory).
    force : bool
        When true, bypass the local release-tag short-circuit and reinstall
        the selected asset. Used to recover tagged-but-unopenable persisted
        Docker volumes.
    logger : structured logger, optional
        Object with ``info``/``warning`` methods accepting ``**kwargs``.
        Falls back to the standard ``logging`` module.

    Returns
    -------
    bool
        ``True`` when a DB was downloaded and extracted, ``False`` otherwise.

    Security
    --------
    Members whose names start with ``/`` or contain ``..`` are rejected to
    prevent path-traversal extraction (CWE-22).
    """
    log = logger or _DEFAULT_LOGGER
    _info = getattr(log, "info", None) or _stdlib_log(logging.INFO)
    _warn = getattr(log, "warning", None) or _stdlib_log(logging.WARNING)
    # Stdlib loggers (logging.Logger) expose info/warning that do not accept
    # arbitrary kwargs, so wrap them to honour the documented kwargs contract.
    # The module-level default is also a stdlib logger.
    if isinstance(log, logging.Logger):
        _info = _stdlib_log(logging.INFO)
        _warn = _stdlib_log(logging.WARNING)

    if not repo:
        _info("knowledge_db_skip", reason="KNOWLEDGE_RELEASE_REPO not set")
        return False

    if expected_sha256 and not _normalise_sha256(expected_sha256):
        _warn("knowledge_db_digest_invalid", asset=asset_name)
        return False

    manifest_path = db_path.parent / manifest_name
    local_state = _read_local_manifest_state(manifest_path)
    local_tag = local_state.get("release_tag")

    if (
        release_tag
        and not force
        and local_tag == release_tag
        and db_path.exists()
        and _local_install_matches(
            local_state,
            asset_name=asset_name,
            archive_member=archive_member,
            projection=projection,
        )
        and (
            not expected_sha256
            or local_state.get("knowledge_asset_sha256") == _normalise_sha256(expected_sha256)
        )
    ):
        _info("knowledge_db_pinned_cache_hit", tag=release_tag, asset=asset_name)
        return False

    api_url = (
        f"https://api.github.com/repos/{repo}/releases/tags/{quote(release_tag, safe='')}"
        if release_tag
        else f"https://api.github.com/repos/{repo}/releases?per_page=100"
    )
    try:
        resp = httpx.get(api_url, timeout=_RELEASE_INFO_TIMEOUT, follow_redirects=True)
        resp.raise_for_status()
        payload = resp.json()
        if release_tag:
            release = payload
        elif isinstance(payload, list):
            release = next(
                (
                    item
                    for item in payload
                    if str(item.get("tag_name", "")).startswith("knowledge-db-")
                ),
                None,
            )
            if release is None:
                _warn("knowledge_db_release_missing", prefix="knowledge-db-")
                return False
        else:
            # Backward-compatible with older mocked callers and GitHub API
            # proxies that expose one release object.
            release = payload
    except Exception as exc:
        # Network unreachable / rate-limited / GitHub down. If we already
        # have a local DB on disk, keep using it instead of failing the
        # whole startup — far better than spinning up with an empty graph.
        if db_path.exists() and local_tag:
            _info(
                "knowledge_db_offline_using_local",
                tag=local_tag,
                asset=asset_name,
                error=str(exc),
            )
            return False
        _warn("knowledge_db_fetch_failed", error=str(exc))
        return False

    remote_tag = release.get("tag_name")
    if (
        not force
        and remote_tag
        and local_tag == remote_tag
        and db_path.exists()
        and _local_install_matches(
            local_state,
            asset_name=asset_name,
            archive_member=archive_member,
            projection=projection,
        )
    ):
        _info("knowledge_db_up_to_date", tag=remote_tag, asset=asset_name)
        return False
    if force:
        _info(
            "knowledge_db_refresh_forced",
            local_tag=local_tag,
            remote_tag=remote_tag,
            asset=asset_name,
        )

    asset_url = None
    asset_digest = ""
    for asset in release.get("assets", []):
        if asset.get("name") == asset_name:
            asset_url = asset.get("browser_download_url")
            asset_digest = _normalise_sha256(str(asset.get("digest", "")))
            break

    if not asset_url:
        _warn("knowledge_db_no_asset", release=remote_tag, asset=asset_name)
        return False

    expected_digest = _normalise_sha256(expected_sha256) or asset_digest
    if require_digest and not expected_digest:
        _warn(
            "knowledge_db_digest_missing",
            release=remote_tag,
            asset=asset_name,
        )
        return False

    _info("knowledge_db_downloading", url=asset_url)
    try:
        with tempfile.TemporaryDirectory() as tmp:
            tar_path = Path(tmp) / asset_name
            with httpx.stream(
                "GET", asset_url, timeout=_DOWNLOAD_TIMEOUT, follow_redirects=True
            ) as r:
                r.raise_for_status()
                with open(tar_path, "wb") as f:
                    for chunk in r.iter_bytes(chunk_size=65536):
                        f.write(chunk)

            actual_digest = _sha256_file(tar_path)
            if expected_digest and actual_digest != expected_digest:
                _warn(
                    "knowledge_db_digest_mismatch",
                    release=remote_tag,
                    asset=asset_name,
                    expected=expected_digest,
                    actual=actual_digest,
                )
                return False

            with tarfile.open(tar_path, "r:gz") as tf:
                for member in tf.getmembers():
                    if member.name.startswith("/") or ".." in member.name:
                        raise ValueError(f"Unsafe tar member: {member.name}")
                tf.extractall(tmp, filter="data")

            extracted_db = Path(tmp) / archive_member
            if not extracted_db.exists():
                _warn(
                    "knowledge_db_extract_failed",
                    reason=f"{archive_member} not found in archive",
                )
                return False

            extracted_manifest = Path(tmp) / "manifest.json"
            if not extracted_manifest.exists():
                extracted_manifest = _download_manifest_asset(
                    release,
                    tmp_dir=Path(tmp),
                )
            _install_atomically(extracted_db, db_path)
            if extracted_manifest.exists():
                _write_local_manifest(
                    extracted_manifest,
                    manifest_path,
                    release_tag=remote_tag,
                    asset_name=asset_name,
                    archive_member=archive_member,
                    projection=projection,
                    asset_sha256=actual_digest,
                )

            _info("knowledge_db_installed", tag=remote_tag, asset=asset_name)
            return True
    except Exception as exc:
        _warn("knowledge_db_download_failed", error=str(exc))
        return False


def _normalise_sha256(value: str) -> str:
    value = value.strip().lower()
    if value.startswith("sha256:"):
        value = value.removeprefix("sha256:")
    return value if len(value) == 64 and all(c in "0123456789abcdef" for c in value) else ""


def _sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _install_atomically(extracted_db: Path, db_path: Path) -> None:
    """Stage a database beside its destination and swap it into place.

    The previous install remains available for rollback until the new path has
    been activated. Keeping all rename operations on one filesystem makes the
    replacement safe on both Windows and POSIX hosts.
    """
    db_path.parent.mkdir(parents=True, exist_ok=True)
    token = uuid.uuid4().hex
    staging = db_path.with_name(f".{db_path.name}.install-{token}")
    backup = db_path.with_name(f".{db_path.name}.backup-{token}")
    if extracted_db.is_dir():
        shutil.copytree(extracted_db, staging)
    else:
        shutil.copy2(extracted_db, staging)

    had_existing = db_path.exists()
    try:
        if had_existing:
            os.replace(db_path, backup)
        os.replace(staging, db_path)
    except Exception:
        if staging.exists():
            _remove_existing_install(staging)
        if had_existing and backup.exists() and not db_path.exists():
            os.replace(backup, db_path)
        raise
    else:
        if backup.exists():
            _remove_existing_install(backup)
        # Sidecars belong to the old database handle and must not survive a
        # successful replacement.
        for sidecar in _candidate_sidecar_paths(db_path):
            if sidecar.exists():
                _remove_existing_install(sidecar)


def _local_install_matches(
    state: dict,
    *,
    asset_name: str,
    archive_member: str,
    projection: str,
) -> bool:
    """Return True when the local manifest describes the requested artifact.

    Older manifests do not carry these fields. They are treated as legacy
    runtime installs for backward compatibility, but never as compiler/v2
    installs; otherwise a v2 boot could incorrectly reuse a same-release
    legacy DB at the same ``GRAPH_DB_PATH``.
    """
    if asset_name == "knowledge_db.tar.gz" and archive_member == "knowledge_db":
        return state.get("knowledge_asset") in {None, "", asset_name}
    return (
        state.get("knowledge_asset") == asset_name
        and state.get("knowledge_archive_member") == archive_member
        and state.get("knowledge_projection") == projection
    )


def _remove_existing_install(db_path: Path) -> None:
    """Remove a DB install and known Ladybug sidecars before reinstalling."""
    if db_path.exists():
        if db_path.is_dir():
            shutil.rmtree(db_path)
        else:
            db_path.unlink()
    for sidecar in _candidate_sidecar_paths(db_path):
        if not sidecar.exists():
            continue
        if sidecar.is_dir():
            shutil.rmtree(sidecar)
        else:
            sidecar.unlink()


def _candidate_sidecar_paths(db_path: Path) -> list[Path]:
    """Return conservative file names Ladybug/Kuzu may create beside DB files."""
    names = (
        f"{db_path.name}.wal",
        f"{db_path.name}-wal",
        f"{db_path.name}.tmp",
        f"{db_path.name}.lock",
    )
    return [db_path.with_name(name) for name in names]


def _download_manifest_asset(release: dict, *, tmp_dir: Path) -> Path:
    """Download the standalone ``manifest.json`` asset for sidecar archives.

    The legacy runtime archive embeds the manifest. Compiler sidecars do not,
    so v2 installs fetch the manifest asset separately and stamp it next to
    the selected DB for the server's schema-version check.
    """
    manifest_url = None
    for asset in release.get("assets", []):
        if asset.get("name") == "manifest.json":
            manifest_url = asset.get("browser_download_url")
            break
    if not manifest_url:
        return tmp_dir / "manifest.json"

    manifest_path = tmp_dir / "manifest.json"
    with httpx.stream("GET", manifest_url, timeout=_DOWNLOAD_TIMEOUT, follow_redirects=True) as r:
        r.raise_for_status()
        with open(manifest_path, "wb") as f:
            for chunk in r.iter_bytes(chunk_size=65536):
                f.write(chunk)
    return manifest_path
