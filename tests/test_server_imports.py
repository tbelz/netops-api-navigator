"""Server factory, lifecycle, and profile registration tests."""

from __future__ import annotations

import importlib
import json
import sys
from types import SimpleNamespace

import pytest

from netops_api_navigator import __version__
from netops_api_navigator.config import Settings

pytestmark = pytest.mark.unit


def _settings(tmp_path, **overrides) -> Settings:
    values = {
        "knowledge_release_repo": "",
        "graph_db_path": tmp_path / "graph.db",
        "script_library_path": tmp_path / "scripts",
        "docs_path": tmp_path / "docs",
        "spec_cache_path": tmp_path / "cache",
        "compiler_db_path": tmp_path / "compiler",
        "compiler_ast_db_path": tmp_path / "ast",
    }
    values.update(overrides)
    return Settings(**values)


def _tool_names(runtime) -> set[str]:
    return {tool.name for tool in runtime.mcp._tool_manager._tools.values()}


def test_server_module_import_has_no_runtime_side_effects(monkeypatch, tmp_path):
    monkeypatch.setenv("GRAPH_DB_PATH", str(tmp_path / "must-not-exist"))
    monkeypatch.setenv("KNOWLEDGE_RELEASE_REPO", "")
    sys.modules.pop("netops_api_navigator.server", None)

    module = importlib.import_module("netops_api_navigator.server")

    assert hasattr(module, "create_server")
    assert hasattr(module, "main")
    assert not (tmp_path / "must-not-exist").exists()
    assert not hasattr(module, "mcp")


def test_connected_server_factory_registers_connected_tools(tmp_path):
    from netops_api_navigator.server import create_server

    runtime = create_server(
        _settings(
            tmp_path,
            central_base_url="https://example.invalid",
            central_client_id="test",
            central_client_secret="test",
        ),
        validate_credentials=False,
        start_background=False,
    )
    try:
        names = _tool_names(runtime)
        assert runtime.offline_mode is False
        assert runtime.client is not None
        for expected in ("call_central_api", "execute_script", "query_graph", "write_graph"):
            assert expected in names
    finally:
        runtime.close()


def test_offline_full_profile_keeps_discovery_and_script_authoring(tmp_path):
    from netops_api_navigator.server import create_server

    runtime = create_server(_settings(tmp_path), start_background=False)
    try:
        names = _tool_names(runtime)
        assert runtime.offline_mode is True
        assert runtime.client is None
        for connected_only in ("call_central_api", "call_greenlake_api", "execute_script"):
            assert connected_only not in names
        for expected in ("query_graph", "write_graph", "list_scripts", "save_script"):
            assert expected in names
    finally:
        runtime.close()


def test_server_registers_compiler_tools_when_enabled(tmp_path):
    from netops_api_navigator.server import create_server

    runtime = create_server(
        _settings(tmp_path, compiler_tools=True),
        start_background=False,
    )
    try:
        names = _tool_names(runtime)
        assert "get_openapi_source_detail" in names
        assert "get_compiler_graph_health" in names
    finally:
        runtime.close()


def test_server_registers_runtime_hydration_when_enabled(tmp_path):
    from netops_api_navigator.server import create_server

    runtime = create_server(
        _settings(tmp_path, runtime_hydration=True),
        start_background=False,
    )
    try:
        names = _tool_names(runtime)
        for expected in (
            "get_runtime_hydration_status",
            "hydrate_runtime_endpoint",
            "plan_runtime_hydration",
            "materialize_runtime_facts",
            "promote_runtime_entities",
        ):
            assert expected in names
    finally:
        runtime.close()


def test_workshop_profile_exposes_only_safe_surface(tmp_path):
    from netops_api_navigator.server import create_server

    runtime = create_server(
        _settings(
            tmp_path,
            profile="workshop",
            central_base_url="https://example.invalid",
            central_client_id="test",
            central_client_secret="test",
            compiler_tools=True,
            runtime_hydration=True,
        ),
        validate_credentials=False,
        start_background=False,
    )
    try:
        assert _tool_names(runtime) == {
            "call_central_api",
            "get_raw_schema",
            "get_server_status",
            "paginate_central_api",
            "query_api_schema",
            "query_fts",
            "query_graph",
            "query_topology",
            "query_yang",
        }
        assert runtime.mcp._mcp_server.version == __version__
        assert runtime.settings.read_only is True
        assert runtime.settings.compiler_tools is False
        assert runtime.settings.runtime_hydration is False
        assert runtime.glp_client is None
        assert runtime.ipc_server is None
    finally:
        runtime.close()


