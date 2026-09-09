"""NetOps API Navigator lifecycle and CLI."""

from __future__ import annotations

import argparse
import graphlib
import json
import os
import platform
import shutil
import sys
import threading
from dataclasses import dataclass, field
from pathlib import Path
from typing import Sequence

import httpx
from mcp.server.fastmcp import FastMCP

from . import __version__
from .api_tree import render_path_tree
from .central_client import CentralAPIError, CentralClient, GreenLakeClient
from .config import Settings, load_settings
from .graph import GraphManager
from .graph.ipc_server import GraphIPCServer
from .instructions import build_instructions
from .knowledge_db import download_knowledge_db
from .logging import setup_logging
from .prompts.workflows import register_prompts
from .resources.docs import register_api_catalog_resource, register_resources
from .resources.graph import register_graph_resources
from .tools.api_call import (
    register_api_call_tools,
    register_greenlake_api_call_tools,
    register_workshop_api_call_tool,
)
from .tools.execution import _run_script, register_execution_tools
from .tools.graph import register_graph_tools
from .tools.scripts import register_script_tools, sync_seeds_to_graph
from .tools.status import register_status_tool

logger = setup_logging()
_KNOWLEDGE_SCHEMA_VERSION = 10
_WORKSHOP_GRAPH_TOOLS_TO_REMOVE = {"write_graph", "query_runtime"}
_WORKSHOP_AUTO_RUN_SEEDS = frozenset(
    {"populate_base_graph.py", "enrich_topology.py"}
)


class StartupError(RuntimeError):
    """Raised when the server cannot reach a safe, usable startup state."""


@dataclass
class ServerRuntime:
    """Resources owned by one configured MCP server instance."""

    settings: Settings
    mcp: FastMCP
    graph_manager: GraphManager
    client: CentralClient | None
    glp_client: GreenLakeClient | None
    offline_mode: bool
    knowledge_downloaded: bool
    ipc_server: GraphIPCServer | None = None
    seed_status: dict[str, dict] = field(default_factory=dict)

    def close(self) -> None:
        if self.ipc_server is not None:
            self.ipc_server.stop()
            self.ipc_server = None
        if self.client is not None:
            self.client.close()
        if self.glp_client is not None:
            self.glp_client.close()
        self.graph_manager.close()


def _credential_presence(settings: Settings) -> tuple[bool, bool, bool]:
    return (
        bool(settings.central_base_url),
        bool(settings.central_client_id),
        bool(settings.central_client_secret),
    )


def _create_central_client(
    settings: Settings, *, validate_credentials: bool
) -> tuple[CentralClient | None, bool]:
    presence = _credential_presence(settings)
    if any(presence) and not all(presence):
        names = ("CENTRAL_BASE_URL", "CENTRAL_CLIENT_ID", "CENTRAL_CLIENT_SECRET")
        missing = [name for name, present in zip(names, presence) if not present]
        raise StartupError(
            "Partial Central credentials detected. Provide all three Central "
            f"settings or none. Missing: {', '.join(missing)}"
        )
    if not settings.has_credentials:
        return None, True

    client = CentralClient(
        settings.central_base_url,
        settings.central_client_id,
        settings.central_client_secret,
    )
    if validate_credentials:
        try:
            client.validate()
        except (CentralAPIError, httpx.HTTPError, OSError) as exc:
            client.close()
            raise StartupError(
                "Central credential validation failed; could not obtain an OAuth2 token."
            ) from exc
    return client, False


def _create_greenlake_client(
    settings: Settings, *, offline_mode: bool, validate_credentials: bool
) -> GreenLakeClient | None:
    if offline_mode or settings.workshop_mode or not settings.has_glp_credentials:
        return None
    client = GreenLakeClient(
        settings.glp_base_url,
        settings.effective_glp_client_id,
        settings.effective_glp_client_secret,
    )
    if validate_credentials:
        try:
            client.validate()
        except Exception as exc:
            logger.warning(
                "glp_validation_failed",
                error=str(exc),
                hint="GreenLake features will be unavailable. Central features still work.",
            )
            client.close()
            return None
    return client


def _knowledge_artifact(settings: Settings) -> tuple[str, str]:
    if settings.knowledge_projection == "v2":
        return "knowledge_db_compiler.tar.gz", "knowledge_db_compiler"
    return "knowledge_db.tar.gz", "knowledge_db"


