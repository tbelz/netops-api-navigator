"""Real extension and pre-migration failure regressions (no Central access)."""
from __future__ import annotations

import hashlib
import shutil
from unittest.mock import Mock

import pytest

from netops_api_navigator import extensions
from netops_api_navigator.config import Settings
from netops_api_navigator.graph.manager import GraphManager
from netops_api_navigator.knowledge_db import select_knowledge_release

pytestmark = pytest.mark.unit


@pytest.mark.parametrize("failure", ["missing", "checksum", "engine", "platform", "load"])
def test_bad_extensions_do_not_touch_existing_graph_or_sidecars(tmp_path, monkeypatch, failure):
    from netops_api_navigator import server
    graph = tmp_path / "existing_graph"
    files = [graph, tmp_path / "existing_graph.wal", tmp_path / "existing_graph.shadow",
             tmp_path / "manifest.json"]
    for path in files:
        path.write_bytes(b"original graph evidence")
    before = {p: hashlib.sha256(p.read_bytes()).digest() for p in files}
    root = tmp_path / "extensions"
    shutil.copytree(extensions._ROOT, root)
    monkeypatch.setattr(extensions, "_ROOT", root)
    key = extensions.platform_key()
    if failure == "missing":
        (root / key / "libfts.lbug_extension").unlink()
    elif failure == "checksum":
        (root / key / "libfts.lbug_extension").write_bytes(b"not executable")
    elif failure == "engine":
        monkeypatch.setattr(extensions.lb, "__version__", "0.17.1")
    elif failure == "platform":
        monkeypatch.setattr(extensions, "platform_key", lambda: "unsupported")
    else:
        monkeypatch.setattr(extensions, "load_extensions",
                            Mock(side_effect=extensions.ExtensionError("cannot load fts")))
    initialize = Mock(side_effect=AssertionError("must not open persistent graph"))
    monkeypatch.setattr(server, "_initialize_runtime_graph", initialize)
    with pytest.raises(extensions.ExtensionError):
        server.create_server(Settings(graph_db_path=graph, knowledge_release_repo=""))
    initialize.assert_not_called()
    assert before == {p: hashlib.sha256(p.read_bytes()).digest() for p in files}


def test_bundled_fts_algo_and_indexed_seed_sync_survive_reopen(tmp_path):
    from netops_api_navigator.server import create_server
    from scripts.smoke_ladybug_extensions import smoke
    smoke(tmp_path / "extension-smoke")
    settings = Settings(graph_db_path=tmp_path / "graph",
                        script_library_path=tmp_path / "scripts", knowledge_release_repo="")
    for iteration in range(2):
        runtime = create_server(settings, start_background=False)
        try:
            gm = runtime.graph_manager
            assert gm.fts_available
            rows = gm.query("MATCH (s:Script) RETURN s.filename AS name")
            assert rows
            if iteration == 0:
                gm.execute("CREATE (s:Script {filename:'user.py',description:'retained script'})")
                gm.execute("MATCH (s:Script) WHERE s.filename <> 'user.py' "
                           "SET s.last_exit_code = 7")
            else:
                assert gm.query("MATCH (s:Script {filename:'user.py'}) RETURN s.description AS d"
                                ) == [{"d": "retained script"}]
                assert gm.query("MATCH (s:Script) WHERE s.filename <> 'user.py' "
                                "RETURN DISTINCT s.last_exit_code AS code") == [{"code": 7}]
            assert gm.query("CALL QUERY_FTS_INDEX('Script','script_fts','retained') "
                            "RETURN node.filename AS name") == [{"name": "user.py"}]
        finally:
            runtime.close()


def test_standalone_graph_manager_preflights_before_open(tmp_path, monkeypatch):
    monkeypatch.setattr(extensions.lb, "__version__", "wrong")
    path = tmp_path / "graph"
    with pytest.raises(extensions.ExtensionError, match="installed version"):
        GraphManager(path).initialize()
    assert not path.exists()


def test_release_channel_prefers_43_and_keeps_legacy_pins_separate():
    legacy = {"tag_name": "knowledge-db-newer"}
    current = {"tag_name": "knowledge-lbug43-older"}
    assert select_knowledge_release([
        {"tag_name": "knowledge-lbug43-draft", "draft": True},
        {"tag_name": "knowledge-lbug43-preview", "prerelease": True},
        legacy, current,
    ]) is current
    assert select_knowledge_release([legacy]) is legacy
    assert select_knowledge_release([{"tag_name": "v0.3.3"}]) is None
    # Old clients' published selector must never see the new format.
    assert not current["tag_name"].startswith("knowledge-db-")


def test_preparation_rejects_untrusted_archive_without_changing_files(tmp_path, monkeypatch):
    from scripts import prepare_extensions
    root = tmp_path / "bundle"
    root.mkdir()
    shutil.copy2(extensions._ROOT / "manifest.json", root / "manifest.json")
    archive = tmp_path / "corrupt.zip"
    archive.write_bytes(b"corrupt or substituted download")
    monkeypatch.setattr(prepare_extensions, "BUNDLE", root)
    with pytest.raises(ValueError, match="archive checksum"):
        prepare_extensions.prepare(archive)
    assert list(root.iterdir()) == [root / "manifest.json"]


def test_artifact_identity_includes_engine_and_storage_version(tmp_path, monkeypatch):
    from netops_api_navigator.compiler import artifact_cache
    before = artifact_cache.compiler_artifact_identity([], repo_root=tmp_path)
    monkeypatch.setattr(artifact_cache, "STORAGE_VERSION", 44)
    after = artifact_cache.compiler_artifact_identity([], repo_root=tmp_path)
    assert before["identity"] != after["identity"]
    assert before["storage_version"] == 43
