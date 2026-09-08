"""Unit tests for ``netops_api_navigator.knowledge_db.download_knowledge_db``.

Covers the extracted helper formerly inlined into ``server.py``. Uses
``httpx.MockTransport`` (no new dep) to drive the GitHub release API and the
asset download. No real network is touched.
"""

from __future__ import annotations

import hashlib
import io
import json
import tarfile

import httpx
import pytest

from netops_api_navigator import knowledge_db as kdb_mod
from netops_api_navigator.knowledge_db import download_knowledge_db

pytestmark = pytest.mark.unit


# --- helpers ---------------------------------------------------------------


def _make_tarball(members: dict[str, bytes] | None = None) -> bytes:
    """Build an in-memory tar.gz with the given members.

    Defaults to a tarball containing a ``knowledge_db/`` directory (one
    placeholder file inside) and a ``manifest.json`` sibling.
    """
    if members is None:
        members = {
            "knowledge_db/db.lbd": b"binary-db-bytes",
            "manifest.json": json.dumps({"schema_version": 3}).encode(),
        }
    buf = io.BytesIO()
    with tarfile.open(fileobj=buf, mode="w:gz") as tf:
        for name, data in members.items():
            info = tarfile.TarInfo(name=name)
            info.size = len(data)
            tf.addfile(info, io.BytesIO(data))
    return buf.getvalue()


def _release_payload(
    asset_url: str = "https://example.invalid/asset.tar.gz",
    tag: str = "knowledge-db-test",
    *,
    assets: list[dict] | None = None,
) -> dict:
    if assets is None:
        assets = [
            {
                "name": "knowledge_db.tar.gz",
                "browser_download_url": asset_url,
            }
        ]
    return {
        "tag_name": tag,
        "assets": assets,
    }


def _install_transport(monkeypatch: pytest.MonkeyPatch, handler) -> None:
    """Patch ``httpx.get`` and ``httpx.stream`` to use a MockTransport."""
    transport = httpx.MockTransport(handler)
    real_client_cls = httpx.Client

    def _get(url, **kwargs):
        with real_client_cls(transport=transport) as c:
            return c.get(url, **kwargs)

    def _stream(method, url, **kwargs):
        # Mimic httpx.stream context manager
        return real_client_cls(transport=transport).stream(method, url, **kwargs)

    monkeypatch.setattr(kdb_mod.httpx, "get", _get)
    monkeypatch.setattr(kdb_mod.httpx, "stream", _stream)


# --- tests -----------------------------------------------------------------


def test_returns_false_when_repo_empty(tmp_path):
    assert download_knowledge_db("", tmp_path / "kdb") is False


def test_invalid_explicit_digest_fails_before_network(tmp_path, monkeypatch):
    def unexpected_get(*args, **kwargs):
        raise AssertionError("invalid digest must fail before a network request")

    monkeypatch.setattr(kdb_mod.httpx, "get", unexpected_get)
    assert download_knowledge_db(
        "owner/repo",
        tmp_path / "kdb",
        expected_sha256="not-a-sha256",
    ) is False


def test_returns_false_when_release_api_fails(tmp_path, monkeypatch):
    def handler(request):
        return httpx.Response(500, text="boom")

    _install_transport(monkeypatch, handler)
    assert download_knowledge_db("owner/repo", tmp_path / "kdb") is False


def test_returns_false_when_no_matching_asset(tmp_path, monkeypatch):
    def handler(request):
        return httpx.Response(200, json={"tag_name": "x", "assets": [{"name": "other.zip"}]})

    _install_transport(monkeypatch, handler)
    assert download_knowledge_db("owner/repo", tmp_path / "kdb") is False


def test_unpinned_download_skips_newer_package_release(tmp_path, monkeypatch):
    tarball = _make_tarball()
    asset_url = "https://example.invalid/knowledge_db.tar.gz"

    def handler(request):
        url = str(request.url)
        if "api.github.com" in url:
            assert "/releases?per_page=100" in url
            return httpx.Response(
                200,
                json=[
                    _release_payload(
                        "https://example.invalid/package.whl",
                        tag="v0.3.0",
                        assets=[{"name": "package.whl"}],
                    ),
                    _release_payload(asset_url, tag="knowledge-db-selected"),
                ],
            )
        assert url == asset_url
        return httpx.Response(200, content=tarball)

    _install_transport(monkeypatch, handler)
    db_path = tmp_path / "kdb"
    assert download_knowledge_db("owner/repo", db_path) is True
    manifest = json.loads((tmp_path / "manifest.json").read_text())
    assert manifest["release_tag"] == "knowledge-db-selected"