def _download_runtime_knowledge_db(settings: Settings, *, force: bool = False) -> bool:
    asset_name, archive_member = _knowledge_artifact(settings)
    return download_knowledge_db(
        settings.knowledge_release_repo,
        settings.graph_db_path,
        asset_name=asset_name,
        archive_member=archive_member,
        projection=settings.knowledge_projection,
        release_tag=settings.knowledge_release_tag,
        expected_sha256=settings.knowledge_asset_sha256,
        require_digest=bool(settings.knowledge_release_repo),
        force=force,
        logger=logger,
    )


def _is_recoverable_runtime_db_open_error(exc: BaseException) -> bool:
    if not isinstance(exc, (RuntimeError, OSError)):
        return False
    msg = str(exc).lower()
    return any(
        marker in msg
        for marker in (
            "corrupt",
            ".wal",
            "wal file",
            "invalid wal",
            "not a database",
            "reading past the end",
        )
    )


def _initialize_runtime_graph(settings: Settings) -> tuple[GraphManager, bool]:
    manager = GraphManager(settings.graph_db_path)
    try:
        manager.initialize()
        return manager, False
    except Exception as exc:
        if (
            not settings.knowledge_release_repo
            or not settings.graph_db_path.exists()
            or not _is_recoverable_runtime_db_open_error(exc)
        ):
            raise
        logger.warning(
            "knowledge_db_open_failed_reinstalling",
            db_path=str(settings.graph_db_path),
            error=str(exc),
        )
        if not _download_runtime_knowledge_db(settings, force=True):
            raise StartupError("Could not reinstall an unreadable knowledge database") from exc
        recovered = GraphManager(settings.graph_db_path)
        recovered.initialize()
        return recovered, True


def _check_knowledge_schema_version(settings: Settings) -> None:
    manifest_path = settings.graph_db_path.parent / "manifest.json"
    if not manifest_path.exists():
        logger.info("knowledge_manifest_missing", path=str(manifest_path))
        return
    try:
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    except Exception as exc:
        raise StartupError(f"Knowledge manifest is unreadable: {manifest_path}") from exc
    found = manifest.get("schema_version")
    if found != _KNOWLEDGE_SCHEMA_VERSION:
        raise StartupError(
            f"Knowledge DB schema_version={found!r} does not match required "
            f"version {_KNOWLEDGE_SCHEMA_VERSION}."
        )


def _check_knowledge_pin(settings: Settings) -> None:
    """Refuse an existing cache that does not match an explicit workshop pin."""
    if not settings.knowledge_release_tag and not settings.knowledge_asset_sha256:
        return
    manifest_path = settings.graph_db_path.parent / "manifest.json"
    try:
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    except Exception as exc:
        raise StartupError(
            "The installed Knowledge DB has no readable manifest for the requested pin."
        ) from exc
    if (
        settings.knowledge_release_tag
        and manifest.get("release_tag") != settings.knowledge_release_tag
    ):
        raise StartupError(
            "The installed Knowledge DB does not match KNOWLEDGE_RELEASE_TAG."
        )
    if (
        settings.knowledge_asset_sha256
        and manifest.get("knowledge_asset_sha256") != settings.knowledge_asset_sha256
    ):
        raise StartupError(
            "The installed Knowledge DB does not match KNOWLEDGE_ASSET_SHA256."
        )


def _load_api_tree(graph_manager: GraphManager, settings: Settings) -> str:
    try:
        rows = graph_manager.query(
            "MATCH (e:ApiEndpoint) RETURN e.method AS method, e.path AS path, "
            "e.category AS category, e.deprecated AS deprecated",
            read_only=True,
        )
    except Exception as exc:
        logger.warning("api_tree_query_failed", error=str(exc))
        rows = []
    return render_path_tree(rows, read_only=settings.read_only)


def _prepare_script_library(settings: Settings, graph_manager: GraphManager) -> None:
    settings.script_library_path.mkdir(parents=True, exist_ok=True)
    package_dir = Path(__file__).parent
    for helper_name in ("_http_core.py", "central_helpers.py"):
        source = package_dir / helper_name
        if source.exists():
            shutil.copy2(source, settings.script_library_path / helper_name)
    seeds_dir = package_dir / "seeds"
    if seeds_dir.is_dir():
        sync_seeds_to_graph(graph_manager, seeds_dir, settings.script_library_path)


