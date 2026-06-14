"""HPE Networking Central MCP Server - API Discovery + Code Interpreter Pattern."""

from __future__ import annotations

import argparse
import atexit
import graphlib
import json
import os
import shutil
import sys
import threading
from pathlib import Path

import httpx

from mcp.server.fastmcp import FastMCP

from .api_tree import render_path_tree
from .central_client import CentralAPIError, CentralClient, GreenLakeClient
from .config import load_settings
from .graph import GraphManager
from .graph.ipc_server import GraphIPCServer
from .instructions import build_instructions
from .knowledge_db import download_knowledge_db
from .logging import setup_logging
from .prompts.workflows import register_prompts
from .resources.docs import register_api_catalog_resource, register_resources
from .resources.graph import register_graph_resources
from .tools.api_call import register_api_call_tools, register_greenlake_api_call_tools
from .tools.compiler import register_compiler_tools
from .tools.execution import register_execution_tools, _run_script
from .tools.graph import register_graph_tools
from .tools.hydration import register_runtime_hydration_tools
from .tools.scripts import register_script_tools, sync_seeds_to_graph

logger = setup_logging()


def _parse_server_args() -> None:
    """Translate optional CLI args into the env vars that ``config.py`` reads.

    The MCP server has historically been configured exclusively through
    environment variables, which forces deployments (Claude Desktop, VS Code,
    docker-compose, ...) to duplicate every credential between the Docker
    ``-e VAR`` flag list and a separate JSON ``env`` block. Accepting the same
    values as proper CLI arguments lets the launcher pass everything in one
    flat ``args`` array and drop the ``env`` block entirely. CLI values, when
    provided, take precedence over any already-set env vars.

    Unknown arguments are tolerated (``parse_known_args``) so this never
    fights FastMCP's own argv handling.
    """
    parser = argparse.ArgumentParser(
        prog="hpe-networking-central-mcp",
        description=(
            "HPE Networking Central MCP server. With no credentials the server "
            "starts in discovery-only mode (knowledge DB + script CRUD only, "
            "no live Central / GreenLake API calls)."
        ),
        add_help=False,  # don't shadow FastMCP's own --help if it ever adds one
        allow_abbrev=False,  # prevent partial-flag matching from swallowing launcher args
    )
    parser.add_argument("--central-url", dest="central_url", default=None,
                        help="Central API base URL (sets CENTRAL_BASE_URL).")
    parser.add_argument("--client-id", dest="client_id", default=None,
                        help="Central OAuth2 client ID (sets CENTRAL_CLIENT_ID).")
    parser.add_argument("--client-secret", dest="client_secret", default=None,
                        help="Central OAuth2 client secret (sets CENTRAL_CLIENT_SECRET).")
    parser.add_argument("--glp-client-id", dest="glp_client_id", default=None,
                        help="GreenLake OAuth2 client ID (sets GREENLAKE_CLIENT_ID).")
    parser.add_argument("--glp-client-secret", dest="glp_client_secret", default=None,
                        help="GreenLake OAuth2 client secret (sets GREENLAKE_CLIENT_SECRET).")
    parser.add_argument("--read-only", dest="read_only", action="store_true",
                        help="Refuse mutating Central / GreenLake API calls (sets READ_ONLY=true).")
    args, _unknown = parser.parse_known_args()
    if args.central_url:
        os.environ["CENTRAL_BASE_URL"] = args.central_url
    if args.client_id:
        os.environ["CENTRAL_CLIENT_ID"] = args.client_id
    if args.client_secret:
        os.environ["CENTRAL_CLIENT_SECRET"] = args.client_secret
    if args.glp_client_id:
        os.environ["GREENLAKE_CLIENT_ID"] = args.glp_client_id
    if args.glp_client_secret:
        os.environ["GREENLAKE_CLIENT_SECRET"] = args.glp_client_secret
    if args.read_only:
        os.environ["READ_ONLY"] = "true"


_parse_server_args()
settings = load_settings()


def _is_recoverable_runtime_db_open_error(exc: BaseException) -> bool:
    """Return True for persisted LadybugDB failures that a fresh release can fix."""
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