def test_happy_path_extracts_db_and_manifest(tmp_path, monkeypatch):
    tarball = _make_tarball()
    asset_url = "https://example.invalid/knowledge_db.tar.gz"

    def handler(request):
        if "api.github.com" in str(request.url):
            return httpx.Response(200, json=_release_payload(asset_url))
        if str(request.url) == asset_url:
            return httpx.Response(200, content=tarball)
        return httpx.Response(404)

    _install_transport(monkeypatch, handler)

    db_path = tmp_path / "kdb"
    assert download_knowledge_db("owner/repo", db_path) is True
    assert db_path.exists() and db_path.is_dir()
    assert (db_path / "db.lbd").read_bytes() == b"binary-db-bytes"
    # manifest must be copied next to the db
    manifest = (tmp_path / "manifest.json")
    assert manifest.exists()
    assert json.loads(manifest.read_text())["schema_version"] == 3


def test_happy_path_extracts_compiler_sidecar_and_manifest_asset(tmp_path, monkeypatch):
    tarball = _make_tarball({"knowledge_db_compiler/db.lbd": b"compiler-db"})
    compiler_url = "https://example.invalid/knowledge_db_compiler.tar.gz"
    manifest_url = "https://example.invalid/manifest.json"
    manifest_payload = json.dumps({"schema_version": 10}).encode()

    def handler(request):
        url = str(request.url)
        if "api.github.com" in url:
            return httpx.Response(
                200,
                json=_release_payload(
                    compiler_url,
                    tag="knowledge-db-v2",
                    assets=[
                        {
                            "name": "knowledge_db_compiler.tar.gz",
                            "browser_download_url": compiler_url,
                        },
                        {
                            "name": "manifest.json",
                            "browser_download_url": manifest_url,
                        },
                    ],
                ),
            )
        if url == compiler_url:
            return httpx.Response(200, content=tarball)
        if url == manifest_url:
            return httpx.Response(200, content=manifest_payload)
        return httpx.Response(404)

    _install_transport(monkeypatch, handler)

    db_path = tmp_path / "graph.db"
    assert download_knowledge_db(
        "owner/repo",
        db_path,
        asset_name="knowledge_db_compiler.tar.gz",
        archive_member="knowledge_db_compiler",
        projection="v2",
    ) is True
    assert (db_path / "db.lbd").read_bytes() == b"compiler-db"
    manifest = json.loads((tmp_path / "manifest.json").read_text())
    assert manifest["schema_version"] == 10
    assert manifest["release_tag"] == "knowledge-db-v2"
    assert manifest["knowledge_asset"] == "knowledge_db_compiler.tar.gz"
    assert manifest["knowledge_archive_member"] == "knowledge_db_compiler"
    assert manifest["knowledge_projection"] == "v2"


def test_sidecar_manifest_name_does_not_clobber_runtime_manifest(tmp_path, monkeypatch):
    (tmp_path / "manifest.json").write_text(
        json.dumps({"schema_version": 10, "release_tag": "runtime"}),
        encoding="utf-8",
    )
    compiler_url = "https://example.invalid/knowledge_db_compiler.tar.gz"
    manifest_url = "https://example.invalid/manifest.json"

    def handler(request):
        url = str(request.url)
        if "api.github.com" in url:
            return httpx.Response(
                200,
                json=_release_payload(
                    compiler_url,
                    tag="knowledge-db-sidecar",
                    assets=[
                        {
                            "name": "knowledge_db_compiler.tar.gz",
                            "browser_download_url": compiler_url,
                        },
                        {
                            "name": "manifest.json",
                            "browser_download_url": manifest_url,
                        },
                    ],
                ),
            )
        if url == compiler_url:
            return httpx.Response(
                200,
                content=_make_tarball({"knowledge_db_compiler/db.lbd": b"compiler"}),
            )
        if url == manifest_url:
            return httpx.Response(
                200,
                content=json.dumps({"schema_version": 10}).encode(),
            )
        return httpx.Response(404)

    _install_transport(monkeypatch, handler)

    assert download_knowledge_db(
        "owner/repo",
        tmp_path / "knowledge_db_compiler",
        asset_name="knowledge_db_compiler.tar.gz",
        archive_member="knowledge_db_compiler",
        projection="v2",
        manifest_name="compiler_manifest.json",
    ) is True

    runtime_manifest = json.loads((tmp_path / "manifest.json").read_text())
    compiler_manifest = json.loads((tmp_path / "compiler_manifest.json").read_text())
    assert runtime_manifest["release_tag"] == "runtime"
    assert compiler_manifest["release_tag"] == "knowledge-db-sidecar"
    assert compiler_manifest["knowledge_asset"] == "knowledge_db_compiler.tar.gz"