def _get_auto_run_seeds(
    settings: Settings,
    allowed_scripts: frozenset[str] | None = None,
) -> list[str]:
    auto_seeds: dict[str, list[str]] = {}
    for meta_file in sorted(settings.script_library_path.glob("*.meta.json")):
        try:
            meta = json.loads(meta_file.read_text(encoding="utf-8"))
        except Exception:
            continue
        if meta.get("auto_run"):
            script_name = meta_file.name.replace(".meta.json", ".py")
            if allowed_scripts is not None and script_name not in allowed_scripts:
                continue
            if (settings.script_library_path / script_name).exists():
                auto_seeds[script_name] = meta.get("depends_on", [])

    graph = {
        name: {dependency for dependency in dependencies if dependency in auto_seeds}
        for name, dependencies in auto_seeds.items()
    }
    try:
        return list(graphlib.TopologicalSorter(graph).static_order())
    except graphlib.CycleError:
        logger.exception("seed_dependency_cycle")
        return sorted(auto_seeds)


def _update_script_node(
    graph_manager: GraphManager, script_name: str, finished: str, exit_code: int
) -> None:
    try:
        graph_manager.execute(
            "MATCH (s:Script {filename: $fn}) "
            "SET s.last_run = $lr, s.last_exit_code = $ec",
            {"fn": script_name, "lr": finished, "ec": exit_code},
        )
    except Exception as exc:
        logger.debug("script_node_update_failed", filename=script_name, error=str(exc))


def _run_auto_seeds(
    runtime: ServerRuntime,
    ipc_env: dict[str, str],
    allowed_scripts: frozenset[str] | None = None,
) -> None:
    import time

    script_names = _get_auto_run_seeds(runtime.settings, allowed_scripts)
    queued_at = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
    for script_name in script_names:
        runtime.seed_status[script_name] = {
            "status": "pending",
            "queued_at": queued_at,
        }

    for script_name in script_names:
        started = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
        runtime.seed_status[script_name] = {"status": "running", "started_at": started}
        try:
            result = json.loads(
                _run_script(runtime.settings, script_name, ipc_env=ipc_env)
            )
            exit_code = result.get("exit_code", -1)
            finished = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
            _update_script_node(runtime.graph_manager, script_name, finished, exit_code)
            entry = {
                "status": "success" if exit_code == 0 else "failed",
                "exit_code": exit_code,
                "started_at": started,
                "finished_at": finished,
            }
            stdout = result.get("stdout") or ""
            stderr = result.get("stderr") or ""
            try:
                summary = json.loads(stdout) if stdout else None
            except json.JSONDecodeError:
                summary = None
            if isinstance(summary, dict):
                entry["summary"] = summary
                reported_errors = summary.get("errors") or summary.get("error")
                if exit_code == 0 and reported_errors:
                    entry["status"] = "partial"
            if exit_code != 0:
                entry["error"] = (stderr or stdout)[:500]
            elif stderr:
                entry["diagnostics"] = stderr[:500]
            runtime.seed_status[script_name] = entry
        except Exception as exc:
            runtime.seed_status[script_name] = {
                "status": "error",
                "error": str(exc)[:500],
                "started_at": started,
            }


def _download_compiler_artifacts(settings: Settings) -> None:
    if not settings.compiler_tools:
        return
    if not (
        settings.compiler_db_path == settings.graph_db_path
        and settings.knowledge_projection == "v2"
    ):
        download_knowledge_db(
            settings.knowledge_release_repo,
            settings.compiler_db_path,
            asset_name="knowledge_db_compiler.tar.gz",
            archive_member="knowledge_db_compiler",
            projection="v2",
            manifest_name="compiler_manifest.json",
            release_tag=settings.knowledge_release_tag,
            require_digest=bool(settings.knowledge_release_repo),
            logger=logger,
        )
    download_knowledge_db(
        settings.knowledge_release_repo,
        settings.compiler_ast_db_path,
        asset_name="knowledge_db_ast.tar.gz",
        archive_member="knowledge_db_ast",
        projection="ast",
        manifest_name="ast_manifest.json",
        release_tag=settings.knowledge_release_tag,
        require_digest=bool(settings.knowledge_release_repo),
        logger=logger,
    )


