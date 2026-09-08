"""Script library management tools — scripts stored in the graph database."""

from __future__ import annotations

import json
import re
import time
from pathlib import Path
from typing import TYPE_CHECKING, Any

import structlog
from mcp.types import ToolAnnotations

from ..config import Settings
from .execution import _run_script

if TYPE_CHECKING:
    from ..graph.manager import GraphManager

logger = structlog.get_logger("tools.scripts")

_graph_manager: GraphManager | None = None


def _cypher_escape(value: str) -> str:
    """Escape a string for safe inclusion as a Cypher string literal.

    LadybugDB 0.15.x has a parameter-binding bug that segfaults when a
    STRING parameter looks like a JSON array of objects.  This helper
    lets us inline the value directly in the Cypher query instead.
    """
    return value.replace("\\", "\\\\").replace("'", "\\'")


def _cypher_string_list(values: list[str]) -> str:
    """Build a Cypher list literal from Python strings, escaping quotes.

    real_ladybug cannot bind Python lists as STRING[] parameters
    (triggers 'Trying to create a vector with ANY type' error).
    """
    if not values:
        return "CAST([] AS STRING[])"
    escaped = [v.replace("\\", "\\\\").replace("'", "\\'") for v in values]
    return "[" + ", ".join(f"'{e}'" for e in escaped) + "]"

def _validate_filename(filename: str) -> str | None:
    """Validate script filename. Returns error message or None if valid."""
    if not filename:
        return "Filename cannot be empty."
    if not filename.endswith(".py"):
        return "Filename must end with .py"
    if "/" in filename or "\\" in filename or ".." in filename:
        return "Filename must not contain path separators or '..'."
    if not re.match(r"^[a-zA-Z0-9_-]+\.py$", filename):
        return "Filename must contain only alphanumeric characters, underscores, and hyphens."
    return None