def test_happy_path_replaces_existing_db_dir(tmp_path, monkeypatch):
    db_path = tmp_path / "kdb"
    db_path.mkdir()
    (db_path / "stale.txt").write_text("old")

    tarball = _make_tarball()
    asset_url = "https://example.invalid/knowledge_db.tar.gz"

    def handler(request):
        if "api.github.com" in str(request.url):
            return httpx.Response(200, json=_release_payload(asset_url))
        return httpx.Response(200, content=tarball)

    _install_transport(monkeypatch, handler)
    assert download_knowledge_db("owner/repo", db_path) is True
    assert not (db_path / "stale.txt").exists(), "stale dir contents must be wiped"
    assert (db_path / "db.lbd").exists()


def test_rejects_path_traversal_in_tar_member(tmp_path, monkeypatch):
    """Security: tar member with `..` must be rejected without extraction."""
    bad_tarball = _make_tarball({
        "../etc/passwd": b"pwned",
        "knowledge_db/db.lbd": b"x",
    })
    asset_url = "https://example.invalid/bad.tar.gz"

    def handler(request):
        if "api.github.com" in str(request.url):
            return httpx.Response(200, json=_release_payload(asset_url))
        return httpx.Response(200, content=bad_tarball)

    _install_transport(monkeypatch, handler)
    db_path = tmp_path / "kdb"
    # Should return False (caught + logged), not raise, not extract
    assert download_knowledge_db("owner/repo", db_path) is False
    assert not db_path.exists()


def test_rejects_absolute_tar_member(tmp_path, monkeypatch):
    bad_tarball = _make_tarball({"/abs/evil": b"x"})
    asset_url = "https://example.invalid/bad.tar.gz"

    def handler(request):
        if "api.github.com" in str(request.url):
            return httpx.Response(200, json=_release_payload(asset_url))
        return httpx.Response(200, content=bad_tarball)

    _install_transport(monkeypatch, handler)
    assert download_knowledge_db("owner/repo", tmp_path / "kdb") is False


def test_returns_false_when_archive_has_no_knowledge_db_dir(tmp_path, monkeypatch):
    tarball = _make_tarball({"unrelated/file.txt": b"x"})
    asset_url = "https://example.invalid/x.tar.gz"

    def handler(request):
        if "api.github.com" in str(request.url):
            return httpx.Response(200, json=_release_payload(asset_url))
        return httpx.Response(200, content=tarball)

    _install_transport(monkeypatch, handler)
    assert download_knowledge_db("owner/repo", tmp_path / "kdb") is False


# --- resume-on-restart / offline behaviour ---------------------------------


def test_skips_download_when_local_tag_matches_release(tmp_path, monkeypatch):
    """A second cold start that finds a manifest with the latest release_tag
    must not re-download the multi-MB tarball — the slow download is what
    blew Claude's MCP ``initialize`` budget on cold start."""
    db_path = tmp_path / "kdb"
    db_path.mkdir()
    (db_path / "db.lbd").write_bytes(b"existing")
    (tmp_path / "manifest.json").write_text(
        json.dumps({"release_tag": "knowledge-db-test", "schema_version": 8})
    )

    download_calls: list[str] = []

    def handler(request):
        url = str(request.url)
        if "api.github.com" in url:
            return httpx.Response(
                200,
                json=_release_payload(
                    "https://example.invalid/knowledge_db.tar.gz",
                    tag="knowledge-db-test",
                ),
            )
        download_calls.append(url)
        return httpx.Response(200, content=_make_tarball())

    _install_transport(monkeypatch, handler)
    # Returns False because no install was performed; existing DB remains.
    assert download_knowledge_db("owner/repo", db_path) is False
    assert download_calls == [], "must not hit the asset URL when local matches"
    assert (db_path / "db.lbd").read_bytes() == b"existing"