def create_server(
    settings: Settings | None = None,
    *,
    validate_credentials: bool = True,
    start_background: bool = True,
) -> ServerRuntime:
    """Build one configured server without import-time side effects."""
    settings = settings or load_settings()
    client, offline_mode = _create_central_client(
        settings, validate_credentials=validate_credentials
    )
    glp_client: GreenLakeClient | None = None
    graph_manager: GraphManager | None = None
    runtime: ServerRuntime | None = None
    try:
        knowledge_downloaded = _download_runtime_knowledge_db(settings)
        if settings.knowledge_release_repo and not settings.graph_db_path.exists():
            raise StartupError(
                "Knowledge database is unavailable and no verified local cache exists."
            )
        _check_knowledge_pin(settings)
        _download_compiler_artifacts(settings)
        graph_manager, recovered = _initialize_runtime_graph(settings)
        knowledge_downloaded = knowledge_downloaded or recovered
        graph_manager.create_fts_indexes()
        _check_knowledge_schema_version(settings)

        glp_client = _create_greenlake_client(
            settings,
            offline_mode=offline_mode,
            validate_credentials=validate_credentials,
        )
        mcp = FastMCP(
            "netops-api-navigator",
            instructions=build_instructions(
                read_only=settings.read_only,
                api_tree=_load_api_tree(graph_manager, settings),
                offline_mode=offline_mode,
                workshop_mode=settings.workshop_mode,
            ),
        )
        # FastMCP 1.x does not expose the low-level Server version argument.
        # Set it explicitly so MCP initialization reports this package version
        # instead of falling back to the MCP SDK version.
        mcp._mcp_server.version = __version__
        runtime = ServerRuntime(
            settings=settings,
            mcp=mcp,
            graph_manager=graph_manager,
            client=client,
            glp_client=glp_client,
            offline_mode=offline_mode,
            knowledge_downloaded=knowledge_downloaded,
        )

        register_graph_tools(mcp, settings, graph_manager)
        if settings.workshop_mode:
            for tool_name in _WORKSHOP_GRAPH_TOOLS_TO_REMOVE:
                mcp.remove_tool(tool_name)
            ipc_env: dict[str, str] | None = None
            if client is not None and start_background:
                _prepare_script_library(settings, graph_manager)
                runtime.ipc_server = GraphIPCServer(graph_manager)
                runtime.ipc_server.start()
                ipc_env = runtime.ipc_server.environment
            if client is not None:
                register_workshop_api_call_tool(mcp, settings, client, graph_manager)
            register_status_tool(
                mcp,
                settings,
                graph_available=lambda: graph_manager.is_available,
                connected=client is not None and validate_credentials,
                seed_status=lambda: runtime.seed_status,
            )
            register_api_catalog_resource(mcp, settings, graph_manager)
            register_graph_resources(mcp, graph_manager, lambda: runtime.seed_status)
            if ipc_env is not None:
                threading.Thread(
                    target=_run_auto_seeds,
                    args=(runtime, ipc_env, _WORKSHOP_AUTO_RUN_SEEDS),
                    name="netops-api-navigator-workshop-seeds",
                    daemon=True,
                ).start()
        else:
            _prepare_script_library(settings, graph_manager)
            ipc_env: dict[str, str] | None = None
            if client is not None and start_background:
                runtime.ipc_server = GraphIPCServer(graph_manager)
                runtime.ipc_server.start()
                ipc_env = runtime.ipc_server.environment

            register_script_tools(
                mcp,
                settings,
                graph_manager,
                offline_mode=offline_mode,
                ipc_env=ipc_env,
            )
            if client is not None:
                register_execution_tools(mcp, settings, ipc_env=ipc_env)
                register_api_call_tools(mcp, settings, client, graph_manager)
                if glp_client is not None:
                    register_greenlake_api_call_tools(mcp, settings, glp_client, graph_manager)
            if settings.compiler_tools:
                from .tools.compiler import register_compiler_tools

                register_compiler_tools(mcp, settings)
            if settings.runtime_hydration:
                from .tools.hydration import register_runtime_hydration_tools

                register_runtime_hydration_tools(
                    mcp, settings, graph_manager, client, glp_client
                )
            register_status_tool(
                mcp,
                settings,
                graph_available=lambda: graph_manager.is_available,
                connected=client is not None and validate_credentials,
                seed_status=lambda: runtime.seed_status,
            )
            register_resources(mcp, settings, graph_manager)
            register_api_catalog_resource(mcp, settings, graph_manager)
            register_graph_resources(mcp, graph_manager, lambda: runtime.seed_status)
            register_prompts(mcp, graph_manager)
            if client is not None and start_background and ipc_env is not None:
                threading.Thread(
                    target=_run_auto_seeds,
                    args=(runtime, ipc_env),
                    name="netops-api-navigator-auto-seeds",
                    daemon=True,
                ).start()

        logger.info(
            "server_ready",
            profile=settings.profile,
            mode="discovery_only" if offline_mode else "connected",
            credentials_configured=settings.has_credentials,
            knowledge_db_loaded=knowledge_downloaded,
            read_only=settings.read_only,
        )
        return runtime
    except Exception:
        if runtime is not None:
            runtime.close()
        else:
            if glp_client is not None:
                glp_client.close()
            if client is not None:
                client.close()
            if graph_manager is not None:
                graph_manager.close()
        raise


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="netops-api-navigator",
        description="Run or diagnose the NetOps API Navigator.",
    )
    parser.add_argument("command", nargs="?", choices=("serve", "doctor"), default="serve")
    parser.add_argument("--version", action="version", version=f"%(prog)s {__version__}")
    parser.add_argument("--profile", choices=("full", "workshop"), default=None)
    parser.add_argument("--central-url", default=None)
    parser.add_argument("--client-id", default=None)
    parser.add_argument("--client-secret", default=None)
    parser.add_argument("--glp-client-id", default=None)
    parser.add_argument("--glp-client-secret", default=None)
    parser.add_argument("--read-only", action="store_true")
    parser.add_argument("--knowledge-release-tag", default=None)
    parser.add_argument("--knowledge-sha256", default=None)
    parser.add_argument(
        "--skip-credentials",
        action="store_true",
        help="Doctor only: validate local runtime without contacting Central.",
    )
    return parser