# ── Credential gate: connected vs discovery-only mode ─────────────────
# When CENTRAL_BASE_URL / CENTRAL_CLIENT_ID / CENTRAL_CLIENT_SECRET are all
# present we run in connected mode and validate the token up front. With
# no credentials the server starts in *discovery-only* mode: it serves the
# knowledge DB (graph queries, embedded API catalog) and script CRUD, but
# does not register the tools that talk to live Central / GreenLake. This
# supports the workflow where an agent designs API calls and writes scripts
# the user reviews before running against a real environment.
# Detect partial / misconfigured credentials and surface the problem loudly
# rather than silently falling through to discovery-only mode.
_cred_set = bool(settings.central_base_url), bool(settings.central_client_id), bool(settings.central_client_secret)
if any(_cred_set) and not all(_cred_set):
    _missing = [name for name, present in zip(
        ("CENTRAL_BASE_URL", "CENTRAL_CLIENT_ID", "CENTRAL_CLIENT_SECRET"), _cred_set
    ) if not present]
    logger.error(
        "partial_credentials_detected",
        hint=(
            "Some but not all Central credentials are set. This is probably a "
            "configuration mistake. Provide all three (CENTRAL_BASE_URL, "
            "CENTRAL_CLIENT_ID, CENTRAL_CLIENT_SECRET) for connected mode, or "
            "none of them for discovery-only mode."
        ),
        missing=_missing,
    )
    sys.exit(1)

_offline_mode = not settings.has_credentials
client: CentralClient | None = None
if _offline_mode:
    logger.info(
        "discovery_only_mode_active",
        hint=(
            "No Central credentials configured. The server is running in "
            "discovery-only mode: query_graph, write_graph, list_scripts, "
            "get_script_content, and save_script are available; "
            "call_central_api, call_greenlake_api, and execute_script are not. "
            "Pass --central-url / --client-id / --client-secret (or the matching "
            "CENTRAL_* env vars) to enable connected mode."
        ),
    )
else:
    try:
        client = CentralClient(
            settings.central_base_url,
            settings.central_client_id,
            settings.central_client_secret,
        )
        client.validate()
        logger.info("credentials_validated")
    except (CentralAPIError, httpx.HTTPError, OSError) as exc:
        logger.error(
            "startup_failed",
            reason="Credential validation failed — could not obtain OAuth2 token.",
            error=str(exc),
        )
        sys.exit(1)
    except Exception:
        # Unexpected error (e.g. coding regression, missing dep) — surface the
        # full traceback rather than masking it as a credential failure.
        raise

# ── Download knowledge DB from GitHub release (if configured) ─────────
# Try to download knowledge DB before initializing graph
_knowledge_asset_name = (
    "knowledge_db_compiler.tar.gz"
    if settings.knowledge_projection == "v2"
    else "knowledge_db.tar.gz"
)
_knowledge_archive_member = (
    "knowledge_db_compiler"
    if settings.knowledge_projection == "v2"
    else "knowledge_db"
)


def _download_runtime_knowledge_db(*, force: bool = False) -> bool:
    return download_knowledge_db(
        settings.knowledge_release_repo,
        settings.graph_db_path,
        asset_name=_knowledge_asset_name,
        archive_member=_knowledge_archive_member,
        projection=settings.knowledge_projection,
        force=force,
        logger=logger,
    )


knowledge_downloaded = _download_runtime_knowledge_db()
logger.info(
    "knowledge_projection_selected",
    projection=settings.knowledge_projection,
    asset=_knowledge_asset_name,
    graph_db_path=str(settings.graph_db_path),
)

compiler_db_downloaded = False
compiler_ast_downloaded = False
if settings.compiler_tools:
    if settings.compiler_db_path == settings.graph_db_path and settings.knowledge_projection == "v2":
        logger.info(
            "compiler_db_reusing_runtime_projection",
            compiler_db_path=str(settings.compiler_db_path),
        )
    else:
        compiler_db_downloaded = download_knowledge_db(
            settings.knowledge_release_repo,
            settings.compiler_db_path,
            asset_name="knowledge_db_compiler.tar.gz",
            archive_member="knowledge_db_compiler",
            projection="v2",
            manifest_name="compiler_manifest.json",
            logger=logger,
        )
    compiler_ast_downloaded = download_knowledge_db(
        settings.knowledge_release_repo,
        settings.compiler_ast_db_path,
        asset_name="knowledge_db_ast.tar.gz",
        archive_member="knowledge_db_ast",
        projection="ast",
        manifest_name="ast_manifest.json",
        logger=logger,
    )
    logger.info(
        "compiler_tools_artifacts_selected",
        compiler_db_path=str(settings.compiler_db_path),
        ast_db_path=str(settings.compiler_ast_db_path),
        compiler_db_downloaded=compiler_db_downloaded,
        ast_downloaded=compiler_ast_downloaded,
    )