def test_force_refresh_reinstalls_when_local_tag_matches_release(tmp_path, monkeypatch):
    """Startup recovery can replace a tagged but corrupt persisted DB."""
    db_path = tmp_path / "kdb"
    db_path.mkdir()
    (db_path / "db.lbd").write_bytes(b"corrupt")
    (tmp_path / "kdb.wal").write_bytes(b"stale corrupt wal")
    (tmp_path / "manifest.json").write_text(
        json.dumps(
            {
                "release_tag": "knowledge-db-test",
                "schema_version": 10,
                "knowledge_asset": "knowledge_db_compiler.tar.gz",
                "knowledge_archive_member": "knowledge_db_compiler",
                "knowledge_projection": "v2",
            }
        )
    )

    asset_url = "https://example.invalid/knowledge_db_compiler.tar.gz"
    manifest_url = "https://example.invalid/manifest.json"
    download_calls: list[str] = []

    def handler(request):
        url = str(request.url)
        if "api.github.com" in url:
            return httpx.Response(
                200,
                json=_release_payload(
                    asset_url,
                    tag="knowledge-db-test",
                    assets=[
                        {
                            "name": "knowledge_db_compiler.tar.gz",
                            "browser_download_url": asset_url,
                        },
                        {
                            "name": "manifest.json",
                            "browser_download_url": manifest_url,
                        },
                    ],
                ),
            )
        download_calls.append(url)
        if url == asset_url:
            return httpx.Response(
                200,
                content=_make_tarball({"knowledge_db_compiler/db.lbd": b"fresh"}),
            )
        if url == manifest_url:
            return httpx.Response(200, content=json.dumps({"schema_version": 10}).encode())
        return httpx.Response(404)

    _install_transport(monkeypatch, handler)

    assert download_knowledge_db(
        "owner/repo",
        db_path,
        asset_name="knowledge_db_compiler.tar.gz",
        archive_member="knowledge_db_compiler",
        projection="v2",
        force=True,
    ) is True
    assert download_calls == [asset_url, manifest_url]
    assert (db_path / "db.lbd").read_bytes() == b"fresh"
    assert not (tmp_path / "kdb.wal").exists()


def test_v2_request_does_not_reuse_same_release_legacy_install(tmp_path, monkeypatch):
    db_path = tmp_path / "kdb"
    db_path.mkdir()
    (db_path / "db.lbd").write_bytes(b"legacy")
    (tmp_path / "manifest.json").write_text(
        json.dumps({"release_tag": "knowledge-db-test", "schema_version": 10})
    )
    compiler_url = "https://example.invalid/knowledge_db_compiler.tar.gz"
    manifest_url = "https://example.invalid/manifest.json"
    download_calls: list[str] = []

    def handler(request):
        url = str(request.url)
        if "api.github.com" in url:
            return httpx.Response(
                200,
                json=_release_payload(
                    compiler_url,
                    tag="knowledge-db-test",
                    assets=[
                        {
                            "name": "knowledge_db_compiler.tar.gz",
                            "browser_download_url": compiler_url,
                        },
                        {
                            "name": "manifest.json",
                            "browser_download_url": manifest_url,
                        },
                    ],
                ),
            )
        download_calls.append(url)
        if url == compiler_url:
            return httpx.Response(
                200,
                content=_make_tarball({"knowledge_db_compiler/db.lbd": b"compiler"}),
            )
        if url == manifest_url:
            return httpx.Response(200, content=json.dumps({"schema_version": 10}).encode())
        return httpx.Response(404)

    _install_transport(monkeypatch, handler)

    assert download_knowledge_db(
        "owner/repo",
        db_path,
        asset_name="knowledge_db_compiler.tar.gz",
        archive_member="knowledge_db_compiler",
        projection="v2",
    ) is True
    assert download_calls == [compiler_url, manifest_url]
    assert (db_path / "db.lbd").read_bytes() == b"compiler"


def test_falls_back_to_local_when_release_api_unreachable(tmp_path, monkeypatch):
    """Spotty network: GitHub API call raises. With a local DB on disk,
    keep using it instead of failing startup."""
    db_path = tmp_path / "kdb"
    db_path.mkdir()
    (db_path / "db.lbd").write_bytes(b"existing")
    (tmp_path / "manifest.json").write_text(
        json.dumps({"release_tag": "knowledge-db-old", "schema_version": 8})
    )

    def handler(request):
        raise httpx.ConnectError("network unreachable")

    _install_transport(monkeypatch, handler)
    # Returns False (no install) but does NOT raise; existing DB preserved.
    assert download_knowledge_db("owner/repo", db_path) is False
    assert (db_path / "db.lbd").read_bytes() == b"existing"