def register_script_tools(
    mcp,
    settings: Settings,
    graph_manager: GraphManager,
    offline_mode: bool = False,
    *,
    ipc_env: dict[str, str] | None = None,
):
    """Register script library management tools with the MCP server."""
    global _graph_manager
    _graph_manager = graph_manager

    @mcp.tool(
        annotations=ToolAnnotations(readOnlyHint=True, idempotentHint=True),
    )
    def list_scripts(tag: str | None = None) -> str:
        """List all scripts in the automation library.

        Use this before writing a new script to check if a reusable one already exists.

        Args:
            tag: Optional tag to filter by (e.g., "onboarding", "monitoring", "site-management").

        Returns:
            JSON list of scripts with name, description, tags, parameters, and last-run info.
        """
        gm = _graph_manager
        if gm is None or not gm.is_available:
            return json.dumps({"scripts": [], "message": "Graph database not available."})

        if tag:
            rows = gm.query(
                "MATCH (s:Script) WHERE list_contains(s.tags, $tag) "
                "RETURN s.filename, s.description, s.tags, s.parameters, "
                "s.created_at, s.last_run, s.last_exit_code ORDER BY s.filename",
                {"tag": tag},
                read_only=True,
            )
        else:
            rows = gm.query(
                "MATCH (s:Script) "
                "RETURN s.filename, s.description, s.tags, s.parameters, "
                "s.created_at, s.last_run, s.last_exit_code ORDER BY s.filename",
                read_only=True,
            )

        scripts = []
        for row in rows:
            params_raw = row.get("s.parameters", "[]")
            try:
                params = json.loads(params_raw) if params_raw else []
            except (json.JSONDecodeError, TypeError):
                params = []
            scripts.append({
                "filename": row.get("s.filename", ""),
                "description": row.get("s.description", "No description"),
                "tags": row.get("s.tags", []) or [],
                "parameters": params,
                "created_at": row.get("s.created_at"),
                "last_run": row.get("s.last_run"),
                "last_exit_code": row.get("s.last_exit_code"),
            })

        return json.dumps({"scripts": scripts, "total": len(scripts)}, indent=2)

    @mcp.tool(
        annotations=ToolAnnotations(readOnlyHint=True, idempotentHint=True),
    )
    def get_script_content(filename: str) -> str:
        """Read the source code of a script from the automation library.

        Use this to inspect how an existing script works before running it,
        or to learn patterns (e.g., how seed scripts use central_helpers.graph
        for graph enrichment). Also known as read_script.

        Args:
            filename: Script filename in the library (e.g., "populate_base_graph.py").

        Returns:
            The full Python source code of the script, or an error message.
        """
        error = _validate_filename(filename)
        if error:
            return json.dumps({"error": error})

        gm = _graph_manager
        if gm is None or not gm.is_available:
            return json.dumps({"error": "Graph database not available."})

        rows = gm.query(
            "MATCH (s:Script {filename: $fn}) RETURN s.content, s.description",
            {"fn": filename},
            read_only=True,
        )
        if not rows:
            return json.dumps({
                "error": f"Script '{filename}' not found.",
                "hint": "Use list_scripts() to see available scripts. Filenames must match [a-zA-Z0-9_-]+\\.py",
            })

        return json.dumps({
            "filename": filename,
            "description": rows[0].get("s.description", "No description"),
            "content": rows[0].get("s.content", ""),
        }, indent=2)

    @mcp.tool(
        annotations=ToolAnnotations(readOnlyHint=False, idempotentHint=True),
    )
    def save_script(
        filename: str,
        content: str,
        description: str,
        tags: list[str],
        parameters: list[dict[str, str]] | None = None,
        execute: bool = False,
    ) -> str:
        """Save a Python script to the automation library for reuse.

        BEFORE calling this tool you MUST have:
        1. Called list_scripts() to check for existing scripts
        2. Read the API endpoint catalog (`api://endpoint-catalog`) for
           relevant `METHOD /path` combinations
        3. Used ``query_graph`` against the `Parameter`/`RequestBody`/
           `Property` subgraph for the field-by-field guide of every
           endpoint the script will call (see `graph://schema` for canned
           patterns)

        Scripts should use ``from central_helpers import api`` for all API calls.
        Use ``api.paginate()`` for collection endpoints, ``api.get()`` for single-item lookups.
        Read docs://script-writing-guide for the full template.

        Args:
            filename: Script filename (e.g., "onboard_device.py"). Must end in .py.
            content: The full Python script content.
            description: Human-readable description of what the script does.
            tags: List of tags for categorization (e.g., ["onboarding", "glp"]).
            parameters: List of parameter definitions, each with keys:
                        name, type, description, required (bool), default (optional).
            execute: If True, execute the script immediately after saving and return
                     combined save + execution results.

        Returns:
            Confirmation message with the saved file path, or error details.
        """
        error = _validate_filename(filename)
        if error:
            return json.dumps({"error": error})

        gm = _graph_manager
        if gm is None or not gm.is_available:
            return json.dumps({"error": "Graph database not available."})

        created_at = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
        params_json = json.dumps(parameters or [])

        # ── LadybugDB workaround ────────────────────────────────
        # Passing a STRING parameter that resembles a JSON array of
        # objects triggers a segfault in real_ladybug 0.15.x.
        # We inline params_json as a Cypher string literal instead.
        escaped_params = _cypher_escape(params_json)

        # Preserve run metadata before deleting the existing node
        existing = gm.query(
            "MATCH (s:Script {filename: $fn}) RETURN s.last_run AS lr, s.last_exit_code AS ec",
            {"fn": filename},
            read_only=True,
        )
        prev_last_run = existing[0].get("lr") if existing else None
        prev_last_exit_code = existing[0].get("ec") if existing else None

        # Store script in graph database (DELETE + CREATE instead of
        # MERGE to sidestep a second LadybugDB planner crash).
        gm.execute(
            "MATCH (s:Script {filename: $fn}) DELETE s",
            {"fn": filename},
        )
        # Inline tags as Cypher list literal (real_ladybug can't bind lists)
        tags_lit = _cypher_string_list(tags)

        gm.execute(
            "CREATE (s:Script {"
            "filename: $fn, content: $content, description: $descr, "
            f"tags: {tags_lit}, parameters: '{escaped_params}', "
            "created_at: $created})",
            {
                "fn": filename,
                "content": content,
                "descr": description,
                "created": created_at,
            },
        )
        # Re-apply run metadata so history is not lost on save
        if prev_last_run is not None or prev_last_exit_code is not None:
            gm.execute(
                "MATCH (s:Script {filename: $fn}) SET s.last_run = $lr, s.last_exit_code = $ec",
                {"fn": filename, "lr": prev_last_run, "ec": prev_last_exit_code},
            )

        # Also write to disk so subprocess can execute it
        lib = settings.script_library_path
        lib.mkdir(parents=True, exist_ok=True)
        script_path = lib / filename
        script_path.write_text(content, encoding="utf-8")

        logger.info("script_saved", filename=filename, tags=tags)

        save_result = {
            "status": "saved",
            "filename": filename,
            "description": description,
        }

        if execute:
            if offline_mode:
                return json.dumps({
                    "error": (
                        "execute=True is not available in discovery-only mode. "
                        "Save the script, then run it from a connected workspace."
                    )
                })
            exec_result_json = _run_script(settings, filename, None, ipc_env=ipc_env)
            exec_result = json.loads(exec_result_json)
            save_result["execution"] = exec_result

            # Update run metadata in graph
            gm.execute(
                "MATCH (s:Script {filename: $fn}) "
                "SET s.last_run = $lr, s.last_exit_code = $ec",
                {
                    "fn": filename,
                    "lr": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
                    "ec": exec_result.get("exit_code", -1),
                },
            )

        return json.dumps(save_result, indent=2)