def _initialize_runtime_graph() -> tuple[GraphManager, bool]:
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
        redownloaded = _download_runtime_knowledge_db(force=True)
        if not redownloaded:
            logger.error(
                "knowledge_db_reinstall_failed",
                db_path=str(settings.graph_db_path),
                hint="Could not refresh the persisted knowledge DB after an open failure.",
            )
            raise
        recovered = GraphManager(settings.graph_db_path)
        recovered.initialize()
        logger.info(
            "knowledge_db_reinstall_recovered",
            db_path=str(settings.graph_db_path),
        )
        return recovered, True


# Initialize file-backed graph database
graph_manager, _knowledge_recovered = _initialize_runtime_graph()
knowledge_downloaded = knowledge_downloaded or _knowledge_recovered
graph_manager.create_fts_indexes()


# ── Knowledge DB schema-version check ────────────────────────────────
# Version 8 (ADR 009 Phase 2E) drops the skeleton/glossary/components
# JSON blob columns and the ApiEndpointSkeleton node table — all API
# discovery now flows through the Property/Parameter/SchemaComponent
# subgraph and the ``query_graph`` tool (see ADR 010).
# Version 9 adds the ``lastSyncedAt`` timestamp column to
# Site/SiteCollection/DeviceGroup/Device for freshness signalling.
# Existing databases are migrated in place via ``ALTER TABLE ... ADD``
# at startup, but the version bump triggers a clean rebuild for users
# who prefer it.
_KNOWLEDGE_SCHEMA_VERSION = 10


def _check_knowledge_schema_version() -> None:
    manifest_path = settings.graph_db_path.parent / "manifest.json"
    if not manifest_path.exists():
        logger.info("knowledge_manifest_missing", path=str(manifest_path))
        return
    try:
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    except Exception as exc:
        logger.warning("knowledge_manifest_unreadable", error=str(exc))
        return
    found = manifest.get("schema_version")
    if found != _KNOWLEDGE_SCHEMA_VERSION:
        msg = (
            f"Knowledge DB schema_version={found!r} does not match "
            f"server-required version {_KNOWLEDGE_SCHEMA_VERSION}. "
            "Re-run scripts/build_knowledge_db.py or wait for the next "
            "knowledge-db release. Refusing to start to avoid serving "
            "stale query_graph / pre-flight validator results against an "
            "out-of-date schema."
        )
        logger.error("knowledge_schema_version_mismatch", expected=_KNOWLEDGE_SCHEMA_VERSION, found=found)
        raise SystemExit(msg)


_check_knowledge_schema_version()


# ── Render API endpoint catalog as a path-tree for the system instructions ──
def _load_api_tree() -> str:
    """Query all ApiEndpoint rows and render them as a category-grouped path-tree.

    The result is embedded into the MCP server's system instructions so the
    agent always sees the full set of available endpoints without needing
    to call a search tool first.
    """
    try:
        rows = graph_manager.query(
            "MATCH (e:ApiEndpoint) "
            "RETURN e.method AS method, e.path AS path, "
            "e.category AS category, e.deprecated AS deprecated",
            read_only=True,
        )
    except Exception as exc:
        logger.warning("api_tree_query_failed", error=str(exc))
        return render_path_tree([], read_only=settings.read_only)

    text = render_path_tree(rows, read_only=settings.read_only)
    logger.info(
        "api_tree_rendered",
        endpoint_count=len(rows),
        chars=len(text),
        approx_tokens=len(text) // 4,
    )
    return text


_api_tree_text = _load_api_tree()

mcp = FastMCP(
    "hpe-networking-central-mcp",
    instructions=build_instructions(
        read_only=settings.read_only,
        api_tree=_api_tree_text,
        offline_mode=_offline_mode,
    ),
)