def test_install_writes_release_tag_into_manifest(tmp_path, monkeypatch):
    """The downloader must stamp the GitHub release ``tag_name`` into the
    on-disk manifest so the next cold start can short-circuit."""
    asset_url = "https://example.invalid/knowledge_db.tar.gz"

    def handler(request):
        if "api.github.com" in str(request.url):
            return httpx.Response(
                200, json=_release_payload(asset_url, tag="knowledge-db-stamped")
            )
        return httpx.Response(200, content=_make_tarball())

    _install_transport(monkeypatch, handler)
    db_path = tmp_path / "kdb"
    assert download_knowledge_db("owner/repo", db_path) is True

    manifest = json.loads((tmp_path / "manifest.json").read_text())
    assert manifest["release_tag"] == "knowledge-db-stamped"
    # Original schema_version field from the tarball manifest preserved.
    assert manifest["schema_version"] == 3


def test_pinned_cached_release_skips_network_entirely(tmp_path, monkeypatch):
    db_path = tmp_path / "kdb"
    db_path.mkdir()
    (db_path / "db.lbd").write_bytes(b"cached")
    digest = "a" * 64
    (tmp_path / "manifest.json").write_text(
        json.dumps(
            {
                "release_tag": "knowledge-db-pinned",
                "knowledge_asset": "knowledge_db.tar.gz",
                "knowledge_archive_member": "knowledge_db",
                "knowledge_projection": "legacy",
                "knowledge_asset_sha256": digest,
            }
        ),
        encoding="utf-8",
    )

    def handler(request):
        raise AssertionError(f"network must not be used for cached pin: {request.url}")

    _install_transport(monkeypatch, handler)
    assert download_knowledge_db(
        "owner/repo",
        db_path,
        release_tag="knowledge-db-pinned",
        expected_sha256=digest,
        require_digest=True,
    ) is False
    assert (db_path / "db.lbd").read_bytes() == b"cached"


def test_pinned_release_uses_tag_endpoint_and_verifies_digest(tmp_path, monkeypatch):
    tarball = _make_tarball()
    digest = hashlib.sha256(tarball).hexdigest()
    asset_url = "https://example.invalid/knowledge_db.tar.gz"

    def handler(request):
        url = str(request.url)
        if "api.github.com" in url:
            assert "/releases/tags/knowledge-db-pinned" in url
            return httpx.Response(
                200,
                json=_release_payload(
                    asset_url,
                    tag="knowledge-db-pinned",
                    assets=[
                        {
                            "name": "knowledge_db.tar.gz",
                            "browser_download_url": asset_url,
                            "digest": f"sha256:{digest}",
                        }
                    ],
                ),
            )
        return httpx.Response(200, content=tarball)

    _install_transport(monkeypatch, handler)
    db_path = tmp_path / "kdb"
    assert download_knowledge_db(
        "owner/repo",
        db_path,
        release_tag="knowledge-db-pinned",
        require_digest=True,
    ) is True
    manifest = json.loads((tmp_path / "manifest.json").read_text())
    assert manifest["knowledge_asset_sha256"] == digest


def test_digest_mismatch_preserves_existing_database(tmp_path, monkeypatch):
    db_path = tmp_path / "kdb"
    db_path.mkdir()
    (db_path / "db.lbd").write_bytes(b"existing")
    (tmp_path / "manifest.json").write_text(
        json.dumps({"release_tag": "knowledge-db-old"}), encoding="utf-8"
    )
    asset_url = "https://example.invalid/knowledge_db.tar.gz"

    def handler(request):
        if "api.github.com" in str(request.url):
            return httpx.Response(
                200,
                json=_release_payload(
                    asset_url,
                    tag="knowledge-db-new",
                    assets=[
                        {
                            "name": "knowledge_db.tar.gz",
                            "browser_download_url": asset_url,
                            "digest": f"sha256:{'0' * 64}",
                        }
                    ],
                ),
            )
        return httpx.Response(200, content=_make_tarball())

    _install_transport(monkeypatch, handler)
    assert download_knowledge_db(
        "owner/repo", db_path, require_digest=True
    ) is False
    assert (db_path / "db.lbd").read_bytes() == b"existing"