def test_workshop_auto_seed_allowlist_excludes_user_scripts(tmp_path):
    from netops_api_navigator.server import (
        _WORKSHOP_AUTO_RUN_SEEDS,
        _get_auto_run_seeds,
    )

    settings = _settings(tmp_path, profile="workshop")
    settings.script_library_path.mkdir(parents=True)
    metadata = {
        "populate_base_graph.py": {"auto_run": True},
        "enrich_topology.py": {
            "auto_run": True,
            "depends_on": ["populate_base_graph.py"],
        },
        "user_script.py": {"auto_run": True},
    }
    for script_name, meta in metadata.items():
        (settings.script_library_path / script_name).write_text("pass\n", encoding="utf-8")
        (settings.script_library_path / script_name.replace(".py", ".meta.json")).write_text(
            json.dumps(meta),
            encoding="utf-8",
        )

    assert _get_auto_run_seeds(settings, _WORKSHOP_AUTO_RUN_SEEDS) == [
        "populate_base_graph.py",
        "enrich_topology.py",
    ]


def test_connected_workshop_starts_only_trusted_background_seeds(monkeypatch, tmp_path):
    import netops_api_navigator.server as server

    captured: dict = {}

    class _CapturedThread:
        def __init__(self, *, target, args, name, daemon):
            captured.update(target=target, args=args, name=name, daemon=daemon)

        def start(self):
            captured["started"] = True

    class _FakeIPCServer:
        def __init__(self, graph_manager):
            self.environment = {
                "GRAPH_IPC_HOST": "127.0.0.1",
                "GRAPH_IPC_PORT": "12345",
                "GRAPH_IPC_TOKEN": "test-token",
            }

        def start(self):
            captured["ipc_started"] = True

        def stop(self):
            captured["ipc_stopped"] = True

    monkeypatch.setattr(server.threading, "Thread", _CapturedThread)
    monkeypatch.setattr(server, "GraphIPCServer", _FakeIPCServer)
    runtime = server.create_server(
        _settings(
            tmp_path,
            profile="workshop",
            central_base_url="https://example.invalid",
            central_client_id="test",
            central_client_secret="test",
        ),
        validate_credentials=False,
        start_background=True,
    )
    try:
        assert runtime.ipc_server is not None
        assert captured["target"] is server._run_auto_seeds
        assert captured["args"][2] == server._WORKSHOP_AUTO_RUN_SEEDS
        assert captured["name"] == "netops-api-navigator-workshop-seeds"
        assert captured["daemon"] is True
        assert captured["started"] is True
        assert captured["ipc_started"] is True
    finally:
        runtime.close()
    assert captured["ipc_stopped"] is True


def test_auto_seed_reports_partial_success_when_summary_contains_errors(monkeypatch):
    import netops_api_navigator.server as server

    runtime = SimpleNamespace(settings=object(), graph_manager=object(), seed_status={})
    monkeypatch.setattr(server, "_get_auto_run_seeds", lambda settings, allowed=None: ["seed.py"])
    monkeypatch.setattr(
        server,
        "_run_script",
        lambda settings, script_name, ipc_env=None: json.dumps(
            {
                "exit_code": 0,
                "stdout": json.dumps({"items": 0, "errors": ["GET failed"]}),
                "stderr": "",
            }
        ),
    )
    monkeypatch.setattr(server, "_update_script_node", lambda *args: None)

    server._run_auto_seeds(runtime, {})

    assert runtime.seed_status["seed.py"]["status"] == "partial"
    assert runtime.seed_status["seed.py"]["summary"]["errors"] == ["GET failed"]


def test_explicit_knowledge_pin_rejects_stale_local_cache(tmp_path):
    from netops_api_navigator.server import StartupError, _check_knowledge_pin

    settings = _settings(tmp_path, knowledge_release_tag="knowledge-db-expected")
    settings.graph_db_path.parent.mkdir(parents=True, exist_ok=True)
    (settings.graph_db_path.parent / "manifest.json").write_text(
        json.dumps({"release_tag": "knowledge-db-old"}),
        encoding="utf-8",
    )

    with pytest.raises(StartupError, match="KNOWLEDGE_RELEASE_TAG"):
        _check_knowledge_pin(settings)