# Start IPC server for script subprocesses
ipc_server = GraphIPCServer(settings.graph_ipc_socket, graph_manager)
ipc_server.start()
atexit.register(ipc_server.stop)

# ── Optionally initialize GreenLake client ────────────────────────────────
glp_client: GreenLakeClient | None = None
if _offline_mode:
    logger.info("greenlake_disabled", reason="discovery_only_mode")
elif settings.has_glp_credentials:
    try:
        glp_client = GreenLakeClient(
            settings.glp_base_url,
            settings.effective_glp_client_id,
            settings.effective_glp_client_secret,
        )
        glp_client.validate()
        logger.info("glp_credentials_validated")
    except Exception as exc:
        logger.warning(
            "glp_validation_failed",
            error=str(exc),
            hint="GreenLake features will be unavailable. Central features still work.",
        )
        glp_client = None
else:
    logger.info("glp_credentials_not_configured", hint="GreenLake features disabled")

# Ensure script library exists and central_helpers.py + _http_core.py are available
settings.script_library_path.mkdir(parents=True, exist_ok=True)

_pkg_dir = Path(__file__).parent
for _helper_name in ("_http_core.py", "central_helpers.py"):
    _helpers_src = _pkg_dir / _helper_name
    _helpers_dst = settings.script_library_path / _helper_name
    if _helpers_src.exists():
        shutil.copy2(_helpers_src, _helpers_dst)
        logger.info("helper_copied", file=_helper_name, dest=str(_helpers_dst))

# Sync seed scripts into graph DB and disk library
_seeds_dir = Path(__file__).parent / "seeds"
if _seeds_dir.is_dir():
    sync_seeds_to_graph(graph_manager, _seeds_dir, settings.script_library_path)


# Run auto-run seed scripts in background to populate graph on startup
_seed_status: dict[str, dict] = {}  # filename -> {status, exit_code, error, started_at, finished_at}

def _get_auto_run_seeds() -> list[str]:
    """Return seed script filenames in dependency order (topological sort)."""
    lib = settings.script_library_path
    auto_seeds: dict[str, list[str]] = {}  # script_name -> depends_on
    for meta_file in sorted(lib.glob("*.meta.json")):
        try:
            meta = json.loads(meta_file.read_text(encoding="utf-8"))
            if meta.get("auto_run"):
                script_name = meta_file.name.replace(".meta.json", ".py")
                if (lib / script_name).exists():
                    deps = meta.get("depends_on", [])
                    auto_seeds[script_name] = deps
        except Exception:
            continue

    # Topological sort: only include dependencies that are in the auto_run set
    graph: dict[str, set[str]] = {}
    for name, deps in auto_seeds.items():
        valid_deps = {d for d in deps if d in auto_seeds}
        if len(valid_deps) < len(deps):
            missing = set(deps) - valid_deps
            logger.warning("seed_dep_not_auto_run", seed=name, missing=list(missing))
        graph[name] = valid_deps

    try:
        sorter = graphlib.TopologicalSorter(graph)
        ordered = list(sorter.static_order())
    except graphlib.CycleError as e:
        logger.error("seed_dependency_cycle", detail=str(e))
        ordered = sorted(auto_seeds.keys())  # fallback to alphabetical

    logger.info("auto_run_seed_order", order=ordered)
    return ordered


def _update_script_node(script_name: str, finished: str, exit_code: int):
    """Update the Script graph node's last_run/last_exit_code after seed execution."""
    try:
        # `query()` defaults to read_only=True and rejects SET; use execute()
        # for the write path.
        graph_manager.execute(
            "MATCH (s:Script {filename: $fn}) SET s.last_run = $lr, s.last_exit_code = $ec",
            {"fn": script_name, "lr": finished, "ec": exit_code},
        )
    except Exception as exc:
        logger.debug("script_node_update_failed", filename=script_name, error=str(exc))