def sync_seeds_to_graph(graph_manager: GraphManager, seeds_dir: Path, lib_dir: Path) -> None:
    """Copy seed scripts into the graph DB and the script library on disk.

    Called at server startup to ensure seeds from the package are available
    both in the graph (for list_scripts/get_script_content) and on disk
    (for subprocess execution).
    """
    import shutil

    lib_dir.mkdir(parents=True, exist_ok=True)

    for seed_file in sorted(seeds_dir.iterdir()):
        if seed_file.suffix == ".py" and seed_file.name != "__init__.py":
            # Copy to disk (includes helper modules)
            shutil.copy2(seed_file, lib_dir / seed_file.name)

            # Skip private helper modules from graph registration
            if seed_file.name.startswith("_"):
                continue

            # Read metadata
            meta_path = seed_file.with_suffix(".meta.json")
            meta: dict[str, Any] = {}
            if meta_path.exists():
                meta = json.loads(meta_path.read_text(encoding="utf-8"))
                shutil.copy2(meta_path, lib_dir / meta_path.name)

            content = seed_file.read_text(encoding="utf-8")
            params_json = json.dumps(meta.get("parameters", []))
            escaped_params = _cypher_escape(params_json)

            # ── LadybugDB workaround ────────────────────────────────
            # Passing JSON-array strings as Cypher parameters triggers
            # segfaults or type-confusion in real_ladybug 0.15.x.
            # We inline params_json as a Cypher string literal and
            # use DELETE + CREATE instead of MERGE.

            # Preserve run metadata before deleting the existing node
            existing = graph_manager.query(
                "MATCH (s:Script {filename: $fn}) RETURN s.last_run AS lr, s.last_exit_code AS ec",
                {"fn": seed_file.name},
                read_only=True,
            )
            prev_last_run = existing[0].get("lr") if existing else None
            prev_last_exit_code = existing[0].get("ec") if existing else None

            graph_manager.execute(
                "MATCH (s:Script {filename: $fn}) DELETE s",
                {"fn": seed_file.name},
            )
            # Inline tags as Cypher list literal (real_ladybug can't bind lists)
            seed_tags_lit = _cypher_string_list(meta.get("tags") or [])
            graph_manager.execute(
                "CREATE (s:Script {"
                f"filename: $fn, description: $d, tags: {seed_tags_lit}, "
                f"content: $content, parameters: '{escaped_params}', "
                "created_at: $created})",
                {
                    "fn": seed_file.name,
                    "d": meta.get("description", "Seed script"),
                    "content": content,
                    "created": meta.get("created_at", time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())),
                },
            )
            # Re-apply run metadata so history is not lost on startup sync
            if prev_last_run is not None or prev_last_exit_code is not None:
                graph_manager.execute(
                    "MATCH (s:Script {filename: $fn}) SET s.last_run = $lr, s.last_exit_code = $ec",
                    {"fn": seed_file.name, "lr": prev_last_run, "ec": prev_last_exit_code},
                )
            logger.info("seed_synced", filename=seed_file.name)