def _apply_cli_environment(args: argparse.Namespace) -> None:
    mappings = {
        "profile": "MCP_PROFILE",
        "central_url": "CENTRAL_BASE_URL",
        "client_id": "CENTRAL_CLIENT_ID",
        "client_secret": "CENTRAL_CLIENT_SECRET",
        "glp_client_id": "GREENLAKE_CLIENT_ID",
        "glp_client_secret": "GREENLAKE_CLIENT_SECRET",
        "knowledge_release_tag": "KNOWLEDGE_RELEASE_TAG",
        "knowledge_sha256": "KNOWLEDGE_ASSET_SHA256",
    }
    for attr, env_name in mappings.items():
        value = getattr(args, attr)
        if value is not None:
            os.environ[env_name] = value
    if args.read_only:
        os.environ["READ_ONLY"] = "true"


def run_doctor(settings: Settings, *, skip_credentials: bool) -> int:
    """Exercise the installed runtime and emit a secret-free JSON report."""
    result = {
        "status": "failed",
        "profile": settings.profile,
        "platform": platform.system(),
        "architecture": platform.machine(),
        "python": platform.python_version(),
        "paths": {
            "graph_db": str(settings.graph_db_path),
            "script_library": str(settings.script_library_path),
            "spec_cache": str(settings.spec_cache_path),
        },
        "credentials_checked": settings.has_credentials and not skip_credentials,
    }
    runtime: ServerRuntime | None = None
    try:
        if sys.version_info < (3, 12):
            raise StartupError("Python 3.12 or newer is required")
        runtime = create_server(
            settings,
            validate_credentials=not skip_credentials,
            start_background=False,
        )
        result.update(
            {
                "status": "ready",
                "graph_available": runtime.graph_manager.is_available,
                "knowledge_release_tag": _local_knowledge_tag(settings),
                "tool_count": len(runtime.mcp._tool_manager._tools),
            }
        )
        print(json.dumps(result, indent=2))
        return 0
    except Exception as exc:
        result["error"] = str(exc)
        print(json.dumps(result, indent=2))
        return 1
    finally:
        if runtime is not None:
            runtime.close()


def _local_knowledge_tag(settings: Settings) -> str | None:
    manifest_path = settings.graph_db_path.parent / "manifest.json"
    try:
        payload = json.loads(manifest_path.read_text(encoding="utf-8"))
    except Exception:
        return None
    value = payload.get("release_tag")
    return value if isinstance(value, str) else None


def main(argv: Sequence[str] | None = None) -> int:
    args = _build_parser().parse_args(argv)
    _apply_cli_environment(args)
    try:
        settings = load_settings()
    except (TypeError, ValueError) as exc:
        print(f"Configuration error: {exc}", file=sys.stderr)
        return 2
    if args.command == "doctor":
        return run_doctor(settings, skip_credentials=args.skip_credentials)

    runtime: ServerRuntime | None = None
    try:
        runtime = create_server(settings)
        runtime.mcp.run(transport="stdio")
        return 0
    except StartupError as exc:
        logger.error("startup_failed", error=str(exc))
        return 1
    finally:
        if runtime is not None:
            runtime.close()


if __name__ == "__main__":
    raise SystemExit(main())