def _bg_auto_run_seeds():
    """Execute all auto_run seed scripts sequentially in a background thread."""
    import time as _time

    for script_name in _get_auto_run_seeds():
        logger.info("auto_run_seed_start", filename=script_name)
        started = _time.strftime("%Y-%m-%dT%H:%M:%SZ", _time.gmtime())
        _seed_status[script_name] = {"status": "running", "started_at": started}
        try:
            result_json = _run_script(settings, script_name)
            result = json.loads(result_json)
            exit_code = result.get("exit_code", -1)
            finished = _time.strftime("%Y-%m-%dT%H:%M:%SZ", _time.gmtime())
            _update_script_node(script_name, finished, exit_code)
            if exit_code == 0:
                logger.info("auto_run_seed_done", filename=script_name)
                _seed_status[script_name] = {
                    "status": "success",
                    "exit_code": 0,
                    "started_at": started,
                    "finished_at": finished,
                }
            else:
                stderr = result.get("stderr", "")[:500]
                logger.warning(
                    "auto_run_seed_failed",
                    filename=script_name,
                    exit_code=exit_code,
                    stderr=stderr,
                )
                _seed_status[script_name] = {
                    "status": "failed",
                    "exit_code": exit_code,
                    "error": stderr or result.get("stdout", "")[:500],
                    "started_at": started,
                    "finished_at": finished,
                }
        except Exception as e:
            finished = _time.strftime("%Y-%m-%dT%H:%M:%SZ", _time.gmtime())
            logger.warning("auto_run_seed_error", filename=script_name, error=str(e))
            _seed_status[script_name] = {
                "status": "error",
                "error": str(e)[:500],
                "started_at": started,
                "finished_at": finished,
            }

    # Log summary at startup
    succeeded = sum(1 for s in _seed_status.values() if s["status"] == "success")
    failed = sum(1 for s in _seed_status.values() if s["status"] != "success")
    if failed:
        logger.error(
            "seed_startup_summary",
            succeeded=succeeded,
            failed=failed,
            failures={k: v.get("error", "") for k, v in _seed_status.items() if v["status"] != "success"},
        )
    else:
        logger.info("seed_startup_summary", succeeded=succeeded, failed=0)


# Register all components
# In discovery-only mode we deliberately omit the tools that need live
# Central / GreenLake credentials (call_central_api, call_greenlake_api)
# and the execute_script tool — the agent designs and saves scripts that
# the user reviews before running in a connected workspace.
register_graph_tools(mcp, settings, graph_manager)
if settings.compiler_tools:
    register_compiler_tools(mcp, settings)
    logger.info(
        "compiler_tools_registered",
        compiler_db_path=str(settings.compiler_db_path),
        ast_db_path=str(settings.compiler_ast_db_path),
    )
else:
    logger.info("compiler_tools_disabled", reason="MCP_COMPILER_TOOLS not enabled")
if settings.runtime_hydration:
    register_runtime_hydration_tools(mcp, settings)
    logger.info("runtime_hydration_tools_registered", stage="flag_shell")
else:
    logger.info("runtime_hydration_disabled", reason="MCP_RUNTIME_HYDRATION not enabled")
register_script_tools(mcp, settings, graph_manager, offline_mode=_offline_mode)
if _offline_mode:
    logger.info(
        "connected_tools_disabled",
        reason="discovery_only_mode",
        disabled=["execute_script", "call_central_api", "call_greenlake_api"],
    )
else:
    register_execution_tools(mcp, settings)
    register_api_call_tools(mcp, settings, client, graph_manager)
    if glp_client is not None:
        register_greenlake_api_call_tools(mcp, settings, glp_client, graph_manager)
    else:
        logger.info("greenlake_tools_disabled", reason="GreenLake credentials not configured")
register_resources(mcp, settings, graph_manager)
register_api_catalog_resource(mcp, settings, graph_manager)
register_graph_resources(mcp, graph_manager, lambda: _seed_status)
register_prompts(mcp, graph_manager)

# Start auto-run seeds in background AFTER tools are registered. Skipped in
# discovery-only mode because seeds populate the domain graph from live
# Central / GreenLake APIs.
if _offline_mode:
    logger.info("auto_run_seeds_skipped", reason="discovery_only_mode")
else:
    threading.Thread(target=_bg_auto_run_seeds, daemon=True).start()

logger.info(
    "server_ready",
    mode="discovery_only" if _offline_mode else "connected",
    credentials_configured=settings.has_credentials,
    glp_configured=glp_client is not None,
    knowledge_db_loaded=knowledge_downloaded,
    script_library=str(settings.script_library_path),
    read_only=settings.read_only,
)


def main():
    """Entry point for the MCP server."""
    mcp.run(transport="stdio")


if __name__ == "__main__":
    main()
